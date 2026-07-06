"""Backend binary runtime status.

Mirrors the resolution chains the worker actually uses so the Settings UI
never reports a status that disagrees with what a run would launch:

- Claude Code: ``worker/sandbox.py``'s ``spec.claude_binary_path or
  shutil.which("claude") or "/usr/local/bin/claude"`` fallback chain, with
  the global default coming from ``ConfigRow.claude_binary_path``.
- opencode: ``backends/opencode.py``'s ``_resolve_binary`` chain
  (``opencode_binary_path`` override, then ``shutil.which("opencode")``,
  then ``~/.opencode/bin/opencode``).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional


_VERSION_TIMEOUT_S = 3.0
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
_CLAUDE_DEFAULT_PATH = "/usr/local/bin/claude"
_OPENCODE_DEFAULT_PATH = os.path.expanduser("~/.opencode/bin/opencode")


@dataclass(frozen=True)
class BackendRuntimeStatus:
    binary_path_override: Optional[str]
    resolved_path: Optional[str]
    source: Optional[str]  # "override" | "path" | "default"
    found: bool
    version: Optional[str]


def _is_executable(path: Optional[str]) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _probe_version(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        out = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
        )
    except Exception:
        return None
    text = (out.stdout + out.stderr).strip()
    if not text:
        return None
    m = _VERSION_RE.search(text)
    return m.group(1) if m else text


def _status(override: Optional[str], which_name: str, default_path: str) -> BackendRuntimeStatus:
    if override:
        resolved, source = override, "override"
    else:
        found_on_path = shutil.which(which_name)
        resolved, source = (found_on_path, "path") if found_on_path else (default_path, "default")
    found = _is_executable(resolved)
    version = _probe_version(resolved) if found else None
    return BackendRuntimeStatus(
        binary_path_override=override,
        resolved_path=resolved,
        source=source,
        found=found,
        version=version,
    )


def claude_runtime_status(override: Optional[str]) -> BackendRuntimeStatus:
    return _status(override, "claude", _CLAUDE_DEFAULT_PATH)


def opencode_runtime_status(override: Optional[str]) -> BackendRuntimeStatus:
    return _status(override, "opencode", _OPENCODE_DEFAULT_PATH)


def runtime_status_for(code: str, config_row) -> Optional[BackendRuntimeStatus]:
    """Return runtime status for a backend code, or ``None`` if unsupported."""
    if code == "claude_sdk":
        override = getattr(config_row, "claude_binary_path", None) if config_row else None
        return claude_runtime_status(override)
    if code == "opencode":
        override = getattr(config_row, "opencode_binary_path", None) if config_row else None
        return opencode_runtime_status(override)
    return None
