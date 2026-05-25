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
