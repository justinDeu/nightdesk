from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from datetime import time as dtime
from pathlib import Path

import uvicorn

from nightdesk.api.app import create_app
from nightdesk.config import (
    DEFAULT_CONFIG_PATH,
    NightdeskConfig,
    load_config,
)
from nightdesk.db.session import make_engine, session_factory
from nightdesk.domain.profiles import seed_default_profiles
from nightdesk.worker.main import WorkerLoop, WorkerSettings, default_host


_DEFAULT_CONFIG_TOML = """\
# nightdesk configuration
bearer_token = ""
bind_host = "127.0.0.1"
bind_port = 8765
# db_path = "~/.local/share/nightdesk/nightdesk.db"
# transcript_root = "~/.local/share/nightdesk/transcripts"
# worktree_root = "~/.local/share/nightdesk-worktrees"
"""

# Minimum claude CLI version supported.
_CC_FLOOR = "2.1.80"

# Unit file templates.
_API_UNIT = """\
[Unit]
Description=nightdesk API server
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/nightdesk-api
Restart=on-failure
RestartSec=2
RestartPreventExitStatus=70
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=default.target
"""

_WORKER_UNIT = """\
[Unit]
Description=nightdesk worker daemon
After=network.target nightdesk-api.service

[Service]
Type=simple
ExecStart=%h/.local/bin/nightdesk-worker
Restart=on-failure
RestartSec=2
RestartPreventExitStatus=70
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=default.target
"""


def _alembic_config(cfg: NightdeskConfig):
    """Build an Alembic Config pointed at the user's DB.

    Returns None if alembic.ini cannot be found (e.g. installed wheel
    without the repo tree). The caller should fall back to
    ``Base.metadata.create_all`` in that case.
    """
    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    if not alembic_ini.exists():
        return None
    from alembic.config import Config as AlembicConfig

    engine = make_engine(cfg.db_path)
    try:
        alembic_cfg = AlembicConfig(str(alembic_ini))
        alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
        return alembic_cfg
    finally:
        engine.dispose()


def _run_migrations(cfg: NightdeskConfig) -> None:
    alembic_cfg = _alembic_config(cfg)
    if alembic_cfg is not None:
        from alembic import command as alembic_cmd

        alembic_cmd.upgrade(alembic_cfg, "head")
    else:
        engine = make_engine(cfg.db_path)
        try:
            from nightdesk.db.models import Base

            Base.metadata.create_all(engine)
        finally:
            engine.dispose()


