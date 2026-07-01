from __future__ import annotations
import json

from pathlib import Path
from zoneinfo import ZoneInfo

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from pydantic import ValidationError

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.schemas import ConfigUpdate
from nightdesk.db.models import ConfigRow, Run, ScheduleWindow, Ticket
from nightdesk.domain.projects import (
    ProjectNameTaken,
    ProjectNotFound,
    archive_project,
    create_project,
    get_project,
    list_projects,
    preview_defaults,
    update_project,
)
from nightdesk.domain.toolchains import (
    BUILTIN_TOOLCHAINS,
    assert_paths_not_excluded,
    toolchain_names,
    toolchain_options,
)
from nightdesk.domain.notifications import build_test_payload, fire_webhook
from nightdesk.domain.tickets import (
    archive, requeue, request_run_now, transition_status,
    TicketNotFound, InvalidTransition,
)


def _validate_preset_upsert(
    new_name: str,
    paths: list,
    existing: dict[str, list[str]],
    *,
    original_name: str = "",
) -> dict[str, list[str]]:
    """Validate and return an updated {name: [paths]} custom-presets map.

    Handles rename by removing the old key first. Enforces uniqueness and
    built-in-name collision rules. The returned dict has only custom presets
    (built-ins are not stored in config).
    """
    if not new_name:
        raise HTTPException(422, "preset name is required")
    if new_name in BUILTIN_TOOLCHAINS:
        raise HTTPException(
            422, f"preset name {new_name!r} is reserved by a built-in toolchain"
        )
    if not isinstance(paths, list):
        raise HTTPException(422, "paths must be a list")

    updated = dict(existing)
    # Rename: drop the old key so it doesn't persist.
    if original_name and original_name != new_name:
        updated.pop(original_name, None)
    # Duplicate check — skip when updating in place.
    if new_name in updated and (not original_name or original_name != new_name):
        raise HTTPException(422, f"duplicate preset name {new_name!r}")

    updated[new_name] = paths
    try:
        validated = ConfigUpdate(toolchain_presets=updated)
    except ValidationError as exc:
        raise HTTPException(422, f"invalid toolchain presets: {exc.errors()[0]['msg']}")
    result = validated.toolchain_presets or {}
    for pname, ppaths in result.items():
        try:
            assert_paths_not_excluded(ppaths, field=f"toolchain_presets.{pname}")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    return result


# Active lifecycle statuses surfaced in the project workspace panes, in pill
# order. Excludes archived/cancelled (the workspace is the live work surface).
_PANE_STATUSES = ("inbox", "draft", "queued", "running", "review")
_PANE_STATUS_PILLS = [
    ("", "All"),
    ("inbox", "Inbox"),
    ("draft", "Draft"),
    ("queued", "Queued"),
    ("running", "Running"),
    ("review", "Review"),
]


