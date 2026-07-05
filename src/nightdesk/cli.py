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
    default_config_path,
    default_secrets_path,
    load_config,
)
from nightdesk.db.session import make_engine, session_factory
from nightdesk.domain.labels import seed_default_labels
from nightdesk.domain.profiles import seed_default_profiles
from nightdesk.worker.main import WorkerLoop, WorkerSettings, default_host


_DEFAULT_CONFIG_TOML = """\
# nightdesk configuration
bearer_token = ""
bind_host = "127.0.0.1"
bind_port = 8765
# data_dir = "~/.local/share/nightdesk"
# log_dir = "~/.local/share/nightdesk/logs"
# db_path = "~/.local/share/nightdesk/nightdesk.db"
# transcript_root = "~/.local/share/nightdesk/transcripts"
# worktree_root = "~/.local/share/nightdesk-worktrees"
"""

# Minimum claude CLI version supported.
_CC_FLOOR = "2.1.80"

# Unit file templates. ``{exec_start}`` is filled in at install time with the
# absolute path of the matching console script, resolved from wherever the
# package was actually installed (see ``_resolve_entrypoint``).
_API_UNIT_TEMPLATE = """\
[Unit]
Description=nightdesk API server
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=2
RestartPreventExitStatus=70
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=default.target
"""

_WORKER_UNIT_TEMPLATE = """\
[Unit]
Description=nightdesk worker daemon
After=network.target nightdesk-api.service

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=2
RestartPreventExitStatus=70
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=default.target
"""


def _entrypoint_dir_candidates() -> list[Path]:
    """Directories likely to hold the nightdesk console scripts, in priority order.

    ``nightdesk-setup`` is itself a console script, so its own directory
    (``sys.argv[0]``) and the bin dir of the interpreter running it
    (``sys.executable``) are where pip/uv/pipx placed its siblings. Returns
    resolved, de-duplicated directories.
    """
    dirs: list[Path] = []
    argv0 = sys.argv[0] if sys.argv else ""
    sources = [argv0, sys.executable]
    for src in sources:
        if not src:
            continue
        try:
            d = Path(src).resolve().parent
        except OSError:
            continue
        if d not in dirs:
            dirs.append(d)
    return dirs


def _resolve_entrypoint(name: str) -> str:
    """Resolve the absolute path of an installed nightdesk console script.

    Console scripts are installed next to the interpreter/shim that runs them:
    a uv-tool or ``~/.local/bin`` shim dir, a project ``.venv/bin``, or
    ``~/.local/bin`` for ``pip install --user``. We probe, in order, the dir of
    the running setup script and the interpreter's bin dir (the reliable cases),
    then ``PATH``, then fall back to ``~/.local/bin/<name>``. Returning a real,
    existing path is what lets the systemd unit's ``ExecStart`` actually start.
    """
    for d in _entrypoint_dir_candidates():
        cand = d / name
        if cand.is_file():
            return str(cand)
    which = shutil.which(name)
    if which:
        return str(Path(which))
    return str(Path(os.path.expanduser("~/.local/bin")) / name)


def _render_api_unit() -> str:
    return _API_UNIT_TEMPLATE.format(exec_start=_resolve_entrypoint("nightdesk-api"))


def _render_worker_unit() -> str:
    return _WORKER_UNIT_TEMPLATE.format(exec_start=_resolve_entrypoint("nightdesk-worker"))


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
    _add_config_flags(parser)
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

    cfg = _apply_config_flags(args)
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
    config_path = default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(_DEFAULT_CONFIG_TOML)
        print(f"Created default config at {config_path}")

    cfg = load_config()

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.transcript_root.mkdir(parents=True, exist_ok=True)
    cfg.worktree_root.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    _run_migrations(cfg)
    engine = make_engine(cfg.db_path)
    seed_default_profiles(engine)
    seed_default_labels(engine)
    return cfg


def init() -> None:
    """CLI entry point: create directories, default config, and run migrations."""
    import argparse
    parser = argparse.ArgumentParser(prog="nightdesk-init")
    _add_config_flags(parser)
    args = parser.parse_args()
    _apply_config_flags(args)
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
    config_path = default_config_path()
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


