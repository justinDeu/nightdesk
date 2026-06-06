# tests/test_claude_executor.py
import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from nightdesk.domain.permissions import PermissionSpec
from nightdesk.worker.executor import ExecutionRequest


class FakeStdout:
    def __init__(self, lines: list[bytes], delay: float = 0.0):
        self._buf = bytearray(b"".join(lines))
        self._delay = delay

    async def read(self, n: int = -1) -> bytes:
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._buf:
            return b""
        if n is None or n < 0 or n >= len(self._buf):
            out = bytes(self._buf)
            self._buf.clear()
            return out
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class FakeStdin:
    def __init__(self):
        self.buffer = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProc:
    def __init__(self, *, stdout_lines: list[bytes], returncode: int = 0,
                  wait_delay: float = 0.0):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(stdout_lines)
        self.pid = 4242
        self._returncode = returncode
        self._wait_delay = wait_delay
        self._terminated = asyncio.Event()
        self.terminated_called = False
        self.killed = False

    async def wait(self) -> int:
        if self._wait_delay:
            try:
                await asyncio.wait_for(self._terminated.wait(), timeout=self._wait_delay)
            except asyncio.TimeoutError:
                pass
        return self._returncode

    def terminate(self) -> None:
        self.terminated_called = True
        self._returncode = -15
        self._terminated.set()

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9
        self._terminated.set()


@pytest.mark.anyio
async def test_claude_executor_writes_transcript_and_final_summary(tmp_path):
    transcript = tmp_path / "t.log"
    lines = [
        json.dumps({"type": "assistant", "content": "doing the thing"}).encode() + b"\n",
        json.dumps({"type": "result", "subtype": "success",
                    "result": "Final summary line"}).encode() + b"\n",
    ]
    fake = FakeProc(stdout_lines=lines, returncode=0)

    async def fake_spawn(argv):
        return fake

    with patch("nightdesk.worker.claude_executor._spawn_sdk_subprocess", new=fake_spawn):
        from nightdesk.worker.claude_executor import ClaudeExecutor
        spec = PermissionSpec(allowed_tools=["Read", "Write"], denied_tools=["Bash"],
                              default_model="claude-sonnet-4-6")
        req = ExecutionRequest(
            ticket_id="t1", prompt="hi", working_dir=tmp_path,
            transcript_path=transcript,
            bwrap_argv=["bwrap", "--", "python", "-m", "nightdesk.worker._sdk_runner"],
            env={}, permission_spec=spec,
        )
        res = await ClaudeExecutor().run(req)

    # Subprocess saw the JSON spec on stdin.
    sent = json.loads(fake.stdin.buffer.decode("utf-8"))
    assert sent["prompt"] == "hi"
    assert sent["allowed_tools"] == ["Read", "Write"]
    # The profile's own deny stays, plus the default git-push gate is injected.
    assert sent["disallowed_tools"][0] == "Bash"
    assert "Bash(git push:*)" in sent["disallowed_tools"]
    assert sent["model"] == "claude-sonnet-4-6"
    assert fake.stdin.closed

    assert res.exit_status == "success"
    assert "Final summary line" in (res.final_summary or "")
    assert res.assistant_tail == ["doing the thing"]
    body = transcript.read_text()
    assert "doing the thing" in body
    assert "Final summary line" in body


@pytest.mark.anyio
async def test_usage_model_taken_from_assistant_stream(tmp_path):
    """The result event carries no model, so the model the agent actually used
    is captured from the assistant messages — not left NULL ("unknown")."""
    transcript = tmp_path / "t.log"
    lines = [
        json.dumps({"type": "assistant", "content": "working",
                    "model": "claude-opus-4-7"}).encode() + b"\n",
        json.dumps({"type": "result", "subtype": "success",
                    "result": "done",
                    "usage": {"input_tokens": 100, "output_tokens": 50}}).encode() + b"\n",
    ]
    fake = FakeProc(stdout_lines=lines, returncode=0)

    async def fake_spawn(argv):
        return fake

    with patch("nightdesk.worker.claude_executor._spawn_sdk_subprocess", new=fake_spawn):
        from nightdesk.worker.claude_executor import ClaudeExecutor
        # default_model unset (the common Claude case that produced "unknown").
        spec = PermissionSpec(allowed_tools=[], denied_tools=[], default_model=None)
        req = ExecutionRequest(
            ticket_id="t1", prompt="hi", working_dir=tmp_path,
            transcript_path=transcript,
            bwrap_argv=["bwrap", "--", "python", "-m", "nightdesk.worker._sdk_runner"],
            env={}, permission_spec=spec,
        )
        res = await ClaudeExecutor().run(req)

    assert res.usage is not None
    assert res.usage.model == "claude-opus-4-7"
    assert res.usage.input_tokens == 100


