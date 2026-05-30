import pytest

from tests.conftest import workspace_payload

from nightdesk.domain.tickets import (
    clone_ticket, create_ticket, get_ticket, list_tickets, merge_next_run_context_into_prompt,
    request_run_now, requeue, resume_ticket, restart_ticket, retry_ticket, set_next_run_context,
    set_run_now, transition_status, transition_with_position, reorder_in_column,
    archive, unarchive, update_ticket, delete_ticket, TicketNotFound, InvalidTransition,
)


def make_ticket(session, sample_profile, **kw):
    fields = dict(title="t", prompt="p", priority=0,
                   profile_id=sample_profile.id, workspaces=workspace_payload(), run_now=False)
    fields.update(kw)
    return create_ticket(session, **fields)


def test_create_ticket_starts_draft(session, sample_profile):
    t = make_ticket(session, sample_profile)
    assert t.status == "draft"


def test_create_ticket_respects_explicit_status(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    assert t.status == "queued"


def test_list_filters_by_status(session, sample_profile):
    a = make_ticket(session, sample_profile, title="a", status="queued")
    b = make_ticket(session, sample_profile, title="b", status="queued")
    transition_status(session, b.id, "running")
    queued = list_tickets(session, status="queued")
    running = list_tickets(session, status="running")
    assert {x.id for x in queued} == {a.id}
    assert {x.id for x in running} == {b.id}


def test_transition_status_valid_v2_lifecycle(session, sample_profile):
    t = make_ticket(session, sample_profile)  # draft
    transition_status(session, t.id, "queued")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    transition_status(session, t.id, "queued")
    assert get_ticket(session, t.id).status == "queued"


def test_transition_status_invalid_raises(session, sample_profile):
    t = make_ticket(session, sample_profile)
    with pytest.raises(InvalidTransition):
        transition_status(session, t.id, "review")


def test_drop_to_running_from_draft_sets_run_now(session, sample_profile):
    t = make_ticket(session, sample_profile)  # draft
    transition_with_position(session, t.id, "running")
    refreshed = get_ticket(session, t.id)
    assert refreshed.status == "running"
    assert refreshed.run_now is True


def test_drop_to_running_from_queued_sets_run_now(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    transition_with_position(session, t.id, "running")
    assert get_ticket(session, t.id).run_now is True


def test_set_run_now(session, sample_profile):
    t = make_ticket(session, sample_profile)
    set_run_now(session, t.id, True)
    assert get_ticket(session, t.id).run_now is True


def test_request_run_now_from_draft_queues_and_flags(session, sample_profile):
    """Draft -> queued AND run_now=true in one shot. Setting only the flag
    would leave the ticket parked forever (scheduler filters by queued)."""
    t = make_ticket(session, sample_profile)  # draft
    out = request_run_now(session, t.id)
    assert out.status == "queued"
    assert out.run_now is True
    # Persisted, not just in-memory.
    refreshed = get_ticket(session, t.id)
    assert refreshed.status == "queued"
    assert refreshed.run_now is True


def test_request_run_now_from_queued_only_flips_flag(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    out = request_run_now(session, t.id)
    assert out.status == "queued"
    assert out.run_now is True


def test_request_run_now_from_review_queues_and_flags(session, sample_profile):
    """Review -> queued is allowed by the lifecycle and Run-now should take
    advantage of it for a one-click re-run."""
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    out = request_run_now(session, t.id)
    assert out.status == "queued"
    assert out.run_now is True


def test_request_run_now_from_archived_queues_and_flags(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    archive(session, t.id)
    out = request_run_now(session, t.id)
    assert out.status == "queued"
    assert out.run_now is True


def test_request_run_now_running_rejected(session, sample_profile):
    """Don't restart a live run because someone clicked Run-now twice."""
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    with pytest.raises(InvalidTransition):
        request_run_now(session, t.id)


def test_requeue_from_review(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    requeue(session, t.id)
    assert get_ticket(session, t.id).status == "queued"


def test_requeue_from_archived(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    archive(session, t.id)
    requeue(session, t.id)
    assert get_ticket(session, t.id).status == "queued"


def test_requeue_rejects_draft(session, sample_profile):
    t = make_ticket(session, sample_profile)
    with pytest.raises(InvalidTransition):
        requeue(session, t.id)


def test_archive_only_from_review(session, sample_profile):
    t = make_ticket(session, sample_profile)
    with pytest.raises(InvalidTransition):
        archive(session, t.id)


def test_unarchive_only_from_archived(session, sample_profile):
    t = make_ticket(session, sample_profile)
    with pytest.raises(InvalidTransition):
        unarchive(session, t.id)


def test_set_next_run_context(session, sample_profile):
    t = make_ticket(session, sample_profile, status="review")
    out = set_next_run_context(session, t.id, "Use polling")
    assert out.next_run_context == "Use polling"
    assert out.next_run_context_updated_at is not None


def test_merge_next_run_context_into_prompt(session, sample_profile):
    t = make_ticket(session, sample_profile, prompt="base prompt", status="review")
    set_next_run_context(session, t.id, "Use polling")
    out = merge_next_run_context_into_prompt(session, t.id)
    assert out.prompt == "base prompt\n\nUse polling"
    assert out.next_run_context is None


def test_resume_ticket_queues_run_with_transient_context(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    out = resume_ticket(session, t.id, next_run_context="Use polling, not SSE")
    assert out.status == "queued"
    assert out.run_now is True
    assert out.next_run_context == "Use polling, not SSE"
    assert out.permission_overrides["nightdesk_run_intent"] == "resume"


def test_retry_ticket_queues_run_with_transient_context(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    out = retry_ticket(session, t.id, next_run_context="Retry with polling")
    assert out.status == "queued"
    assert out.run_now is True
    assert out.next_run_context == "Retry with polling"
    assert out.permission_overrides["nightdesk_run_intent"] == "retry"


def test_restart_ticket_requires_workspace_policy(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    with pytest.raises(ValueError, match="restart workspace policy is required"):
        restart_ticket(session, t.id, next_run_context=None, workspace_policy=None)


def test_restart_ticket_stages_workspace_policy(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    out = restart_ticket(session, t.id, next_run_context="Start fresh", workspace_policy="fresh_path")
    assert out.status == "queued"
    assert out.permission_overrides["nightdesk_run_intent"] == "restart"
    assert out.permission_overrides["nightdesk_restart_workspace_policy"] == "fresh_path"


def test_clone_ticket_copies_prompt_and_context(session, sample_profile):
    t = make_ticket(session, sample_profile, title="orig", prompt="base prompt", status="review")
    set_next_run_context(session, t.id, "Use polling")
    out = clone_ticket(session, t.id, title=None, carry_context=True)
    assert out.title == "orig (clone)"
    assert out.prompt == "base prompt\n\nUse polling"
    assert out.status == "draft"


def test_delete_ticket(session, sample_profile):
    t = make_ticket(session, sample_profile)
    delete_ticket(session, t.id)
    with pytest.raises(TicketNotFound):
        get_ticket(session, t.id)


def test_delete_running_ticket_forbidden(session, sample_profile):
    t = make_ticket(session, sample_profile, status="queued")
    transition_status(session, t.id, "running")
    with pytest.raises(InvalidTransition):
        delete_ticket(session, t.id)


def test_create_ticket_assigns_position(session, sample_profile):
    a = make_ticket(session, sample_profile, status="queued")
    b = make_ticket(session, sample_profile, status="queued")
    c = make_ticket(session, sample_profile, status="queued")
    positions = [t.position for t in (a, b, c)]
    assert positions == sorted(positions), positions
    assert len(set(positions)) == 3


def test_reorder_in_column_rewrites_positions(session, sample_profile):
    a = make_ticket(session, sample_profile, title="a", status="queued")
    b = make_ticket(session, sample_profile, title="b", status="queued")
    c = make_ticket(session, sample_profile, title="c", status="queued")
    reorder_in_column(session, "queued", [c.id, a.id, b.id])
    listed = list_tickets(session, status="queued")
    assert [t.id for t in listed] == [c.id, a.id, b.id]
    assert [t.position for t in listed] == [0, 1, 2]


def test_transition_with_position_inserts_at_index(session, sample_profile):
    a = make_ticket(session, sample_profile, title="a", status="queued")
    b = make_ticket(session, sample_profile, title="b", status="queued")
    c = make_ticket(session, sample_profile, title="c", status="queued")
    # Move 'c' from queued to draft at position 0.
    transition_with_position(session, c.id, "draft", position=0)
    drafts = list_tickets(session, status="draft")
    assert [t.id for t in drafts] == [c.id]
    queued = list_tickets(session, status="queued")
    # remaining queued tickets have positions repacked
    assert [t.id for t in queued] == [a.id, b.id]
    assert [t.position for t in queued] == [0, 1]


def test_additional_dirs_default_empty(session, sample_profile):
    t = make_ticket(session, sample_profile)
    assert t.additional_dirs == []


def test_additional_dirs_persisted(session, sample_profile):
    t = make_ticket(session, sample_profile,
                     additional_dirs=[{"path": "/srv/x", "mode": "rw"}])
    refreshed = get_ticket(session, t.id)
    assert refreshed.additional_dirs == [{"path": "/srv/x", "mode": "rw"}]


def test_create_ticket_requires_primary_workspace(session, sample_profile):
    with pytest.raises(ValueError, match="workspaces must include exactly one primary workspace"):
        create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=sample_profile.id, run_now=False)


def test_create_ticket_rejects_missing_primary_source_path(session, sample_profile):
    with pytest.raises(ValueError, match="primary workspace source_path is required"):
        create_ticket(
            session,
            title="t",
            prompt="p",
            priority=0,
            profile_id=sample_profile.id,
            run_now=False,
            workspaces=[{
                "role": "primary",
                "label": "primary",
                "kind": "directory",
                "access": "read_write",
            }],
        )
