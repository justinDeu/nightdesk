from datetime import datetime, time, timezone

from nightdesk.worker.scheduler import in_window, pick_eligible
from nightdesk.db.models import Ticket
from nightdesk.domain.tickets import create_ticket


def _qt(session, sample_profile, **kw):
    fields = dict(title="t", prompt="", priority=0,
                   profile_id=sample_profile.id, cwd="/tmp",
                   run_now=False, status="queued")
    fields.update(kw)
    return create_ticket(session, **fields)


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


def test_pick_eligible_respects_run_now_and_window(session, sample_profile):
    a = _qt(session, sample_profile, title="a", priority=1, run_now=False)
    b = _qt(session, sample_profile, title="b", priority=0, run_now=True)
    # Out of window, only run_now ticket is eligible.
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    picked = pick_eligible(session, now=now, window_start=time(22, 0), window_end=time(7, 0),
                              max_parallel=5, total_running=0)
    assert [t.id for t in picked] == [b.id]


def test_pick_eligible_priority_then_created_at_for_normal(session, sample_profile):
    older_high = _qt(session, sample_profile, title="oh", priority=5, run_now=False)
    newer_low = _qt(session, sample_profile, title="nl", priority=0, run_now=False)
    # Inside window so normal picks apply.
    picked = pick_eligible(session, now=datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc),
                              window_start=time(22, 0), window_end=time(7, 0),
                              max_parallel=5, total_running=0)
    assert [t.id for t in picked] == [older_high.id, newer_low.id]


def test_pick_eligible_caps_normal_at_capacity(session, sample_profile):
    for i in range(5):
        _qt(session, sample_profile, title=f"t{i}", run_now=False)
    picked = pick_eligible(session, now=datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc),
                              window_start=time(22, 0), window_end=time(7, 0),
                              max_parallel=3, total_running=2)
    assert len(picked) == 1


def test_pick_eligible_run_now_bypasses_capacity(session, sample_profile):
    """All run_now=true tickets are picked even when over capacity."""
    a = _qt(session, sample_profile, title="a", run_now=True)
    b = _qt(session, sample_profile, title="b", run_now=True)
    # 4 already running, max_parallel=4 → capacity 0; both run-now still picked.
    picked = pick_eligible(session, now=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
                              window_start=time(22, 0), window_end=time(7, 0),
                              max_parallel=4, total_running=4)
    assert {t.id for t in picked} == {a.id, b.id}


# --- Spec-aligned overflow scenarios (max_parallel=4) ----------------------


def test_overflow_then_queued_normal_no_picks(session, sample_profile):
    """Spec scenario: 3 normal running + 2 run-now picked previously = 5 total
    (capacity=0). A queued normal arrives next tick: no pick happens.
    """
    _qt(session, sample_profile, title="normal", run_now=False)

    picked = pick_eligible(
        session,
        now=datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc),
        window_start=time(22, 0), window_end=time(7, 0),
        max_parallel=4, total_running=5,  # overflow already in effect
    )
    assert picked == []


def test_overflow_drains_then_normal_pick(session, sample_profile):
    """Spec scenario continuation: both run-nows finish, total drops to 3/4
    (capacity=1), scheduler picks one queued normal next tick.
    """
    n1 = _qt(session, sample_profile, title="normal-1", run_now=False, priority=5)
    n2 = _qt(session, sample_profile, title="normal-2", run_now=False, priority=0)

    picked = pick_eligible(
        session,
        now=datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc),
        window_start=time(22, 0), window_end=time(7, 0),
        max_parallel=4, total_running=3,
    )
    # One slot of capacity → highest-priority queued normal picked.
    assert [t.id for t in picked] == [n1.id]


def test_overflow_four_normal_no_room_then_one_run_now(session, sample_profile):
    """4 normal running + 1 run-now still bypasses, no normal gets picked."""
    rn = _qt(session, sample_profile, title="rn", run_now=True)
    _qt(session, sample_profile, title="normal", run_now=False)

    picked = pick_eligible(
        session,
        now=datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc),
        window_start=time(22, 0), window_end=time(7, 0),
        max_parallel=4, total_running=4,
    )
    picked_ids = [t.id for t in picked]
    # capacity = max(0, 4-4) = 0 → only run-now is picked.
    assert picked_ids == [rn.id]


def test_no_picks_when_outside_window_and_no_run_now(session, sample_profile):
    _qt(session, sample_profile, title="normal", run_now=False)
    picked = pick_eligible(
        session,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
        window_start=time(22, 0), window_end=time(7, 0),
        max_parallel=4, total_running=0,
    )
    assert picked == []


def test_pick_eligible_orders_normal_by_position_then_priority(session, sample_profile):
    """Normal picks honor (position ASC, priority DESC, created_at ASC)."""
    from nightdesk.domain.tickets import reorder_in_column
    a = _qt(session, sample_profile, title="a", priority=0, run_now=False)
    b = _qt(session, sample_profile, title="b", priority=9, run_now=False)
    c = _qt(session, sample_profile, title="c", priority=5, run_now=False)
    # Force ordering: c, a, b
    reorder_in_column(session, "queued", [c.id, a.id, b.id])

    picked = pick_eligible(
        session,
        now=datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc),
        window_start=time(22, 0), window_end=time(7, 0),
        max_parallel=10, total_running=0,
    )
    assert [t.id for t in picked] == [c.id, a.id, b.id]
