"""Inbox: triage surface for under-specified work.

The inbox is a dedicated surface, separate from the runnable board, where
captured-but-under-specified tickets (``status='inbox'``) wait to be triaged.
From here a ticket can be:

  * promoted (accepted) onto the board as a ``draft`` or queued directly,
  * fleshed out / edited (via the ticket detail editor),
  * labelled, re-prioritised, or assigned to a project inline (reusing the
    shared property + label pickers), or
  * declined (archived).

Promotion crosses the incomplete-ticket validation boundary in
``domain.tickets`` — an item missing a workspace cannot be promoted until it is
fleshed out, but it can always be declined.

These are HTML/HTMX routes (cookie-or-bearer auth), mounted at the app root.
The shared per-property picker (``/board/tickets/{id}/picker|property/...``)
and label routes (``/board/tickets/{id}/labels``) are reused as-is for inline
metadata edits, so this module owns only the inbox page, its list fragment, and
the promote/decline/capture actions.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.routes.board import (
    reconcile_deps_from_form,
    reconcile_labels_from_form,
    ticket_fields_from_form,
)
from nightdesk.domain.labels import list_labels
from nightdesk.domain.profiles import list_profiles
from nightdesk.domain.projects import (
    ProjectNotFound,
    get_project_by_slug,
    list_projects,
)
from nightdesk.domain.toolchains import current_config, toolchain_options
from nightdesk.domain.tickets import (
    IncompleteTicket,
    InvalidTransition,
    TicketNotFound,
    create_ticket,
    decline_ticket,
    get_ticket,
    list_inbox,
    list_tickets,
    promote_ticket,
    update_ticket,
)

# Execution-context preview endpoint (HTML), owned by the optional
# execution-context work. The promote modal renders a live preview when this
# route is mounted and leaves the hook dormant when it is not — so the flow
# lights up automatically once that work lands, with no inbox-side change.
_PREVIEW_PATH = "/effective-config/preview"


def build_router(
    get_session,
    bearer_token: str,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter(tags=["inbox"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    def _resolve_project(session: Session, project: str):
        """Map the ``?project=`` slug to (project_id_filter, selected_project).

        ``""`` → all items; ``"none"`` → unassigned only; a slug → that project.
        An unknown slug yields a filter that matches nothing so the surface
        stays coherent rather than silently showing everything.
        """
        project = (project or "").strip()
        if not project:
            return None, None
        if project in ("none", "null"):
            return "null", None
        try:
            selected = get_project_by_slug(session, project)
        except ProjectNotFound:
            return "__missing__", None
        return selected.id, selected

    def _gather(session: Session, *, project: str):
        project_filter, selected_project = _resolve_project(session, project)
        if project_filter == "__missing__":
            tickets: list = []
        else:
            tickets = list_inbox(session, project_id=project_filter)
        projects = list_projects(session)
        profiles = list_profiles(session)
        return {
            "tickets": tickets,
            "count": len(tickets),
            "projects": projects,
            "projects_by_id": {p.id: p for p in projects},
            "profiles": profiles,
            "profiles_by_id": {p.id: p for p in profiles},
            "all_labels": list_labels(session),
            "selected_project": selected_project,
            "project_filter": project,
        }

    def _list_response(request: Request, session: Session, *, project: str) -> HTMLResponse:
        ctx = _gather(session, project=project)
        return templates.TemplateResponse(request, "partials/inbox_list.html", ctx)

    def _preview_url(request: Request) -> Optional[str]:
        """Return the execution-context preview endpoint iff it is mounted.

        Keeps the promote-modal preview a clean, optional hook: present only
        when the execution-context work is available, absent otherwise.
        """
        for route in request.app.routes:
            if getattr(route, "path", None) == _PREVIEW_PATH:
                return _PREVIEW_PATH
        return None

    @router.get("/inbox", response_class=HTMLResponse, dependencies=[auth])
    async def inbox_page(
        request: Request,
        project: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        ctx = _gather(session, project=project)
        ctx["title"] = "Inbox"
        ctx["active_page"] = "inbox"
        return templates.TemplateResponse(request, "inbox.html", ctx)

    @router.get("/inbox/list", response_class=HTMLResponse, dependencies=[auth])
    async def inbox_list(
        request: Request,
        project: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        """Refreshable list fragment swapped in after an inbox action."""
        return _list_response(request, session, project=project)

    @router.get(
        "/inbox/tickets/{tid}/promote-modal",
        response_class=HTMLResponse,
        dependencies=[auth],
    )
    async def promote_modal(
        request: Request,
        tid: str,
        project: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        """The full edit/create modal in *promote* mode for one inbox item.

        Reuses ``partials/ticket_edit_modal.html`` (so the picker, workspace,
        toolchain, label, and dependency primitives are never duplicated). The
        modal is pre-filled from the inbox item, highlights the required
        execution fields that are still missing, and submits to the promote
        action below — letting the user satisfy the requirements and promote in
        one step.
        """
        try:
            ticket = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        if ticket.status != "inbox":
            raise HTTPException(409, f"cannot promote from {ticket.status}")
        return templates.TemplateResponse(
            request,
            "partials/ticket_edit_modal.html",
            {
                "ticket": ticket,
                "modal_id": "inbox-promote-modal",
                "promote": True,
                "project_filter": project,
                "profiles": list_profiles(session),
                "projects": list_projects(session),
                "all_labels": list_labels(session),
                # Inbox items are excluded: an unpromotable dependency would
                # silently block the dependent ticket forever.
                "dep_all": [
                    t
                    for t in list_tickets(session, limit=500)
                    if t.status != "inbox"
                ],
                "toolchain_options": toolchain_options(current_config(session)),
                # Optional execution-context preview hook — only set when the
                # endpoint is mounted (see _preview_url / _PREVIEW_PATH).
                "effective_preview_url": _preview_url(request),
            },
        )

    @router.post("/inbox/tickets/{tid}/promote", dependencies=[auth])
    async def promote(
        request: Request,
        tid: str,
        session: Session = Depends(get_session),
    ):
        """Accept an inbox item onto the board (``draft`` or ``queued``).

        Two shapes of submission are accepted:

          * the quick promote buttons on an already-complete inbox row, which
            post only ``target`` (+ ``project``); and
          * the promote modal, which posts the full edit form — here we persist
            the user's edits first (reusing the shared edit-form parsing), then
            cross the completeness boundary.

        Either way, ``promote_ticket`` enforces the validation boundary: a
        queued/draft promotion with required execution fields still missing is
        rejected with 422 and the item stays in the inbox.
        """
        form = await request.form()
        target = (form.get("target") or "draft").strip() or "draft"
        project = form.get("project") or ""
        # Full-form submission (the modal): persist edits before promoting so the
        # workspace/profile the user just filled in count toward completeness.
        if form.get("workspace_form") == "1" or "title" in form:
            try:
                fields = ticket_fields_from_form(form)
                update_ticket(session, tid, **fields)
            except TicketNotFound:
                raise HTTPException(404, "not found")
            except ProjectNotFound:
                raise HTTPException(404, "project not found")
            except ValueError as e:
                raise HTTPException(422, str(e))
            reconcile_deps_from_form(session, tid, form)
            reconcile_labels_from_form(session, tid, form)
        try:
            promote_ticket(session, tid, target)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except IncompleteTicket as e:
            # 422 (unprocessable) is more accurate than 409 here: the item is
            # well-formed for the inbox but missing fields a run needs. The
            # message lists exactly what to flesh out.
            raise HTTPException(422, "; ".join(e.reasons))
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        if request.headers.get("HX-Request") == "true":
            return _list_response(request, session, project=project)
        return RedirectResponse(url="/inbox", status_code=303)

    @router.post("/inbox/tickets/{tid}/decline", dependencies=[auth])
    async def decline(
        request: Request,
        tid: str,
        project: str = Form(""),
        session: Session = Depends(get_session),
    ):
        """Decline (archive) an inbox item. Always allowed."""
        try:
            decline_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        if request.headers.get("HX-Request") == "true":
            return _list_response(request, session, project=project)
        return RedirectResponse(url="/inbox", status_code=303)

    @router.post("/inbox/capture", dependencies=[auth])
    async def capture(
        request: Request,
        title: str = Form(...),
        prompt: str = Form(""),
        project_id: str = Form(""),
        project: str = Form(""),
        session: Session = Depends(get_session),
    ):
        """Capture a new, possibly under-specified item straight into the inbox.

        Only a title is required. A workspace is intentionally optional — that
        is what makes the item "under-specified"; it must be fleshed out before
        promotion. A profile is required by the data model, so we default to the
        first available one when the capturer doesn't care yet.
        """
        title = (title or "").strip()
        if not title:
            raise HTTPException(422, "a title is required")
        profiles = list_profiles(session)
        if not profiles:
            raise HTTPException(422, "create a profile before capturing inbox items")
        proj_id = (project_id or "").strip() or None
        try:
            create_ticket(
                session,
                title=title,
                prompt=(prompt or "").strip(),
                status="inbox",
                profile_id=profiles[0].id,
                project_id=proj_id,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        if request.headers.get("HX-Request") == "true":
            return _list_response(request, session, project=project)
        return RedirectResponse(url="/inbox", status_code=303)

    return router
