"""SessionHost loop integration with an in-process FakeResidentBackend.

Covers resident-agents-v3.md §17 host-loop bullets: turns -> transcript + done +
usage slicing; idle reap -> idle + resume published; needs-input round trip
(row written, turn parked, answer delivered, resumed, row answered); reap-gating
on open pending; restart-runtime -> same session id, env re-merged, replay cost
on the first post-restart turn.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nightdesk.db.models import Session as SessionModel, SessionTurn
from nightdesk.db.session import session_factory
from nightdesk.domain import sessions as sess
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.transcript import read_events
from nightdesk.worker.resident_backends import StartSpec
from nightdesk.worker.session_host import SessionHost


pytestmark = pytest.mark.anyio

BOX = ProfileSecretBox("t")


class FakeHandle:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._q: asyncio.Queue = asyncio.Queue()
        self.pid = 123456
        self.closed = False

    async def send(self, obj: dict) -> None:
        self.sent.append(obj)

    async def events(self):
        while True:
            evt = await self._q.get()
            if evt is None:
                return
            yield evt

    async def close(self) -> None:
        self.closed = True
        self._q.put_nowait(None)

    def push(self, evt: dict) -> None:
        self._q.put_nowait(evt)


class FakeOpencodeHandle(FakeHandle):
    """Like ``FakeHandle`` but also carries ``session_id`` (the shape
    ``_OpencodeResidentHandle`` has and ``_SubprocHandle``/claude's
    ``FakeHandle`` does not) so teardown can recover it when a turn ends
    without a ``turn_complete`` event."""

    def __init__(self) -> None:
        super().__init__()
        self.session_id: str | None = None


class FakeOpencodeBackend:
    def __init__(self) -> None:
        self.starts: list[tuple[StartSpec, str]] = []
        self.handles: list[FakeOpencodeHandle] = []

    async def start(self, spec: StartSpec, posture: str) -> FakeOpencodeHandle:
        self.starts.append((spec, posture))
        h = FakeOpencodeHandle()
        self.handles.append(h)
        return h


class FakeBackend:
    def __init__(self) -> None:
        self.starts: list[tuple[StartSpec, str]] = []
        self.handles: list[FakeHandle] = []

    async def start(self, spec: StartSpec, posture: str) -> FakeHandle:
        self.starts.append((spec, posture))
        h = FakeHandle()
        self.handles.append(h)
        return h


async def _await(cond, timeout=3.0, interval=0.01):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return False


def _make(engine, tmp_path, **over):
    factory = session_factory(engine)
    with factory() as db:
        row = sess.create_session(
            db, profile_id="p",
            source_path=str(tmp_path / "src"),
            transcript_root=str(tmp_path / "tr"),
            box=BOX, **over,
        )
        (tmp_path / "src").mkdir(exist_ok=True)
        return factory, row.id, Path(row.transcript_path)


async def _run_host(factory, sid, backend, **kw):
    host = SessionHost(session_factory=factory, session_id=sid, backend=backend,
                       box=BOX, host="test", poll_interval=0.01, **kw)
    task = asyncio.create_task(host.run())
    return host, task


async def test_turn_streams_transcript_and_slices_usage(engine, tmp_path):
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=3600)
    with factory() as db:
        sess.post_message(db, sid, "hello")
    backend = FakeBackend()
    host, task = await _run_host(factory, sid, backend)
    try:
        # Host boots and delivers the user turn to the inner.
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        h = backend.handles[0]
        h.push({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi there"}]}})
        h.push({"type": "turn_complete", "turn_id": _first_turn_id(factory, sid),
                "session_id": "cc-sess-1",
                "usage": {"input_tokens": 100, "output_tokens": 40}, "cost_usd": 0.5})

        assert await _await(lambda: _turn_status(factory, sid) == "done")
        with factory() as db:
            turn = db.query(SessionTurn).filter_by(session_id=sid).first()
            assert turn.input_tokens == 100 and turn.output_tokens == 40
            assert turn.cost_usd == 0.5 and not turn.includes_resume
            row = db.get(SessionModel, sid)
            assert row.input_tokens == 100 and row.cost_usd == 0.5
            assert (row.resume_handle or {}).get("session_id") == "cc-sess-1"
        events = list(read_events(tpath))
        types = [e["type"] for e in events]
        assert "session_booting" in types and "assistant_text" in types
        # The user's message is persisted before the assistant output so the
        # transcript reads in conversation order.
        assert types.index("user_message") < types.index("assistant_text")
        um = next(e for e in events if e["type"] == "user_message")
        assert um["text"] == "hello"
        assert um["turn_id"] == _first_turn_id(factory, sid)
    finally:
        await _end(factory, sid, task)


async def test_idle_reap_publishes_resume(engine, tmp_path):
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=0)
    with factory() as db:
        sess.post_message(db, sid, "hi")
    backend = FakeBackend()
    host, task = await _run_host(factory, sid, backend)
    try:
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        backend.handles[0].push({"type": "turn_complete",
                                 "turn_id": _first_turn_id(factory, sid),
                                 "session_id": "cc-9", "usage": {}, "cost_usd": 0.0})
        # idle_timeout_s=0 -> reaps once the turn finishes and idle > 0.
        assert await asyncio.wait_for(task, timeout=5.0) == 0
        with factory() as db:
            row = db.get(SessionModel, sid)
            assert row.status == "idle" and row.host_pid is None
            assert (row.resume_handle or {}).get("session_id") == "cc-9"
        assert backend.handles[0].closed
    finally:
        if not task.done():
            task.cancel()


async def test_needs_input_round_trip(engine, tmp_path):
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=3600)
    with factory() as db:
        sess.post_message(db, sid, "make a plan")
    backend = FakeBackend()
    host, task = await _run_host(factory, sid, backend)
    try:
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        h = backend.handles[0]
        # Inner parks: emits pending_input mid-turn.
        h.push({"type": "pending_input", "request_id": "req-1",
                "kind": "plan_approval", "tool": "ExitPlanMode",
                "payload": {"plan": "the plan"}, "options": []})
        assert await _await(lambda: sess_open(factory, sid) is not None)
        # Human answers via the API path -> durable answer turn.
        with factory() as db:
            sess.answer_pending(db, sid, "req-1", decision="approve", answer="go")
        # Host delivers the answer to the inner while the turn is parked.
        assert await _await(lambda: any(m.get("type") == "answer"
                                        and m.get("request_id") == "req-1"
                                        for m in h.sent))
        # Inner resolves + completes the turn.
        h.push({"type": "pending_resolved", "request_id": "req-1"})
        h.push({"type": "turn_complete", "turn_id": _first_turn_id(factory, sid),
                "session_id": "cc-1", "usage": {}, "cost_usd": 0.0})
        assert await _await(lambda: sess_open(factory, sid) is None)
        with factory() as db:
            p = db.query(sess.PendingInput).filter_by(session_id=sid).first()
            assert p.status == "answered"
            assert _turn_status(factory, sid) == "done"
    finally:
        await _end(factory, sid, task)


async def test_reap_gated_by_open_pending(engine, tmp_path):
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=0)
    with factory() as db:
        sess.post_message(db, sid, "plan")
    backend = FakeBackend()
    host, task = await _run_host(factory, sid, backend)
    try:
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        backend.handles[0].push({"type": "pending_input", "request_id": "r1",
                                 "kind": "permission", "tool": "Bash",
                                 "payload": {}, "options": []})
        assert await _await(lambda: sess_open(factory, sid) is not None)
        # Despite idle_timeout_s=0, the open pending pins the agent: it must NOT
        # reap while the human is the blocker.
        await asyncio.sleep(0.2)
        assert not task.done()
        with factory() as db:
            assert db.get(SessionModel, sid).status == "active"
    finally:
        await _end(factory, sid, task)


async def test_restart_reuses_session_id_and_folds_replay_cost(engine, tmp_path):
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=3600)
    with factory() as db:
        sess.put_env(db, sid, {"TOK": {"value": "s", "secret": True}}, BOX)
        sess.post_message(db, sid, "first")
    backend = FakeBackend()
    host, task = await _run_host(factory, sid, backend)
    try:
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        backend.handles[0].push({"type": "turn_complete",
                                 "turn_id": _first_turn_id(factory, sid),
                                 "session_id": "cc-keep", "usage": {},
                                 "cost_usd": 0.1})
        assert await _await(lambda: _turn_status(factory, sid) == "done")
        # Request a runtime restart.
        with factory() as db:
            sess.request_restart(db, sid, force=False)
        assert await _await(lambda: len(backend.starts) == 2)
        spec2 = backend.starts[1][0]
        assert spec2.resume == "cc-keep"          # same claude session id
        assert spec2.env == {"TOK": "s"}          # env re-decrypted + merged

        # First post-restart turn folds the resume replay cost.
        with factory() as db:
            sess.post_message(db, sid, "second")
        h2 = backend.handles[1]
        assert await _await(lambda: any(m.get("type") == "user_turn" for m in h2.sent))
        with factory() as db:
            tid = db.query(SessionTurn).filter_by(
                session_id=sid, body="second").first().id
        h2.push({"type": "turn_complete", "turn_id": tid, "session_id": "cc-keep",
                 "usage": {"input_tokens": 500, "output_tokens": 10}, "cost_usd": 1.0})
        assert await _await(lambda: _named_turn(factory, sid, "second") == "done")
        with factory() as db:
            t = db.query(SessionTurn).filter_by(session_id=sid, body="second").first()
            assert t.includes_resume is True
    finally:
        await _end(factory, sid, task)


async def test_explicit_reap_control_turn_idles_host(engine, tmp_path):
    """A reap control turn makes a live host run its normal idle-reap flow now:
    disconnect the inner, publish the resume handle, release to idle."""
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=3600)
    with factory() as db:
        sess.post_message(db, sid, "hi")
    backend = FakeBackend()
    host, task = await _run_host(factory, sid, backend)
    try:
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        backend.handles[0].push({"type": "turn_complete",
                                 "turn_id": _first_turn_id(factory, sid),
                                 "session_id": "cc-reap", "usage": {}, "cost_usd": 0.0})
        assert await _await(lambda: _turn_status(factory, sid) == "done")
        # Explicit reap while warm.
        with factory() as db:
            sess.request_reap(db, sid)
        assert await asyncio.wait_for(task, timeout=5.0) == 0
        with factory() as db:
            row = db.get(SessionModel, sid)
            assert row.status == "idle" and row.host_pid is None
            assert (row.resume_handle or {}).get("session_id") == "cc-reap"
        assert backend.handles[0].closed
    finally:
        if not task.done():
            task.cancel()


async def test_max_turn_seconds_watchdog_fails_unresponsive_turn(engine, tmp_path):
    """A turn that never completes: the watchdog interrupts it, and when the
    inner stays silent past the grace window the host tears down (crashed ->
    idle) with the turn failed — the agent stays recoverable."""
    from nightdesk.db.models import ConfigRow
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=3600)
    with factory() as db:
        db.add(ConfigRow(id=1, worktree_root="/w", transcript_root="/t",
                         max_turn_seconds=1))
        sess.post_message(db, sid, "runaway")
    backend = FakeBackend()
    # Tight grace so the test doesn't wait the production default.
    host = SessionHost(session_factory=factory, session_id=sid, backend=backend,
                       box=BOX, host="test", poll_interval=0.01, watchdog_grace=0.1)
    task = asyncio.create_task(host.run())
    try:
        # The inner receives the turn but never pushes turn_complete.
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        # Watchdog fires interrupt (>1s), then tears down after the grace.
        assert await asyncio.wait_for(task, timeout=8.0) == 0
        h = backend.handles[0]
        assert any(m.get("type") == "interrupt" for m in h.sent)
        assert h.closed
        with factory() as db:
            row = db.get(SessionModel, sid)
            assert row.status == "idle" and row.host_pid is None  # recoverable
            turn = db.query(SessionTurn).filter_by(session_id=sid).first()
            assert turn.status == "failed"
        assert "session_crashed" in [e["type"] for e in read_events(tpath)]
    finally:
        if not task.done():
            task.cancel()


async def test_turn_complete_with_error_marks_turn_failed(engine, tmp_path):
    """Bug 6: a turn_complete carrying an ``error`` (opencode still reports
    session.idle after a provider error, e.g. ProviderAuthError on a
    misconfigured endpoint) must not read as a successful "done" — the host
    marks the turn failed and records the error."""
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=3600)
    with factory() as db:
        sess.post_message(db, sid, "hello")
    backend = FakeBackend()
    host, task = await _run_host(factory, sid, backend)
    try:
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        backend.handles[0].push({
            "type": "turn_complete", "turn_id": _first_turn_id(factory, sid),
            "session_id": "sess-1", "usage": {}, "cost_usd": 0.0,
            "error": "OpenAI API key is missing",
        })
        assert await _await(lambda: _turn_status(factory, sid) == "failed")
        with factory() as db:
            turn = db.query(SessionTurn).filter_by(session_id=sid).first()
            assert turn.error == "OpenAI API key is missing"
            # The (possibly stale) resume handle from a prior generation is
            # still published even when the turn failed.
            row = db.get(SessionModel, sid)
            assert (row.resume_handle or {}).get("session_id") == "sess-1"
    finally:
        await _end(factory, sid, task)


async def test_teardown_recovers_session_id_from_handle_when_turn_interrupted(
        engine, tmp_path):
    """Bug 4: a turn torn down without ``turn_complete`` (shutdown mid-turn)
    never reaches ``_slice_usage`` (the only other place ``resume_handle`` is
    published), so on an opencode-style handle that already knows its inner
    session id, teardown must recover it from the live handle rather than
    leaving ``resume_handle`` at whatever the DB already had (None on a
    session's first generation)."""
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=3600)
    with factory() as db:
        sess.post_message(db, sid, "hi")
    backend = FakeOpencodeBackend()
    host, task = await _run_host(factory, sid, backend)
    try:
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        # The inner has already created its session (mirrors
        # ``_OpencodeResidentHandle._ensure_session``) but never reports
        # turn_complete before the shutdown races it.
        backend.handles[0].session_id = "ses_recovered"
        with factory() as db:
            row = db.get(SessionModel, sid)
            assert row.resume_handle in (None, {})
        with factory() as db:
            sess.end_session(db, sid)
        await asyncio.wait_for(task, timeout=5.0)
        with factory() as db:
            row = db.get(SessionModel, sid)
            assert (row.resume_handle or {}).get("session_id") == "ses_recovered"
            turn = db.query(SessionTurn).filter_by(session_id=sid).first()
            assert turn.status == "interrupted"
        assert backend.handles[0].closed
    finally:
        if not task.done():
            task.cancel()


