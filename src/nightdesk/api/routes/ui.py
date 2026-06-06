from __future__ import annotations
import json

from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from pydantic import ValidationError

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.schemas import ConfigUpdate
from nightdesk.db.models import ConfigRow, ScheduleWindow
from nightdesk.domain.projects import (
    ProjectNameTaken,
    ProjectNotFound,
    archive_project,
    create_project,
    list_projects,
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


def _parse_toolchain_presets(form) -> dict[str, list[str]]:
    """Reconstruct {preset_name: [paths]} from the worktrees-pane form.

    Path inputs aren't submitted directly — each preset card carries a hidden
    ``preset_paths_json`` field that JS keeps in sync with the visible rows.
    Card order is preserved by DOM submission order, so the two getlist() calls
    pair up positionally. Validation goes through the ConfigUpdate validator so
    the form path and the JSON API path enforce the same rules.
    """
    names = list(form.getlist("preset_name"))
    paths_jsons = list(form.getlist("preset_paths_json"))
    raw: dict[str, list[str]] = {}
    for raw_name, raw_paths in zip(names, paths_jsons):
        name = str(raw_name or "").strip()
        if not name:
            continue
        # A custom preset name must not shadow a built-in toolchain (which would
        # silently override its paths) or collide with another custom card.
        if name in BUILTIN_TOOLCHAINS:
            raise HTTPException(
                422, f"preset name {name!r} is reserved by a built-in toolchain"
            )
        if name in raw:
            raise HTTPException(422, f"duplicate preset name {name!r}")
        try:
            paths = json.loads(raw_paths or "[]")
        except json.JSONDecodeError as exc:
            raise HTTPException(422, f"invalid preset {name!r} paths JSON: {exc.msg}")
        if not isinstance(paths, list):
            raise HTTPException(422, f"preset {name!r} paths must be a list")
        raw[name] = paths
    if not raw:
        return {}
    try:
        validated = ConfigUpdate(toolchain_presets=raw)
    except ValidationError as exc:
        raise HTTPException(422, f"invalid toolchain presets: {exc.errors()[0]['msg']}")
    presets = validated.toolchain_presets or {}
    # Reject absolute paths that overlap protected nightdesk directories, the
    # same guard the ticket/project path inputs use.
    for name, paths in presets.items():
        try:
            assert_paths_not_excluded(paths, field=f"toolchain_presets.{name}")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    return presets


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
            "external_tools": "partials/settings_external_tools_pane.html",
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
        }

    def _render_settings(request: Request, session: Session, *, category: str, saved: bool):
        return templates.TemplateResponse(
            request,
            "settings_shell.html",
            _settings_context(session, category=category, saved=saved),
        )

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

        from nightdesk.domain import analytics

        data = analytics.build_dashboard(session, now=datetime.now(timezone.utc))
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

    @router.get("/settings/external-tools", response_class=HTMLResponse, dependencies=[auth])
    async def settings_external_tools_page(request: Request, session: Session = Depends(get_session)):
        return _render_settings(request, session, category="external_tools", saved=False)

    @router.post("/settings/external-tools", response_class=HTMLResponse, dependencies=[auth])
    async def settings_external_tools_save(
        request: Request,
        session: Session = Depends(get_session),
    ):
        form = await request.form()
        cfg = _ensure_cfg(session)
        cfg.toolchain_presets = _parse_toolchain_presets(form)
        session.commit()
        return _render_settings(request, session, category="external_tools", saved=True)

    @router.get("/settings/projects", response_class=HTMLResponse, dependencies=[auth])
    async def settings_projects_page(request: Request, session: Session = Depends(get_session)):
        return _render_settings(request, session, category="projects", saved=False)


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
        return _render_settings(request, session, category="projects", saved=True)

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
        return _render_settings(request, session, category="projects", saved=True)


    @router.post("/settings/projects/{project_id}/archive", response_class=HTMLResponse, dependencies=[auth])
    async def settings_projects_archive(
        request: Request,
        project_id: str,
        session: Session = Depends(get_session),
    ):
        try:
            archive_project(session, project_id)
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        return _render_settings(request, session, category="projects", saved=True)


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
