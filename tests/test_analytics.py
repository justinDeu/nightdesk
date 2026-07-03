"""Aggregation correctness + live-spend helpers for the cost dashboard."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from nightdesk.db.models import Profile, Run, RunLatency, Ticket
from nightdesk.domain import analytics
from nightdesk.domain.pricing import PriceInfo


NOW = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

# A non-UTC zone for the local-window tests. America/New_York is EDT (UTC-4) in
# late May, so 2026-05-24 12:00 UTC == 2026-05-24 08:00 local, and local
# midnight is 2026-05-24 04:00 UTC.
NY = ZoneInfo("America/New_York")


def _profile(session, name="p"):
    p = Profile(name=name, fs_read=[], fs_write=[], allowed_tools=[],
                denied_tools=[], network_mode="off", network_allowlist=[],
                secret_keys=[])
    session.add(p)
    session.commit()
    return p


def _ticket(session, profile, title="t"):
    t = Ticket(title=title, prompt="", status="review", priority=0,
               profile_id=profile.id)
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


def _run_latency(session, run, *, model, turn_latencies,
                 total_model_seconds=None, total_tool_seconds=0.0,
                 ttft_seconds=None):
    """Insert a cached run_latency row (the dashboard's latency data source)."""
    if total_model_seconds is None:
        total_model_seconds = float(sum(turn_latencies))
    rl = RunLatency(
        run_id=run.id, model=model,
        total_model_seconds=total_model_seconds,
        total_tool_seconds=total_tool_seconds,
        turn_count=len(turn_latencies),
        ttft_seconds=ttft_seconds,
        turn_latencies=list(turn_latencies),
    )
    session.add(rl)
    session.commit()
    return rl


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
def test_daily_usage_by_model_series_zero_filled(session):
    p = _profile(session)
    t = _ticket(session, p)
    r1 = _run(session, t, started_at=NOW, input_tokens=100, output_tokens=20, cost=1.0)
    r1.model_used = "claude-opus-4-7"
    r2 = _run(session, t, started_at=NOW, input_tokens=80, cost=0.4)
    r2.model_used = "claude-sonnet-4-6"
    r3 = _run(session, t, started_at=NOW - timedelta(days=2), input_tokens=300, cost=3.0)
    r3.model_used = "claude-opus-4-7"
    session.commit()

    series = analytics.daily_usage_by_model_series(
        session, start=analytics.start_of_day(NOW) - timedelta(days=4), now=NOW)
    # 5 days inclusive (today minus 4 .. today).
    assert len(series) == 5
    by_day = {d["date"]: d for d in series}
    today = by_day["2026-05-24"]
    assert today["total_tokens"] == 200  # 120 opus + 80 sonnet
    assert today["by_model"]["claude-opus-4-7"] == 120
    assert today["by_model"]["claude-sonnet-4-6"] == 80
    assert today["cost"] == pytest.approx(1.4)
    assert by_day["2026-05-22"]["by_model"]["claude-opus-4-7"] == 300
    # zero-filled empty day
    assert by_day["2026-05-23"]["total_tokens"] == 0
    assert by_day["2026-05-23"]["by_model"] == {}
    # ordered oldest -> newest
    assert series[0]["date"] == "2026-05-20"
    assert series[-1]["date"] == "2026-05-24"


def test_build_dashboard_assigns_model_colors(session):
    p = _profile(session)
    t = _ticket(session, p)
    r = _run(session, t, started_at=NOW, input_tokens=500)
    r.model_used = "claude-opus-4-7"
    session.commit()

    data = analytics.build_dashboard(session, now=NOW)
    legend = data["model_legend"]
    assert any(m["model"] == "claude-opus-4-7" for m in legend)
    # Every legend entry carries a color, mirrored onto the by_model rows.
    assert all(m["color"] for m in legend)
    assert all("color" in row for row in data["by_model"])


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


# --- live price repricing --------------------------------------------------
# Live prices that differ from the bundled table: sonnet input $9/MTok vs $3.
LIVE = PriceInfo("live", "2026-06-30", {
    "claude-sonnet-4": {
        "input": 9.0, "output": 45.0, "cache_read": 0.9, "cache_write": 11.25},
})


def test_window_totals_reprices_with_live_prices(session):
    p = _profile(session)
    t = _ticket(session, p)
    # 1M sonnet input tokens; stored cost_usd is NULL (cost=None default).
    r = _run(session, t, started_at=NOW, input_tokens=1_000_000)
    r.model_used = "claude-sonnet-4-5"
    session.commit()

    w_stored = analytics.window_totals(session, start=analytics.start_of_day(NOW))
    assert w_stored["cost"] == pytest.approx(0.0)  # NULL cost, no prices given

    w_live = analytics.window_totals(
        session, start=analytics.start_of_day(NOW), prices=LIVE)
    # 1M * $9 = $9.00, not the bundled $3.00.
    assert w_live["cost"] == pytest.approx(9.0)


def test_build_dashboard_surfaces_source_and_reprices(session):
    p = _profile(session)
    t = _ticket(session, p)
    r = _run(session, t, started_at=NOW, input_tokens=1_000_000)
    r.model_used = "claude-sonnet-4-5"
    session.commit()

    data = analytics.build_dashboard(session, now=NOW, price_info=LIVE)
    assert data["price_source"] == "live"
    assert "live prices" in data["price_source_label"]
    assert data["price_as_of"] == "2026-06-30"
    assert data["today"]["cost"] == pytest.approx(9.0)
    # The per-model row is repriced too.
    assert any(row["cost"] == pytest.approx(9.0) for row in data["by_model"])

    # Default (no price_info) resolves to the bundled table -> $3.00/MTok.
    data_tbl = analytics.build_dashboard(session, now=NOW)
    assert data_tbl["price_source"] == "table"
    assert data_tbl["today"]["cost"] == pytest.approx(3.0)


def test_usage_by_profile_reprices_with_live_prices(session):
    p = _profile(session, "alpha")
    t = _ticket(session, p, "ta")
    r = _run(session, t, started_at=NOW, input_tokens=1_000_000)
    r.model_used = "claude-sonnet-4-5"
    session.commit()

    rows = analytics.usage_by_profile(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29), prices=LIVE)
    assert rows[0]["name"] == "alpha"
    assert rows[0]["cost"] == pytest.approx(9.0)


