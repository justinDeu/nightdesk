from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nightdesk.db.models import Ticket, TicketDependency, TicketWorkspace
from nightdesk.domain.projects import apply_project_defaults, get_project


# v2 lifecycle. Run-level outcomes (success/failed/cancelled) now live on Run.exit_status.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "draft":    {"queued", "running"},   # running implies run-now
    "queued":   {"draft", "running"},
    "running":  {"review"},
    "review":   {"queued", "archived"},
    "archived": {"queued"},
}

_ALL_STATUSES = ("draft", "queued", "running", "review", "archived")


class TicketNotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


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




def _stage_next_run(ticket: Ticket, *, intent: str,
                    workspace_policy: Optional[str] = None) -> None:
    overrides = dict(ticket.permission_overrides or {})
    overrides["nightdesk_run_intent"] = intent
    overrides["nightdesk_parent_run_id"] = ticket.current_run_id
    if workspace_policy is None:
        overrides.pop("nightdesk_restart_workspace_policy", None)
    else:
        overrides["nightdesk_restart_workspace_policy"] = workspace_policy
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



def create_ticket(session: Session, **fields) -> Ticket:
    """Create a ticket. Defaults to status='draft' (v2)."""
    fields = apply_project_defaults(session, fields)
    workspace_specs = fields.pop("workspaces", None)
    worktree_name = fields.pop("worktree_name", None)
    worktree_path = fields.pop("worktree_path", None)
    fields.setdefault("status", "draft")
    fields.setdefault("additional_dirs", [])
    status = fields.get("status")
    if status not in _ALL_STATUSES:
        raise InvalidTransition(f"unknown status {status!r}")

    if workspace_specs:
        normalized = [_workspace_dict(w) for w in workspace_specs]
        normalized = _validate_workspace_specs(normalized)
        primary = next(w for w in normalized if w.get("role") == "primary")
        if not fields.get("cwd") and primary.get("source_path"):
            fields["cwd"] = primary["source_path"]
        fields["workspace_mode"] = primary["kind"]
        workspace_specs = normalized
    elif worktree_name or worktree_path:
        workspace_specs = [{
            "role": "primary",
            "label": "primary",
            "kind": _normalize_workspace_kind(fields.get("workspace_mode", "directory")),
            "access": "read_write",
            "source_path": fields.get("cwd"),
            "worktree_name": worktree_name,
            "worktree_path": worktree_path,
            "retention": "preserve",
        }]

    cwd = fields.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("cwd is required")
    fields["workspace_mode"] = _normalize_workspace_kind(
        fields.get("workspace_mode", "directory")
    )
    # Position defaults to end of the chosen column.
    if "position" not in fields:
        fields["position"] = _next_position(session, status)
    t = Ticket(**fields)
    session.add(t)
    session.flush()
    if workspace_specs:
        _apply_workspaces(session, t, workspace_specs)
    session.commit()
    session.refresh(t)
    return t


def get_ticket(session: Session, ticket_id: str) -> Ticket:
    t = session.get(Ticket, ticket_id)
    if t is None:
        raise TicketNotFound(ticket_id)
    return t


