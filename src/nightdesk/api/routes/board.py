from __future__ import annotations

import os
import html
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.domain.profiles import list_profiles
from nightdesk.domain.runs import get_run
from nightdesk.domain.tickets import (
    InvalidTransition,
    TicketNotFound,
    archive,
    create_ticket,
    delete_ticket,
    get_ticket,
    list_tickets,
    reorder_in_column,
    set_run_now,
    transition_status,
    transition_with_position,
    update_ticket,
)


_COLUMNS = [
    ("draft", "Draft"),
    ("queued", "Queued"),
    ("running", "Running"),
    ("review", "Review"),
]


def _parse_additional_dirs(values: list[str] | None) -> list[dict]:
    """Build the additional_dirs JSON from a list of form values.

    Each value is an absolute path. Empty values and duplicates are dropped.
    Non-absolute paths are silently skipped; the sidebar input should reject
    those client-side before submit.
    """
    if not values:
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for raw in values:
        p = (raw or "").strip()
        if not p:
            continue
        p = os.path.expanduser(p)
        if not p.startswith("/"):
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append({"path": p, "mode": "rw"})
    return out


def _normalize_cwd(raw: Optional[str]) -> str:
    """Normalize a cwd form value to an absolute path.

    Expands a leading ``~`` so paths picked from the autocomplete dropdown
    (which preserves the tilde token for display) round-trip through the
    server. Empty or non-absolute values are rejected with 422 — the
    previous silent ``None`` return made the sidebar look like it had
    saved while actually clearing the field.
    """
    if raw is None or not raw.strip():
        raise HTTPException(422, "cwd is required")
    p = os.path.expanduser(raw.strip())
    if not p.startswith("/"):
        raise HTTPException(422, "cwd must be an absolute path")
    return p


_WORKSPACE_MODES = ("directory", "git_worktree", "scratch", "in_place", "worktree")


def _normalize_workspace_mode(raw: Optional[str]) -> str:
    if raw is None or raw == "":
        return "directory"
    if raw == "in_place":
        return "directory"
    if raw == "worktree":
        return "git_worktree"
    if raw not in _WORKSPACE_MODES:
        raise HTTPException(422, f"workspace_mode must be one of {_WORKSPACE_MODES}")
    return raw


def _optional_abs_path(raw: object) -> Optional[str]:
    if raw is None or not str(raw).strip():
        return None
    p = os.path.expanduser(str(raw).strip())
    if not p.startswith("/"):
        raise HTTPException(422, "path must be absolute")
    return p


def _workspace_payload_from_form(form) -> tuple[str, Optional[str], Optional[str], list[dict]]:
    cwd = _normalize_cwd(form.get("cwd"))
    mode = "git_worktree" if form.get("use_worktree") else "directory"
    worktree_name = (form.get("worktree_name") or "").strip() or None
    worktree_path = _optional_abs_path(form.get("worktree_path"))
    workspaces = [{
        "role": "primary",
        "label": "primary",
        "kind": mode,
        "access": "read_write",
        "source_path": cwd,
        "worktree_name": worktree_name,
        "worktree_path": worktree_path,
        "retention": "preserve",
    }]
    linked_paths = list(form.getlist("linked_workspace_path"))
    linked_kinds = list(form.getlist("linked_workspace_kind"))
    linked_accesses = list(form.getlist("linked_workspace_access"))
    linked_path_overrides = list(form.getlist("linked_workspace_path_override"))
    for idx, raw_path in enumerate(linked_paths):
        linked_path = _optional_abs_path(raw_path)
        if not linked_path:
            continue
        raw_kind = linked_kinds[idx] if idx < len(linked_kinds) else "directory"
        raw_access = linked_accesses[idx] if idx < len(linked_accesses) else "read_only"
        kind = _normalize_workspace_mode(raw_kind)
        if kind == "git_worktree":
            access = "read_write"
            linked_worktree_name = worktree_name
        else:
            access = raw_access or "read_only"
            linked_worktree_name = None
        if access not in ("read_only", "read_write"):
            raise HTTPException(422, "linked workspace access must be read_only or read_write")
        path_override = (
            _optional_abs_path(linked_path_overrides[idx])
            if idx < len(linked_path_overrides) else None
        )
        workspaces.append({
            "role": "linked",
            "label": Path(linked_path).name or f"linked-{idx + 1}",
            "kind": kind,
            "access": access,
            "source_path": linked_path,
            "worktree_name": linked_worktree_name,
            "worktree_path": path_override,
            "retention": "preserve",
        })
    return mode, worktree_name, worktree_path, workspaces

