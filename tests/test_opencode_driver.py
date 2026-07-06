"""Regression tests for the opencode HTTP driver's stream-handling bugs.

All httpx / subprocess interaction is faked — no real opencode server or
network is involved.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nightdesk.backends import opencode_driver
from nightdesk.domain.permissions import PermissionSpec
from nightdesk.worker.executor import ExecutionRequest


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
    """Fakes the ``async with client.stream(...) as resp`` context manager."""

    def __init__(self, line_gen_factory):
        self._line_gen_factory = line_gen_factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def aiter_lines(self):
        return self._line_gen_factory()


class _FakeClient:
    """Fakes ``httpx.AsyncClient`` for the calls the driver makes."""

    def __init__(self, line_gen_factory):
        self.calls: list[str] = []
        self._line_gen_factory = line_gen_factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, timeout=None):
        self.calls.append(f"get:{path}")
        return _FakeResponse(200)

    async def post(self, path, **kw):
        self.calls.append(f"post:{path}")
        if path == "/session":
            return _FakeResponse(200, {"id": "sess-1"})
        return _FakeResponse(200, {})

    def stream(self, method, path, timeout=None):
        self.calls.append(f"stream:{path}")
        return _FakeEventStream(self._line_gen_factory)


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.pid = 4242
        self.stdout = _EmptyAsyncIter()

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


class _EmptyAsyncIter:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _make_request(tmp_path: Path, cancel_event=None, launch_meta=None) -> ExecutionRequest:
    return ExecutionRequest(
        ticket_id="t1",
        prompt="do the thing",
        working_dir=tmp_path,
        transcript_path=tmp_path / "run.log",
        bwrap_argv=["true"],
        env={},
        permission_spec=PermissionSpec(),
        cancel_event=cancel_event or asyncio.Event(),
        http_port=9999,
        launch_meta=launch_meta or {},
    )


def _patch_process_spawn(monkeypatch):
    async def _fake_exec(*a, **kw):
        return _FakeProcess()

    monkeypatch.setattr(opencode_driver.asyncio, "create_subprocess_exec", _fake_exec)


async def test_prompt_posted_after_event_stream_subscribed(tmp_path, monkeypatch):
    """Bug 1: subscribing to /event must happen BEFORE the prompt is posted,
    so no event emitted between the two calls is lost."""
    _patch_process_spawn(monkeypatch)

    async def _idle_immediately():
        yield 'data: {"type": "session.idle", "properties": {"sessionID": "sess-1"}}'

    fake_client = _FakeClient(_idle_immediately)
    monkeypatch.setattr(opencode_driver.httpx, "AsyncClient", lambda **kw: fake_client)

    req = _make_request(tmp_path)
    result = await opencode_driver.drive_opencode(req)

    assert result.exit_status == "success"
    stream_idx = fake_client.calls.index("stream:/event")
    prompt_idx = fake_client.calls.index("post:/session/sess-1/prompt_async")
    assert stream_idx < prompt_idx, fake_client.calls


async def test_post_prompt_splits_endpoint_prefixed_model(tmp_path, monkeypatch):
    """The per-prompt model param now carries the primary assignment's
    endpoint-block id and model id, split out of the ``nd_<eid>/<model>``
    string threaded through ``launch_meta``."""
    _patch_process_spawn(monkeypatch)

    async def _idle_immediately():
        yield 'data: {"type": "session.idle", "properties": {"sessionID": "sess-1"}}'

    fake_client = _FakeClient(_idle_immediately)
    monkeypatch.setattr(opencode_driver.httpx, "AsyncClient", lambda **kw: fake_client)

    posted_bodies: list[dict] = []
    orig_post = fake_client.post

    async def _capturing_post(path, **kw):
        if path.endswith("/prompt_async"):
            posted_bodies.append(kw.get("json") or {})
        return await orig_post(path, **kw)

    fake_client.post = _capturing_post

    req = _make_request(tmp_path, launch_meta={"model": "nd_ep_zai/glm-5.2"})
    result = await opencode_driver.drive_opencode(req)

    assert result.exit_status == "success"
    assert posted_bodies[0]["model"] == {"providerID": "nd_ep_zai", "modelID": "glm-5.2"}


async def test_drive_opencode_surfaces_usage_by_model_for_two_models(tmp_path, monkeypatch):
    """A run whose transcript touched two models (primary + subagent) surfaces
    a populated ``usage_by_model`` map on the result."""
    _patch_process_spawn(monkeypatch)

    async def _two_models_then_idle():
        yield ('data: {"type": "message.updated", "properties": {"info": '
               '{"id": "msg-1", "modelID": "gpt-5.4", '
               '"tokens": {"input": 1000, "output": 200}}}}')
        yield ('data: {"type": "message.updated", "properties": {"info": '
               '{"id": "msg-2", "modelID": "glm-5.2", '
               '"tokens": {"input": 500, "output": 100, '
               '"cache": {"read": 10, "write": 5}}}}}')
        yield 'data: {"type": "session.idle", "properties": {"sessionID": "sess-1"}}'

    fake_client = _FakeClient(_two_models_then_idle)
    monkeypatch.setattr(opencode_driver.httpx, "AsyncClient", lambda **kw: fake_client)

    req = _make_request(tmp_path)
    result = await opencode_driver.drive_opencode(req)

    assert result.exit_status == "success"
    assert result.usage_by_model == {
        "gpt-5.4": {"input_tokens": 1000, "output_tokens": 200,
                    "cache_read_tokens": 0, "cache_write_tokens": 0},
        "glm-5.2": {"input_tokens": 500, "output_tokens": 100,
                    "cache_read_tokens": 10, "cache_write_tokens": 5},
    }


async def test_drive_opencode_usage_by_model_none_without_usage_events(tmp_path, monkeypatch):
    """No ``message.updated`` usage events at all -> ``usage_by_model`` stays
    None rather than an empty dict, matching the single-model fallback
    contract in ``ExecutionResult``."""
    _patch_process_spawn(monkeypatch)

    async def _idle_immediately():
        yield 'data: {"type": "session.idle", "properties": {"sessionID": "sess-1"}}'

    fake_client = _FakeClient(_idle_immediately)
    monkeypatch.setattr(opencode_driver.httpx, "AsyncClient", lambda **kw: fake_client)

    req = _make_request(tmp_path)
    result = await opencode_driver.drive_opencode(req)

    assert result.exit_status == "success"
    assert result.usage_by_model is None


async def test_consume_events_fails_when_stream_ends_without_idle(tmp_path, monkeypatch):
    """Bug 2: the event stream closing without a session.idle (and without an
    explicit error) must be reported as a failure, not a silent success."""
    _patch_process_spawn(monkeypatch)

    async def _closes_early():
        yield 'data: {"type": "message.part.updated", "properties": {"part": ' \
              '{"id": "t1", "type": "text", "text": "partial"}}}'
        # Stream ends here — no session.idle, no session.error.

    fake_client = _FakeClient(_closes_early)
    monkeypatch.setattr(opencode_driver.httpx, "AsyncClient", lambda **kw: fake_client)

    req = _make_request(tmp_path)
    result = await opencode_driver.drive_opencode(req)

    assert result.exit_status == "failed"
    assert result.error_summary == "event stream ended unexpectedly"


async def test_consume_events_cancels_promptly_on_silent_stream(tmp_path):
    """Bug 3: cancellation must not depend on another SSE line arriving. A
    stream that never produces another line must still honor cancel quickly
    once the cancel event is set, rather than hanging until stream EOF."""

    async def _silent_forever():
        while True:
            await asyncio.sleep(3600)
            yield "unused"  # pragma: no cover - never reached

    class _AbortRecordingClient:
        def __init__(self):
            self.aborted = False

        async def post(self, path, **kw):
            if path.endswith("/abort"):
                self.aborted = True
            return _FakeResponse(200)

    cancel_event = asyncio.Event()
    req = _make_request(tmp_path, cancel_event=cancel_event)
    state: dict = {}

    async def _emit(events):
        pass

    client = _AbortRecordingClient()
    resp = _FakeEventStream(_silent_forever)

    async def _cancel_soon():
        await asyncio.sleep(0.05)
        cancel_event.set()

    asyncio.create_task(_cancel_soon())

    exit_status, error = await asyncio.wait_for(
        opencode_driver._consume_events(client, resp, "sess-1", req, state, _emit),
        timeout=2.0,
    )

    assert exit_status == "cancelled"
    assert error is None
    assert client.aborted
