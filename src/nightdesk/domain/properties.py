"""Shared metadata-property registry for the reusable property picker.

This module is the single source of truth that backs the one anchored
property-picker primitive (``partials/property_picker.html`` +
``static/property_picker.js``). Every ticket metadata property the picker can
edit — priority, status, project, and (when its schema lands) labels — is
described here exactly once, so cards, rows, the sidebar, the ticket-detail
header, the command palette, and bulk actions all share the same options,
chip rendering, and focused-update behaviour. There are deliberately **no**
per-property picker components or per-property update routes: the generic
``/board/tickets/{id}/picker/{prop}`` (menu) and
``/board/tickets/{id}/property/{prop}`` (commit) routes drive everything from
this registry.

Each property exposes four things:

* ``chip(ticket, project=None)`` – the small display chip for the current
  value (label + css + optional colour swatch).
* ``options(session, ticket)`` – the selectable options for the popover menu.
* ``apply(session, ticket_id, value)`` – the focused update. Raises
  ``ValueError`` for an invalid value, ``TicketNotFound`` /
  ``InvalidTransition`` / ``ProjectNotFound`` from the domain layer.
* ``searchable`` – whether the popover should show a filter box.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from nightdesk.domain.priority import (
    PRIORITY_SCALE,
    priority_css,
    priority_label,
    resolve_priority,
)
from nightdesk.domain.projects import get_project, list_projects
from nightdesk.domain.tickets import (
    _VALID_TRANSITIONS,
    get_ticket,
    transition_status,
    update_ticket,
)

# ---------------------------------------------------------------------------
# Status presentation + transition rules
# ---------------------------------------------------------------------------

# Display metadata for each lifecycle status. The css strings reuse utility
# classes already present in the compiled stylesheet (see the status chips in
# sidebar.html) so no Tailwind rebuild is required.
STATUS_META: dict[str, dict[str, str]] = {
    "inbox": {"label": "Inbox", "css": "border-accent/40 text-accent"},
    "draft": {"label": "Draft", "css": "border-border text-fg-muted"},
    "queued": {"label": "Queued", "css": "border-info/40 text-info"},
    "running": {"label": "Running", "css": "border-accent text-accent"},
    "review": {"label": "Review", "css": "border-warn/40 text-warn"},
    "archived": {"label": "Archived", "css": "border-border text-fg-muted"},
}

# Statuses the picker will never offer as a *target*. Entering ``running`` is
# the worker's job (it creates the Run row and sandbox); the board models a
# user "run now" via the drag-to-running / Run-now controls instead, so the
# picker deliberately omits it as a destination.
_STATUS_PICKER_EXCLUDE = {"running"}


def status_targets(current: str) -> list[str]:
    """Selectable target statuses for *current*: the current status plus its
    valid transitions, minus statuses the picker doesn't drive directly."""
    out = [current]
    for nxt in sorted(_VALID_TRANSITIONS.get(current, set())):
        if nxt in _STATUS_PICKER_EXCLUDE:
            continue
        out.append(nxt)
    return out


# ---------------------------------------------------------------------------
# Chip / option value objects
# ---------------------------------------------------------------------------


@dataclass
class Chip:
    """Renderable current-value chip for a property."""

    label: str
    css: str = "border-border text-fg-muted"
    swatch_color: Optional[str] = None
    title: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "css": self.css,
            "swatch_color": self.swatch_color,
            "title": self.title,
        }


@dataclass
class Option:
    """A selectable option in the picker popover."""

    value: str
    label: str
    css: str = "border-border text-fg-muted"
    swatch_color: Optional[str] = None
    current: bool = False
    search_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "label": self.label,
            "css": self.css,
            "swatch_color": self.swatch_color,
            "current": self.current,
            "search_text": self.search_text or self.label.lower(),
        }


@dataclass
class Property:
    """A single editable ticket metadata property."""

    key: str
    label: str
    searchable: bool
    chip_fn: Callable[..., Chip]
    options_fn: Callable[[Session, Any], list[Option]]
    apply_fn: Callable[[Session, str, str], Any]

    def chip(self, ticket: Any, project: Any = None) -> Chip:
        return self.chip_fn(ticket, project)

    def options(self, session: Session, ticket: Any) -> list[Option]:
        return self.options_fn(session, ticket)

    def apply(self, session: Session, ticket_id: str, value: str) -> Any:
        return self.apply_fn(session, ticket_id, value)


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def _priority_chip(ticket: Any, project: Any = None) -> Chip:
    value = int(getattr(ticket, "priority", 0) or 0)
    label = priority_label(value)
    return Chip(
        label=label,
        css=priority_css(value),
        title=f"Priority: {label} ({value}) — click to change",
    )


