"""Per-run request-latency derivation from canonical transcripts.

Latency is derived purely from ``ts`` deltas between canonical transcript
events (see ``nightdesk.transcript``). No new event collection is required —
the data is already on disk. This module turns a transcript into per-turn
samples and a per-run summary; ``domain.analytics`` rolls the cached
summaries up for the dashboard.

Latency kinds (one sample each):

- ``"ttft"``  — first-token latency: gap from the run's first event (its
  ``meta`` header) to the first assistant output event (``assistant_text`` or
  ``thinking``).
- ``"turn"``  — per-turn model latency: gap from a ``tool_result`` event to
  the NEXT assistant output event. By that point the agent already has the
  tool output, so the interval is pure model inference.
- ``"tool"``  — tool execution time: gap from a ``tool_use`` event to its
  matching ``tool_result`` (matched by id; ``tool_use`` carries ``id``,
  ``tool_result`` carries ``tool_use_id``).

``Run model-time`` = sum of ``turn`` samples; ``run tool-time`` = sum of
``tool`` samples. Each sample is tagged with the model from the surrounding
``stats`` events (assistant/tool events themselves carry no model).

ACCURACY CAVEAT — ``ts`` is when nightdesk *received* each SDK event at
ingest, NOT the provider API's internal response time. For streamed responses
the assistant-output ``ts`` lands when the chunk/stream completes, so per-turn
latency reflects *streaming completion cadence*. That is accurate enough for
trend comparison (the goal: "is this model slower right now than
historically?") but it is NOT a millisecond-precise API TTFT. Do not treat
these numbers as provider-side response-time SLOs.

Run isolation: a transcript file is shared by every turn (Run) in a
conversation (see ``Conversation.transcript_path``). Each turn opens with a
``meta`` header, so the run's events are exactly the segment between its
``meta`` and the next ``meta`` (or EOF). This module picks that segment by the
run's ``[started_at, finished_at]`` window landing on its ``meta`` ts, then
processes the whole segment — exact isolation, no cross-run bleed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional

from sqlalchemy.orm import Session

from nightdesk import transcript
from nightdesk.db.models import Run, RunLatency

log = logging.getLogger(__name__)


# Slack (seconds) around the run's [started_at, finished_at] window used to
# match the run's ``meta`` header inside a shared conversation transcript.
# Runs are strictly serialized (a turn finishes before the next starts), so a
# small margin absorbs clock jitter without crossing into an adjacent turn.
_WINDOW_MARGIN = 5.0

# Assistant-output event types: the model replying (text or thinking).
_ASSISTANT_OUTPUT_TYPES = ("assistant_text", "thinking")


LatencyKind = Literal["turn", "ttft", "tool"]


@dataclass(frozen=True)
class LatencySample:
    """One derived latency measurement."""

    model: Optional[str]
    seconds: float
    kind: LatencyKind


@dataclass(frozen=True)
class RunLatencySummary:
    """Aggregated latency for a single run.

    ``turn_latencies`` keeps the raw per-turn seconds so percentiles/medians
    can be merged across runs (in ``run_latency`` rows) without rescanning
    transcript files.
    """

    model: Optional[str]
    total_model_seconds: float
    total_tool_seconds: float
    turn_count: int
    ttft_seconds: Optional[float]
    turn_latencies: list[float] = field(default_factory=list)


def _parse_ts(ts: object) -> Optional[datetime]:
    """Parse an ISO-8601 ``ts`` string into an aware datetime, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return _as_aware_utc(dt)


def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Attach UTC to a naive datetime (assume UTC). No-op for aware values.

    SQLite returns ``DateTime(timezone=True)`` columns as offset-naive on
    reload, while transcript ``ts`` strings are aware (``+00:00``); normalizing
    both sides to aware-UTC keeps the window comparison consistent.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _in_window(ts: Optional[datetime], lo: Optional[datetime],
               hi: Optional[datetime]) -> bool:
    if ts is None:
        return False
    if lo is not None and ts < lo:
        return False
    if hi is not None and ts > hi:
        return False
    return True