def _create_data_dirs(cfg: NightdeskConfig) -> None:
    """Create the standard data directories.

    ``cfg.worktree_root`` is created outside the data dir (the sandbox cannot
    bind-mount paths under the data dir).
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.transcript_root.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    (cfg.log_dir / "runs").mkdir(parents=True, exist_ok=True)
    cfg.worktree_root.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)


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
                    worktree_base_ref=(cfg.worktree_base_ref or None),
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
        print(f"[dry-run] would write nightdesk-api.service "
              f"(ExecStart={_resolve_entrypoint('nightdesk-api')})")
        print(f"[dry-run] would write nightdesk-worker.service "
              f"(ExecStart={_resolve_entrypoint('nightdesk-worker')})")
        return unit_dir

    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "nightdesk-api.service").write_text(_render_api_unit())
    (unit_dir / "nightdesk-worker.service").write_text(_render_worker_unit())
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


def _add_config_flags(parser: "argparse.ArgumentParser") -> None:
    """Register NIGHTDESK_* override flags on parser."""
    g = parser.add_argument_group("config overrides")
    g.add_argument("--config", metavar="PATH", default=None,
                   help="Alternate config.toml path (sets NIGHTDESK_CONFIG).")
    g.add_argument("--secrets", metavar="PATH", default=None,
                   help="Alternate secrets.env path (sets NIGHTDESK_SECRETS).")
    g.add_argument("--data-dir", metavar="PATH", default=None,
                   help="Data directory root (sets NIGHTDESK_DATA_DIR).")
    g.add_argument("--db-path", metavar="PATH", default=None,
                   help="SQLite database path (sets NIGHTDESK_DB_PATH).")
    g.add_argument("--transcript-root", metavar="PATH", default=None,
                   help="Transcript directory (sets NIGHTDESK_TRANSCRIPT_ROOT).")
    g.add_argument("--worktree-root", metavar="PATH", default=None,
                   help="Git worktree root (sets NIGHTDESK_WORKTREE_ROOT).")
    g.add_argument("--log-dir", metavar="PATH", default=None,
                   help="Log directory (sets NIGHTDESK_LOG_DIR).")
    g.add_argument("--bind-host", metavar="HOST", default=None,
                   help="API bind host (sets NIGHTDESK_BIND_HOST).")
    g.add_argument("--bind-port", metavar="PORT", type=int, default=None,
                   help="API bind port (sets NIGHTDESK_BIND_PORT).")


def _apply_config_flags(args: "argparse.Namespace") -> NightdeskConfig:
    """Export CLI override flags as NIGHTDESK_* env vars, then load config.

    Env vars are set before load_config() runs so uvicorn reload workers,
    run_dev's subprocess, and nightdesk-run-ticket child processes all
    inherit the overrides automatically.
    """
    _FLAG_ENV = [
        ("config", "NIGHTDESK_CONFIG"),
        ("secrets", "NIGHTDESK_SECRETS"),
        ("data_dir", "NIGHTDESK_DATA_DIR"),
        ("db_path", "NIGHTDESK_DB_PATH"),
        ("transcript_root", "NIGHTDESK_TRANSCRIPT_ROOT"),
        ("worktree_root", "NIGHTDESK_WORKTREE_ROOT"),
        ("log_dir", "NIGHTDESK_LOG_DIR"),
        ("bind_host", "NIGHTDESK_BIND_HOST"),
        ("bind_port", "NIGHTDESK_BIND_PORT"),
    ]
    for attr, env_key in _FLAG_ENV:
        val = getattr(args, attr, None)
        if val is not None:
            os.environ[env_key] = str(val)
    return load_config()


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
    _add_config_flags(parser)
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
    _apply_config_flags(args)

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
    config_path = default_config_path()
    if dry_run:
        print(f"[dry-run] would write {config_path} with new bearer token (chmod 600)")
        cfg = load_config() if config_path.exists() else NightdeskConfig(
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
        print(f"[dry-run] would create: {cfg.data_dir}/{{transcripts,logs,logs/runs}} and {cfg.worktree_root}")
    else:
        _create_data_dirs(cfg)

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
    print(
        f"\nnightdesk setup complete.\n"
        f"  data:   {cfg.data_dir}\n"
        f"  logs:   {cfg.log_dir}\n"
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

    # Derive all paths from config; fall back to defaults when no file.
    cfg = load_config()
    config_path = default_config_path()
    secrets_path = default_secrets_path()
    config_dir = config_path.parent
    data_dir = cfg.data_dir
    worktree_dir = cfg.worktree_root
    # cc-sessions lives as a sibling of the worktree root.
    cc_sessions_dir = worktree_dir.parent / "nightdesk-cc-sessions"

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
    import argparse
    parser = argparse.ArgumentParser(prog="nightdesk-login")
    _add_config_flags(parser)
    args = parser.parse_args()
    cfg = _apply_config_flags(args)
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
    _add_config_flags(parser)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Restart the server when source files change (development).",
    )
    args = parser.parse_args()

    cfg = _apply_config_flags(args)

    from nightdesk.logging_setup import configure_root_logging
    configure_root_logging(component="api", log_dir=cfg.log_dir)

    # Auto-migrate on every start; exit 70 on failure so systemd won't loop.
    try:
        _run_migrations(cfg)
    except Exception:
        logging.exception("Migration failed; refusing to start API.")
        sys.exit(70)

    # Alembic's fileConfig resets the root logger level and disables loggers
    # not listed in alembic.ini. Re-apply our config so app logs get through.
    configure_root_logging(component="api", log_dir=cfg.log_dir)

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
    # Keep the FTS search index complete and self-maintaining: recreate the
    # triggers a batch migration may have dropped and reindex on drift, so
    # tickets never silently fall out of text search.
    from nightdesk.domain.search import ensure_fts_index
    ensure_fts_index(engine)
    static_root = Path(__file__).parent / "static"
    app = create_app(
        engine=engine,
        bearer_token=cfg.bearer_token,
        static_root=static_root,
        transcript_root=cfg.transcript_root,
        worktree_root=cfg.worktree_root,
        bind_host=cfg.bind_host,
        bind_port=cfg.bind_port,
        data_dir=cfg.data_dir,
        pricing_url=cfg.pricing_url,
        spa_dist_root=cfg.spa_dist,
    )
    uvicorn.run(app, host=cfg.bind_host, port=cfg.bind_port, log_level="info")


def run_worker() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="nightdesk-worker")
    _add_config_flags(parser)
    args = parser.parse_args()
    cfg = _apply_config_flags(args)

    from nightdesk.logging_setup import configure_root_logging
    configure_root_logging(component="worker", log_dir=cfg.log_dir)

    # Auto-migrate on every start; exit 70 on failure so systemd won't loop.
    try:
        _run_migrations(cfg)
    except Exception:
        logging.exception("Migration failed; refusing to start worker.")
        sys.exit(70)

    # Alembic's fileConfig resets the root logger level and disables loggers
    # not listed in alembic.ini. Re-apply our config so app logs get through.
    configure_root_logging(component="worker", log_dir=cfg.log_dir)

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
    _add_config_flags(parser)
    parser.add_argument("ticket_id")
    parser.add_argument("--dump", action="store_true",
                        help="print the resolved spec and bwrap argv, then exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = _apply_config_flags(args)
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
                working_dir=str(spec.fs_write[0] if spec.fs_write else "/tmp"),
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


# ---------------------------------------------------------------------------
# `nightdesk config` command.
# ---------------------------------------------------------------------------


# Keys that live in config.toml and are accepted by `config set`.
_FILE_KEYS = {
    "bind_host": str,
    "bind_port": int,
    "bearer_token": str,
    "worktree_base_ref": str,
    "db_path": str,
    "transcript_root": str,
    "worktree_root": str,
    "data_dir": str,
    "log_dir": str,
    "pricing_url": str,
}

# Keys that are runtime-updatable via PATCH /api/v1/config.
_RUNTIME_KEYS = {
    "window_start": str,
    "window_end": str,
    "max_parallel": int,
    "worktree_base_ref": str,
}

_HH_MM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return "*" * len(token)
    return "*" * (len(token) - 8) + token[-8:]


def _api_get_config(cfg: NightdeskConfig) -> dict | None:
    """GET /api/v1/config. Returns parsed JSON dict or None on failure."""
    import json
    import urllib.error
    import urllib.request

    url = f"http://{cfg.bind_host}:{cfg.bind_port}/api/v1/config"
    headers = {}
    if cfg.bearer_token:
        headers["Authorization"] = f"Bearer {cfg.bearer_token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _api_patch_config(cfg: NightdeskConfig, payload: dict) -> bool:
    """PATCH /api/v1/config with the given payload. Returns True on success."""
    import json
    import urllib.error
    import urllib.request

    url = f"http://{cfg.bind_host}:{cfg.bind_port}/api/v1/config"
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if cfg.bearer_token:
        headers["Authorization"] = f"Bearer {cfg.bearer_token}"
    req = urllib.request.Request(url, data=body, method="PATCH", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _write_config_key(key: str, raw_value: str) -> None:
    """Update a single key in config.toml, preserving comments and formatting."""
    config_path = default_config_path()
    if not config_path.exists():
        print(
            f"Config file not found at {config_path}. Run nightdesk-setup first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine the TOML representation.
    if key in ("bind_port",):
        # Integer keys — validate first, write bare number.
        try:
            int(raw_value)
        except ValueError:
            print(
                f"Value for {key!r} must be an integer, got {raw_value!r}.",
                file=sys.stderr,
            )
            sys.exit(1)
        toml_val = raw_value
    else:
        escaped = raw_value.replace("\\", "\\\\").replace('"', '\\"')
        toml_val = f'"{escaped}"'

    text = config_path.read_text()
    # Try to replace an existing key = ... line.
    pattern = re.compile(rf"^({re.escape(key)}\s*=\s*).+$", re.MULTILINE)
    new_line = f"{key} = {toml_val}"
    if pattern.search(text):
        text = pattern.sub(new_line, text)
    else:
        # Append at end.
        if not text.endswith("\n"):
            text += "\n"
        text += new_line + "\n"

    config_path.write_text(text)
    config_path.chmod(0o600)


def _validate_value(key: str, value: str) -> str:
    """Validate and coerce a value for the given key. Returns the string to write."""
    if key in ("bind_port", "max_parallel"):
        try:
            int(value)
        except ValueError:
            print(
                f"Value for {key!r} must be an integer, got {value!r}.",
                file=sys.stderr,
            )
            sys.exit(1)

    if key in ("window_start", "window_end"):
        if not _HH_MM_RE.match(value):
            print(
                f"Value for {key!r} must be HH:MM format (00:00-23:59), got {value!r}.",
                file=sys.stderr,
            )
            sys.exit(1)

    return value


def config_list() -> None:
    """Print all current config values in key = value format."""
    cfg = load_config()

    # File-based config.
    print(f"data_dir = {cfg.data_dir}")
    print(f"log_dir = {cfg.log_dir}")
    print(f"bind_host = {cfg.bind_host}")
    print(f"bind_port = {cfg.bind_port}")
    print(f"bearer_token = {_mask_token(cfg.bearer_token)}")
    print(f"db_path = {cfg.db_path}")
    print(f"transcript_root = {cfg.transcript_root}")
    print(f"worktree_root = {cfg.worktree_root}")
    print(f"pricing_url = {cfg.pricing_url}")
    if cfg.worktree_base_ref is not None:
        print(f"worktree_base_ref = {cfg.worktree_base_ref}")
    else:
        print("worktree_base_ref = (not set)")

    # Runtime config from the API.
    runtime = _api_get_config(cfg)
    if runtime is not None:
        print(f"window_start = {runtime.get('window_start', 'N/A')}")
        print(f"window_end = {runtime.get('window_end', 'N/A')}")
        print(f"max_parallel = {runtime.get('max_parallel', 'N/A')}")
    else:
        print("window_start = (API unreachable)")
        print("window_end = (API unreachable)")
        print("max_parallel = (API unreachable)")


def config_cmd() -> None:
    """CLI entry point: `nightdesk-config list` or `nightdesk-config set <key> <value>`."""
    import argparse

    all_keys = sorted(set(_FILE_KEYS) | set(_RUNTIME_KEYS))

    parser = argparse.ArgumentParser(
        prog="nightdesk-config",
        description="Inspect and modify nightdesk configuration.",
    )
    _add_config_flags(parser)
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("list", help="Show all current config values")
    set_parser = sub.add_parser("set", help="Set a config key")
    set_parser.add_argument("key", help=f"Config key to set. Valid keys: {', '.join(all_keys)}")
    set_parser.add_argument("value", help="Value to set")

    args = parser.parse_args()
    _apply_config_flags(args)

    if args.action == "list":
        config_list()
    elif args.action == "set":
        key: str = args.key
        value: str = args.value

        if key not in all_keys:
            print(
                f"Unknown config key {key!r}.\n"
                f"Valid keys: {', '.join(all_keys)}",
                file=sys.stderr,
            )
            sys.exit(1)

        value = _validate_value(key, value)

        is_file_key = key in _FILE_KEYS
        is_runtime_key = key in _RUNTIME_KEYS

        if is_file_key:
            _write_config_key(key, value)
            print(f"Written {key} = {value} to {default_config_path()}")

        if is_runtime_key:
            cfg = load_config()
            patch_val: str | int = value
            if key in ("max_parallel",):
                patch_val = int(value)
            if _api_patch_config(cfg, {key: patch_val}):
                print(f"Applied {key} = {value} to running server.")
            else:
                print(
                    f"Could not reach the API at http://{cfg.bind_host}:{cfg.bind_port}. "
                    f"The config file has been updated but the running server was not notified.",
                    file=sys.stderr,
                )

        if is_file_key and not is_runtime_key:
            print("Note: this setting requires a server restart to take effect.")
    else:
        parser.print_help()
        sys.exit(1)


# ---------------------------------------------------------------------------
# `nightdesk install-skills` command.
# ---------------------------------------------------------------------------


_VERSION_MARKER = ".nightdesk-skills-version"


def _find_bundled_skills_dir() -> Path:
    """Locate the bundled skills directory relative to the package root.

    Works for editable installs and running from source. The skills live in
    ``<repo_root>/.claude/skills/``, three directories up from this file
    (src/nightdesk/cli.py -> repo_root).
    """
    # Same navigation pattern as _alembic_config uses for alembic.ini.
    repo_root = Path(__file__).resolve().parent.parent.parent
    skills_dir = repo_root / ".claude" / "skills"
    if not skills_dir.is_dir():
        print(
            f"Bundled skills directory not found at {skills_dir}.\n"
            "This command requires a source or editable install of nightdesk.",
            file=sys.stderr,
        )
        sys.exit(1)
    return skills_dir


def _is_internal_skill(skill_dir: Path) -> bool:
    """True if a bundled skill is internal and must NOT ship to users.

    A skill opts out of shipping by setting ``internal: true`` in its
    ``SKILL.md`` frontmatter. Anything else — no SKILL.md, no frontmatter, a
    missing or false-valued key, no closing fence, or an unreadable file —
    ships normally. That default is deliberate: new user skills install
    automatically, and only dev/internal runbooks need to opt out.
    """
    import re

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return False
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    block: list[str] = []
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        block.append(line)
    if not closed:
        return False
    m = re.search(r'^[ \t]*internal[ \t]*:[ \t]*([^\s#]+)', "\n".join(block), re.MULTILINE)
    if not m:
        return False
    return m.group(1).strip().lower() in ("true", "yes", "1")


def _shippable_skill_dirs(bundled: Path) -> list[Path]:
    """Sorted bundled skill directories that are NOT marked internal."""
    return sorted(
        d for d in bundled.iterdir()
        if d.is_dir() and not _is_internal_skill(d)
    )


def _hash_skills(skills_dir: Path) -> str:
    """Compute a deterministic SHA-256 hash over all shippable skill contents.

    Walks skill directories in sorted order and hashes each file's relative
    path + content, so the hash is stable regardless of filesystem traversal
    order. Internal skills (``internal: true`` frontmatter) are excluded, so
    editing a dev-only runbook does not mark installed user skills as drifted.
    """
    import hashlib

    h = hashlib.sha256()
    for skill_dir in _shippable_skill_dirs(skills_dir):
        for dirpath, dirnames, filenames in sorted(os.walk(skill_dir)):
            # Skip the version marker itself if it somehow ended up in the source.
            dirnames.sort()
            for fname in sorted(filenames):
                if fname == _VERSION_MARKER:
                    continue
                fpath = Path(dirpath) / fname
                rel = fpath.relative_to(skills_dir)
                h.update(str(rel).encode())
                h.update(fpath.read_bytes())
    return h.hexdigest()


def _read_version_marker(target_skills: Path) -> dict | None:
    """Read the installed version marker. Returns None if missing or invalid."""
    marker_path = target_skills / _VERSION_MARKER
    if not marker_path.is_file():
        return None
    import json

    try:
        return json.loads(marker_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_version_marker(target_skills: Path, version: str, skills_hash: str) -> None:
    """Write the version marker JSON file."""
    import json

    marker_path = target_skills / _VERSION_MARKER
    marker_path.write_text(json.dumps(
        {"nightdesk_version": version, "skills_hash": skills_hash},
        indent=2,
    ) + "\n")


def _resolve_skills_dir(target_arg: str | None) -> Path:
    """Resolve the ``.claude/skills`` directory to install into.

    - ``--target DIR`` -> ``DIR/.claude/skills`` (project-level install).
    - default          -> the user's Claude config dir + ``/skills``:
      ``$CLAUDE_CONFIG_DIR/skills`` when that env var is set (the same override
      Claude Code itself honors), otherwise ``~/.claude/skills``.
    """
    if target_arg:
        p = Path(target_arg).resolve()
        if not p.is_dir():
            print(f"Target directory does not exist: {p}", file=sys.stderr)
            sys.exit(1)
        return p / ".claude" / "skills"

    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(cfg).expanduser() if cfg else Path.home() / ".claude"
    return base / "skills"


# ---------------------------------------------------------------------------
# Harness registry: supported coding-agent targets for skill installation.
#
# nightdesk's bundled skills are generic HTTP-API docs shipped as
# ``<name>/SKILL.md`` folders. That exact layout is loaded natively by every
# harness below, so one copy routine serves all of them — only the config-root
# resolution and the "is this agent installed?" detection differ per harness.
#
# Confirmed default locations (do not invent new ones — if a harness's skill
# location can't be confirmed from its docs, leave it out of the registry):
#   - Claude Code : ``$CLAUDE_CONFIG_DIR/skills`` or ``~/.claude/skills``
#                   https://docs.claude.com/en/docs/claude-code/skills
#   - opencode    : ``$OPENCODE_CONFIG_DIR/skills`` or
#                   ``$XDG_CONFIG_HOME/opencode/skills`` (default
#                   ``~/.config/opencode/skills``). opencode also reads
#                   ``~/.claude/skills`` natively, but we install into its own
#                   dir so the install is first-class and isolated.
#                   Skills are ``skills/<name>/SKILL.md`` folders; the ``name``
#                   frontmatter must equal the directory name.
#                   https://opencode.ai/docs/skills/  (config: /docs/config/)
#   - pi          : ``$PI_CODING_AGENT_DIR/skills`` or ``~/.pi/agent/skills``
#                   (pi does NOT honor XDG_CONFIG_HOME). Skills are
#                   ``skills/<name>/SKILL.md`` folders; ``name`` need not match
#                   the directory. https://pi.dev/docs/latest/skills
# ---------------------------------------------------------------------------


class Harness:
    """A supported coding-agent harness nightdesk can install skills into."""

    def __init__(self, name, display, doc_url, config_root, detect):
        self.name = name
        self.display = display
        self.doc_url = doc_url
        self._config_root = config_root  # callable[[], Path]
        self._detect = detect            # callable[[], bool]

    def config_root(self) -> Path:
        """Resolve this harness's config-directory root (env overrides honored)."""
        return self._config_root()

    def skills_dir(self) -> Path:
        """Directory skills are installed into / loaded from for this harness."""
        return self.config_root() / "skills"

    def is_installed(self) -> bool:
        """True when this harness appears to be installed on the machine."""
        return self._detect()


