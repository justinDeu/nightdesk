"""Tests for nightdesk setup command and CC version check.

Covers:
- check_cc_binary happy path (version >= floor)
- check_cc_binary too-old
- check_cc_binary missing binary
- nightdesk-setup --dry-run (no filesystem side effects)
- unit file content matches expected template strings
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_shim(tmp_path: Path, output: str, rc: int = 0) -> Path:
    """Write a tiny shell script that prints output and exits rc."""
    shim = tmp_path / "claude"
    shim.write_text(
        f"#!/bin/sh\necho '{output}'\nexit {rc}\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _minimal_config(tmp_path: Path):
    """Return a NightdeskConfig pointing at a tmp DB."""
    from nightdesk.config import NightdeskConfig

    db = tmp_path / "nd.db"
    return NightdeskConfig(
        bearer_token="test-bearer",
        db_path=db,
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


def _bootstrap_db(cfg):
    """Create schema tables so cc_check can read/write DaemonStatus."""
    from nightdesk.db.models import Base
    from nightdesk.db.session import make_engine

    engine = make_engine(cfg.db_path)
    Base.metadata.create_all(engine)
    engine.dispose()


# ---------------------------------------------------------------------------
# check_cc_binary tests.
# ---------------------------------------------------------------------------


def test_check_cc_binary_happy_path(tmp_path):
    """A shim that prints a version >= floor should return status 'ok'."""
    shim = _make_shim(tmp_path, "2.1.99")
    cfg = _minimal_config(tmp_path)
    _bootstrap_db(cfg)

    from nightdesk.domain.cc_check import CcStatus, check_cc_binary

    result = check_cc_binary(cfg, binary_path=str(shim), floor="2.1.80")

    assert result.status == CcStatus.ok
    assert result.version == "2.1.99"
    assert result.binary_path == str(shim)
    assert "ok" in result.message


def test_check_cc_binary_too_old(tmp_path):
    """A shim that prints a version below the floor should return 'too_old'."""
    shim = _make_shim(tmp_path, "2.1.50")
    cfg = _minimal_config(tmp_path)
    _bootstrap_db(cfg)

    from nightdesk.domain.cc_check import CcStatus, check_cc_binary

    result = check_cc_binary(cfg, binary_path=str(shim), floor="2.1.80")

    assert result.status == CcStatus.too_old
    assert result.version == "2.1.50"
    assert "2.1.80" in result.message


def test_check_cc_binary_missing(tmp_path):
    """When no binary can be found, status should be 'missing'."""
    cfg = _minimal_config(tmp_path)
    _bootstrap_db(cfg)

    from nightdesk.domain.cc_check import CcStatus, check_cc_binary

    # Pass a path that does not exist; also ensure PATH lookup fails.
    result = check_cc_binary(
        cfg,
        binary_path=str(tmp_path / "no_such_claude"),
        floor="2.1.80",
    )

    assert result.status == CcStatus.missing


def test_check_cc_binary_missing_no_path(tmp_path):
    """When binary_path is None and shutil.which returns None, status is 'missing'."""
    cfg = _minimal_config(tmp_path)
    _bootstrap_db(cfg)

    from nightdesk.domain.cc_check import CcStatus, check_cc_binary

    with patch("nightdesk.domain.cc_check.shutil.which", return_value=None):
        result = check_cc_binary(cfg, binary_path=None, floor="2.1.80")

    assert result.status == CcStatus.missing


def test_check_cc_binary_unparseable_version(tmp_path):
    """A shim that prints no version number should return 'error'."""
    shim = _make_shim(tmp_path, "claude (no version)")
    cfg = _minimal_config(tmp_path)
    _bootstrap_db(cfg)

    from nightdesk.domain.cc_check import CcStatus, check_cc_binary

    result = check_cc_binary(cfg, binary_path=str(shim), floor="2.1.80")

    assert result.status == CcStatus.error


# ---------------------------------------------------------------------------
# persist_cc_check test.
# ---------------------------------------------------------------------------


def test_persist_cc_check_creates_row(tmp_path):
    """persist_cc_check should upsert into daemon_status id=1."""
    cfg = _minimal_config(tmp_path)
    _bootstrap_db(cfg)

    from nightdesk.db.models import DaemonStatus
    from nightdesk.db.session import make_engine, session_factory
    from nightdesk.domain.cc_check import CcCheckResult, CcStatus, persist_cc_check

    engine = make_engine(cfg.db_path)
    Session = session_factory(engine)

    result = CcCheckResult(
        status=CcStatus.ok,
        message="all good",
        binary_path="/usr/bin/claude",
        version="2.1.99",
    )

    with Session() as s:
        persist_cc_check(result, s)

    with Session() as s:
        row = s.get(DaemonStatus, 1)
        assert row is not None
        assert row.cc_check_status == "ok"
        assert row.cc_version == "2.1.99"
        assert row.cc_binary_path == "/usr/bin/claude"

    engine.dispose()


# ---------------------------------------------------------------------------
# setup --dry-run test: no filesystem mutation.
# ---------------------------------------------------------------------------


def test_setup_dry_run_no_filesystem_changes(tmp_path, capsys):
    """--dry-run should print planned actions and touch nothing."""
    # Keep a snapshot of tmp_path children before running setup.
    before = set(tmp_path.iterdir())

    # We patch out anything that would normally reach the real filesystem
    # or network. The dry_run path should reach the print statements
    # without making any FS calls at all — but to be safe, redirect
    # DEFAULT_CONFIG_PATH and home-expansion targets to tmp_path.
    import nightdesk.cli as cli_mod

    fake_config_path = tmp_path / "config.toml"

    with (
        patch.dict("os.environ", {"NIGHTDESK_CONFIG": str(fake_config_path)}, clear=False),
        patch("nightdesk.cli._check_platform"),
        patch("nightdesk.cli._check_bwrap"),
        patch("sys.argv", ["nightdesk-setup", "--dry-run"]),
    ):
        # Should not raise.
        try:
            cli_mod.setup()
        except SystemExit as exc:
            pytest.fail(f"setup --dry-run raised SystemExit({exc.code})")

    # Nothing new in tmp_path (config not written, dirs not created).
    after = set(tmp_path.iterdir())
    assert after == before, f"dry-run mutated filesystem: {after - before}"

    captured = capsys.readouterr()
    assert "[dry-run]" in captured.out
    assert "setup complete" in captured.out


# ---------------------------------------------------------------------------
# Unit file content tests.
# ---------------------------------------------------------------------------


def _execstart_path(unit_text: str) -> Path:
    """Extract the ExecStart binary path (first token) from a rendered unit."""
    lines = [ln for ln in unit_text.splitlines() if ln.startswith("ExecStart=")]
    assert lines, f"no ExecStart line in unit:\n{unit_text}"
    return Path(lines[0].split("=", 1)[1].split()[0])


def _fake_bindir(tmp_path: Path, *names: str) -> Path:
    """Create a bin dir holding executable shim scripts for the given names."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in names:
        p = bindir / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def test_api_unit_file_content(tmp_path, monkeypatch):
    """Rendered API unit must include all required directives and a real ExecStart."""
    from nightdesk.cli import _render_api_unit

    bindir = _fake_bindir(tmp_path, "nightdesk-setup", "nightdesk-api")
    monkeypatch.setattr(sys, "argv", [str(bindir / "nightdesk-setup")])

    rendered = _render_api_unit()
    assert "StandardOutput=journal" in rendered
    assert "StandardError=journal" in rendered
    assert 'Environment="PYTHONUNBUFFERED=1"' in rendered
    assert "Restart=on-failure" in rendered
    assert "RestartSec=2" in rendered
    assert "RestartPreventExitStatus=70" in rendered
    # ExecStart must point at an absolute, existing nightdesk-api binary —
    # not the old hardcoded %h/.local/bin path that setup never installed.
    exec_path = _execstart_path(rendered)
    assert exec_path.is_absolute()
    assert exec_path.name == "nightdesk-api"
    assert exec_path.is_file()


