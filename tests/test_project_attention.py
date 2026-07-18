"""Attention rollup: GET /api/v1/projects/attention + domain.attention_rollup.

Per docs/design/project-control-plane.md §Chrome: four attention signals
(review / latest-run-failed / blocked-or-stale-inbox / unacked) sum into the
needs-you badge; running drives the lamp pulse but scores zero; last activity
derives from the latest run/event, never ``Project.updated_at``.
"""
from datetime import datetime, timedelta, timezone

from nightdesk.db.models import Project
from nightdesk.domain.ack import acknowledge_ticket
from nightdesk.domain.events import run_actor
from nightdesk.domain.projects import attention_rollup, create_project
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import (
    create_ticket,
    transition_status,
)

# A ticket only enters review/archived UNACKNOWLEDGED when the worker moves it
# there (a run actor). An admin transition acks it — the human acted on it — so
# the realistic unacked path is always a run-actor transition.
RUN = run_actor(None)


def _project(session, name="P"):
    return create_project(session, name=name, source_path=f"/tmp/{name}")


def _failed_run(session, ticket, *, started_at=None):
    """A finished run that exited non-success (counts as 'failed')."""
    run = start_run(
        session, ticket_id=ticket.id, worktree_path="/tmp/w",
        transcript_path="/tmp/t", pid=1, host="h",
    )
    finish_run(session, run.id, exit_status="error", error_summary="boom")
    if started_at is not None:
        run.started_at = started_at
        run.finished_at = started_at + timedelta(minutes=5)
        session.commit()
    return run


def _success_run(session, ticket, *, started_at=None):
    run = start_run(
        session, ticket_id=ticket.id, worktree_path="/tmp/w",
        transcript_path="/tmp/t", pid=1, host="h",
    )
    finish_run(session, run.id, exit_status="success", error_summary="")
    if started_at is not None:
        run.started_at = started_at
        run.finished_at = started_at + timedelta(minutes=5)
        session.commit()
    return run


def _by_id(rows):
    return {r.id: r for r in rows}


def test_attention_empty_when_no_projects(session):
    assert attention_rollup(session) == []


def test_attention_counts_each_signal(session, sample_profile):
    p = _project(session, "Acme")
    # review counts only as pending work, not acknowledgement debt
    t_review = create_ticket(
        session, title="r", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="draft",
    )
    transition_status(session, t_review.id, "running")
    transition_status(session, t_review.id, "review", actor=RUN)
    # latest run failed on a non-archived ticket
    t_failed = create_ticket(
        session, title="f", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="draft",
    )
    _failed_run(session, t_failed)
    # inbox blocked (no profile -> not promotable)
    create_ticket(session, title="ib", prompt="", project_id=p.id, status="inbox")
    # archived + unacked (settled but unseen)
    t_arch = create_ticket(
        session, title="a", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="draft",
    )
    transition_status(session, t_arch.id, "running")
    transition_status(session, t_arch.id, "review", actor=RUN)
    transition_status(session, t_arch.id, "archived", actor=RUN)

    rows = _by_id(attention_rollup(session))
    row = rows[p.id]
    assert row.review == 1
    assert row.failed == 1
    assert row.inbox_blocked == 1
    assert row.unacked == 1  # only the decided archived ticket
    assert row.needs_you == row.review + row.failed + row.inbox_blocked + row.unacked


def test_running_scores_zero_but_drives_pulse(session, sample_profile):
    p = _project(session, "Runco")
    t = create_ticket(
        session, title="r", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="draft",
    )
    transition_status(session, t.id, "running")

    [row] = attention_rollup(session)
    assert row.running == 1
    assert row.needs_you == 0  # system working != needs you
    assert row.review == 0 and row.failed == 0 and row.inbox_blocked == 0 and row.unacked == 0


def test_inbox_blocked_or_stale_only(session, sample_profile):
    p = _project(session, "Inboxo")
    # blocked: no profile (ticket_completeness non-empty)
    create_ticket(session, title="blocked", prompt="", project_id=p.id, status="inbox")
    # stale: complete but older than 48h
    stale = create_ticket(
        session, title="stale", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="inbox",
    )
    stale.created_at = datetime.now(timezone.utc) - timedelta(hours=49)
    session.commit()
    # healthy: complete and fresh -> not counted
    create_ticket(
        session, title="fresh", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="inbox",
    )

    [row] = attention_rollup(session)
    assert row.inbox_blocked == 2
    assert row.needs_you == 2


