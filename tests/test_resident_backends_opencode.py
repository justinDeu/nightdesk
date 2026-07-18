"""OpencodeResidentBackend: backend selection + a mocked start/turn round-trip.

All httpx / subprocess interaction is faked — no real ``opencode serve`` or
network is involved. Mirrors the faking style in ``test_opencode_driver.py``
(the ticket-side driver these fakes are modeled on) and the host-loop style
in ``test_agents_host.py``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nightdesk.db.models import Profile
from nightdesk.db.models import Session as SessionModel
from nightdesk.db.models import SessionTurn
from nightdesk.db.session import session_factory
from nightdesk.domain import sessions as sess
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.providers import ResolvedEndpoint
from nightdesk.transcript import read_events
from nightdesk.worker import resident_backends as rb
from nightdesk.worker.resident_backends import (
    ClaudeResidentBackend,
    OpencodeResidentBackend,
    StartSpec,
    resident_backend_for,
    translator_for,
)
from nightdesk.worker.session_host import SessionHost


pytestmark = pytest.mark.anyio

BOX = ProfileSecretBox("t")


# ---------------------------------------------------------------------------
# Backend / translator selection
# ---------------------------------------------------------------------------
def test_resident_backend_for_selects_by_session_backend_code():
    assert isinstance(resident_backend_for("claude"), ClaudeResidentBackend)
    assert isinstance(resident_backend_for("opencode"), OpencodeResidentBackend)
    # Unknown/legacy codes fall back to Claude (the only value a pre-multi-
    # backend row can carry).
    assert isinstance(resident_backend_for("something-old"), ClaudeResidentBackend)


def test_translator_for_selects_identity_for_opencode():
    assert translator_for("opencode")({"type": "assistant_text", "text": "hi"}) == [
        {"type": "assistant_text", "text": "hi"}
    ]
    # Claude's translator is the real SDK-shaped translator, not identity.
    claude_translate = translator_for("claude")
    assert claude_translate is not translator_for("opencode")


# ---------------------------------------------------------------------------
# Fakes (httpx.AsyncClient / subprocess), modeled on test_opencode_driver.py
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._json


class _FakeEventStream:
    def __init__(self, line_gen_factory):
        self._line_gen_factory = line_gen_factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def aiter_lines(self):
        return self._line_gen_factory()


class _FakeClient:
    """Fakes ``httpx.AsyncClient`` for the calls the resident handle makes."""

    def __init__(self, line_gen_factory, session_id="sess-1"):
        self.calls: list[str] = []
        self._line_gen_factory = line_gen_factory
        self._session_id = session_id
        self.closed = False
        self.stream_params = None

    async def get(self, path, timeout=None):
        self.calls.append(f"get:{path}")
        return _FakeResponse(200)

    async def post(self, path, **kw):
        self.calls.append(f"post:{path}")
        if path == "/session":
            return _FakeResponse(200, {"id": self._session_id})
        return _FakeResponse(200, {})

    def stream(self, method, path, params=None, timeout=None):
        self.calls.append(f"stream:{path}")
        self.stream_params = params
        return _FakeEventStream(self._line_gen_factory)

    async def aclose(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.pid = 4242

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


def _queue_lines(queue: "asyncio.Queue[str | None]"):
    async def _gen():
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item
    return _gen


async def _await(cond, timeout=3.0, interval=0.01):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return False


def _patch_process_spawn(monkeypatch, proc=None):
    async def _fake_exec(*a, **kw):
        return proc or _FakeProcess()
    monkeypatch.setattr(rb.asyncio, "create_subprocess_exec", _fake_exec)


# ---------------------------------------------------------------------------
# start() / handle round-trip
# ---------------------------------------------------------------------------
async def test_start_raises_when_server_fails_to_start(monkeypatch, tmp_path):
    _patch_process_spawn(monkeypatch, proc=_FakeProcess(returncode=1))
    fake_client = _FakeClient(_queue_lines(asyncio.Queue()))
    monkeypatch.setattr(rb.httpx, "AsyncClient", lambda **kw: fake_client)

    spec = StartSpec(working_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="failed to start"):
        await OpencodeResidentBackend().start(spec, "trusted")
    assert fake_client.closed


async def test_start_and_turn_round_trip_yields_canonical_events_and_turn_complete(
    monkeypatch, tmp_path,
):
    _patch_process_spawn(monkeypatch)
    line_queue: "asyncio.Queue[str | None]" = asyncio.Queue()
    fake_client = _FakeClient(_queue_lines(line_queue))
    monkeypatch.setattr(rb.httpx, "AsyncClient", lambda **kw: fake_client)

    spec = StartSpec(working_dir=str(tmp_path), model="nd_ep1/gpt-5.4",
                     system_prompt="be terse")
    backend = OpencodeResidentBackend()
    handle = await backend.start(spec, "trusted")
    assert handle.pid == 4242

    collected: list[dict] = []

    async def _consume():
        async for evt in handle.events():
            collected.append(evt)

    consumer = asyncio.create_task(_consume())
    try:
        await handle.send({"type": "user_turn", "turn_id": "t1", "text": "hello"})
        assert await _await(
            lambda: any(c.endswith("/prompt_async") for c in fake_client.calls))
        prompt_call = next(c for c in fake_client.calls if c.endswith("/prompt_async"))
        assert prompt_call == "post:/session/sess-1/prompt_async"

        await line_queue.put("data: " + json.dumps({
            "type": "message.part.updated",
            "properties": {"part": {"id": "p1", "type": "text", "text": "hi there"}},
        }))
        await line_queue.put("data: " + json.dumps({
            "type": "message.updated",
            "properties": {"info": {
                "id": "m1", "modelID": "gpt-5.4",
                "tokens": {"input": 100, "output": 40}, "cost": 0.02,
            }},
        }))
        await line_queue.put("data: " + json.dumps({
            "type": "session.idle",
            "properties": {"sessionID": "sess-1"},
        }))

        assert await _await(lambda: any(e.get("type") == "turn_complete" for e in collected))
        assert any(e.get("type") == "assistant_text" and e.get("text") == "hi there"
                  for e in collected)
        tc = next(e for e in collected if e["type"] == "turn_complete")
        assert tc["turn_id"] == "t1"
        assert tc["session_id"] == "sess-1"
        assert tc["usage"] == {
            "input_tokens": 100, "output_tokens": 40,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }
        assert tc["cost_usd"] == pytest.approx(0.02)

        # interrupt() aborts the live session.
        await handle.send({"type": "interrupt"})
        assert await _await(
            lambda: "post:/session/sess-1/abort" in fake_client.calls)

        # answer() is a no-op — opencode never parks a pending_input.
        await handle.send({"type": "answer", "request_id": "r1", "decision": "allow"})
    finally:
        await handle.close()
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass
    assert fake_client.closed


async def test_event_stream_scoped_to_workdir_directory(monkeypatch, tmp_path):
    """Bug 1: the /event stream must carry the same ``directory`` param as
    ``/session`` and ``prompt_async``, or opencode 1.16 scopes the event bus
    to a different instance and the resident handle never sees a turn
    finish."""
    _patch_process_spawn(monkeypatch)
    line_queue: "asyncio.Queue[str | None]" = asyncio.Queue()
    fake_client = _FakeClient(_queue_lines(line_queue))
    monkeypatch.setattr(rb.httpx, "AsyncClient", lambda **kw: fake_client)

    spec = StartSpec(working_dir=str(tmp_path))
    handle = await OpencodeResidentBackend().start(spec, "trusted")
    try:
        assert fake_client.stream_params == {"directory": str(tmp_path)}
    finally:
        await handle.close()


async def test_session_error_during_turn_surfaces_on_turn_complete(monkeypatch, tmp_path):
    """Bug 6: opencode still ends the turn via session.idle after a provider
    error (e.g. ProviderAuthError on a misconfigured endpoint) — the
    resulting turn_complete must carry the error so the host doesn't mark
    a failed turn as a successful "done"."""
    _patch_process_spawn(monkeypatch)
    line_queue: "asyncio.Queue[str | None]" = asyncio.Queue()
    fake_client = _FakeClient(_queue_lines(line_queue))
    monkeypatch.setattr(rb.httpx, "AsyncClient", lambda **kw: fake_client)

    spec = StartSpec(working_dir=str(tmp_path))
    handle = await OpencodeResidentBackend().start(spec, "trusted")
    collected: list[dict] = []
    consumer = asyncio.create_task(_collect(handle, collected))
    try:
        await handle.send({"type": "user_turn", "turn_id": "t1", "text": "hello"})
        await _await(lambda: any(c.endswith("/prompt_async") for c in fake_client.calls))

        await line_queue.put("data: " + json.dumps({
            "type": "session.error",
            "properties": {"sessionID": "sess-1", "error": {
                "name": "ProviderAuthError",
                "data": {"message": "OpenAI API key is missing"},
            }},
        }))
        await line_queue.put("data: " + json.dumps({
            "type": "session.idle", "properties": {"sessionID": "sess-1"}}))

        assert await _await(lambda: _turn_complete(collected, "t1") is not None)
        tc = _turn_complete(collected, "t1")
        assert tc["error"] == "OpenAI API key is missing"
        assert any(e.get("type") == "worker_error" and e.get("kind") == "ProviderAuthError"
                  for e in collected)
    finally:
        await handle.close()
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass


async def test_turn_complete_usage_is_cumulative_across_turns(monkeypatch, tmp_path):
    """opencode reports usage per-message; the handle must sum every message
    seen so far so each ``turn_complete`` carries session-cumulative tokens
    and cost — the shape ``SessionHost._slice_usage`` deltas against a
    per-generation baseline (parity with Claude's cumulative ResultMessage)."""
    _patch_process_spawn(monkeypatch)
    line_queue: "asyncio.Queue[str | None]" = asyncio.Queue()
    fake_client = _FakeClient(_queue_lines(line_queue))
    monkeypatch.setattr(rb.httpx, "AsyncClient", lambda **kw: fake_client)

    handle = await OpencodeResidentBackend().start(
        StartSpec(working_dir=str(tmp_path)), "trusted")
    collected: list[dict] = []
    consumer = asyncio.create_task(_collect(handle, collected))
    try:
        # Turn 1: one message with input/output + cache tokens.
        await handle.send({"type": "user_turn", "turn_id": "t1", "text": "a"})
        await _await(lambda: any(c.endswith("/prompt_async") for c in fake_client.calls))
        await line_queue.put("data: " + json.dumps({
            "type": "message.updated",
            "properties": {"info": {
                "id": "m1", "modelID": "gpt-5.4",
                "tokens": {"input": 100, "output": 40, "cache": {"read": 10, "write": 5}},
                "cost": 0.02,
            }},
        }))
        await line_queue.put("data: " + json.dumps({
            "type": "session.idle", "properties": {"sessionID": "sess-1"}}))
        assert await _await(lambda: _turn_complete(collected, "t1") is not None)
        tc1 = _turn_complete(collected, "t1")
        assert tc1["usage"] == {
            "input_tokens": 100, "output_tokens": 40,
            "cache_read_tokens": 10, "cache_write_tokens": 5}
        assert tc1["cost_usd"] == pytest.approx(0.02)

        # Turn 2: a second, distinct message. The running total must include m1.
        await handle.send({"type": "user_turn", "turn_id": "t2", "text": "b"})
        await line_queue.put("data: " + json.dumps({
            "type": "message.updated",
            "properties": {"info": {
                "id": "m2", "modelID": "gpt-5.4",
                "tokens": {"input": 30, "output": 20, "cache": {"read": 1, "write": 2}},
                "cost": 0.01,
            }},
        }))
        await line_queue.put("data: " + json.dumps({
            "type": "session.idle", "properties": {"sessionID": "sess-1"}}))
        assert await _await(lambda: _turn_complete(collected, "t2") is not None)
        tc2 = _turn_complete(collected, "t2")
        assert tc2["usage"] == {
            "input_tokens": 130, "output_tokens": 60,
            "cache_read_tokens": 11, "cache_write_tokens": 7}
        assert tc2["cost_usd"] == pytest.approx(0.03)
    finally:
        await handle.close()
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass


def _turn_complete(collected: list[dict], turn_id: str):
    return next((e for e in collected
                 if e.get("type") == "turn_complete" and e.get("turn_id") == turn_id), None)


async def _collect(handle, sink: list[dict]):
    async for evt in handle.events():
        sink.append(evt)


async def test_resume_reuses_existing_session_without_creating_a_new_one(
    monkeypatch, tmp_path,
):
    """A ``StartSpec.resume`` handle (a prior generation's opencode session
    id) is reused directly — no ``POST /session`` call on the first turn."""
    _patch_process_spawn(monkeypatch)
    line_queue: "asyncio.Queue[str | None]" = asyncio.Queue()
    fake_client = _FakeClient(_queue_lines(line_queue), session_id="new-sess-should-not-be-used")
    monkeypatch.setattr(rb.httpx, "AsyncClient", lambda **kw: fake_client)

    spec = StartSpec(working_dir=str(tmp_path), resume="oc-sess-old")
    handle = await OpencodeResidentBackend().start(spec, "trusted")
    assert handle.session_id == "oc-sess-old"

    consumer = asyncio.create_task(_drain(handle))
    try:
        await handle.send({"type": "user_turn", "turn_id": "t1", "text": "continue"})
        assert await _await(
            lambda: any(c.endswith("/prompt_async") for c in fake_client.calls))
        assert "post:/session" not in fake_client.calls
        assert "post:/session/oc-sess-old/prompt_async" in fake_client.calls
    finally:
        await handle.close()
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass


async def _drain(handle):
    async for _ in handle.events():
        pass


# ---------------------------------------------------------------------------
# SessionHost._build_opencode_spec: model/endpoint resolution mirrors tickets
# ---------------------------------------------------------------------------
async def test_build_opencode_spec_resolves_endpoint_and_model(engine, tmp_path, monkeypatch):
    factory = session_factory(engine)
    with factory() as db:
        profile = Profile(
            name="oc-profile", backend="opencode", endpoint_id="ep1",
            allowed_tools=["bash"], denied_tools=[], backend_config={},
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)

        row = sess.create_session(
            db, profile_id=profile.id, source_path=str(tmp_path / "src"),
            transcript_root=str(tmp_path / "tr"), box=BOX,
        )
        (tmp_path / "src").mkdir(exist_ok=True)

        endpoint = ResolvedEndpoint(
            id="ep1", label="prod", protocol_kind="openai_compat",
            base_url="https://api.example.com/v1", credential="sk-test",
            credential_source="api_key", default_model="gpt-5.4",
        )
        import nightdesk.worker.session_host as sh_mod
        monkeypatch.setattr(
            sh_mod, "resolve_endpoints_for_profile",
            lambda db, profile, box: {"ep1": endpoint},
        )

        host = SessionHost(session_factory=factory, session_id=row.id, box=BOX, host="test")
        spec = host._build_opencode_spec(db, row, {"FOO": "bar"}, None)

    assert spec.model == "nd_ep1/gpt-5.4"
    assert spec.working_dir == row.source_path
    assert spec.env["FOO"] == "bar"  # the session's own env still merges through
    cfg = json.loads(spec.env["OPENCODE_CONFIG_CONTENT"])
    assert cfg["model"] == "nd_ep1/gpt-5.4"
    assert cfg["provider"]["nd_ep1"]["options"]["apiKey"] == "sk-test"
    assert cfg["provider"]["nd_ep1"]["options"]["baseURL"] == "https://api.example.com/v1"
    auth = json.loads(spec.env["OPENCODE_AUTH_CONTENT"])
    assert auth["nd_ep1"] == {"type": "api", "key": "sk-test"}


# ---------------------------------------------------------------------------
# End-to-end: SessionHost auto-selects OpencodeResidentBackend from row.backend
# ---------------------------------------------------------------------------
async def test_session_host_runs_opencode_backend_end_to_end(engine, tmp_path, monkeypatch):
    _patch_process_spawn(monkeypatch)
    line_queue: "asyncio.Queue[str | None]" = asyncio.Queue()
    fake_client = _FakeClient(_queue_lines(line_queue))
    monkeypatch.setattr(rb.httpx, "AsyncClient", lambda **kw: fake_client)

    factory = session_factory(engine)
    with factory() as db:
        profile = Profile(name="oc-profile", backend="opencode",
                          allowed_tools=[], denied_tools=[], backend_config={})
        db.add(profile)
        db.commit()
        db.refresh(profile)
        row = sess.create_session(
            db, profile_id=profile.id, source_path=str(tmp_path / "src"),
            transcript_root=str(tmp_path / "tr"), box=BOX, idle_timeout_s=3600,
        )
        (tmp_path / "src").mkdir(exist_ok=True)
        assert row.backend == "opencode"
        sess.post_message(db, row.id, "hi")

    # backend=None -> the host must auto-select OpencodeResidentBackend from
    # the row's frozen backend, exactly like production.
    host = SessionHost(session_factory=factory, session_id=row.id, box=BOX,
                       host="test", poll_interval=0.01)
    task = asyncio.create_task(host.run())
    try:
        assert await _await(lambda: host._backend is not None)
        assert isinstance(host._backend, OpencodeResidentBackend)
        assert await _await(
            lambda: any(c.endswith("/prompt_async") for c in fake_client.calls))
        await line_queue.put("data: " + json.dumps({
            "type": "message.part.updated",
            "properties": {"part": {"id": "p1", "type": "text", "text": "hi there"}},
        }))
        await line_queue.put("data: " + json.dumps({
            "type": "session.idle", "properties": {"sessionID": "sess-1"},
        }))

        def _turn_done():
            with factory() as db:
                t = db.query(SessionTurn).filter_by(session_id=row.id).first()
                return t is not None and t.status == "done"

        assert await _await(_turn_done)
        with factory() as db:
            r = db.get(SessionModel, row.id)
            assert (r.resume_handle or {}).get("session_id") == "sess-1"
        events = list(read_events(Path(row.transcript_path)))
        assert any(e.get("type") == "assistant_text" and e.get("text") == "hi there"
                  for e in events)
    finally:
        with factory() as db:
            sess.end_session(db, row.id)
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            if not task.done():
                task.cancel()
