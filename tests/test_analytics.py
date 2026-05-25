"""Aggregation correctness + live-spend helpers for the cost dashboard."""
from datetime import datetime, timedelta, timezone

import pytest

from nightdesk.db.models import Profile, Run, Ticket
from nightdesk.domain import analytics


NOW = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


def _profile(session, name="p"):
    p = Profile(name=name, fs_read=[], fs_write=[], allowed_tools=[],
                denied_tools=[], network_mode="off", network_allowlist=[],
                secret_keys=[])
    session.add(p)
    session.commit()
    return p


def _ticket(session, profile, title="t"):
    t = Ticket(title=title, prompt="", status="review", priority=0,
               profile_id=profile.id, cwd="/tmp")
    session.add(t)
    session.commit()
    return t


def _run(session, ticket, *, started_at, cost=None, exit_status="success",
         finished_at="auto", input_tokens=0, output_tokens=0,
         cache_read_tokens=0, cache_write_tokens=0):
    if finished_at == "auto":
        finished_at = started_at + timedelta(seconds=60)
    r = Run(ticket_id=ticket.id, started_at=started_at, finished_at=finished_at,
            exit_status=exit_status, worktree_path="/w", transcript_path="/x",
            host="h", cost_usd=cost, input_tokens=input_tokens,
            output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens)
    session.add(r)
    session.commit()
    return r


# --- spend / window totals -------------------------------------------------
def test_window_totals_sums_cost_and_tokens(session):
    p = _profile(session)
    t = _ticket(session, p)
    _run(session, t, started_at=NOW, cost=1.0, input_tokens=100, output_tokens=50)
    _run(session, t, started_at=NOW - timedelta(hours=2), cost=2.0,
         input_tokens=10, output_tokens=5)
    # 10 days ago — outside the 'today' window.
    _run(session, t, started_at=NOW - timedelta(days=10), cost=8.0)

    today = analytics.window_totals(session, start=analytics.start_of_day(NOW))
    assert today["cost"] == pytest.approx(3.0)
    assert today["run_count"] == 2
    assert today["input_tokens"] == 110
    assert today["output_tokens"] == 55
    assert today["total_tokens"] == 165

    last_30d = analytics.window_totals(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29))
    assert last_30d["cost"] == pytest.approx(11.0)
    assert last_30d["run_count"] == 3


def test_window_totals_cache_hit_rate(session):
    p = _profile(session)
    t = _ticket(session, p)
    # 100 fresh input, 300 cache read, 100 cache write, 50 output.
    # hit rate = 300 / (100 + 300 + 100) = 0.6 (output excluded).
    _run(session, t, started_at=NOW, input_tokens=100, output_tokens=50,
         cache_read_tokens=300, cache_write_tokens=100)
    w = analytics.window_totals(session, start=analytics.start_of_day(NOW))
    assert w["cache_read_tokens"] == 300
    assert w["cache_write_tokens"] == 100
    assert w["cache_hit_rate"] == pytest.approx(0.6)


def test_cache_hit_rate_zero_when_no_prompt_tokens():
    assert analytics.cache_hit_rate(0, 0, 0) == 0.0


def test_spend_between_excludes_null_cost(session):
    p = _profile(session)
    t = _ticket(session, p)
    _run(session, t, started_at=NOW, cost=5.0)
    # In-flight / unknown-model run: NULL cost contributes nothing.
    _run(session, t, started_at=NOW, cost=None, exit_status=None, finished_at=None)
    assert analytics.spend_between(
        session, start=analytics.start_of_day(NOW)) == pytest.approx(5.0)


# --- breakdowns ------------------------------------------------------------
def test_usage_by_profile_groups_and_orders_by_tokens(session):
    pa = _profile(session, "alpha")
    pb = _profile(session, "beta")
    ta = _ticket(session, pa, "ta")
    tb = _ticket(session, pb, "tb")
    _run(session, ta, started_at=NOW, input_tokens=100)
    _run(session, ta, started_at=NOW, input_tokens=150)
    _run(session, tb, started_at=NOW, input_tokens=400)

    rows = analytics.usage_by_profile(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29))
    assert [r["name"] for r in rows] == ["beta", "alpha"]
    assert rows[0]["total_tokens"] == 400
    assert rows[1]["total_tokens"] == 250
    assert rows[1]["run_count"] == 2


