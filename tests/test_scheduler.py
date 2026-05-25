from datetime import datetime, time, timedelta, timezone

from nightdesk.worker.scheduler import in_window, pick_eligible
from nightdesk.db.models import Run, ScheduleWindow, Ticket
from nightdesk.domain.tickets import create_ticket


# Day bitmask reference: Mon=1 Tue=2 Wed=4 Thu=8 Fri=16 Sat=32 Sun=64.
ALL_DAYS = 127

# 2026-05-09 is a Saturday; 2026-05-11 is a Monday. Used to drive day_mask cases.
SATURDAY = datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc)
MONDAY = datetime(2026, 5, 11, 23, 0, tzinfo=timezone.utc)


def _qt(session, sample_profile, **kw):
    fields = dict(title="t", prompt="", priority=0,
                   profile_id=sample_profile.id, cwd="/tmp",
                   run_now=False, status="queued")
    fields.update(kw)
    return create_ticket(session, **fields)


def _window(session, *, label="w", day_mask=ALL_DAYS, start="22:00",
            end="07:00", max_parallel=5, position=0):
    w = ScheduleWindow(label=label, day_mask=day_mask, start=start, end=end,
                       max_parallel=max_parallel, position=position)
    session.add(w)
    session.commit()
    return w


def _completed_run(session, ticket_id, *, started_at, cost):
    """Seed a finished, priced run so budget sums see spend."""
    r = Run(ticket_id=ticket_id, started_at=started_at,
            finished_at=started_at + timedelta(seconds=10),
            exit_status="success", worktree_path="/w",
            transcript_path="/x", host="h", cost_usd=cost)
    session.add(r)
    session.commit()
    return r


def test_in_window_simple():
    assert in_window(time(0, 0), time(12, 0), datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc))
    assert not in_window(time(0, 0), time(12, 0), datetime(2026, 5, 9, 13, 0, tzinfo=timezone.utc))


def test_in_window_wraps_midnight():
    assert in_window(time(22, 0), time(7, 0), datetime(2026, 5, 9, 1, 0, tzinfo=timezone.utc))
    assert in_window(time(22, 0), time(7, 0), datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc))
    assert not in_window(time(22, 0), time(7, 0), datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc))


def test_in_window_equal_means_always_on():
    """Settings UI emits 00:00 -> 00:00 for the Always-on toggle. The
    scheduler treats any equal start/end pair as 'no restriction'."""
    for hr in (0, 6, 12, 18, 23):
        now = datetime(2026, 5, 9, hr, 0, tzinfo=timezone.utc)
        assert in_window(time(0, 0), time(0, 0), now)
        assert in_window(time(13, 0), time(13, 0), now)


# --- Multi-window: no match ------------------------------------------------


def test_no_window_rows_means_no_normal_dispatch(session, sample_profile):
    """With zero windows configured, only run-now tickets ever dispatch."""
    _qt(session, sample_profile, title="normal", run_now=False)
    rn = _qt(session, sample_profile, title="rn", run_now=True)
    picked = pick_eligible(session, now=SATURDAY, total_running=0)
    assert [t.id for t in picked] == [rn.id]


def test_no_matching_window_by_time(session, sample_profile):
    """A window exists but the current time falls outside it: no normal pick."""
    _window(session, start="22:00", end="07:00", max_parallel=5)
    _qt(session, sample_profile, title="normal", run_now=False)
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)  # noon, outside 22-07
    picked = pick_eligible(session, now=now, total_running=0)
    assert picked == []


def test_no_matching_window_by_day(session, sample_profile):
    """Window is weekdays-only (Mon-Fri); on Saturday it must not match."""
    weekdays = 1 | 2 | 4 | 8 | 16  # Mon..Fri
    _window(session, day_mask=weekdays, start="22:00", end="07:00", max_parallel=5)
    _qt(session, sample_profile, title="normal", run_now=False)
    # Saturday at 23:00: time is inside but the day_mask excludes it.
    picked = pick_eligible(session, now=SATURDAY, total_running=0)
    assert picked == []
    # Monday at 23:00: both day and time match.
    picked = pick_eligible(session, now=MONDAY, total_running=0)
    assert len(picked) == 1


