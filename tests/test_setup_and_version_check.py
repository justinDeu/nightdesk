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
        patch.object(cli_mod, "DEFAULT_CONFIG_PATH", fake_config_path),
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


def test_api_unit_file_content():
    """API unit file must include all required directives."""
    from nightdesk.cli import _API_UNIT

    assert "StandardOutput=journal" in _API_UNIT
    assert "StandardError=journal" in _API_UNIT
    assert 'Environment="PYTHONUNBUFFERED=1"' in _API_UNIT
    assert "Restart=on-failure" in _API_UNIT
    assert "RestartSec=2" in _API_UNIT
    assert "RestartPreventExitStatus=70" in _API_UNIT
    assert "ExecStart=%h/.local/bin/nightdesk-api" in _API_UNIT


def test_worker_unit_file_content():
    """Worker unit file must include all required directives."""
    from nightdesk.cli import _WORKER_UNIT

    assert "StandardOutput=journal" in _WORKER_UNIT
    assert "StandardError=journal" in _WORKER_UNIT
    assert 'Environment="PYTHONUNBUFFERED=1"' in _WORKER_UNIT
    assert "Restart=on-failure" in _WORKER_UNIT
    assert "RestartSec=2" in _WORKER_UNIT
    assert "RestartPreventExitStatus=70" in _WORKER_UNIT
    assert "ExecStart=%h/.local/bin/nightdesk-worker" in _WORKER_UNIT
    assert "nightdesk-api.service" in _WORKER_UNIT


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