def migrate() -> None:
    """CLI entry point for schema migrations.

    Default: ``nightdesk-migrate`` runs ``alembic upgrade head`` against the
    DB resolved from ``~/.config/nightdesk/config.toml``.

    Subcommands:
        nightdesk-migrate                 upgrade to latest (default)
        nightdesk-migrate up [rev]        upgrade to revision (default head)
        nightdesk-migrate down <rev>      downgrade to revision
        nightdesk-migrate current         print the DB's current revision
        nightdesk-migrate history         print the migration history
        nightdesk-migrate heads           print the latest revision id
        nightdesk-migrate stamp <rev>     stamp the DB at a revision (no DDL)

    Useful when the DB needs to be brought up to date without restarting
    the API/worker, or for inspecting where the schema currently sits.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="nightdesk-migrate")
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("up", help="upgrade to a revision (default head)").add_argument(
        "rev", nargs="?", default="head",
    )
    sub.add_parser("down", help="downgrade to a revision").add_argument("rev")
    sub.add_parser("current", help="show current DB revision")
    sub.add_parser("history", help="show migration history")
    sub.add_parser("heads", help="show latest available revision id")
    stamp = sub.add_parser("stamp", help="stamp DB at a revision (no DDL)")
    stamp.add_argument("rev")
    args = parser.parse_args()

    cfg = load_config()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    alembic_cfg = _alembic_config(cfg)
    if alembic_cfg is None:
        print("alembic.ini not found; falling back to metadata.create_all")
        from nightdesk.db.models import Base
        engine = make_engine(cfg.db_path)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        print(f"DB at {cfg.db_path} now matches the current schema.")
        return

    from alembic import command as alembic_cmd

    action = args.action or "up"
    rev = getattr(args, "rev", None) or "head"
    if action == "up":
        alembic_cmd.upgrade(alembic_cfg, rev)
        print(f"Upgraded {cfg.db_path} -> {rev}")
    elif action == "down":
        alembic_cmd.downgrade(alembic_cfg, rev)
        print(f"Downgraded {cfg.db_path} -> {rev}")
    elif action == "current":
        alembic_cmd.current(alembic_cfg, verbose=True)
    elif action == "history":
        alembic_cmd.history(alembic_cfg, verbose=False)
    elif action == "heads":
        alembic_cmd.heads(alembic_cfg, verbose=False)
    elif action == "stamp":
        alembic_cmd.stamp(alembic_cfg, rev)
        print(f"Stamped {cfg.db_path} at {rev} (no DDL)")
    else:
        parser.print_help()


def _init() -> NightdeskConfig:
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_CONFIG_PATH.exists():
        DEFAULT_CONFIG_PATH.write_text(_DEFAULT_CONFIG_TOML)
        print(f"Created default config at {DEFAULT_CONFIG_PATH}")

    cfg = load_config()

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.transcript_root.mkdir(parents=True, exist_ok=True)
    cfg.worktree_root.mkdir(parents=True, exist_ok=True)

    _run_migrations(cfg)
    seed_default_profiles(make_engine(cfg.db_path))
    return cfg


def init() -> None:
    """CLI entry point: create directories, default config, and run migrations."""
    cfg = _init()
    print(f"Ready. DB: {cfg.db_path}")


# ---------------------------------------------------------------------------
# Setup helpers.
# ---------------------------------------------------------------------------


def _check_platform() -> None:
    """Verify Linux. Exit 1 on any other OS."""
    if sys.platform != "linux":
        print(
            "nightdesk requires Linux. "
            f"Detected platform: {sys.platform}",
            file=sys.stderr,
        )
        sys.exit(1)


def _check_bwrap() -> None:
    """Verify bubblewrap is present. Print install hint and exit 1 if missing."""
    if shutil.which("bwrap") is not None:
        return
    print(
        "bubblewrap (bwrap) is required but not found on PATH.\n"
        "Install it with:\n"
        "  Arch:   pacman -S bubblewrap\n"
        "  Debian: apt install bubblewrap\n"
        "  Fedora: dnf install bubblewrap",
        file=sys.stderr,
    )
    sys.exit(1)


def _resolve_claude(claude_path: str | None) -> str:
    """Resolve and validate the claude binary. Exits on failure."""
    binary = claude_path or shutil.which("claude")
    if not binary:
        print(
            "claude binary not found. "
            "Install from https://docs.claude.com/en/docs/claude-code/quickstart",
            file=sys.stderr,
        )
        sys.exit(1)
    p = Path(binary)
    if not p.is_file():
        print(f"claude binary not found at {binary!r}.", file=sys.stderr)
        sys.exit(1)
    return str(p)


def _run_claude_version(binary: str) -> str:
    """Run ``claude --version``, return the parsed version string. Exits on failure."""
    from packaging.version import InvalidVersion, Version

    try:
        out = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        print(
            f"claude --version timed out. Is {binary!r} a valid claude binary?",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"Failed to run claude --version: {exc}", file=sys.stderr)
        sys.exit(1)

    text = (out.stdout + out.stderr).strip()
    m = re.search(r"\b(\d+\.\d+\.\d+)\b", text)
    if not m:
        print(
            f"Could not parse version from `claude --version` output: {text!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    version_str = m.group(1)
    try:
        actual = Version(version_str)
        floor = Version(_CC_FLOOR)
    except InvalidVersion as exc:
        print(f"Invalid version string: {exc}", file=sys.stderr)
        sys.exit(1)

    if actual < floor:
        print(
            f"claude {version_str} is below the minimum required version {_CC_FLOOR}.\n"
            "Run `claude update` or reinstall from "
            "https://docs.claude.com/en/docs/claude-code/quickstart",
            file=sys.stderr,
        )
        sys.exit(1)

    return version_str


def _write_config(token: str, force: bool) -> Path:
    """Write config.toml with the given bearer token. Returns config path."""
    config_path = DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if config_path.exists() and not force:
        print(f"Config exists at {config_path}, leaving as is.")
        return config_path

    content = _DEFAULT_CONFIG_TOML.replace('bearer_token = ""', f'bearer_token = "{token}"')
    config_path.write_text(content)
    config_path.chmod(0o600)
    if force:
        print(f"Config written (forced) to {config_path}.")
    else:
        print(f"Config written to {config_path}.")
    return config_path


def _create_data_dirs(worktree_root: Path) -> Path:
    """Create the standard data directories. Returns the data root.

    ``worktree_root`` is created separately because it lives outside the data
    dir (the sandbox cannot bind-mount paths under the data dir).
    """
    data_root = Path(os.path.expanduser("~/.local/share/nightdesk"))
    for sub in ("transcripts", "logs", "logs/runs"):
        (data_root / sub).mkdir(parents=True, exist_ok=True)
    worktree_root.mkdir(parents=True, exist_ok=True)
    return data_root


def _persist_setup_to_db(cfg: NightdeskConfig, binary: str, version: str) -> None:
    """Write DaemonStatus and ConfigRow rows after a successful setup."""
    from nightdesk.db.models import ConfigRow, DaemonStatus

    engine = make_engine(cfg.db_path)
    try:
        Session = session_factory(engine)
        with Session() as s:
            ds = s.get(DaemonStatus, 1)
            if ds is None:
                ds = DaemonStatus(id=1)
                s.add(ds)
            ds.cc_binary_path = binary
            ds.cc_version = version
            ds.cc_check_status = "ok"
            ds.cc_check_message = f"set during nightdesk setup (claude {version})"

            cr = s.get(ConfigRow, 1)
            if cr is None:
                cr = ConfigRow(
                    id=1,
                    worktree_root=str(cfg.worktree_root),
                    transcript_root=str(cfg.transcript_root),
                    max_run_duration_seconds=86400,
                    run_token_grace_seconds=300,
                )
                s.add(cr)
            cr.claude_binary_path = binary

            s.commit()
    finally:
        engine.dispose()


def _install_systemd_units(dry_run: bool) -> Path:
    """Write unit files into ~/.config/systemd/user/. Returns the unit dir."""
    unit_dir = Path(os.path.expanduser("~/.config/systemd/user"))
    if dry_run:
        print(f"[dry-run] would create directory {unit_dir}")
        print("[dry-run] would write nightdesk-api.service")
        print("[dry-run] would write nightdesk-worker.service")
        return unit_dir

    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "nightdesk-api.service").write_text(_API_UNIT)
    (unit_dir / "nightdesk-worker.service").write_text(_WORKER_UNIT)
    print(f"Unit files written to {unit_dir}.")
    return unit_dir


def _enable_systemd_units(dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] would run: systemctl --user daemon-reload")
        print("[dry-run] would run: systemctl --user enable --now nightdesk-api.service nightdesk-worker.service")
        return

    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        check=True,
    )
    subprocess.run(
        ["systemctl", "--user", "enable", "--now",
         "nightdesk-api.service", "nightdesk-worker.service"],
        check=True,
    )
    print("systemd units enabled and started.")


def _wait_for_api(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll GET /healthz until 200 or timeout. Returns True on success."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    url = f"http://{host}:{port}/healthz"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _mint_handshake(bearer: str, host: str, port: int) -> str | None:
    """POST to /auth/mint-handshake. Returns the browser URL or None on failure."""
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/auth/mint-handshake"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {bearer}", "Content-Length": "0"},
        data=b"",
    )
    try:
        import json
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
            return body.get("url")
    except Exception as exc:
        print(f"Could not mint handshake token: {exc}", file=sys.stderr)
        return None


def _open_browser(url: str) -> None:
    """Open url in the browser, non-blocking. Print if xdg-open is missing."""
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url])
    else:
        print(f"xdg-open not found. Open this URL in your browser:\n  {url}")


# ---------------------------------------------------------------------------
# `nightdesk setup` command.
# ---------------------------------------------------------------------------


def setup() -> None:
    """Install nightdesk: check deps, write config, install systemd units, open browser.

    Usage:
        nightdesk-setup
        nightdesk-setup --claude-path /usr/bin/claude
        nightdesk-setup --force          # overwrite existing config
        nightdesk-setup --dry-run        # print actions without doing them
    """
    import argparse

    parser = argparse.ArgumentParser(prog="nightdesk-setup")
    parser.add_argument(
        "--claude-path",
        metavar="PATH",
        help="Absolute path to the claude binary (default: auto-detect from PATH).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config.toml with a new bearer token.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without touching the filesystem or systemd.",
    )
    args = parser.parse_args()

    dry_run: bool = args.dry_run

    # -- Platform checks ---------------------------------------------------
    if dry_run:
        print("[dry-run] would check: Linux OS")
        print("[dry-run] would check: bwrap on PATH")
    else:
        _check_platform()
        _check_bwrap()

    # -- CC binary ---------------------------------------------------------
    if dry_run:
        binary = args.claude_path or shutil.which("claude") or "/usr/bin/claude"
        print(f"[dry-run] would resolve claude binary: {binary}")
        print(f"[dry-run] would run claude --version and verify >= {_CC_FLOOR}")
        version = _CC_FLOOR
    else:
        binary = _resolve_claude(args.claude_path)
        version = _run_claude_version(binary)
        print(f"claude {version} found at {binary}.")

    # -- Config ------------------------------------------------------------
    token = secrets.token_urlsafe(32)
    if dry_run:
        print(f"[dry-run] would write {DEFAULT_CONFIG_PATH} with new bearer token (chmod 600)")
        cfg = load_config() if DEFAULT_CONFIG_PATH.exists() else NightdeskConfig(
            bearer_token=token,
        )
    else:
        config_path = _write_config(token, force=args.force)
        cfg = load_config(config_path)
        # If config already existed (--force not given), load the existing token.
        if not args.force and cfg.bearer_token:
            token = cfg.bearer_token

    # -- Data directories --------------------------------------------------
    if dry_run:
        data_root = Path(os.path.expanduser("~/.local/share/nightdesk"))
        print(f"[dry-run] would create: {data_root}/{{transcripts,logs,logs/runs}} and {cfg.worktree_root}")
    else:
        data_root = _create_data_dirs(cfg.worktree_root)

    # -- Migrations --------------------------------------------------------
    if dry_run:
        print("[dry-run] would run: alembic upgrade head")
    else:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        _run_migrations(cfg)
        _persist_setup_to_db(cfg, binary, version)

    # -- Systemd -----------------------------------------------------------
    _install_systemd_units(dry_run)
    _enable_systemd_units(dry_run)

    if dry_run:
        print("[dry-run] would wait for API at /healthz then open browser.")
        print("\n[dry-run] setup complete (no changes made).")
        return

    # -- Wait for API and open browser -------------------------------------
    host = cfg.bind_host
    port = cfg.bind_port

    print(f"Waiting for API at http://{host}:{port}/healthz ...", end="", flush=True)
    if _wait_for_api(host, port):
        print(" ok.")
        url = _mint_handshake(token, host, port)
        if url:
            print(f"Opening browser: {url}")
            _open_browser(url)
        else:
            print(
                f"Could not mint a login URL. "
                f"Open http://{host}:{port}/auth/login and enter your bearer token."
            )
    else:
        print(" timed out.")
        print(
            f"API did not start in 5s. Check logs with:\n"
            f"  journalctl --user -u nightdesk-api -n 50\n"
            f"Then open http://{host}:{port}/auth/login and enter your bearer token."
        )

    # -- Summary -----------------------------------------------------------
    log_root = data_root / "logs"
    print(
        f"\nnightdesk setup complete.\n"
        f"  data:   {data_root}\n"
        f"  logs:   {log_root}\n"
        f"  db:     {cfg.db_path}\n"
        f"  api:    http://{host}:{port}\n"
        f"\nView logs:\n"
        f"  journalctl --user -u nightdesk-api\n"
        f"  journalctl --user -u nightdesk-worker\n"
        f"\nTo open the UI again: nightdesk-login"
    )


# ---------------------------------------------------------------------------
# `nightdesk stop` command.
# ---------------------------------------------------------------------------

_UNIT_NAMES = ("nightdesk-api.service", "nightdesk-worker.service")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """Run a systemctl --user command. Returns the CompletedProcess."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True,
    )