# --- local-time windows (day/week/month boundaries in a configured zone) ----
def test_start_of_day_is_local_midnight_as_utc():
    # 2026-05-24 12:00 UTC == 08:00 EDT (UTC-4). Local midnight is 04:00 UTC.
    assert analytics.start_of_day(NOW, NY) == datetime(2026, 5, 24, 4, 0, tzinfo=timezone.utc)
    # Default (no tz) is UTC midnight — unchanged from the original behavior.
    assert analytics.start_of_day(NOW) == datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc)
    assert analytics.start_of_day(NOW) == analytics.start_of_day(NOW, ZoneInfo("UTC"))


def test_start_of_month_is_local_first_as_utc():
    # 2026-05-01 00:00 EDT == 2026-05-01 04:00 UTC.
    assert analytics.start_of_month(NOW, NY) == datetime(2026, 5, 1, 4, 0, tzinfo=timezone.utc)
    assert analytics.start_of_month(NOW) == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


def test_today_counts_local_morning_run_not_utc_aged_off(session):
    # now = 2026-05-24 03:00 UTC == 2026-05-23 23:00 EDT: still local the 23rd.
    now = datetime(2026, 5, 24, 3, 0, tzinfo=timezone.utc)
    p = _profile(session)
    t = _ticket(session, p)
    # Local-morning run: 2026-05-23 09:00 EDT == 13:00 UTC -> local today.
    _run(session, t, started_at=datetime(2026, 5, 23, 13, 0, tzinfo=timezone.utc),
         input_tokens=100, cost=2.0)
    # Prior local-evening run: 2026-05-22 23:00 EDT == 2026-05-23 03:00 UTC ->
    # before local midnight of the 23rd, so excluded from today.
    _run(session, t, started_at=datetime(2026, 5, 23, 3, 0, tzinfo=timezone.utc),
         input_tokens=50, cost=1.0)

    data = analytics.build_dashboard(session, now=now, tz=NY)
    # With local boundaries, only the local-morning run is "today".
    assert data["today"]["input_tokens"] == 100
    assert data["today"]["run_count"] == 1
    # last_7d/last_30d derive from the same local today_start, so both recent
    # runs (the morning run and the prior-evening run) fall inside them.
    assert data["last_7d"]["input_tokens"] == 150
    assert data["last_30d"]["input_tokens"] == 150

    # Contrast with the UTC default: "today" = [2026-05-24 00:00 UTC, now], so
    # BOTH runs (which are on UTC the 23rd) have aged off -> today is empty.
    # This is the reported symptom: tokens-used-today drops mid-day.
    data_utc = analytics.build_dashboard(session, now=now)
    assert data_utc["today"]["input_tokens"] == 0


