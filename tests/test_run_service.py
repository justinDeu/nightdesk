from datetime import datetime, timezone

from nightdesk.domain.runs import start_run, finish_run, list_runs, get_run
from nightdesk.domain.tickets import create_ticket


def test_start_and_finish_run(session, sample_profile):
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=sample_profile.id,
                       source_path="/tmp", run_now=False)
    r = start_run(session, ticket_id=t.id, worktree_path="/w", transcript_path="/w/log",
                   pid=1234, host="h")
    assert r.id
    assert r.exit_status is None

    finished = finish_run(session, r.id, exit_status="success", error_summary=None)
    assert finished.finished_at is not None
    assert finished.exit_status == "success"


def test_list_runs_filters_by_ticket(session, sample_profile):
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=sample_profile.id,
                       source_path="/tmp", run_now=False)
    r = start_run(session, ticket_id=t.id, worktree_path="/w", transcript_path="/w/log",
                   pid=1, host="h")
    runs = list_runs(session, ticket_id=t.id)
    assert [x.id for x in runs] == [r.id]


def test_start_run_records_intent_and_parent(session, sample_profile):
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=sample_profile.id,
                       source_path="/tmp", run_now=False)
    first = start_run(session, ticket_id=t.id, worktree_path="/w1", transcript_path="/w/1.log",
                      pid=1, host="h")
    second = start_run(
        session,
        ticket_id=t.id,
        worktree_path="/w2",
        transcript_path="/w/2.log",
        pid=2,
        host="h",
        intent="retry",
        parent_run_id=first.id,
        headless_policy_version="v1",
        restart_workspace_policy="fresh_path",
    )
    assert second.intent == "retry"
    assert second.parent_run_id == first.id
    assert second.restart_workspace_policy == "fresh_path"
