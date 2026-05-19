"""Diagnostics page: surface system info + tail logs for bug reports."""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_admin
from nightdesk.api.deps import get_session_dep
from nightdesk.db.models import DaemonStatus


LOG_TAIL_LINES = 200


def _tail(path: Path, n: int) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            chunk = min(end, 64 * 1024)
            f.seek(end - chunk)
            data = f.read()
        return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _bwrap_version() -> Optional[str]:
    try:
        out = subprocess.run(
            ["bwrap", "--version"],
            check=False, capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() or out.stderr.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def build_router(*, bearer_token: str, engine: Engine, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])
    admin_dep = Depends(require_admin(bearer_token, engine))
    get_session = get_session_dep(engine)

    @router.get("/diagnostics", response_class=HTMLResponse)
    async def page(request: Request, _admin=admin_dep, session: Session = Depends(get_session)):
        from nightdesk import __version__ as nd_version  # may not exist; handle below

        log_root = Path(os.path.expanduser("~/.local/share/nightdesk/logs"))
        api_log = _tail(log_root / "api.log", LOG_TAIL_LINES)
        worker_log = _tail(log_root / "worker.log", LOG_TAIL_LINES)
        ds = session.get(DaemonStatus, 1)
        return templates.TemplateResponse(
            request,
            "diagnostics.html",
            {
                "active_page": "diagnostics",
                "nightdesk_version": getattr(__import__("nightdesk"), "__version__", "0.1.0"),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "kernel": platform.release(),
                "bwrap_version": _bwrap_version() or "(missing)",
                "cc_check_status": getattr(ds, "cc_check_status", "unknown"),
                "cc_version": getattr(ds, "cc_version", None),
                "cc_binary_path": getattr(ds, "cc_binary_path", None),
                "cc_check_message": getattr(ds, "cc_check_message", None),
                "log_root": str(log_root),
                "api_log": api_log,
                "worker_log": worker_log,
            },
        )

    return router
