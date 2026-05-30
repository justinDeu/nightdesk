"""Tests for ticket dependencies: add/remove, cycle detection, scheduler
gate, and context handoff."""

import pytest
from datetime import datetime, time, timezone

from nightdesk.db.models import ScheduleWindow, TicketDependency
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import (
    CyclicDependency,
    DependencyNotFound,
    add_dependency,
    check_dependencies_satisfied,
    create_ticket,
    get_ticket,
    list_dependencies,
    list_dependents,
    remove_dependency,
    transition_status,
)
from nightdesk.worker.scheduler import pick_eligible


def _qt(session, profile, **kw):
    fields = dict(
        title="t", prompt="", priority=0,
        profile_id=profile.id, source_path="/tmp",
        run_now=False, status="draft",
    )
    fields.update(kw)
    return create_ticket(session, **fields)


def _all_days_window(session, *, start="22:00", end="07:00", max_parallel=5):
    """An always-on schedule window so capacity is available for the normal
    dispatch pass (multi-window model). Covers every day; 22:00–07:00 spans
    the 23:00 'now' used in these tests."""
    w = ScheduleWindow(label="test", day_mask=0b1111111, start=start, end=end,
                       max_parallel=max_parallel, position=0)
    session.add(w)
    session.commit()
    return w


def _make_run(session, ticket, *, exit_status="success"):
    """Transition ticket to running, create a run, finish it, transition to review."""
    transition_status(session, ticket.id, "queued")
    transition_status(session, ticket.id, "running")
    run = start_run(
        session,
        ticket_id=ticket.id,
        worktree_path="/tmp/wt",
        transcript_path="/tmp/transcript",
        pid=1234,
        host="test",
    )
    finish_run(session, run.id, exit_status=exit_status, error_summary=None)
    transition_status(session, ticket.id, "review")
    return run


# --- Add / remove / list ---------------------------------------------------


