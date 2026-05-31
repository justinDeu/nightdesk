from __future__ import annotations

import os
import html
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.db.models import ConfigRow
from nightdesk.domain.projects import ProjectNotFound, get_project_by_slug, list_projects
from nightdesk.domain.profiles import list_profiles
from nightdesk.domain.runs import get_run, list_runs
from nightdesk.domain.tickets import (
    CyclicDependency,
    DependencyNotFound,
    InvalidTransition,
    TicketNotFound,
    add_dependency,
    archive,
    create_ticket,
    delete_ticket,
    get_ticket,
    list_dependencies,
    list_dependents,
    list_tickets,
    remove_dependency,
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


def _normalize_source_path(raw: Optional[str]) -> str:
    """Normalize a source path form value to an absolute path."""
    if raw is None or not raw.strip():
        raise HTTPException(422, "source_path is required")
    p = os.path.expanduser(raw.strip())
    if not p.startswith("/"):
        raise HTTPException(422, "source_path must be an absolute path")
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
    source_path = _normalize_source_path(form.get("source_path") or form.get("primary_source_path"))
    mode = "git_worktree" if form.get("use_worktree") else "directory"
    worktree_name = (form.get("worktree_name") or "").strip() or None
    worktree_path = _optional_abs_path(form.get("worktree_path"))
    base_ref = (form.get("base_ref") or "").strip() or None
    workspaces = [{
        "role": "primary",
        "label": "primary",
        "kind": mode,
        "access": "read_write",
        "source_path": source_path,
        "worktree_name": worktree_name,
        "worktree_path": worktree_path,
        "base_ref": base_ref,
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


def _split_lines(raw: object) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in str(raw or "").splitlines():
        item = line.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _toolchain_overrides_from_form(form) -> Optional[dict]:
    overrides = {
        "enable": _split_lines(form.get("toolchain_enable")),
        "disable": _split_lines(form.get("toolchain_disable")),
        "extra_paths": _split_lines(form.get("toolchain_extra_paths")),
    }
    return overrides if any(overrides.values()) else None

def _safe_preview_name(name: Optional[str]) -> str:
    raw = (name or "").strip().strip("/")
    if not raw:
        return "ticket-worktree"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in raw)
    return safe.strip(".-_") or "ticket-worktree"


def _git_value(source_dir: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _base_ref_status(source_path: str, base_ref: Optional[str]) -> Optional[str]:
    """Return whether ``base_ref`` resolves to a commit in the repo at ``source_path``.

    - ``None``  -> nothing to check (no base_ref, or source_path not a usable git dir).
    - ``"ok"``  -> the ref resolves to a commit; the worktree branch can start there.
    - ``"missing"`` -> the ref does not resolve. The "branch is gone" case the
      UI must warn about: ``git worktree add ... <base_ref>`` would fail at run
      time, leaving the ticket stuck. Surfacing it at edit time is the whole
      point of this check.
    """
    ref = (base_ref or "").strip()
    if not ref:
        return None
    try:
        source = Path(os.path.expanduser(source_path.strip())).resolve()
    except Exception:
        return None
    # Confirm this is actually a git working area before judging the ref.
    if _git_value(source, "rev-parse", "--git-dir") is None:
        return None
    resolved = _git_value(source, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return "ok" if resolved else "missing"


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


def _preview_worktree_path(*, source_path: str, name: Optional[str],
                           custom_path: Optional[str],
                           worktree_root: Path) -> tuple[Path, str]:
    if custom_path and custom_path.strip():
        return Path(os.path.expanduser(custom_path.strip())), "custom path"
    source = Path(os.path.expanduser(source_path.strip())).resolve()
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


def _project_filter(session: Session, project: str) -> tuple[Optional[str], object | None]:
    if project == "none":
        return "null", None
    if not project:
        return None, None
    try:
        selected = get_project_by_slug(session, project)
    except ProjectNotFound:
        return "__missing__", None
    return selected.id, selected


def _gather_board(session: Session, *, project: str = ""):
    project_id, selected_project = _project_filter(session, project)
    projects = list_projects(session)
    projects_by_id = {p.id: p for p in projects}
    profiles = list_profiles(session)
    profiles_by_id = {p.id: p for p in profiles}

    # Pull every ticket per real status, then bucket for the visual columns.
    raw = {status: list_tickets(session, status=status, project_id=project_id, limit=500)
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

    # Build a map of ticket_id -> list of upstream titles for blocked indicators.
    dep_titles: dict[str, list[str]] = {}
    all_tickets = []
    for status in raw:
        all_tickets.extend(raw[status])
    for t in all_tickets:
        if t.dependencies:
            titles = []
            for dep in t.dependencies:
                upstream = dep.depends_on
                if upstream:
                    titles.append(upstream.title)
            if titles:
                dep_titles[t.id] = titles

    columns = [
        {"status": status, "label": label, "tickets": raw[status]}
        for status, label in _COLUMNS
    ]

    return {
        "columns": columns,
        "profiles": profiles,
        "profiles_by_id": profiles_by_id,
        "run_outcomes": review_run_outcomes,
        "dep_titles": dep_titles,
        "projects": projects,
        "projects_by_id": projects_by_id,
        "selected_project": selected_project,
        "project_filter": project,
        "project_id_filter": project_id,
        # Every ticket, for the modal's "Depends on" picker (all statuses).
        "dep_all": list_tickets(session, project_id=project_id, limit=500),
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
        source_path: str = "",
        name: Optional[str] = None,
        path: Optional[str] = None,
        base_ref: Optional[str] = None,
        format: str = "html",
    ):
        if not source_path.strip() and not (path and path.strip()):
            return HTMLResponse(
                '<span class="text-fg-muted">Enable worktree and choose a source path.</span>'
            )
        try:
            preview_path, source = _preview_worktree_path(
                source_path=source_path or "/",
                name=name,
                custom_path=path,
                worktree_root=worktree_root,
            )
        except Exception:
            return HTMLResponse(
                '<span class="text-warn">Preview unavailable until the path is valid.</span>'
            )
        ref = (base_ref or "").strip()
        ref_status = _base_ref_status(source_path, ref) if source_path.strip() else None
        if format == "json":
            payload: dict = {"path": str(preview_path), "source": source}
            # Only attach base-ref fields when a ref was supplied so callers
            # that don't use the feature keep their stable two-key response.
            if ref:
                payload["base_ref"] = ref
                payload["base_ref_status"] = ref_status
            return JSONResponse(payload)
        ref_line = ""
        if ref:
            if ref_status == "missing":
                ref_line = (
                    '<div class="mt-1 text-[11px] text-warn">'
                    f'Base ref "{html.escape(ref)}" not found in this repo — '
                    'the worktree will fail to create. Check the branch/ref name.'
                    '</div>'
                )
            else:
                ref_line = (
                    '<div class="mt-1 text-[11px] text-fg-muted">'
                    f'Branch will start from <span class="text-accent">{html.escape(ref)}</span>.'
                    '</div>'
                )
        return HTMLResponse(
            '<div class="mb-0.5 text-[11px] uppercase tracking-wide text-fg-muted">'
            f'Worktree Path Preview ({html.escape(source)})</div>'
            f'<code class="block break-all text-accent">{html.escape(str(preview_path))}</code>'
            f'{ref_line}'
        )
    @router.get("/", response_class=HTMLResponse, dependencies=[auth])
    async def board(
        request: Request,
        project: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        ctx = _gather_board(session, project=project)
        return templates.TemplateResponse(
            request,
            "board.html",
            {
                "title": "Board",
                "columns": ctx["columns"],
                "profiles": ctx["profiles"],
                "profiles_by_id": ctx["profiles_by_id"],
                "run_outcomes": ctx["run_outcomes"],
                "dep_titles": ctx["dep_titles"],
                "dep_all": ctx["dep_all"],
                "projects": ctx["projects"],
                "projects_by_id": ctx["projects_by_id"],
                "selected_project": ctx["selected_project"],
                "project_filter": ctx["project_filter"],
                "project_id_filter": ctx["project_id_filter"],
                "mode": "create",
                "ticket": None,
            },
        )

    @router.get("/board/columns", response_class=HTMLResponse, dependencies=[auth])
    async def columns(
        request: Request,
        project: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        """Polled fragment returning the four column sections as OOB swaps.

        The board template polls this every few seconds. The response carries
        no main-target body — each column is an ``hx-swap-oob="true"`` block
        that replaces the matching ``#board-col-<status>`` section in place.
        The sidebar is outside those ids and is never touched, so unsaved
        edits in the editor survive every poll cycle.
        """
        ctx = _gather_board(session, project=project)
        return templates.TemplateResponse(
            request,
            "partials/board_columns_oob.html",
            {
                "columns": ctx["columns"],
                "profiles_by_id": ctx["profiles_by_id"],
                "projects_by_id": ctx["projects_by_id"],
                "run_outcomes": ctx["run_outcomes"],
                "dep_titles": ctx["dep_titles"],
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
        runs: list = []
        deps_upstreams: list = []
        deps_downstreams: list = []
        if ticket_id:
            try:
                ticket = get_ticket(session, ticket_id)
                mode = "edit"
            except TicketNotFound:
                raise HTTPException(404, "ticket not found")
            runs = list_runs(session, ticket_id=ticket_id)
            deps_upstreams = list_dependencies(session, ticket_id)
            deps_downstreams = list_dependents(session, ticket_id)
        return templates.TemplateResponse(
            request,
            "partials/sidebar.html",
            {
                "profiles": profiles,
                "ticket": ticket,
                "mode": mode,
                "runs": runs,
                "deps_upstreams": deps_upstreams,
                "deps_downstreams": deps_downstreams,
                "dep_all": list_tickets(session, limit=500),
                "projects": list_projects(session),
            },
        )

    @router.get("/board/new-ticket-modal", response_class=HTMLResponse, dependencies=[auth])
    async def new_ticket_modal(
        request: Request,
        project: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        """The create-ticket modal as a standalone partial so any page can
        lazy-load and open it. The board includes it inline; other pages fetch
        it into a host container on demand (see ndOpenCreateTicket)."""
        return templates.TemplateResponse(
            request,
            "partials/ticket_edit_modal.html",
            {
                "profiles": list_profiles(session),
                "ticket": None,
                "modal_id": "ticket-create-modal",
                "dep_all": list_tickets(session, limit=500),
                "projects": list_projects(session),
                "selected_project": _project_filter(session, project)[1],
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
        project_id = (form.get("project_id") or "").strip() or None
        try:
            workspace_mode, worktree_name, worktree_path, workspaces = _workspace_payload_from_form(form)
            # Global default: a git-worktree ticket created without an explicit
            # base_ref inherits the configured worktree_base_ref so all new
            # branches start from the same ref unless the user overrides it.
            if workspace_mode == "git_worktree":
                primary = workspaces[0]
                if not primary.get("base_ref"):
                    cfg = session.get(ConfigRow, 1)
                    default_ref = (getattr(cfg, "worktree_base_ref", None) or "").strip() if cfg else ""
                    if default_ref:
                        primary["base_ref"] = default_ref
            new_ticket = create_ticket(
                session,
                title=title,
                prompt=(form.get("prompt") or ""),
                profile_id=profile_id,
                project_id=project_id,
                workspaces=workspaces,
                additional_dirs=_parse_additional_dirs(form.getlist("additional_dirs")),
                toolchain_overrides=_toolchain_overrides_from_form(form),
            )
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        except (InvalidTransition, ValueError) as e:
            raise HTTPException(422, str(e))
        # Optional dependencies from the modal's "Depends on" picker. A brand
        # new ticket can't form a cycle, but guard anyway and skip bad ids.
        if form.get("deps_form") == "1":
            for dep_id in form.getlist("depends_on_id"):
                dep_id = (dep_id or "").strip()
                if not dep_id or dep_id == new_ticket.id:
                    continue
                try:
                    add_dependency(session, new_ticket.id, dep_id)
                except (CyclicDependency, TicketNotFound):
                    pass
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
        if "project_id" in form:
            fields["project_id"] = (form.get("project_id") or "").strip() or None
        if "source_path" in form or "primary_source_path" in form:
            if form.get("workspace_form") == "1":
                workspace_mode, worktree_name, worktree_path, workspaces = _workspace_payload_from_form(form)
                fields["workspaces"] = workspaces
                if "additional_dirs" in form:
                    fields["additional_dirs"] = _parse_additional_dirs(form.getlist("additional_dirs"))
            else:
                fields["source_path"] = _normalize_source_path(form.get("source_path"))
        if "toolchain_form" in form:
            fields["toolchain_overrides"] = _toolchain_overrides_from_form(form)
        try:
            update_ticket(session, tid, **fields)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        except ValueError as e:
            raise HTTPException(422, str(e))
        # Reconcile dependencies from the modal's "Depends on" picker. Only
        # touched when the deps section was part of the submission (the modal
        # sends deps_form=1); an empty selection then means "remove all".
        if form.get("deps_form") == "1":
            desired = {d.strip() for d in form.getlist("depends_on_id")
                       if d.strip() and d.strip() != tid}
            current = {d.id for d in list_dependencies(session, tid)}
            for dep_id in desired - current:
                try:
                    add_dependency(session, tid, dep_id)
                except (CyclicDependency, TicketNotFound):
                    pass
            for dep_id in current - desired:
                try:
                    remove_dependency(session, tid, dep_id)
                except DependencyNotFound:
                    pass
        # Return the sidebar partial re-rendered in edit mode for the same
        # ticket so HTMX swaps just the rail in place. Previously this
        # returned HX-Redirect to "/", which reloaded the board and dropped
        # the user back into create mode — losing their selection.
        ticket = get_ticket(session, tid)
        profiles = list_profiles(session)
        return templates.TemplateResponse(
            request,
            "partials/sidebar.html",
            {"profiles": profiles, "ticket": ticket, "mode": "edit",
             "runs": list_runs(session, ticket_id=tid),
             "deps_upstreams": list_dependencies(session, tid),
             "deps_downstreams": list_dependents(session, tid),
             "dep_all": list_tickets(session, limit=500),
             "projects": list_projects(session)},
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
            {"profiles": profiles, "ticket": ticket, "mode": "edit",
             "runs": list_runs(session, ticket_id=tid),
             "deps_upstreams": list_dependencies(session, tid),
             "deps_downstreams": list_dependents(session, tid),
             "dep_all": list_tickets(session, limit=500),
             "projects": list_projects(session)},
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

    # --- Dependency management (cookie-auth) ---------------------------------

    @router.get("/board/tickets/{tid}/dependencies", response_class=HTMLResponse,
                dependencies=[auth])
    async def deps_fragment(tid: str, request: Request,
                             session: Session = Depends(get_session)):
        """Render the dependency section as an HTMX-swappable fragment."""
        try:
            ticket = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        upstreams = list_dependencies(session, tid)
        downstreams = list_dependents(session, tid)
        return templates.TemplateResponse(
            request,
            "partials/dependency_section.html",
            {
                "ticket": ticket,
                "upstreams": upstreams,
                "downstreams": downstreams,
            },
        )

    @router.post("/board/tickets/{tid}/dependencies", dependencies=[auth])
    async def add_dep(
        tid: str, request: Request,
        depends_on_id: str = Form(...),
        session: Session = Depends(get_session),
    ):
        try:
            add_dependency(session, tid, depends_on_id)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        except CyclicDependency as e:
            raise HTTPException(422, str(e))
        ticket = get_ticket(session, tid)
        upstreams = list_dependencies(session, tid)
        downstreams = list_dependents(session, tid)
        return templates.TemplateResponse(
            request,
            "partials/dependency_section.html",
            {
                "ticket": ticket,
                "upstreams": upstreams,
                "downstreams": downstreams,
            },
        )

    @router.post("/board/tickets/{tid}/dependencies/{dep_on_id}/remove",
                 dependencies=[auth])
    async def remove_dep(
        tid: str, dep_on_id: str, request: Request,
        session: Session = Depends(get_session),
    ):
        try:
            remove_dependency(session, tid, dep_on_id)
        except DependencyNotFound:
            raise HTTPException(404, "dependency not found")
        ticket = get_ticket(session, tid)
        upstreams = list_dependencies(session, tid)
        downstreams = list_dependents(session, tid)
        return templates.TemplateResponse(
            request,
            "partials/dependency_section.html",
            {
                "ticket": ticket,
                "upstreams": upstreams,
                "downstreams": downstreams,
            },
        )

    @router.get("/board/ticket-search", dependencies=[auth])
    async def ticket_search(
        q: str = Query(default=""),
        exclude: str = Query(default=""),
        limit: int = Query(default=10, ge=1, le=50),
        session: Session = Depends(get_session),
    ):
        """Search tickets for the dependency picker.

        Returns a JSON list of ``{id, title, status}`` objects. Excludes the
        ticket identified by ``exclude`` (so a ticket can't depend on itself).
        """
        from sqlalchemy import or_
        from nightdesk.db.models import Ticket as TicketModel
        stmt = select(TicketModel).order_by(
            TicketModel.created_at.desc()
        ).limit(limit)
        if q:
            stmt = stmt.where(
                or_(
                    TicketModel.title.ilike(f"%{q}%"),
                    TicketModel.id.ilike(f"%{q}%"),
                )
            )
        if exclude:
            stmt = stmt.where(TicketModel.id != exclude)
        results = session.scalars(stmt)
        return [
            {"id": t.id, "title": t.title, "status": t.status}
            for t in results
        ]

    return router
