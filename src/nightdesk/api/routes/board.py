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
from nightdesk.domain.query import Cmp, parse_query, search_runs, search_tickets
from nightdesk.domain.runs import get_run, list_runs
from nightdesk.domain.toolchains import current_config, toolchain_options
from nightdesk.domain.priority import (
    resolve_priority, PRIORITY_MIN, PRIORITY_MAX, PRIORITY_SCALE,
)
from nightdesk.domain.properties import (
    PROPERTY_REGISTRY,
    get_property,
    property_chip,
)
from nightdesk.domain.tickets import (
    CyclicDependency,
    DependencyNotFound,
    InvalidTransition,
    TicketNotFound,
    add_dependency,
    archive,
    bulk_archive, bulk_unarchive,
    bulk_update_priority, bulk_update_profile, bulk_update_project, bulk_update_status,
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
    update_ticket_priority, update_ticket_profile, update_ticket_project,
)
from nightdesk.domain.profiles import ProfileNotFound
from nightdesk.domain.labels import (
    LabelNotFound, bulk_add_labels, get_ticket_labels,
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


def _form_strs(form, key: str) -> list[str]:
    """Order-preserving, de-duped, stripped values for a repeated form field.

    The toolchain enable/disable pickers submit one checkbox value per chosen
    name and the extra-paths picker submits one input per row, so the modal
    posts these as repeated fields rather than newline-delimited textareas.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in form.getlist(key):
        item = str(raw or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _toolchain_overrides_from_form(form) -> Optional[dict]:
    overrides = {
        "enable": _form_strs(form, "toolchain_enable"),
        "disable": _form_strs(form, "toolchain_disable"),
        "extra_paths": _form_strs(form, "toolchain_extra_paths"),
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


def _sole_project(session: Session, ast) -> object | None:
    """If the query is exactly ``project=<slug>``, return that project.

    Lets the board's create modal keep prefilling workspace defaults from the
    project the user filtered to. Any richer query yields None.
    """
    if not isinstance(ast, Cmp) or ast.field != "project" or ast.op not in ("=", ":"):
        return None
    value = ast.value.strip()
    if "," in value or value in ("none", "null", ""):
        return None
    try:
        return get_project_by_slug(session, value)
    except ProjectNotFound:
        return None


def _toolchain_options(session: Session) -> list[dict]:
    """Available toolchain presets (built-ins + configured) for the ticket modal
    picker, so enable/disable are chosen from a discoverable list rather than
    typed as free text."""
    return toolchain_options(current_config(session))


def _list_labels_sidebar(session: Session) -> list:
    """Fetch all labels for template contexts (sidebar, modals)."""
    from nightdesk.domain.labels import list_labels as _ll
    return _ll(session)


def _ticket_exists(session: Session, ticket_id: str) -> bool:
    """True if a ticket with this id exists (no raise). Used to capture prior
    values for bulk undo without aborting on already-deleted ids."""
    try:
        get_ticket(session, ticket_id)
        return True
    except TicketNotFound:
        return False


def _restore_undo(label: str, prop: str, prior: dict) -> Optional[dict]:
    """Build the undo descriptor for a reversible bulk metadata change. The
    client re-POSTs ``payload`` to ``/board/tickets/bulk/restore`` (JSON) to
    revert each ticket to its captured prior value. Returns ``None`` when there
    is nothing to undo."""
    if not prior:
        return None
    return {
        "label": label,
        "route": "/board/tickets/bulk/restore",
        "json": True,
        "payload": {"prop": prop, "values": prior},
    }


def _gather_board(session: Session, *, q: str = ""):
    ast = parse_query(q)
    selected_project = _sole_project(session, ast)
    projects = list_projects(session)
    projects_by_id = {p.id: p for p in projects}
    profiles = list_profiles(session)
    profiles_by_id = {p.id: p for p in profiles}

    # Pull every ticket per real status (filtered by the query), then bucket for
    # the visual columns. A ``status=`` term in the query simply empties the
    # columns it excludes, so the kanban layout always survives.
    raw = {status: search_tickets(session, ast, status=status, limit=500)
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

    # All labels for the label picker on ticket cards.
    from nightdesk.domain.labels import list_labels as _list_labels
    all_labels = _list_labels(session)

    return {
        "columns": columns,
        "profiles": profiles,
        "profiles_by_id": profiles_by_id,
        "run_outcomes": review_run_outcomes,
        "dep_titles": dep_titles,
        "projects": projects,
        "projects_by_id": projects_by_id,
        "selected_project": selected_project,
        "query": q,
        # Every ticket, for the modal's "Depends on" picker (all statuses). When
        # the query is a single project, scope the picker to it (as the old
        # project filter did); richer queries leave the picker unscoped.
        "dep_all": list_tickets(
            session,
            project_id=selected_project.id if selected_project else None,
            limit=500,
        ),
        "toolchain_options": _toolchain_options(session),
        "all_labels": all_labels,
    }


def _runs_rows(session: Session, q: str) -> list[dict]:
    """Assemble runs-view rows (run + ticket + project + profile) for a query."""
    runs = search_runs(session, parse_query((q or "").strip()), limit=300)
    projects = {p.id: p for p in list_projects(session)}
    profiles = {p.id: p for p in list_profiles(session)}
    rows = []
    for r in runs:
        t = r.ticket
        rows.append({
            "run": r,
            "ticket": t,
            "project": projects.get(t.project_id) if t and t.project_id else None,
            "profile": profiles.get(t.profile_id) if t else None,
        })
    return rows


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
    def _board_query(q: str, project: str) -> str:
        """Resolve the active query, honouring legacy ``?project=`` bookmarks."""
        q = (q or "").strip()
        if not q and project:
            return f"project={project}"
        return q

    @router.get("/", response_class=HTMLResponse, dependencies=[auth])
    async def board(
        request: Request,
        q: str = Query(default=""),
        project: str = Query(default=""),
        view: str = Query(default="tickets"),
        session: Session = Depends(get_session),
    ):
        query = _board_query(q, project)
        view = "runs" if view == "runs" else "tickets"
        ctx = _gather_board(session, q=query)
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
                "toolchain_options": ctx["toolchain_options"],
                "all_labels": ctx["all_labels"],
                # Bulk action bar option sources.
                "priority_scale": PRIORITY_SCALE,
                "bulk_statuses": ["draft", "queued", "review", "archived"],
                "query": ctx["query"],
                "view": view,
                # Only assembled when the runs view is the initial render.
                "runs_rows": _runs_rows(session, query) if view == "runs" else [],
                "mode": "create",
                "ticket": None,
            },
        )

    @router.get("/board/columns", response_class=HTMLResponse, dependencies=[auth])
    async def columns(
        request: Request,
        q: str = Query(default=""),
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
        ctx = _gather_board(session, q=_board_query(q, project))
        return templates.TemplateResponse(
            request,
            "partials/board_columns_oob.html",
            {
                "columns": ctx["columns"],
                "profiles_by_id": ctx["profiles_by_id"],
                "projects_by_id": ctx["projects_by_id"],
                "run_outcomes": ctx["run_outcomes"],
                "dep_titles": ctx["dep_titles"],
                "all_labels": ctx["all_labels"],
            },
        )

    @router.get("/board/runs", response_class=HTMLResponse, dependencies=[auth])
    async def board_runs(
        request: Request,
        q: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        """Runs view for the board's Tickets/Runs toggle.

        Returns a flat, query-filtered runs table that swaps in where the
        kanban grid normally sits. Same search bar, run-resource fields.
        """
        return templates.TemplateResponse(
            request,
            "partials/runs_table.html",
            {"rows": _runs_rows(session, q), "query": (q or "").strip()},
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
                "toolchain_options": _toolchain_options(session),
                "all_labels": _list_labels_sidebar(session),
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
                "toolchain_options": _toolchain_options(session),
                "all_labels": _list_labels_sidebar(session),
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
                priority=int(form.get("priority") or 0),
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
        # Optional labels from the label picker.
        if form.get("labels_form") == "1":
            from nightdesk.domain.labels import set_ticket_labels as _sl
            label_ids = [v for v in form.getlist("label_ids") if v.strip()]
            if label_ids:
                try:
                    _sl(session, new_ticket.id, label_ids)
                except Exception:
                    pass
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = "/"
        return resp

    # --- Bulk metadata update (cookie-auth / HTMX) ----------------------------
    # Registered BEFORE /board/tickets/{tid} routes so FastAPI does not
    # match "bulk" as a tid parameter.

    @router.post("/board/tickets/bulk/priority", dependencies=[auth])
    async def bulk_priority_inline(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Cookie-auth bulk priority update.  Accepts ``ticket_ids`` (comma-
        separated) and ``priority`` as form fields.  Returns JSON with updated
        and skipped lists."""
        form = await request.form()
        raw_ids = (form.get("ticket_ids") or "")
        ticket_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
        if not ticket_ids:
            raise HTTPException(422, "ticket_ids required")
        try:
            priority = int(form.get("priority", 0))
        except (TypeError, ValueError):
            raise HTTPException(422, "priority must be an integer")
        if priority < 0:
            raise HTTPException(422, "priority must be >= 0")
        # Capture prior priorities so the action bar can offer an Undo.
        prior = {tid: get_ticket(session, tid).priority
                 for tid in ticket_ids if _ticket_exists(session, tid)}
        updated, skipped = bulk_update_priority(session, ticket_ids, priority)
        return JSONResponse({
            "updated": [{"id": t.id, "priority": t.priority} for t in updated],
            "skipped": skipped,
            "undo": _restore_undo("priority", "priority", prior),
        })

    @router.post("/board/tickets/bulk/status", dependencies=[auth])
    async def bulk_status_inline(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Cookie-auth bulk status transition.  Each ticket is transitioned
        independently; invalid transitions are skipped."""
        form = await request.form()
        raw_ids = (form.get("ticket_ids") or "")
        ticket_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
        if not ticket_ids:
            raise HTTPException(422, "ticket_ids required")
        new_status = (form.get("status") or "").strip()
        if not new_status:
            raise HTTPException(422, "status required")
        prior = {tid: get_ticket(session, tid).status
                 for tid in ticket_ids if _ticket_exists(session, tid)}
        try:
            updated, skipped = bulk_update_status(session, ticket_ids, new_status)
        except InvalidTransition as e:
            raise HTTPException(422, str(e))
        # Only offer undo for tickets that actually moved; restoring a status is
        # best-effort because the lifecycle state machine may reject the reverse
        # transition (that's the "where feasible" caveat).
        moved = {t.id: prior[t.id] for t in updated if t.id in prior}
        return JSONResponse({
            "updated": [{"id": t.id, "status": t.status} for t in updated],
            "skipped": skipped,
            "undo": _restore_undo("status", "status", moved),
        })

    @router.post("/board/tickets/bulk/project", dependencies=[auth])
    async def bulk_project_inline(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Cookie-auth bulk project assignment."""
        form = await request.form()
        raw_ids = (form.get("ticket_ids") or "")
        ticket_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
        if not ticket_ids:
            raise HTTPException(422, "ticket_ids required")
        project_id = (form.get("project_id") or "").strip() or None
        prior = {tid: (get_ticket(session, tid).project_id or "")
                 for tid in ticket_ids if _ticket_exists(session, tid)}
        try:
            updated, skipped = bulk_update_project(session, ticket_ids, project_id)
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        return JSONResponse({
            "updated": [{"id": t.id, "project_id": t.project_id} for t in updated],
            "skipped": skipped,
            "undo": _restore_undo("project", "project", prior),
        })

    @router.post("/board/tickets/bulk/profile", dependencies=[auth])
    async def bulk_profile_inline(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Cookie-auth bulk profile reassignment."""
        form = await request.form()
        raw_ids = (form.get("ticket_ids") or "")
        ticket_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
        if not ticket_ids:
            raise HTTPException(422, "ticket_ids required")
        profile_id = (form.get("profile_id") or "").strip()
        if not profile_id:
            raise HTTPException(422, "profile_id required")
        prior = {tid: get_ticket(session, tid).profile_id
                 for tid in ticket_ids if _ticket_exists(session, tid)}
        try:
            updated, skipped = bulk_update_profile(session, ticket_ids, profile_id)
        except ProfileNotFound:
            raise HTTPException(404, "profile not found")
        return JSONResponse({
            "updated": [{"id": t.id, "profile_id": t.profile_id} for t in updated],
            "skipped": skipped,
            "undo": _restore_undo("profile", "profile", prior),
        })

    @router.post("/board/tickets/bulk/labels", dependencies=[auth])
    async def bulk_labels_inline(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Cookie-auth bulk label add.  Adds the given ``label_ids`` (comma-
        separated) to every selected ticket (set union — existing labels are
        kept).  Returns JSON with updated/skipped lists and an undo payload
        that restores each ticket's prior label set."""
        form = await request.form()
        raw_ids = (form.get("ticket_ids") or "")
        ticket_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
        if not ticket_ids:
            raise HTTPException(422, "ticket_ids required")
        raw_labels = (form.get("label_ids") or "")
        label_ids = [s.strip() for s in raw_labels.split(",") if s.strip()]
        if not label_ids:
            raise HTTPException(422, "label_ids required")
        # Capture prior label sets for undo before mutating.
        prior = {
            tid: [lbl.id for lbl in get_ticket_labels(session, tid)]
            for tid in ticket_ids if _ticket_exists(session, tid)
        }
        try:
            updated, skipped = bulk_add_labels(session, ticket_ids, label_ids)
        except LabelNotFound as e:
            raise HTTPException(404, str(e))
        return JSONResponse({
            "updated": [
                {"id": t.id, "label_ids": [lbl.id for lbl in t.labels]}
                for t in updated
            ],
            "skipped": skipped,
            "undo": _restore_undo("labels", "labels", prior),
        })

    @router.post("/board/tickets/bulk/archive", dependencies=[auth])
    async def bulk_archive_inline(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Cookie-auth bulk archive.  Archives every selected ticket that is in
        ``review`` (the only valid source state); others are skipped.  The undo
        payload unarchives exactly the tickets that were archived."""
        form = await request.form()
        raw_ids = (form.get("ticket_ids") or "")
        ticket_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
        if not ticket_ids:
            raise HTTPException(422, "ticket_ids required")
        updated, skipped = bulk_archive(session, ticket_ids)
        archived_ids = [t.id for t in updated]
        undo = None
        if archived_ids:
            undo = {
                "label": "archive",
                "route": "/board/tickets/bulk/unarchive",
                "json": False,
                "payload": {"ticket_ids": ",".join(archived_ids)},
            }
        return JSONResponse({
            "updated": [{"id": t.id, "status": t.status} for t in updated],
            "skipped": skipped,
            "undo": undo,
        })

    @router.post("/board/tickets/bulk/unarchive", dependencies=[auth])
    async def bulk_unarchive_inline(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Cookie-auth bulk unarchive.  Returns each archived ticket to
        ``queued``.  Used both as a standalone action and as the undo path for
        bulk archive."""
        form = await request.form()
        raw_ids = (form.get("ticket_ids") or "")
        ticket_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
        if not ticket_ids:
            raise HTTPException(422, "ticket_ids required")
        updated, skipped = bulk_unarchive(session, ticket_ids)
        return JSONResponse({
            "updated": [{"id": t.id, "status": t.status} for t in updated],
            "skipped": skipped,
        })

    @router.post("/board/tickets/bulk/restore", dependencies=[auth])
    async def bulk_restore_inline(
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Generic undo endpoint: restore a property to per-ticket prior values.

        Accepts a JSON body ``{"prop": <name>, "values": {tid: value}}`` where
        the value type depends on the property (int for priority, id-or-empty
        for project/profile, status string, or a list of label ids). Each ticket
        is restored independently; failures (missing ticket, invalid status
        transition) are skipped so a partial undo still applies what it can."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "JSON body required")
        prop = (body.get("prop") or "").strip()
        values = body.get("values") or {}
        if prop not in ("priority", "project", "profile", "status", "labels"):
            raise HTTPException(422, f"cannot restore property {prop!r}")
        if not isinstance(values, dict):
            raise HTTPException(422, "values must be an object")
        restored: list[str] = []
        skipped: list[dict] = []
        for tid, val in values.items():
            try:
                if prop == "priority":
                    update_ticket_priority(session, tid, int(val))
                elif prop == "project":
                    update_ticket_project(session, tid, (val or None))
                elif prop == "profile":
                    update_ticket_profile(session, tid, val)
                elif prop == "status":
                    transition_status(session, tid, val)
                elif prop == "labels":
                    from nightdesk.domain.labels import set_ticket_labels
                    set_ticket_labels(session, tid, list(val or []))
                restored.append(tid)
            except TicketNotFound:
                skipped.append({"ticket_id": tid, "reason": "not found"})
            except (InvalidTransition, ProjectNotFound, ProfileNotFound,
                    LabelNotFound, ValueError) as exc:
                skipped.append({"ticket_id": tid, "reason": str(exc)})
        return JSONResponse({"restored": restored, "skipped": skipped})

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
        if "priority" in form:
            fields["priority"] = int(form.get("priority") or 0)
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
        # Reconcile labels from the label picker. Only touched when the
        # labels section was part of the submission (labels_form=1).
        if form.get("labels_form") == "1":
            from nightdesk.domain.labels import set_ticket_labels as _sl
            label_ids = [v for v in form.getlist("label_ids") if v.strip()]
            try:
                _sl(session, tid, label_ids)
            except Exception:
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
             "projects": list_projects(session),
             "toolchain_options": _toolchain_options(session),
             "all_labels": _list_labels_sidebar(session)},
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
             "projects": list_projects(session),
             "toolchain_options": _toolchain_options(session),
             "all_labels": _list_labels_sidebar(session)},
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

    # --- Shared property picker (priority / status / project / …) ------------
    #
    # ONE menu route + ONE commit route back the reusable property-picker
    # primitive for every editable ticket metadata field. The per-property
    # behaviour lives in domain.properties; nothing here is property-specific
    # beyond looking the descriptor up by name. This is the "focused metadata
    # update route" the picker calls instead of submitting the full ticket
    # modal.

    def _render_chip(request: Request, prop: str, ticket) -> HTMLResponse:
        """Render the single property chip partial for an HTMX swap."""
        return templates.TemplateResponse(
            request,
            "partials/property_chip.html",
            {"tid": ticket.id, "prop": prop, "chip": property_chip(prop, ticket)},
        )

    @router.get(
        "/board/tickets/{tid}/picker/{prop}",
        response_class=HTMLResponse,
        dependencies=[auth],
    )
    async def property_picker_menu(
        tid: str,
        prop: str,
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Lazy-loaded popover body for a property picker.

        Returns the option list (searchable for properties that opt in) the
        picker JS injects under the chip. Options are computed fresh per open
        so e.g. the project list and the valid status transitions are always
        current.
        """
        try:
            descriptor = get_property(prop)
        except KeyError:
            raise HTTPException(404, f"unknown property {prop!r}")
        try:
            ticket = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        options = [o.as_dict() for o in descriptor.options(session, ticket)]
        return templates.TemplateResponse(
            request,
            "partials/property_picker_menu.html",
            {
                "tid": tid,
                "prop": prop,
                "label": descriptor.label,
                "searchable": descriptor.searchable,
                "options": options,
            },
        )

    async def _read_property_value(request: Request, *keys: str) -> str:
        """Pull the submitted value from a form field or JSON body.

        Accepts the canonical ``value`` key (picker) plus any property-specific
        aliases (e.g. the legacy ``priority`` field) so HTMX forms and JSON API
        clients share one parser.
        """
        content_type = request.headers.get("content-type") or ""
        if "application/json" in content_type:
            body = await request.json()
            for k in keys:
                if k in body:
                    return str(body.get(k, ""))
            return ""
        form = await request.form()
        for k in keys:
            if k in form:
                return str(form.get(k, ""))
        return ""

    @router.post("/board/tickets/{tid}/property/{prop}", dependencies=[auth])
    async def update_property(
        tid: str,
        prop: str,
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Focused metadata update for the shared property picker.

        HTMX callers get the re-rendered chip to swap in place; JSON API
        callers get ``{property, value, label}``.
        """
        try:
            descriptor = get_property(prop)
        except KeyError:
            raise HTTPException(404, f"unknown property {prop!r}")
        raw = (await _read_property_value(request, "value", prop)).strip()
        try:
            ticket = descriptor.apply(session, tid, raw)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

        if request.headers.get("HX-Request") == "true":
            return _render_chip(request, prop, ticket)
        chip = property_chip(prop, ticket)
        return JSONResponse(
            {"property": prop, "value": raw, "label": chip["label"]}
        )

    @router.post("/board/tickets/{tid}/priority", dependencies=[auth])
    async def update_priority(
        tid: str,
        request: Request,
        session: Session = Depends(get_session),
    ):
        """Back-compatible priority update.

        Delegates to the shared property registry so there is exactly one
        priority code path. Kept as a stable alias for the JSON API and any
        existing clients posting a ``priority`` field; the response shape
        (``{priority, label}``) is preserved.
        """
        raw = (await _read_property_value(request, "priority", "value")).strip()
        # The focused priority route is a write surface, so it rejects negative
        # numeric priorities (the named scale starts at 0). The picker route
        # only ever submits named levels, so this guard is harmless there.
        if raw.lstrip("-").isdigit() and int(raw) < 0:
            raise HTTPException(422, f"priority must be >= 0: {raw!r}")
        try:
            ticket = get_property("priority").apply(session, tid, raw)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ValueError:
            raise HTTPException(422, f"invalid priority value: {raw!r}")

        if request.headers.get("HX-Request") == "true":
            return _render_chip(request, "priority", ticket)
        from nightdesk.domain.priority import priority_label
        return JSONResponse(
            {"priority": ticket.priority, "label": priority_label(ticket.priority)}
        )

    # --- Focused metadata updates (cookie-auth / HTMX) -----------------------
    #
    # Per-property HTMX aliases that mirror the JSON API focused-update routes.
    # They delegate to the SAME domain functions the shared property registry
    # uses (transition_status / update_ticket_project / update_ticket_profile),
    # so there is exactly one write path per property — the picker, keyboard
    # shortcuts, and these focused routes can never diverge. Kept as stable
    # aliases for the metadata-update-routes contract and its callers; the
    # picker route ``/property/{prop}`` remains the primary UI surface.

    @router.post("/board/tickets/{tid}/status", dependencies=[auth])
    async def update_status_inline(
        tid: str,
        request: Request,
        new_status: str = Form(...),
        session: Session = Depends(get_session),
    ):
        """Cookie-auth status transition for inline status changes and keyboard
        shortcuts.  Respects the lifecycle state machine."""
        try:
            t = transition_status(session, tid, new_status)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return JSONResponse({"id": t.id, "status": t.status})

    @router.post("/board/tickets/{tid}/project", dependencies=[auth])
    async def update_project_inline(
        tid: str,
        request: Request,
        project_id: str = Form(""),
        session: Session = Depends(get_session),
    ):
        """Cookie-auth project assignment for the property picker.  Empty
        string clears the project."""
        pid = project_id.strip() or None
        try:
            t = update_ticket_project(session, tid, pid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        return JSONResponse({"id": t.id, "project_id": t.project_id})

    @router.post("/board/tickets/{tid}/profile", dependencies=[auth])
    async def update_profile_inline(
        tid: str,
        request: Request,
        profile_id: str = Form(...),
        session: Session = Depends(get_session),
    ):
        """Cookie-auth profile reassignment for the property picker."""
        try:
            t = update_ticket_profile(session, tid, profile_id)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProfileNotFound:
            raise HTTPException(404, "profile not found")
        return JSONResponse({"id": t.id, "profile_id": t.profile_id})

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

    @router.put("/board/tickets/{tid}/labels", dependencies=[auth])
    async def set_ticket_labels_hx(
        request: Request,
        tid: str,
        session: Session = Depends(get_session),
    ):
        """HTMX: replace a ticket's labels.  Body is ``label_ids`` (repeated)."""
        from nightdesk.domain.labels import set_ticket_labels as _set_labels
        from nightdesk.domain.labels import LabelNotFound as _LNF
        from nightdesk.db.models import Label
        form = await request.form()
        label_ids = [v for v in form.getlist("label_ids") if v.strip()]
        try:
            ticket = _set_labels(session, tid, label_ids)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        except _LNF as e:
            raise HTTPException(404, str(e))
        all_labels = list(session.scalars(select(Label).order_by(Label.name.asc())))
        return templates.TemplateResponse(
            request,
            "partials/label_picker.html",
            {
                "ticket": ticket,
                "all_labels": all_labels,
                "picker_name": "label_ids",
                "picker_target": f"label-picker-{tid}",
            },
        )

    @router.get("/board/labels-search", dependencies=[auth])
    async def labels_search(
        q: str = Query(default=""),
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
    ):
        """Search labels for the label picker.  Returns JSON."""
        from nightdesk.db.models import Label as LabelModel
        stmt = select(LabelModel).order_by(LabelModel.name.asc()).limit(limit)
        if q:
            stmt = stmt.where(LabelModel.name.ilike(f"%{q}%"))
        results = session.scalars(stmt)
        return [
            {"id": l.id, "name": l.name, "color": l.color}
            for l in results
        ]

    return router