def _priority_options(session: Session, ticket: Any) -> list[Option]:
    current = int(getattr(ticket, "priority", 0) or 0)
    return [
        Option(
            value=level["name"],
            label=level["label"],
            css=level["css"],
            current=(level["value"] == current),
            search_text=f'{level["label"].lower()} {level["name"]}',
        )
        for level in PRIORITY_SCALE
    ]


def _priority_apply(session: Session, ticket_id: str, value: str) -> Any:
    resolved = resolve_priority((value or "").strip())
    if resolved is None:
        raise ValueError(f"invalid priority value: {value!r}")
    return update_ticket(session, ticket_id, priority=resolved)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _status_chip(ticket: Any, project: Any = None) -> Chip:
    status = getattr(ticket, "status", "") or ""
    meta = STATUS_META.get(status, {"label": status or "—", "css": "border-border text-fg-muted"})
    return Chip(
        label=meta["label"],
        css=meta["css"],
        title=f"Status: {meta['label']} — click to change",
    )


def _status_options(session: Session, ticket: Any) -> list[Option]:
    current = getattr(ticket, "status", "") or ""
    opts: list[Option] = []
    for status in status_targets(current):
        meta = STATUS_META.get(status, {"label": status, "css": "border-border text-fg-muted"})
        opts.append(
            Option(
                value=status,
                label=meta["label"],
                css=meta["css"],
                current=(status == current),
            )
        )
    return opts


def _status_apply(session: Session, ticket_id: str, value: str) -> Any:
    target = (value or "").strip()
    if target not in STATUS_META:
        raise ValueError(f"invalid status value: {value!r}")
    if target in _STATUS_PICKER_EXCLUDE:
        # Defensive: the picker never offers these, but a hand-rolled POST
        # might. Surface as a domain-level invalid transition.
        raise ValueError(f"status {target!r} cannot be set from the picker")
    ticket = get_ticket(session, ticket_id)
    if ticket.status == target:
        return ticket  # no-op
    return transition_status(session, ticket_id, target)


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

_NO_PROJECT_VALUE = ""


def _project_chip(ticket: Any, project: Any = None) -> Chip:
    # Prefer an explicitly-passed project (template call sites that already
    # resolved it), else fall back to the ORM relationship.
    proj = project if project is not None else getattr(ticket, "project", None)
    if proj is None:
        return Chip(
            label="No project",
            css="border-border text-fg-muted",
            title="Project: none — click to assign",
        )
    return Chip(
        label=proj.name,
        css="border-border text-fg-muted",
        swatch_color=getattr(proj, "color", None),
        title=f"Project: {proj.name} — click to change",
    )


def _project_options(session: Session, ticket: Any) -> list[Option]:
    current_id = getattr(ticket, "project_id", None) or _NO_PROJECT_VALUE
    opts: list[Option] = [
        Option(
            value=_NO_PROJECT_VALUE,
            label="No project",
            css="border-border text-fg-muted",
            current=(current_id == _NO_PROJECT_VALUE),
        )
    ]
    for proj in list_projects(session):
        opts.append(
            Option(
                value=proj.id,
                label=proj.name,
                swatch_color=getattr(proj, "color", None),
                current=(proj.id == current_id),
                search_text=f"{proj.name.lower()} {getattr(proj, 'slug', '') or ''}",
            )
        )
    return opts


def _project_apply(session: Session, ticket_id: str, value: str) -> Any:
    project_id = (value or "").strip() or None
    if project_id is not None:
        # Raise ProjectNotFound early with a clean 404 rather than letting the
        # FK fail deep in update_ticket.
        get_project(session, project_id)
    return update_ticket(session, ticket_id, project_id=project_id)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PROPERTY_REGISTRY: dict[str, Property] = {
    "priority": Property(
        key="priority",
        label="Priority",
        searchable=False,
        chip_fn=_priority_chip,
        options_fn=_priority_options,
        apply_fn=_priority_apply,
    ),
    "status": Property(
        key="status",
        label="Status",
        searchable=False,
        chip_fn=_status_chip,
        options_fn=_status_options,
        apply_fn=_status_apply,
    ),
    "project": Property(
        key="project",
        label="Project",
        searchable=True,
        chip_fn=_project_chip,
        options_fn=_project_options,
        apply_fn=_project_apply,
    ),
}


def get_property(prop: str) -> Property:
    """Look up a registered property or raise ``KeyError``."""
    return PROPERTY_REGISTRY[prop]


def property_chip(prop: str, ticket: Any, project: Any = None) -> dict[str, Any]:
    """Template helper: chip dict for *prop* on *ticket*.

    Exposed as a Jinja global so any template can render the current-value
    chip without threading per-property logic through the view.
    """
    return get_property(prop).chip(ticket, project).as_dict()