def _xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(xdg).expanduser() if xdg else Path.home() / ".config"


def _on_path(binary: str) -> bool:
    return shutil.which(binary) is not None


def _claude_config_root() -> Path:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(cfg).expanduser() if cfg else Path.home() / ".claude"


def _opencode_config_root() -> Path:
    cfg = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if cfg:
        return Path(cfg).expanduser()
    return _xdg_config_home() / "opencode"


def _pi_config_root() -> Path:
    cfg = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    return Path(cfg).expanduser() if cfg else Path.home() / ".pi" / "agent"


def _claude_detected() -> bool:
    return (
        bool(os.environ.get("CLAUDE_CONFIG_DIR", "").strip())
        or _claude_config_root().is_dir()
        or _on_path("claude")
    )


def _opencode_detected() -> bool:
    return (
        bool(os.environ.get("OPENCODE_CONFIG_DIR", "").strip())
        or _opencode_config_root().is_dir()
        or _on_path("opencode")
    )


def _pi_detected() -> bool:
    return (
        bool(os.environ.get("PI_CODING_AGENT_DIR", "").strip())
        or _pi_config_root().is_dir()
        or _on_path("pi")
    )


# Registry order = listing/prompt order. Claude Code first (the default).
_HARNESSES: list[Harness] = [
    Harness("claude", "Claude Code",
            "https://docs.claude.com/en/docs/claude-code/skills",
            _claude_config_root, _claude_detected),
    Harness("opencode", "opencode",
            "https://opencode.ai/docs/skills/",
            _opencode_config_root, _opencode_detected),
    Harness("pi", "pi",
            "https://pi.dev/docs/latest/skills",
            _pi_config_root, _pi_detected),
]