def _stop_systemd_units(dry_run: bool) -> None:
    """Stop and disable the nightdesk systemd user units. Idempotent."""
    if dry_run:
        print("[dry-run] would run: systemctl --user stop nightdesk-api.service nightdesk-worker.service")
        print("[dry-run] would run: systemctl --user disable nightdesk-api.service nightdesk-worker.service")
        print("[dry-run] would run: systemctl --user daemon-reload")
        return

    _systemctl("stop", *_UNIT_NAMES)
    _systemctl("disable", *_UNIT_NAMES)
    _systemctl("daemon-reload")
    print("systemd units stopped and disabled.")


def _remove_systemd_unit_files(dry_run: bool) -> None:
    """Remove the nightdesk unit files from ~/.config/systemd/user/."""
    unit_dir = Path(os.path.expanduser("~/.config/systemd/user"))
    for name in _UNIT_NAMES:
        path = unit_dir / name
        if path.exists():
            if dry_run:
                print(f"[dry-run] would remove: {path}")
            else:
                path.unlink()
                print(f"Removed {path}")
        else:
            print(f"Unit file not found (already removed): {path}")

    if not dry_run:
        _systemctl("daemon-reload")
        print("systemd daemon reloaded.")


def _is_service_active() -> bool:
    """Check whether any nightdesk service is currently active."""
    for name in _UNIT_NAMES:
        result = _systemctl("is-active", name)
        if result.stdout.strip() == "active":
            return True
    return False


