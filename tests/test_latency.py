"""Unit tests for transcript latency derivation (domain.latency)."""
import json
from datetime import datetime, timedelta, timezone

from nightdesk.db.models import Profile, Run, RunLatency, Ticket
from nightdesk.domain import latency


BASE = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


def at(offset: float) -> str:
    """ISO ts string `offset` seconds after BASE."""
    return (BASE + timedelta(seconds=offset)).isoformat()


def write_transcript(path, events) -> str:
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return str(path)


def samples_by_kind(samples):
    out = {"turn": [], "ttft": [], "tool": []}
    for s in samples:
        out[s.kind].append(s)
    return out


# --- pure core -------------------------------------------------------------
def test_multi_turn_transcript_with_tool_use():
    events = [
        {"type": "meta", "ts": at(0)},
        {"type": "assistant_text", "ts": at(2), "text": "hi"},
        {"type": "stats", "ts": at(2.5), "model": "opus"},
        {"type": "tool_use", "ts": at(3), "id": "u1"},
        {"type": "tool_result", "ts": at(8), "tool_use_id": "u1"},
        {"type": "assistant_text", "ts": at(12), "text": "ok"},
        {"type": "stats", "ts": at(12.5), "model": "opus"},
        {"type": "tool_use", "ts": at(13), "id": "u2"},
        {"type": "tool_result", "ts": at(20), "tool_use_id": "u2"},
        {"type": "thinking", "ts": at(24), "text": "hmm"},
        {"type": "stats", "ts": at(24.5), "model": "opus"},
        {"type": "result", "ts": at(25), "subtype": "success"},
    ]
    samples = latency.latency_samples_from_events(events)
    by_kind = samples_by_kind(samples)

    # TTFT: first event (meta) -> first assistant output = 2s.
    assert len(by_kind["ttft"]) == 1
    assert by_kind["ttft"][0].seconds == 2.0
    # Two per-turn latencies: tool_result->next output (12-8=4, 24-20=4).
    assert [s.seconds for s in by_kind["turn"]] == [4.0, 4.0]
    # Two tool-execution gaps matched by id (8-3=5, 20-13=7).
    assert sorted(s.seconds for s in by_kind["tool"]) == [5.0, 7.0]
    # Single model in the run -> every sample tagged with it.
    assert all(s.model == "opus" for s in samples)


def test_run_with_no_tool_calls():
    events = [
        {"type": "meta", "ts": at(0)},
        {"type": "assistant_text", "ts": at(2), "text": "done"},
        {"type": "stats", "ts": at(2.5), "model": "sonnet"},
        {"type": "result", "ts": at(5), "subtype": "success"},
    ]
    samples = latency.latency_samples_from_events(events)
    by_kind = samples_by_kind(samples)
    # Only a TTFT sample; no turns (turns require a preceding tool_result).
    assert len(by_kind["ttft"]) == 1
    assert by_kind["ttft"][0].seconds == 2.0
    assert by_kind["turn"] == []
    assert by_kind["tool"] == []


def test_summarize_aggregates_totals():
    events = [
        {"type": "meta", "ts": at(0)},
        {"type": "assistant_text", "ts": at(2)},
        {"type": "stats", "ts": at(2.5), "model": "opus"},
        {"type": "tool_use", "ts": at(3), "id": "u1"},
        {"type": "tool_result", "ts": at(8), "tool_use_id": "u1"},
        {"type": "assistant_text", "ts": at(12)},
    ]
    samples = latency.latency_samples_from_events(events)
    summary = latency.summarize_samples(samples, model="opus")
    assert summary.model == "opus"
    assert summary.turn_count == 1
    assert summary.total_model_seconds == 4.0  # 12 - 8
    assert summary.total_tool_seconds == 5.0   # 8 - 3
    assert summary.ttft_seconds == 2.0
    assert summary.turn_latencies == [4.0]


def test_multi_model_run_not_retagged():
    # Two distinct models: samples keep their provisional (last-stats) tag and
    # are NOT force-retagged. Documents the single-model approximation.
    events = [
        {"type": "meta", "ts": at(0)},
        {"type": "assistant_text", "ts": at(2)},
        {"type": "stats", "ts": at(2.5), "model": "modelA"},
        {"type": "tool_use", "ts": at(3), "id": "u1"},
        {"type": "tool_result", "ts": at(8), "tool_use_id": "u1"},
        {"type": "assistant_text", "ts": at(12)},
        {"type": "stats", "ts": at(12.5), "model": "modelB"},
        {"type": "tool_use", "ts": at(13), "id": "u2"},
        {"type": "tool_result", "ts": at(20), "tool_use_id": "u2"},
        {"type": "assistant_text", "ts": at(24)},
    ]
    samples = latency.latency_samples_from_events(events)
    by_kind = samples_by_kind(samples)
    turns = by_kind["turn"]
    assert {s.model for s in turns} == {"modelA", "modelB"}
    # TTFT precedes any stats event -> untagged (no single model to retag with).
    assert by_kind["ttft"][0].model is None


