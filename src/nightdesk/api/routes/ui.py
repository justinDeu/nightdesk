from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.domain.tickets import (
    archive, requeue, request_run_now, transition_status,
    TicketNotFound, InvalidTransition,
)


def build_router(get_session, bearer_token: str, templates: Jinja2Templates,
                  *, transcript_root: Path) -> APIRouter:
    router = APIRouter(tags=["ui"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return templates.TemplateResponse(request, "login_body.html", {
            "title": "Login",
        })

    @router.post("/login")
    async def login(token: str = Form(...)):
        if token != bearer_token:
            raise HTTPException(401, "bad token")
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("nightdesk_token", token, httponly=True, samesite="strict")
        return resp

    @router.post("/tickets/{tid}/run-now", dependencies=[auth])
    async def ui_run_now(
        request: Request,
        tid: str,
        session: Session = Depends(get_session),
    ):
        # request_run_now does both: set run_now=true AND transition
        # draft/review/archived -> queued. Without the transition the flag
        # sticks but the scheduler's WHERE status='queued' filter keeps the
        # ticket parked forever, which is exactly the bug we're fixing.
        try:
            request_run_now(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            # Currently only fires for status='running'. Returning 409 lets
            # the HTMX layer surface a real error instead of silently
            # claiming the click was handled.
            raise HTTPException(409, str(e))
        # The hard requirement on this ticket is that clicking Run-now from
        # the board MUST NOT navigate. HTMX clients get a 204 with an
        # HX-Trigger event so the page can flash feedback without a reload.
        # Plain (curl, no-JS) clients keep the legacy 303 fallback so the
        # endpoint remains usable from a shell.
        if request.headers.get("HX-Request") == "true":
            resp = Response(status_code=204)
            resp.headers["HX-Trigger"] = "nd-run-now-queued"
            return resp
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/cancel", dependencies=[auth])
    async def ui_cancel(tid: str, session: Session = Depends(get_session)):
        try:
            transition_status(session, tid, "cancelled")
        except (TicketNotFound, InvalidTransition) as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/requeue", dependencies=[auth])
    async def ui_requeue(tid: str, session: Session = Depends(get_session)):
        try:
            requeue(session, tid)
        except (TicketNotFound, InvalidTransition) as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/archive", dependencies=[auth])
    async def ui_archive(
        request: Request,
        tid: str,
        session: Session = Depends(get_session),
    ):
        # Cookie-auth twin of POST /api/v1/tickets/{tid}/archive. The JSON
        # route uses bearer-only auth, so the browser's cookie-only session
        # would 401 there; clicking Archive from the detail page would then
        # silently fail while the after-request handler still navigated the
        # user away. Owning a UI-side endpoint keeps the browser path honest
        # and gives the template a stable success/failure contract.
        try:
            archive(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        # HTMX: 204 No Content; the template gates navigation on
        # event.detail.successful so failure responses don't yank the user
        # off the page. Non-HTMX (curl, no-JS) clients keep the 303 fallback
        # back to the ticket detail page so the endpoint stays usable from
        # a shell.
        if request.headers.get("HX-Request") == "true":
            resp = Response(status_code=204)
            resp.headers["HX-Trigger"] = "nd-ticket-archived"
            return resp
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.get("/settings", response_class=HTMLResponse, dependencies=[auth])
    async def settings_page(request: Request, session: Session = Depends(get_session)):
        import shutil

        from nightdesk.db.models import ConfigRow
        cfg = session.get(ConfigRow, 1)
        return templates.TemplateResponse(
            request, "settings.html",
            {"title": "Settings", "active_page": "settings", "cfg": cfg,
             "path_claude_binary": shutil.which("claude"), "saved": False},
        )

    @router.post("/settings", response_class=HTMLResponse, dependencies=[auth])
    async def settings_save(
        request: Request,
        session: Session = Depends(get_session),
        max_parallel: int = Form(...),
        polling_interval_seconds: int = Form(...),
        window_start: str = Form("22:00"),
        window_end: str = Form("07:00"),
        always_on: str = Form(""),
        claude_binary_path: str = Form(""),
        cc_minimum_version: str = Form(""),
        worktree_base_ref: str = Form(""),
    ):
        import re

        from nightdesk.db.models import ConfigRow
        cfg = session.get(ConfigRow, 1)
        if cfg is None:
            cfg = ConfigRow(id=1, worktree_root="", transcript_root="")
            session.add(cfg)
        # Clamp to sane ranges so a fat-finger doesn't wedge the worker.
        cfg.max_parallel = max(1, min(int(max_parallel), 16))
        cfg.polling_interval_seconds = max(1, min(int(polling_interval_seconds), 300))

        # Work-hours block. "Always on" sends 00:00 -> 00:00; the scheduler's
        # in_window() reads equal start and end as "no restriction".
        hhmm_re = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
        if always_on:
            cfg.window_start = "00:00"
            cfg.window_end = "00:00"
        else:
            if hhmm_re.match(window_start or ""):
                cfg.window_start = window_start
            if hhmm_re.match(window_end or ""):
                cfg.window_end = window_end

        cfg.claude_binary_path = (claude_binary_path or "").strip() or None
        cfg.cc_minimum_version = (cc_minimum_version or "").strip() or cfg.cc_minimum_version
        cfg.worktree_base_ref = (worktree_base_ref or "").strip() or None
        session.commit()
        session.refresh(cfg)
        import shutil
        return templates.TemplateResponse(
            request, "settings.html",
            {"title": "Settings", "active_page": "settings", "cfg": cfg,
             "path_claude_binary": shutil.which("claude"), "saved": True},
        )

    return router