def stop() -> None:
    """Stop and disable nightdesk services. Preserves all config and data.

    Usage:
        nightdesk-stop
        nightdesk-stop --dry-run
    """
    import argparse

    parser = argparse.ArgumentParser(prog="nightdesk-stop")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without touching systemd.",
    )
    args = parser.parse_args()

    _stop_systemd_units(args.dry_run)

    if args.dry_run:
        print("\n[dry-run] stop complete (no changes made).")
    else:
        print("\nServices stopped. Config and data are intact.")


# ---------------------------------------------------------------------------
# `nightdesk uninstall` command.
# ---------------------------------------------------------------------------


def uninstall() -> None:
    """Reverse nightdesk-setup: stop services, remove units and config.

    Data is PRESERVED by default. Use --purge-data / --purge-worktrees to
    remove data directories as well.

    Usage:
        nightdesk-uninstall
        nightdesk-uninstall --dry-run
        nightdesk-uninstall --purge-data
        nightdesk-uninstall --purge-data --purge-worktrees
    """
    import argparse

    parser = argparse.ArgumentParser(prog="nightdesk-uninstall")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every action without performing any.",
    )
    parser.add_argument(
        "--purge-data",
        action="store_true",
        help=(
            "Remove the data directory (~/.local/share/nightdesk/) including "
            "the database, transcripts, and logs."
        ),
    )
    parser.add_argument(
        "--purge-worktrees",
        action="store_true",
        help=(
            "Remove worktree directories (~/.local/share/nightdesk-worktrees/ "
            "and ~/.local/share/nightdesk-cc-sessions/)."
        ),
    )
    args = parser.parse_args()

    dry_run: bool = args.dry_run
    config_dir = Path(os.path.expanduser("~/.config/nightdesk"))
    config_path = DEFAULT_CONFIG_PATH
    secrets_path = Path(os.path.expanduser("~/.config/nightdesk/secrets.env"))
    data_dir = Path(os.path.expanduser("~/.local/share/nightdesk"))
    worktree_dir = Path(os.path.expanduser("~/.local/share/nightdesk-worktrees"))
    cc_sessions_dir = Path(os.path.expanduser("~/.local/share/nightdesk-cc-sessions"))

    # -- Guard: stop running services first --------------------------------
    if not dry_run and _is_service_active():
        print("nightdesk services are running. Stopping them first.")
        _stop_systemd_units(dry_run=False)
    elif dry_run:
        print("[dry-run] would check if services are running and stop them if so.")

    # -- Remove systemd unit files ------------------------------------------
    _remove_systemd_unit_files(dry_run)

    # -- Remove config files ------------------------------------------------
    for path, label in ((config_path, "config"), (secrets_path, "secrets.env")):
        if path.exists():
            if dry_run:
                print(f"[dry-run] would remove: {path}")
            else:
                path.unlink()
                print(f"Removed {label}: {path}")
        else:
            print(f"{label} not found (already removed): {path}")

    # Remove config dir if empty. In dry-run the files above are still on disk,
    # so discount the ones a real run would have just deleted; otherwise the
    # preview would say "not empty, preserving" when a real run would remove it.
    if config_dir.exists():
        would_remove = {config_path, secrets_path}
        remaining = [p for p in config_dir.iterdir() if p not in would_remove]
        if not remaining:
            if dry_run:
                print(f"[dry-run] would remove empty directory: {config_dir}")
            else:
                config_dir.rmdir()
                print(f"Removed empty config directory: {config_dir}")
        else:
            print(f"Config directory not empty, preserving: {config_dir} ({len(remaining)} items remain)")

    # -- Data removal (opt-in) ----------------------------------------------
    if args.purge_data:
        if data_dir.exists():
            if dry_run:
                print(f"[dry-run] would remove data directory: {data_dir}")
            else:
                print(f"WARNING: permanently deleting all nightdesk data at {data_dir} — this cannot be undone.")
                shutil.rmtree(data_dir)
                print(f"Removed data directory: {data_dir}")
        else:
            print(f"Data directory not found: {data_dir}")
    else:
        if data_dir.exists():
            print(f"Data directory preserved: {data_dir} (use --purge-data to remove)")
        else:
            print(f"Data directory not found: {data_dir}")

    if args.purge_worktrees:
        for d, label in ((worktree_dir, "worktree"), (cc_sessions_dir, "cc-sessions")):
            if d.exists():
                if dry_run:
                    print(f"[dry-run] would remove {label} directory: {d}")
                else:
                    print(f"WARNING: permanently deleting {label} directory at {d} — this cannot be undone.")
                    shutil.rmtree(d)
                    print(f"Removed {label} directory: {d}")
            else:
                print(f"{label} directory not found: {d}")
    else:
        if worktree_dir.exists():
            print(f"Worktree directory preserved: {worktree_dir} (use --purge-worktrees to remove)")
        if cc_sessions_dir.exists():
            print(f"CC sessions directory preserved: {cc_sessions_dir} (use --purge-worktrees to remove)")

    # -- Summary ------------------------------------------------------------
    if dry_run:
        print("\n[dry-run] uninstall complete (no changes made).")
    else:
        print("\nnightdesk uninstall complete.")
        print("  Removed: systemd units, config files")
        if args.purge_data:
            print("  Removed: data directory")
        else:
            print("  Preserved: data directory")
        if args.purge_worktrees:
            print("  Removed: worktree directories")
        else:
            print("  Preserved: worktree directories")