def test_usage_by_ticket_top_n_by_tokens(session):
    p = _profile(session)
    t1 = _ticket(session, p, "small")
    t2 = _ticket(session, p, "heavy")
    _run(session, t1, started_at=NOW, input_tokens=50)
    _run(session, t2, started_at=NOW, input_tokens=900, output_tokens=100)

    rows = analytics.usage_by_ticket(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29), limit=10)
    assert rows[0]["title"] == "heavy"
    assert rows[0]["total_tokens"] == 1000


def test_tokens_by_model_groups_and_orders(session):
    p = _profile(session)
    t = _ticket(session, p)
    o1 = _run(session, t, started_at=NOW, input_tokens=200, cache_read_tokens=600,
              cache_write_tokens=200, cost=5.0)
    o1.model_used = "claude-opus-4-7"
    s1 = _run(session, t, started_at=NOW, input_tokens=100, cost=0.5)
    s1.model_used = "claude-sonnet-4-6"
    # No model recorded -> grouped under "unknown".
    _run(session, t, started_at=NOW, input_tokens=10)
    session.commit()

    rows = analytics.tokens_by_model(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29))
    # opus has the most tokens (1000) -> first.
    assert rows[0]["model"] == "claude-opus-4-7"
    assert rows[0]["total_tokens"] == 1000
    assert rows[0]["cache_hit_rate"] == pytest.approx(0.6)
    models = {r["model"] for r in rows}
    assert "unknown" in models


# --- run stats + durations -------------------------------------------------
def test_run_stats_success_and_failure(session):
    p = _profile(session)
    t = _ticket(session, p)
    _run(session, t, started_at=NOW, cost=1.0, exit_status="success")
    _run(session, t, started_at=NOW, cost=1.0, exit_status="failed")
    _run(session, t, started_at=NOW, cost=1.0, exit_status="cancelled")
    # Unfinished run is not counted as completed.
    _run(session, t, started_at=NOW, cost=None, exit_status=None, finished_at=None)

    stats = analytics.run_stats(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29))
    assert stats["completed"] == 3
    assert stats["success"] == 1
    assert stats["failure"] == 2
    assert stats["success_rate"] == pytest.approx(1 / 3)


def test_duration_percentiles(session):
    p = _profile(session)
    t = _ticket(session, p)
    for secs in (10, 20, 30, 40, 100):
        _run(session, t, started_at=NOW,
             finished_at=NOW + timedelta(seconds=secs), cost=1.0)
    d = analytics.duration_percentiles(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29))
    assert d["count"] == 5
    assert d["median_seconds"] == pytest.approx(30)
    # nearest-rank p90 over 5 samples -> index ceil(0.9*5)-1 = 4 -> 100s
    assert d["p90_seconds"] == pytest.approx(100)


def test_duration_percentiles_empty(session):
    p = _profile(session)
    _ticket(session, p)
    d = analytics.duration_percentiles(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29))
    assert d["count"] == 0
    assert d["median_seconds"] is None


# --- daily series ----------------------------------------------------------
def test_daily_usage_series_zero_filled(session):
    p = _profile(session)
    t = _ticket(session, p)
    _run(session, t, started_at=NOW, input_tokens=100, output_tokens=20, cost=1.0)
    _run(session, t, started_at=NOW - timedelta(days=2), input_tokens=300, cost=3.0)

    series = analytics.daily_usage_series(
        session, start=analytics.start_of_day(NOW) - timedelta(days=4), now=NOW)
    # 5 days inclusive (today minus 4 .. today).
    assert len(series) == 5
    by_day = {d["date"]: d["tokens"] for d in series}
    assert by_day["2026-05-24"] == 120
    assert by_day["2026-05-22"] == 300
    assert by_day["2026-05-23"] == 0
    # cost still rides along for the tooltip.
    assert {d["date"]: d["cost"] for d in series}["2026-05-24"] == pytest.approx(1.0)
    # ordered oldest -> newest
    assert series[0]["date"] == "2026-05-20"
    assert series[-1]["date"] == "2026-05-24"


# --- live spend ------------------------------------------------------------
def test_spend_status_day_and_month(session):
    p = _profile(session)
    t = _ticket(session, p)
    # Two runs earlier this month plus today; last month must not count.
    _run(session, t, started_at=NOW.replace(day=2), cost=6.0)
    _run(session, t, started_at=NOW, cost=5.0)
    _run(session, t, started_at=NOW.replace(month=4, day=15), cost=50.0)

    status = analytics.compute_spend_status(session, now=NOW)
    assert status.day_spend_usd == pytest.approx(5.0)
    assert status.month_spend_usd == pytest.approx(11.0)
