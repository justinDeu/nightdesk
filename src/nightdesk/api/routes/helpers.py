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
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.domain import scopes as sc
from nightdesk.api.schemas import (
    CronPreviewOut, CronPreviewRequest, DiagnosticsOut, ProjectActivityFeed,
    ProjectActivityItem, ProjectActivityWeekRollup,
    WebhookTestRequest, WorktreeNamePreviewOut, WorktreeNamePreviewRequest,
)
from nightdesk.db.models import DaemonStatus
from nightdesk.domain.activity import (
    VALID_FILTERS, project_activity_feed,
)
from nightdesk.domain.cron_jobs import InvalidCronJob, validate_schedule, validate_timezone
from nightdesk.domain.notifications import build_test_payload, fire_webhook
from nightdesk.domain.projects import ProjectNotFound
from nightdesk.domain.worktree_preview import base_ref_status, preview_worktree_path


_WEBHOOK_URL_RE = re.compile(r"^https?://")


def _activity_item_out(it) -> "ProjectActivityItem":
    """Map a domain.activity.ActivityItem onto its Pydantic schema."""
    return ProjectActivityItem(
        id=it.id,
        kind=it.kind,
        ts=it.ts,
        title=it.title,
        outcome=it.outcome,
        duration_seconds=it.duration_seconds,
        tokens=it.tokens,
        cost_usd=it.cost_usd,
        run_id=it.run_id,
        ticket_id=it.ticket_id,
        to_status=it.to_status,
        repo_kind=it.repo_kind,
        repo_link_id=it.repo_link_id,
        external_iid=it.external_iid,
        external_url=it.external_url,
        state=it.state,
        diff_add=it.diff_add,
        diff_del=it.diff_del,
        skipped_reason=it.skipped_reason,
    )


def _rollup_out(r) -> "ProjectActivityWeekRollup":
    return ProjectActivityWeekRollup(
        week_start=r.week_start.date(),
        runs=r.runs,
        failures=r.failures,
        shipped=r.shipped,
        cost_usd=round(r.cost_usd, 4),
        success_rate=round(r.success_rate, 4),
    )


def _bwrap_version() -> Optional[str]:
    try:
        out = subprocess.run(
            ["bwrap", "--version"],
            check=False, capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() or out.stderr.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def build_router(get_session, bearer_token: str, scoped, *, worktree_root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["helpers"])
    auth = Depends(scoped(sc.TICKETS_READ))
    config_write = Depends(scoped(sc.CONFIG_WRITE))
    config_read = Depends(scoped(sc.CONFIG_READ))

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

    @router.post("/notifications/test", status_code=204, dependencies=[config_write])
    async def notifications_test(payload: WebhookTestRequest, request: Request):
        """Fire a synthetic run-completion payload at the given webhook URL."""
        target = payload.url.strip()
        if not target or not _WEBHOOK_URL_RE.match(target):
            raise HTTPException(422, "provide a valid http(s) webhook URL")
        base_url = str(request.base_url).rstrip("/")
        fire_webhook(target, build_test_payload(base_url))
        return None

    @router.get(
        "/projects/{project_id}/activity", response_model=ProjectActivityFeed,
        dependencies=[auth],
    )
    async def project_activity(
        project_id: str,
        kind: str = Query("all", description=f"filter chip: {', '.join(VALID_FILTERS)}"),
        q: Optional[str] = Query(None, description="server-side title search"),
        cursor: Optional[str] = Query(None, description="opaque cursor from next_cursor"),
        limit: int = Query(50, ge=1, le=200),
        include_rollups: bool = Query(False, description="weekly aggregates (first page only)"),
        session: Session = Depends(get_session),
    ):
        """Unified project activity feed — one merged, reverse-chronological,
        cursor-paginated stream of run outcomes, ticket lifecycle transitions,
        repo (MR/issue) events, and cron fires. Filters + search are server-side
        so a chip never lies about rows past the loaded window
        (docs/design/project-control-plane.md §History)."""
        try:
            feed = project_activity_feed(
                session, project_id, kind=kind, q=q, cursor=cursor,
                limit=limit, include_rollups=include_rollups,
            )
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        return ProjectActivityFeed(
            items=[_activity_item_out(it) for it in feed.items],
            rollups=[_rollup_out(r) for r in feed.rollups],
            next_cursor=feed.next_cursor,
            has_more=feed.has_more,
        )

    @router.get("/diagnostics", response_model=DiagnosticsOut, dependencies=[config_read])
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