# ---------------------------------------------------------------------------
# `nightdesk login` command.
# ---------------------------------------------------------------------------


def login() -> None:
    """Mint a one-shot browser login URL and open it.

    Reads the bearer from config, calls POST /auth/mint-handshake, then
    opens the result URL with xdg-open (or prints it if unavailable).
    """
    cfg = load_config()
    if not cfg.bearer_token:
        print(
            "No bearer token in config. Run nightdesk-setup first.",
            file=sys.stderr,
        )
        sys.exit(1)

    host = cfg.bind_host
    port = cfg.bind_port

    url = _mint_handshake(cfg.bearer_token, host, port)
    if not url:
        print(
            f"Could not reach the nightdesk API at http://{host}:{port}.\n"
            "Is the daemon running? Try:\n"
            "  systemctl --user start nightdesk-api.service",
            file=sys.stderr,
        )
        sys.exit(1)

    _open_browser(url)
    print(f"Login URL: {url}")


# ---------------------------------------------------------------------------
# Daemon entrypoints.
# ---------------------------------------------------------------------------


def run_api() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="nightdesk-api")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Restart the server when source files change (development).",
    )
    args = parser.parse_args()

    cfg = load_config()

    from nightdesk.logging_setup import configure_root_logging
    configure_root_logging(component="api")

    # Auto-migrate on every start; exit 70 on failure so systemd won't loop.
    try:
        _run_migrations(cfg)
    except Exception:
        logging.exception("Migration failed; refusing to start API.")
        sys.exit(70)

    # Alembic's fileConfig resets the root logger level and disables loggers
    # not listed in alembic.ini. Re-apply our config so app logs get through.
    configure_root_logging(component="api")

    # CC version check (non-fatal: daemon still boots).
    from nightdesk.domain.cc_check import check_cc_binary, persist_cc_check

    try:
        engine = make_engine(cfg.db_path)
        result = check_cc_binary(cfg)
        if result.status.value != "ok":
            logging.basicConfig(level=logging.WARNING)
            logging.warning("CC check: %s — %s", result.status.value, result.message)
        Session = session_factory(engine)
        with Session() as s:
            persist_cc_check(result, s)
    except Exception:
        logging.basicConfig(level=logging.WARNING)
        logging.warning("CC check failed to run", exc_info=True)
    finally:
        try:
            engine.dispose()
        except Exception:
            pass

    if args.reload:
        src_dir = str(Path(__file__).parent)
        uvicorn.run(
            "nightdesk.api._factory:make",
            factory=True,
            host=cfg.bind_host,
            port=cfg.bind_port,
            log_level="info",
            reload=True,
            reload_dirs=[src_dir],
        )
        return

    engine = make_engine(cfg.db_path)
    static_root = Path(__file__).parent / "static"
    app = create_app(
        engine=engine,
        bearer_token=cfg.bearer_token,
        static_root=static_root,
        transcript_root=cfg.transcript_root,
        worktree_root=cfg.worktree_root,
        bind_host=cfg.bind_host,
        bind_port=cfg.bind_port,
    )
    uvicorn.run(app, host=cfg.bind_host, port=cfg.bind_port, log_level="info")


