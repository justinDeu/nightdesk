"""Worker-level mid-run steering: the run-completion drain + auto-continue.

Queue-only backend path (claude_sdk, no live watcher): a run finishes with
messages still queued, so run_one folds them into next_run_context and, when the
conversation is resumable, auto-issues a continue (queued + run_now) instead of
transitioning to review. A non-resumable conversation falls through to review
with the drained text staged as the "Guidance staged" chip.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from nightdesk.db.models import Ticket
from nightdesk.domain.conversations import create_conversation
from nightdesk.domain.steering import add_steer_message, list_steer_messages
from nightdesk.domain.tickets import create_ticket, transition_status
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult
from nightdesk.worker.run_one import RunOneConfig, run_one


class _SuccessExecutor:
    async def run(self, req: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(exit_status="success", final_summary="done")


def _running_ticket_with_conversation(session, sample_profile, tmp_path, *, session_id):
    primary = tmp_path / "primary"
    primary.mkdir(exist_ok=True)
    ticket = create_ticket(
        session, title="steer me", prompt="do it", status="queued", priority=0,
        profile_id=sample_profile.id, source_path=str(primary),
    )
    transition_status(session, ticket.id, "running")
    conv = create_conversation(
        session, ticket_id=ticket.id, profile_id=ticket.profile_id,
        backend="claude_sdk",
        transcript_path=str(tmp_path / "transcripts" / "c.log"),
    )
    conv.session_id = session_id
    ticket.current_conversation_id = conv.id
    # Stage the conversation so run_one runs THIS conversation (and reads the
    # steer messages seeded on it) rather than creating a fresh one.
    ticket.permission_overrides = {
        "nightdesk_run_intent": "continue",
        "nightdesk_conversation_id": conv.id,
    }
    session.commit()
    return ticket, conv


def _cfg(tmp_path):
    return RunOneConfig(
        worktree_root=tmp_path / "work",
        transcript_root=tmp_path / "transcripts",
        secrets={},
        host="testhost",
        executor=_SuccessExecutor(),
    )


@pytest.mark.anyio
async def test_drain_auto_continues_when_resumable(session, sample_profile, tmp_path):
    ticket, conv = _running_ticket_with_conversation(
        session, sample_profile, tmp_path, session_id="sess-resumable",
    )
    add_steer_message(session, conversation_id=conv.id, ticket_id=ticket.id, body="do X next")
    add_steer_message(session, conversation_id=conv.id, ticket_id=ticket.id, body="and Y")
    tid = ticket.id
    cid = conv.id

    result = await run_one(lambda: session, _cfg(tmp_path), tid)
    assert result.exit_status == "success"

    session.expire_all()
    t = session.get(Ticket, tid)
    # Auto-continue: re-queued for its next turn, not parked in review.
    assert t.status == "queued"
    assert t.run_now is True
    # The queued follow-ups were folded into next_run_context (the continue's msg).
    assert "do X next" in (t.next_run_context or "")
    assert "and Y" in (t.next_run_context or "")
    # And the staged intent is a continue on the same conversation.
    assert t.permission_overrides.get("nightdesk_run_intent") == "continue"
    assert t.permission_overrides.get("nightdesk_conversation_id") == cid
    # No live queue rows remain (drained -> cancelled).
    assert list_steer_messages(session, cid) == []


@pytest.mark.anyio
async def test_drain_falls_through_to_review_when_not_resumable(session, sample_profile, tmp_path):
    ticket, conv = _running_ticket_with_conversation(
        session, sample_profile, tmp_path, session_id=None,
    )
    add_steer_message(session, conversation_id=conv.id, ticket_id=ticket.id, body="please also Z")
    tid = ticket.id
    cid = conv.id

    result = await run_one(lambda: session, _cfg(tmp_path), tid)
    assert result.exit_status == "success"

    session.expire_all()
    t = session.get(Ticket, tid)
    # Non-resumable: normal review landing, but the drained text is staged so the
    # user sees the "Guidance staged" chip and one click runs it.
    assert t.status == "review"
    assert "please also Z" in (t.next_run_context or "")
    assert list_steer_messages(session, cid) == []


@pytest.mark.anyio
async def test_no_steer_messages_is_a_plain_review(session, sample_profile, tmp_path):
    ticket, _conv = _running_ticket_with_conversation(
        session, sample_profile, tmp_path, session_id="sess-resumable",
    )
    tid = ticket.id
    result = await run_one(lambda: session, _cfg(tmp_path), tid)
    assert result.exit_status == "success"
    session.expire_all()
    t = session.get(Ticket, tid)
    # With nothing queued the drain is a no-op: ordinary review landing.
    assert t.status == "review"
    assert t.run_now is False