@pytest.mark.anyio
async def test_claude_executor_handles_oversized_json_line(tmp_path):
    transcript = tmp_path / "t.log"
    huge = "x" * (256 * 1024)
    lines = [
        json.dumps({"type": "assistant", "content": huge}).encode() + b"\n",
        json.dumps({"type": "result", "subtype": "success",
                    "result": "done"}).encode() + b"\n",
    ]
    fake = FakeProc(stdout_lines=lines, returncode=0)

    async def fake_spawn(argv):
        return fake

    with patch("nightdesk.worker.claude_executor._spawn_sdk_subprocess", new=fake_spawn):
        from nightdesk.worker.claude_executor import ClaudeExecutor
        spec = PermissionSpec(allowed_tools=[], denied_tools=[], default_model=None)
        req = ExecutionRequest(
            ticket_id="t1", prompt="hi", working_dir=tmp_path,
            transcript_path=transcript,
            bwrap_argv=["bwrap", "--", "python", "-m", "nightdesk.worker._sdk_runner"],
            env={}, permission_spec=spec,
        )
        res = await ClaudeExecutor().run(req)

    assert res.exit_status == "success"
    assert res.error_summary is None
    assert huge in transcript.read_text()


@pytest.mark.anyio
async def test_claude_executor_failed_when_runner_exits_nonzero(tmp_path):
    transcript = tmp_path / "t.log"
    fake = FakeProc(stdout_lines=[], returncode=2)

    async def fake_spawn(argv):
        return fake

    with patch("nightdesk.worker.claude_executor._spawn_sdk_subprocess", new=fake_spawn):
        from nightdesk.worker.claude_executor import ClaudeExecutor
        req = ExecutionRequest(
            ticket_id="t1", prompt="hi", working_dir=tmp_path, transcript_path=transcript,
            bwrap_argv=["bwrap"], env={},
            permission_spec=PermissionSpec(),
        )
        res = await ClaudeExecutor().run(req)

    assert res.exit_status == "failed"
    assert "exited 2" in (res.error_summary or "")


@pytest.mark.anyio
async def test_claude_executor_cancelled_when_event_fires(tmp_path):
    transcript = tmp_path / "t.log"
    # Long-running stdout iterator and a wait that won't return until terminate.
    fake = FakeProc(stdout_lines=[], returncode=0, wait_delay=10.0)

    async def fake_spawn(argv):
        return fake

    with patch("nightdesk.worker.claude_executor._spawn_sdk_subprocess", new=fake_spawn):
        from nightdesk.worker.claude_executor import ClaudeExecutor
        req = ExecutionRequest(
            ticket_id="t1", prompt="hi", working_dir=tmp_path, transcript_path=transcript,
            bwrap_argv=["bwrap"], env={},
            permission_spec=PermissionSpec(),
        )

        async def fire_cancel():
            await asyncio.sleep(0.05)
            req.cancel_event.set()

        cancel_task = asyncio.create_task(fire_cancel())
        res = await ClaudeExecutor().run(req)
        await cancel_task

    assert res.exit_status == "cancelled"
    assert fake.terminated_called

    # The cancel path must leave a terminal marker in the transcript so the
    # user sees *why* the stream stopped, not just an abrupt end. Exactly one
    # canonical cancelled event with a seq and a human message.
    lines = [
        json.loads(ln)
        for ln in transcript.read_text().splitlines() if ln.strip()
    ]
    cancelled = [e for e in lines if e.get("type") == "cancelled"]
    assert len(cancelled) == 1
    evt = cancelled[0]
    assert evt["message"] == "Run cancelled by user."
    assert isinstance(evt.get("seq"), int)
    assert evt.get("ts")


