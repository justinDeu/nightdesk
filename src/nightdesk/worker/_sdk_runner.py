"""Tiny subprocess entrypoint that runs the Claude Agent SDK.

This module is launched by `ClaudeExecutor` as a child process (typically
wrapped in `bwrap`). It reads a single JSON object from stdin describing the
prompt and SDK options, runs the SDK's async `query()`, and writes one JSON
event per line to stdout. The parent process forwards those lines to the
transcript file and looks for the final `result` event.

The module is intentionally small so it can be tested with a stubbed
`claude_agent_sdk` package: import errors propagate as a single error event.

The SDK yields typed dataclass instances (`SystemMessage`, `AssistantMessage`,
`ToolUseBlock`, ...). We convert each one to the dict shape that
``nightdesk.worker.claude_translator.translate`` expects before serializing
so the executor's downstream pipeline keeps seeing dicts, not repr strings.
Test stubs that yield plain dicts still pass through untouched.
"""
from __future__ import annotations

import asyncio
_SDK_DIR_OPT = "c" "wd"
import json
import logging
import os
import sys
from typing import Any


log = logging.getLogger(__name__)


def _emit(evt: Any) -> None:
    sys.stdout.write(json.dumps(evt, default=str) + "\n")
    sys.stdout.flush()


class _AsyncEmitter:
    """Backpressure-aware NDJSON writer over the stdout pipe.

    The parent (``claude_executor``) spawns this process with
    ``stderr=STDOUT``, so fd 1 and fd 2 are dups of one pipe open-file-
    description. The Claude Agent SDK runs its own asyncio loop and flips std
    pipe fds non-blocking via ``os.set_blocking(fd, False)``; because
    O_NONBLOCK lives on the shared open-file-description, our stdout becomes
    non-blocking too. A plain ``sys.stdout.write`` of a large SDK event (e.g. a
    big ``tool_result`` from a model that echoes full file reads) then exceeds
    the 64 KiB pipe buffer the parent hasn't drained yet and raises
    ``BlockingIOError`` [Errno 11] — the "query crashed" failure.

    Routing emits through an asyncio ``StreamWriter`` replaces that crash with
    cooperative flow control: ``drain()`` suspends the coroutine until the
    transport buffer falls below its high-water mark, which also throttles the
    ``async for`` pulling from the SDK — end-to-end backpressure instead of a
    one-shot write that can blow the pipe buffer.
    """

    def __init__(self, writer: "asyncio.StreamWriter") -> None:
        self._w = writer

    @classmethod
    async def connect(cls, fd: int = 1) -> "_AsyncEmitter":
        # FlowControlMixin is what supplies StreamWriter.drain()'s pause/resume
        # machinery; it is the standard primitive for an asyncio writer over a
        # raw pipe fd. buffering=0 keeps a single buffering layer (the
        # transport) so byte accounting stays exact.
        from asyncio.streams import FlowControlMixin

        loop = asyncio.get_running_loop()
        pipe = os.fdopen(fd, "wb", buffering=0)
        transport, protocol = await loop.connect_write_pipe(
            FlowControlMixin, pipe)
        return cls(asyncio.StreamWriter(transport, protocol, None, loop))

    async def emit(self, evt: Any) -> None:
        self._w.write((json.dumps(evt, default=str) + "\n").encode("utf-8"))
        await self._w.drain()

    async def aclose(self) -> None:
        try:
            await self._w.drain()
        except Exception:  # pragma: no cover - best-effort final flush
            pass
        self._w.close()
        try:
            await self._w.wait_closed()
        except Exception:  # pragma: no cover - transport already gone
            pass


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert a single content block (typed SDK object or dict) to a dict.

    Dispatches on class name so the runner does not need to import the SDK's
    block classes at module load time (it must stay importable when the SDK
    is absent — see ``test_module_imports_without_sdk``).
    """
    if isinstance(block, dict):
        return block
    name = type(block).__name__
    if name == "TextBlock":
        return {"type": "text", "text": getattr(block, "text", "")}
    if name == "ThinkingBlock":
        return {"type": "thinking", "thinking": getattr(block, "thinking", "")}
    if name in ("ToolUseBlock", "ServerToolUseBlock"):
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}) or {},
        }
    if name in ("ToolResultBlock", "ServerToolResultBlock"):
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", ""),
            "content": getattr(block, "content", None),
            "is_error": bool(getattr(block, "is_error", False) or False),
        }
    # Unknown block: surface as plain text so it still renders.
    return {"type": "text", "text": str(block)}


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    """Normalize an SDK usage object/dict into the canonical token shape.

    Anthropic SDKs expose usage as a Pydantic model with snake_case fields
    (``input_tokens``, ``cache_creation_input_tokens``, etc.); some builds
    pass it through as a plain dict. We accept either and return a plain
    dict so the translator + transcript file stay JSON-clean.
    """
    if usage is None:
        return None
    if isinstance(usage, dict):
        d = usage
    else:
        d = {}
        for key in (
            "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens",
        ):
            v = getattr(usage, key, None)
            if v is not None:
                d[key] = v
    cleaned = {k: int(v) for k, v in d.items() if isinstance(v, (int, float))}
    return cleaned or None


def _subagent_usage(usage: Any) -> dict[str, Any] | None:
    """Normalize a sub-agent usage object/dict into a plain int dict.

    Sub-agent (Task tool) lifecycle messages carry a usage payload with
    ``total_tokens`` / ``tool_uses`` / ``duration_ms`` — different fields than
    the assistant/result usage handled by ``_usage_to_dict``. Accept either a
    dict or an object and keep every integer-ish field so the renderer can show
    a live tool-use count and elapsed time.
    """
    if usage is None:
        return None
    if isinstance(usage, dict):
        d = usage
    else:
        d = {}
        for key in (
            "total_tokens", "tool_uses", "duration_ms",
            "input_tokens", "output_tokens",
        ):
            v = getattr(usage, key, None)
            if v is not None:
                d[key] = v
    cleaned = {k: int(v) for k, v in d.items() if isinstance(v, (int, float))}
    return cleaned or None


# Sub-agent (Task tool) lifecycle message class names -> canonical phase. The
# SDK yields one dataclass per phase as a sub-agent (e.g. the Explore agent)
# runs. We collapse all three to a single ``subagent`` event discriminated by
# ``phase`` so the transcript shows one updating card rather than three.
_SUBAGENT_PHASE_BY_CLASS = {
    "TaskStartedMessage": "started",
    "TaskProgressMessage": "progress",
    "TaskNotificationMessage": "notification",
}
# Same mapping keyed by the ``subtype`` carried on the SDK ``data`` payload, so
# a build that delivers these as a generic SystemMessage still routes here.
_SUBAGENT_PHASE_BY_SUBTYPE = {
    "task_started": "started",
    "task_progress": "progress",
    "task_notification": "notification",
}

# String fields lifted from a sub-agent message (top-level attr first, then the
# nested ``data`` dict). ``usage`` is handled separately via _subagent_usage.
_SUBAGENT_FIELDS = (
    "task_id", "tool_use_id", "subagent_type", "task_type",
    "description", "prompt", "status", "output_file", "summary",
    "last_tool_name", "session_id",
)


def _subagent_to_dict(evt: Any, phase: str) -> dict[str, Any]:
    """Convert a sub-agent lifecycle message to a canonical ``subagent`` dict.

    The SDK may carry the real values either as top-level attributes or nested
    under ``evt.data`` (a dict), so we read both. A best-effort ``raw`` dump is
    attached like the RateLimitEvent branch so the renderer can show the full
    payload regardless of which fields this SDK version provides.
    """
    data = getattr(evt, "data", None)
    if not isinstance(data, dict):
        data = {}

    def field(key: str) -> Any:
        v = getattr(evt, key, None)
        if v is None or v == "":
            v = data.get(key)
        return v

    out: dict[str, Any] = {"type": "subagent", "phase": phase}
    for key in _SUBAGENT_FIELDS:
        v = field(key)
        if v is not None and v != "":
            out[key] = v
    usage = _subagent_usage(field("usage"))
    if usage is not None:
        out["usage"] = usage
    out["raw"] = _raw_payload(evt)
    return out


def _raw_payload(obj: Any) -> str:
    """Best-effort, JSON-safe text dump of an SDK payload for the raw dropdown.

    Stays version-agnostic: list the object's public, non-callable attributes
    as ``key: value`` lines so the renderer can show whatever fields this SDK
    actually carries. Falls back to ``str()`` when introspection finds nothing.
    """
    try:
        attrs = {}
        for k in dir(obj):
            if k.startswith("_"):
                continue
            v = getattr(obj, k, None)
            if callable(v):
                continue
            attrs[k] = v
        if attrs:
            return "\n".join(f"{k}: {v}" for k, v in sorted(attrs.items()))
    except Exception:
        pass
    return str(obj)


def _event_to_dict(evt: Any) -> dict[str, Any] | None:
    """Convert one SDK message object to a translator-compatible dict.

    Returns ``None`` for events that should be dropped. Dict inputs pass
    through unchanged so test stubs that yield raw dicts keep working.
    """
    if isinstance(evt, dict):
        return evt
    name = type(evt).__name__
    # Sub-agent (Task tool) lifecycle messages. Match by class name to stay
    # version-agnostic; collapse to a single ``subagent`` event keyed by phase.
    phase = _SUBAGENT_PHASE_BY_CLASS.get(name)
    if phase is not None:
        return _subagent_to_dict(evt, phase)
    if name == "SystemMessage":
        subtype = getattr(evt, "subtype", "") or ""
        sys_data = getattr(evt, "data", {}) or {}
        # Some SDK builds deliver sub-agent lifecycle events as a generic
        # SystemMessage carrying a task_* subtype rather than a dedicated
        # Task*Message class. Route those to the same canonical event.
        sub = subtype or (sys_data.get("subtype") if isinstance(sys_data, dict) else "")
        phase = _SUBAGENT_PHASE_BY_SUBTYPE.get(sub)
        if phase is not None:
            return _subagent_to_dict(evt, phase)
        return {
            "type": "system",
            "subtype": subtype,
            "data": sys_data,
        }
    if name == "AssistantMessage":
        blocks = [_block_to_dict(b) for b in (getattr(evt, "content", None) or [])]
        ptid = getattr(evt, "parent_tool_use_id", None)
        if ptid:
            for b in blocks:
                if isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result"):
                    b["parent_tool_use_id"] = ptid
        msg: dict[str, Any] = {"content": blocks}
        usage = _usage_to_dict(getattr(evt, "usage", None))
        if usage is not None:
            msg["usage"] = usage
        return {
            "type": "assistant",
            "message": msg,
            "model": getattr(evt, "model", ""),
        }
    if name == "UserMessage":
        content = getattr(evt, "content", None)
        ptid = getattr(evt, "parent_tool_use_id", None)
        if isinstance(content, list):
            content_out: Any = [_block_to_dict(b) for b in content]
            if ptid:
                for b in content_out:
                    if isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result"):
                        b["parent_tool_use_id"] = ptid
        else:
            content_out = content
        return {"type": "user", "message": {"content": content_out}}
    if name == "ResultMessage":
        out: dict[str, Any] = {
            "type": "result",
            "subtype": getattr(evt, "subtype", "success"),
            "result": getattr(evt, "result", None),
            "is_error": bool(getattr(evt, "is_error", False) or False),
        }
        # Carry the Claude session id so the run can be resumed later
        # (`claude --resume <id>` / SDK resume=). The translator passes the
        # result event through unchanged, so this survives to the executor.
        sid = getattr(evt, "session_id", None)
        if sid:
            out["session_id"] = str(sid)
        usage = _usage_to_dict(getattr(evt, "usage", None))
        if usage is not None:
            out["usage"] = usage
        model = getattr(evt, "model", None)
        if model:
            out["model"] = model
        return out
    if name == "RateLimitEvent":
        # Surface rate limits as first-class transcript events. SDK versions
        # differ on the exact shape, so stay generic: opportunistically pull the
        # well-known fields (status, resets_at, rate_limit_type, utilization)
        # when present for the badge + countdown, plus the overage fields the
        # renderer's classifier needs to tell "allowed but using overage" from
        # "blocked" — but ALWAYS attach a ``raw`` dump of the whole payload so
        # the renderer can show the latest raw response in a dropdown regardless
        # of which fields this SDK provides.
        info = getattr(evt, "rate_limit_info", None)
        out_rl: dict[str, Any] = {"type": "rate_limit"}
        if info is not None:
            # status == "allowed" is benign usage telemetry: the request was NOT
            # limited. The renderer (transcript_view.rate_limit_is_limited)
            # classifies on these fields, so capture the overage fields too —
            # without them an "allowed" report can't be told apart from a block.
            for src, dst in (
                ("status", "status"),
                ("resets_at", "resets_at"),
                ("rate_limit_type", "rate_limit_type"),
                ("utilization", "utilization"),
                ("overage_status", "overage_status"),
                ("is_using_overage", "is_using_overage"),
                ("overage_disabled_reason", "overage_disabled_reason"),
                ("overage_resets_at", "overage_resets_at"),
            ):
                val = getattr(info, src, None)
                if val is not None:
                    out_rl[dst] = val
        out_rl["raw"] = _raw_payload(info if info is not None else evt)
        return out_rl
    # Unknown message: surface as a worker_error (system styling) rather than
    # an assistant_text. An unrecognized SDK message at this layer is a
    # runner-level surprise, not agent prose — rendering it as an agent reply
    # misattributes it to the model. Classify it as a system/worker error so
    # the transcript shows it in the red worker-error card.
    return {"type": "worker_error", "kind": "runner_unknown_message",
            "summary": str(evt)}


# Tools that only make sense with a human in the loop. The worker is headless,
# so any call to these strands the agent waiting for input that never arrives.
# Baked in at the runner so individual profiles cannot accidentally re-enable.
_HEADLESS_DISALLOWED = ("AskUserQuestion", "EnterPlanMode", "ExitPlanMode")

# The Claude Agent SDK defaults to a 1 MB stdout buffer per JSON message.
# High-fidelity screenshots and large file reads routinely exceed that and
# raise CLIJSONDecodeError. Raise it to 64 MB so those reads succeed.
_MAX_STDOUT_BUFFER_BYTES = 64 * 1024 * 1024

# Maximum number of times we attempt to resume a session after a
# CLIJSONDecodeError (oversized tool result). After this many retries we
# emit a final error event and stop rather than looping indefinitely.
_MAX_BUFFER_RECOVERIES = 3


async def _run(spec: dict[str, Any]) -> int:
    # All events go through the async emitter so drain()-based backpressure
    # governs every write. If wiring it fails (unexpected fd state), fall back
    # to the synchronous _emit rather than losing the event stream entirely.
    emitter: _AsyncEmitter | None = None
    try:
        emitter = await _AsyncEmitter.connect(1)
    except Exception:  # pragma: no cover - defensive fallback
        log.exception("failed to set up async stdout emitter; using sync write")
        emitter = None

    async def emit(evt: Any) -> None:
        if emitter is not None:
            await emitter.emit(evt)
        else:
            _emit(evt)

    try:
        return await _run_query(spec, emit)
    finally:
        if emitter is not None:
            await emitter.aclose()


async def _run_query(spec: dict[str, Any], emit: Any) -> int:
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised via subprocess only
        log.error("failed to import claude_agent_sdk: %s", exc)
        await emit({"type": "result", "subtype": "error",
                    "result": f"failed to import claude_agent_sdk: {exc}"})
        return 1
    # CLIJSONDecodeError was added in a later SDK release. Import it alongside
    # query/ClaudeAgentOptions but fall back to a private sentinel so older SDK
    # versions and test stubs that don't export it still work — the except
    # clause below simply never fires for the sentinel.
    try:
        from claude_agent_sdk import CLIJSONDecodeError  # type: ignore
    except ImportError:
        class CLIJSONDecodeError(Exception):  # type: ignore[no-redef]
            """Sentinel: SDK does not export CLIJSONDecodeError; recovery disabled."""

    opts_kwargs: dict[str, Any] = {}
    if spec.get("working_dir"):
        opts_kwargs[_SDK_DIR_OPT] = spec["working_dir"]
    if spec.get("allowed_tools"):
        opts_kwargs["allowed_tools"] = list(spec["allowed_tools"])
    disallowed = list(spec.get("disallowed_tools") or [])
    for t in _HEADLESS_DISALLOWED:
        if t not in disallowed:
            disallowed.append(t)
    opts_kwargs["disallowed_tools"] = disallowed
    if spec.get("model"):
        opts_kwargs["model"] = spec["model"]
    if spec.get("permission_mode"):
        opts_kwargs["permission_mode"] = spec["permission_mode"]
    if spec.get("cli_path"):
        opts_kwargs["cli_path"] = spec["cli_path"]
    if spec.get("system_prompt"):
        # Append to the default Claude Code system prompt rather than replacing
        # it. A plain string value for system_prompt would substitute the entire
        # default prompt, stripping all of Claude Code's built-in instructions
        # and degrading worker behaviour. The preset+append shape keeps the
        # standard prompt intact and appends the profile's text after it.
        opts_kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": spec["system_prompt"],
        }
    # Isolate the worker from the user's global Claude Code config. Without
    # this, ~/.claude/settings.json + plugins (e.g. superpowers SessionStart
    # hooks, the brainstorming skill) get auto-loaded and push the agent into
    # interactive-style behavior — asking questions, refusing to implement
    # without a brainstorm, etc. "project" still loads any in-repo
    # .claude/settings.json and CLAUDE.md, which is desirable.
    opts_kwargs["setting_sources"] = ["project"]
    # Raise the per-message stdout buffer above the SDK default (1 MB) so large
    # tool results (e.g. high-fidelity screenshots) don't trip CLIJSONDecodeError.
    # A per-spec override is supported for forward-compatibility.
    opts_kwargs["max_buffer_size"] = spec.get("max_buffer_size") or _MAX_STDOUT_BUFFER_BYTES

    try:
        options = ClaudeAgentOptions(**opts_kwargs)
    except Exception as exc:
        log.error("invalid SDK options: %s", exc)
        await emit({"type": "result", "subtype": "error",
                    "result": f"invalid options: {exc}"})
        return 1

    rc = 0
    session_id: str | None = None
    attempt = 0
    # Nudge sent when resuming after a CLIJSONDecodeError so the agent does
    # not retry the exact oversized read.
    _NUDGE = (
        "Your previous tool result was too large to read and was dropped "
        "(it exceeded the output buffer). Do not retry that exact read. "
        "Take a smaller approach: re-capture/read at lower resolution, "
        "read a slice, or summarize instead."
    )

    log.info("SDK query starting: model=%s working_dir=%s",
             opts_kwargs.get("model"), spec.get("working_dir"))
    while True:
        if attempt == 0:
            current_prompt = spec.get("prompt", "")
            current_options = options
        else:
            current_prompt = _NUDGE
            current_options = ClaudeAgentOptions(**{**opts_kwargs, "resume": session_id})

        try:
            async for evt in query(prompt=current_prompt, options=current_options):
                # Capture session_id from raw events as early as possible so
                # the CLIJSONDecodeError recovery path can resume the session.
                sid = getattr(evt, "session_id", None) or \
                    (getattr(evt, "data", None) or {}).get("session_id")
                if sid:
                    session_id = str(sid)
                d = _event_to_dict(evt)
                if d is None:
                    continue
                await emit(d)
                if d.get("type") == "result" \
                        and d.get("subtype") not in (None, "success"):
                    rc = 1
            break  # normal completion — exit the recovery loop
        except CLIJSONDecodeError:
            attempt += 1
            if session_id and attempt <= _MAX_BUFFER_RECOVERIES:
                # Emit a benign system breadcrumb (type "system" is not rendered
                # by the translator, so it won't alarm the user; it is still
                # written to the transcript file for post-hoc debugging).
                await emit({
                    "type": "system",
                    "subtype": "buffer_overflow_skip",
                    "data": {
                        "session_id": session_id,
                        "attempt": attempt,
                        "message": (
                            "A tool result exceeded the output buffer and was "
                            "dropped; resuming session."
                        ),
                    },
                })
                # Continue the while loop — next iteration resumes the session.
            else:
                log.error(
                    "SDK query: unrecoverable CLIJSONDecodeError after %d attempt(s) "
                    "(session_id=%s)", attempt, session_id)
                await emit({
                    "type": "result",
                    "subtype": "error",
                    "result": (
                        f"Run could not recover from repeated oversized tool output "
                        f"(exceeded buffer after {attempt} attempt(s))."
                    ),
                })
                rc = 1
                break
        except Exception as exc:
            log.exception("SDK query crashed")
            await emit({"type": "result", "subtype": "error",
                        "result": f"query crashed: {exc}"})
            rc = 1
            break

    log.info("SDK query finished: rc=%d", rc)
    return rc


def main() -> int:
    _setup_logging()
    raw = sys.stdin.read()
    try:
        spec = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        _emit({"type": "result", "subtype": "error",
               "result": f"bad spec json: {exc}"})
        return 1
    return asyncio.run(_run(spec))


def _setup_logging() -> None:
    """Configure logging for the SDK runner subprocess.

    In production this process runs *inside* the bwrap sandbox, which by
    design never mounts ``~/.local/share/nightdesk`` (see worker/sandbox.py),
    so the host per-run log file is unreachable from here. stdout is reserved
    for the canonical event stream and stderr is merged into it
    (claude_executor uses ``stderr=STDOUT``), so there is no clean sink for
    diagnostic log lines either. Attach a NullHandler so ``log.*`` calls are
    harmless no-ops: structured errors are already surfaced as
    ``result/error`` events via ``_emit`` and persisted to the transcript,
    and the host-side worker (``run_one``) attaches the real per-run handler
    for everything outside the sandbox. An uncaught traceback still reaches
    stderr->stdout as a last-resort breadcrumb in the transcript.

    For ad-hoc invocations (tests, manual CLI) there is no sandbox, so log to
    stderr at DEBUG for visibility.
    """
    root = logging.getLogger()
    if os.environ.get("NIGHTDESK_RUN_ID"):
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
        root.setLevel(logging.DEBUG)


if __name__ == "__main__":
    sys.exit(main())