def run_worker() -> None:
    cfg = load_config()
    from nightdesk.logging_setup import configure_root_logging
    configure_root_logging(component="worker")

    # Auto-migrate on every start; exit 70 on failure so systemd won't loop.
    try:
        _run_migrations(cfg)
    except Exception:
        logging.exception("Migration failed; refusing to start worker.")
        sys.exit(70)

    # Alembic's fileConfig resets the root logger level and disables loggers
    # not listed in alembic.ini. Re-apply our config so app logs get through.
    configure_root_logging(component="worker")

    # CC version check (non-fatal: worker still starts; tick loop guards picks).
    from nightdesk.domain.cc_check import check_cc_binary, persist_cc_check

    try:
        engine_cc = make_engine(cfg.db_path)
        result = check_cc_binary(cfg)
        if result.status.value != "ok":
            logging.warning("CC check: %s — %s", result.status.value, result.message)
        Session_cc = session_factory(engine_cc)
        with Session_cc() as s:
            persist_cc_check(result, s)
    except Exception:
        logging.warning("CC check failed to run", exc_info=True)
    finally:
        try:
            engine_cc.dispose()
        except Exception:
            pass

    engine = make_engine(cfg.db_path)
    SessionLocal = session_factory(engine)

    settings = WorkerSettings(
        max_parallel=2,
        window_start=dtime(22, 0),
        window_end=dtime(7, 0),
        worktree_root=cfg.worktree_root,
        transcript_root=cfg.transcript_root,
        secrets=cfg.secrets,
        host=default_host(),
        # executor=None -> worker resolves per ticket via profile.backend.
    )
    loop = WorkerLoop(session_factory=lambda: SessionLocal(), settings=settings)
    asyncio.run(loop.run_forever())


