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
    "worker_error", "rate_limit", "cancelled", "subagent",
]


KNOWN_TYPES: frozenset[str] = frozenset(
    {"meta", "assistant_text", "thinking", "tool_use", "tool_result", "result",
     "worker_error", "rate_limit", "cancelled", "stats", "subagent"}
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
    parent_tool_use_id: str


class ToolResultEvent(_BaseEvent, total=False):
    type: Literal["tool_result"]
    tool_use_id: str
    output: str
    is_error: bool
    truncated: bool
    parent_tool_use_id: str


class ResultEvent(_BaseEvent, total=False):
    type: Literal["result"]
    subtype: Literal["success", "error"]
    summary: str


class WorkerErrorEvent(_BaseEvent, total=False):
    """Nightdesk-level failure that occurred outside the agent's own output.

    Emitted as the final event of a failed run so the transcript view can
    surface the worker error (executor crash, sdk runner non-zero exit,
    transcript drain failure, etc.) instead of
    burying it in a separate ticket comment.

    ``kind`` is a short machine tag (``executor_error``,
    ``runner_exit_nonzero``, ``run_failed``, ``drain_error``).
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
    raw: str


class SubagentEvent(_BaseEvent, total=False):
    """A sub-agent (Task tool) lifecycle update surfaced from the agent SDK.

    When the run's agent uses the Task tool to spawn a sub-agent (e.g. the
    ``Explore`` agent), the SDK yields lifecycle messages. The runner collapses
    them to a single ``subagent`` event discriminated by ``phase``:

    - ``started`` — the sub-agent was spawned. Carries ``subagent_type``,
      ``task_type``, ``description``, ``prompt``.
    - ``progress`` — incremental status; carries an updated ``description``,
      ``last_tool_name``, and a cumulative ``usage`` (``total_tokens``,
      ``tool_uses``, ``duration_ms``). Many arrive per run; the renderer
      collapses consecutive ones per ``task_id`` into one updating card.
    - ``notification`` — terminal; carries ``status`` (``completed`` |
      ``failed``), ``summary``, ``output_file``.

    ``task_id`` / ``tool_use_id`` tie the event back to the parent Task
    tool_use. ``raw`` is a best-effort dump of the full SDK payload.
    """

    type: Literal["subagent"]
    phase: str
    task_id: str
    tool_use_id: str
    subagent_type: str
    task_type: str
    description: str
    prompt: str
    status: str
    summary: str
    output_file: str
    last_tool_name: str
    usage: dict
    raw: str


class CancelledEvent(_BaseEvent, total=False):
    """A terminal marker written when the user cancels a running ticket.

    The worker terminates the agent process mid-run, so the event stream stops
    abruptly; this event makes the reason explicit. ``message`` is the
    one-line user-facing reason (e.g. ``Run cancelled by user.``).
    """

    type: Literal["cancelled"]
    message: str


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


def next_transcript_seq(path: str | Path) -> int:
    """The NEXT available ``seq`` for a transcript file: max(seq)+1, or 0 if the
    file is empty/missing/has no integer seqs.

    Used to seed a turn's seq counter so a conversation's single transcript
    file keeps ONE monotonic seq space across turns: turn N+1 continues ABOVE
    max(seq) rather than restarting at 0 (which would collide with turn N's
    events and break the live-tail lastSeq dedup). Mirrors the next-seq rule
    ``append_event`` already uses for out-of-band event writes.
    """
    highest = -1
    for evt in read_events(path):
        seq = evt.get("seq")
        if isinstance(seq, int) and seq > highest:
            highest = seq
    return highest + 1


def append_event(path: str | Path, event: dict, *, _type: str | None = None) -> None:
    """Append a single canonical event to a transcript file.

    Safe to call when the transcript file does not yet exist (creates the
    parent directory and the file). The ``seq`` is taken from the highest
    seen ``seq`` in the existing file so the event lands after agent output,
    not in the middle of it. ``ts`` is stamped if absent. Failures to write
    are swallowed — losing a transcript event is worse than crashing the
    worker again on top of it, but a write failure here is itself logged via
    the standard logging module.

    ``_type`` optionally forces ``event["type"]`` (used by ``append_worker_error``
    so callers can construct the payload without naming the type).
    """
    import logging

    log = logging.getLogger(__name__)
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("could not create transcript dir for event: %s", p)
        return

    next_seq_value = 0
    if p.exists():
        try:
            for evt in read_events(p):
                seq = evt.get("seq")
                if isinstance(seq, int) and seq >= next_seq_value:
                    next_seq_value = seq + 1
        except Exception:
            log.exception("failed to read transcript while appending event: %s", p)

    out = dict(event)
    if _type is not None:
        out["type"] = _type
    out.setdefault("ts", now_iso())
    out["seq"] = next_seq_value

    try:
        with p.open("ab") as f:
            write_event(f, out)
    except OSError:
        log.exception("failed to append event to transcript: %s", p)


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
    event: dict[str, object] = {
        "kind": kind,
        "summary": summary,
    }
    if traceback_text:
        event["traceback"] = traceback_text
    append_event(path, event, _type="worker_error")


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