def latency_samples_from_events(
    events: Iterable[dict],
    *,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    margin: float = _WINDOW_MARGIN,
) -> list[LatencySample]:
    """Compute latency samples from canonical transcript events.

    Pure core (no file I/O) — pass a list of parsed events to unit-test against
    a synthetic transcript. When ``started_at``/``finished_at`` are given, only
    the run segment whose ``meta`` header falls in the window is processed
    (see the module docstring on run isolation). Without a window the whole
    stream is treated as one segment.

    Streams with small running state: it holds the output samples (bounded by
    the number of turns) but never the whole event stream.
    """
    started = _as_aware_utc(started_at)
    finished = _as_aware_utc(finished_at)
    lo = started - timedelta(seconds=margin) if started is not None else None
    hi = finished + timedelta(seconds=margin) if finished is not None else None
    have_window = started_at is not None or finished_at is not None

    samples: list[LatencySample] = []
    models_seen: set[str] = set()
    current_model: Optional[str] = None

    # Segment state (reset at every ``meta`` header).
    in_target = not have_window
    first_event_ts: Optional[datetime] = None
    first_output_done = False
    pending_turn_start: Optional[datetime] = None  # last tool_result ts
    open_tools: dict[str, datetime] = {}  # tool_use id -> ts

    def reset_segment(is_target: bool, meta_ts: Optional[datetime]) -> None:
        nonlocal in_target, first_event_ts, first_output_done
        nonlocal pending_turn_start, open_tools, current_model
        in_target = is_target
        first_event_ts = meta_ts  # the meta header is the run's first event
        first_output_done = False
        pending_turn_start = None
        open_tools = {}
        current_model = None

    for evt in events:
        etype = evt.get("type")
        ts = _parse_ts(evt.get("ts"))

        # A ``meta`` header opens a new run segment. Decide whether THIS segment
        # is the target (its meta falls in the run's window) and reset state.
        if etype == "meta":
            is_target = True if not have_window else _in_window(ts, lo, hi)
            reset_segment(is_target, ts)
            continue

        if not in_target:
            continue
        if ts is None:
            continue
        if first_event_ts is None:
            first_event_ts = ts

        if etype == "stats":
            m = evt.get("model")
            if isinstance(m, str) and m:
                current_model = m
                models_seen.add(m)
        elif etype == "tool_use":
            tid = evt.get("id")
            if isinstance(tid, str) and tid:
                open_tools[tid] = ts
        elif etype == "tool_result":
            # The next assistant output after a tool_result is the model
            # replying — record the gap start.
            pending_turn_start = ts
            tuid = evt.get("tool_use_id")
            if isinstance(tuid, str) and tuid in open_tools:
                started = open_tools.pop(tuid)
                samples.append(
                    LatencySample(current_model,
                                  (ts - started).total_seconds(), "tool")
                )
        elif etype in _ASSISTANT_OUTPUT_TYPES:
            if not first_output_done:
                # TTFT: first assistant output of the run.
                if first_event_ts is not None:
                    samples.append(
                        LatencySample(current_model,
                                      (ts - first_event_ts).total_seconds(),
                                      "ttft")
                    )
                first_output_done = True
            elif pending_turn_start is not None:
                # Per-turn model latency: tool_result -> this output.
                samples.append(
                    LatencySample(current_model,
                                  (ts - pending_turn_start).total_seconds(),
                                  "turn")
                )
                pending_turn_start = None
        # Other event types (result, rate_limit, subagent, ...) carry no
        # latency signal and are ignored.

    # Single-model runs: the model rides on the *trailing* stats event, so the
    # first turn's TTFT is provisionally tagged None. With exactly one model
    # in the run, retag everything (the common case — a run uses one model).
    if len(models_seen) == 1:
        only = next(iter(models_seen))
        samples = [LatencySample(only, s.seconds, s.kind) for s in samples]

    return samples


def run_latency_samples(
    transcript_path: str | Path,
    *,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> list[LatencySample]:
    """Stream a transcript file and return its latency samples.

    Thin file-reading wrapper over :func:`latency_samples_from_events`; missing
    or unreadable files yield an empty list (best-effort, never raises).
    """
    try:
        return latency_samples_from_events(
            transcript.read_events(transcript_path),
            started_at=started_at,
            finished_at=finished_at,
        )
    except OSError:
        log.exception("could not read transcript for latency: %s", transcript_path)
        return []


def summarize_samples(
    samples: Iterable[LatencySample], *, model: Optional[str]
) -> RunLatencySummary:
    """Reduce per-turn samples into the per-run cache summary."""
    turns: list[float] = []
    tool_seconds = 0.0
    ttft: Optional[float] = None
    for s in samples:
        if s.kind == "turn":
            turns.append(s.seconds)
        elif s.kind == "tool":
            tool_seconds += s.seconds
        elif s.kind == "ttft" and ttft is None:
            ttft = s.seconds
    return RunLatencySummary(
        model=model,
        total_model_seconds=sum(turns),
        total_tool_seconds=tool_seconds,
        turn_count=len(turns),
        ttft_seconds=ttft,
        turn_latencies=turns,
    )


def compute_run_latency(
    transcript_path: str | Path,
    *,
    started_at: datetime,
    finished_at: Optional[datetime] = None,
    model: Optional[str] = None,
) -> RunLatencySummary:
    """Scan a run's transcript once and return its latency summary.

    ``model`` is the authoritative per-run model (typically ``Run.model_used``);
    the per-event ``stats`` model is used only for the samples' provisional
    tags, while this argument drives the summary and cache row.
    """
    samples = run_latency_samples(
        transcript_path, started_at=started_at, finished_at=finished_at
    )
    return summarize_samples(samples, model=model)


def populate_run_latency(
    session: Session, run: Optional[Run], *, force: bool = False
) -> Optional[RunLatency]:
    """Compute and cache a run's latency summary in the ``run_latency`` table.

    Idempotent — if a row already exists it is left untouched unless ``force``
    is set (transcripts are terminal, so a cached summary never needs refresh).
    This is the run-completion hook: the dashboard then aggregates rows instead
    of scanning files. Best-effort: callers should wrap in try/except so a
    latency-cache miss never fails the run.
    """
    if run is None:
        return None
    existing = session.get(RunLatency, run.id)
    if existing is not None and not force:
        return existing

    summary = compute_run_latency(
        run.transcript_path,
        started_at=run.started_at,
        finished_at=run.finished_at,
        model=run.model_used,
    )

    if existing is not None:
        row = existing
    else:
        row = RunLatency(run_id=run.id)
        session.add(row)
    row.model = summary.model
    row.total_model_seconds = summary.total_model_seconds
    row.total_tool_seconds = summary.total_tool_seconds
    row.turn_count = summary.turn_count
    row.ttft_seconds = summary.ttft_seconds
    row.turn_latencies = list(summary.turn_latencies)
    session.commit()
    session.refresh(row)
    return row