def test_window_isolates_one_run_in_a_shared_transcript():
    # A conversation transcript with two runs (two meta headers). The window
    # selects only the second run's segment.
    events = [
        # Run 1 (10:00).
        {"type": "meta", "ts": at(0)},
        {"type": "assistant_text", "ts": at(2)},
        {"type": "tool_use", "ts": at(3), "id": "r1u1"},
        {"type": "tool_result", "ts": at(9), "tool_use_id": "r1u1"},
        {"type": "assistant_text", "ts": at(13)},
        # Run 2 (11:00 = +3600s).
        {"type": "meta", "ts": at(3600)},
        {"type": "assistant_text", "ts": at(3602)},
        {"type": "stats", "ts": at(3602.5), "model": "opus"},
        {"type": "tool_use", "ts": at(3603), "id": "r2u1"},
        {"type": "tool_result", "ts": at(3608), "tool_use_id": "r2u1"},
        {"type": "assistant_text", "ts": at(3612)},
    ]
    started = BASE + timedelta(seconds=3600)
    finished = BASE + timedelta(seconds=3620)
    samples = latency.latency_samples_from_events(
        events, started_at=started, finished_at=finished
    )
    by_kind = samples_by_kind(samples)
    # Only run-2 events: one ttft and one turn; run-1 tool_use ids do not leak.
    assert len(by_kind["ttft"]) == 1
    assert by_kind["ttft"][0].seconds == 2.0  # 3602 - 3600
    assert len(by_kind["turn"]) == 1
    assert by_kind["turn"][0].seconds == 4.0  # 3612 - 3608
    # Tool sample only for r2u1 (5s), not r1u1.
    assert [s.seconds for s in by_kind["tool"]] == [5.0]


def test_unmatched_tool_use_and_result_do_not_crash():
    events = [
        {"type": "meta", "ts": at(0)},
        {"type": "assistant_text", "ts": at(2)},
        {"type": "tool_result", "ts": at(5), "tool_use_id": "orphan"},  # no tool_use
        {"type": "assistant_text", "ts": at(9)},  # turn = 9-5
        {"type": "tool_use", "ts": at(10), "id": "dangling"},  # never resolved
    ]
    samples = latency.latency_samples_from_events(events)
    by_kind = samples_by_kind(samples)
    assert [s.seconds for s in by_kind["turn"]] == [4.0]
    assert by_kind["tool"] == []  # neither pair matched


def test_empty_and_malformed_events_skipped():
    events = [
        {"type": "meta", "ts": at(0)},
        {"type": "assistant_text"},  # no ts -> skipped
        {"type": "assistant_text", "ts": "not-a-date"},  # bad ts -> skipped
        {"type": "assistant_text", "ts": at(3), "text": "ok"},
    ]
    samples = latency.latency_samples_from_events(events)
    by_kind = samples_by_kind(samples)
    assert len(by_kind["ttft"]) == 1
    assert by_kind["ttft"][0].seconds == 3.0


# --- file streaming + DB cache population ----------------------------------
def _setup_run(session, tmp_path, *, transcript_events, started_offset=0,
               finished_offset=60, model="opus"):
    profile = Profile(name="p", fs_read=[], fs_write=[], allowed_tools=[],
                      denied_tools=[], network_mode="off",
                      network_allowlist=[], secret_keys=[])
    session.add(profile)
    session.commit()
    t = Ticket(title="t", prompt="", status="review", priority=0,
               profile_id=profile.id)
    session.add(t)
    session.commit()
    path = tmp_path / "conv.log"
    write_transcript(path, transcript_events)
    started = BASE + timedelta(seconds=started_offset)
    finished = BASE + timedelta(seconds=finished_offset)
    run = Run(ticket_id=t.id, started_at=started, finished_at=finished,
              exit_status="success", worktree_path="/w",
              transcript_path=str(path), host="h", model_used=model)
    session.add(run)
    session.commit()
    return run


def test_run_latency_samples_reads_file(tmp_path):
    events = [
        {"type": "meta", "ts": at(0)},
        {"type": "assistant_text", "ts": at(2)},
        {"type": "stats", "ts": at(2.5), "model": "opus"},
        {"type": "tool_use", "ts": at(3), "id": "u1"},
        {"type": "tool_result", "ts": at(8), "tool_use_id": "u1"},
        {"type": "assistant_text", "ts": at(12)},
    ]
    path = tmp_path / "conv.log"
    write_transcript(path, events)
    samples = latency.run_latency_samples(
        str(path), started_at=BASE, finished_at=BASE + timedelta(seconds=60),
    )
    by_kind = samples_by_kind(samples)
    assert by_kind["ttft"][0].seconds == 2.0
    assert [s.seconds for s in by_kind["turn"]] == [4.0]


def test_missing_transcript_returns_empty():
    samples = latency.run_latency_samples("/no/such/file.log")
    assert samples == []
    summary = latency.compute_run_latency(
        "/no/such/file.log", started_at=BASE, finished_at=BASE, model=None,
    )
    assert summary.turn_count == 0
    assert summary.turn_latencies == []


def test_populate_run_latency_caches_summary(session, tmp_path):
    events = [
        {"type": "meta", "ts": at(0)},
        {"type": "assistant_text", "ts": at(2)},
        {"type": "stats", "ts": at(2.5), "model": "opus"},
        {"type": "tool_use", "ts": at(3), "id": "u1"},
        {"type": "tool_result", "ts": at(8), "tool_use_id": "u1"},
        {"type": "assistant_text", "ts": at(12)},
        {"type": "result", "ts": at(13), "subtype": "success"},
    ]
    run = _setup_run(session, tmp_path, transcript_events=events,
                     finished_offset=13, model="opus")

    row = latency.populate_run_latency(session, run)
    assert row is not None
    assert row.run_id == run.id
    assert row.model == "opus"
    assert row.turn_count == 1
    assert row.total_model_seconds == 4.0
    assert row.total_tool_seconds == 5.0
    assert row.ttft_seconds == 2.0
    assert row.turn_latencies == [4.0]

    # Idempotent: a second call returns the cached row without recomputing.
    again = latency.populate_run_latency(session, run)
    assert again.run_id == row.run_id
    assert session.query(RunLatency).count() == 1

    # force=True recomputes (e.g. if the transcript grew before the run ended).
    latency.populate_run_latency(session, run, force=True)
    assert session.query(RunLatency).count() == 1


def test_populate_run_latency_none_run_is_noop(session):
    assert latency.populate_run_latency(session, None) is None
