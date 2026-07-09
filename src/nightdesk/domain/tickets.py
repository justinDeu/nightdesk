from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from nightdesk.db.models import (
    Label, Run, Ticket, TicketDependency, TicketEvent, TicketWorkspace,
)
from nightdesk.domain.events import ADMIN, Actor, record_transition_event
from nightdesk.domain.projects import apply_project_defaults, get_project
from nightdesk.domain.priority import validate_priority
from nightdesk.domain.toolchains import (
    assert_known_toolchains,
    assert_paths_not_excluded,
    clean_string_list,
    current_config,
)


# v2 lifecycle. Run-level outcomes (success/failed/cancelled) now live on Run.exit_status.
#
# ``inbox`` is the entry point for under-specified work captured for triage. It
# sits OUTSIDE the runnable board (the board only renders draft/queued/running/
# review) and the scheduler never picks it (it picks ``status='queued'`` only).
# An inbox item is promoted onto the board (``draft``/``queued``) once it is
# complete enough to run, or declined (``archived``). Promotion crosses the
# incomplete-ticket validation boundary enforced in ``transition_with_position``.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "inbox":    {"draft", "queued", "archived"},  # promote / promote+queue / decline
    "draft":    {"queued", "running", "inbox", "archived"},   # running implies run-now
    "queued":   {"draft", "running", "archived"},
    "running":  {"review"},
    "review":   {"queued", "archived"},
    "archived": {"queued"},
}

_ALL_STATUSES = ("inbox", "draft", "queued", "running", "review", "archived")

# Statuses on (or destined for) the runnable board. Promoting an inbox item to
# any of these requires the ticket to be complete; declining (inbox -> archived)
# does not.
_RUNNABLE_TARGETS = {"draft", "queued", "running"}


class TicketNotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


