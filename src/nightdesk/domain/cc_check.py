"""CC binary version check.

Resolves the ``claude`` binary, runs ``claude --version``, and compares
the parsed version against the configured floor. The result is persisted
to the ``daemon_status`` table so the UI can show a banner when CC is
missing or too old.

The check is intentionally non-fatal for the daemon: it boots and serves
the UI even when the check fails. The worker refuses to dispatch new runs
when ``cc_check_status != "ok"``.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from packaging.version import InvalidVersion, Version
from sqlalchemy.orm import Session

from nightdesk.config import NightdeskConfig
from nightdesk.db.models import ConfigRow, DaemonStatus
from nightdesk.db.session import make_engine, session_factory


log = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")

_DEFAULT_FLOOR = "2.1.80"


class CcStatus(str, Enum):
    ok = "ok"
    too_old = "too_old"
    missing = "missing"
    error = "error"


@dataclass
class CcCheckResult:
    status: CcStatus
    message: str
    binary_path: Optional[str] = None
    version: Optional[str] = None


def _resolve_binary(cfg: NightdeskConfig, engine) -> Optional[str]:
    """Return the claude binary path from ConfigRow, then PATH."""
    try:
        Session = session_factory(engine)
        with Session() as s:
            row = s.get(ConfigRow, 1)
            if row and row.claude_binary_path:
                p = Path(row.claude_binary_path)
                if p.is_file():
                    return str(p)
    except Exception:
        log.debug("could not read ConfigRow for binary path", exc_info=True)

    return shutil.which("claude")


def _floor_version(cfg: NightdeskConfig, engine) -> str:
    """Return the configured minimum CC version string."""
    try:
        Session = session_factory(engine)
        with Session() as s:
            row = s.get(ConfigRow, 1)
            if row and row.cc_minimum_version:
                return row.cc_minimum_version
    except Exception:
        pass
    return _DEFAULT_FLOOR


def check_cc_binary(
    cfg: NightdeskConfig,
    *,
    binary_path: Optional[str] = None,
    floor: Optional[str] = None,
) -> CcCheckResult:
    """Resolve and validate the CC binary.

    Args:
        cfg: Nightdesk config (used to build a DB engine for ConfigRow lookup).
        binary_path: Override the resolved path (used in tests / setup).
        floor: Override the minimum version string (used in tests / setup).

    Returns:
        A ``CcCheckResult`` with ``status``, ``message``, ``binary_path``,
        and ``version``.
    """
    engine = make_engine(cfg.db_path)
    try:
        resolved = binary_path or _resolve_binary(cfg, engine)
        min_ver = floor or _floor_version(cfg, engine)
    finally:
        engine.dispose()

    if not resolved:
        return CcCheckResult(
            status=CcStatus.missing,
            message=(
                "claude binary not found. "
                "Install from https://docs.claude.com/en/docs/claude-code/quickstart"
            ),
        )

    try:
        out = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return CcCheckResult(
            status=CcStatus.missing,
            message=f"claude binary not found at {resolved!r}.",
            binary_path=resolved,
        )
    except subprocess.TimeoutExpired:
        return CcCheckResult(
            status=CcStatus.error,
            message=f"claude --version timed out after 5s at {resolved!r}.",
            binary_path=resolved,
        )
    except Exception as exc:
        return CcCheckResult(
            status=CcStatus.error,
            message=f"claude --version failed: {exc}",
            binary_path=resolved,
        )

    text = (out.stdout + out.stderr).strip()
    m = _VERSION_RE.search(text)
    if not m:
        return CcCheckResult(
            status=CcStatus.error,
            message=f"could not parse version from: {text!r}",
            binary_path=resolved,
        )

    version_str = m.group(1)
    try:
        actual = Version(version_str)
        minimum = Version(min_ver)
    except InvalidVersion as exc:
        return CcCheckResult(
            status=CcStatus.error,
            message=f"unparseable version string: {exc}",
            binary_path=resolved,
            version=version_str,
        )

    if actual < minimum:
        return CcCheckResult(
            status=CcStatus.too_old,
            message=(
                f"claude {version_str} is below the minimum {min_ver}. "
                "Run `claude update` or reinstall from "
                "https://docs.claude.com/en/docs/claude-code/quickstart"
            ),
            binary_path=resolved,
            version=version_str,
        )

    return CcCheckResult(
        status=CcStatus.ok,
        message=f"claude {version_str} ok (>= {min_ver})",
        binary_path=resolved,
        version=version_str,
    )


def persist_cc_check(result: CcCheckResult, session: Session) -> None:
    """Write or update the ``daemon_status`` row (id=1) with the check result."""
    row = session.get(DaemonStatus, 1)
    if row is None:
        row = DaemonStatus(id=1)
        session.add(row)
    row.cc_check_status = result.status.value
    row.cc_check_message = result.message
    row.cc_binary_path = result.binary_path
    row.cc_version = result.version
    row.last_checked_at = datetime.now(timezone.utc)
    session.commit()