def list_tickets(
    session: Session,
    status: Optional[str] = None,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 200,
) -> list[Ticket]:
    stmt = (
        select(Ticket)
        .order_by(Ticket.position.asc(), Ticket.priority.desc(), Ticket.created_at.asc())
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    if profile_id is not None:
        stmt = stmt.where(Ticket.profile_id == profile_id)
    if project_id is not None:
        if project_id == "null":
            stmt = stmt.where(Ticket.project_id.is_(None))
        else:
            stmt = stmt.where(Ticket.project_id == project_id)
    return list(session.scalars(stmt))


def update_ticket(session: Session, ticket_id: str, **fields) -> Ticket:
    workspace_specs = fields.pop("workspaces", None)
    worktree_name = fields.pop("worktree_name", None)
    worktree_path = fields.pop("worktree_path", None)
    t = get_ticket(session, ticket_id)
    if "cwd" in fields:
        cwd = fields["cwd"]
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError("cwd cannot be empty")
    if "workspace_mode" in fields:
        fields["workspace_mode"] = _normalize_workspace_kind(fields["workspace_mode"])
    if fields.get("project_id") is not None:
        get_project(session, fields["project_id"])
    for k, v in fields.items():
        setattr(t, k, v)
    if workspace_specs is not None:
        _apply_workspaces(session, t, workspace_specs)
    elif t.workspaces:
        primary = next((w for w in t.workspaces if w.role == "primary"), None)
        if primary is not None:
            if any(k in fields for k in ("cwd", "workspace_mode")):
                primary.source_path = t.cwd
                primary.kind = _normalize_workspace_kind(t.workspace_mode)
            if worktree_name is not None:
                primary.worktree_name = worktree_name
            if worktree_path is not None:
                primary.worktree_path = worktree_path
    elif worktree_name or worktree_path:
        _apply_workspaces(session, t, [{
            "role": "primary",
            "label": "primary",
            "kind": _normalize_workspace_kind(t.workspace_mode),
            "access": "read_write",
            "source_path": t.cwd,
            "worktree_name": worktree_name,
            "worktree_path": worktree_path,
            "retention": "preserve",
        }])
    session.commit()
    session.refresh(t)
    return t


def transition_status(session: Session, ticket_id: str, new_status: str) -> Ticket:
    """Plain transition; appends to the bottom of the new column."""
    return transition_with_position(session, ticket_id, new_status, position=None)


def transition_with_position(
    session: Session,
    ticket_id: str,
    new_status: str,
    position: Optional[int] = None,
) -> Ticket:
    """Transition a ticket to ``new_status``, optionally placing it at ``position``.

    Drop-to-running from draft/queued sets ``run_now=True`` BEFORE the scheduler
    picks the ticket (so the next pick records ``started_as_run_now=true``).
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

    # Drop-to-running from draft/queued flips run_now=true so the very next
    # scheduler tick treats this as a forced pick.
    if new_status == "running" and t.status in ("draft", "queued"):
        t.run_now = True

    _reorder_inserting(session, t, new_status, position)
    t.updated_at = datetime.now(timezone.utc)
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


def archive(session: Session, ticket_id: str) -> Ticket:
    """Convenience: review -> archived."""
    t = get_ticket(session, ticket_id)
    if t.status != "review":
        raise InvalidTransition(f"cannot archive from {t.status}")
    return transition_status(session, ticket_id, "archived")


def unarchive(session: Session, ticket_id: str) -> Ticket:
    """Convenience: archived -> queued."""
    t = get_ticket(session, ticket_id)
    if t.status != "archived":
        raise InvalidTransition(f"cannot unarchive from {t.status}")
    return transition_status(session, ticket_id, "queued")


def set_run_now(session: Session, ticket_id: str, run_now: bool) -> Ticket:
    return update_ticket(session, ticket_id, run_now=run_now)


def request_run_now(session: Session, ticket_id: str) -> Ticket:
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
        return transition_with_position(session, ticket_id, "queued", position=None)
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


def resume_ticket(session: Session, ticket_id: str, *, next_run_context: Optional[str]) -> Ticket:
    t = set_next_run_context(session, ticket_id, next_run_context) if next_run_context is not None else get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot resume from {t.status}")
    _stage_next_run(t, intent="resume")
    session.commit()
    session.refresh(t)
    return request_run_now(session, ticket_id)


def retry_ticket(session: Session, ticket_id: str, *, next_run_context: Optional[str]) -> Ticket:
    t = set_next_run_context(session, ticket_id, next_run_context) if next_run_context is not None else get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot retry from {t.status}")
    _stage_next_run(t, intent="retry")
    session.commit()
    session.refresh(t)
    return request_run_now(session, ticket_id)


def restart_ticket(session: Session, ticket_id: str, *, next_run_context: Optional[str],
                   workspace_policy: Optional[str]) -> Ticket:
    if workspace_policy not in ("recreate_in_place", "fresh_path"):
        raise ValueError("restart workspace policy is required")
    t = set_next_run_context(session, ticket_id, next_run_context) if next_run_context is not None else get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot restart from {t.status}")
    _stage_next_run(t, intent="restart", workspace_policy=workspace_policy)
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
        additional_dirs=list(t.additional_dirs or []),
        cwd=t.cwd,
        workspace_mode=t.workspace_mode,
        project_id=t.project_id,
        workspaces=workspace_specs,
    )

def requeue(session: Session, ticket_id: str) -> Ticket:
    """Move a ticket back to queued. Allowed from review or archived."""
    t = get_ticket(session, ticket_id)
    if t.status not in ("review", "archived"):
        raise InvalidTransition(f"cannot requeue from {t.status}")
    return transition_status(session, ticket_id, "queued")


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
    """Check if a single upstream dependency is satisfied."""
    if upstream.status in ("review", "archived"):
        # Check the most recent run.
        from nightdesk.domain.runs import list_runs
        runs = list_runs(session, ticket_id=upstream.id)
        if not runs:
            return False, "upstream has no runs"
        latest = runs[0]  # list_runs orders by started_at DESC
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
