"""Translate opencode SSE events into nightdesk's canonical transcript events.

The canonical schema (``nightdesk.transcript``) is Claude-shaped:
``assistant_text``, ``thinking``, ``tool_use``, ``tool_result``,
``worker_error`` (plus ``meta`` / ``result`` written by the driver). opencode
streams text and reasoning parts *cumulatively* and tool parts as a
status machine, so translation is stateful: the caller threads a ``state``
dict (see :func:`new_state`) across events to emit only deltas and fire each
tool once.

Pure and side-effect-free apart from mutating the passed-in ``state`` —
which makes the mapping unit-testable without a live server.
"""
from __future__ import annotations

import json
from typing import Any


def new_state() -> dict:
    return {
        "text_len": {},        # part id -> chars of text already emitted
        "reasoning_len": {},    # part id -> chars of reasoning already emitted
        "tools_started": set(),
        "tools_done": set(),
        "error": None,          # last session.error message, if any
        "final_text": "",       # latest full assistant text (for the result)
        "usage": None,          # latest {tokens, cost, model} from message.updated
        # message id -> latest {model, input_tokens, output_tokens,
        # cache_read_tokens, cache_write_tokens} for that message. Keyed by
        # message id (not model) because ``message.updated`` re-fires with
        # cumulative-within-message totals as a reply streams — keying by
        # message id and keeping only the latest snapshot per id avoids
        # double-counting those intra-message updates when summed per model
        # in :func:`usage_by_model`. Falls back to the model id itself when
        # a message carries no id, which degrades to one accumulator per
        # model (last value wins), matching ``usage`` above.
        "usage_by_message": {},
    }


def usage_by_model(state: dict) -> dict[str, dict[str, int]]:
    """Sum the per-message usage snapshots in ``state`` by model id.

    Returns ``{}`` when no ``message.updated`` event ever carried usable
    ``modelID`` + token data (e.g. a run that failed before the agent
    produced a message). Safe to call at any point during or after a drive.
    """
    totals: dict[str, dict[str, int]] = {}
    for entry in state.get("usage_by_message", {}).values():
        model = entry.get("model")
        if not model:
            continue
        agg = totals.setdefault(model, {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        })
        agg["input_tokens"] += entry["input_tokens"]
        agg["output_tokens"] += entry["output_tokens"]
        agg["cache_read_tokens"] += entry["cache_read_tokens"]
        agg["cache_write_tokens"] += entry["cache_write_tokens"]
    return totals


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def translate_event(evt: dict, state: dict) -> list[dict]:
    """Return canonical events for one opencode SSE event (may be empty)."""
    if not isinstance(evt, dict):
        return []
    etype = evt.get("type")
    props = evt.get("properties") or {}

    if etype == "message.part.updated":
        return _translate_part(props.get("part") or {}, state)

    if etype == "message.updated":
        info = props.get("info") or {}
        tokens = info.get("tokens")
        model = info.get("modelID")
        if tokens is not None or info.get("cost") is not None:
            state["usage"] = {
                "tokens": tokens or {},
                "cost": info.get("cost"),
                "model": model,
            }
            mid = info.get("id") or model
            if mid:
                cache = (tokens or {}).get("cache") or {}
                state["usage_by_message"][mid] = {
                    "model": model,
                    "input_tokens": int((tokens or {}).get("input") or 0),
                    "output_tokens": int((tokens or {}).get("output") or 0),
                    "cache_read_tokens": int(cache.get("read") or 0),
                    "cache_write_tokens": int(cache.get("write") or 0),
                }
        return []

    if etype == "session.error":
        err = props.get("error") or {}
        name = err.get("name") or "opencode_error"
        data = err.get("data") or {}
        msg = data.get("message") or name
        state["error"] = msg
        return [{"type": "worker_error", "kind": name, "summary": msg}]

    return []


def _translate_part(part: dict, state: dict) -> list[dict]:
    ptype = part.get("type")
    pid = part.get("id") or part.get("callID") or ptype or "part"

    if ptype == "text":
        text = part.get("text") or ""
        prev = state["text_len"].get(pid, 0)
        if len(text) <= prev:
            return []
        delta = text[prev:]
        state["text_len"][pid] = len(text)
        state["final_text"] = text
        if not delta.strip():
            return []
        return [{"type": "assistant_text", "text": delta}]

    if ptype == "reasoning":
        text = part.get("text") or ""
        prev = state["reasoning_len"].get(pid, 0)
        if len(text) <= prev:
            return []
        delta = text[prev:]
        state["reasoning_len"][pid] = len(text)
        if not delta.strip():
            return []
        return [{"type": "thinking", "text": delta}]

    if ptype == "tool":
        call = part.get("callID") or part.get("id") or pid
        tool = part.get("tool") or "tool"
        st = part.get("state") or {}
        status = st.get("status")
        if status == "running" and call not in state["tools_started"]:
            state["tools_started"].add(call)
            return [{
                "type": "tool_use", "id": call, "tool": tool,
                "input": st.get("input") or {},
            }]
        if status in ("completed", "error") and call not in state["tools_done"]:
            state["tools_done"].add(call)
            is_error = status == "error"
            output = st.get("error") if is_error else st.get("output")
            return [{
                "type": "tool_result", "tool_use_id": call,
                "output": _stringify(output), "is_error": is_error,
            }]
        return []

    return []
