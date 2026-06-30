"""Tests for _sdk_runner conversion helpers.

Stubs SDK message/block classes by class name so the test does not require the
real claude_agent_sdk package — matching the "importable without SDK" contract.
"""
from __future__ import annotations


def test_module_imports_without_sdk():
    """The runner must be importable even if claude_agent_sdk is absent."""
    import importlib
    import sys
    saved = sys.modules.pop("claude_agent_sdk", None)
    try:
        import nightdesk.worker._sdk_runner  # noqa: F401
    finally:
        if saved is not None:
            sys.modules["claude_agent_sdk"] = saved


def test_block_to_dict_keeps_no_parent_on_block():
    from nightdesk.worker._sdk_runner import _block_to_dict

    class ToolUseBlock:
        def __init__(self):
            self.id = "t1"
            self.name = "Glob"
            self.input = {"p": "*"}

    d = _block_to_dict(ToolUseBlock())
    assert d["type"] == "tool_use"
    # parent lives on the message, not the block
    assert "parent_tool_use_id" not in d


def test_event_to_dict_stamps_parent_on_assistant_tool_uses():
    from nightdesk.worker._sdk_runner import _event_to_dict

    class ToolUseBlock:
        def __init__(self):
            self.id = "t1"
            self.name = "Glob"
            self.input = {}

    class AssistantMessage:
        def __init__(self):
            self.content = [ToolUseBlock()]
            self.model = "m"
            self.usage = None
            self.parent_tool_use_id = "toolu_agent1"

    d = _event_to_dict(AssistantMessage())
    blocks = d["message"]["content"]
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["parent_tool_use_id"] == "toolu_agent1"


def test_event_to_dict_no_parent_when_top_level():
    from nightdesk.worker._sdk_runner import _event_to_dict

    class ToolUseBlock:
        def __init__(self):
            self.id = "t1"
            self.name = "Read"
            self.input = {}

    class AssistantMessage:
        def __init__(self):
            self.content = [ToolUseBlock()]
            self.model = "m"
            self.usage = None
            self.parent_tool_use_id = None

    d = _event_to_dict(AssistantMessage())
    assert "parent_tool_use_id" not in d["message"]["content"][0]


def test_event_to_dict_stamps_parent_on_user_tool_results():
    from nightdesk.worker._sdk_runner import _event_to_dict

    class ToolResultBlock:
        def __init__(self):
            self.tool_use_id = "t1"
            self.content = "ok"
            self.is_error = False

    class UserMessage:
        def __init__(self):
            self.content = [ToolResultBlock()]
            self.parent_tool_use_id = "toolu_agent1"

    d = _event_to_dict(UserMessage())
    blocks = d["message"]["content"]
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["parent_tool_use_id"] == "toolu_agent1"


def test_event_to_dict_text_blocks_not_stamped():
    """Text blocks inside a sub-agent message should NOT get parent_tool_use_id."""
    from nightdesk.worker._sdk_runner import _event_to_dict

    class TextBlock:
        def __init__(self):
            self.text = "hello"

    class AssistantMessage:
        def __init__(self):
            self.content = [TextBlock()]
            self.model = "m"
            self.usage = None
            self.parent_tool_use_id = "toolu_agent1"

    d = _event_to_dict(AssistantMessage())
    assert d["message"]["content"][0]["type"] == "text"
    assert "parent_tool_use_id" not in d["message"]["content"][0]


def test_event_to_dict_user_no_parent_when_top_level():
    from nightdesk.worker._sdk_runner import _event_to_dict

    class ToolResultBlock:
        def __init__(self):
            self.tool_use_id = "t1"
            self.content = "ok"
            self.is_error = False

    class UserMessage:
        def __init__(self):
            self.content = [ToolResultBlock()]
            self.parent_tool_use_id = None

    d = _event_to_dict(UserMessage())
    assert "parent_tool_use_id" not in d["message"]["content"][0]


