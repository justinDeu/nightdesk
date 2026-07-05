"""JSON analytics API: the same numbers ``/analytics`` renders, as clean JSON.

Reuses ``domain.analytics.build_dashboard`` (the single source of truth the
HTML dashboard also calls) so the two surfaces can never disagree. All four
routes compute the dashboard once per request and slice the piece the caller
asked for; there is no separate aggregation path here. Every route accepts an
optional ``project_id`` that scopes the whole response to one project's
tickets (404 if the project doesn't exist) — it flows straight into
``build_dashboard``, which threads it through every underlying aggregate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.schemas import (
    AnalyticsLatencyOut, AnalyticsSpendOut, AnalyticsSummaryOut, AnalyticsTokensOut,
)
from nightdesk.db.models import ConfigRow
from nightdesk.domain import analytics, pricing
from nightdesk.domain.projects import ProjectNotFound, get_project


_RANGE_DAYS = {"today": 1, "7d": 7, "30d": 30}
_RANGE_WINDOW_KEY = {"today": "today", "7d": "last_7d", "30d": "last_30d"}


def _resolve_tz(session: Session) -> ZoneInfo:
    """Same timezone resolution as the HTML analytics page and the scheduler."""
    cfg = session.get(ConfigRow, 1)
    tz_name = cfg.schedule_timezone if cfg and cfg.schedule_timezone else "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def _check_project(session: Session, project_id: str | None) -> None:
    if project_id is None:
        return
    try:
        get_project(session, project_id)
    except ProjectNotFound:
        raise HTTPException(404, "project not found")


async def _dashboard(request: Request, session: Session, *, project_id: str | None) -> dict:
    _check_project(session, project_id)
    now = datetime.now(timezone.utc)
    tz = _resolve_tz(session)
    price_info = await run_in_threadpool(
        pricing.resolve_prices,
        getattr(request.app.state, "data_dir", None),
        url=getattr(request.app.state, "pricing_url", None),
        now=now,
    )
    return analytics.build_dashboard(
        session, now=now, price_info=price_info, tz=tz, project_id=project_id,
    )


def _tail(rows: list, n: int) -> list:
    """Last ``n`` entries of a day-ordered series (oldest-first)."""
    return rows[-n:] if n < len(rows) else rows


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/summary", response_model=AnalyticsSummaryOut, dependencies=[auth])
    async def summary(
        request: Request,
        project_id: str | None = Query(default=None),
        session: Session = Depends(get_session),
    ):
        data = await _dashboard(request, session, project_id=project_id)
        return AnalyticsSummaryOut(
            price_source=data["price_source"],
            price_as_of=data["price_as_of"],
            price_source_label=data["price_source_label"],
            today=data["today"],
            last_7d=data["last_7d"],
            last_30d=data["last_30d"],
            run_stats=data["run_stats"],
            duration=data["duration"],
            by_project=data["by_project"],
        )

    @router.get("/spend", response_model=AnalyticsSpendOut, dependencies=[auth])
    async def spend(
        request: Request,
        range: str = Query(default="30d"),
        project_id: str | None = Query(default=None),
        session: Session = Depends(get_session),
    ):
        rng = range if range in _RANGE_DAYS else "30d"
        data = await _dashboard(request, session, project_id=project_id)
        series = _tail(data["daily_series"], _RANGE_DAYS[rng])
        return AnalyticsSpendOut(
            range=rng,
            project_id=project_id,
            totals=data[_RANGE_WINDOW_KEY[rng]],
            by_model=data["by_model"],
            by_profile=data["by_profile"],
            by_ticket=data["by_ticket"],
            by_project=data["by_project"],
            daily_series=series,
            price_source=data["price_source"],
            price_as_of=data["price_as_of"],
        )

    @router.get("/tokens", response_model=AnalyticsTokensOut, dependencies=[auth])
    async def tokens(
        request: Request,
        range: str = Query(default="30d"),
        project_id: str | None = Query(default=None),
        session: Session = Depends(get_session),
    ):
        rng = range if range in _RANGE_DAYS else "30d"
        data = await _dashboard(request, session, project_id=project_id)
        series = _tail(data["daily_series"], _RANGE_DAYS[rng])
        return AnalyticsTokensOut(
            range=rng,
            project_id=project_id,
            by_model=data["by_model"],
            model_legend=data["model_legend"],
            daily_series=series,
            max_daily_tokens=max((d["total_tokens"] for d in series), default=0),
        )

    @router.get("/latency", response_model=AnalyticsLatencyOut, dependencies=[auth])
    async def latency(
        request: Request,
        range: str = Query(default="30d"),
        project_id: str | None = Query(default=None),
        session: Session = Depends(get_session),
    ):
        rng = range if range in _RANGE_DAYS else "30d"
        data = await _dashboard(request, session, project_id=project_id)
        series = _tail(data["latency_series"], _RANGE_DAYS[rng])
        max_lat = max(
            (med for d in series for med in d["by_model"].values()), default=0.0,
        )
        return AnalyticsLatencyOut(
            range=rng,
            project_id=project_id,
            latency_by_model=data["latency_by_model"],
            latency_series=series,
            latency_model_legend=data["latency_model_legend"],
            max_daily_latency=max_lat,
            model_vs_tool_time=data["model_vs_tool_time"],
        )

    return router
