from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nightdesk.db.models import Project
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