def test_run_now_dispatches_with_no_matching_window(session, sample_profile):
    """run-now bypasses windows entirely, even when none match."""
    weekdays = 1 | 2 | 4 | 8 | 16
    _window(session, day_mask=weekdays, start="22:00", end="07:00", max_parallel=5)
    rn = _qt(session, sample_profile, title="rn", run_now=True)
    _qt(session, sample_profile, title="normal", run_now=False)
    picked = pick_eligible(session, now=SATURDAY, total_running=0)
    assert [t.id for t in picked] == [rn.id]


# --- Multi-window: single match --------------------------------------------


def test_single_matching_window_caps_normal(session, sample_profile):
    _window(session, start="22:00", end="07:00", max_parallel=3)
    for i in range(5):
        _qt(session, sample_profile, title=f"t{i}", run_now=False)
    picked = pick_eligible(session, now=SATURDAY, total_running=2)
    # capacity = max(0, 3 - 2) = 1
    assert len(picked) == 1


def test_single_match_priority_then_created(session, sample_profile):
    _window(session, start="22:00", end="07:00", max_parallel=5)
    older_high = _qt(session, sample_profile, title="oh", priority=5, run_now=False)
    newer_low = _qt(session, sample_profile, title="nl", priority=0, run_now=False)
    picked = pick_eligible(session, now=SATURDAY, total_running=0)
    assert [t.id for t in picked] == [older_high.id, newer_low.id]


def test_single_match_run_now_bypasses_capacity(session, sample_profile):
    _window(session, start="22:00", end="07:00", max_parallel=4)
    a = _qt(session, sample_profile, title="a", run_now=True)
    b = _qt(session, sample_profile, title="b", run_now=True)
    # 4 running, cap 4 → capacity 0; both run-now still picked.
    picked = pick_eligible(session, now=SATURDAY, total_running=4)
    assert {t.id for t in picked} == {a.id, b.id}


# --- Multi-window: overlapping windows -------------------------------------


def test_overlapping_windows_use_highest_max_parallel(session, sample_profile):
    """Two windows match the same instant; the most permissive cap wins."""
    _window(session, label="lo", start="22:00", end="07:00", max_parallel=2, position=0)
    _window(session, label="hi", start="20:00", end="08:00", max_parallel=6, position=1)
    for i in range(10):
        _qt(session, sample_profile, title=f"t{i}", run_now=False)
    # Highest cap is 6, nothing running → 6 picked.
    picked = pick_eligible(session, now=SATURDAY, total_running=0)
    assert len(picked) == 6


def test_overlapping_windows_only_one_matches_time(session, sample_profile):
    """When windows overlap in definition but only one covers `now`, use it."""
    # Daytime window (cap 1) and overnight window (cap 9). At 23:00 only the
    # overnight one matches.
    _window(session, label="day", start="09:00", end="17:00", max_parallel=1, position=0)
    _window(session, label="night", start="22:00", end="07:00", max_parallel=9, position=1)
    for i in range(5):
        _qt(session, sample_profile, title=f"t{i}", run_now=False)
    picked = pick_eligible(session, now=SATURDAY, total_running=0)
    assert len(picked) == 5  # overnight cap 9 covers all 5 queued


def test_overlapping_windows_distinct_days(session, sample_profile):
    """Two windows with the same time but different days; only the matching
    day's cap applies."""
    weekend = 32 | 64  # Sat | Sun
    weekday = 1 | 2 | 4 | 8 | 16  # Mon..Fri
    _window(session, label="weekend", day_mask=weekend, start="22:00",
            end="07:00", max_parallel=8, position=0)
    _window(session, label="weekday", day_mask=weekday, start="22:00",
            end="07:00", max_parallel=2, position=1)
    for i in range(10):
        _qt(session, sample_profile, title=f"t{i}", run_now=False)
    # Saturday → weekend cap 8.
    picked = pick_eligible(session, now=SATURDAY, total_running=0)
    assert len(picked) == 8


# --- Timezone-aware window evaluation --------------------------------------


def test_window_matches_uses_configured_timezone():
    from zoneinfo import ZoneInfo
    from nightdesk.worker.scheduler import window_matches
    w = ScheduleWindow(label="ny-eve", day_mask=1, start="20:00", end="23:00",
                       max_parallel=1, position=0)  # Monday 20:00-23:00 local
    tz = ZoneInfo("America/New_York")
    # Mon 21:00 EDT == Tue 01:00 UTC. UTC eval would see Tuesday and miss it.
    inside = datetime(2026, 5, 12, 1, 0, tzinfo=timezone.utc)
    assert window_matches(w, inside, tz) is True
    # Mon 18:00 EDT == Mon 22:00 UTC, before the window opens.
    before = datetime(2026, 5, 11, 22, 0, tzinfo=timezone.utc)
    assert window_matches(w, before, tz) is False


