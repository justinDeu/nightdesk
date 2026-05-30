# tests/test_heartbeat.py
from datetime import datetime, timezone

import pytest

from nightdesk.db.models import Ticket, Run, WorkerHeartbeat
from nightdesk.domain.tickets import create_ticket
from nightdesk.domain.runs import start_run
from nightdesk.worker import heartbeat as hb_module
from nightdesk.worker.heartbeat import write_heartbeat, recover_orphaned_runs


@pytest.fixture
def force_dead_pids(monkeypatch):
    """Pretend every recorded pid is dead so orphan recovery proceeds.

    Real liveness is checked via os.kill(pid, 0); on some systems the
    pids the test uses (1, 999, ...) are actually live, which would
    cause recovery to skip them. The 'subprocess crashed' bookkeeping
    is what we're asserting here, not real-process detection.
    """
    monkeypatch.setattr(hb_module, "_pid_alive", lambda _pid: False)


def test_write_heartbeat_upserts(session):
    write_heartbeat(session, host="h", pid=1)
    write_heartbeat(session, host="h", pid=2)
    hb = session.get(WorkerHeartbeat, 1)
    assert hb.pid == 2


def test_recover_orphaned_runs(session, sample_profile, force_dead_pids):
    t = create_ticket(session, title="t", prompt="", priority=0,
                       profile_id=sample_profile.id, source_path="/tmp", run_now=False)
    t.status = "running"
    session.commit()
    r = start_run(session, ticket_id=t.id, worktree_path="/w",
                   transcript_path="/w/log", pid=999, host="h")
    recover_orphaned_runs(session, host="h")
    session.refresh(t)
    session.refresh(r)
    assert t.status == "review"
    assert r.exit_status == "worker_crash"
    assert r.finished_at is not None


def test_recover_orphaned_runs_scoped_to_host(session, sample_profile, force_dead_pids):
    """Recovery only touches runs (and tickets) belonging to the specified host."""
    from nightdesk.db.models import Profile

    # Create a second profile so each ticket can use a valid profile_id.
    p2 = Profile(
        name="p2", fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(p2)
    session.commit()

    t1 = create_ticket(session, title="h1-ticket", prompt="", priority=0,
                        profile_id=sample_profile.id, source_path="/tmp", run_now=False)
    t1.status = "running"
    t2 = create_ticket(session, title="h2-ticket", prompt="", priority=0,
                        profile_id=p2.id, source_path="/tmp", run_now=False)
    t2.status = "running"
    session.commit()

    r1 = start_run(session, ticket_id=t1.id, worktree_path="/w1",
                    transcript_path="/w1/log", pid=1, host="h1")
    r2 = start_run(session, ticket_id=t2.id, worktree_path="/w2",
                    transcript_path="/w2/log", pid=2, host="h2")

    recover_orphaned_runs(session, host="h1")

    session.refresh(t1)
    session.refresh(t2)
    session.refresh(r1)
    session.refresh(r2)

    # h1 run and ticket should be failed.
    assert r1.exit_status == "worker_crash"
    assert r1.finished_at is not None
    assert t1.status == "review"

    # h2 run and ticket must be untouched.
    assert r2.exit_status is None
    assert r2.finished_at is None
    assert t2.status == "running"