def _safe_preview_name(name: Optional[str]) -> str:
    raw = (name or "").strip().strip("/")
    if not raw:
        return "ticket-worktree"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in raw)
    return safe.strip(".-_") or "ticket-worktree"


def _git_value(cwd: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None



def _is_bare_container(path: Path) -> bool:
    bare = path / ".bare"
    git_file = path / ".git"
    if not bare.is_dir() or not git_file.is_file():
        return False
    try:
        if "gitdir:" not in git_file.read_text():
            return False
    except OSError:
        return False
    return _git_value(bare, "rev-parse", "--is-bare-repository") == "true"


def _preview_worktree_path(*, cwd: str, name: Optional[str],
                           custom_path: Optional[str],
                           worktree_root: Path) -> tuple[Path, str]:
    if custom_path and custom_path.strip():
        return Path(os.path.expanduser(custom_path.strip())), "custom path"
    source = Path(os.path.expanduser(cwd.strip())).resolve()
    wt_name = _safe_preview_name(name)
    if _is_bare_container(source):
        return source / wt_name, "bare-container layout"
    repo_root_raw = _git_value(source, "rev-parse", "--show-toplevel")
    common_raw = _git_value(source, "rev-parse", "--git-common-dir")
    if repo_root_raw and common_raw:
        repo_root = Path(repo_root_raw).resolve()
        common = Path(common_raw)
        if not common.is_absolute():
            common = (source / common).resolve()
        else:
            common = common.resolve()
        if common != repo_root / ".git" and common.parent == repo_root.parent:
            return common.parent / wt_name, "bare-container layout"
        return worktree_root / repo_root.name / wt_name, "Nightdesk worktree root"
    return worktree_root / source.name / wt_name, "Nightdesk worktree root"


def _gather_board(session: Session):
    profiles = list_profiles(session)
    profiles_by_id = {p.id: p for p in profiles}

    # Pull every ticket per real status, then bucket for the visual columns.
    # The "running" column visually contains:
    #   - actually running tickets (status == 'running'), and
    #   - run-now queued tickets (status == 'queued' AND run_now == True),
    #     which are about to be picked up by the next scheduler tick.
    #
    # Surfacing run-now queued tickets in the running column means a
    # drag-from-queued-to-running doesn't visually snap back to queued
    # while the worker is still in the <=5s window between the drop and
    # the actual status transition. The DB stays the source of truth;
    # this is purely a render-time decision.
    raw = {status: list_tickets(session, status=status, limit=500)
           for status, _ in _COLUMNS}

    queued_all = raw["queued"]
    real_queued = [t for t in queued_all if not getattr(t, "run_now", False)]
    run_now_queued = [t for t in queued_all if getattr(t, "run_now", False)]
    raw["queued"] = real_queued
    raw["running"] = run_now_queued + raw["running"]

    review_run_outcomes: dict[str, str] = {}
    for t in raw["review"]:
        if t.current_run_id:
            try:
                run = get_run(session, t.current_run_id)
                if run.exit_status:
                    review_run_outcomes[t.id] = run.exit_status
            except Exception:
                pass

    columns = [
        {"status": status, "label": label, "tickets": raw[status]}
        for status, label in _COLUMNS
    ]

    return {
        "columns": columns,
        "profiles": profiles,
        "profiles_by_id": profiles_by_id,
        "run_outcomes": review_run_outcomes,
    }


def build_router(
    get_session,
    bearer_token: str,
    templates: Jinja2Templates,
    *,
    transcript_root: Path,
    worktree_root: Path,
) -> APIRouter:
    router = APIRouter(tags=["board"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))


    @router.get("/board/worktree-preview", response_class=HTMLResponse, dependencies=[auth])
    async def worktree_preview(
        cwd: str = "",
        name: Optional[str] = None,
        path: Optional[str] = None,
        format: str = "html",
    ):
        if not cwd.strip() and not (path and path.strip()):
            return HTMLResponse(
                '<span class="text-fg-muted">Enable worktree and choose a working dir.</span>'
            )
        try:
            preview_path, source = _preview_worktree_path(
                cwd=cwd or "/",
                name=name,
                custom_path=path,
                worktree_root=worktree_root,
            )
        except Exception:
            return HTMLResponse(
                '<span class="text-warn">Preview unavailable until the path is valid.</span>'
            )
        if format == "json":
            return JSONResponse({"path": str(preview_path), "source": source})
        return HTMLResponse(
            '<div class="mb-0.5 text-[11px] uppercase tracking-wide text-fg-muted">'
            f'Worktree Path Preview ({html.escape(source)})</div>'
            f'<code class="block break-all text-accent">{html.escape(str(preview_path))}</code>'
        )
    @router.get("/", response_class=HTMLResponse, dependencies=[auth])
    async def board(request: Request, session: Session = Depends(get_session)):
        ctx = _gather_board(session)
        return templates.TemplateResponse(
            request,
            "board.html",
            {
                "title": "Board",
                "columns": ctx["columns"],
                "profiles": ctx["profiles"],
                "profiles_by_id": ctx["profiles_by_id"],
                "run_outcomes": ctx["run_outcomes"],
                "mode": "create",
                "ticket": None,
            },
        )

    @router.get("/board/columns", response_class=HTMLResponse, dependencies=[auth])
    async def columns(request: Request, session: Session = Depends(get_session)):
        """Polled fragment returning the four column sections as OOB swaps.

        The board template polls this every few seconds. The response carries
        no main-target body — each column is an ``hx-swap-oob="true"`` block
        that replaces the matching ``#board-col-<status>`` section in place.
        The sidebar is outside those ids and is never touched, so unsaved
        edits in the editor survive every poll cycle.
        """
        ctx = _gather_board(session)
        return templates.TemplateResponse(
            request,
            "partials/board_columns_oob.html",
            {
                "columns": ctx["columns"],
                "profiles_by_id": ctx["profiles_by_id"],
                "run_outcomes": ctx["run_outcomes"],
            },
        )

    @router.get("/board/sidebar", response_class=HTMLResponse, dependencies=[auth])
    async def sidebar(
        request: Request,
        ticket_id: Optional[str] = None,
        session: Session = Depends(get_session),
    ):
        profiles = list_profiles(session)
        ticket = None
        mode = "create"
        if ticket_id:
            try:
                ticket = get_ticket(session, ticket_id)
                mode = "edit"
            except TicketNotFound:
                raise HTTPException(404, "ticket not found")
        return templates.TemplateResponse(
            request,
            "partials/sidebar.html",
            {
                "profiles": profiles,
                "ticket": ticket,
                "mode": mode,
            },
        )

    @router.post("/board/tickets", dependencies=[auth])
    async def create(
        request: Request,
        session: Session = Depends(get_session),
    ):
        form = await request.form()
        title = (form.get("title") or "").strip()
        if not title:
            raise HTTPException(422, "title required")
        profile_id = form.get("profile_id")
        if not profile_id:
            raise HTTPException(422, "profile_id required")
        try:
            workspace_mode, worktree_name, worktree_path, workspaces = _workspace_payload_from_form(form)
            create_ticket(
                session,
                title=title,
                prompt=(form.get("prompt") or ""),
                profile_id=profile_id,
                cwd=_normalize_cwd(form.get("cwd")),
                workspace_mode=workspace_mode,
                worktree_name=worktree_name,
                worktree_path=worktree_path,
                workspaces=workspaces,
                additional_dirs=_parse_additional_dirs(form.getlist("additional_dirs")),
            )
        except InvalidTransition as e:
            raise HTTPException(422, str(e))
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = "/"
        return resp

    @router.post("/board/tickets/{tid}", dependencies=[auth])
    async def update(
        tid: str,
        request: Request,
        session: Session = Depends(get_session),
    ):
        # PATCH semantics: only fields the form actually includes are
        # updated. Missing fields are left alone. The sidebar always
        # sends all fields, so the UI flow is unchanged; the difference
        # matters for hand-rolled curl posts and stale browser tabs that
        # would otherwise wipe (e.g.) the prompt to "" because the field
        # wasn't part of the submission.
        form = await request.form()
        fields: dict = {}
        if "title" in form:
            title = (form.get("title") or "").strip()
            if not title:
                raise HTTPException(422, "title cannot be empty")
            fields["title"] = title
        if "profile_id" in form:
            profile_id = form.get("profile_id")
            if not profile_id:
                raise HTTPException(422, "profile_id cannot be empty")
            fields["profile_id"] = profile_id
        if "prompt" in form:
            fields["prompt"] = form.get("prompt") or ""
        if "cwd" in form:
            fields["cwd"] = _normalize_cwd(form.get("cwd"))
            if form.get("workspace_form") == "1":
                workspace_mode, worktree_name, worktree_path, workspaces = _workspace_payload_from_form(form)
                fields["workspace_mode"] = workspace_mode
                fields["worktree_name"] = worktree_name
                fields["worktree_path"] = worktree_path
                fields["workspaces"] = workspaces
                if "additional_dirs" in form:
                    fields["additional_dirs"] = _parse_additional_dirs(form.getlist("additional_dirs"))
        try:
            update_ticket(session, tid, **fields)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        # Return the sidebar partial re-rendered in edit mode for the same
        # ticket so HTMX swaps just the rail in place. Previously this
        # returned HX-Redirect to "/", which reloaded the board and dropped
        # the user back into create mode — losing their selection.
        ticket = get_ticket(session, tid)
        profiles = list_profiles(session)
        return templates.TemplateResponse(
            request,
            "partials/sidebar.html",
            {"profiles": profiles, "ticket": ticket, "mode": "edit"},
        )

    @router.post("/board/tickets/{tid}/archive", dependencies=[auth])
    async def archive_inline(
        tid: str,
        request: Request,
        session: Session = Depends(get_session),
    ):
        # Sidebar Archive button target. Performs the review->archived
        # transition and re-renders the sidebar in edit mode for the same
        # ticket. The hard requirement matches Run-now: no page navigation,
        # the sidebar updates in place. Once archived, the new Archive
        # button gate (status == 'review') no longer matches so the button
        # disappears; Run-now stays visible because it's enabled for the
        # archived status too.
        try:
            archive(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        ticket = get_ticket(session, tid)
        profiles = list_profiles(session)
        return templates.TemplateResponse(
            request,
            "partials/sidebar.html",
            {"profiles": profiles, "ticket": ticket, "mode": "edit"},
        )

    @router.post("/board/tickets/{tid}/cancel", dependencies=[auth])
    async def cancel_inline(
        tid: str,
        request: Request,
        session: Session = Depends(get_session),
    ):
        # Detail-page Cancel button target. Same anti-pattern fix as Archive:
        # the old button posted a plain <form> to the bearer-only
        # /api/v1/tickets/{tid}/cancel JSON route, which 401s for browser
        # cookie sessions (and otherwise dumps raw TicketOut JSON over the
        # page). This cookie-auth twin performs the running->review transition
        # via transition_status. HTMX clients get 204 + HX-Redirect back to
        # the ticket page so it reloads showing the new review status; the
        # redirect only fires on the success path, so 4xx responses leave the
        # user on the page. Non-HTMX (curl, no-JS) clients keep a 303 fallback.
        try:
            transition_status(session, tid, "review")
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        if request.headers.get("HX-Request") == "true":
            resp = Response(status_code=204)
            resp.headers["HX-Redirect"] = f"/tickets/{tid}"
            return resp
        return RedirectResponse(url=f"/tickets/{tid}", status_code=303)

    @router.delete("/board/tickets/{tid}", dependencies=[auth])
    async def delete(tid: str, session: Session = Depends(get_session)):
        try:
            delete_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = "/"
        return resp

    @router.post("/board/tickets/{tid}/move", dependencies=[auth])
    async def move(
        tid: str,
        request: Request,
        target_status: str = Form(...),
        position: Optional[int] = Form(None),
        session: Session = Depends(get_session),
    ):
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        # Dragging into "running" doesn't actually start the agent: only the
        # worker process can do that (it creates the Run row, prepares the
        # sandbox, etc.). Instead we flag the ticket as run_now=True and
        # park it in queued; the next scheduler tick (<=5s) will pick it up
        # via the run-now bypass and transition it to running for real.
        # Bypassing the worker here would leave the ticket wedged in
        # "running" with no Run row, which is exactly the bug we're fixing.
        if target_status == "running":
            if t.status not in ("draft", "queued"):
                raise HTTPException(
                    409, f"can't run-now a ticket in status {t.status!r}"
                )
            set_run_now(session, tid, True)
            try:
                transition_with_position(session, tid, "queued", position=position)
            except InvalidTransition as e:
                raise HTTPException(409, str(e))
            return Response(status_code=204)
        # Dragging *into* queued cancels any pending run-now intent. Without
        # this, a card that was previously dropped on running (status=queued
        # + run_now=true, visually in the running column per _gather_board)
        # would render right back into running on the next poll because the
        # render bucketing keys off run_now.
        if target_status == "queued" and t.run_now:
            set_run_now(session, tid, False)
        try:
            transition_with_position(session, tid, target_status, position=position)
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return Response(status_code=204)

    @router.post(
        "/board/columns/{status}/reorder",
        dependencies=[auth],
    )
    async def reorder(
        status: str,
        request: Request,
        ticket_ids: str = Form(...),
        session: Session = Depends(get_session),
    ):
        ids = [s for s in (x.strip() for x in ticket_ids.split(",")) if s]
        try:
            reorder_in_column(session, status, ids)
        except InvalidTransition as e:
            raise HTTPException(422, str(e))
        return Response(status_code=204)

    return router
