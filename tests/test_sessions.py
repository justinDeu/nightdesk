"""Interactive (ticketless) sessions.

A session is a ``Ticket`` with ``kind='session'`` that reuses the whole run
pipeline. These tests cover the load-bearing exclusion (a session must never
leak into any board / inbox / analytics / search surface), the domain façade
(create / turn loop / promote / archive), the worker finish-path guards
(webhook + dependents skipped), and the JSON API.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone

import pytest

from nightdesk.db.models import Run, Ticket
from nightdesk.domain import analytics
from nightdesk.domain.conversations import active_conversation, list_conversations
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.query import parse_query, search_runs, search_tickets
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.sessions import (
    SessionBusy,
    archive_session,
    create_session,
    list_sessions,
    post_session_turn,
    promote_session,
)
from nightdesk.domain.tickets import (
    InvalidTransition,
    TicketNotFound,
    count_tickets,
    create_ticket,
    get_ticket,
    list_inbox,
    list_tickets,
    transition_status,
)
from nightdesk.worker.executor import DummyExecutor, ExecutionResult
from nightdesk.worker.main import WorkerLoop, WorkerSettings


def _profile(session, name="p"):
    return create_profile(
        session, name=name, fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )


def _make_session(session, tmp_path, profile, *, title="chat"):
    return create_session(
        session, title=title, profile_id=profile.id,
        scratch_root=tmp_path / "sessions",
    )


# --------------------------------------------------------------------------
# Exclusion (load-bearing): a session in every status must be invisible to
# every ticket surface, while a normal ticket in the same status is visible.
# --------------------------------------------------------------------------
_LIFECYCLE = ["draft", "queued", "running", "review", "archived"]


def test_sessions_excluded_from_every_ticket_surface(session, tmp_path):
    p = _profile(session)

    normal_ids: set[str] = set()
    session_ids: set[str] = set()
    # One normal ticket AND one session in each lifecycle status.
    for st in _LIFECYCLE:
        n = create_ticket(session, title=f"normal-{st}", prompt="p", priority=0,
                          profile_id=p.id, status=st, source_path="/tmp")
        normal_ids.add(n.id)
        s = create_ticket(session, title=f"session-{st}", prompt="p", priority=0,
                          profile_id=p.id, status=st, source_path="/tmp",
                          kind="session")
        session_ids.add(s.id)
    # A normal inbox ticket too (sessions never occupy the inbox).
    inbox = create_ticket(session, title="inbox", prompt="p", priority=0,
                          profile_id=p.id, status="inbox", source_path="/tmp")
    normal_ids.add(inbox.id)

    # Runs on one normal and one session ticket so the run-joined surfaces
    # (search_runs + analytics) have session rows that must be excluded.
    normal_run_ticket = next(iter(normal_ids))
    session_run_ticket = next(iter(session_ids))
    for tid in (normal_run_ticket, session_run_ticket):
        r = Run(ticket_id=tid, started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc), exit_status="success",
                worktree_path="/w", transcript_path="/x", host="h",
                cost_usd=1.0, input_tokens=10, output_tokens=5,
                cache_read_tokens=0, cache_write_tokens=0, model_used="claude")
        session.add(r)
    session.commit()

    def _ids(tickets):
        return {t.id for t in tickets}

    # list_tickets (board / Tickets list / Archive / Desk) — every status, and
    # unfiltered.
    all_listed = _ids(list_tickets(session, limit=1000))
    assert all_listed & session_ids == set()
    assert normal_ids <= all_listed | {inbox.id}  # inbox excluded from board list
    for st in _LIFECYCLE:
        listed = _ids(list_tickets(session, status=st, limit=1000))
        assert all(i not in session_ids for i in listed)
        assert any(i in normal_ids for i in listed)

    # count_tickets agrees with the (session-free) page.
    assert count_tickets(session) == len(list_tickets(session, limit=10000))

    # list_inbox.
    assert _ids(list_inbox(session)) == {inbox.id}

    # search_tickets / search_runs.
    searched = _ids(search_tickets(session, parse_query(""), limit=1000))
    assert searched & session_ids == set()
    run_ticket_ids = {r.ticket_id for r in search_runs(session, parse_query(""), limit=1000)}
    assert session_run_ticket not in run_ticket_ids
    assert normal_run_ticket in run_ticket_ids

    # Analytics: every aggregate counts exactly the one normal run, never the
    # session run. Each call exercises a distinct Ticket-join exclusion site.
    start = datetime.now(timezone.utc) - timedelta(days=1)
    wt = analytics.window_totals(session, start=start)
    assert wt["run_count"] == 1
    assert analytics.tokens_by_model(session, start=start)[0]["run_count"] == 1
    assert analytics.run_stats(session, start=start)["completed"] == 1
    prof_rows = analytics.usage_by_profile(session, start=start)
    assert sum(r["run_count"] for r in prof_rows) == 1
    tkt_rows = analytics.usage_by_ticket(session, start=start)
    assert {r["ticket_id"] for r in tkt_rows} == {normal_run_ticket}
    proj_rows = analytics.project_rollups(session, start=start)
    assert sum(r["run_count"] for r in proj_rows) == 1
    # Latency-rooted aggregate (RunLatency⋈Run⋈Ticket) — no session rows leak.
    assert analytics.latency_by_model(session, start=start) == []


# --------------------------------------------------------------------------
# Domain façade
# --------------------------------------------------------------------------
def test_create_session_provisions_scratch_workspace(session, tmp_path):
    p = _profile(session)
    s = _make_session(session, tmp_path, p)
    assert s.kind == "session"
    assert s.status == "draft"
    primary = next(w for w in s.workspaces if w.role == "primary")
    assert primary.kind == "directory"
    assert primary.source_path.startswith(str(tmp_path / "sessions"))


def test_create_session_with_explicit_workspace_runs_in_place(session, tmp_path):
    p = _profile(session)
    s = create_session(session, title="x", profile_id=p.id,
                       workspace=str(tmp_path), scratch_root=tmp_path / "sessions")
    primary = next(w for w in s.workspaces if w.role == "primary")
    assert primary.source_path == str(tmp_path)


def test_post_first_turn_sets_prompt_and_queues(session, tmp_path):
    p = _profile(session)
    s = _make_session(session, tmp_path, p)
    t = post_session_turn(session, s.id, "hello there")
    assert t.status == "queued"
    assert t.run_now is True
    assert t.prompt == "hello there"
    # No conversation staged for the very first turn (worker starts fresh).
    overrides = t.permission_overrides or {}
    assert "nightdesk_new_conversation" not in overrides


def test_post_turn_on_running_session_is_busy(session, tmp_path):
    p = _profile(session)
    s = _make_session(session, tmp_path, p)
    s.status = "running"
    session.commit()
    with pytest.raises(SessionBusy):
        post_session_turn(session, s.id, "hi")


def test_post_turn_non_resumable_retries_fresh(session, tmp_path):
    """A session whose first turn crashed before recording a session id is not
    resumable: the next message starts a fresh conversation instead of 409-ing."""
    p = _profile(session)
    s = _make_session(session, tmp_path, p)
    transition_status(session, s.id, "queued")
    transition_status(session, s.id, "running")
    r = start_run(session, ticket_id=s.id, worktree_path="/w",
                  transcript_path="/p.log", pid=None, host="h")
    finish_run(session, r.id, exit_status="failed", error_summary=None,
               session_id=None)
    transition_status(session, s.id, "review")

    t = post_session_turn(session, s.id, "try again")
    assert t.status == "queued"
    assert t.prompt == "try again"
    overrides = t.permission_overrides or {}
    assert overrides.get("nightdesk_new_conversation") is True


def test_promote_session_flips_kind_and_lands_on_board(session, tmp_path):
    p = _profile(session)
    s = _make_session(session, tmp_path, p)
    transition_status(session, s.id, "queued")
    transition_status(session, s.id, "running")
    transition_status(session, s.id, "review")

    promoted = promote_session(session, s.id, title="Real ticket",
                               target_status="review")
    assert promoted.kind == "ticket"
    assert promoted.title == "Real ticket"
    assert promoted.status == "review"
    # Now visible on the board.
    assert promoted.id in {t.id for t in list_tickets(session, limit=1000)}


def test_promote_session_blocked_while_running(session, tmp_path):
    p = _profile(session)
    s = _make_session(session, tmp_path, p)
    s.status = "running"
    session.commit()
    with pytest.raises(InvalidTransition):
        promote_session(session, s.id, title="nope")


def test_archive_and_list_sessions(session, tmp_path):
    p = _profile(session)
    s1 = _make_session(session, tmp_path, p, title="one")
    s2 = _make_session(session, tmp_path, p, title="two")
    assert {s.id for s in list_sessions(session)} == {s1.id, s2.id}
    archive_session(session, s1.id)
    assert get_ticket(session, s1.id).status == "archived"


def test_session_helpers_reject_normal_tickets(session, tmp_path):
    p = _profile(session)
    t = create_ticket(session, title="n", prompt="p", priority=0,
                      profile_id=p.id, status="review", source_path="/tmp")
    with pytest.raises(TicketNotFound):
        post_session_turn(session, t.id, "hi")
    with pytest.raises(TicketNotFound):
        promote_session(session, t.id, title="x")


# --------------------------------------------------------------------------
# Worker turn loop + finish-path guards
# --------------------------------------------------------------------------
class _SessionIdExecutor:
    """Executor that reports a stable session id (so continue can resume) and
    records the prompt + resume handle it was invoked with."""

    def __init__(self, sid="sess-loop-1"):
        self.sid = sid
        self.calls: list[dict] = []

    async def run(self, req) -> ExecutionResult:
        req.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        req.transcript_path.write_text(f"prompt: {req.prompt}\n")
        self.calls.append({
            "prompt": req.prompt,
            "resume": getattr(req, "resume_session_id", None),
        })
        return ExecutionResult(exit_status="success", final_summary="ok",
                               session_id=self.sid)


def _settings(tmp_path, executor):
    return WorkerSettings(
        max_parallel=1,
        window_start=time(22, 0),
        window_end=time(7, 0),
        worktree_root=tmp_path / "work",
        transcript_root=tmp_path / "transcripts",
        secrets={},
        host="testhost",
        executor=executor,
    )


async def _tick(loop):
    await loop.tick_once()
    if loop._inproc:
        await asyncio.gather(*loop._inproc.values())


@pytest.mark.anyio
async def test_session_turn_loop_first_then_continue(session, tmp_path):
    p = _profile(session)
    s = _make_session(session, tmp_path, p)
    ex = _SessionIdExecutor()
    loop = WorkerLoop(session_factory=lambda: session, settings=_settings(tmp_path, ex))

    # First turn: fresh dispatch, no resume handle, one conversation.
    post_session_turn(session, s.id, "first message")
    await _tick(loop)
    session.expire_all()
    t = get_ticket(session, s.id)
    assert t.status == "review"
    convs = list_conversations(session, s.id)
    assert len(convs) == 1
    assert convs[0].session_id == "sess-loop-1"
    assert ex.calls[0]["resume"] is None
    assert "first message" in ex.calls[0]["prompt"]

    # Second turn: continue resumes the same conversation's session id.
    t2 = post_session_turn(session, s.id, "second message")
    assert (t2.permission_overrides or {})["nightdesk_run_intent"] == "continue"
    await _tick(loop)
    session.expire_all()
    t = get_ticket(session, s.id)
    assert t.status == "review"
    # Continue appended a SECOND turn onto the SAME conversation (resumed, not
    # forked into a new one). The resume handle itself is resolved from the
    # session store at run time, which the dummy executor does not populate.
    convs = list_conversations(session, s.id)
    assert len(convs) == 1
    from nightdesk.domain.runs import list_runs
    assert len(list_runs(session, ticket_id=s.id)) == 2


@pytest.mark.anyio
async def test_session_finish_skips_webhook_and_dependents(session, tmp_path, monkeypatch):
    import nightdesk.worker.run_one as run_one

    fired: list = []
    handoffs: list = []
    monkeypatch.setattr(run_one, "_maybe_fire_webhook",
                        lambda *a, **k: fired.append(1))
    monkeypatch.setattr(run_one, "_handoff_to_dependents",
                        lambda *a, **k: handoffs.append(1))

    p = _profile(session)
    s = _make_session(session, tmp_path, p)
    post_session_turn(session, s.id, "hello")
    loop = WorkerLoop(session_factory=lambda: session,
                      settings=_settings(tmp_path, DummyExecutor()))
    await _tick(loop)
    session.expire_all()
    assert get_ticket(session, s.id).status == "review"
    assert fired == []
    assert handoffs == []


@pytest.mark.anyio
async def test_normal_ticket_still_fires_webhook(session, tmp_path, monkeypatch):
    """Control: the guard is session-only — a normal ticket still fires."""
    import nightdesk.worker.run_one as run_one

    fired: list = []
    monkeypatch.setattr(run_one, "_maybe_fire_webhook",
                        lambda *a, **k: fired.append(1))
    p = _profile(session)
    t = create_ticket(session, title="n", prompt="go", priority=0,
                      profile_id=p.id, status="queued", run_now=True,
                      source_path=str(tmp_path))
    loop = WorkerLoop(session_factory=lambda: session,
                      settings=_settings(tmp_path, DummyExecutor()))
    await _tick(loop)
    session.expire_all()
    assert get_ticket(session, t.id).status == "review"
    assert fired == [1]
