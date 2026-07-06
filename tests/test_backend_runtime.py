"""Tests for nightdesk.domain.backend_runtime — the hard found/not-found
binary status shown in Settings > Harnesses. Mirrors the resolution chains
in backends/opencode.py and worker/sandbox.py so status never lies about
what a run would actually launch.
"""
from __future__ import annotations

import stat
from pathlib import Path

from nightdesk.domain.backend_runtime import (
    claude_runtime_status,
    opencode_runtime_status,
    runtime_status_for,
)


def _make_shim(tmp_path: Path, name: str, output: str = "1.2.3") -> Path:
    shim = tmp_path / name
    shim.write_text(f"#!/bin/sh\necho '{output}'\nexit 0\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def test_claude_override_found(tmp_path):
    shim = _make_shim(tmp_path, "claude", "2.3.4")
    status = claude_runtime_status(str(shim))
    assert status.source == "override"
    assert status.found is True
    assert status.resolved_path == str(shim)
    assert status.version == "2.3.4"


def test_claude_override_missing_file(tmp_path):
    missing = tmp_path / "no-such-claude"
    status = claude_runtime_status(str(missing))
    assert status.source == "override"
    assert status.found is False
    assert status.version is None


def test_claude_no_override_falls_back_to_path(monkeypatch, tmp_path):
    shim = _make_shim(tmp_path, "claude")
    monkeypatch.setattr("nightdesk.domain.backend_runtime.shutil.which", lambda name: str(shim))
    status = claude_runtime_status(None)
    assert status.source == "path"
    assert status.found is True
    assert status.resolved_path == str(shim)


def test_claude_no_override_no_path_uses_default(monkeypatch):
    monkeypatch.setattr("nightdesk.domain.backend_runtime.shutil.which", lambda name: None)
    status = claude_runtime_status(None)
    assert status.source == "default"
    assert status.resolved_path == "/usr/local/bin/claude"
    assert isinstance(status.found, bool)
    if not status.found:
        assert status.version is None


def test_opencode_override_found(tmp_path):
    shim = _make_shim(tmp_path, "opencode", "1.16.2")
    status = opencode_runtime_status(str(shim))
    assert status.source == "override"
    assert status.found is True
    assert status.version == "1.16.2"


def test_opencode_no_override_no_path_uses_default(monkeypatch):
    monkeypatch.setattr("nightdesk.domain.backend_runtime.shutil.which", lambda name: None)
    status = opencode_runtime_status(None)
    assert status.source == "default"
    assert status.resolved_path.endswith("/.opencode/bin/opencode")
    # found/version depend on whether this machine happens to have opencode
    # installed at the default path — just check the field is a real bool.
    assert isinstance(status.found, bool)
    if not status.found:
        assert status.version is None


def test_runtime_status_for_unknown_code_is_none():
    assert runtime_status_for("dummy", None) is None


def test_runtime_status_for_reads_config_row_overrides(tmp_path):
    shim = _make_shim(tmp_path, "claude")

    class _Row:
        claude_binary_path = str(shim)
        opencode_binary_path = None

    status = runtime_status_for("claude_sdk", _Row())
    assert status.source == "override"
    assert status.found is True