def test_failed_excludes_archived_and_uses_latest_run(session, sample_profile):
    p = _project(session, "Faileo")
    # non-archived whose LATEST run succeeded -> not failed
    t_recovered = create_ticket(
        session, title="rec", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="draft",
    )
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _failed_run(session, t_recovered, started_at=base)
    _success_run(session, t_recovered, started_at=base + timedelta(hours=1))
    # archived with a failed latest run -> excluded (settled history)
    t_arch = create_ticket(
        session, title="arch", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="draft",
    )
    _failed_run(session, t_arch)
    transition_status(session, t_arch.id, "running")
    transition_status(session, t_arch.id, "review", actor=RUN)
    transition_status(session, t_arch.id, "archived", actor=RUN)

    [row] = attention_rollup(session)
    assert row.failed == 0
    # archived is decided and unacknowledged
    assert row.unacked == 1


def test_unacked_excludes_acknowledged(session, sample_profile):
    p = _project(session, "Acko")
    t = create_ticket(
        session, title="r", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="draft",
    )
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review", actor=RUN)  # worker -> unacked
    [row] = attention_rollup(session)
    assert row.unacked == 0  # still pending in Needs-you, not acknowledgement debt
    transition_status(session, t.id, "archived", actor=RUN)
    [row] = attention_rollup(session)
    assert row.unacked == 1
    acknowledge_ticket(session, t.id)  # human has seen it

    [row] = attention_rollup(session)
    assert row.unacked == 0
    assert row.review == 0


def test_last_activity_from_run_not_project_updated_at(session, sample_profile):
    p = _project(session, "Liveo")
    # Force the project's updated_at far in the past — it must NOT be the
    # activity signal (the bug this fixes: "1mo ago" on a project that ran 30m ago).
    long_ago = datetime.now(timezone.utc) - timedelta(days=40)
    session.query(Project).filter(Project.id == p.id).update({"updated_at": long_ago})
    session.commit()

    t = create_ticket(
        session, title="r", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p.id, status="draft",
    )
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    run = start_run(
        session, ticket_id=t.id, worktree_path="/tmp/w", transcript_path="/tmp/t",
        pid=1, host="h",
    )
    run.started_at = recent
    run.finished_at = recent + timedelta(minutes=10)
    session.commit()

    [row] = attention_rollup(session)
    assert row.last_activity_at is not None
    # Derived from the run (~30m ago), nowhere near the 40-day-old updated_at.
    delta = datetime.now(timezone.utc) - row.last_activity_at
    assert delta < timedelta(hours=1)


def test_ordering_attention_then_running_then_activity(session, sample_profile):
    # Three projects with distinct profiles.
    p_busy = _project(session, "Busy")      # needs_you highest
    p_run = _project(session, "Running")    # no attention, but running
    p_idle = _project(session, "Idle")      # nothing

    # p_busy: one review ticket (needs_you=1)
    t = create_ticket(
        session, title="r", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p_busy.id, status="draft",
    )
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review", actor=RUN)
    # p_run: one running ticket (needs_you=0, running=1)
    tr = create_ticket(
        session, title="rr", prompt="", profile_id=sample_profile.id,
        source_path="/tmp/x", project_id=p_run.id, status="draft",
    )
    transition_status(session, tr.id, "running")

    order = [r.id for r in attention_rollup(session)]
    assert order[0] == p_busy.id   # attention beats running
    assert order[1] == p_run.id    # running beats idle
    assert order[2] == p_idle.id


async def test_attention_route_returns_rollup(client):
    # No projects yet -> empty list (route is wired and reachable).
    resp = await client.get("/api/v1/projects/attention")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_attention_route_with_data(client):
    # A project + an under-specified inbox ticket (no profile => blocked).
    proj = (await client.post("/api/v1/projects", json={
        "name": "Routeo", "source_path": "/tmp/routeo",
    })).json()
    ticket = (await client.post("/api/v1/tickets", json={
        "title": "needs triage", "project_id": proj["id"], "status": "inbox",
    })).json()
    assert ticket["status"] == "inbox"

    resp = await client.get("/api/v1/projects/attention")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list) and len(data) == 1
    row = data[0]
    assert row["id"] == proj["id"]
    assert row["name"] == "Routeo"
    assert row["slug"] == "routeo"
    assert row["inbox_blocked"] == 1
    assert row["needs_you"] == 1
    # The blocked inbox is fresh; running/review/failed/unacked all zero.
    assert row["running"] == 0 and row["review"] == 0
    assert row["failed"] == 0 and row["unacked"] == 0
    # Schema carries every field, with a sane (run/event-derived) last_activity.
    for k in ("review", "failed", "inbox_blocked", "unacked", "running", "last_activity_at"):
        assert k in row


async def test_attention_route_is_not_shadowed_by_project_id(client):
    """GET /api/v1/projects/attention must hit the rollup, not be captured as
    /{project_id} with id='attention' (route ordering regression guard)."""
    resp = await client.get("/api/v1/projects/attention")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