def test_worker_unit_file_content(tmp_path, monkeypatch):
    """Rendered worker unit must include all required directives and a real ExecStart."""
    from nightdesk.cli import _render_worker_unit

    bindir = _fake_bindir(tmp_path, "nightdesk-setup", "nightdesk-worker")
    monkeypatch.setattr(sys, "argv", [str(bindir / "nightdesk-setup")])

    rendered = _render_worker_unit()
    assert "StandardOutput=journal" in rendered
    assert "StandardError=journal" in rendered
    assert 'Environment="PYTHONUNBUFFERED=1"' in rendered
    assert "Restart=on-failure" in rendered
    assert "RestartSec=2" in rendered
    assert "RestartPreventExitStatus=70" in rendered
    assert "nightdesk-api.service" in rendered
    exec_path = _execstart_path(rendered)
    assert exec_path.is_absolute()
    assert exec_path.name == "nightdesk-worker"
    assert exec_path.is_file()


# ---------------------------------------------------------------------------
# Entrypoint resolution tests.
# ---------------------------------------------------------------------------


def test_resolve_entrypoint_prefers_setup_sibling(tmp_path, monkeypatch):
    """The script next to the running nightdesk-setup wins (the venv/shim dir)."""
    import nightdesk.cli as cli

    bindir = _fake_bindir(tmp_path, "nightdesk-setup", "nightdesk-api")
    monkeypatch.setattr(sys, "argv", [str(bindir / "nightdesk-setup")])

    got = Path(cli._resolve_entrypoint("nightdesk-api"))
    assert got.is_file()
    assert got.name == "nightdesk-api"
    assert got.parent == bindir.resolve()