async def test_teardown_leaves_resume_handle_unchanged_without_handle_session_id(
        engine, tmp_path):
    """The claude path's handle has no ``session_id`` attribute at all
    (its session id only ever arrives via a ``turn_complete`` event) —
    teardown's ``getattr`` returns None and ``resume_handle`` is left exactly
    as the DB already had it (still None here, since the turn never
    completed)."""
    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=3600)
    with factory() as db:
        sess.post_message(db, sid, "hi")
    backend = FakeBackend()
    host, task = await _run_host(factory, sid, backend)
    try:
        assert await _await(lambda: backend.handles
                            and any(m.get("type") == "user_turn"
                                    for m in backend.handles[0].sent))
        assert not hasattr(backend.handles[0], "session_id")
        with factory() as db:
            sess.end_session(db, sid)
        await asyncio.wait_for(task, timeout=5.0)
        with factory() as db:
            row = db.get(SessionModel, sid)
            assert row.resume_handle in (None, {})
            turn = db.query(SessionTurn).filter_by(session_id=sid).first()
            assert turn.status == "interrupted"
        assert backend.handles[0].closed
    finally:
        if not task.done():
            task.cancel()


async def test_rewake_does_not_reimport_live_streamed_history(
        engine, tmp_path, monkeypatch):
    """A resumed-generation boot must not replay already-transcribed history.

    Turns streamed live by the resident land in BOTH the transcript and the
    CLI's session jsonl. The wake-time delta-import exists only for terminal
    round-trips made while the agent is cold; it must not re-emit the live
    turns (the watermark advances as turns complete). Lines appended while
    cold ARE still imported exactly once.
    """
    from claude_agent_sdk import project_key_for_directory
    from nightdesk.transcript import read_events as _read

    factory, sid, tpath = _make(engine, tmp_path, idle_timeout_s=3600)
    cc_root = tmp_path / "cc-projects"
    monkeypatch.setenv("NIGHTDESK_CC_PROJECTS_DIR", str(cc_root))
    slug = project_key_for_directory(str(tmp_path / "src"))
    jsonl = cc_root / slug / "cc-live-1.jsonl"
    jsonl.parent.mkdir(parents=True)

    def _append(entry):
        import json as _json
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(entry) + "\n")

    # --- generation 1: one live turn ------------------------------------
    with factory() as db:
        sess.post_message(db, sid, "hello")
    backend = FakeBackend()
    host, task = await _run_host(factory, sid, backend)
    assert await _await(lambda: backend.handles
                        and any(m.get("type") == "user_turn"
                                for m in backend.handles[0].sent))
    h = backend.handles[0]
    h.push({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi there"}]}})
    # The CLI persists the same turn to its own jsonl as it streams it.
    _append({"type": "user", "message": {"role": "user", "content": "hello"}})
    _append({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi there"}]}})
    h.push({"type": "turn_complete", "turn_id": _first_turn_id(factory, sid),
            "session_id": "cc-live-1", "usage": {}, "cost_usd": 0.0})
    assert await _await(lambda: _turn_status(factory, sid) == "done")
    with factory() as db:
        handle = db.get(SessionModel, sid).resume_handle or {}
        assert handle.get("session_id") == "cc-live-1"
        assert handle.get("imported_lines") == 2  # watermark past the live turn
    with factory() as db:
        sess.request_reap(db, sid)
    assert await asyncio.wait_for(task, timeout=5.0) == 0

    def _texts():
        return [e.get("text") for e in _read(tpath) if e["type"] == "assistant_text"]

    assert _texts() == ["hi there"]

    # --- generation 2: cold wake, nothing happened while cold -----------
    backend2 = FakeBackend()
    host2, task2 = await _run_host(factory, sid, backend2)
    assert await _await(lambda: len(backend2.starts) == 1)
    assert backend2.starts[0][0].resume == "cc-live-1"
    await asyncio.sleep(0.1)  # let any (wrong) delta-import land
    assert _texts() == ["hi there"]  # history NOT re-emitted
    with factory() as db:
        sess.request_reap(db, sid)
    assert await asyncio.wait_for(task2, timeout=5.0) == 0

    # --- generation 3: a terminal round-trip happened while cold --------
    _append({"type": "user", "message": {"role": "user", "content": "psst"}})
    _append({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "terminal reply"}]}})
    backend3 = FakeBackend()
    host3, task3 = await _run_host(factory, sid, backend3)
    assert await _await(lambda: len(backend3.starts) == 1)
    assert await _await(lambda: _texts() == ["hi there", "terminal reply"])
    with factory() as db:
        assert (db.get(SessionModel, sid).resume_handle or {}).get(
            "imported_lines") == 4
    with factory() as db:
        sess.request_reap(db, sid)
    assert await asyncio.wait_for(task3, timeout=5.0) == 0
    # Still exactly once after the reap-time watermark refresh.
    assert _texts() == ["hi there", "terminal reply"]


# ---- helpers --------------------------------------------------------------
def _first_turn_id(factory, sid):
    with factory() as db:
        return db.query(SessionTurn).filter_by(session_id=sid).order_by(
            SessionTurn.position.asc()).first().id


def _turn_status(factory, sid):
    with factory() as db:
        t = db.query(SessionTurn).filter_by(session_id=sid).order_by(
            SessionTurn.position.asc()).first()
        return t.status if t else None


def _named_turn(factory, sid, body):
    with factory() as db:
        t = db.query(SessionTurn).filter_by(session_id=sid, body=body).first()
        return t.status if t else None


def sess_open(factory, sid):
    with factory() as db:
        return sess.open_pending(db, sid)


async def _end(factory, sid, task):
    with factory() as db:
        sess.end_session(db, sid)
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        if not task.done():
            task.cancel()
