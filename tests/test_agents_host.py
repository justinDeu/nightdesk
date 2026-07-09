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
        events = [e["type"] for e in read_events(tpath)]
        assert "session_booting" in events and "assistant_text" in events
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