def test_add_dependency(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    deps = list_dependencies(session, b.id)
    assert len(deps) == 1
    assert deps[0].id == a.id


def test_add_dependency_idempotent(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    add_dependency(session, b.id, a.id)
    deps = list_dependencies(session, b.id)
    assert len(deps) == 1


def test_remove_dependency(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    remove_dependency(session, b.id, a.id)
    deps = list_dependencies(session, b.id)
    assert len(deps) == 0


def test_remove_dependency_not_found(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    with pytest.raises(DependencyNotFound):
        remove_dependency(session, b.id, a.id)


def test_list_dependents(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    c = _qt(session, sample_profile, title="C")
    add_dependency(session, b.id, a.id)
    add_dependency(session, c.id, a.id)
    dependents = list_dependents(session, a.id)
    ids = {d.id for d in dependents}
    assert ids == {b.id, c.id}


# --- Cycle detection -------------------------------------------------------


def test_self_dependency_rejected(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    with pytest.raises(CyclicDependency):
        add_dependency(session, a.id, a.id)


def test_direct_cycle_rejected(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    with pytest.raises(CyclicDependency):
        add_dependency(session, a.id, b.id)


def test_transitive_cycle_rejected(session, sample_profile):
    """A -> B -> C; adding C -> A should be rejected."""
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    c = _qt(session, sample_profile, title="C")
    add_dependency(session, b.id, a.id)
    add_dependency(session, c.id, b.id)
    with pytest.raises(CyclicDependency):
        add_dependency(session, a.id, c.id)


# --- Dependency satisfaction ------------------------------------------------


def test_dependency_unsatisfied_when_upstream_draft(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    satisfied, unsatisfied = check_dependencies_satisfied(session, b.id)
    assert not satisfied
    assert len(unsatisfied) == 1
    assert "draft" in unsatisfied[0]["reason"]


def test_dependency_unsatisfied_when_upstream_running(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    transition_status(session, a.id, "queued")
    transition_status(session, a.id, "running")
    satisfied, unsatisfied = check_dependencies_satisfied(session, b.id)
    assert not satisfied
    assert "running" in unsatisfied[0]["reason"]


def test_dependency_satisfied_when_upstream_succeeded(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    _make_run(session, a, exit_status="success")
    satisfied, unsatisfied = check_dependencies_satisfied(session, b.id)
    assert satisfied
    assert len(unsatisfied) == 0


def test_dependency_unsatisfied_when_upstream_failed(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    _make_run(session, a, exit_status="failed")
    satisfied, unsatisfied = check_dependencies_satisfied(session, b.id)
    assert not satisfied
    assert "failed" in unsatisfied[0]["reason"]


def test_dependency_satisfied_when_upstream_archived(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    _make_run(session, a, exit_status="success")
    transition_status(session, a.id, "archived")
    satisfied, _ = check_dependencies_satisfied(session, b.id)
    assert satisfied


# --- Scheduler gate ---------------------------------------------------------


def test_scheduler_skips_ticket_with_unsatisfied_dep(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B", status="queued")
    add_dependency(session, b.id, a.id)
    _all_days_window(session)
    now = datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc)
    picked = pick_eligible(session, now=now, total_running=0)
    ids = [t.id for t in picked]
    assert b.id not in ids


def test_scheduler_picks_ticket_after_dep_satisfied(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B", status="queued")
    add_dependency(session, b.id, a.id)
    _make_run(session, a, exit_status="success")
    _all_days_window(session)
    now = datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc)
    picked = pick_eligible(session, now=now, total_running=0)
    ids = [t.id for t in picked]
    assert b.id in ids


def test_scheduler_skips_when_upstream_failed(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B", status="queued")
    add_dependency(session, b.id, a.id)
    _make_run(session, a, exit_status="failed")
    _all_days_window(session)
    now = datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc)
    picked = pick_eligible(session, now=now, total_running=0)
    ids = [t.id for t in picked]
    assert b.id not in ids


def test_scheduler_run_now_blocked_by_unsatisfied_dep(session, sample_profile):
    """Run-now tickets with unsatisfied deps are still skipped (same gate
    as scheduled_after for consistency)."""
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B", status="queued", run_now=True)
    add_dependency(session, b.id, a.id)
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    picked = pick_eligible(session, now=now, total_running=0)
    ids = [t.id for t in picked]
    assert b.id not in ids


# --- Multi-dependency -------------------------------------------------------


def test_all_deps_must_be_satisfied(session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    c = _qt(session, sample_profile, title="C", status="queued")
    add_dependency(session, c.id, a.id)
    add_dependency(session, c.id, b.id)
    # Only A succeeds.
    _make_run(session, a, exit_status="success")
    satisfied, unsatisfied = check_dependencies_satisfied(session, c.id)
    assert not satisfied
    assert len(unsatisfied) == 1
    # Now B succeeds too.
    _make_run(session, b, exit_status="success")
    satisfied, _ = check_dependencies_satisfied(session, c.id)
    assert satisfied


# --- Deleting upstream ticket -----------------------------------------------


def test_deleting_upstream_removes_dependency(session, sample_profile):
    """CASCADE on delete should remove the dependency row."""
    from nightdesk.domain.tickets import delete_ticket
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    delete_ticket(session, a.id)
    deps = list_dependencies(session, b.id)
    assert len(deps) == 0


def test_deleting_upstream_unblocks_dependent(session, sample_profile):
    """When upstream is deleted, the dependent becomes eligible (no deps)."""
    from nightdesk.domain.tickets import delete_ticket
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B", status="queued")
    add_dependency(session, b.id, a.id)
    delete_ticket(session, a.id)
    satisfied, unsatisfied = check_dependencies_satisfied(session, b.id)
    assert satisfied
    assert len(unsatisfied) == 0


# --- Context handoff --------------------------------------------------------


def test_context_handoff_populates_next_run_context(session, sample_profile):
    """When a ticket finishes successfully, its dependents should receive
    a summary in their next_run_context."""
    from nightdesk.worker.run_one import _handoff_to_dependents

    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)

    _make_run(session, a, exit_status="success")
    a = get_ticket(session, a.id)
    run = a.runs[0]

    _handoff_to_dependents(session, a.id, run)

    b = get_ticket(session, b.id)
    assert b.next_run_context is not None
    assert "Upstream ticket: A" in b.next_run_context
    assert "success" in b.next_run_context


def test_context_handoff_appends_to_existing_context(session, sample_profile):
    """If the dependent already has next_run_context, the handoff appends."""
    from nightdesk.domain.tickets import set_next_run_context
    from nightdesk.worker.run_one import _handoff_to_dependents

    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    set_next_run_context(session, b.id, "Existing context")

    _make_run(session, a, exit_status="success")
    a = get_ticket(session, a.id)
    run = a.runs[0]

    _handoff_to_dependents(session, a.id, run)

    b = get_ticket(session, b.id)
    assert "Existing context" in b.next_run_context
    assert "Upstream ticket: A" in b.next_run_context


# --- API tests --------------------------------------------------------------


@pytest.mark.anyio
async def test_api_add_dependency(client, session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    resp = await client.post(f"/api/v1/tickets/{b.id}/dependencies",
                             json={"depends_on_id": a.id})
    assert resp.status_code == 201
    body = resp.json()
    assert body["depends_on_id"] == a.id
    assert body["depends_on_title"] == "A"


@pytest.mark.anyio
async def test_api_add_dependency_cycle_rejected(client, session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    await client.post(f"/api/v1/tickets/{b.id}/dependencies",
                      json={"depends_on_id": a.id})
    resp = await client.post(f"/api/v1/tickets/{a.id}/dependencies",
                             json={"depends_on_id": b.id})
    assert resp.status_code == 422
    assert "cycle" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_api_list_dependencies(client, session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    await client.post(f"/api/v1/tickets/{b.id}/dependencies",
                      json={"depends_on_id": a.id})
    resp = await client.get(f"/api/v1/tickets/{b.id}/dependencies")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["depends_on_id"] == a.id


@pytest.mark.anyio
async def test_api_remove_dependency(client, session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    await client.post(f"/api/v1/tickets/{b.id}/dependencies",
                      json={"depends_on_id": a.id})
    resp = await client.delete(f"/api/v1/tickets/{b.id}/dependencies/{a.id}")
    assert resp.status_code == 204


@pytest.mark.anyio
async def test_api_ticket_out_includes_dependencies(client, session, sample_profile):
    a = _qt(session, sample_profile, title="A")
    b = _qt(session, sample_profile, title="B")
    add_dependency(session, b.id, a.id)
    resp = await client.get(f"/api/v1/tickets/{b.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "dependencies" in body
    assert len(body["dependencies"]) == 1
    assert body["dependencies"][0]["depends_on_id"] == a.id
