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
import json
import sys
from typing import Any


def _emit(evt: Any) -> None:
    sys.stdout.write(json.dumps(evt, default=str) + "\n")
    sys.stdout.flush()


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
    if name == "SystemMessage":
        return {
            "type": "system",
            "subtype": getattr(evt, "subtype", ""),
            "data": getattr(evt, "data", {}) or {},
        }
    if name == "AssistantMessage":
        msg: dict[str, Any] = {
            "content": [_block_to_dict(b) for b in (getattr(evt, "content", None) or [])],
        }
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
        if isinstance(content, list):
            content_out: Any = [_block_to_dict(b) for b in content]
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
        # when present for the badge + countdown, but ALWAYS attach a ``raw``
        # dump of the whole payload so the renderer can show the latest raw
        # response in a dropdown regardless of which fields this SDK provides.
        info = getattr(evt, "rate_limit_info", None)
        out_rl: dict[str, Any] = {"type": "rate_limit"}
        if info is not None:
            status = getattr(info, "status", None)
            if status is not None:
                out_rl["status"] = status
            resets_at = getattr(info, "resets_at", None)
            if resets_at is not None:
                out_rl["resets_at"] = resets_at
            limit_type = getattr(info, "rate_limit_type", None)
            if limit_type is not None:
                out_rl["rate_limit_type"] = limit_type
            utilization = getattr(info, "utilization", None)
            if utilization is not None:
                out_rl["utilization"] = utilization
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


async def _run(spec: dict[str, Any]) -> int:
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised via subprocess only
        _emit({"type": "result", "subtype": "error",
               "result": f"failed to import claude_agent_sdk: {exc}"})
        return 1

    opts_kwargs: dict[str, Any] = {}
    if spec.get("cwd"):
        opts_kwargs["cwd"] = spec["cwd"]
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
    # Isolate the worker from the user's global Claude Code config. Without
    # this, ~/.claude/settings.json + plugins (e.g. superpowers SessionStart
    # hooks, the brainstorming skill) get auto-loaded and push the agent into
    # interactive-style behavior — asking questions, refusing to implement
    # without a brainstorm, etc. "project" still loads any in-repo
    # .claude/settings.json and CLAUDE.md, which is desirable.
    opts_kwargs["setting_sources"] = ["project"]

    try:
        options = ClaudeAgentOptions(**opts_kwargs)
    except Exception as exc:
        _emit({"type": "result", "subtype": "error",
               "result": f"invalid options: {exc}"})
        return 1

    rc = 0
    try:
        async for evt in query(prompt=spec.get("prompt", ""), options=options):
            d = _event_to_dict(evt)
            if d is None:
                continue
            _emit(d)
            if d.get("type") == "result" \
                    and d.get("subtype") not in (None, "success"):
                rc = 1
    except Exception as exc:
        _emit({"type": "result", "subtype": "error",
               "result": f"query crashed: {exc}"})
        rc = 1
    return rc


def main() -> int:
    raw = sys.stdin.read()
    try:
        spec = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        _emit({"type": "result", "subtype": "error",
               "result": f"bad spec json: {exc}"})
        return 1
    return asyncio.run(_run(spec))


if __name__ == "__main__":
    sys.exit(main())