def test_run_query_passes_resume_into_options():
    """A spec carrying ``resume`` (the continue intent) is threaded into
    ClaudeAgentOptions as ``resume=<id>`` on the very first query call, so the
    new run resumes the prior Claude Code conversation instead of starting
    fresh."""
    import asyncio
    import sys
    from types import ModuleType

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    captured: list[dict] = []

    async def fake_query(prompt: str, options: FakeOptions):  # type: ignore[return]
        captured.append(dict(options.kwargs))

        class ResultMessage:
            session_id = "sess-parent-7"
            subtype = "success"
            result = "continued"
            is_error = False
            usage = None
            model = None
        yield ResultMessage()

    sdk_mod = ModuleType("claude_agent_sdk")
    sdk_mod.query = fake_query  # type: ignore[attr-defined]
    sdk_mod.ClaudeAgentOptions = FakeOptions  # type: ignore[attr-defined]
    # Provide a sentinel CLIJSONDecodeError so the import guard is satisfied.
    class _SentinelDecodeError(Exception):
        pass
    sdk_mod.CLIJSONDecodeError = _SentinelDecodeError  # type: ignore[attr-defined]

    emitted: list[dict] = []

    async def emit(evt: dict) -> None:
        emitted.append(evt)

    saved_sdk = sys.modules.pop("claude_agent_sdk", None)
    saved_runner = sys.modules.pop("nightdesk.worker._sdk_runner", None)
    try:
        sys.modules["claude_agent_sdk"] = sdk_mod
        from nightdesk.worker._sdk_runner import _run_query
        rc = asyncio.run(_run_query(
            {"prompt": "keep going", "resume": "sess-parent-7"}, emit,
        ))
    finally:
        sys.modules.pop("claude_agent_sdk", None)
        sys.modules.pop("nightdesk.worker._sdk_runner", None)
        if saved_sdk is not None:
            sys.modules["claude_agent_sdk"] = saved_sdk
        if saved_runner is not None:
            sys.modules["nightdesk.worker._sdk_runner"] = saved_runner

    assert rc == 0, f"expected rc=0, got {rc}; emitted={emitted}"
    assert captured, "query was never called"
    # The first (and only) query call must carry resume=<parent session id>.
    assert captured[0].get("resume") == "sess-parent-7", (
        f"resume not wired into options: {captured[0]}"
    )


def test_run_query_recovers_on_cli_json_decode_error():
    """CLIJSONDecodeError mid-stream triggers a resume and the run succeeds.

    Stubs the first query invocation to yield a system init event carrying a
    session_id and then raise CLIJSONDecodeError (simulating an oversized tool
    result blowing the buffer). The second invocation (resume) yields a normal
    success result. Asserts:
    - overall rc is 0 (recovered)
    - second query call received ``resume=<session_id>`` in its options
    - a breadcrumb event of type "system"/subtype "buffer_overflow_skip" was emitted
    """
    import asyncio
    import sys
    from types import ModuleType

    class FakeCLIJSONDecodeError(Exception):
        pass

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    call_count = [0]
    resume_kwargs: list[dict] = []

    async def fake_query(prompt: str, options: FakeOptions):  # type: ignore[return]
        call_count[0] += 1
        if call_count[0] == 1:
            class SystemMessage:
                session_id = "sess-test-42"
                subtype = "init"
                data: dict = {}
            yield SystemMessage()
            raise FakeCLIJSONDecodeError("buffer exceeded")
        else:
            resume_kwargs.append(dict(options.kwargs))

            class ResultMessage:
                session_id = "sess-test-42"
                subtype = "success"
                result = "done"
                is_error = False
                usage = None
                model = None
            yield ResultMessage()

    sdk_mod = ModuleType("claude_agent_sdk")
    sdk_mod.query = fake_query  # type: ignore[attr-defined]
    sdk_mod.ClaudeAgentOptions = FakeOptions  # type: ignore[attr-defined]
    sdk_mod.CLIJSONDecodeError = FakeCLIJSONDecodeError  # type: ignore[attr-defined]

    emitted: list[dict] = []

    async def emit(evt: dict) -> None:
        emitted.append(evt)

    saved_sdk = sys.modules.pop("claude_agent_sdk", None)
    saved_runner = sys.modules.pop("nightdesk.worker._sdk_runner", None)
    try:
        sys.modules["claude_agent_sdk"] = sdk_mod
        from nightdesk.worker._sdk_runner import _run_query
        rc = asyncio.run(_run_query({"prompt": "do something"}, emit))
    finally:
        sys.modules.pop("claude_agent_sdk", None)
        sys.modules.pop("nightdesk.worker._sdk_runner", None)
        if saved_sdk is not None:
            sys.modules["claude_agent_sdk"] = saved_sdk
        if saved_runner is not None:
            sys.modules["nightdesk.worker._sdk_runner"] = saved_runner

    assert rc == 0, f"expected rc=0 after recovery, got {rc}; emitted={emitted}"
    assert len(resume_kwargs) == 1, "expected exactly one resume call"
    assert resume_kwargs[0].get("resume") == "sess-test-42", (
        f"resume kwarg missing or wrong: {resume_kwargs[0]}"
    )
    breadcrumb = next(
        (e for e in emitted
         if e.get("type") == "system" and e.get("subtype") == "buffer_overflow_skip"),
        None,
    )
    assert breadcrumb is not None, f"no breadcrumb emitted; emitted={emitted}"
    assert breadcrumb.get("data", {}).get("session_id") == "sess-test-42"