def test_today_uses_utc_midnight_when_tz_unset(session):
    # When schedule_timezone is unset, behavior is identical to UTC: today_start
    # is UTC midnight, so a run on the local-calendar day but before UTC midnight
    # of now's UTC date is NOT counted as today.
    now = datetime(2026, 5, 24, 3, 0, tzinfo=timezone.utc)
    p = _profile(session)
    t = _ticket(session, p)
    _run(session, t, started_at=datetime(2026, 5, 23, 13, 0, tzinfo=timezone.utc),
         input_tokens=100)
    data = analytics.build_dashboard(session, now=now)
    assert data["today"]["input_tokens"] == 0


def test_spend_status_uses_local_day(session):
    # The header spend chip: day spend must reset at local midnight, not UTC.
    now = datetime(2026, 5, 24, 3, 0, tzinfo=timezone.utc)  # local 2026-05-23 23:00 EDT
    p = _profile(session)
    t = _ticket(session, p)
    _run(session, t, started_at=datetime(2026, 5, 23, 13, 0, tzinfo=timezone.utc), cost=7.0)

    status = analytics.compute_spend_status(session, now=now, tz=NY)
    assert status.day_spend_usd == pytest.approx(7.0)
    # UTC default: today starts at 2026-05-24 00:00 UTC, so the 13:00-UTC run
    # has aged off -> 0 (the bug).
    assert analytics.compute_spend_status(session, now=now).day_spend_usd == pytest.approx(0.0)


def test_daily_series_buckets_by_local_date(session):
    # now = 2026-05-24 03:00 UTC == local 2026-05-23 23:00 EDT (local "today" = 23rd).
    now = datetime(2026, 5, 24, 3, 0, tzinfo=timezone.utc)
    p = _profile(session)
    t = _ticket(session, p)
    # Run A: 2026-05-24 02:30 UTC == local 2026-05-23 22:30 EDT -> local 23rd.
    # (Its UTC date is the 24th; without local bucketing it would land on the 24th.)
    rA = _run(session, t, started_at=datetime(2026, 5, 24, 2, 30, tzinfo=timezone.utc),
              input_tokens=70)
    rA.model_used = "claude-opus-4-7"
    # Run B: 2026-05-23 03:00 UTC == local 2026-05-22 23:00 EDT -> local 22nd.
    rB = _run(session, t, started_at=datetime(2026, 5, 23, 3, 0, tzinfo=timezone.utc),
              input_tokens=40)
    rB.model_used = "claude-opus-4-7"
    session.commit()

    series = analytics.daily_usage_by_model_series(
        session, start=analytics.start_of_day(now, NY) - timedelta(days=2),
        now=now, tz=NY)
    by_day = {d["date"]: d for d in series}
    # Run A is attributed to its LOCAL date (23rd), not its UTC date (24th).
    assert by_day["2026-05-23"]["total_tokens"] == 70
    assert by_day["2026-05-23"]["by_model"]["claude-opus-4-7"] == 70
    # No UTC-24th bucket leaks in: local "today" is the 23rd.
    assert "2026-05-24" not in by_day
    # Run B lands on local the 22nd.
    assert by_day["2026-05-22"]["total_tokens"] == 40


# --- latency aggregations (read cached run_latency rows) -------------------
def test_latency_by_model_percentiles_and_count(session):
    p = _profile(session)
    t = _ticket(session, p)
    r1 = _run(session, t, started_at=NOW)
    _run_latency(session, r1, model="claude-opus-4-7",
                 turn_latencies=[4.0, 4.0, 10.0], total_tool_seconds=12.0)
    r2 = _run(session, t, started_at=NOW)
    _run_latency(session, r2, model="claude-opus-4-7", turn_latencies=[6.0])
    r3 = _run(session, t, started_at=NOW)
    _run_latency(session, r3, model="claude-sonnet-4-6", turn_latencies=[8.0])

    rows = analytics.latency_by_model(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29))
    by_model = {r["model"]: r for r in rows}
    # opus turns merged across both runs: [4,4,10,6].
    opus = by_model["claude-opus-4-7"]
    assert opus["count"] == 4
    assert opus["median_seconds"] == pytest.approx(5.0)  # median of [4,4,6,10]
    # nearest-rank p90/p99 over 4 samples -> index 3 -> 10.
    assert opus["p90_seconds"] == pytest.approx(10.0)
    assert opus["p99_seconds"] == pytest.approx(10.0)
    # sonnet has the most turns? no — opus (4) sorts first by count.
    assert rows[0]["model"] == "claude-opus-4-7"
    assert by_model["claude-sonnet-4-6"]["count"] == 1


def test_latency_by_model_empty_when_no_rows(session):
    p = _profile(session)
    _ticket(session, p)
    rows = analytics.latency_by_model(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29))
    assert rows == []


