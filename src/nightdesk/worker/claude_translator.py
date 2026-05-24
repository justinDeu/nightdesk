"""Translator from Claude Agent SDK message shapes -> canonical events.

The SDK emits structured messages with shapes like:

    {"type": "assistant", "message": {"content": [{"type": "text", "text": ...},
                                                  {"type": "thinking", "thinking": ...},
                                                  {"type": "tool_use", "id": ..., "name": ..., "input": ...}]}}
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ..., "content": ..., "is_error": false}]}}
    {"type": "result", "subtype": "success" | "error_*", "result": "..."}

Some older SDK builds put the blocks at the top level (no "message" wrapper).
This translator handles both. Unknown content types map to a generic
``assistant_text`` so the renderer always has something to show.

``translate`` is a pure function: it does NOT add ``ts`` or ``seq``. The
caller is responsible for wrapping each event with those (and writing it
through ``nightdesk.transcript.write_event``).
"""
from __future__ import annotations

import json
from typing import Any


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _content_blocks(event: dict) -> list[Any]:
    """Extract the list of content blocks from an SDK event, robust to layout."""
    if not isinstance(event, dict):
        return []
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return content
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
    content = event.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _extract_usage(event: dict) -> dict | None:
    """Pull the usage dict out of an SDK assistant/result event, robust to layout."""
    usage = event.get("usage")
    if not isinstance(usage, dict) or not usage:
        msg = event.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    return usage


def _extract_model(event: dict) -> str | None:
    model = event.get("model")
    if isinstance(model, str) and model:
        return model
    msg = event.get("message")
    if isinstance(msg, dict):
        m = msg.get("model")
        if isinstance(m, str) and m:
            return m
    return None


def _stats_event_from_usage(usage: dict, *, model: str | None, scope: str) -> dict:
    """Build a canonical stats event from a CC SDK usage dict.

    ``scope`` is ``"turn"`` for per-assistant-message usage (incremental) or
    ``"run"`` for the cumulative totals carried on the final result event.
    """
    return {
        "type": "stats",
        "scope": scope,
        "model": model,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(
            usage.get("cache_read_input_tokens")
            or usage.get("cache_read_tokens")
            or 0
        ),
        "cache_write_tokens": int(
            usage.get("cache_creation_input_tokens")
            or usage.get("cache_write_tokens")
            or 0
        ),
    }


def _translate_assistant(event: dict) -> list[dict]:
    out: list[dict] = []
    for block in _content_blocks(event):
        if not isinstance(block, dict):
            out.append({"type": "assistant_text", "text": _stringify(block)})
            continue
        btype = block.get("type")
        if btype == "text":
            out.append({"type": "assistant_text", "text": block.get("text", "")})
        elif btype == "thinking":
            text = block.get("thinking") or block.get("text") or ""
            # Some Claude variants (e.g. claude-opus-4-7-fast-*) return
            # thinking blocks with empty `thinking` content and only a
            # `signature` — the raw chain-of-thought never leaves the
            # API. Drop those so the renderer doesn't show a misleading
            # "thinking (0 tokens)" card.
            if text.strip():
                out.append({"type": "thinking", "text": text})
        elif btype == "tool_use":
            out.append({
                "type": "tool_use",
                "id": block.get("id", ""),
                "tool": block.get("name") or block.get("tool") or "",
                "input": block.get("input") or {},
            })
        else:
            # Unknown block; preserve the raw json as assistant_text.
            out.append({"type": "assistant_text", "text": _stringify(block)})
    usage = _extract_usage(event)
    if usage is not None:
        out.append(_stats_event_from_usage(usage, model=_extract_model(event), scope="turn"))
    return out


def _translate_user(event: dict) -> list[dict]:
    out: list[dict] = []
    for block in _content_blocks(event):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                # Re-flatten list-of-text-blocks into a single string.
                parts: list[str] = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        parts.append(str(c.get("text", "")))
                    else:
                        parts.append(_stringify(c))
                output = "\n".join(parts)
            else:
                output = _stringify(content)
            out.append({
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "output": output,
                "is_error": bool(block.get("is_error", False)),
                "truncated": bool(block.get("truncated", False)),
            })
    return out


def _translate_result(event: dict) -> list[dict]:
    subtype_raw = str(event.get("subtype") or "success")
    subtype = "success" if subtype_raw == "success" else "error"
    summary = event.get("result") or event.get("summary") or ""
    out: list[dict] = [{
        "type": "result",
        "subtype": subtype,
        "summary": _stringify(summary),
    }]
    usage = _extract_usage(event)
    if usage is not None:
        model = _extract_model(event)
        stats = _stats_event_from_usage(usage, model=model, scope="run")
        try:
            from nightdesk.domain.cost import compute_cost
            stats["cost_usd"] = compute_cost(
                model=model,
                input_tokens=stats["input_tokens"],
                output_tokens=stats["output_tokens"],
                cache_read_tokens=stats["cache_read_tokens"],
                cache_write_tokens=stats["cache_write_tokens"],
            )
        except Exception:
            stats["cost_usd"] = None
        out.append(stats)
    return out


_SUBAGENT_PHASES = ("started", "progress", "notification")


def _translate_subagent(event: dict) -> list[dict]:
    """Normalize a sub-agent (Task tool) lifecycle event to one canonical event.

    The runner already collapses the three SDK message classes to a single
    ``subagent`` event discriminated by ``phase``; this keeps the well-known
    fields and drops nothing renderable. Per-``task_id`` collapse of the many
    ``progress`` events into one updating card happens later in the renderer
    (``transcript_view.pair_tool_events``) since ``translate`` is stateless and
    sees one event at a time.
    """
    phase = str(event.get("phase") or "progress")
    if phase not in _SUBAGENT_PHASES:
        phase = "progress"
    out: dict[str, Any] = {"type": "subagent", "phase": phase}
    for key in (
        "task_id", "tool_use_id", "subagent_type", "task_type",
        "description", "prompt", "status", "output_file", "summary",
        "last_tool_name", "usage", "raw",
    ):
        v = event.get(key)
        if v is not None and v != "":
            out[key] = v
    return [out]


def translate(event_dict: dict) -> list[dict]:
    """Translate one SDK event dict to a list of canonical event dicts.

    Returns ``[]`` if there is nothing renderable. Never raises for well-formed
    input.
    """
    if not isinstance(event_dict, dict):
        return []
    etype = event_dict.get("type")
    if etype == "assistant":
        return _translate_assistant(event_dict)
    if etype == "user":
        return _translate_user(event_dict)
    if etype == "result":
        return _translate_result(event_dict)
    if etype == "subagent":
        return _translate_subagent(event_dict)
    if etype == "system":
        # System init / config messages — not rendered.
        return []
    # Some SDKs emit raw shape that already matches our canonical form.
    if etype in {"assistant_text", "thinking", "tool_use", "tool_result",
                 "result", "meta", "rate_limit", "cancelled", "worker_error"}:
        return [dict(event_dict)]
    return []