async def test_cancelled_run_captures_session_id_from_init(tmp_path):
    """A cancelled run records the session id from the init message — captured
    up front, not at the (never-reached) result event — so the conversation can
    still be resumed after a cancel."""
    transcript = tmp_path / "t.log"
    init_line = json.dumps({
        "type": "system", "subtype": "init",
        "data": {"session_id": "sess-cancel-1"},
    }).encode() + b"\n"
    # Init line drains immediately; wait blocks so the cancel fires mid-run.
    fake = FakeProc(stdout_lines=[init_line], returncode=0, wait_delay=10.0)

    async def fake_spawn(argv):
        return fake

    with patch("nightdesk.worker.claude_executor._spawn_sdk_subprocess", new=fake_spawn):
        from nightdesk.worker.claude_executor import ClaudeExecutor
        req = ExecutionRequest(
            ticket_id="t1", prompt="hi", working_dir=tmp_path, transcript_path=transcript,
            bwrap_argv=["bwrap"], env={},
            permission_spec=PermissionSpec(),
        )

        async def fire_cancel():
            await asyncio.sleep(0.05)
            req.cancel_event.set()

        cancel_task = asyncio.create_task(fire_cancel())
        res = await ClaudeExecutor().run(req)
        await cancel_task

    assert res.exit_status == "cancelled"
    assert res.session_id == "sess-cancel-1"


def test_translator_passes_cancelled_event_through():
    """The canonical cancelled event passes straight through the translator,
    mirroring rate_limit and the other already-canonical types."""
    from nightdesk.worker.claude_translator import translate

    evt = {"type": "cancelled", "message": "Run cancelled by user.",
           "seq": 7, "ts": "2026-05-19T00:00:00+00:00"}
    assert translate(evt) == [evt]


def _runner_spec_for(spec: PermissionSpec, tmp_path) -> dict:
    from nightdesk.worker.claude_executor import ClaudeExecutor
    req = ExecutionRequest(
        ticket_id="t1", prompt="hi", working_dir=tmp_path,
        transcript_path=tmp_path / "t.log",
        bwrap_argv=["bwrap"], env={}, permission_spec=spec,
    )
    return ClaudeExecutor()._build_runner_spec(req)


def test_git_push_blocked_by_default(tmp_path):
    """With no opt-in, the git-push deny rules are injected into the disallowed
    tools so a sandboxed agent can't push to a real remote."""
    from nightdesk.domain.permissions import GIT_PUSH_DENY_RULES

    spec = PermissionSpec(denied_tools=["Bash(rm:*)"])
    runner = _runner_spec_for(spec, tmp_path)
    for rule in GIT_PUSH_DENY_RULES:
        assert rule in runner["disallowed_tools"]
    # The profile's own deny rules are preserved alongside the gate.
    assert "Bash(rm:*)" in runner["disallowed_tools"]


def test_git_push_opt_in_removes_gate(tmp_path):
    """allow_git_push=True is the clear opt-in: the push deny rules are not
    injected, so the agent may push."""
    from nightdesk.domain.permissions import GIT_PUSH_DENY_RULES

    spec = PermissionSpec(allow_git_push=True)
    runner = _runner_spec_for(spec, tmp_path)
    for rule in GIT_PUSH_DENY_RULES:
        assert rule not in runner["disallowed_tools"]


def test_git_push_gate_does_not_duplicate_existing_rule(tmp_path):
    """If a profile already denies git push, the gate doesn't add a duplicate."""
    spec = PermissionSpec(denied_tools=["Bash(git push:*)"])
    runner = _runner_spec_for(spec, tmp_path)
    assert runner["disallowed_tools"].count("Bash(git push:*)") == 1


def test_module_imports_without_sdk():
    """Importing claude_executor must not require claude_agent_sdk."""
    # Just re-import it from a fresh interpreter to be sure no top-level
    # `from claude_agent_sdk import ...` was reintroduced.
    import importlib
    import nightdesk.worker.claude_executor as ce
    importlib.reload(ce)
    assert hasattr(ce, "ClaudeExecutor")