# --- Budget guardrails -----------------------------------------------------
# SATURDAY (2026-05-09 23:00 UTC) falls inside the default _window (22:00-07:00,
# all days), so a window match is never the reason normal picks pause here.
_NOW = datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc)


def test_budget_under_cap_allows_normal_picks(session, sample_profile):
    """A daily budget that hasn't been reached doesn't pause normal picks."""
    _window(session)
    normal = _qt(session, sample_profile, title="normal", run_now=False)
    _completed_run(session, normal.id, started_at=_NOW, cost=1.0)
    picked = pick_eligible(session, now=_NOW, total_running=0, daily_budget_usd=10.0)
    assert [t.id for t in picked] == [normal.id]


def test_daily_budget_exceeded_pauses_normal_but_honors_run_now(session, sample_profile):
    """Once the daily budget is reached, normal queued picks pause while a
    run_now ticket still dispatches (user's explicit choice bypasses)."""
    _window(session)
    normal = _qt(session, sample_profile, title="normal", run_now=False)
    forced = _qt(session, sample_profile, title="forced", run_now=True)
    # Spend $12 today against a $10 daily cap.
    _completed_run(session, normal.id, started_at=_NOW, cost=12.0)

    picked = pick_eligible(session, now=_NOW, total_running=0, daily_budget_usd=10.0)
    # Only the run_now ticket is dispatched; the normal one is held.
    assert [t.id for t in picked] == [forced.id]


def test_monthly_budget_exceeded_pauses_normal_picks(session, sample_profile):
    _window(session)
    normal = _qt(session, sample_profile, title="normal", run_now=False)
    # Spread spend across the month so 'today' alone wouldn't trip a daily cap.
    _completed_run(session, normal.id, started_at=_NOW.replace(day=2), cost=6.0)
    _completed_run(session, normal.id, started_at=_NOW.replace(day=5), cost=6.0)

    picked = pick_eligible(session, now=_NOW, total_running=0, monthly_budget_usd=10.0)
    assert picked == []


def test_no_budget_set_never_pauses(session, sample_profile):
    _window(session)
    normal = _qt(session, sample_profile, title="normal", run_now=False)
    _completed_run(session, normal.id, started_at=_NOW, cost=999.0)
    picked = pick_eligible(session, now=_NOW, total_running=0)
    assert [t.id for t in picked] == [normal.id]


def test_pick_eligible_orders_normal_by_position_then_priority(session, sample_profile):
    """Normal picks honor (position ASC, priority DESC, created_at ASC)."""
    from nightdesk.domain.tickets import reorder_in_column
    _window(session)
    a = _qt(session, sample_profile, title="a", priority=0, run_now=False)
    b = _qt(session, sample_profile, title="b", priority=9, run_now=False)
    c = _qt(session, sample_profile, title="c", priority=5, run_now=False)
    # Force ordering: c, a, b
    reorder_in_column(session, "queued", [c.id, a.id, b.id])
    picked = pick_eligible(session, now=SATURDAY, total_running=0)
    assert [t.id for t in picked] == [c.id, a.id, b.id]


def test_window_matches_dst_boundary():
    from zoneinfo import ZoneInfo
    from nightdesk.worker.scheduler import window_matches
    tz = ZoneInfo("America/New_York")
    w = ScheduleWindow(label="daily9", day_mask=127, start="09:00", end="10:00",
                       max_parallel=1, position=0)
    # 2026-03-09 Monday, EDT (UTC-4): 09:30 EDT == 13:30 UTC.
    assert window_matches(w, datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc), tz) is True
    # 2026-01-05 Monday, EST (UTC-5): 09:30 EST == 14:30 UTC.
    assert window_matches(w, datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc), tz) is True


def test_window_matches_utc_default_unchanged():
    from zoneinfo import ZoneInfo
    from nightdesk.worker.scheduler import window_matches
    w = ScheduleWindow(label="w", day_mask=127, start="22:00", end="07:00",
                       max_parallel=1, position=0)
    assert window_matches(w, datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc), ZoneInfo("UTC")) is True
    assert window_matches(w, datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc), ZoneInfo("UTC")) is False
