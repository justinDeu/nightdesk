"""Effective execution-context resolver endpoints.

Two surfaces over one resolver (:mod:`nightdesk.domain.effective_config`):

* JSON  -- ``GET /api/v1/tickets/{tid}/effective-config`` and
           ``POST /api/v1/effective-config/preview`` for programmatic clients.
* HTMX  -- ``GET /tickets/{tid}/effective-config`` and
           ``POST /effective-config/preview`` render the reusable
           ``partials/effective_config.html`` partial for the ticket preview,
           project-defaults preview, promote-from-inbox, and profile/backend
           UX to swap in.

The draft preview accepts the same fields ``create_ticket`` does, so a preview
reflects exactly what would be persisted (project defaults are applied).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer, require_token_cookie_or_bearer
from nightdesk.domain.effective_config import (
    resolve_for_draft,
    resolve_for_ticket,
)
from nightdesk.domain.tickets import get_ticket, TicketNotFound


# Scalar fields the draft editors post directly. Structured fields
# (workspaces / toolchain overrides / additional dirs) are parsed from the
# ticket form below via the shared board helpers so a live preview reflects
# exactly what ``create_ticket`` would persist.
_SCALAR_DRAFT_FIELDS = (
    "profile_id", "project_id", "source_path",
    "workspace_mode", "base_ref", "title", "prompt",
)


def _draft_from_form(form) -> dict:
    """Normalize a ticket create/edit form post into a draft dict.

    Reuses the same form parsers the board create/update routes use so the
    preview never re-implements workspace / toolchain / additional-dir
    interpretation. Parsing is *lenient*: a half-typed or absent path must not
    422 the preview, so validation errors fall back to a best-effort primary
    workspace built from the raw scalar fields. The resolver then reports the
    gap as a configuration note rather than the request failing.
    """
    # Imported lazily to avoid a route-module import cycle at app construction.
    from nightdesk.api.routes import board as board_routes

    fields: dict = {}
    for key in _SCALAR_DRAFT_FIELDS:
        if key in form and form[key] != "":
            fields[key] = form[key]

    # use_worktree drives the primary workspace kind in the board parser.
    has_workspace = bool(
        form.get("source_path") or form.get("primary_source_path")
        or form.get("use_worktree") or form.getlist("linked_workspace_path")
    )
    if has_workspace:
        try:
            _mode, _wn, _wp, workspaces = board_routes._workspace_payload_from_form(form)
            fields["workspaces"] = workspaces
        except HTTPException:
            # Best-effort primary so partial / mid-typing input still previews.
            raw = (form.get("source_path") or form.get("primary_source_path") or "").strip()
            kind = "git_worktree" if form.get("use_worktree") else "directory"
            fields["workspaces"] = [{
                "role": "primary",
                "label": "primary",
                "kind": kind,
                "access": "read_write",
                "source_path": raw or None,
                "worktree_name": (form.get("worktree_name") or "").strip() or None,
                "worktree_path": (form.get("worktree_path") or "").strip() or None,
                "base_ref": (form.get("base_ref") or "").strip() or None,
            }]

    if (form.get("toolchain_form") == "1" or form.getlist("toolchain_enable")
            or form.getlist("toolchain_disable") or form.getlist("toolchain_extra_paths")):
        fields["toolchain_overrides"] = board_routes._toolchain_overrides_from_form(form)

    if "additional_dirs" in form:
        try:
            fields["additional_dirs"] = board_routes._parse_additional_dirs(
                form.getlist("additional_dirs")
            )
        except HTTPException:
            pass

    return fields


def build_api_router(get_session, bearer_token: str) -> APIRouter:
    """JSON resolver endpoints (bearer auth)."""
    router = APIRouter(prefix="/api/v1", tags=["effective-config"])
    auth = Depends(require_bearer(bearer_token))

    @router.get("/tickets/{tid}/effective-config", dependencies=[auth])
    async def ticket_effective_config(tid: str, session: Session = Depends(get_session)):
        try:
            ticket = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        return resolve_for_ticket(session, ticket).as_dict()

    @router.post("/effective-config/preview", dependencies=[auth])
    async def preview_effective_config(
        payload: dict = Body(default_factory=dict),
        session: Session = Depends(get_session),
    ):
        return resolve_for_draft(session, payload or {}).as_dict()

    return router


def build_router(get_session, bearer_token: str, templates: Jinja2Templates) -> APIRouter:
    """HTMX partial resolver endpoints (cookie-or-bearer auth)."""
    router = APIRouter(tags=["effective-config-page"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/tickets/{tid}/effective-config",
                response_class=HTMLResponse, dependencies=[auth])
    async def ticket_effective_config_partial(
        tid: str, request: Request, session: Session = Depends(get_session),
    ):
        try:
            ticket = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        effective = resolve_for_ticket(session, ticket)
        return templates.TemplateResponse(
            request, "partials/effective_config.html",
            {"effective": effective},
        )

    @router.post("/effective-config/preview",
                 response_class=HTMLResponse, dependencies=[auth])
    async def preview_effective_config_partial(
        request: Request, session: Session = Depends(get_session),
    ):
        # Accept either a JSON body or an HTMX form post. Form posts arrive as
        # urlencoded fields; we read the common scalar fields used by the draft
        # editors and leave structured fields to the JSON path.
        fields: dict = {}
        ctype = request.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    fields = body
            except Exception:
                fields = {}
        else:
            form = await request.form()
            fields = _draft_from_form(form)
        effective = resolve_for_draft(session, fields)
        return templates.TemplateResponse(
            request, "partials/effective_config.html",
            {"effective": effective},
        )

    return router