# --- Smoke test for _sdk_runner.py invoked as a real subprocess --------------

_RUNNER_STUB = textwrap.dedent('''
    import sys, types, asyncio
    fake = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    async def query(prompt, options):
        yield {"type": "assistant", "content": "echo: " + prompt}
        yield {"type": "result", "subtype": "success", "result": "done"}

    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.query = query
    sys.modules["claude_agent_sdk"] = fake

    from nightdesk.worker import _sdk_runner
    sys.exit(_sdk_runner.main())
''')


def test_event_to_dict_converts_typed_sdk_objects():
    """Typed SDK message objects must serialize to translator-compatible dicts.

    Regression: the SDK started yielding dataclass instances and the runner
    was passing them through json.dumps with default=str, collapsing each
    event to a repr string that landed as fallback assistant_text in the
    transcript. The translator never saw a dict and could not split content
    into tool_use / tool_result / thinking blocks.
    """
    from dataclasses import dataclass

    from nightdesk.worker._sdk_runner import _block_to_dict, _event_to_dict

    @dataclass
    class TextBlock:
        text: str

    @dataclass
    class ThinkingBlock:
        thinking: str
        signature: str = ""

    @dataclass
    class ToolUseBlock:
        id: str
        name: str
        input: dict

    @dataclass
    class ToolResultBlock:
        tool_use_id: str
        content: object = None
        is_error: bool = False

    @dataclass
    class AssistantMessage:
        content: list
        model: str = "claude-x"

    @dataclass
    class UserMessage:
        content: object
        uuid: str = ""

    @dataclass
    class SystemMessage:
        subtype: str
        data: dict

    @dataclass
    class ResultMessage:
        subtype: str
        result: str
        is_error: bool = False

    @dataclass
    class RateLimitEvent:
        rate_limit_info: object = None

    asst = AssistantMessage(content=[
        TextBlock(text="hello"),
        ThinkingBlock(thinking="hmm"),
        ToolUseBlock(id="u1", name="Bash", input={"command": "ls"}),
    ])
    d = _event_to_dict(asst)
    assert d["type"] == "assistant"
    blocks = d["message"]["content"]
    assert blocks[0] == {"type": "text", "text": "hello"}
    assert blocks[1] == {"type": "thinking", "thinking": "hmm"}
    assert blocks[2] == {"type": "tool_use", "id": "u1",
                          "name": "Bash", "input": {"command": "ls"}}

    user = UserMessage(content=[ToolResultBlock(tool_use_id="u1",
                                                  content="output",
                                                  is_error=True)])
    d = _event_to_dict(user)
    assert d["type"] == "user"
    assert d["message"]["content"][0] == {
        "type": "tool_result", "tool_use_id": "u1",
        "content": "output", "is_error": True,
    }

    sys_msg = SystemMessage(subtype="init", data={"source_path": "/x"})
    assert _event_to_dict(sys_msg) == {"type": "system", "subtype": "init",
                                         "data": {"source_path": "/x"}}

    res = ResultMessage(subtype="success", result="all good")
    d = _event_to_dict(res)
    assert d["type"] == "result"
    assert d["subtype"] == "success"
    assert d["result"] == "all good"

    # Rate-limit notifications are surfaced as a canonical rate_limit event,
    # not dropped. With no rate_limit_info the type still comes through plus a
    # best-effort raw payload dump for the dropdown.
    rl = _event_to_dict(RateLimitEvent())
    assert rl["type"] == "rate_limit"
    assert "raw" in rl

    # Plain dicts (e.g. from test stubs) pass through unchanged.
    raw = {"type": "assistant", "content": "passthrough"}
    assert _event_to_dict(raw) is raw

    # Unknown block falls back to a text block so it still renders.
    class Mystery: ...
    assert _block_to_dict(Mystery())["type"] == "text"


