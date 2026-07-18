from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from nightdesk.db.models import Project, Run, Ticket, TicketEvent
from nightdesk.domain.toolchains import (
    assert_known_toolchains,
    assert_paths_not_excluded,
    clean_string_list,
    current_config,
)


class ProjectNotFound(Exception):
    pass


class ProjectNameTaken(Exception):
    pass


_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_VALID_WORKSPACE_MODES = {"in_place", "directory", "git_worktree", "worktree"}
_PROJECT_COLORS = (
    "#34d399",
    "#60a5fa",
    "#f59e0b",
    "#f472b6",
    "#a78bfa",
    "#22d3ee",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(value: str) -> str:
    slug = _SLUG_CHARS.sub("-", value.strip().lower()).strip("-")
    return slug or "project"


def _default_color(slug: str) -> str:
    total = sum(ord(ch) for ch in slug)
    return _PROJECT_COLORS[total % len(_PROJECT_COLORS)]


def normalize_source_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source_path is required")
    path = os.path.expanduser(value.strip())
    if not path.startswith("/"):
        raise ValueError("source_path must be absolute (start with '/')")
    return path.rstrip("/") or "/"


def create_project(session: Session, **fields: Any) -> Project:
    name = fields.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    fields["name"] = name.strip()
    fields["slug"] = slugify(str(fields.get("slug") or fields["name"]))
    if not fields.get("color"):
        fields["color"] = _default_color(fields["slug"])
    fields["source_path"] = normalize_source_path(fields.get("source_path"))
    _validate_workspace_mode(fields.get("default_workspace_mode"))
    fields["default_toolchains"] = clean_string_list(
        fields.get("default_toolchains"), field="default_toolchains",
    )
    assert_known_toolchains(fields["default_toolchains"], config=current_config(session))
    fields["default_tool_paths"] = clean_string_list(
        fields.get("default_tool_paths"), field="default_tool_paths",
    )
    assert_paths_not_excluded(fields["default_tool_paths"], field="default_tool_paths")
    project = Project(**fields)
    session.add(project)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ProjectNameTaken(fields["name"]) from exc
    session.refresh(project)
    return project


def get_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(project_id)
    return project


def get_project_by_slug(session: Session, slug: str) -> Project:
    project = session.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise ProjectNotFound(slug)
    return project


def list_projects(session: Session, *, include_archived: bool = False) -> list[Project]:
    stmt = select(Project).order_by(Project.position.asc(), Project.name.asc())
    if not include_archived:
        stmt = stmt.where(Project.archived_at.is_(None))
    return list(session.scalars(stmt))


def update_project(session: Session, project_id: str, **fields: Any) -> Project:
    project = get_project(session, project_id)
    if "name" in fields:
        name = fields["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required")
        fields["name"] = name.strip()
    if "slug" in fields and fields["slug"] is not None:
        fields["slug"] = slugify(str(fields["slug"]))
    if "source_path" in fields:
        fields["source_path"] = normalize_source_path(fields["source_path"])
    if "default_workspace_mode" in fields:
        _validate_workspace_mode(fields["default_workspace_mode"])
    if "default_toolchains" in fields:
        fields["default_toolchains"] = clean_string_list(
            fields["default_toolchains"], field="default_toolchains",
        )
        assert_known_toolchains(fields["default_toolchains"], config=current_config(session))
    if "default_tool_paths" in fields:
        fields["default_tool_paths"] = clean_string_list(
            fields["default_tool_paths"], field="default_tool_paths",
        )
        assert_paths_not_excluded(fields["default_tool_paths"], field="default_tool_paths")
    for key, value in fields.items():
        setattr(project, key, value)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ProjectNameTaken(fields.get("name") or project.name) from exc
    session.refresh(project)
    return project


def archive_project(session: Session, project_id: str) -> Project:
    project = get_project(session, project_id)
    project.archived_at = _now()
    session.commit()
    session.refresh(project)
    return project


def apply_project_defaults(session: Session, fields: dict[str, Any]) -> dict[str, Any]:
    project_id = fields.get("project_id")
    if project_id is None:
        return fields
    project = get_project(session, project_id)
    out = dict(fields)
    if "workspaces" not in out or out["workspaces"] is None:
        out["workspaces"] = _default_workspaces(project, out)
    if project.default_workspace_mode and not out.get("workspace_mode"):
        out["workspace_mode"] = project.default_workspace_mode
    # Apply the project's default toolchains when the ticket itself does not
    # specify any toolchain overrides. Stored as an ``enable`` override so the
    # default is captured on the ticket (and visible in the editor) rather than
    # only being resolved implicitly at run time. ``create_ticket`` still runs
    # ``assert_known_toolchains`` over the merged list, so a stale/unknown
    # default surfaces as an error instead of being silently dropped.
    default_toolchains = [
        name for name in (project.default_toolchains or [])
        if isinstance(name, str) and name.strip()
    ]
    if default_toolchains and not out.get("toolchain_overrides"):
        out["toolchain_overrides"] = {
            "enable": list(default_toolchains),
            "disable": [],
            "extra_paths": [],
        }
    return out


def preview_defaults(session: Session, project: Project) -> dict[str, Any]:
    """Compute the effective creation defaults preview for a project.

    Uses the effective-config resolver (``apply_project_defaults``) to show
    exactly what a new ticket would receive.  The returned dict is for display
    only — it is not used in ticket creation.

    The *project* object is expected to be already loaded (e.g. from
    ``list_projects``).  ``apply_project_defaults`` will look it up again via
    its identity-map cache so no extra query is incurred.
    """
    resolved = apply_project_defaults(
        session, {"project_id": project.id, "title": "example-ticket"},
    )

    workspaces = resolved.get("workspaces") or []
    primary = next((w for w in workspaces if w.get("role") == "primary"), {})
    linked = [w for w in workspaces if w.get("role") == "linked"]

    return {
        "source_path": project.source_path,
        "workspace_mode": (
            resolved.get("workspace_mode")
            or project.default_workspace_mode
            or "directory"
        ),
        "worktree_name_template": project.default_worktree_name_template,
        "worktree_name_resolved": primary.get("worktree_name"),
        "base_ref": project.default_base_ref,
        "linked_workspaces": linked,
        "toolchains": project.default_toolchains or [],
        "tool_paths": project.default_tool_paths or [],
        "toolchain_overrides": resolved.get("toolchain_overrides"),
    }


def _default_workspaces(project: Project, fields: dict[str, Any]) -> list[dict[str, Any]]:
    linked = list(project.default_linked_workspaces or [])
    worktree_name = fields.get("worktree_name")
    if not worktree_name:
        worktree_name = _resolve_worktree_name(project, str(fields.get("title") or ""))
    primary = {
        "role": "primary",
        "label": "primary",
        "kind": fields.get("workspace_mode") or project.default_workspace_mode or "directory",
        "access": "read_write",
        "source_path": fields.get("source_path") or project.source_path,
        "worktree_name": worktree_name,
        "worktree_path": fields.get("worktree_path"),
        "base_ref": project.default_base_ref,
        "retention": "preserve",
    }
    return [primary, *linked]


def _resolve_worktree_name(project: Project, title: str) -> str | None:
    template = project.default_worktree_name_template
    if not template:
        return None
    return template.replace("{slug}", slugify(title))


def _validate_workspace_mode(value: str | None) -> None:
    if value is not None and value not in _VALID_WORKSPACE_MODES:
        raise ValueError(f"unknown workspace mode {value!r}")


# ---------------------------------------------------------------------------
# Attention rollup — drives the project sidebar/strip badges + ordering.
# docs/design/project-control-plane.md §Chrome.
# ---------------------------------------------------------------------------

# Inbox item counts as "needs you" once it has sat untriaged this long.
INBOX_STALE_AFTER = timedelta(hours=48)

# Decided states whose tickets appear in the acknowledgement digest (mirrors
# domain.ack.DIGEST_STATUSES — duplicated only to avoid a cross-domain import).
_DIGEST_STATUSES = ("archived",)


@dataclass
class ProjectAttentionRow:
    """One active project's attention rollup. See schemas.ProjectAttention."""

    id: str
    name: str
    slug: str
    color: Optional[str]
    review: int
    failed: int
    inbox_blocked: int
    unacked: int
    running: int
    needs_you: int
    last_activity_at: Optional[datetime]


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops tzinfo; treat naive stored datetimes as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _latest_runs_by_ticket(session: Session, ticket_ids: list[str]) -> dict[str, Run]:
    """Most-recent run per ticket (by started_at, id breaks ties), batched.

    One query over the ticket set; the latest row per ticket wins because rows
    arrive newest-first. Mirrors the pattern in ``domain.ack._latest_runs``."""
    if not ticket_ids:
        return {}
    rows = session.scalars(
        select(Run)
        .where(Run.ticket_id.in_(ticket_ids))
        .order_by(Run.started_at.desc(), Run.id.desc())
    )
    out: dict[str, Run] = {}
    for r in rows:
        out.setdefault(r.ticket_id, r)
    return out


def _latest_events_by_ticket(session: Session, ticket_ids: list[str]) -> dict[str, TicketEvent]:
    """Most-recent ticket_event per ticket, batched (any to_status)."""
    if not ticket_ids:
        return {}
    rows = session.scalars(
        select(TicketEvent)
        .where(TicketEvent.ticket_id.in_(ticket_ids))
        .order_by(TicketEvent.created_at.desc(), TicketEvent.id.desc())
    )
    out: dict[str, TicketEvent] = {}
    for ev in rows:
        out.setdefault(ev.ticket_id, ev)
    return out


def attention_rollup(session: Session) -> list[ProjectAttentionRow]:
    """Per-active-project attention rollup for sidebar/strip badges + ordering.

    One efficient pass: a small fixed set of batched queries covers every active
    project (never one query per project). Returns display-ordered rows:
    attention desc, then running, then true last activity, then name.

    "Last activity" derives from the latest run/event across the project's
    tickets — deliberately NOT ``Project.updated_at``, which drifts.
    """
    # Lazy import to avoid the domain.tickets <-> domain.projects cycle.
    from nightdesk.domain.tickets import ticket_completeness

    projects = list_projects(session)
    if not projects:
        return []

    proj_ids = [p.id for p in projects]
    # Every ticket owned by an active project (archived + non-archived —
    # archived tickets still carry unacked debt). Workspaces are eager-loaded
    # so the inbox "blocked" check (ticket_completeness) doesn't N+1.
    tickets = list(
        session.scalars(
            select(Ticket)
            .where(Ticket.project_id.in_(proj_ids))
            .options(selectinload(Ticket.workspaces))
        )
    )
    ticket_ids = [t.id for t in tickets]
    latest_runs = _latest_runs_by_ticket(session, ticket_ids)
    latest_events = _latest_events_by_ticket(session, ticket_ids)

    now = _now()
    stale_before = now - INBOX_STALE_AFTER
    epoch = datetime(1, 1, 1, tzinfo=timezone.utc)

    # Accumulators per project.
    agg: dict[str, dict[str, Any]] = {
        p.id: {
            "project": p,
            "review": 0,
            "failed": 0,
            "inbox_blocked": 0,
            "unacked": 0,
            "running": 0,
            "last_activity": epoch,
        }
        for p in projects
    }

    for t in tickets:
        bucket = agg.get(t.project_id)
        if bucket is None:
            continue
        if t.status == "review":
            bucket["review"] += 1
        if t.status == "running":
            bucket["running"] += 1
        # Unacked debt: decided tickets a human has not acknowledged.
        if t.status in _DIGEST_STATUSES and t.acknowledged_at is None:
            bucket["unacked"] += 1
        # Inbox needing triage: blocked (can't promote) or stale >48h.
        if t.status == "inbox":
            created = _aware(t.created_at)
            stale = created is not None and created < stale_before
            blocked = bool(ticket_completeness(t))
            if stale or blocked:
                bucket["inbox_blocked"] += 1
        # Latest run failed (non-archived only — an archived ticket's old
        # failure is settled history, not a live "needs you").
        if t.status != "archived":
            run = latest_runs.get(t.id)
            if run is not None and run.exit_status is not None and run.exit_status != "success":
                bucket["failed"] += 1
        # True last activity: the most recent run finish/start or event.
        run = latest_runs.get(t.id)
        if run is not None:
            run_ts = _aware(run.finished_at) or _aware(run.started_at)
            if run_ts and run_ts > bucket["last_activity"]:
                bucket["last_activity"] = run_ts
        ev = latest_events.get(t.id)
        if ev is not None:
            ev_ts = _aware(ev.created_at)
            if ev_ts and ev_ts > bucket["last_activity"]:
                bucket["last_activity"] = ev_ts

    rows: list[ProjectAttentionRow] = []
    for bucket in agg.values():
        p: Project = bucket["project"]
        needs = (
            bucket["review"] + bucket["failed"]
            + bucket["inbox_blocked"] + bucket["unacked"]
        )
        last = bucket["last_activity"]
        rows.append(ProjectAttentionRow(
            id=p.id,
            name=p.name,
            slug=p.slug,
            color=p.color,
            review=bucket["review"],
            failed=bucket["failed"],
            inbox_blocked=bucket["inbox_blocked"],
            unacked=bucket["unacked"],
            running=bucket["running"],
            needs_you=needs,
            last_activity_at=last if last != epoch else None,
        ))

    # Display order: attention desc, then running, then last activity desc
    # (None last), then name for a stable tie-break.
    rows.sort(key=lambda r: (
        -r.needs_you,
        -r.running,
        -int((r.last_activity_at or epoch).timestamp()),
        (r.name or "").lower(),
    ))
    return rows
