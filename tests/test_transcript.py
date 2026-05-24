"""Canonical transcript schema + translator tests."""
from __future__ import annotations

import io
import json
from pathlib import Path

from nightdesk.transcript import (
    KNOWN_TYPES, append_worker_error, is_canonical, next_seq, now_iso,
    read_events, write_event,
)
from nightdesk.worker.claude_translator import translate


def test_next_seq_monotonic():
    counter: list[int] = []
    assert next_seq(counter) == 0
    assert next_seq(counter) == 1
    assert next_seq(counter) == 2


def test_now_iso_format():
    ts = now_iso()
    # Should at least look like 'YYYY-MM-DDTHH:MM:SS...+00:00'.
    assert "T" in ts
    assert ts.endswith("+00:00")


def test_write_and_read_roundtrip(tmp_path: Path):
    path = tmp_path / "t.log"
    events = [
        {"type": "meta", "ts": now_iso(), "seq": 0,
         "run_id": "r1", "ticket_id": "tk1"},
        {"type": "assistant_text", "ts": now_iso(), "seq": 1, "text": "hi"},
        {"type": "tool_use", "ts": now_iso(), "seq": 2,
         "id": "u1", "tool": "Read", "input": {"file_path": "/x"}},
        {"type": "tool_result", "ts": now_iso(), "seq": 3,
         "tool_use_id": "u1", "output": "ok", "is_error": False},
        {"type": "result", "ts": now_iso(), "seq": 4,
         "subtype": "success", "summary": "done"},
    ]
    with path.open("ab") as f:
        for e in events:
            write_event(f, e)
    out = list(read_events(path))
    assert out == events


def test_read_events_skips_garbage(tmp_path: Path):
    path = tmp_path / "t.log"
    path.write_text(
        "{\"type\":\"meta\",\"seq\":0,\"ts\":\"now\"}\n"
        "not json\n"
        "[1,2,3]\n"  # parses but not a dict
        "{\"type\":\"assistant_text\",\"seq\":1,\"ts\":\"now\",\"text\":\"hi\"}\n"
    )
    out = list(read_events(path))
    assert [e["type"] for e in out] == ["meta", "assistant_text"]


def test_is_canonical_true_for_meta(tmp_path: Path):
    path = tmp_path / "t.log"
    path.write_text(
        "{\"type\":\"meta\",\"seq\":0,\"ts\":\"now\"}\n"
        "{\"type\":\"assistant_text\",\"seq\":1,\"ts\":\"now\",\"text\":\"hi\"}\n"
    )
    assert is_canonical(path) is True


def test_is_canonical_false_for_raw_sdk(tmp_path: Path):
    path = tmp_path / "t.log"
    path.write_text(
        "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"hi\"}]}}\n"
    )
    # 'assistant' is not in our canonical KNOWN_TYPES.
    assert is_canonical(path) is False


def test_worker_error_is_a_known_canonical_type():
    """The renderer dispatches off KNOWN_TYPES; the new worker_error event
    must be in the set so SSE-streamed events get a typed channel rather
    than the plain ``data:`` fallback."""
    assert "worker_error" in KNOWN_TYPES


def test_append_worker_error_sequences_after_existing_events(tmp_path: Path):
    """append_worker_error must place the event after the run's last seq so
    the worker-level failure visually lands at the end of the transcript."""
    path = tmp_path / "t.log"
    with path.open("ab") as f:
        write_event(f, {"type": "meta", "ts": now_iso(), "seq": 0,
                         "run_id": "r1", "ticket_id": "tk1"})
        write_event(f, {"type": "assistant_text", "ts": now_iso(), "seq": 1,
                         "text": "doing the thing"})
    append_worker_error(path, kind="executor_error",
                         summary="boom",
                         traceback_text="Traceback (most recent call last):\n  RuntimeError: boom")
    events = list(read_events(path))
    assert [e["type"] for e in events] == ["meta", "assistant_text", "worker_error"]
    final = events[-1]
    assert final["seq"] == 2
    assert final["kind"] == "executor_error"
    assert final["summary"] == "boom"
    assert "RuntimeError" in final["traceback"]


def test_append_worker_error_creates_file_when_missing(tmp_path: Path):
    """A setup failure may strike before the transcript file exists; the
    helper must still write the error event so the user has something to
    read in the transcript view."""
    path = tmp_path / "nested" / "t.log"
    append_worker_error(path, kind="setup_error", summary="cwd missing")
    events = list(read_events(path))
    assert len(events) == 1
    assert events[0]["type"] == "worker_error"
    assert events[0]["seq"] == 0
    assert events[0]["summary"] == "cwd missing"
    # traceback omitted when not provided.
    assert "traceback" not in events[0]


# --- Translator ------------------------------------------------------------


def test_translate_assistant_text():
    evt = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hello"},
    ]}}
    out = translate(evt)
    assert out == [{"type": "assistant_text", "text": "hello"}]