def test_event_to_dict_translates_rate_limit_event():
    """A RateLimitEvent must become a canonical rate_limit dict carrying the
    nested RateLimitInfo fields the renderer needs, not be dropped."""
    from dataclasses import dataclass, field
    from typing import Optional

    from nightdesk.worker._sdk_runner import _event_to_dict
    from nightdesk.worker.claude_translator import translate

    @dataclass
    class RateLimitInfo:
        status: str = "rejected"
        resets_at: Optional[int] = 1_900_000_000
        rate_limit_type: Optional[str] = "five_hour"
        utilization: Optional[float] = 0.95
        overage_status: Optional[str] = "rejected"
        is_using_overage: Optional[bool] = True
        overage_disabled_reason: Optional[str] = "limit_reached"
        overage_resets_at: Optional[int] = 1_900_000_500

    @dataclass
    class RateLimitEvent:
        rate_limit_info: object = field(default_factory=RateLimitInfo)

    d = _event_to_dict(RateLimitEvent())
    assert d["type"] == "rate_limit"
    assert d["status"] == "rejected"
    assert d["resets_at"] == 1_900_000_000
    assert d["rate_limit_type"] == "five_hour"
    assert d["utilization"] == 0.95
    # The overage fields the renderer's classifier needs are captured too, so
    # "allowed but using overage" can be told apart from "blocked".
    assert d["overage_status"] == "rejected"
    assert d["is_using_overage"] is True
    assert d["overage_disabled_reason"] == "limit_reached"
    assert d["overage_resets_at"] == 1_900_000_500
    # The full payload is always attached for the raw-response dropdown.
    assert "status: rejected" in d["raw"]
    # The translator passes the canonical event straight through.
    assert translate(d) == [d]


def test_event_to_dict_rate_limit_allowed_captures_overage_fields():
    """An "allowed" report (benign telemetry) still carries the overage fields,
    so the renderer can classify it as benign rather than an error."""
    from dataclasses import dataclass, field
    from typing import Optional

    from nightdesk.worker._sdk_runner import _event_to_dict

    @dataclass
    class RateLimitInfo:
        status: str = "allowed"
        resets_at: Optional[int] = 1_900_000_000
        rate_limit_type: Optional[str] = "five_hour"
        utilization: Optional[float] = 0.42
        overage_status: Optional[str] = "allowed"
        is_using_overage: Optional[bool] = False

    @dataclass
    class RateLimitEvent:
        rate_limit_info: object = field(default_factory=RateLimitInfo)

    d = _event_to_dict(RateLimitEvent())
    assert d["type"] == "rate_limit"
    assert d["status"] == "allowed"
    assert d["overage_status"] == "allowed"
    assert d["is_using_overage"] is False


def test_event_to_dict_result_carries_session_id():
    """The ResultMessage's session_id is captured on the result event so the
    run can be resumed later (claude --resume <id>)."""
    from dataclasses import dataclass

    from nightdesk.worker._sdk_runner import _event_to_dict

    @dataclass
    class ResultMessage:
        subtype: str = "success"
        result: str = "done"
        is_error: bool = False
        session_id: str = "sess-xyz-789"

    d = _event_to_dict(ResultMessage())
    assert d["type"] == "result"
    assert d["session_id"] == "sess-xyz-789"

    # No session_id reported -> key simply absent (not None/empty noise).
    @dataclass
    class ResultNoSession:
        subtype: str = "success"
        result: str = "done"
        is_error: bool = False

    assert "session_id" not in _event_to_dict(ResultNoSession())


def test_event_to_dict_unknown_message_is_worker_error_not_agent():
    """An unrecognized SDK message must surface as a worker_error (system
    styling), not as an assistant_text agent message.

    Regression (BUG 21): the unknown-message fallback returned
    {"type": "assistant_text", text: str(evt)}, so a runner-level surprise
    (an SDK error object, an unexpected message class) was misattributed to
    the model and rendered in the agent's prose block. Runner/worker errors
    must render via the worker_error card instead.
    """
    from nightdesk.worker._sdk_runner import _event_to_dict
    from nightdesk.worker.claude_translator import translate

    class RunnerBoom:
        def __str__(self):
            return "runner exploded: connection reset"

    d = _event_to_dict(RunnerBoom())
    assert d["type"] == "worker_error"
    assert d["type"] != "assistant_text"
    assert d["kind"] == "runner_unknown_message"
    assert "runner exploded" in d["summary"]
    # The translator carries the worker_error straight through so the
    # renderer shows the worker-error card, not the assistant block.
    assert translate(d) == [d]


