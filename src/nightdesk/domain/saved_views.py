"""Saved views: named, bookmarkable slices of board or list.

A saved view captures the URL params for a surface so the user can navigate
back to a recurring query with one click.  Applying a view is pure client-side
navigation to the composed URL — no server-side rendering state.

Surfaces: "board" (/?q=&group=) and "list" (/list?q=&group=&order=).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import SavedView


# ---------------------------------------------------------------------------
# Surface constants
# ---------------------------------------------------------------------------

VALID_SURFACES: frozenset[str] = frozenset(["board", "list"])

# Allowed URL params per surface.  All values must be non-empty strings.
SURFACE_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "board": frozenset(["q", "group", "order", "props"]),
    "list": frozenset(["q", "group", "order", "props"]),
}

_SURFACE_PATHS: dict[str, str] = {
    "board": "/",
    "list": "/list",
}

# Stable emission order so URLs are deterministic.
_PARAM_ORDER = ("q", "group", "order", "props")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SavedViewNotFound(Exception):
    pass


class SavedViewNameTaken(Exception):
    pass


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    return name


def _validate_surface(surface: str) -> str:
    surface = (surface or "").strip().lower()
    if surface not in VALID_SURFACES:
        raise ValueError(
            f"surface must be one of {sorted(VALID_SURFACES)}, got {surface!r}"
        )
    return surface


def _validate_params(surface: str, params: dict) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    allowed = SURFACE_ALLOWED_PARAMS[surface]
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(
            f"unknown params for surface {surface!r}: {sorted(unknown)}; "
            f"allowed: {sorted(allowed)}"
        )
    for k, v in params.items():
        if not isinstance(v, str):
            raise ValueError(f"param {k!r} must be a string, got {type(v).__name__}")
    # Strip empty-string values — they carry no filter effect.
    return {k: v for k, v in params.items() if v}


# ---------------------------------------------------------------------------
# URL composition
# ---------------------------------------------------------------------------

def view_url(view: SavedView) -> str:
    """Compose the navigation URL for a saved view from its surface and params."""
    base = _SURFACE_PATHS[view.surface]
    params = view.params or {}
    ordered = [(k, params[k]) for k in _PARAM_ORDER if k in params and params[k]]
    if ordered:
        return base + "?" + urlencode(ordered)
    return base


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def create_saved_view(
    session: Session,
    *,
    name: str,
    surface: str,
    params: dict,
) -> SavedView:
    """Create a new saved view.

    Raises ``SavedViewNameTaken`` on duplicate name, ``ValueError`` on invalid
    surface or params.
    """
    name = _validate_name(name)
    surface = _validate_surface(surface)
    params = _validate_params(surface, params)
    existing = session.scalar(select(SavedView).where(SavedView.name == name))
    if existing is not None:
        raise SavedViewNameTaken(name)
    # Position after the current last view.
    max_pos = session.scalar(
        select(SavedView.position).order_by(SavedView.position.desc()).limit(1)
    )
    position = (max_pos if max_pos is not None else -1) + 1
    view = SavedView(name=name, surface=surface, params=params, position=position)
    session.add(view)
    session.commit()
    session.refresh(view)
    return view


def list_saved_views(session: Session) -> list[SavedView]:
    """All saved views ordered by position then creation time."""
    return list(session.scalars(
        select(SavedView).order_by(
            SavedView.position.asc(), SavedView.created_at.asc()
        )
    ))


def get_saved_view(session: Session, view_id: str) -> SavedView:
    """Fetch a single view; raises ``SavedViewNotFound`` if missing."""
    view = session.get(SavedView, view_id)
    if view is None:
        raise SavedViewNotFound(view_id)
    return view


def rename_saved_view(session: Session, view_id: str, *, name: str) -> SavedView:
    """Rename a saved view.

    Raises ``SavedViewNotFound``, ``SavedViewNameTaken``, or ``ValueError``.
    """
    name = _validate_name(name)
    view = get_saved_view(session, view_id)
    if name == view.name:
        return view
    existing = session.scalar(select(SavedView).where(SavedView.name == name))
    if existing is not None:
        raise SavedViewNameTaken(name)
    view.name = name
    session.commit()
    session.refresh(view)
    return view


def delete_saved_view(session: Session, view_id: str) -> None:
    """Delete a saved view; raises ``SavedViewNotFound`` if missing."""
    view = get_saved_view(session, view_id)
    session.delete(view)
    session.commit()


def reorder_saved_views(session: Session, ids: list[str]) -> None:
    """Assign sequential positions to views in the given id order.

    Views whose ids are not in the list keep their existing positions.
    """
    for pos, vid in enumerate(ids):
        view = session.get(SavedView, vid)
        if view is not None:
            view.position = pos
    session.commit()