def test_run_query_cap_exhaustion_ends_gracefully():
    """Exceeding _MAX_BUFFER_RECOVERIES without succeeding stops cleanly.

    Stubs query to always raise CLIJSONDecodeError (after yielding a session_id
    once on the first call). After _MAX_BUFFER_RECOVERIES+1 total attempts the
    runner must emit a result/error event and return rc=1 without looping further.
    """
    import asyncio
    import sys
    from types import ModuleType

    from nightdesk.worker import _sdk_runner as _mod_ref
    max_recoveries = _mod_ref._MAX_BUFFER_RECOVERIES

    class FakeCLIJSONDecodeError(Exception):
        pass

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    call_count = [0]

    async def fake_query(prompt: str, options: FakeOptions):  # type: ignore[return]
        call_count[0] += 1
        if call_count[0] == 1:
            class SystemMessage:
                session_id = "sess-cap-99"
                subtype = "init"
                data: dict = {}
            yield SystemMessage()
        raise FakeCLIJSONDecodeError("always fails")

    sdk_mod = ModuleType("claude_agent_sdk")
    sdk_mod.query = fake_query  # type: ignore[attr-defined]
    sdk_mod.ClaudeAgentOptions = FakeOptions  # type: ignore[attr-defined]
    sdk_mod.CLIJSONDecodeError = FakeCLIJSONDecodeError  # type: ignore[attr-defined]

    emitted: list[dict] = []

    async def emit(evt: dict) -> None:
        emitted.append(evt)

    saved_sdk = sys.modules.pop("claude_agent_sdk", None)
    saved_runner = sys.modules.pop("nightdesk.worker._sdk_runner", None)
    try:
        sys.modules["claude_agent_sdk"] = sdk_mod
        from nightdesk.worker._sdk_runner import _run_query
        rc = asyncio.run(_run_query({"prompt": "do something"}, emit))
    finally:
        sys.modules.pop("claude_agent_sdk", None)
        sys.modules.pop("nightdesk.worker._sdk_runner", None)
        if saved_sdk is not None:
            sys.modules["claude_agent_sdk"] = saved_sdk
        if saved_runner is not None:
            sys.modules["nightdesk.worker._sdk_runner"] = saved_runner

    assert rc == 1, f"expected rc=1 after cap exhaustion, got {rc}"
    # Total query calls: 1 initial + max_recoveries resume attempts
    assert call_count[0] == max_recoveries + 1, (
        f"expected {max_recoveries + 1} calls, got {call_count[0]}"
    )
    final_errors = [
        e for e in emitted
        if e.get("type") == "result" and e.get("subtype") == "error"
    ]
    assert final_errors, f"no final error result emitted; emitted={emitted}"