def _harness_by_name(name: str) -> Harness | None:
    for h in _HARNESSES:
        if h.name == name:
            return h
    return None


def _claude_harness() -> Harness:
    return _harness_by_name("claude")  # always present


def _detect_harnesses() -> list[Harness]:
    return [h for h in _HARNESSES if h.is_installed()]


def _install_into_target(
    target_dir: Path, bundled: Path, skills_hash: str, version: str, force: bool
) -> dict | None:
    """Copy every bundled skill into ``target_dir`` and write its version marker.

    Shared by every harness target (and by ``--target``). Returns a result dict
    ``{"installed", "updated", "marker_existed", "force"}`` when skills were
    written, or ``None`` when it stopped early for a reason it already printed
    (up to date / no bundled skills / no changes). Refuses to install into the
    bundled source directory (exits 1) — the guard holds for every target.
    """
    # Refuse to install into our own bundled skills dir. If a target resolves
    # to the bundled source, the per-skill rmtree + copytree below would delete
    # each source skill before copying it back from the now-empty path,
    # destroying the bundled skills. Nothing to install here.
    if target_dir.resolve() == bundled.resolve():
        print(
            "Target resolves to nightdesk's own bundled skills directory "
            f"({bundled}); nothing to install.\n"
            "Run from another project, or pass --target <other-project>.",
            file=sys.stderr,
        )
        sys.exit(1)

    target_dir.mkdir(parents=True, exist_ok=True)

    bundled_skills = _shippable_skill_dirs(bundled)
    if not bundled_skills:
        print("No bundled skills found. Nothing to install.")
        return None

    marker = _read_version_marker(target_dir)
    needs_update = force

    if marker and not force:
        installed_hash = marker.get("skills_hash", "")
        installed_version = marker.get("nightdesk_version", "?")
        if installed_hash == skills_hash:
            print(f"Skills are up to date (nightdesk {installed_version}). Use --force to reinstall.")
            return None
        print(f"Skills drift detected (installed from {installed_version}, current {version}).")
        needs_update = True
    elif not marker:
        needs_update = True

    if not needs_update:
        print("No changes needed.")
        return None

    installed: list[str] = []
    updated: list[str] = []

    for skill_dir in bundled_skills:
        name = skill_dir.name
        dest = target_dir / name
        if dest.exists():
            shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)
            updated.append(name)
        else:
            shutil.copytree(skill_dir, dest)
            installed.append(name)

    # Each target keeps its own marker in its own dir, so drift detection is
    # per-harness and isolated. Same filename everywhere; the Claude Code one
    # stays exactly where it has always been.
    _write_version_marker(target_dir, version, skills_hash)

    return {"installed": installed, "updated": updated,
            "marker_existed": bool(marker), "force": force}