def _relative_time(dt: datetime | None) -> str:
    """Compact "Nm ago" relative label (UTC-aware), empty string when absent."""
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago" if months < 12 else f"{days // 365}y ago"


def _run_duration(run: Run) -> str:
    """Wall-clock duration of a finished run as a compact string ('' if open)."""
    if run.started_at is None or run.finished_at is None:
        return ""
    start = run.started_at
    end = run.finished_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    secs = int((end - start).total_seconds())
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


def _run_outcome(run: Run) -> str:
    """Display outcome for a run: explicit exit status, else running/none."""
    if run.exit_status:
        return run.exit_status
    return "running" if run.finished_at is None else "none"


def _windows_payload(session: Session) -> list[dict[str, str | int]]:
    """Serialize ScheduleWindow rows for the settings editor / JSON island."""
    rows = session.scalars(
        select(ScheduleWindow).order_by(ScheduleWindow.position.asc(), ScheduleWindow.id.asc())
    ).all()
    return [
        {"label": w.label, "day_mask": w.day_mask, "start": w.start,
         "end": w.end, "max_parallel": w.max_parallel, "position": w.position}
        for w in rows
    ]


def build_router(get_session, bearer_token: str, templates: Jinja2Templates,
                  *, transcript_root: Path) -> APIRouter:
    router = APIRouter(tags=["ui"])
    auth = Depends(require_token_cookie_or_bearer(bearer_token))

    def _ensure_cfg(session: Session) -> ConfigRow:
        cfg = session.get(ConfigRow, 1)
        if cfg is None:
            cfg = ConfigRow(id=1, worktree_root="", transcript_root="")
            session.add(cfg)
            session.flush()
        return cfg

    def _settings_context(session: Session, *, category: str, saved: bool):
        import shutil

        cfg = session.get(ConfigRow, 1)
        pane_template = {
            "scheduling": "partials/settings_scheduling_pane.html",
            "claude": "partials/settings_claude_pane.html",
            "worktrees": "partials/settings_worktrees_pane.html",
            "notifications": "partials/settings_notifications_pane.html",
            "projects": "partials/settings_projects_pane.html",
            "labels": "partials/settings_labels_pane.html",
        }[category]
        return {
            "title": "Settings",
            "active_page": "settings",
            "settings_category": category,
            "pane_template": pane_template,
            "cfg": cfg,
            "saved": saved,
            "path_claude_binary": shutil.which("claude"),
            "windows": _windows_payload(session),
            "schedule_timezone": (cfg.schedule_timezone if cfg else "UTC"),
            "toolchain_names": toolchain_names(cfg),
            "toolchain_options": toolchain_options(cfg),
            "projects": list_projects(session, include_archived=True),
            "project_previews": {
                p.id: preview_defaults(session, p)
                for p in list_projects(session, include_archived=True)
                if not p.archived_at
            },
        }

    def _render_settings(request: Request, session: Session, *, category: str, saved: bool):
        return templates.TemplateResponse(
            request,
            "settings_shell.html",
            _settings_context(session, category=category, saved=saved),
        )

    def _render_toolsets(
        request: Request,
        session: Session,
        *,
        selected_name: str | None = None,
        pane_mode: str | None = None,
        error: str | None = None,
        partial: bool = False,
    ):
        cfg = session.get(ConfigRow, 1)
        all_opts = toolchain_options(cfg)
        builtin_presets = [o for o in all_opts if o["builtin"]]
        custom_presets = [o for o in all_opts if not o["builtin"]]

        if selected_name is None and all_opts:
            selected_name = all_opts[0]["name"]

        preset = next((o for o in all_opts if o["name"] == selected_name), None)

        if pane_mode is None:
            if preset is None:
                pane_mode = "empty"
            elif preset["builtin"]:
                pane_mode = "builtin"
            else:
                pane_mode = "edit"

        ctx = {
            "title": "Toolsets",
            "active_page": "toolsets",
            "builtin_presets": builtin_presets,
            "custom_presets": custom_presets,
            "selected_name": selected_name,
            "pane_mode": pane_mode,
            "preset": preset,
            "error": error,
        }
        tmpl = "partials/toolset_pane.html" if partial else "toolsets.html"
        return templates.TemplateResponse(request, tmpl, ctx)

    def _render_projects(request: Request, session: Session, *, saved: bool):
        ctx = _settings_context(session, category="projects", saved=saved)
        ctx["title"] = "Projects"
        ctx["active_page"] = "projects"
        ctx["project_workspaces"] = _project_workspace_context(session, ctx["projects"])
        return templates.TemplateResponse(request, "projects.html", ctx)

    def _project_workspace_context(session: Session, projects: list) -> dict[str, dict]:
        statuses = ["inbox", "draft", "queued", "running", "review"]
        out: dict[str, dict] = {}
        if not projects:
            return out
        project_ids = [p.id for p in projects]
        tickets = list(session.scalars(
            select(Ticket)
            .where(Ticket.project_id.in_(project_ids))
            .where(Ticket.status.in_(statuses))
            .order_by(Ticket.position.asc(), Ticket.updated_at.desc())
        ))
        for project in projects:
            out[project.id] = {
                "counts": {status: 0 for status in statuses},
                "by_status": {status: [] for status in statuses},
                "recent_work": [],
                "inbox_items": [],
            }
        for ticket in tickets:
            workspace = out.get(ticket.project_id)
            if workspace is None:
                continue
            workspace["counts"][ticket.status] += 1
            workspace["by_status"][ticket.status].append(ticket)
        for project in projects:
            workspace = out[project.id]
            work_tickets = [
                t
                for status in ("draft", "queued", "running", "review")
                for t in workspace["by_status"][status]
            ]
            workspace["recent_work"] = sorted(
                work_tickets,
                key=lambda t: t.updated_at or t.created_at,
                reverse=True,
            )[:5]
            workspace["inbox_items"] = sorted(
                workspace["by_status"]["inbox"],
                key=lambda t: t.updated_at or t.created_at,
                reverse=True,
            )[:5]
            for status in statuses:
                workspace["by_status"][status] = workspace["by_status"][status][:5]
        return out

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

    @router.get("/analytics", response_class=HTMLResponse, dependencies=[auth])
    async def analytics_page(request: Request, session: Session = Depends(get_session)):
        from datetime import datetime, timezone

        from starlette.concurrency import run_in_threadpool

        from nightdesk.domain import analytics, pricing

        now = datetime.now(timezone.utc)
        # Resolve the analytics timezone the same way the scheduler does
        # (src/nightdesk/worker/scheduler.py): cfg.schedule_timezone, defaulting
        # to UTC when unset/invalid so day boundaries stay local-midnight-to-now
        # for the configured zone. ``now`` stays an aware UTC instant; only the
        # boundary localization changes.
        cfg = session.get(ConfigRow, 1)
        tz_name = cfg.schedule_timezone if cfg and cfg.schedule_timezone else "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        # Resolve live → cached → bundled prices off the event loop: a slow
        # upstream is bounded by the fetch timeout and never blocks the page.
        price_info = await run_in_threadpool(
            pricing.resolve_prices,
            getattr(request.app.state, "data_dir", None),
            url=getattr(request.app.state, "pricing_url", None),
            now=now,
        )
        data = analytics.build_dashboard(session, now=now, price_info=price_info, tz=tz)
        return templates.TemplateResponse(
            request, "analytics.html",
            {"title": "Analytics", "active_page": "analytics", **data},
        )

    @router.get("/settings", response_class=HTMLResponse, dependencies=[auth])
    async def settings_root():
        return RedirectResponse(url="/settings/scheduling", status_code=303)

    @router.get("/settings/scheduling", response_class=HTMLResponse, dependencies=[auth])
    async def settings_scheduling_page(request: Request, session: Session = Depends(get_session)):
        return _render_settings(request, session, category="scheduling", saved=False)

    @router.post("/settings/scheduling", response_class=HTMLResponse, dependencies=[auth])
    async def settings_scheduling_save(
        request: Request,
        session: Session = Depends(get_session),
        polling_interval_seconds: int = Form(...),
        windows_json: str = Form("[]"),
        schedule_timezone: str = Form("UTC"),
    ):
        import json as _json

        cfg = _ensure_cfg(session)
        # Clamp to sane ranges so a fat-finger doesn't wedge the worker.
        cfg.polling_interval_seconds = max(1, min(int(polling_interval_seconds), 300))

        # Validate the timezone; fall back to UTC on anything unrecognized.
        try:
            ZoneInfo(schedule_timezone)
            cfg.schedule_timezone = schedule_timezone
        except Exception:
            cfg.schedule_timezone = "UTC"

        # Replace all schedule windows from the editor's JSON. Times are stored
        # as wall-clock HH:MM in the configured timezone (no UTC conversion).
        try:
            rows = _json.loads(windows_json or "[]")
        except _json.JSONDecodeError:
            raise HTTPException(422, "windows_json must be valid JSON")
        if not isinstance(rows, list):
            raise HTTPException(422, "windows_json must be a JSON list")
        for existing in session.scalars(select(ScheduleWindow)).all():
            session.delete(existing)
        session.flush()
        for i, w in enumerate(rows):
            session.add(ScheduleWindow(
                label=str(w.get("label", "")),
                day_mask=int(w.get("day_mask", 127)),
                start=str(w.get("start", "00:00")),
                end=str(w.get("end", "00:00")),
                max_parallel=max(1, min(int(w.get("max_parallel", 1)), 16)),
                position=i,
            ))
        session.commit()
        return _render_settings(request, session, category="scheduling", saved=True)

    @router.get("/settings/claude", response_class=HTMLResponse, dependencies=[auth])
    async def settings_claude_page(request: Request, session: Session = Depends(get_session)):
        return _render_settings(request, session, category="claude", saved=False)

    @router.post("/settings/claude", response_class=HTMLResponse, dependencies=[auth])
    async def settings_claude_save(
        request: Request,
        session: Session = Depends(get_session),
        claude_binary_path: str = Form(""),
        cc_minimum_version: str = Form(""),
    ):
        cfg = _ensure_cfg(session)
        cfg.claude_binary_path = (claude_binary_path or "").strip() or None
        cfg.cc_minimum_version = (cc_minimum_version or "").strip() or cfg.cc_minimum_version
        session.commit()
        return _render_settings(request, session, category="claude", saved=True)

    @router.get("/settings/worktrees", response_class=HTMLResponse, dependencies=[auth])
    async def settings_worktrees_page(request: Request, session: Session = Depends(get_session)):
        return _render_settings(request, session, category="worktrees", saved=False)

    @router.post("/settings/worktrees", response_class=HTMLResponse, dependencies=[auth])
    async def settings_worktrees_save(
        request: Request,
        session: Session = Depends(get_session),
        worktree_base_ref: str = Form(""),
    ):
        cfg = _ensure_cfg(session)
        cfg.worktree_base_ref = (worktree_base_ref or "").strip() or None
        session.commit()
        return _render_settings(request, session, category="worktrees", saved=True)

    @router.get("/toolsets", response_class=HTMLResponse, dependencies=[auth])
    async def toolsets_page(
        request: Request,
        session: Session = Depends(get_session),
        selected: str | None = Query(default=None),
    ):
        return _render_toolsets(request, session, selected_name=selected)

    @router.get("/toolsets/new", response_class=HTMLResponse, dependencies=[auth])
    async def toolsets_new(request: Request, session: Session = Depends(get_session)):
        partial = request.headers.get("HX-Request") == "true"
        return _render_toolsets(request, session, selected_name=None,
                                pane_mode="new", partial=partial)

    @router.get("/toolsets/pane/{name}", response_class=HTMLResponse, dependencies=[auth])
    async def toolsets_pane(
        name: str,
        request: Request,
        session: Session = Depends(get_session),
    ):
        cfg = session.get(ConfigRow, 1)
        opts = toolchain_options(cfg)
        preset = next((o for o in opts if o["name"] == name), None)
        if preset is None:
            raise HTTPException(404, f"toolset {name!r} not found")
        partial = request.headers.get("HX-Request") == "true"
        mode = "builtin" if preset["builtin"] else "edit"
        return _render_toolsets(request, session, selected_name=name,
                                pane_mode=mode, partial=partial)

    @router.post("/toolsets/preset", dependencies=[auth])
    async def toolsets_preset_upsert(
        request: Request,
        session: Session = Depends(get_session),
    ):
        form = await request.form()
        original_name = str(form.get("original_name", "") or "").strip()
        new_name = str(form.get("preset_name", "") or "").strip()
        paths_raw = str(form.get("preset_paths_json", "[]") or "[]")

        try:
            paths = json.loads(paths_raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(422, f"invalid paths JSON: {exc.msg}")

        cfg = _ensure_cfg(session)
        existing: dict[str, list[str]] = dict(cfg.toolchain_presets or {})
        cfg.toolchain_presets = _validate_preset_upsert(
            new_name, paths, existing, original_name=original_name
        )
        session.commit()
        return RedirectResponse(url=f"/toolsets/pane/{new_name}", status_code=303)

    @router.post("/toolsets/preset/{name}/delete", dependencies=[auth])
    async def toolsets_preset_delete(
        name: str,
        session: Session = Depends(get_session),
    ):
        if name in BUILTIN_TOOLCHAINS:
            raise HTTPException(422, f"cannot delete built-in toolchain {name!r}")
        cfg = _ensure_cfg(session)
        existing = dict(cfg.toolchain_presets or {})
        if name not in existing:
            raise HTTPException(404, f"toolset preset {name!r} not found")
        existing.pop(name)
        cfg.toolchain_presets = existing
        session.commit()
        return RedirectResponse(url="/toolsets", status_code=303)

    @router.get("/settings/external-tools", response_class=HTMLResponse, dependencies=[auth])
    async def settings_external_tools_page(request: Request, session: Session = Depends(get_session)):
        return RedirectResponse(url="/toolsets", status_code=303)

    @router.get("/projects", response_class=HTMLResponse, dependencies=[auth])
    async def projects_page(request: Request, session: Session = Depends(get_session)):
        return _render_projects(request, session, saved=False)

    @router.get("/settings/projects", response_class=HTMLResponse, dependencies=[auth])
    async def settings_projects_page(request: Request, session: Session = Depends(get_session)):
        return RedirectResponse(url="/projects", status_code=303)


    def _project_linked_workspaces(form) -> list[dict]:
        linked_workspaces = []
        linked_paths = list(form.getlist("linked_workspace_path"))
        linked_kinds = list(form.getlist("linked_workspace_kind"))
        linked_accesses = list(form.getlist("linked_workspace_access"))
        for idx, raw_path in enumerate(linked_paths):
            path = (raw_path or "").strip()
            if not path:
                continue
            kind = linked_kinds[idx] if idx < len(linked_kinds) else "directory"
            access = linked_accesses[idx] if idx < len(linked_accesses) else "read_only"
            linked_workspaces.append({
                "role": "linked",
                "label": Path(path).name or f"linked-{idx + 1}",
                "kind": kind,
                "access": access,
                "source_path": path,
            })
        return linked_workspaces


    def _form_list(form, key: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in form.getlist(key):
            item = str(raw or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    @router.post("/projects", response_class=HTMLResponse, dependencies=[auth])
    async def projects_save(
        request: Request,
        session: Session = Depends(get_session),
        name: str = Form(...),
        source_path: str = Form(...),
        default_workspace_mode: str = Form("directory"),
        default_worktree_name_template: str = Form(""),
        default_base_ref: str = Form(""),
    ):
        form = await request.form()
        linked_workspaces = _project_linked_workspaces(form)
        if default_workspace_mode != "git_worktree":
            default_worktree_name_template = ""
            default_base_ref = ""
        try:
            create_project(
                session,
                name=name,
                source_path=source_path,
                default_workspace_mode=default_workspace_mode or None,
                default_worktree_name_template=default_worktree_name_template or None,
                default_base_ref=default_base_ref or None,
                default_linked_workspaces=linked_workspaces or None,
                default_toolchains=_form_list(form, "default_toolchain"),
                default_tool_paths=_form_list(form, "default_tool_path"),
            )
        except ProjectNameTaken:
            raise HTTPException(409, "project name or slug already exists")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return _render_projects(request, session, saved=True)

    @router.post("/settings/projects", response_class=HTMLResponse, dependencies=[auth])
    async def settings_projects_save(
        request: Request,
        session: Session = Depends(get_session),
        name: str = Form(...),
        source_path: str = Form(...),
        default_workspace_mode: str = Form("directory"),
        default_worktree_name_template: str = Form(""),
        default_base_ref: str = Form(""),
    ):
        await projects_save(
            request,
            session,
            name,
            source_path,
            default_workspace_mode,
            default_worktree_name_template,
            default_base_ref,
        )
        return RedirectResponse(url="/projects", status_code=303)

    @router.post("/projects/{project_id}", response_class=HTMLResponse, dependencies=[auth])
    async def projects_update(
        request: Request,
        project_id: str,
        session: Session = Depends(get_session),
        name: str = Form(...),
        source_path: str = Form(...),
        default_workspace_mode: str = Form("directory"),
        default_worktree_name_template: str = Form(""),
        default_base_ref: str = Form(""),
    ):
        form = await request.form()
        linked_workspaces = _project_linked_workspaces(form)
        if default_workspace_mode != "git_worktree":
            default_worktree_name_template = ""
            default_base_ref = ""
        try:
            update_project(
                session,
                project_id,
                name=name,
                source_path=source_path,
                default_workspace_mode=default_workspace_mode or None,
                default_worktree_name_template=default_worktree_name_template or None,
                default_base_ref=default_base_ref or None,
                default_linked_workspaces=linked_workspaces or None,
                default_toolchains=_form_list(form, "default_toolchain"),
                default_tool_paths=_form_list(form, "default_tool_path"),
            )
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        except ProjectNameTaken:
            raise HTTPException(409, "project name or slug already exists")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return _render_projects(request, session, saved=True)

    @router.post("/settings/projects/{project_id}", response_class=HTMLResponse, dependencies=[auth])
    async def settings_projects_update(
        request: Request,
        project_id: str,
        session: Session = Depends(get_session),
        name: str = Form(...),
        source_path: str = Form(...),
        default_workspace_mode: str = Form("directory"),
        default_worktree_name_template: str = Form(""),
        default_base_ref: str = Form(""),
    ):
        await projects_update(
            request,
            project_id,
            session,
            name,
            source_path,
            default_workspace_mode,
            default_worktree_name_template,
            default_base_ref,
        )
        return RedirectResponse(url="/projects", status_code=303)


    @router.post("/projects/{project_id}/archive", response_class=HTMLResponse, dependencies=[auth])
    async def projects_archive(
        request: Request,
        project_id: str,
        session: Session = Depends(get_session),
    ):
        try:
            archive_project(session, project_id)
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        return _render_projects(request, session, saved=True)

    @router.post("/settings/projects/{project_id}/archive", response_class=HTMLResponse, dependencies=[auth])
    async def settings_projects_archive(
        request: Request,
        project_id: str,
        session: Session = Depends(get_session),
    ):
        await projects_archive(request, project_id, session)
        return RedirectResponse(url="/projects", status_code=303)


    @router.get("/projects/{project_id}/tickets-pane", response_class=HTMLResponse, dependencies=[auth])
    async def project_tickets_pane(
        request: Request,
        project_id: str,
        status: str = Query(default=""),
        session: Session = Depends(get_session),
    ):
        """Read-focused ticket rows for a project's Tickets tab.

        Filtered to the active lifecycle statuses (or one of them via ?status=)
        and ordered most-recently-updated first. Rows link to the ticket detail
        page; no inline editing, drag, or bulk select here by design.
        """
        try:
            project = get_project(session, project_id)
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        active = status.strip().lower()
        if active and active not in _PANE_STATUSES:
            active = ""
        stmt = (
            select(Ticket)
            .where(Ticket.project_id == project_id)
            .where(Ticket.status == active if active else Ticket.status.in_(_PANE_STATUSES))
            .order_by(Ticket.updated_at.desc())
        )
        tickets = list(session.scalars(stmt))
        # Last-run outcome per ticket, batched so a long list never fans out.
        run_ids = [t.current_run_id for t in tickets if t.current_run_id]
        runs_by_id = {
            r.id: r
            for r in (session.scalars(select(Run).where(Run.id.in_(run_ids))) if run_ids else [])
        }
        run_outcomes = {
            t.id: _run_outcome(runs_by_id[t.current_run_id])
            for t in tickets
            if t.current_run_id and t.current_run_id in runs_by_id
        }
        return templates.TemplateResponse(
            request,
            "partials/project_tickets_pane.html",
            {
                "project": project,
                "tickets": tickets,
                "active_status": active,
                "status_pills": _PANE_STATUS_PILLS,
                "run_outcomes": run_outcomes,
            },
        )

    @router.get("/projects/{project_id}/activity-pane", response_class=HTMLResponse, dependencies=[auth])
    async def project_activity_pane(
        request: Request,
        project_id: str,
        session: Session = Depends(get_session),
    ):
        """Chronological feed of the project's ~30 most recent agent runs."""
        try:
            project = get_project(session, project_id)
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
            rows.append({
                "run": run,
                "ticket": ticket,
                "outcome": _run_outcome(run),
                "duration": _run_duration(run),
                "tokens": f"{tokens:,}" if tokens else "",
                "rel": _relative_time(run.started_at),
            })
        return templates.TemplateResponse(
            request,
            "partials/project_activity_pane.html",
            {"project": project, "rows": rows},
        )

    @router.get("/settings/notifications", response_class=HTMLResponse, dependencies=[auth])
    async def settings_notifications_page(request: Request, session: Session = Depends(get_session)):
        return _render_settings(request, session, category="notifications", saved=False)

    @router.post("/settings/notifications", response_class=HTMLResponse, dependencies=[auth])
    async def settings_notifications_save(
        request: Request,
        session: Session = Depends(get_session),
        notify_webhook_url: str = Form(""),
    ):
        cfg = _ensure_cfg(session)
        cfg.notify_webhook_url = (notify_webhook_url or "").strip() or None
        session.commit()
        return _render_settings(request, session, category="notifications", saved=True)

    @router.post("/settings/notifications/test-webhook", dependencies=[auth])
    async def test_webhook(
        request: Request,
        url: str = Form(""),
    ):
        """Fire a synthetic test payload to the given webhook URL."""
        import re as _re
        target = (url or "").strip()
        if not target or not _re.match(r"^https?://", target):
            raise HTTPException(422, "provide a valid http(s) webhook URL")
        base_url = str(request.base_url).rstrip("/")
        payload = build_test_payload(base_url)
        fire_webhook(target, payload)
        return Response(status_code=204)

    @router.post("/settings/test-webhook", dependencies=[auth])
    async def test_webhook_legacy(
        request: Request,
        url: str = Form(""),
    ):
        return await test_webhook(request, url)

    # --- Labels settings -----------------------------------------------------

    @router.get("/settings/labels", response_class=HTMLResponse, dependencies=[auth])
    async def settings_labels_page(request: Request, session: Session = Depends(get_session)):
        from nightdesk.domain.labels import list_labels
        ctx = _settings_context(session, category="labels", saved=False)
        ctx["labels"] = list_labels(session)
        return templates.TemplateResponse(request, "settings_shell.html", ctx)

    @router.post("/settings/labels", response_class=HTMLResponse, dependencies=[auth])
    async def settings_labels_create(
        request: Request,
        session: Session = Depends(get_session),
        name: str = Form(""),
        color: str = Form(""),
    ):
        from nightdesk.domain.labels import create_label, list_labels, LabelNameTaken
        name_clean = (name or "").strip()
        if not name_clean:
            raise HTTPException(422, "label name is required")
        try:
            create_label(session, name=name_clean, color=(color or "").strip())
        except LabelNameTaken:
            raise HTTPException(409, f"label name already taken: {name_clean}")
        ctx = _settings_context(session, category="labels", saved=True)
        ctx["labels"] = list_labels(session)
        return templates.TemplateResponse(request, "settings_shell.html", ctx)

    @router.post("/settings/labels/{label_id}", response_class=HTMLResponse, dependencies=[auth])
    async def settings_labels_update(
        request: Request,
        label_id: str,
        session: Session = Depends(get_session),
        name: str = Form(""),
        color: str = Form(""),
    ):
        from nightdesk.domain.labels import (
            update_label, list_labels, LabelNotFound, LabelNameTaken,
        )
        name_clean = (name or "").strip()
        if not name_clean:
            raise HTTPException(422, "label name is required")
        try:
            update_label(
                session, label_id,
                name=name_clean,
                color=(color or "").strip(),
            )
        except LabelNotFound:
            raise HTTPException(404, "label not found")
        except LabelNameTaken as e:
            raise HTTPException(409, f"label name already taken: {e}")
        except ValueError as e:
            raise HTTPException(422, str(e))
        ctx = _settings_context(session, category="labels", saved=True)
        ctx["labels"] = list_labels(session)
        return templates.TemplateResponse(request, "settings_shell.html", ctx)

    @router.post("/settings/labels/{label_id}/delete", response_class=HTMLResponse, dependencies=[auth])
    async def settings_labels_delete(
        request: Request,
        label_id: str,
        session: Session = Depends(get_session),
    ):
        from nightdesk.domain.labels import delete_label, list_labels, LabelNotFound
        try:
            delete_label(session, label_id)
        except LabelNotFound:
            raise HTTPException(404, "label not found")
        ctx = _settings_context(session, category="labels", saved=True)
        ctx["labels"] = list_labels(session)
        return templates.TemplateResponse(request, "settings_shell.html", ctx)

    return router