def test_run_query_no_session_id_stops_gracefully():
    """CLIJSONDecodeError with no session_id captured stops without resuming."""
    import asyncio
    import sys
    from types import ModuleType

    class FakeCLIJSONDecodeError(Exception):
        pass

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    call_count = [0]

    async def fake_query(prompt: str, options: FakeOptions):  # type: ignore[return]
        call_count[0] += 1
        # Yield nothing with a session_id, then crash immediately
        raise FakeCLIJSONDecodeError("no session yet")
        yield  # make it an async generator  # noqa: unreachable

    sdk_mod = ModuleType("claude_agent_sdk")
    sdk_mod.query = fake_query  # type: ignore[attr-defined]
    sdk_mod.ClaudeAgentOptions = FakeOptions  # type: ignore[attr-defined]
    sdk_mod.CLIJSONDecodeError = FakeCLIJSONDecodeError  # type: ignore[attr-defined]

    emitted: list[dict] = []

    async def emit(evt: dict) -> None:
        emitted.append(evt)

    saved_sdk = sys.modules.pop("claude_agent_sdk", None)
    saved_runner = sys.modules.pop("nightdesk.worker._sdk_runner", None)
    try:
        sys.modules["claude_agent_sdk"] = sdk_mod
        from nightdesk.worker._sdk_runner import _run_query
        rc = asyncio.run(_run_query({"prompt": "do something"}, emit))
    finally:
        sys.modules.pop("claude_agent_sdk", None)
        sys.modules.pop("nightdesk.worker._sdk_runner", None)
        if saved_sdk is not None:
            sys.modules["claude_agent_sdk"] = saved_sdk
        if saved_runner is not None:
            sys.modules["nightdesk.worker._sdk_runner"] = saved_runner

    assert rc == 1
    # Must not have retried since no session_id was available
    assert call_count[0] == 1, f"should not retry without session_id; calls={call_count[0]}"
    final_errors = [
        e for e in emitted
        if e.get("type") == "result" and e.get("subtype") == "error"
    ]
    assert final_errors, f"no final error result emitted; emitted={emitted}"


def test_async_emitter_survives_oversized_event_on_nonblocking_pipe():
    """A >64 KiB event must not crash when stdout is a non-blocking pipe.

    Regression for the zai/glm-5.1 failures: the SDK puts our stdout (fd 1,
    dup'd to stderr via stderr=STDOUT in the parent) into non-blocking mode,
    so a single large write of a big tool_result exceeded the 64 KiB pipe
    buffer and raised BlockingIOError [Errno 11] ("write could not complete
    without blocking"). The async emitter must apply cooperative backpressure
    via drain() instead of crashing, and the bytes must round-trip intact.
    """
    import asyncio
    import json
    import os

    from nightdesk.worker._sdk_runner import _AsyncEmitter

    payload = {"type": "tool_result", "output": "x" * 200_000}
    expected = (json.dumps(payload, default=str) + "\n").encode("utf-8")

    async def scenario() -> bytes:
        r, w = os.pipe()
        os.set_blocking(w, False)  # mimic the SDK flipping stdout non-blocking
        loop = asyncio.get_running_loop()
        emitter = await _AsyncEmitter.connect(w)

        buf = bytearray()

        async def reader() -> None:
            # Small, slow reads force the writer to pause/resume repeatedly so
            # the test actually exercises the backpressure path.
            while len(buf) < len(expected):
                chunk = await loop.run_in_executor(None, os.read, r, 4096)
                if not chunk:
                    break
                buf.extend(chunk)
                await asyncio.sleep(0)

        rt = asyncio.create_task(reader())
        await emitter.emit(payload)  # raises BlockingIOError under a raw write
        await emitter.aclose()       # closes w -> reader sees EOF
        await rt
        os.close(r)
        return bytes(buf)

    assert asyncio.run(scenario()) == expected