def run_ticket() -> None:
    """CLI entry point: run a single ticket as a subprocess.

    This is the unit of execution. The daemon's tick spawns
    ``nightdesk-run-ticket <id>`` for each pick; you can also invoke it
    by hand to retry a failed run or to step through a new profile.

    Usage:
        nightdesk-run-ticket <ticket_id>
        nightdesk-run-ticket --dump <ticket_id>     # print bwrap argv + spec, don't run
    """
    import argparse

    parser = argparse.ArgumentParser(prog="nightdesk-run-ticket")
    parser.add_argument("ticket_id")
    parser.add_argument("--dump", action="store_true",
                        help="print the resolved spec and bwrap argv, then exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = load_config()
    engine = make_engine(cfg.db_path)
    SessionLocal = session_factory(engine)

    from nightdesk.db.models import Ticket
    from nightdesk.domain.tickets import transition_status as _trans
    from nightdesk.worker.run_one import (
        RunOneConfig, _build_env, _profile_to_spec, run_one,
    )
    from nightdesk.worker.sandbox import build_bwrap_argv

    if args.dump:
        with SessionLocal() as s:
            t = s.get(Ticket, args.ticket_id)
            if t is None:
                print(f"ticket {args.ticket_id!r} not found", file=sys.stderr)
                sys.exit(2)
            from nightdesk.domain.profile_secrets import ProfileSecretBox
            secret_box = ProfileSecretBox(cfg.bearer_token) if cfg.bearer_token else None
            spec = _profile_to_spec(t, secret_box=secret_box)
            env = _build_env(spec, cfg.secrets, run_token="ndr_DUMP", run_id="DUMP",
                             ticket_id=t.id, api_url=f"http://{cfg.bind_host}:{cfg.bind_port}")
            argv = build_bwrap_argv(
                spec,
                cwd=str(t.cwd or (spec.fs_write[0] if spec.fs_write else "/tmp")),
                cmd=["python", "-m", "nightdesk.worker._sdk_runner"],
                env=env,
            )
            print("=== profile.backend ===", spec.backend)
            print("=== profile.permission_mode ===", spec.permission_mode)
            print("=== profile.default_model ===", spec.default_model)
            print("=== fs_write ===", spec.fs_write)
            print("=== fs_read ===", spec.fs_read)
            print("=== env keys (values redacted) ===", sorted(env))
            print("=== bwrap argv ===")
            print(" ".join(repr(a) if " " in a else a for a in argv))
        return

    # Force the ticket into 'running' so the status invariants hold for
    # the cancel-watcher / SSE / pill. The daemon's tick does this same
    # transition just before spawning us; doing it here lets the CLI also
    # be invoked manually for queued/draft tickets.
    with SessionLocal() as s:
        t = s.get(Ticket, args.ticket_id)
        if t is None:
            print(f"ticket {args.ticket_id!r} not found", file=sys.stderr)
            sys.exit(2)
        if t.status != "running":
            try:
                _trans(s, t.id, "running")
            except Exception as exc:
                print(f"cannot transition ticket {t.id} from {t.status!r} "
                      f"to running: {exc}", file=sys.stderr)
                sys.exit(2)
        print(f"running ticket {t.id} ({t.title!r}) via profile={t.profile.name} "
              f"backend={getattr(t.profile, 'backend', None)} pid={os.getpid()}")

    rcfg = RunOneConfig(
        worktree_root=cfg.worktree_root,
        transcript_root=cfg.transcript_root,
        secrets=cfg.secrets,
        host=default_host(),
        bearer_token=cfg.bearer_token,
        api_url=f"http://{cfg.bind_host}:{cfg.bind_port}",
    )

    # Lifecycle hardening for the per-run subprocess:
    # - setsid: make us a process group leader so SIGTERM to this PID
    #   reaches the bwrap child via the same pgrp.
    # - PR_SET_PDEATHSIG: kernel sends us SIGTERM the moment the parent
    #   worker dies, which in turn kills bwrap via --die-with-parent.
    try:
        os.setsid()
    except OSError:
        pass
    try:
        import ctypes
        PR_SET_PDEATHSIG = 1
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
            logging.warning("prctl(PR_SET_PDEATHSIG) failed; orphan-on-worker-crash possible")
    except Exception:
        logging.warning("prctl unavailable; orphan-on-worker-crash possible")

    result = asyncio.run(run_one(lambda: SessionLocal(), rcfg, args.ticket_id))
    print(f"done. exit_status={result.exit_status} "
          f"error={result.error_summary!r}")
    if result.exit_status not in ("success",):
        sys.exit(1)


def run_dev() -> None:
    cfg = _init()
    src_dir = str(Path(__file__).parent)

    print(f"nightdesk dev  http://{cfg.bind_host}:{cfg.bind_port}")

    api_proc = subprocess.Popen([
        sys.executable, "-m", "uvicorn",
        "nightdesk.api._factory:make", "--factory",
        "--host", cfg.bind_host,
        "--port", str(cfg.bind_port),
        "--reload",
        "--reload-dir", src_dir,
    ])

    from watchfiles import PythonFilter, run_process
    try:
        run_process(src_dir, target=run_worker, watch_filter=PythonFilter())
    finally:
        api_proc.terminate()
