"""Resident interactive agents: domain, env, reaper, runner-unit, migration.

Host-loop integration lives in ``test_agents_host.py``; the JSON API in
``test_api_agents.py``. Covers resident-agents-v3.md §17 (minus live-only smoke,
which is ``scripts/smoke_resident_sdk.py``).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from nightdesk.db.models import ConfigRow, PendingInput, Session, SessionTurn
from nightdesk.domain import sessions as sess
from nightdesk.domain.profile_secrets import ProfileSecretBox


BOX = ProfileSecretBox("t")


def _mk(session, **over) -> Session:
    row = sess.create_session(
        session,
        title=over.pop("title", "Agent"),
        profile_id=over.pop("profile_id", None) or "p1",
        source_path=over.pop("source_path", "/tmp/agent"),
        transcript_root=over.pop("transcript_root", "/tmp/tr"),
        box=BOX,
        **over,
    )
    return row


# ---------------------------------------------------------------------------
# CRUD + liveness
# ---------------------------------------------------------------------------
def test_create_defaults_idle_cold(session):
    row = _mk(session)
    assert row.status == "idle"
    assert row.backend == "claude"
    assert row.host_pid is None
    assert sess.describe_liveness(row, session=session) == "cold"


def test_create_scratch_dir_when_no_source(session, tmp_path):
    row = sess.create_session(
        session, profile_id="p", scratch_root=tmp_path, transcript_root=tmp_path,
        box=BOX,
    )
    assert row.source_path.startswith(str(tmp_path))
    import os
    assert os.path.isdir(row.source_path)


def test_liveness_ended_wins(session):
    row = _mk(session)
    row.status = "ended"
    assert sess.describe_liveness(row, session=session) == "ended"


def test_liveness_needs_input(session):
    row = _mk(session)
    row.host_pid = None
    sess.create_pending(session, row.id, request_id="r1", kind="permission",
                        tool="Bash", payload={})
    assert sess.describe_liveness(row, session=session) == "needs-input"


def test_liveness_alive_and_warm(session):
    import os
    row = _mk(session)
    row.host_pid = os.getpid()  # this test process is alive
    session.commit()
    assert sess.describe_liveness(row, session=session) == "warm"
    session.add(SessionTurn(session_id=row.id, position=1, kind="user",
                            body="hi", status="streaming"))
    session.commit()
    assert sess.describe_liveness(row, session=session) == "alive"


def test_delete_blocked_while_live(session):
    import os
    row = _mk(session)
    row.host_pid = os.getpid()
    session.commit()
    with pytest.raises(sess.SessionBusy):
        sess.delete_session(session, row.id)


# ---------------------------------------------------------------------------
# Effective timeout inheritance (live-evaluated)
# ---------------------------------------------------------------------------
def test_effective_timeout_inheritance_and_override(session):
    cfg = ConfigRow(id=1, worktree_root="/w", transcript_root="/t",
                    session_idle_timeout_s=300)
    row = _mk(session)
    assert sess.effective_timeout(row, cfg) == 300
    # Global change reaches an inheriting agent (read live, not frozen).
    cfg.session_idle_timeout_s = 60
    assert sess.effective_timeout(row, cfg) == 60
    # Per-agent override wins and survives global change.
    row.idle_timeout_s = 999
    assert sess.effective_timeout(row, cfg) == 999
    cfg.session_idle_timeout_s = 30
    assert sess.effective_timeout(row, cfg) == 999
    # Clearing the override resumes inheritance.
    row.idle_timeout_s = None
    assert sess.effective_timeout(row, cfg) == 30


# ---------------------------------------------------------------------------
# Env encrypt / merge / mask
# ---------------------------------------------------------------------------
def test_env_encrypt_mask_and_decrypt(session):
    row = _mk(session)
    sess.put_env(session, row.id, {
        "PLAIN": {"value": "v1", "secret": False},
        "TOKEN": {"value": "s3cr3t", "secret": True},
    }, BOX)
    # Stored: secret is a cipher, not the plaintext.
    assert row.env["TOKEN"]["value"] != "s3cr3t"
    # Masked view never returns the secret value.
    masked = {e["key"]: e for e in sess.masked_env(row)}
    assert masked["TOKEN"]["secret"] and "value" not in masked["TOKEN"]
    assert masked["PLAIN"]["value"] == "v1"
    # Decrypt for spawn round-trips.
    env = sess.decrypt_env_for_spawn(row, BOX)
    assert env == {"PLAIN": "v1", "TOKEN": "s3cr3t"}


def test_env_preserves_untouched_cipher(session):
    row = _mk(session)
    sess.put_env(session, row.id, {"TOKEN": {"value": "orig", "secret": True}}, BOX)
    cipher = row.env["TOKEN"]["value"]
    # Re-PUT with value:null keeps the stored cipher (write-only contract).
    sess.put_env(session, row.id, {"TOKEN": {"value": None, "secret": True}}, BOX)
    assert row.env["TOKEN"]["value"] == cipher
    assert sess.decrypt_env_for_spawn(row, BOX)["TOKEN"] == "orig"


def test_env_replace_removes_absent_keys(session):
    row = _mk(session)
    sess.put_env(session, row.id, {"A": {"value": "1", "secret": False},
                                   "B": {"value": "2", "secret": False}}, BOX)
    sess.put_env(session, row.id, {"A": {"value": "1", "secret": False}}, BOX)
    assert list(row.env.keys()) == ["A"]


# ---------------------------------------------------------------------------
# Inbox: enqueue / cap / claim
# ---------------------------------------------------------------------------
def test_queue_cap_enforced_for_user_turns(session):
    session.add(ConfigRow(id=1, worktree_root="/w", transcript_root="/t",
                          max_queued_turns=2))
    session.commit()
    row = _mk(session)
    sess.post_message(session, row.id, "1")
    sess.post_message(session, row.id, "2")
    with pytest.raises(sess.QueueFull):
        sess.post_message(session, row.id, "3")
    # Control turns bypass the cap.
    sess.enqueue_turn(session, row.id, kind="interrupt", enforce_cap=False)


def test_claim_turn_is_atomic(session):
    row = _mk(session)
    turn = sess.post_message(session, row.id, "hi")
    claimed = sess.claim_turn(session, turn.id)
    assert claimed is not None and claimed.status == "delivering"
    # Second claim loses.
    assert sess.claim_turn(session, turn.id) is None


def test_interrupt_requires_something_in_flight(session):
    row = _mk(session)
    with pytest.raises(sess.SessionBusy):
        sess.request_interrupt(session, row.id)
    session.add(SessionTurn(session_id=row.id, position=1, kind="user",
                            body="x", status="streaming"))
    session.commit()
    turn = sess.request_interrupt(session, row.id)
    assert turn.kind == "interrupt"


def test_restart_409_while_streaming_unless_forced(session):
    row = _mk(session)
    session.add(SessionTurn(session_id=row.id, position=1, kind="user",
                            body="x", status="streaming"))
    session.commit()
    with pytest.raises(sess.SessionBusy):
        sess.request_restart(session, row.id, force=False)
    turn = sess.request_restart(session, row.id, force=True)
    assert turn.kind == "restart"


# ---------------------------------------------------------------------------
# Pending answer round trip (durable transport)
# ---------------------------------------------------------------------------
def test_answer_pending_enqueues_answer_turn(session):
    row = _mk(session)
    sess.create_pending(session, row.id, request_id="r1", kind="plan_approval",
                        tool="ExitPlanMode", payload={"plan": "do it"})
    turn = sess.answer_pending(session, row.id, "r1", decision="approve",
                               answer="go")
    assert turn.kind == "answer" and turn.ref_request_id == "r1"
    # The pending row records the human's choice (audit) but stays pending until
    # the host confirms resolution.
    p = sess.open_pending(session, row.id)
    assert p is not None and p.answer["decision"] == "approve"


def test_answer_pending_409_when_resolved(session):
    row = _mk(session)
    sess.create_pending(session, row.id, request_id="r1", kind="permission",
                        tool="Bash", payload={})
    sess.resolve_pending(session, row.id, "r1", status="answered")
    with pytest.raises(sess.PendingNotOpen):
        sess.answer_pending(session, row.id, "r1", decision="allow")


def test_double_pending_emit_rejected_by_unique_index(session):
    row = _mk(session)
    first = sess.create_pending(session, row.id, request_id="r1", kind="permission",
                                tool="Bash", payload={})
    assert first is not None
    # Second open pending for the same agent is rejected (partial unique index).
    second = sess.create_pending(session, row.id, request_id="r2", kind="permission",
                                 tool="Read", payload={})
    assert second is None
    assert len(sess.list_open_pending(session)) == 1


def test_list_open_pending_across_agents(session):
    a = _mk(session)
    b = _mk(session)
    sess.create_pending(session, a.id, request_id="r1", kind="permission",
                        tool="Bash", payload={})
    sess.create_pending(session, b.id, request_id="r2", kind="ask_question",
                        tool="AskUserQuestion", payload={})
    assert len(sess.list_open_pending(session)) == 2


# ---------------------------------------------------------------------------
# Reaper: orphan sweep + LRU eviction
# ---------------------------------------------------------------------------
def test_orphan_sweep_fails_turn_and_cancels_pending(session, tmp_path):
    from nightdesk.worker import session_reaper as reaper
    row = _mk(session, transcript_root=str(tmp_path))
    row.status = "active"
    row.host_pid = 2 ** 31 - 1  # a pid that is not alive
    session.add(SessionTurn(session_id=row.id, position=1, kind="user",
                            body="x", status="streaming"))
    sess.create_pending(session, row.id, request_id="r1", kind="permission",
                        tool="Bash", payload={})
    session.commit()
    swept = reaper.orphan_sweep(session, host="h")
    assert swept == 1
    session.refresh(row)
    assert row.status == "idle" and row.host_pid is None
    turn = session.query(SessionTurn).filter_by(session_id=row.id).first()
    assert turn.status == "failed"
    assert sess.open_pending(session, row.id) is None


def test_over_cap_eviction_is_lru_and_skips_pending(session):
    import os
    from nightdesk.worker import session_reaper as reaper
    session.add(ConfigRow(id=1, worktree_root="/w", transcript_root="/t",
                          max_live_sessions=1))
    now = datetime.now(timezone.utc)
    old = _mk(session)
    new = _mk(session)
    pinned = _mk(session)
    for r in (old, new, pinned):
        r.status = "active"
        r.host_pid = os.getpid()
    old.last_activity_at = now - timedelta(minutes=10)
    new.last_activity_at = now - timedelta(minutes=1)
    pinned.last_activity_at = now - timedelta(minutes=30)
    session.commit()
    # pinned has an open pending -> never evicted despite being the LRU.
    sess.create_pending(session, pinned.id, request_id="r1", kind="permission",
                        tool="Bash", payload={})
    evictions = reaper.over_cap_evictions(session)
    ids = {r.id for r in evictions}
    # cap=1, 3 live, pinned excluded -> evict the 2 evictable, oldest first.
    assert pinned.id not in ids
    assert old.id in ids


# ---------------------------------------------------------------------------
# Runner unit: classify + synthetic-deny interrupt (no SDK)
# ---------------------------------------------------------------------------
def test_runner_classify_tools():
    from nightdesk.worker._session_runner import _classify_tool
    kind, payload, _ = _classify_tool("ExitPlanMode", {"plan": "P"}, [])
    assert kind == "plan_approval" and payload["plan"] == "P"
    kind, payload, _ = _classify_tool("AskUserQuestion", {"questions": [1]}, [])
    assert kind == "ask_question"
    kind, payload, _ = _classify_tool("Bash", {"cmd": "ls"}, ["s"])
    assert kind == "permission" and payload["suggestions"] == ["s"]


@pytest.mark.anyio
async def test_runner_interrupt_synthetic_deny():
    """interrupt() while parked resolves the future with a synthetic deny that
    ends the turn (primary path, §19.1)."""
    from nightdesk.worker._session_runner import _Runner

    emitted = []

    async def emit(evt):
        emitted.append(evt)

    r = _Runner(emit)

    class _Ctx:
        suggestions = []
        title = None

    task = asyncio.create_task(r.can_use_tool("ExitPlanMode", {"plan": "p"}, _Ctx()))
    await asyncio.sleep(0.01)  # let it park
    await r._interrupt()
    result = await task
    # Returned a Deny with interrupt=True; emitted pending_input + resolved.
    assert getattr(result, "behavior", None) == "deny"
    assert getattr(result, "interrupt", False) is True
    types = [e["type"] for e in emitted]
    assert "pending_input" in types and "pending_resolved" in types
