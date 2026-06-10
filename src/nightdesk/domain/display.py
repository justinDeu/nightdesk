"""Display settings for ticket surfaces (grouping + ordering).

This is the small, shared "display model" the list view consumes so the board
and the list render the same filtered ticket set but can be grouped and ordered
independently. The upstream Display ticket scoped a broader URL-backed layout
state; this module keeps that surface deliberately minimal and reusable:

* ``LIST_GROUP_OPTIONS`` / ``LIST_ORDER_OPTIONS`` — the selectable keys, with
  human labels, for the list-view toolbar.
* ``normalize_group`` / ``normalize_order`` — coerce a raw query-param value to
  a known key, falling back to the default.
* ``order_tickets`` — sort one bucket of tickets by an ordering key (stable, so
  the board's natural order survives as the tie-breaker).
* ``group_tickets`` — bucket tickets by a grouping key into ordered groups.

The grouping/ordering logic lives here (not in a route or template) so any
future surface — bulk actions, saved views — can reuse it without duplication.
Status grouping is intentionally *not* handled here: the board already buckets
tickets into status columns (with the run-now visual reshuffle), so callers
reuse those columns directly for ``group='status'`` and only fall back to
``group_tickets`` for the other groupings.
"""
from __future__ import annotations

from typing import Any, Optional

from nightdesk.domain.priority import priority_label

# ---------------------------------------------------------------------------
# Option tables (key, human label)
# ---------------------------------------------------------------------------

LIST_GROUP_OPTIONS: list[tuple[str, str]] = [
    ("status", "Status"),
    ("project", "Project"),
    ("priority", "Priority"),
    ("label", "Label"),
    ("profile", "Profile"),
    ("none", "None"),
]

LIST_ORDER_OPTIONS: list[tuple[str, str]] = [
    ("manual", "Manual"),
    ("priority", "Priority"),
    ("updated", "Updated"),
    ("created", "Created"),
    ("title", "Title"),
]

_GROUP_KEYS = {k for k, _ in LIST_GROUP_OPTIONS}
_ORDER_KEYS = {k for k, _ in LIST_ORDER_OPTIONS}

DEFAULT_GROUP = "status"
DEFAULT_ORDER = "manual"


def normalize_group(value: Optional[str]) -> str:
    """Coerce *value* to a known grouping key, defaulting to ``status``."""
    v = (value or "").strip().lower()
    return v if v in _GROUP_KEYS else DEFAULT_GROUP


def normalize_order(value: Optional[str]) -> str:
    """Coerce *value* to a known ordering key, defaulting to ``manual``."""
    v = (value or "").strip().lower()
    return v if v in _ORDER_KEYS else DEFAULT_ORDER


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def order_tickets(tickets: list[Any], order: str) -> list[Any]:
    """Return a new list of *tickets* sorted by the ordering key.

    Python's sort is stable, so ``manual`` (and ties under any other key)
    preserves the incoming order — which is the board's natural order
    (``position`` asc, ``priority`` desc, ``created_at`` asc).
    """
    o = normalize_order(order)
    if o == "manual":
        return list(tickets)
    if o == "priority":
        return sorted(tickets, key=lambda t: -int(getattr(t, "priority", 0) or 0))
    if o == "updated":
        return sorted(
            tickets,
            key=lambda t: (getattr(t, "updated_at", None) or getattr(t, "created_at", None)),
            reverse=True,
        )
    if o == "created":
        return sorted(tickets, key=lambda t: getattr(t, "created_at", None), reverse=True)
    if o == "title":
        return sorted(tickets, key=lambda t: (getattr(t, "title", "") or "").lower())
    return list(tickets)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _group_by_label_for_list(
    tickets: list[Any], labels: list[Any], order: str
) -> list[dict]:
    """Label grouping for the list view.

    Multi-membership: a ticket with several labels appears under each of them.
    Tickets with no labels go into the trailing "Unlabeled" bucket.
    Groups follow the label list order (alphabetical by name, matching
    ``list_labels``), with swatch_color populated from ``label.color``.
    """
    buckets: dict[str, list] = {f"label:{l.id}": [] for l in labels}
    unlabeled: list = []
    for t in tickets:
        tl = list(getattr(t, "labels", None) or [])
        if not tl:
            unlabeled.append(t)
        else:
            for l in tl:
                key = f"label:{l.id}"
                buckets.setdefault(key, []).append(t)
    groups: list[dict] = []
    for l in labels:
        key = f"label:{l.id}"
        ts = buckets.get(key, [])
        groups.append({
            "key": key,
            "label": l.name,
            "swatch_color": (l.color or None),
            "count": len(ts),
            "tickets": order_tickets(ts, order),
        })
    groups.append({
        "key": "label:__none__",
        "label": "Unlabeled",
        "swatch_color": None,
        "count": len(unlabeled),
        "tickets": order_tickets(unlabeled, order),
    })
    return groups


def group_tickets(
    tickets: list[Any],
    *,
    group: str,
    order: str,
    projects_by_id: Optional[dict] = None,
    profiles_by_id: Optional[dict] = None,
    labels: Optional[list] = None,
) -> list[dict]:
    """Bucket *tickets* by the grouping key into ordered groups.

    Each group is a dict ``{key, label, swatch_color, count, tickets}`` with the
    tickets sorted by *order*. Groups themselves are returned in a stable,
    meaningful order (priority high→low, projects/profiles by name with the
    "none" bucket last). ``group='status'`` is not handled here — callers reuse
    the board's status columns for that case.
    """
    g = normalize_group(group)
    projects_by_id = projects_by_id or {}
    profiles_by_id = profiles_by_id or {}

    # Label grouping requires multi-membership so it runs a separate path.
    if g == "label":
        return _group_by_label_for_list(tickets, labels=labels or [], order=order)

    # key -> (sort_key tuple, label, swatch_color, [tickets])
    buckets: dict[str, list] = {}
    meta: dict[str, tuple] = {}

    for t in tickets:
        if g == "project":
            proj = projects_by_id.get(getattr(t, "project_id", None)) if getattr(t, "project_id", None) else None
            if proj is not None:
                key = f"project:{proj.id}"
                label = proj.name
                swatch = getattr(proj, "color", None)
                sortk = (0, int(getattr(proj, "position", 0) or 0), label.lower())
            else:
                key = "project:__none__"
                label = "No project"
                swatch = None
                sortk = (1, 0, "")
        elif g == "priority":
            val = int(getattr(t, "priority", 0) or 0)
            key = f"priority:{val}"
            label = priority_label(val)
            swatch = None
            sortk = (-val, 0, "")
        elif g == "profile":
            prof = profiles_by_id.get(getattr(t, "profile_id", None))
            if prof is not None:
                key = f"profile:{prof.id}"
                label = prof.name
                swatch = None
                sortk = (0, 0, label.lower())
            else:
                key = "profile:__none__"
                label = "No profile"
                swatch = None
                sortk = (1, 0, "")
        else:  # "none" — a single flat bucket
            key = "all"
            label = ""
            swatch = None
            sortk = (0, 0, "")

        buckets.setdefault(key, []).append(t)
        meta.setdefault(key, (sortk, label, swatch))

    groups: list[dict] = []
    for key, ts in buckets.items():
        sortk, label, swatch = meta[key]
        groups.append(
            {
                "key": key,
                "label": label,
                "swatch_color": swatch,
                "count": len(ts),
                "tickets": order_tickets(ts, order),
                "_sort": sortk,
            }
        )
    groups.sort(key=lambda gr: gr["_sort"])
    for gr in groups:
        gr.pop("_sort", None)
    return groups