class IncompleteTicket(InvalidTransition):
    """Raised when an under-specified inbox ticket is promoted to the runnable
    board before it has the fields a run needs. A subclass of
    ``InvalidTransition`` so existing route handlers map it to HTTP 409."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons) or "ticket is incomplete")


class ConversationNotResumable(InvalidTransition):
    """Raised when Continue is requested on a conversation that has no
    resumable session (session_id is null — e.g. its first turn crashed before
    the SDK emitted a session). Continue must route to New conversation instead.
    A subclass of ``InvalidTransition`` so route handlers map it to HTTP 409."""


_WORKSPACE_KINDS = {"directory", "git_worktree"}


def _normalize_workspace_kind(kind: str) -> str:
    if kind == "in_place":
        return "directory"
    if kind == "worktree":
        return "git_worktree"
    return kind


def _workspace_dict(raw: object) -> dict:
    data = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
    data["kind"] = _normalize_workspace_kind(data["kind"])
    return data




def _clean_toolchain_overrides(value: object) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("toolchain_overrides must be an object")
    cleaned = {
        "enable": clean_string_list(value.get("enable"), field="toolchain_overrides.enable"),
        "disable": clean_string_list(value.get("disable"), field="toolchain_overrides.disable"),
        "extra_paths": clean_string_list(value.get("extra_paths"), field="toolchain_overrides.extra_paths"),
    }
    assert_paths_not_excluded(cleaned["extra_paths"], field="toolchain_overrides.extra_paths")
    return cleaned if any(cleaned.values()) else None


def _stage_next_run(ticket: Ticket, *, intent: str,
                    workspace_policy: Optional[str] = None,
                    conversation_id: Optional[str] = None,
                    new_conversation: bool = False) -> None:
    """Stage the next run's intent + conversation target on permission_overrides.

    The worker (run_one) reads these ephemeral keys to decide whether the next
    turn CONTINUES an existing conversation (``conversation_id`` set, resumes
    its session_id) or starts a NEW one (``new_conversation`` set, fresh
    session). ``nightdesk_parent_run_id`` is retained for accounting/traceability
    even though the resume source is now the conversation's session_id.
    """
    overrides = dict(ticket.permission_overrides or {})
    overrides["nightdesk_run_intent"] = intent
    overrides["nightdesk_parent_run_id"] = ticket.current_run_id
    if workspace_policy is None:
        overrides.pop("nightdesk_restart_workspace_policy", None)
    else:
        overrides["nightdesk_restart_workspace_policy"] = workspace_policy
    if conversation_id is None:
        overrides.pop("nightdesk_conversation_id", None)
    else:
        overrides["nightdesk_conversation_id"] = conversation_id
    if new_conversation:
        overrides["nightdesk_new_conversation"] = True
    else:
        overrides.pop("nightdesk_new_conversation", None)
    ticket.permission_overrides = overrides
def _validate_workspace_specs(workspace_specs: list[dict]) -> list[dict]:
    primaries = [w for w in workspace_specs if w.get("role") == "primary"]
    if len(primaries) != 1:
        raise ValueError("workspaces must include exactly one primary workspace")
    if any(w.get("kind") not in _WORKSPACE_KINDS for w in workspace_specs):
        raise ValueError("workspace kind must be directory or git_worktree")

    primary = primaries[0]
    primary_name = primary.get("worktree_name")
    for w in workspace_specs:
        if w.get("role") != "linked" or w.get("kind") != "git_worktree":
            continue
        if w.get("access") != "read_write":
            raise ValueError("linked git workspaces must be read_write")
        linked_name = w.get("worktree_name")
        if linked_name not in (None, "", primary_name):
            raise ValueError("linked git worktree name must match primary")
        w["worktree_name"] = primary_name
    return workspace_specs


def _apply_workspaces(session: Session, ticket: Ticket,
                      workspace_specs: list[object]) -> None:
    ticket.workspaces.clear()
    for idx, raw in enumerate(workspace_specs):
        data = _workspace_dict(raw)
        ticket.workspaces.append(TicketWorkspace(
            ticket_id=ticket.id,
            role=data.get("role") or "linked",
            label=data.get("label") or data.get("role") or f"workspace-{idx + 1}",
            kind=data["kind"],
            access=data.get("access") or "read_write",
            source_path=data.get("source_path"),
            worktree_name=data.get("worktree_name"),
            worktree_path=data.get("worktree_path"),
            branch=data.get("branch"),
            base_ref=data.get("base_ref"),
            retention=data.get("retention") or "preserve",
            state="pending",
            position=idx,
        ))



def ticket_completeness(ticket: Ticket) -> list[str]:
    """Return human-readable reasons an inbox ticket is not yet runnable.

    A ticket is "complete" (promotable onto the runnable board) when it has a
    title, a profile, and a primary workspace with a source path — exactly the
    fields ``create_ticket`` hard-requires for non-inbox tickets. Under-specified
    inbox items may be missing the workspace (and that is the whole point of the
    inbox), so this is the single boundary that decides whether such an item can
    leave triage.
    """
    reasons: list[str] = []
    if not (ticket.title or "").strip():
        reasons.append("a title is required")
    if not ticket.profile_id:
        reasons.append("a profile is required")
    primary = next((w for w in ticket.workspaces if w.role == "primary"), None)
    if primary is None or not (primary.source_path or "").strip():
        reasons.append("a workspace with a source path is required")
    return reasons


def is_ticket_complete(ticket: Ticket) -> bool:
    """True when a ticket has everything a run needs (see ``ticket_completeness``)."""
    return not ticket_completeness(ticket)


def ticket_missing_fields(ticket: Ticket) -> set[str]:
    """Return the set of *form field* keys an inbox item must still satisfy
    before it can be promoted onto the runnable board.

    This is the field-level counterpart to ``ticket_completeness`` (which yields
    human-readable sentences): the promote modal uses it to highlight exactly the
    inputs that need attention. Keys mirror the edit-modal field names:

      * ``"title"``     — title input
      * ``"profile"``   — profile select (``profile_id``)
      * ``"workspace"`` — primary workspace source path

    Kept in lock-step with ``ticket_completeness`` so the highlight and the
    server-side validation boundary can never disagree.
    """
    missing: set[str] = set()
    if not (ticket.title or "").strip():
        missing.add("title")
    if not ticket.profile_id:
        missing.add("profile")
    primary = next((w for w in ticket.workspaces if w.role == "primary"), None)
    if primary is None or not (primary.source_path or "").strip():
        missing.add("workspace")
    return missing


def create_ticket(session: Session, **fields) -> Ticket:
    """Create a ticket. Defaults to status='draft' (v2).

    Inbox tickets are the one exception to the "exactly one primary workspace"
    rule: captured triage items may be under-specified (no workspace yet), so a
    ``status='inbox'`` ticket can be created with no workspace at all. It must
    be fleshed out and promoted (crossing the completeness boundary) before it
    can run.
    """
    fields = apply_project_defaults(session, fields)
    workspace_specs = fields.pop("workspaces", None)
    source_path = fields.pop("source_path", None)
    workspace_mode = fields.pop("workspace_mode", "directory")
    worktree_name = fields.pop("worktree_name", None)
    worktree_path = fields.pop("worktree_path", None)
    fields.setdefault("status", "draft")
    if "toolchain_overrides" in fields:
        fields["toolchain_overrides"] = _clean_toolchain_overrides(
            fields["toolchain_overrides"],
        )
        toolchains = fields["toolchain_overrides"] or {}
        assert_known_toolchains(toolchains.get("enable", []), config=current_config(session))
    fields.setdefault("additional_dirs", [])
    status = fields.get("status")
    if status not in _ALL_STATUSES:
        raise InvalidTransition(f"unknown status {status!r}")
    # Inbox tickets may be captured before a profile is chosen — same exception
    # as the workspace requirement below, and the same field `ticket_completeness`
    # gates at promotion time. Everything else must have a profile up front.
    if status != "inbox" and not fields.get("profile_id"):
        raise ValueError("profile_id is required")
    if "priority" in fields:
        fields["priority"] = validate_priority(fields["priority"])

    if not workspace_specs and source_path:
        workspace_specs = [{
            "role": "primary",
            "label": "primary",
            "kind": _normalize_workspace_kind(workspace_mode),
            "access": "read_write",
            "source_path": source_path,
            "worktree_name": worktree_name,
            "worktree_path": worktree_path,
            "retention": "preserve",
        }]
    # Inbox tickets may be captured before a workspace is known; everything else
    # still requires exactly one primary workspace up front.
    normalized: list[dict] = []
    if not workspace_specs:
        if status != "inbox":
            raise ValueError("workspaces must include exactly one primary workspace")
    else:
        normalized = [_workspace_dict(w) for w in workspace_specs]
        normalized = _validate_workspace_specs(normalized)
        primary = next(w for w in normalized if w.get("role") == "primary")
        if not primary.get("source_path"):
            raise ValueError("primary workspace source_path is required")

    # Position defaults to end of the chosen column.
    if "position" not in fields:
        fields["position"] = _next_position(session, status)
    t = Ticket(**fields)
    session.add(t)
    session.flush()
    if normalized:
        _apply_workspaces(session, t, normalized)
    session.commit()
    session.refresh(t)
    return t


def get_ticket(session: Session, ticket_id: str) -> Ticket:
    t = session.get(Ticket, ticket_id)
    if t is None:
        raise TicketNotFound(ticket_id)
    return t


def _latest_run_scalar(column):
    """Correlated scalar subquery for a column of a ticket's *latest* run.

    "Latest" is by ``started_at`` desc (``id`` desc breaks ties), matching the
    newest-run-first order the runs list and the Archive row use. Correlates on
    ``Ticket.id`` so it composes into both the filter WHERE and the ORDER BY of
    ``list_tickets``/``count_tickets`` without a GROUP BY or row-duplicating
    join — the count and the page therefore stay in lockstep.
    """
    return (
        select(column)
        .where(Run.ticket_id == Ticket.id)
        .order_by(Run.started_at.desc(), Run.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def _ticket_filters(
    status: Optional[str],
    profile_id: Optional[str],
    project_id: Optional[str],
    priority: Optional[int] = None,
    label: Optional[str] = None,
    outcome: Optional[str] = None,
    q: Optional[str] = None,
    kind: Optional[str] = "ticket",
    acknowledged: Optional[bool] = None,
) -> list:
    """Shared WHERE-clause builder for ``list_tickets`` / ``count_tickets``.

    Centralized so the page (``list_tickets``) and the total
    (``count_tickets``) can never drift apart — a mismatch would defeat the
    truncation metadata callers rely on to detect incomplete results.

    ``kind`` defaults to ``"ticket"`` so every board / Tickets-list / Archive /
    Desk surface excludes interactive sessions (``kind='session'``) by default.
    Pass ``kind=None`` to include every kind (or an explicit kind to scope to
    one). This is the single most important session-exclusion filter.

    Beyond the original status/profile/project dimensions the Archive page adds:
    ``priority`` (exact 0-4), ``label`` (name case-insensitive OR id, via an
    EXISTS so no row is duplicated), ``q`` (free-text substring over title and
    prompt), and ``outcome`` (the latest run's terminal state — ``succeeded``
    is ``exit_status == 'success'``; ``failed`` is any other finished status,
    mirroring the binary the UI's run pill shows).
    """
    filters: list = []
    if kind is not None:
        filters.append(Ticket.kind == kind)
    if status is not None:
        filters.append(Ticket.status == status)
    if profile_id is not None:
        filters.append(Ticket.profile_id == profile_id)
    if project_id is not None:
        if project_id == "null":
            filters.append(Ticket.project_id.is_(None))
        else:
            filters.append(Ticket.project_id == project_id)
    if priority is not None:
        filters.append(Ticket.priority == priority)
    if q:
        like = f"%{q}%"
        filters.append(or_(Ticket.title.ilike(like), Ticket.prompt.ilike(like)))
    if label is not None:
        filters.append(
            Ticket.labels.any(
                or_(func.lower(Label.name) == label.lower(), Label.id == label)
            )
        )
    if outcome is not None:
        latest_status = _latest_run_scalar(Run.exit_status)
        if outcome == "succeeded":
            filters.append(latest_status == "success")
        elif outcome == "failed":
            filters.append(
                and_(latest_status.is_not(None), latest_status != "success")
            )
    if acknowledged is not None:
        if acknowledged:
            filters.append(Ticket.acknowledged_at.is_not(None))
        else:
            filters.append(Ticket.acknowledged_at.is_(None))
    return filters


def list_tickets(
    session: Session,
    status: Optional[str] = None,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    sort: str = "board",
    order: str = "desc",
    priority: Optional[int] = None,
    label: Optional[str] = None,
    outcome: Optional[str] = None,
    q: Optional[str] = None,
    kind: Optional[str] = "ticket",
    acknowledged: Optional[bool] = None,
) -> list[Ticket]:
    """One page of tickets matching the filters.

    ``limit``/``offset`` are paging: combine with ``count_tickets`` (same
    filters) for a total and to detect truncation. The default ``limit`` of
    200 is a guardrail, NOT a hard ceiling — callers may request a larger
    page; the API route enforces its own max and returns paging headers.

    ``sort`` selects the ordering:

    - ``"board"`` (default): board-stable order (``position``, then priority,
      then oldest-first). This is what the board/list columns page through and
      is unchanged from before ``sort`` existed, so existing callers — the SPA
      board and any agent hitting ``GET /tickets`` — see identical results.
    - ``"recent"``: most-recently-touched first (``updated_at`` desc). Archived
      tickets have no dedicated ``archived_at`` column — archiving bumps
      ``updated_at`` — so this is the newest-first order the Archive page pages
      through. ``id`` breaks ties for a stable page boundary.
    - ``"created"``: by ``created_at`` (age of the ticket).
    - ``"priority"``: by the 0-4 priority scale, ``updated_at`` breaking ties.
    - ``"cost"``: by the latest run's ``cost_usd`` (a correlated subquery);
      tickets with no run sort as NULL (last under ``desc``, first under
      ``asc``).

    ``order`` (``desc`` default / ``asc``) flips the direction of every sort
    except ``board``, whose fixed board order ignores it. ``sort=recent`` with
    the default ``order`` is byte-identical to the pre-``order`` behavior, so
    the Archive's existing call is unchanged.
    """
    descending = order != "asc"

    def _dir(col):
        return col.desc() if descending else col.asc()

    if sort == "recent":
        order_by = (_dir(Ticket.updated_at), Ticket.id.desc())
    elif sort == "created":
        order_by = (_dir(Ticket.created_at), Ticket.id.desc())
    elif sort == "priority":
        order_by = (_dir(Ticket.priority), Ticket.updated_at.desc(), Ticket.id.desc())
    elif sort == "cost":
        order_by = (
            _dir(_latest_run_scalar(Run.cost_usd)),
            Ticket.updated_at.desc(),
            Ticket.id.desc(),
        )
    else:
        order_by = (Ticket.position.asc(), Ticket.priority.desc(), Ticket.created_at.asc())
    stmt = (
        select(Ticket)
        .where(*_ticket_filters(
            status, profile_id, project_id,
            priority=priority, label=label, outcome=outcome, q=q, kind=kind,
            acknowledged=acknowledged,
        ))
        .order_by(*order_by)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))


def count_tickets(
    session: Session,
    status: Optional[str] = None,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    priority: Optional[int] = None,
    label: Optional[str] = None,
    outcome: Optional[str] = None,
    q: Optional[str] = None,
    kind: Optional[str] = "ticket",
    acknowledged: Optional[bool] = None,
) -> int:
    """Total tickets matching the filters, ignoring ``limit``/``offset``.

    The paging counterpart to ``list_tickets``: it uses the exact same
    ``_ticket_filters`` so the count and the page agree. Callers page with
    ``offset`` and use this for ``X-Total-Count`` / has-more so truncated
    results are always detectable rather than silently clamped.
    """
    stmt = select(func.count(Ticket.id)).where(
        *_ticket_filters(
            status, profile_id, project_id,
            priority=priority, label=label, outcome=outcome, q=q, kind=kind,
            acknowledged=acknowledged,
        )
    )
    return int(session.scalar(stmt) or 0)


def update_ticket(session: Session, ticket_id: str, **fields) -> Ticket:
    workspace_specs = fields.pop("workspaces", None)
    source_path = fields.pop("source_path", None)
    workspace_mode = fields.pop("workspace_mode", None)
    worktree_name = fields.pop("worktree_name", None)
    worktree_path = fields.pop("worktree_path", None)
    t = get_ticket(session, ticket_id)
    if "toolchain_overrides" in fields:
        fields["toolchain_overrides"] = _clean_toolchain_overrides(
            fields["toolchain_overrides"],
        )
        toolchains = fields["toolchain_overrides"] or {}
        assert_known_toolchains(toolchains.get("enable", []), config=current_config(session))
    if fields.get("project_id") is not None:
        get_project(session, fields["project_id"])
    if "priority" in fields:
        fields["priority"] = validate_priority(fields["priority"])
    for k, v in fields.items():
        setattr(t, k, v)
    if workspace_specs is None and any(v is not None for v in (source_path, workspace_mode, worktree_name, worktree_path)):
        primary = next((w for w in t.workspaces if w.role == "primary"), None)
        if primary is None:
            if not source_path:
                raise ValueError("primary workspace source_path is required")
            workspace_specs = [{
                "role": "primary",
                "label": "primary",
                "kind": _normalize_workspace_kind(workspace_mode or "directory"),
                "access": "read_write",
                "source_path": source_path,
                "worktree_name": worktree_name,
                "worktree_path": worktree_path,
                "retention": "preserve",
            }]
        else:
            if source_path is not None:
                primary.source_path = source_path
            if workspace_mode is not None:
                primary.kind = _normalize_workspace_kind(workspace_mode)
            if worktree_name is not None:
                primary.worktree_name = worktree_name
            if worktree_path is not None:
                primary.worktree_path = worktree_path
    if workspace_specs is not None:
        normalized = [_workspace_dict(w) for w in workspace_specs]
        normalized = _validate_workspace_specs(normalized)
        primary = next(w for w in normalized if w.get("role") == "primary")
        if not primary.get("source_path"):
            raise ValueError("primary workspace source_path is required")
        _apply_workspaces(session, t, normalized)
    session.commit()
    session.refresh(t)
    return t


def transition_status(
    session: Session, ticket_id: str, new_status: str, *, actor: Actor = ADMIN,
) -> Ticket:
    """Plain transition; appends to the bottom of the new column."""
    return transition_with_position(
        session, ticket_id, new_status, position=None, actor=actor,
    )


def transition_with_position(
    session: Session,
    ticket_id: str,
    new_status: str,
    position: Optional[int] = None,
    *,
    actor: Actor = ADMIN,
) -> Ticket:
    """Transition a ticket to ``new_status``, optionally placing it at ``position``.

    This is a pure status/position move: it never mutates ``run_now``. The
    ``run_now`` flag means exactly one thing — "the user explicitly bypassed the
    queue" — and is set only by ``request_run_now``/``set_run_now`` (UI run-now,
    JSON API, drag-to-running). The worker transitions a *picked* ticket from
    ``queued`` to ``running`` through here on every scheduler tick; forcing
    ``run_now=True`` on that move would mislabel every normal scheduled pick as
    a run-now (which then taints ``started_as_run_now`` on the Run record). We
    deliberately leave the existing flag untouched so it survives the move and
    accurately reflects the user's real intent when ``run_one`` reads it.
    """
    t = get_ticket(session, ticket_id)
    if new_status == t.status:
        # Same column; treat as a reorder if a position is provided, otherwise no-op.
        if position is not None:
            _reorder_inserting(session, t, new_status, position)
        return t

    allowed = _VALID_TRANSITIONS.get(t.status, set())
    if new_status not in allowed:
        raise InvalidTransition(f"{t.status} -> {new_status}")

    # Incomplete-ticket validation boundary: an under-specified inbox item
    # cannot be promoted onto the runnable board until it has the fields a run
    # needs. Declining it (inbox -> archived) is always allowed.
    if t.status == "inbox" and new_status in _RUNNABLE_TARGETS:
        reasons = ticket_completeness(t)
        if reasons:
            raise IncompleteTicket(reasons)

    from_status = t.status
    _reorder_inserting(session, t, new_status, position)
    t.updated_at = datetime.now(timezone.utc)
    record_transition_event(
        session, t, from_status=from_status, to_status=new_status, actor=actor,
    )
    session.commit()
    session.refresh(t)
    return t


def reorder_in_column(
    session: Session,
    status: str,
    ticket_ids: Iterable[str],
) -> list[Ticket]:
    """Rewrite positions for all tickets in ``status`` to match the given id order.

    Tickets in ``status`` that are not listed are appended after the listed ones,
    preserving their relative order.
    """
    if status not in _ALL_STATUSES:
        raise InvalidTransition(f"unknown status {status!r}")
    listed = list(dict.fromkeys(ticket_ids))  # dedupe, preserve order
    listed_set = set(listed)
    existing = list(
        session.scalars(
            select(Ticket)
            .where(Ticket.status == status)
            .order_by(Ticket.position.asc(), Ticket.priority.desc(), Ticket.created_at.asc())
        )
    )
    by_id = {t.id: t for t in existing}
    out: list[Ticket] = []
    pos = 0
    for tid in listed:
        t = by_id.get(tid)
        if t is None:
            # Caller passed an id not in this column; skip silently — the UI may race.
            continue
        t.position = pos
        out.append(t)
        pos += 1
    for t in existing:
        if t.id in listed_set:
            continue
        t.position = pos
        out.append(t)
        pos += 1
    session.commit()
    return out


_ARCHIVABLE_SOURCES = {"draft", "queued", "review"}


def archive(session: Session, ticket_id: str, *, actor: Actor = ADMIN) -> Ticket:
    """Convenience: draft|queued|review -> archived.

    ``draft``/``queued`` archiving is the non-destructive way to discard a
    ticket that will never run (abandoned triage, or finished out of band) —
    the alternative is a destructive ``delete_ticket``. ``running`` is
    deliberately excluded: a live run must go through ``review`` first
    (cancel or finish) before it can be archived.
    """
    t = get_ticket(session, ticket_id)
    if t.status not in _ARCHIVABLE_SOURCES:
        raise InvalidTransition(f"cannot archive from {t.status}")
    return transition_status(session, ticket_id, "archived", actor=actor)


def unarchive(session: Session, ticket_id: str, *, actor: Actor = ADMIN) -> Ticket:
    """Convenience: archived -> queued."""
    t = get_ticket(session, ticket_id)
    if t.status != "archived":
        raise InvalidTransition(f"cannot unarchive from {t.status}")
    return transition_status(session, ticket_id, "queued", actor=actor)


def send_to_inbox(session: Session, ticket_id: str) -> Ticket:
    """Send a draft ticket back to the inbox for further triage.

    Mirrors the old UI's "send to inbox" action. Valid ONLY from ``draft``:
    even though ``_VALID_TRANSITIONS`` also allows ``queued``/``review``/
    ``archived`` tickets to walk back to ``draft`` via other routes, this
    helper does not chain that hop itself. It is a single-step "I drafted
    this too soon" shortcut, not a bypass around each status's own path
    back to draft.
    """
    t = get_ticket(session, ticket_id)
    if t.status != "draft":
        raise InvalidTransition(f"cannot send to inbox from {t.status}")
    return transition_status(session, ticket_id, "inbox")


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

_PROMOTE_TARGETS = {"draft", "queued"}


def list_inbox(
    session: Session,
    *,
    project_id: Optional[str] = None,
    limit: int = 200,
) -> list[Ticket]:
    """Inbox tickets for the triage surface, highest priority and newest first.

    ``project_id='null'`` selects items with no project; a real id scopes to
    that project; ``None`` returns every inbox item.
    """
    stmt = (
        select(Ticket)
        .where(Ticket.status == "inbox", Ticket.kind == "ticket")
        .order_by(Ticket.priority.desc(), Ticket.created_at.desc())
        .limit(limit)
    )
    if project_id == "null":
        stmt = stmt.where(Ticket.project_id.is_(None))
    elif project_id is not None:
        stmt = stmt.where(Ticket.project_id == project_id)
    return list(session.scalars(stmt))


def promote_ticket(session: Session, ticket_id: str, target_status: str = "draft") -> Ticket:
    """Promote an inbox item onto the runnable board.

    ``target_status`` is ``draft`` (accept for later) or ``queued`` (accept and
    queue for the scheduler). Crosses the completeness boundary, so an
    incomplete item raises ``IncompleteTicket`` (a ``InvalidTransition``).
    """
    if target_status not in _PROMOTE_TARGETS:
        raise InvalidTransition(f"cannot promote to {target_status!r}")
    t = get_ticket(session, ticket_id)
    if t.status != "inbox":
        raise InvalidTransition(f"cannot promote from {t.status}")
    return transition_status(session, ticket_id, target_status)


def decline_ticket(session: Session, ticket_id: str) -> Ticket:
    """Decline an inbox item: inbox -> archived. Always allowed (no completeness
    requirement) so junk and dead ideas can be cleared from triage."""
    t = get_ticket(session, ticket_id)
    if t.status != "inbox":
        raise InvalidTransition(f"cannot decline from {t.status}")
    return transition_status(session, ticket_id, "archived")


def set_run_now(session: Session, ticket_id: str, run_now: bool) -> Ticket:
    return update_ticket(session, ticket_id, run_now=run_now)


def request_run_now(session: Session, ticket_id: str, *, actor: Actor = ADMIN) -> Ticket:
    """Mark a ticket for immediate execution by the next scheduler tick.

    The scheduler only picks tickets where ``status='queued' AND run_now=true``
    (see ``worker/scheduler.py::pick_eligible``). So flipping ``run_now`` on a
    draft/review/archived ticket without also transitioning it to ``queued``
    is a silent no-op — the flag sticks forever and the ticket never runs.
    This helper does both, in one place, so every caller (UI form, JSON API,
    drag-to-running) lands on the same state machine.

    Behavior by current status:

    - ``draft``/``review``/``archived``: transition to ``queued`` AND set
      ``run_now=True``. All three are valid transitions per
      ``_VALID_TRANSITIONS``.
    - ``queued``: leave status alone, just set ``run_now=True``. Idempotent.
    - ``running``: raise ``InvalidTransition``. Callers should map to 409
      — the scheduler bypass has nothing to bypass and we don't want to
      restart a live run by accident.
    """
    t = get_ticket(session, ticket_id)
    if t.status == "running":
        raise InvalidTransition(f"ticket is already running")
    if t.status == "queued":
        t.run_now = True
        t.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(t)
        return t
    if t.status in ("draft", "review", "archived"):
        # Set the flag on the in-session object; the commit inside
        # transition_with_position will persist both the flag and the
        # status change atomically.
        t.run_now = True
        return transition_with_position(
            session, ticket_id, "queued", position=None, actor=actor,
        )
    # _ALL_STATUSES is exhaustive today, but be defensive against future
    # additions: refuse rather than silently swallow.
    raise InvalidTransition(f"cannot run-now from {t.status}")



def set_next_run_context(session: Session, ticket_id: str, body: Optional[str]) -> Ticket:
    t = get_ticket(session, ticket_id)
    clean = (body or "").strip() or None
    t.next_run_context = clean
    t.next_run_context_updated_at = datetime.now(timezone.utc) if clean else None
    session.commit()
    session.refresh(t)
    return t


def append_next_run_context(session: Session, ticket_id: str, text: str) -> Ticket:
    """Append ``text`` to the ticket's staged next-run-context (don't overwrite).

    Keeps ``next_run_context`` the single steering channel: review comments and
    hand-typed guidance stack in the same field rather than forking a parallel
    one. A blank ``text`` is a no-op. Reuses ``set_next_run_context`` so the
    updated-at stamp and empty-normalization stay in one place.
    """
    addition = (text or "").strip()
    if not addition:
        return get_ticket(session, ticket_id)
    t = get_ticket(session, ticket_id)
    existing = (t.next_run_context or "").rstrip()
    merged = f"{existing}\n\n{addition}" if existing else addition
    return set_next_run_context(session, ticket_id, merged)


def merge_next_run_context_into_prompt(session: Session, ticket_id: str) -> Ticket:
    t = get_ticket(session, ticket_id)
    if not t.next_run_context:
        raise ValueError("next run context is empty")
    merged = t.prompt.rstrip()
    if merged:
        merged += "\n\n"
    merged += t.next_run_context.strip()
    t.prompt = merged
    t.next_run_context = None
    t.next_run_context_updated_at = None
    session.commit()
    session.refresh(t)
    return t


def continue_ticket(session: Session, ticket_id: str, *,
                    next_run_context: Optional[str],
                    conversation_id: Optional[str] = None) -> Ticket:
    """Continue a conversation: append the typed message as the next user turn
    on the resumed SDK session, keeping full prior context.

    The target conversation is ``conversation_id`` (a selected conversation in
    the rail) or, by default, the ticket's active conversation. Continuing a
    non-active conversation re-activates it (it becomes current and the ticket
    flips back to running). A conversation with no ``session_id`` is not
    resumable — Continue is refused with a clear message routing to New
    conversation (``ConversationNotResumable``).
    """
    from nightdesk.domain.conversations import active_conversation, get_conversation
    t = set_next_run_context(session, ticket_id, next_run_context) if next_run_context is not None else get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot continue from {t.status}")
    if conversation_id is not None:
        conv = get_conversation(session, conversation_id)
        if conv.ticket_id != t.id:
            raise InvalidTransition("conversation does not belong to this ticket")
    else:
        conv = active_conversation(session, t)
    if conv is None:
        raise ConversationNotResumable(
            "no conversation to continue; start a new conversation")
    if not conv.session_id:
        raise ConversationNotResumable(
            "this conversation has no resumable session (its first turn did not "
            "record one); start a new conversation instead")
    # Re-activate: continuing (possibly an older) conversation makes it current.
    t.current_conversation_id = conv.id
    _stage_next_run(t, intent="continue", conversation_id=conv.id)
    session.commit()
    session.refresh(t)
    return request_run_now(session, ticket_id)


def new_conversation_ticket(session: Session, ticket_id: str, *,
                            next_run_context: Optional[str] = None,
                            profile_id: Optional[str] = None,
                            workspace_policy: Optional[str] = None) -> Ticket:
    """Start a NEW conversation: fresh memory by definition (new session).

    Absorbs the old retry/resume (keep workspace) and restart (fresh worktree)
    verbs — those disappear from the UI but their intent values remain
    internally. ``workspace_policy`` of ``"fresh"`` maps to the restart intent
    (fresh worktree path); anything else keeps the current files (retry intent).
    ``profile_id`` switches the runtime for the NEXT new conversation only
    (the active conversation's runtime is frozen at its creation); switching
    runtime always starts a new conversation because sessions are not portable.
    """
    t = get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot start a new conversation from {t.status}")
    if next_run_context is not None:
        t = set_next_run_context(session, ticket_id, next_run_context)
    if profile_id is not None:
        # Only changes the default for the NEXT new conversation.
        t = update_ticket_profile(session, ticket_id, profile_id)
    if workspace_policy == "fresh":
        _stage_next_run(t, intent="restart", workspace_policy="fresh_path",
                        new_conversation=True)
    else:
        _stage_next_run(t, intent="retry", new_conversation=True)
    session.commit()
    session.refresh(t)
    return request_run_now(session, ticket_id)


def resume_ticket(session: Session, ticket_id: str, *, next_run_context: Optional[str]) -> Ticket:
    """Fresh-context agent on the same worktree — a NEW conversation internally."""
    t = set_next_run_context(session, ticket_id, next_run_context) if next_run_context is not None else get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot resume from {t.status}")
    _stage_next_run(t, intent="resume", new_conversation=True)
    session.commit()
    session.refresh(t)
    return request_run_now(session, ticket_id)


def retry_ticket(session: Session, ticket_id: str, *, next_run_context: Optional[str]) -> Ticket:
    """Re-attempt on the same worktree, fresh context — a NEW conversation internally."""
    t = set_next_run_context(session, ticket_id, next_run_context) if next_run_context is not None else get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot retry from {t.status}")
    _stage_next_run(t, intent="retry", new_conversation=True)
    session.commit()
    session.refresh(t)
    return request_run_now(session, ticket_id)


def restart_ticket(session: Session, ticket_id: str, *, next_run_context: Optional[str],
                   workspace_policy: Optional[str]) -> Ticket:
    """Fresh worktree, fresh context — a NEW conversation internally."""
    if workspace_policy not in ("recreate_in_place", "fresh_path"):
        raise ValueError("restart workspace policy is required")
    t = set_next_run_context(session, ticket_id, next_run_context) if next_run_context is not None else get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot restart from {t.status}")
    _stage_next_run(t, intent="restart", workspace_policy=workspace_policy,
                    new_conversation=True)
    session.commit()
    session.refresh(t)
    return request_run_now(session, ticket_id)


def clone_ticket(session: Session, ticket_id: str, *, title: Optional[str],
                 carry_context: bool) -> Ticket:
    t = get_ticket(session, ticket_id)
    prompt = t.prompt
    if carry_context and t.next_run_context:
        prompt = prompt.rstrip()
        if prompt:
            prompt += "\n\n"
        prompt += t.next_run_context.strip()
    workspace_specs = None
    if t.workspaces:
        workspace_specs = [{
            "role": w.role,
            "label": w.label,
            "kind": w.kind,
            "access": w.access,
            "source_path": w.source_path,
            "worktree_name": w.worktree_name,
            "worktree_path": w.worktree_path,
            "branch": w.branch,
            "base_ref": w.base_ref,
            "retention": w.retention,
        } for w in t.workspaces]
    return create_ticket(
        session,
        title=title or f"{t.title} (clone)",
        prompt=prompt,
        priority=t.priority,
        profile_id=t.profile_id,
        permission_overrides=t.permission_overrides,
        toolchain_overrides=t.toolchain_overrides,
        additional_dirs=list(t.additional_dirs or []),
        project_id=t.project_id,
        workspaces=workspace_specs,
    )

def requeue(session: Session, ticket_id: str, *, actor: Actor = ADMIN) -> Ticket:
    """Move a ticket back to queued. Allowed from review or archived."""
    t = get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot requeue from {t.status}")
    return transition_status(session, ticket_id, "queued", actor=actor)


def delete_ticket(session: Session, ticket_id: str) -> None:
    t = get_ticket(session, ticket_id)
    if t.status == "running":
        raise InvalidTransition("cannot delete running ticket")
    session.delete(t)
    session.commit()


class CyclicDependency(Exception):
    pass


class DependencyNotFound(Exception):
    pass


def add_dependency(session: Session, ticket_id: str, depends_on_id: str) -> Ticket:
    """Add a dependency edge: ``ticket_id`` waits for ``depends_on_id``.

    Rejects self-dependencies, duplicate edges, and cycles (simple DFS).
    """
    if ticket_id == depends_on_id:
        raise CyclicDependency("ticket cannot depend on itself")
    t = get_ticket(session, ticket_id)
    upstream = get_ticket(session, depends_on_id)
    # Check duplicate.
    existing = session.scalar(
        select(TicketDependency).where(
            TicketDependency.ticket_id == ticket_id,
            TicketDependency.depends_on_id == depends_on_id,
        )
    )
    if existing is not None:
        return t
    # Check cycle: walk from depends_on_id following its own dependencies
    # back towards ticket_id.
    if _would_cycle(session, depends_on_id, ticket_id):
        raise CyclicDependency(
            f"adding {ticket_id[:8]} -> {depends_on_id[:8]} would create a cycle"
        )
    dep = TicketDependency(ticket_id=ticket_id, depends_on_id=depends_on_id)
    session.add(dep)
    session.commit()
    session.refresh(t)
    return t


def remove_dependency(session: Session, ticket_id: str, depends_on_id: str) -> Ticket:
    """Remove a dependency edge."""
    dep = session.scalar(
        select(TicketDependency).where(
            TicketDependency.ticket_id == ticket_id,
            TicketDependency.depends_on_id == depends_on_id,
        )
    )
    if dep is None:
        raise DependencyNotFound(
            f"no dependency from {ticket_id[:8]} to {depends_on_id[:8]}"
        )
    session.delete(dep)
    session.commit()
    t = get_ticket(session, ticket_id)
    return t


def list_dependencies(session: Session, ticket_id: str) -> list[Ticket]:
    """Return the tickets this ticket depends on (upstream)."""
    _ = get_ticket(session, ticket_id)
    rows = session.scalars(
        select(TicketDependency).where(
            TicketDependency.ticket_id == ticket_id,
        )
    )
    out: list[Ticket] = []
    for dep in rows:
        upstream = session.get(Ticket, dep.depends_on_id)
        if upstream is not None:
            out.append(upstream)
    return out


def list_dependents(session: Session, ticket_id: str) -> list[Ticket]:
    """Return the tickets that depend on this one (downstream)."""
    _ = get_ticket(session, ticket_id)
    rows = session.scalars(
        select(TicketDependency).where(
            TicketDependency.depends_on_id == ticket_id,
        )
    )
    out: list[Ticket] = []
    for dep in rows:
        downstream = session.get(Ticket, dep.ticket_id)
        if downstream is not None:
            out.append(downstream)
    return out


def _would_cycle(session: Session, start_id: str, target_id: str) -> bool:
    """DFS from ``start_id`` following dependency edges; returns True if
    ``target_id`` is reachable."""
    visited: set[str] = set()
    stack = [start_id]
    while stack:
        current = stack.pop()
        if current == target_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        deps = session.scalars(
            select(TicketDependency.depends_on_id).where(
                TicketDependency.ticket_id == current,
            )
        )
        for dep_id in deps:
            if dep_id not in visited:
                stack.append(dep_id)
    return False


def check_dependencies_satisfied(session: Session, ticket_id: str) -> tuple[bool, list[dict]]:
    """Check whether all dependencies for a ticket are satisfied.

    Returns (all_satisfied, list_of_unsatisfied) where each entry is:
    ``{"ticket_id": ..., "title": ..., "status": ..., "reason": ...}``.
    A dependency is satisfied when the upstream has a most-recent run with
    ``exit_status='success'`` and the upstream is in ``review`` or ``archived``.
    """
    deps = list_dependencies(session, ticket_id)
    unsatisfied: list[dict] = []
    for upstream in deps:
        satisfied, reason = _is_dependency_satisfied(session, upstream)
        if not satisfied:
            unsatisfied.append({
                "ticket_id": upstream.id,
                "title": upstream.title,
                "status": upstream.status,
                "reason": reason,
            })
    return (len(unsatisfied) == 0), unsatisfied


def _is_dependency_satisfied(session: Session, upstream: Ticket) -> tuple[bool, str]:
    """Check if a single upstream dependency is satisfied.

    Satisfied when the upstream's ACTIVE CONVERSATION latest turn succeeded and
    the upstream is in review/archived. (Previously this read the latest run by
    started_at; it now reads through current_conversation_id to that
    conversation's last turn by position — the conversation model's source of
    truth for "the latest turn".)
    """
    if upstream.status in ("review", "archived"):
        from nightdesk.domain.conversations import active_conversation, latest_turn
        conv = active_conversation(session, upstream)
        latest = latest_turn(session, conv.id if conv else None)
        if latest is None:
            return False, "upstream has no runs"
        if latest.exit_status == "success":
            return True, ""
        return False, f"upstream last run exited with '{latest.exit_status}'"
    if upstream.status == "running":
        return False, "upstream is still running"
    if upstream.status == "queued":
        return False, "upstream is queued (has not run yet)"
    if upstream.status == "draft":
        return False, "upstream is in draft (has not been queued)"
    return False, f"upstream status is '{upstream.status}'"


# --- Focused metadata updates -------------------------------------------------
# Thin helpers for the property picker, list inline edits, keyboard actions,
# and bulk operations.  Each function touches exactly one field and returns
# the updated Ticket.  They deliberately avoid the full ``update_ticket``
# code path (which handles workspaces, toolchains, etc.) to keep the
# interaction lightweight and avoid unintended side-effects.


def update_ticket_priority(session: Session, ticket_id: str,
                           priority: int) -> Ticket:
    """Set the priority on a ticket using the fixed 0..4 metadata scale."""
    priority = validate_priority(priority)
    t = get_ticket(session, ticket_id)
    t.priority = priority
    t.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(t)
    return t


def update_ticket_project(session: Session, ticket_id: str,
                          project_id: Optional[str]) -> Ticket:
    """Set or clear the project assignment on a ticket.  Validates that the
    project exists when setting a non-null value."""
    if project_id is not None:
        get_project(session, project_id)
    t = get_ticket(session, ticket_id)
    t.project_id = project_id
    t.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(t)
    return t


def update_ticket_profile(session: Session, ticket_id: str,
                          profile_id: str) -> Ticket:
    """Reassign a ticket to a different profile.  Validates that the profile
    exists."""
    from nightdesk.domain.profiles import get_profile
    get_profile(session, profile_id)
    t = get_ticket(session, ticket_id)
    t.profile_id = profile_id
    t.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(t)
    return t


# --- Bulk metadata updates ----------------------------------------------------

def bulk_update_priority(
    session: Session,
    ticket_ids: list[str],
    priority: int,
) -> tuple[list[Ticket], list[dict]]:
    """Bulk priority update.  Returns ``(updated, skipped)`` where each
    skipped entry is ``{"ticket_id": ..., "reason": ...}``."""
    priority = validate_priority(priority)
    updated: list[Ticket] = []
    skipped: list[dict] = []
    for tid in ticket_ids:
        try:
            t = update_ticket_priority(session, tid, priority)
            updated.append(t)
        except TicketNotFound:
            skipped.append({"ticket_id": tid, "reason": "not found"})
    return updated, skipped


def bulk_update_status(
    session: Session,
    ticket_ids: list[str],
    new_status: str,
) -> tuple[list[Ticket], list[dict]]:
    """Bulk status transition.  Each ticket is transitioned independently;
    tickets that cannot transition are skipped rather than failing the whole
    batch.  Returns ``(updated, skipped)``."""
    if new_status not in _ALL_STATUSES:
        raise InvalidTransition(f"unknown status {new_status!r}")
    updated: list[Ticket] = []
    skipped: list[dict] = []
    for tid in ticket_ids:
        try:
            t = transition_status(session, tid, new_status)
            updated.append(t)
        except TicketNotFound:
            skipped.append({"ticket_id": tid, "reason": "not found"})
        except InvalidTransition as exc:
            skipped.append({"ticket_id": tid, "reason": str(exc)})
    return updated, skipped


def bulk_update_project(
    session: Session,
    ticket_ids: list[str],
    project_id: Optional[str],
) -> tuple[list[Ticket], list[dict]]:
    """Bulk project assignment.  Validates the project once, then applies to
    each ticket.  Returns ``(updated, skipped)``."""
    if project_id is not None:
        get_project(session, project_id)
    updated: list[Ticket] = []
    skipped: list[dict] = []
    for tid in ticket_ids:
        try:
            t = get_ticket(session, tid)
            t.project_id = project_id
            t.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(t)
            updated.append(t)
        except TicketNotFound:
            skipped.append({"ticket_id": tid, "reason": "not found"})
    return updated, skipped


def bulk_update_profile(
    session: Session,
    ticket_ids: list[str],
    profile_id: str,
) -> tuple[list[Ticket], list[dict]]:
    """Bulk profile reassignment.  Validates the profile once, then applies to
    each ticket.  Returns ``(updated, skipped)``."""
    from nightdesk.domain.profiles import get_profile
    get_profile(session, profile_id)
    updated: list[Ticket] = []
    skipped: list[dict] = []
    for tid in ticket_ids:
        try:
            t = get_ticket(session, tid)
            t.profile_id = profile_id
            t.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(t)
            updated.append(t)
        except TicketNotFound:
            skipped.append({"ticket_id": tid, "reason": "not found"})
    return updated, skipped


def bulk_archive(
    session: Session,
    ticket_ids: list[str],
) -> tuple[list[Ticket], list[dict]]:
    """Bulk archive.  Archives each ticket that is in ``draft``, ``queued``, or
    ``review`` (see ``_ARCHIVABLE_SOURCES``); every other ticket (e.g.
    ``running``, already ``archived``) is skipped with a reason rather than
    failing the whole batch.  Returns ``(updated, skipped)`` where each
    skipped entry is ``{"ticket_id": ..., "reason": ...}``."""
    updated: list[Ticket] = []
    skipped: list[dict] = []
    for tid in ticket_ids:
        try:
            t = archive(session, tid)
            updated.append(t)
        except TicketNotFound:
            skipped.append({"ticket_id": tid, "reason": "not found"})
        except InvalidTransition as exc:
            skipped.append({"ticket_id": tid, "reason": str(exc)})
    return updated, skipped


def bulk_unarchive(
    session: Session,
    ticket_ids: list[str],
) -> tuple[list[Ticket], list[dict]]:
    """Bulk unarchive.  Returns each archived ticket to ``queued`` (the
    supported reverse of archiving) and is the undo path for ``bulk_archive``.
    Tickets that are not archived are skipped.  Returns ``(updated, skipped)``."""
    updated: list[Ticket] = []
    skipped: list[dict] = []
    for tid in ticket_ids:
        try:
            t = unarchive(session, tid)
            updated.append(t)
        except TicketNotFound:
            skipped.append({"ticket_id": tid, "reason": "not found"})
        except InvalidTransition as exc:
            skipped.append({"ticket_id": tid, "reason": str(exc)})
    return updated, skipped


# --- internals ---------------------------------------------------------------


def _next_position(session: Session, status: str) -> int:
    cur = session.scalar(
        select(func.coalesce(func.max(Ticket.position), -1)).where(Ticket.status == status)
    )
    return (cur or 0) + 1 if cur is not None and cur >= 0 else 0


def _reorder_inserting(
    session: Session,
    ticket: Ticket,
    new_status: str,
    position: Optional[int],
) -> None:
    """Move ``ticket`` to ``new_status`` at ``position`` (or append if None),
    rewriting positions in both the source and destination columns.
    """
    old_status = ticket.status
    same_column = old_status == new_status

    # Pull the destination column's existing tickets (excluding the moving one).
    dest = [
        t for t in session.scalars(
            select(Ticket)
            .where(Ticket.status == new_status)
            .order_by(Ticket.position.asc(), Ticket.priority.desc(), Ticket.created_at.asc())
        )
        if t.id != ticket.id
    ]

    if position is None or position < 0 or position > len(dest):
        position = len(dest)

    ticket.status = new_status
    dest.insert(position, ticket)
    for idx, t in enumerate(dest):
        t.position = idx

    if not same_column:
        # Re-pack the source column.
        src = list(
            session.scalars(
                select(Ticket)
                .where(Ticket.status == old_status)
                .order_by(Ticket.position.asc(), Ticket.priority.desc(), Ticket.created_at.asc())
            )
        )
        for idx, t in enumerate(src):
            t.position = idx
