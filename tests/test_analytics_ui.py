"""End-to-end render of the /analytics page, including the latency section."""
from datetime import datetime, timedelta, timezone

import pytest

from nightdesk.db.models import Profile, Run, RunLatency, Ticket


def _seed_run_with_latency(session, *, model, turn_latencies,
                           total_model_seconds=None, total_tool_seconds=0.0,
                           started_at=None):
    # Default to "yesterday" so the run lands inside the route's rolling 30-day
    # window (which is anchored on the real current time, not a fixed NOW).
    if started_at is None:
        started_at = datetime.now(timezone.utc) - timedelta(days=1)
    profile = Profile(name=f"p_{model}", fs_read=[], fs_write=[], allowed_tools=[],
                      denied_tools=[], network_mode="off",
                      network_allowlist=[], secret_keys=[])
    session.add(profile)
    session.commit()
    ticket = Ticket(title="t", prompt="", status="review", priority=0,
                    profile_id=profile.id)
    session.add(ticket)
    session.commit()
    run = Run(ticket_id=ticket.id, started_at=started_at,
              finished_at=started_at + timedelta(seconds=60),
              exit_status="success", worktree_path="/w", transcript_path="/x",
              host="h", model_used=model)
    session.add(run)
    session.commit()
    if total_model_seconds is None:
        total_model_seconds = float(sum(turn_latencies))
    session.add(RunLatency(
        run_id=run.id, model=model,
        total_model_seconds=total_model_seconds,
        total_tool_seconds=total_tool_seconds,
        turn_count=len(turn_latencies), ttft_seconds=2.0,
        turn_latencies=list(turn_latencies),
    ))
    session.commit()
    return run


@pytest.mark.anyio
async def test_analytics_page_renders_latency_section(client, session):
    _seed_run_with_latency(session, model="claude-opus-4-7",
                           turn_latencies=[3.0, 5.0], total_tool_seconds=4.0)

    resp = await client.get("/analytics")
    assert resp.status_code == 200
    body = resp.text
    # The three latency deliverables are present.
    assert "Latency by model" in body            # (a) median/p90/p99 table
    assert "Median turn latency by model" in body  # (b) trend chart
    assert "Model time vs tool time" in body      # (c) breakdown
    assert "claude-opus-4-7" in body
    # The latency chart data island is emitted for the chart JS.
    assert "latency-series-data" in body


@pytest.mark.anyio
async def test_analytics_page_renders_without_latency_data(client, session):
    # No run_latency rows -> the page still renders (graceful empty states).
    resp = await client.get("/analytics")
    assert resp.status_code == 200
    assert "Latency by model" in resp.text
    assert "No latency samples in this window." in resp.text
