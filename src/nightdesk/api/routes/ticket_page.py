"""Ticket detail page routes.

Owns the HTML ticket detail page and the small write endpoints that page
needs: additional_dirs add/remove. The unified ticket actions (run-now,
cancel, requeue, archive, delete) are served by the JSON API at
``/api/v1/tickets/{id}/...``. This module's ``POST`` routes exist for form
posts that redirect back to the detail page.

Split out of ``ui.py`` so transcript-rich rendering and per-ticket scope
controls can evolve without colliding with the small login/settings
plumbing that ``ui.py`` still owns.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from sqlalchemy import select

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.db.models import TicketWorkspace
from nightdesk.domain.diff import compute_workspace_diff, select_diff_workspace
from nightdesk.domain.profiles import list_profiles
from nightdesk.domain.projects import list_projects
from nightdesk.domain.runs import get_run, list_runs, RunNotFound
from nightdesk.domain.toolchains import current_config, toolchain_options
from nightdesk.domain.labels import list_labels
from nightdesk.domain.tickets import (
    add_dependency, clone_ticket, continue_ticket, get_ticket, list_dependencies, list_dependents,
    list_tickets, merge_next_run_context_into_prompt, remove_dependency, restart_ticket,
    resume_ticket, retry_ticket, set_next_run_context, update_ticket,
    CyclicDependency, DependencyNotFound, TicketNotFound,
)
from nightdesk.logging_setup import run_log_path
from nightdesk.transcript import is_canonical, read_events


def _load_transcript(path: str | None) -> tuple[list[dict], str]:
    """Returns (canonical_events, raw_text). At most one is populated.

    - If ``path`` is None or missing: both empty.
    - If canonical NDJSON: events list, raw="".
    - Otherwise: events=[], raw=full file text.
    """
    if not path:
        return [], ""
    p = Path(path)
    if not p.exists():
        return [], ""
    if is_canonical(p):
        return list(read_events(p)), ""
    try:
        return [], p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], ""


def build_router(get_session, bearer_token: str, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(tags=["ticket-page"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    @router.get("/tickets/{tid}", response_class=HTMLResponse, dependencies=[auth])
    async def ticket_detail(tid: str, request: Request,
                             session: Session = Depends(get_session)):
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")

        runs = list_runs(session, ticket_id=tid)

        selected_run = runs[0] if runs else None
        events, raw_text = _load_transcript(
            selected_run.transcript_path if selected_run else None
        )

        # Map run_id → TicketWorkspace so the template can show per-run
        # worktree info (branch, resolved_path) without N+1 queries.
        run_workspaces: dict = {}
        for ws in (t.workspaces or []):
            if ws.run_id:
                run_workspaces[ws.run_id] = ws

        # Project settings warning: if the workspace has a project-level
        # .claude/settings.json the CC SDK will merge it with the profile
        # on top of nightdesk's own setting_sources=["project"] pass. The
        # user should know in case behavior is unexpected.
        project_settings_paths: list[str] = []
        workspace_paths = {
            path
            for ws in (t.workspaces or [])
            for path in (ws.resolved_path, ws.worktree_path, ws.source_path)
            if path
        }
        for wpath in workspace_paths:
            if not wpath:
                continue
            for name in (".claude/settings.json", ".claude/settings.local.json"):
                candidate = Path(wpath) / name
                if candidate.exists():
                    project_settings_paths.append(str(candidate))

        deps_upstreams = list_dependencies(session, tid)
        # Candidate tickets for the "Add dependency" control: every other
        # non-inbox ticket that isn't this one and isn't already an upstream
        # dependency. Inbox items are excluded: an unpromotable dependency
        # would silently block the dependent ticket forever.
        _upstream_ids = {d.id for d in deps_upstreams}
        _all_non_inbox = [
            c for c in list_tickets(session, limit=500)
            if c.status != "inbox"
        ]
        dep_candidates = [
            c for c in _all_non_inbox
            if c.id != tid and c.id not in _upstream_ids
        ]

        return templates.TemplateResponse(request, "ticket_detail.html", {
            "ticket": t,
            "runs": runs,
            "selected_run": selected_run,
            "live": selected_run is not None and selected_run.finished_at is None,
            "transcript_events": events,
            "transcript_raw": raw_text,
            "project_settings_paths": project_settings_paths,
            "run_workspaces": run_workspaces,
            "profiles": list_profiles(session),
            "deps_upstreams": deps_upstreams,
            "deps_downstreams": list_dependents(session, tid),
            "dep_candidates": dep_candidates,
            "dep_all": _all_non_inbox,
            "projects": list_projects(session),
            "toolchain_options": toolchain_options(current_config(session)),
            "all_labels": list_labels(session),
            "title": t.title,
            "active_page": "tickets",
        })

    @router.get("/tickets/{tid}/runs/{rid}/transcript-panel",
                response_class=HTMLResponse, dependencies=[auth])
    async def run_transcript_panel(tid: str, rid: str, request: Request,
                                     session: Session = Depends(get_session)):
        """HTMX target for run-history clicks on the detail page.

        Renders the same transcript_panel.html partial used at initial page
        load, but for an arbitrary run instead of the latest one.
        """
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        try:
            run = get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "run not found")
        if run.ticket_id != tid:
            raise HTTPException(404, "run not on this ticket")
        events, raw_text = _load_transcript(run.transcript_path)
        return templates.TemplateResponse(
            request, "partials/transcript_panel.html", {
                "ticket": t,
                "selected_run": run,
                "live": run.finished_at is None,
                "transcript_events": events,
                "transcript_raw": raw_text,
            },
        )

    @router.get("/tickets/{tid}/runs/{rid}/diff-panel",
                response_class=HTMLResponse, dependencies=[auth])
    async def run_diff_panel(tid: str, rid: str, request: Request,
                              session: Session = Depends(get_session)):
        """HTMX target for the Changes tab on the detail page.

        Computes the diff for the run's workspace and renders the
        partial diff panel template.
        """
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        try:
            run = get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "run not found")
        if run.ticket_id != tid:
            raise HTTPException(404, "run not on this ticket")

        ws = select_diff_workspace(_run_workspaces(session, rid, run.ticket_id))
        diff_result = compute_workspace_diff(
            ws,
            transcript_root=Path(run.transcript_path).parent,
            run_id=rid,
        )

        return templates.TemplateResponse(
            request, "partials/diff_panel.html", {
                "ticket": t,
                "selected_run": run,
                "diff": diff_result,
                "workspace": ws,
            },
        )

    @router.get("/runs/{rid}/log", dependencies=[auth])
    async def download_run_log(rid: str, session: Session = Depends(get_session)):
        """Cookie-auth download of a per-run worker log.

        The browser opens this as a plain ``<a href>`` GET that carries the
        signed session cookie, not a bearer header — so it can't hit the
        bearer-only ``/api/v1/runs/{rid}/log`` route without a 401. Same fix
        already applied to Archive / Cancel / Delete. The JSON-API route is
        kept for programmatic clients.
        """
        try:
            get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")
        path = run_log_path(rid)
        if not path.exists():
            return PlainTextResponse("(no log for this run)", status_code=404)
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
            filename=f"nightdesk-run-{rid}.log",
        )

    @router.post("/tickets/{tid}/additional-dirs", dependencies=[auth])
    async def add_additional_dir(
        tid: str, request: Request,
        path: str = Form(...), mode: str = Form("rw"),
        session: Session = Depends(get_session),
    ):
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        clean = path.strip()
        if not clean.startswith("/"):
            raise HTTPException(422, "path must be absolute")
        if mode not in ("rw", "ro"):
            raise HTTPException(422, "mode must be rw or ro")
        dirs = list(t.additional_dirs or [])
        # Idempotent add: skip duplicates by path.
        if not any((d.get("path") == clean) for d in dirs):
            dirs.append({"path": clean, "mode": mode})
            update_ticket(session, tid, additional_dirs=dirs)
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/additional-dirs/delete", dependencies=[auth])
    async def remove_additional_dir_form(
        tid: str, path: str = Form(...),
        session: Session = Depends(get_session),
    ):
        """Form-style delete (HTML forms can't send DELETE without JS)."""
        return _remove_dir(session, tid, path)

    @router.delete("/tickets/{tid}/additional-dirs", dependencies=[auth])
    async def remove_additional_dir(
        tid: str, path: str = Query(...),
        session: Session = Depends(get_session),
    ):
        return _remove_dir(session, tid, path)


    @router.post("/tickets/{tid}/continue", dependencies=[auth])
    async def continue_form(
        tid: str,
        next_run_context: str = Form(""),
        session: Session = Depends(get_session),
    ):
        """Continue the prior run's Claude Code conversation.

        Distinct from /resume: resume starts a fresh-context agent on the same
        worktree (no message history); continue replays the parent run's SDK
        session id so the new run resumes the full prior conversation. Falls
        back to a fresh-context resume transparently when no session is
        available.
        """
        try:
            continue_ticket(session, tid, next_run_context=next_run_context)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except Exception as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/resume", dependencies=[auth])
    async def resume_form(
        tid: str,
        next_run_context: str = Form(""),
        session: Session = Depends(get_session),
    ):
        try:
            resume_ticket(session, tid, next_run_context=next_run_context)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except Exception as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/retry", dependencies=[auth])
    async def retry_form(
        tid: str,
        next_run_context: str = Form(""),
        session: Session = Depends(get_session),
    ):
        try:
            retry_ticket(session, tid, next_run_context=next_run_context)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except Exception as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/restart", dependencies=[auth])
    async def restart_form(
        tid: str,
        next_run_context: str = Form(""),
        workspace_policy: str = Form(...),
        session: Session = Depends(get_session),
    ):
        try:
            restart_ticket(
                session,
                tid,
                next_run_context=next_run_context,
                workspace_policy=workspace_policy,
            )
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ValueError as e:
            raise HTTPException(422, str(e))
        except Exception as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/clone", dependencies=[auth])
    async def clone_form(
        tid: str,
        title: str = Form(""),
        carry_context: bool = Form(False),
        session: Session = Depends(get_session),
    ):
        try:
            cloned = clone_ticket(
                session, tid, title=title.strip() or None, carry_context=carry_context
            )
        except TicketNotFound:
            raise HTTPException(404, "not found")
        return RedirectResponse(url=f"/tickets/{cloned.id}", status_code=303)

    @router.post("/tickets/{tid}/next-run-context", dependencies=[auth])
    async def next_run_context_form(
        tid: str,
        body: str = Form(""),
        session: Session = Depends(get_session),
    ):
        try:
            set_next_run_context(session, tid, body)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/merge-next-run-context", dependencies=[auth])
    async def merge_next_run_context_form(
        tid: str,
        session: Session = Depends(get_session),
    ):
        try:
            merge_next_run_context_into_prompt(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/dependencies", dependencies=[auth])
    async def add_dependency_form(
        tid: str,
        depends_on_id: str = Form(...),
        session: Session = Depends(get_session),
    ):
        """Cookie-auth UI route to add an upstream dependency.

        Mirrors the other form posts on this page: redirects back to the
        detail page (303) so the Dependency chain section re-renders.
        """
        try:
            add_dependency(session, tid, depends_on_id.strip())
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except CyclicDependency as e:
            raise HTTPException(422, str(e))
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.post("/tickets/{tid}/dependencies/{dep_on_id}/delete", dependencies=[auth])
    async def remove_dependency_form(
        tid: str, dep_on_id: str,
        session: Session = Depends(get_session),
    ):
        """Cookie-auth UI route to remove an upstream dependency."""
        try:
            remove_dependency(session, tid, dep_on_id)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except DependencyNotFound:
            # Idempotent: already gone is fine, just redirect back.
            pass
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    return router


def _remove_dir(session: Session, tid: str, path: str):
    try:
        t = get_ticket(session, tid)
    except TicketNotFound:
        raise HTTPException(404, "not found")
    target = path.strip()
    dirs = [d for d in (t.additional_dirs or []) if d.get("path") != target]
    update_ticket(session, tid, additional_dirs=dirs)
    return RedirectResponse(url=f"/tickets/{tid}", status_code=303)


def _run_workspaces(session: Session, run_id: str, ticket_id: str) -> list:
    """Candidate workspaces for a run, run-scoped rows first.

    Prefer workspaces bound to this run; fall back to the ticket's workspaces
    (legacy rows without a stamped run_id). The caller selects the diffable one
    via ``select_diff_workspace`` -- identical to the JSON API path so the two
    surfaces agree for every workspace kind.
    """
    run_scoped = list(session.execute(
        select(TicketWorkspace)
        .where(TicketWorkspace.run_id == run_id)
        .order_by(TicketWorkspace.position)
    ).scalars())
    if run_scoped:
        return run_scoped
    return list(session.execute(
        select(TicketWorkspace)
        .where(TicketWorkspace.ticket_id == ticket_id)
        .order_by(TicketWorkspace.position)
    ).scalars())