def _summarize_install(target_dir: Path, result: dict, version: str, skills_hash: str) -> None:
    """Print the post-install summary for one target."""
    action = ("updated" if result["marker_existed"] and not result["force"]
              else ("reinstalled" if result["force"] else "installed"))
    print(f"\nSkills {action} into {target_dir}:")
    for name in result["installed"]:
        print(f"  + {name} (new)")
    for name in result["updated"]:
        print(f"  ~ {name} (updated)")
    if not result["installed"] and not result["updated"]:
        print("  (none)")
    print(f"\nVersion marker: nightdesk {version}, hash {skills_hash[:12]}...")


def _install_one(harness: Harness, bundled: Path, skills_hash: str,
                 version: str, force: bool) -> None:
    """Install into one harness's default skills dir and summarize."""
    target = harness.skills_dir()
    print(f"\n=== {harness.display} -> {target} ===")
    result = _install_into_target(target, bundled, skills_hash, version, force)
    if result:
        _summarize_install(target, result, version, skills_hash)


def _prompt_yn(question: str) -> bool:
    """y/n prompt; defaults to No on empty input or EOF (non-interactive)."""
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _print_harness_list() -> None:
    detected = {h.name for h in _detect_harnesses()}
    print("Supported harnesses (* = detected on this machine):")
    for h in _HARNESSES:
        mark = "*" if h.name in detected else " "
        print(f"  {mark} {h.name:<10} {h.display:<14} {h.skills_dir()}")


