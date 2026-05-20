"""Canonical NDJSON transcript schema and helpers.

A transcript file is a sequence of newline-delimited JSON events. Each event
carries at minimum:

- ``type`` — one of ``meta | assistant_text | thinking | tool_use |
  tool_result | result``.
- ``ts`` — ISO 8601 UTC timestamp.
- ``seq`` — monotonic per-run integer (starting at 0).

The schema is intentionally narrow so future runners (other agent SDKs) can
emit equivalent events without changes to the UI renderer.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Iterator, Literal, TypedDict


CanonicalEventType = Literal[
    "meta", "assistant_text", "thinking", "tool_use", "tool_result", "result",
    "worker_error", "rate_limit",
]


KNOWN_TYPES: frozenset[str] = frozenset(
    {"meta", "assistant_text", "thinking", "tool_use", "tool_result", "result",
     "worker_error", "rate_limit", "stats"}
)


class _BaseEvent(TypedDict, total=False):
    type: str
    ts: str
    seq: int


class MetaEvent(_BaseEvent, total=False):
    type: Literal["meta"]
    run_id: str
    ticket_id: str


class AssistantTextEvent(_BaseEvent, total=False):
    type: Literal["assistant_text"]
    text: str


class ThinkingEvent(_BaseEvent, total=False):
    type: Literal["thinking"]
    text: str


class ToolUseEvent(_BaseEvent, total=False):
    type: Literal["tool_use"]
    id: str
    tool: str
    input: dict


class ToolResultEvent(_BaseEvent, total=False):
    type: Literal["tool_result"]
    tool_use_id: str
    output: str
    is_error: bool
    truncated: bool


class ResultEvent(_BaseEvent, total=False):
    type: Literal["result"]
    subtype: Literal["success", "error"]
    summary: str


class WorkerErrorEvent(_BaseEvent, total=False):
    """Nightdesk-level failure that occurred outside the agent's own output.

    Emitted as the final event of a failed run so the transcript view can
    surface the worker error (executor crash, sdk runner non-zero exit,
    headless policy escape, transcript drain failure, etc.) instead of
    burying it in a separate ticket comment.

    ``kind`` is a short machine tag (``executor_error``,
    ``runner_exit_nonzero``, ``headless_requested_input``, ``drain_error``).
    ``summary`` is the one-line user-facing message. ``traceback`` is the
    full Python stacktrace when one is available; absent for non-exception
    failures like an unexpected non-zero exit code.
    """

    type: Literal["worker_error"]
    kind: str
    summary: str
    traceback: str


class RateLimitEvent(_BaseEvent, total=False):
    """A rate-limit status change surfaced from the agent SDK.

    ``status`` is the CLI's rate-limit status (``allowed`` | ``allowed_warning``
    | ``rejected``). ``resets_at`` is the Unix timestamp (seconds) the limit
    window resets — the renderer ticks a countdown down to it. ``rate_limit_type``
    names the window (``five_hour``, ``seven_day``, ...) and ``utilization`` is
    the fraction (0.0-1.0) of the window consumed.
    """

    type: Literal["rate_limit"]
    status: str
    resets_at: int
    rate_limit_type: str
    utilization: float


def now_iso() -> str:
    """ISO 8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def next_seq(counter: list[int]) -> int:
    """Returns the next monotonic seq. Pass an ``[int]`` accumulator.

    Using a list-of-int is a small trick to avoid leaking a stateful Counter
    object across the module boundary.
    """
    if not counter:
        counter.append(0)
    v = counter[0]
    counter[0] = v + 1
    return v


def write_event(file_handle: IO[bytes], event: dict) -> None:
    """Append one canonical event as NDJSON to ``file_handle`` (opened in 'ab')."""
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    file_handle.write(line.encode("utf-8"))
    file_handle.write(b"\n")
    try:
        file_handle.flush()
    except Exception:
        pass


def read_events(path: str | Path) -> Iterator[dict]:
    """Yield parsed events from a canonical transcript file.

    Lines that don't parse as JSON, or that aren't dicts, are skipped silently
    so legacy raw-SDK files don't crash readers.
    """
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict):
                yield evt


def append_worker_error(
    path: str | Path,
    *,
    kind: str,
    summary: str,
    traceback_text: str | None = None,
) -> None:
    """Append a ``worker_error`` event to a transcript file.

    Safe to call when the transcript file does not yet exist (creates the
    parent directory and the file). The ``seq`` is taken from the highest
    seen ``seq`` in the existing file so the event lands after agent output,
    not in the middle of it. Failures to write are swallowed — losing the
    error event is worse than crashing the worker again on top of it, but a
    write failure here is itself logged via the standard logging module.
    """
    import logging

    log = logging.getLogger(__name__)
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("could not create transcript dir for worker_error: %s", p)
        return

    next_seq_value = 0
    if p.exists():
        try:
            for evt in read_events(p):
                seq = evt.get("seq")
                if isinstance(seq, int) and seq >= next_seq_value:
                    next_seq_value = seq + 1
        except Exception:
            log.exception("failed to read transcript while appending worker_error: %s", p)

    event: dict[str, object] = {
        "type": "worker_error",
        "ts": now_iso(),
        "seq": next_seq_value,
        "kind": kind,
        "summary": summary,
    }
    if traceback_text:
        event["traceback"] = traceback_text

    try:
        with p.open("ab") as f:
            write_event(f, event)
    except OSError:
        log.exception("failed to append worker_error to transcript: %s", p)


def is_canonical(path: str | Path) -> bool:
    """Heuristic: returns True if the file looks like canonical NDJSON.

    Checks that the first non-empty line parses as a JSON object and carries a
    recognized ``type`` field.
    """
    p = Path(path)
    if not p.exists():
        return False
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    return False
                if not isinstance(evt, dict):
                    return False
                return evt.get("type") in KNOWN_TYPES
    except OSError:
        return False
    return False