def test_translate_assistant_thinking():
    evt = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "reasoning..."},
    ]}}
    out = translate(evt)
    assert out == [{"type": "thinking", "text": "reasoning..."}]


def test_translate_assistant_thinking_empty_dropped():
    # Some Claude variants (e.g. claude-opus-4-7-fast-*) return thinking
    # blocks with empty content (the raw CoT never leaves the API). Those
    # should be dropped so the renderer doesn't show a misleading
    # "thinking (0 tokens)" placeholder card.
    evt = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "", "signature": "abc"},
        {"type": "text", "text": "hello"},
    ]}}
    out = translate(evt)
    assert out == [{"type": "assistant_text", "text": "hello"}]


def test_translate_assistant_thinking_whitespace_only_dropped():
    evt = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "   \n  "},
    ]}}
    assert translate(evt) == []


def test_translate_tool_use():
    evt = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "u1", "name": "Read",
         "input": {"file_path": "/x"}},
    ]}}
    out = translate(evt)
    assert out == [{
        "type": "tool_use", "id": "u1", "tool": "Read",
        "input": {"file_path": "/x"},
    }]


def test_translate_tool_result_with_string_content():
    evt = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "u1",
         "content": "file body", "is_error": False},
    ]}}
    out = translate(evt)
    assert out == [{
        "type": "tool_result", "tool_use_id": "u1",
        "output": "file body", "is_error": False, "truncated": False,
    }]


def test_translate_tool_result_with_list_content():
    evt = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "u1", "content": [
            {"type": "text", "text": "line 1"},
            {"type": "text", "text": "line 2"},
        ], "is_error": True},
    ]}}
    out = translate(evt)
    assert out[0]["output"] == "line 1\nline 2"
    assert out[0]["is_error"] is True


def test_translate_result_success():
    evt = {"type": "result", "subtype": "success", "result": "all done"}
    assert translate(evt) == [
        {"type": "result", "subtype": "success", "summary": "all done"}
    ]


def test_translate_result_error_mapped():
    evt = {"type": "result", "subtype": "error_max_turns", "result": "oops"}
    out = translate(evt)
    assert out[0]["subtype"] == "error"
    assert out[0]["summary"] == "oops"


def test_translate_unknown_block_falls_back_to_assistant_text():
    evt = {"type": "assistant", "message": {"content": [
        {"type": "weird_new_block", "payload": 42},
    ]}}
    out = translate(evt)
    assert out and out[0]["type"] == "assistant_text"
    assert "weird_new_block" in out[0]["text"]


def test_translate_system_event_dropped():
    assert translate({"type": "system", "subtype": "init"}) == []


def test_translate_already_canonical_passed_through():
    evt = {"type": "assistant_text", "text": "hi", "ts": "now", "seq": 3}
    assert translate(evt) == [evt]


def test_translate_handles_top_level_content_no_message_wrapper():
    evt = {"type": "assistant", "content": [
        {"type": "text", "text": "no wrapper"},
    ]}
    assert translate(evt) == [{"type": "assistant_text", "text": "no wrapper"}]


def test_translate_assistant_with_usage_emits_stats_event():
    evt = {"type": "assistant", "message": {
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 25,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 200,
        },
    }}
    out = translate(evt)
    assert out[0] == {"type": "assistant_text", "text": "hi"}
    assert out[1] == {
        "type": "stats", "scope": "turn", "model": "claude-opus-4-7",
        "input_tokens": 100, "output_tokens": 25,
        "cache_read_tokens": 5000, "cache_write_tokens": 200,
    }


def test_translate_assistant_without_usage_emits_no_stats_event():
    evt = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"},
    ]}}
    out = translate(evt)
    assert all(e.get("type") != "stats" for e in out)


def test_translate_result_emits_stats_event_with_cost():
    evt = {
        "type": "result", "subtype": "success", "result": "done",
        "model": "claude-sonnet-4-6",
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }
    out = translate(evt)
    assert out[0]["type"] == "result"
    stats = next(e for e in out if e.get("type") == "stats")
    assert stats["scope"] == "run"
    assert stats["input_tokens"] == 1000
    assert stats["output_tokens"] == 500
    assert stats["model"] == "claude-sonnet-4-6"
    assert "cost_usd" in stats


def test_translate_result_without_usage_emits_no_stats_event():
    evt = {"type": "result", "subtype": "success", "result": "done"}
    out = translate(evt)
    assert all(e.get("type") != "stats" for e in out)


def test_tool_events_accept_parent_tool_use_id():
    from nightdesk.transcript import ToolUseEvent, ToolResultEvent
    use: ToolUseEvent = {
        "type": "tool_use", "id": "t1", "tool": "Glob",
        "input": {}, "parent_tool_use_id": "toolu_agent1",
    }
    res: ToolResultEvent = {
        "type": "tool_result", "tool_use_id": "t1",
        "output": "x", "parent_tool_use_id": "toolu_agent1",
    }
    assert use["parent_tool_use_id"] == "toolu_agent1"
    assert res["parent_tool_use_id"] == "toolu_agent1"