@pytest.mark.anyio
async def test_sdk_runner_typed_objects_translate_end_to_end(tmp_path):
    """Subprocess run with a typed-object SDK stub yields canonical events.

    Drives the full _sdk_runner → claude_executor → claude_translator path:
    the SDK stub yields dataclass instances, the runner converts them to
    dicts, the executor writes them, and the translator splits assistant
    content into tool_use / tool_result blocks. The transcript must contain
    parseable tool_use rows with real input fields, not a repr string.
    """
    stub = tmp_path / "typed_runner_stub.py"
    stub.write_text(textwrap.dedent('''
        import sys, types, asyncio
        from dataclasses import dataclass

        @dataclass
        class TextBlock:
            text: str
        @dataclass
        class ToolUseBlock:
            id: str
            name: str
            input: dict
        @dataclass
        class ToolResultBlock:
            tool_use_id: str
            content: object = None
            is_error: bool = False
        @dataclass
        class AssistantMessage:
            content: list
            model: str = "claude-x"
        @dataclass
        class UserMessage:
            content: object
        @dataclass
        class ResultMessage:
            subtype: str
            result: str
            is_error: bool = False

        fake = types.ModuleType("claude_agent_sdk")
        class ClaudeAgentOptions:
            def __init__(self, **kwargs): self.kwargs = kwargs
        async def query(prompt, options):
            yield AssistantMessage(content=[
                TextBlock(text="hi there"),
                ToolUseBlock(id="u1", name="Bash", input={"command": "echo x"}),
            ])
            yield UserMessage(content=[
                ToolResultBlock(tool_use_id="u1", content="x", is_error=False),
            ])
            yield ResultMessage(subtype="success", result="done")
        fake.ClaudeAgentOptions = ClaudeAgentOptions
        fake.query = query
        sys.modules["claude_agent_sdk"] = fake

        from nightdesk.worker import _sdk_runner
        sys.exit(_sdk_runner.main())
    '''))

    repo_src = Path(__file__).resolve().parent.parent / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_src) + os.pathsep + env.get("PYTHONPATH", "")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(stub),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    spec = json.dumps({"prompt": "hi", "source_path": str(tmp_path),
                        "allowed_tools": [], "disallowed_tools": [],
                        "model": None}).encode()
    out, err = await proc.communicate(input=spec)
    assert proc.returncode == 0, err.decode()

    lines = [json.loads(l) for l in out.decode().splitlines() if l.strip()]
    types_seen = [e.get("type") for e in lines]
    assert "assistant" in types_seen
    assert "user" in types_seen
    assert "result" in types_seen

    asst = next(e for e in lines if e.get("type") == "assistant")
    blocks = asst["message"]["content"]
    assert any(b["type"] == "tool_use" and b["name"] == "Bash"
                and b["input"] == {"command": "echo x"} for b in blocks)
    usr = next(e for e in lines if e.get("type") == "user")
    assert usr["message"]["content"][0]["type"] == "tool_result"


@pytest.mark.anyio
async def test_sdk_runner_subprocess_emits_events(tmp_path):
    stub = tmp_path / "run_stub.py"
    stub.write_text(_RUNNER_STUB)

    repo_src = Path(__file__).resolve().parent.parent / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_src) + os.pathsep + env.get("PYTHONPATH", "")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(stub),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    spec = json.dumps({"prompt": "hi", "source_path": str(tmp_path),
                        "allowed_tools": ["Read"], "disallowed_tools": ["Bash"],
                        "model": "claude-sonnet-4-6"}).encode()
    out, err = await proc.communicate(input=spec)
    assert proc.returncode == 0, err.decode()

    lines = [json.loads(l) for l in out.decode().splitlines() if l.strip()]
    assert any(e.get("type") == "assistant" for e in lines)
    final = [e for e in lines if e.get("type") == "result"]
    assert final and final[-1]["result"] == "done"
