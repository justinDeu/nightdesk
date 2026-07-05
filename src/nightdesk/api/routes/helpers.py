"""Small JSON helper endpoints for the SPA: previews, webhook test,
project activity feed, and install diagnostics.

This file owns only the JSON wrapping; the underlying logic lives in the
domain layer (``domain.worktree_preview``, ``domain.cron_jobs``,
``domain.notifications``), never duplicated here.
"""
from __future__ import annotations

import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.schemas import (
    CronPreviewOut, CronPreviewRequest, DiagnosticsOut, ProjectActivityRow,
    WebhookTestRequest, WorktreeNamePreviewOut, WorktreeNamePreviewRequest,
)
from nightdesk.db.models import DaemonStatus, Run, Ticket
from nightdesk.domain.cron_jobs import InvalidCronJob, validate_schedule, validate_timezone
from nightdesk.domain.notifications import build_test_payload, fire_webhook
from nightdesk.domain.projects import ProjectNotFound, get_project
from nightdesk.domain.worktree_preview import base_ref_status, preview_worktree_path


_WEBHOOK_URL_RE = re.compile(r"^https?://")


def _bwrap_version() -> Optional[str]:
    try:
        out = subprocess.run(
            ["bwrap", "--version"],
            check=False, capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() or out.stderr.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def build_router(get_session, bearer_token: str, *, worktree_root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["helpers"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.post(
        "/preview/worktree-name", response_model=WorktreeNamePreviewOut,
        dependencies=[auth],
    )
    async def worktree_name_preview(payload: WorktreeNamePreviewRequest):
        """Previews where a git_worktree ticket's worktree would land."""
        if not payload.source_path.strip() and not (payload.path and payload.path.strip()):
            raise HTTPException(422, "source_path or path is required")
        try:
            preview_path, source = preview_worktree_path(
                source_path=payload.source_path or "/",
                name=payload.name,
                custom_path=payload.path,
                worktree_root=worktree_root,
            )
        except Exception as exc:
            raise HTTPException(422, f"preview unavailable: {exc}")
        ref = (payload.base_ref or "").strip()
        ref_status = (
            base_ref_status(payload.source_path, ref)
            if ref and payload.source_path.strip() else None
        )
        return WorktreeNamePreviewOut(
            path=str(preview_path),
            source=source,
            base_ref=ref or None,
            base_ref_status=ref_status,
        )

    @router.post(
        "/preview/cron", response_model=CronPreviewOut, dependencies=[auth],
    )
    async def cron_preview(payload: CronPreviewRequest):
        """Validate a cron expression and return its next N fire times."""
        try:
            expr = validate_schedule(payload.schedule)
            tz_name = validate_timezone(payload.timezone)
        except InvalidCronJob as e:
            raise HTTPException(422, str(e))
        tz = ZoneInfo(tz_name)
        base = datetime.now(tz)
        it = croniter(expr, base)
        fires = []
        for _ in range(payload.count):
            fires.append(it.get_next(datetime).astimezone(timezone.utc))
        return CronPreviewOut(next_fire_times=fires)

    @router.post("/notifications/test", status_code=204, dependencies=[auth])
    async def notifications_test(payload: WebhookTestRequest, request: Request):
        """Fire a synthetic run-completion payload at the given webhook URL."""
        target = payload.url.strip()
        if not target or not _WEBHOOK_URL_RE.match(target):
            raise HTTPException(422, "provide a valid http(s) webhook URL")
        base_url = str(request.base_url).rstrip("/")
        fire_webhook(target, build_test_payload(base_url))
        return None

    @router.get(
        "/projects/{project_id}/activity", response_model=list[ProjectActivityRow],
        dependencies=[auth],
    )
    async def project_activity(
        project_id: str, session: Session = Depends(get_session),
    ):
        """JSON twin of the HTML project activity pane: the ~30 most recent
        agent runs against tickets in this project, newest first."""
        try:
            get_project(session, project_id)
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        runs = list(session.scalars(
            select(Run)
            .join(Ticket, Run.ticket_id == Ticket.id)
            .where(Ticket.project_id == project_id)
            .order_by(Run.started_at.desc())
            .limit(30)
        ))
        rows = []
        for run in runs:
            ticket = run.ticket
            if ticket is None:
                continue
            tokens = (run.input_tokens or 0) + (run.output_tokens or 0)
            duration = None
            if run.started_at and run.finished_at:
                s = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
                f = run.finished_at if run.finished_at.tzinfo else run.finished_at.replace(tzinfo=timezone.utc)
                duration = (f - s).total_seconds()
            outcome = run.exit_status or ("running" if run.finished_at is None else "unknown")
            rows.append(ProjectActivityRow(
                run_id=run.id,
                ticket_id=ticket.id,
                ticket_title=ticket.title,
                outcome=outcome,
                duration_seconds=duration,
                tokens=tokens or None,
                started_at=run.started_at,
            ))
        return rows

    @router.get("/diagnostics", response_model=DiagnosticsOut, dependencies=[auth])
    async def diagnostics_json(session: Session = Depends(get_session)):
        """JSON twin of the HTML ``/diagnostics`` page (no log tails)."""
        ds = session.get(DaemonStatus, 1)
        return DiagnosticsOut(
            nightdesk_version=getattr(__import__("nightdesk"), "__version__", "0.1.0"),
            python_version=platform.python_version(),
            platform=platform.platform(),
            kernel=platform.release(),
            bwrap_version=_bwrap_version(),
            cc_check_status=getattr(ds, "cc_check_status", "unknown"),
            cc_version=getattr(ds, "cc_version", None),
            cc_binary_path=getattr(ds, "cc_binary_path", None),
            cc_check_message=getattr(ds, "cc_check_message", None),
        )

    return router