def test_resolve_entrypoint_uses_interpreter_bindir(tmp_path, monkeypatch):
    """When argv0 has no sibling, fall back to the interpreter's bin dir."""
    import nightdesk.cli as cli

    empty = tmp_path / "empty"
    empty.mkdir()
    bindir = _fake_bindir(tmp_path, "nightdesk-worker")
    monkeypatch.setattr(sys, "argv", [str(empty / "nightdesk-setup")])
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))

    got = Path(cli._resolve_entrypoint("nightdesk-worker"))
    assert got == bindir.resolve() / "nightdesk-worker"
    assert got.is_file()


def test_resolve_entrypoint_falls_back_to_local_bin(tmp_path, monkeypatch):
    """With nothing found on disk or PATH, fall back to ~/.local/bin/<name>."""
    import nightdesk.cli as cli

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(sys, "argv", [str(empty / "nightdesk-setup")])
    monkeypatch.setattr(sys, "executable", str(empty / "python"))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    got = cli._resolve_entrypoint("nightdesk-worker")
    assert got == str(tmp_path / ".local" / "bin" / "nightdesk-worker")


def test_install_systemd_units_writes_runnable_execstart(tmp_path, monkeypatch):
    """_install_systemd_units must write units whose ExecStart binaries exist.

    This is the core regression guard: before the fix, units pointed at
    ~/.local/bin/nightdesk-* which setup never installed.
    """
    import nightdesk.cli as cli

    bindir = _fake_bindir(
        tmp_path, "nightdesk-setup", "nightdesk-api", "nightdesk-worker"
    )
    monkeypatch.setattr(sys, "argv", [str(bindir / "nightdesk-setup")])
    monkeypatch.setenv("HOME", str(tmp_path))

    unit_dir = cli._install_systemd_units(dry_run=False)
    assert unit_dir == Path(tmp_path) / ".config" / "systemd" / "user"

    for unit_name, script in (
        ("nightdesk-api.service", "nightdesk-api"),
        ("nightdesk-worker.service", "nightdesk-worker"),
    ):
        text = (unit_dir / unit_name).read_text()
        exec_path = _execstart_path(text)
        assert exec_path.name == script
        assert exec_path.is_file(), f"{unit_name} ExecStart references a missing file"

    # Idempotent: a second install writes byte-identical units.
    before = {
        n: (unit_dir / n).read_text()
        for n in ("nightdesk-api.service", "nightdesk-worker.service")
    }
    cli._install_systemd_units(dry_run=False)
    after = {
        n: (unit_dir / n).read_text()
        for n in ("nightdesk-api.service", "nightdesk-worker.service")
    }
    assert before == after


# ---------------------------------------------------------------------------
# mint-handshake endpoint test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_handshake_requires_bearer(tmp_path):
    """POST /auth/mint-handshake without auth should return 401."""
    import httpx
    from httpx import ASGITransport, AsyncClient

    from nightdesk.api.app import create_app
    from nightdesk.db.models import Base
    from nightdesk.db.session import make_engine
    from sqlalchemy import create_engine as sa_engine

    eng = sa_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(eng)

    app = create_app(
        engine=eng,
        bearer_token="secret",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "tr",
        worktree_root=tmp_path / "wt",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/auth/mint-handshake")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_mint_handshake_returns_url(tmp_path):
    """POST /auth/mint-handshake with correct bearer returns a handshake URL."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import create_engine as sa_engine

    from nightdesk.api.app import create_app
    from nightdesk.db.models import Base

    eng = sa_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(eng)

    app = create_app(
        engine=eng,
        bearer_token="secret",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "tr",
        worktree_root=tmp_path / "wt",
        bind_host="127.0.0.1",
        bind_port=8765,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer secret"},
    ) as ac:
        r = await ac.post("/auth/mint-handshake")

    assert r.status_code == 200
    body = r.json()
    assert "token" in body
    assert "url" in body
    assert "/auth/handshake?token=" in body["url"]