def install_skills() -> None:
    """CLI entry point: install nightdesk skills for the user.

    nightdesk's skills are generic markdown docs, so they are useful inside any
    coding agent that loads ``<dir>/skills/<name>/SKILL.md`` folders. This
    command is harness-aware: it detects installed agents and can install into
    each one's default skills directory.

    Usage:
        nightdesk-install-skills                     # Claude Code only -> straight install (default)
        nightdesk-install-skills --target /path/to/project   # project-local DIR/.claude/skills
        nightdesk-install-skills --list-harnesses    # show supported + detected harnesses
        nightdesk-install-skills --all               # every detected harness, non-interactive
        nightdesk-install-skills --harness opencode  # one specific harness, non-interactive
        nightdesk-install-skills --force             # reinstall even if up to date
    """
    import argparse
    from importlib.metadata import version as pkg_version

    parser = argparse.ArgumentParser(prog="nightdesk-install-skills")
    parser.add_argument(
        "--target",
        metavar="DIR",
        help="Install project-locally into DIR/.claude/skills "
             "(default: the user's $CLAUDE_CONFIG_DIR/skills or ~/.claude/skills).",
    )
    parser.add_argument(
        "--harness",
        metavar="NAME",
        help="Install into one specific harness's default location, "
             "non-interactively (e.g. opencode, pi, claude). "
             "Use --list-harnesses to see supported names.",
    )
    parser.add_argument(
        "--all",
        "--yes",
        dest="install_all",
        action="store_true",
        help="Install into every detected harness non-interactively "
             "(alias: --yes). Needed for worker/sandbox runs.",
    )
    parser.add_argument(
        "--list-harnesses",
        action="store_true",
        help="Print supported harnesses, mark which are detected, then exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-install all skills even if the version marker matches.",
    )
    args = parser.parse_args()

    bundled = _find_bundled_skills_dir()
    skills_hash = _hash_skills(bundled)

    try:
        version = pkg_version("nightdesk")
    except Exception:
        version = "0.0.0"

    if args.list_harnesses:
        _print_harness_list()
        return

    # --target: existing project-local Claude-Code-style install, unchanged.
    if args.target:
        if args.harness or args.install_all:
            print("--target cannot be combined with --harness/--all.",
                  file=sys.stderr)
            sys.exit(2)
        target = _resolve_skills_dir(args.target)
        result = _install_into_target(target, bundled, skills_hash, version, args.force)
        if result:
            _summarize_install(target, result, version, skills_hash)
        return

    # --harness NAME: one specific harness, non-interactive.
    if args.harness:
        harness = _harness_by_name(args.harness)
        if harness is None:
            supported = ", ".join(h.name for h in _HARNESSES)
            print(f"Unknown harness '{args.harness}'. Supported: {supported}.",
                  file=sys.stderr)
            sys.exit(2)
        _install_one(harness, bundled, skills_hash, version, args.force)
        return

    detected = _detect_harnesses()

    # --all / --yes: install into every detected harness, non-interactive.
    if args.install_all:
        if not detected:
            print("No supported harnesses detected on this machine.\n"
                  "Use --harness <name> to install into a specific one "
                  f"(one of: {', '.join(h.name for h in _HARNESSES)}).")
            return
        for harness in detected:
            _install_one(harness, bundled, skills_hash, version, args.force)
        return

    # No flag: default flow.
    non_claude = [h for h in detected if h.name != "claude"]

    if not non_claude:
        # Only Claude Code detected (or nothing detected) — preserve the exact
        # historical behavior: straight install into the Claude skills dir,
        # no per-harness prompt. Existing users see no regression.
        target = _claude_harness().skills_dir()
        result = _install_into_target(target, bundled, skills_hash, version, args.force)
        if result:
            _summarize_install(target, result, version, skills_hash)
        return

    # More than one harness detected — prompt y/n per harness.
    chosen: list[Harness] = []
    print("Multiple coding-agent harnesses detected on this machine.")
    for harness in detected:
        if _prompt_yn(
            f"  Install nightdesk skills into {harness.display} "
            f"({harness.skills_dir()})? [y/N] "
        ):
            chosen.append(harness)

    if not chosen:
        print("Nothing selected. No skills installed.")
        return

    for harness in chosen:
        _install_one(harness, bundled, skills_hash, version, args.force)


def run_dev() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="nightdesk-dev")
    _add_config_flags(parser)
    args = parser.parse_args()
    _apply_config_flags(args)
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