def test_model_vs_tool_time_sums_per_model(session):
    p = _profile(session)
    t = _ticket(session, p)
    r1 = _run(session, t, started_at=NOW)
    _run_latency(session, r1, model="opus", turn_latencies=[4.0, 4.0],
                 total_model_seconds=8.0, total_tool_seconds=12.0)
    r2 = _run(session, t, started_at=NOW)
    _run_latency(session, r2, model="opus", turn_latencies=[6.0],
                 total_model_seconds=6.0, total_tool_seconds=9.0)
    r3 = _run(session, t, started_at=NOW)
    _run_latency(session, r3, model="sonnet", turn_latencies=[8.0],
                 total_model_seconds=8.0, total_tool_seconds=2.0)

    rows = analytics.model_vs_tool_time(
        session, start=analytics.start_of_day(NOW) - timedelta(days=29))
    by_model = {r["model"]: r for r in rows}
    assert by_model["opus"]["model_seconds"] == pytest.approx(14.0)
    assert by_model["opus"]["tool_seconds"] == pytest.approx(21.0)
    assert by_model["opus"]["run_count"] == 2
    assert by_model["sonnet"]["model_seconds"] == pytest.approx(8.0)
    # Sorted by total time desc: opus (35) before sonnet (10).
    assert rows[0]["model"] == "opus"


def test_daily_latency_series_per_day_per_model_median(session):
    p = _profile(session)
    t = _ticket(session, p)
    today = _run(session, t, started_at=NOW)
    _run_latency(session, today, model="claude-opus-4-7",
                 turn_latencies=[4.0, 4.0, 10.0])  # median 4
    today2 = _run(session, t, started_at=NOW)
    _run_latency(session, today2, model="claude-sonnet-4-6",
                 turn_latencies=[8.0])  # median 8
    two_days_ago = _run(session, t, started_at=NOW - timedelta(days=2))
    _run_latency(session, two_days_ago, model="claude-opus-4-7",
                 turn_latencies=[6.0])  # median 6

    series = analytics.daily_latency_by_model_series(
        session, start=analytics.start_of_day(NOW) - timedelta(days=4), now=NOW)
    assert len(series) == 5  # 5-day inclusive window
    by_day = {d["date"]: d for d in series}
    assert by_day["2026-05-24"]["by_model"]["claude-opus-4-7"] == pytest.approx(4.0)
    assert by_day["2026-05-24"]["by_model"]["claude-sonnet-4-6"] == pytest.approx(8.0)
    assert by_day["2026-05-22"]["by_model"]["claude-opus-4-7"] == pytest.approx(6.0)
    # Zero-filled day has no model entries.
    assert by_day["2026-05-23"]["by_model"] == {}
    assert series[0]["date"] == "2026-05-20"
    assert series[-1]["date"] == "2026-05-24"


def test_build_dashboard_returns_latency_fields(session):
    p = _profile(session)
    t = _ticket(session, p)
    r = _run(session, t, started_at=NOW, input_tokens=500)
    r.model_used = "claude-opus-4-7"
    session.commit()
    _run_latency(session, r, model="claude-opus-4-7",
                 turn_latencies=[3.0, 5.0], ttft_seconds=2.0)

    data = analytics.build_dashboard(session, now=NOW)
    # New keys are present.
    for key in ("latency_by_model", "latency_series", "model_vs_tool_time",
                "latency_model_legend", "max_daily_latency"):
        assert key in data
    # The opus row carries median/p90/p99/count + a color.
    opus = next(r for r in data["latency_by_model"] if r["model"] == "claude-opus-4-7")
    assert opus["count"] == 2
    assert opus["median_seconds"] == pytest.approx(4.0)
    assert opus["color"]
    # Model-vs-tool total reflects the cached row.
    mvt = next(r for r in data["model_vs_tool_time"] if r["model"] == "claude-opus-4-7")
    assert mvt["model_seconds"] == pytest.approx(8.0)
    # The latency legend shares the token-rank color for opus.
    tok_color = next(m["color"] for m in data["model_legend"]
                     if m["model"] == "claude-opus-4-7")
    lat_color = next(m["color"] for m in data["latency_model_legend"]
                     if m["model"] == "claude-opus-4-7")
    assert tok_color == lat_color


def test_build_dashboard_latency_empty_without_rows(session):
    # No run_latency rows -> latency sections are empty but the build still works
    # and existing analytics are unaffected.
    p = _profile(session)
    t = _ticket(session, p)
    _run(session, t, started_at=NOW, input_tokens=100)

    data = analytics.build_dashboard(session, now=NOW)
    assert data["latency_by_model"] == []
    assert data["model_vs_tool_time"] == []
    assert data["max_daily_latency"] == 0.0
    assert all(d["by_model"] == {} for d in data["latency_series"])
    # Existing fields still populate normally.
    assert data["last_30d"]["run_count"] == 1

