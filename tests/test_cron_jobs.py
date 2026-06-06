"""Tests for recurring cron jobs: domain, worker, API, and UI."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone

import pytest

from nightdesk.db.models import CronJobFire, DaemonStatus, ScheduleWindow, Ticket
from nightdesk.domain.cron_jobs import (
    InvalidCronJob,
    compute_next_fire,
    create_cron_job,
    disable_cron_job,
    enable_cron_job,
    fire_now,
    get_cron_job,
    list_fires,
    materialize_due_cron_jobs,
    update_cron_job,
)
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket, transition_status
from nightdesk.worker.executor import DummyExecutor
from nightdesk.worker.main import WorkerLoop, WorkerSettings


def _profile(session):
    return create_profile(
        session, name="p", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )


def _job(session, profile_id, **kw):
    kw.setdefault("title", "nightly")
    kw.setdefault("prompt", "do the thing")
    kw.setdefault("source_path", "/tmp")
    kw.setdefault("schedule", "0 9 * * *")
    return create_cron_job(session, profile_id=profile_id, **kw)


# --- domain: validation -------------------------------------------------------


def test_create_computes_next_fire_in_utc(session):
    p = _profile(session)
    # 08:00 EDT on the 24th; the next 9am EDT fire is later the same day.
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, schedule="0 9 * * *", timezone="America/New_York", now=now)
    # 9am EDT on 2026-05-24 is 13:00 UTC.
    assert job.next_fire_at is not None
    assert job.next_fire_at.replace(tzinfo=timezone.utc) == datetime(2026, 5, 24, 13, 0, tzinfo=timezone.utc)


def test_disabled_job_has_null_next_fire(session):
    p = _profile(session)
    job = _job(session, p.id, enabled=False)
    assert job.next_fire_at is None


def test_five_field_required_six_field_rejected(session):
    p = _profile(session)
    with pytest.raises(InvalidCronJob):
        _job(session, p.id, schedule="0 0 * * * *")  # 6-field
    with pytest.raises(InvalidCronJob):
        _job(session, p.id, schedule="garbage")


def test_unknown_timezone_rejected(session):
    p = _profile(session)
    with pytest.raises(InvalidCronJob):
        _job(session, p.id, timezone="Mars/Olympus")


def test_worktree_workspace_rejected(session):
    p = _profile(session)
    with pytest.raises(InvalidCronJob):
        _job(session, p.id, workspace_mode="git_worktree")
    with pytest.raises(InvalidCronJob):
        _job(session, p.id, workspace_mode="worktree")


def test_in_place_and_directory_accepted(session):
    p = _profile(session)
    assert _job(session, p.id, workspace_mode="directory").workspace_mode == "directory"
    assert _job(session, p.id, workspace_mode="in_place").workspace_mode == "in_place"


# --- domain: enable/disable next_fire_at contract -----------------------------


def test_enable_recomputes_disable_leaves_unchanged(session):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, schedule="*/30 * * * *", now=now)
    original = job.next_fire_at

    disable_cron_job(session, job.id)
    job = get_cron_job(session, job.id)
    assert job.enabled is False
    # disable leaves next_fire_at unchanged (stale value harmless; query filters enabled)
    assert job.next_fire_at == original

    later = now + timedelta(hours=2)
    enable_cron_job(session, job.id, now=later)
    job = get_cron_job(session, job.id)
    assert job.enabled is True
    assert job.next_fire_at.replace(tzinfo=timezone.utc) == compute_next_fire("*/30 * * * *", "UTC", later)


def test_schedule_change_recomputes_next_fire(session):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 5, tzinfo=timezone.utc)
    job = _job(session, p.id, schedule="0 9 * * *", now=now)
    update_cron_job(session, job.id, schedule="*/15 * * * *", now=now)
    job = get_cron_job(session, job.id)
    assert job.next_fire_at.replace(tzinfo=timezone.utc) == datetime(2026, 5, 24, 12, 15, tzinfo=timezone.utc)


# --- domain: materialization --------------------------------------------------


def test_materialize_creates_queued_run_now_false_ticket(session):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, schedule="*/15 * * * *", now=now - timedelta(minutes=1))
    fires = materialize_due_cron_jobs(session, now)
    assert len(fires) == 1
    ticket = session.get(Ticket, fires[0].ticket_id)
    assert ticket is not None
    # Critical: generated ticket does NOT bypass the scheduler.
    assert ticket.status == "queued"
    assert ticket.run_now is False
    assert ticket.title == "nightly"
    assert ticket.profile_id == p.id
    assert ticket.workspaces[0].source_path == "/tmp"
    # template copies no engine field; backend comes from the profile.
    job = get_cron_job(session, job.id)
    assert job.last_ticket_id == ticket.id


def test_force_run_materializes_run_now_ticket(session):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, schedule="*/15 * * * *", force_run=True,
               now=now - timedelta(minutes=1))
    assert job.force_run is True
    fires = materialize_due_cron_jobs(session, now)
    ticket = session.get(Ticket, fires[0].ticket_id)
    # force_run jobs produce run_now tickets the scheduler dispatches past the
    # queue and the active-hours window.
    assert ticket.run_now is True
    assert ticket.status == "queued"


def test_force_run_defaults_false(session):
    p = _profile(session)
    assert _job(session, p.id).force_run is False


def test_fire_now_honors_force_run(session):
    p = _profile(session)
    job = _job(session, p.id, force_run=True)
    ticket = fire_now(session, job.id)
    assert ticket.run_now is True


def test_update_can_toggle_force_run(session):
    p = _profile(session)
    job = _job(session, p.id)
    job = update_cron_job(session, job.id, force_run=True)
    assert job.force_run is True
    job = update_cron_job(session, job.id, force_run=False)
    assert job.force_run is False


def test_materialize_idempotent_per_job_fire(session):
    """Re-materializing the same (cron_job_id, fire_at) must not duplicate."""
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    fire_at = now - timedelta(minutes=5)
    job = _job(session, p.id, schedule="*/15 * * * *", now=fire_at - timedelta(minutes=1))
    # force next_fire_at to a known past instant
    job.next_fire_at = fire_at
    session.commit()

    materialize_due_cron_jobs(session, now)
    # Simulate a re-tick that did not observe the advance: reset to same fire_at.
    job = get_cron_job(session, job.id)
    job.next_fire_at = fire_at
    session.commit()
    materialize_due_cron_jobs(session, now)

    fires = list_fires(session, job.id)
    # Only one fire row claimed for fire_at; the constraint blocked the second.
    real = [f for f in fires if f.fire_at.replace(tzinfo=timezone.utc) == fire_at]
    assert len(real) == 1
    assert session.query(Ticket).count() == 1


def test_missed_fires_coalesce_to_one(session):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, schedule="*/30 * * * *", now=now)
    # Pretend the worker was offline for 3 hours: next_fire_at far in the past.
    job.next_fire_at = now - timedelta(hours=3)
    session.commit()

    fires = materialize_due_cron_jobs(session, now)
    # coalesce: exactly one ticket, no backfill of the 6 missed fires.
    assert len(fires) == 1
    assert session.query(Ticket).count() == 1
    job = get_cron_job(session, job.id)
    # advanced to the next fire strictly after now (<= 30 min ahead).
    nf = job.next_fire_at.replace(tzinfo=timezone.utc)
    assert nf > now
    assert nf <= now + timedelta(minutes=30)


@pytest.mark.parametrize("blocking_status", ["draft", "queued", "running"])
def test_skip_if_active_blocks_active_statuses(session, blocking_status):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, schedule="*/15 * * * *", overlap_policy="skip_if_active", now=now)
    # Seed a previous generated ticket in a blocking status.
    prev = create_ticket(session, title="prev", prompt="x", profile_id=p.id,
                         source_path="/tmp", status="queued", run_now=False)
    if blocking_status == "draft":
        prev.status = "draft"
    elif blocking_status == "running":
        prev.status = "running"
    session.commit()
    job.last_ticket_id = prev.id
    job.next_fire_at = now - timedelta(minutes=1)
    session.commit()

    before = session.query(Ticket).count()
    fires = materialize_due_cron_jobs(session, now)
    after = session.query(Ticket).count()
    # No new ticket; a skipped fire row is recorded (no silent gap).
    assert after == before
    assert len(fires) == 1
    assert fires[0].ticket_id is None
    assert fires[0].skipped_reason == "overlap"


def test_skip_if_active_does_not_block_review(session):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, schedule="*/15 * * * *", overlap_policy="skip_if_active", now=now)
    prev = create_ticket(session, title="prev", prompt="x", profile_id=p.id,
                         source_path="/tmp", status="queued", run_now=False)
    # review must NOT block a new fire.
    prev.status = "review"
    session.commit()
    job.last_ticket_id = prev.id
    job.next_fire_at = now - timedelta(minutes=1)
    session.commit()

    fires = materialize_due_cron_jobs(session, now)
    assert len(fires) == 1
    assert fires[0].ticket_id is not None
    assert fires[0].skipped_reason is None


def test_fire_now_bypasses_overlap_and_no_run_now(session):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, overlap_policy="skip_if_active", now=now)
    prev = create_ticket(session, title="prev", prompt="x", profile_id=p.id,
                         source_path="/tmp", status="queued", run_now=False)
    job.last_ticket_id = prev.id
    session.commit()

    ticket = fire_now(session, job.id)
    # Manual fire bypasses overlap and still does not set run_now.
    assert ticket.status == "queued"
    assert ticket.run_now is False
    fires = list_fires(session, job.id)
    assert any(f.ticket_id == ticket.id for f in fires)


def test_fire_now_and_run_sets_run_now(session):
    p = _profile(session)
    job = _job(session, p.id)  # force_run defaults False
    assert job.force_run is False
    ticket = fire_now(session, job.id, run=True)
    # fire-now-and-run forces run_now=True past the queue/window.
    assert ticket.status == "queued"
    assert ticket.run_now is True
    assert any(f.ticket_id == ticket.id for f in list_fires(session, job.id))


def test_fire_now_and_run_bypasses_overlap(session):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, overlap_policy="skip_if_active", now=now)
    prev = create_ticket(session, title="prev", prompt="x", profile_id=p.id,
                         source_path="/tmp", status="queued", run_now=False)
    job.last_ticket_id = prev.id
    session.commit()

    ticket = fire_now(session, job.id, run=True)
    assert ticket.run_now is True
    assert any(f.ticket_id == ticket.id for f in list_fires(session, job.id))


def test_fire_now_default_does_not_run(session):
    p = _profile(session)
    job = _job(session, p.id)
    # Plain fire-now (run defaults False) still honors force_run only.
    assert fire_now(session, job.id).run_now is False


def test_fire_now_sub_minute_does_not_collide_with_scheduled(session):
    p = _profile(session)
    now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    job = _job(session, p.id, schedule="*/15 * * * *", now=now)
    # Scheduled fire at minute granularity.
    job.next_fire_at = now
    session.commit()
    materialize_due_cron_jobs(session, now)
    # Manual fire in the same minute uses sub-minute now -> distinct fire_at.
    sub_minute = now + timedelta(seconds=12)
    t = fire_now(session, job.id, now=sub_minute)
    assert t is not None
    assert session.query(CronJobFire).filter_by(cron_job_id=job.id).count() == 2


# --- worker integration -------------------------------------------------------


def _settings(tmp_path, executor, **kw):
    kw.setdefault("max_parallel", 2)
    kw.setdefault("window_start", time(0, 0))
    kw.setdefault("window_end", time(23, 59))
    return WorkerSettings(
        worktree_root=tmp_path / "w", transcript_root=tmp_path / "t",
        secrets={}, host="th", executor=executor, **kw,
    )


@pytest.mark.anyio
async def test_tick_materializes_before_picking(engine, tmp_path):
    from sqlalchemy.orm import Session
    with Session(engine) as session:
        p = _profile(session)
        job = _job(session, p.id, schedule="*/15 * * * *",
                   now=datetime.now(timezone.utc) - timedelta(hours=1))
        # make it due now
        job.next_fire_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        # Cron tickets are window-gated normal tickets (run_now=False), so an
        # always-on schedule window must exist for the pick to happen.
        session.add(ScheduleWindow(label="always", day_mask=0b1111111,
                                   start="00:00", end="23:59", max_parallel=5,
                                   position=0))
        session.commit()
        jid = job.id

    loop = WorkerLoop(session_factory=lambda: Session(engine),
                      settings=_settings(tmp_path, DummyExecutor()))
    await loop.tick_once()
    # The cron ticket was materialized AND picked within the same tick.
    assert len(loop._inproc) == 1
    await asyncio.gather(*loop._inproc.values())
    with Session(engine) as session:
        tickets = session.query(Ticket).all()
        assert len(tickets) == 1
        # picked this tick -> ran to review (proves materialize ran first)
        assert tickets[0].status == "review"
        assert get_cron_job(session, jid).last_ticket_id == tickets[0].id


@pytest.mark.anyio
async def test_materialization_runs_even_when_backend_down(engine, tmp_path):
    """Due cron jobs must become visible queued tickets even when the readiness
    gate blocks picks — materialization sits before the gate."""
    from sqlalchemy.orm import Session
    with Session(engine) as session:
        # Backend down: cc check not ok -> gate blocks picks.
        session.add(DaemonStatus(id=1, cc_check_status="error", cc_check_message="down"))
        p = _profile(session)
        job = _job(session, p.id, schedule="*/15 * * * *",
                   now=datetime.now(timezone.utc) - timedelta(hours=1))
        job.next_fire_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.commit()

    loop = WorkerLoop(session_factory=lambda: Session(engine),
                      settings=_settings(tmp_path, DummyExecutor()))
    await loop.tick_once()
    # Nothing was picked (gate blocked), but the ticket exists and waits queued.
    assert len(loop._inproc) == 0
    with Session(engine) as session:
        tickets = session.query(Ticket).all()
        assert len(tickets) == 1
        assert tickets[0].status == "queued"
        assert tickets[0].run_now is False


# --- JSON API -----------------------------------------------------------------


async def _api_profile(client):
    r = await client.post("/api/v1/profiles", json={
        "name": "default",
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
        "default_model": None,
        "claude_credentials": {"source": "inherit"},
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.mark.anyio
async def test_api_crud_lifecycle(client):
    pid = await _api_profile(client)
    r = await client.post("/api/v1/cron-jobs", json={
        "title": "nightly", "profile_id": pid, "source_path": "/tmp",
        "schedule": "0 9 * * *", "timezone": "UTC",
    })
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["next_fire_at"] is not None
    assert job["enabled"] is True
    cid = job["id"]

    assert (await client.get("/api/v1/cron-jobs")).json().__len__() == 1
    assert (await client.get(f"/api/v1/cron-jobs/{cid}")).json()["title"] == "nightly"

    r = await client.patch(f"/api/v1/cron-jobs/{cid}", json={"title": "renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "renamed"

    # disable then enable
    assert (await client.post(f"/api/v1/cron-jobs/{cid}/disable")).json()["enabled"] is False
    assert (await client.post(f"/api/v1/cron-jobs/{cid}/enable")).json()["enabled"] is True

    r = await client.delete(f"/api/v1/cron-jobs/{cid}")
    assert r.status_code == 204
    assert (await client.get(f"/api/v1/cron-jobs/{cid}")).status_code == 404


@pytest.mark.anyio
async def test_api_force_run_roundtrips(client):
    pid = await _api_profile(client)
    r = await client.post("/api/v1/cron-jobs", json={
        "title": "x", "profile_id": pid, "source_path": "/tmp",
        "schedule": "0 9 * * *", "force_run": True,
    })
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["force_run"] is True
    # default omitted -> false
    r2 = await client.post("/api/v1/cron-jobs", json={
        "title": "y", "profile_id": pid, "source_path": "/tmp", "schedule": "0 9 * * *",
    })
    assert r2.json()["force_run"] is False
    # patch toggles it
    r3 = await client.patch(f"/api/v1/cron-jobs/{cid}", json={"force_run": False})
    assert r3.json()["force_run"] is False


@pytest.mark.anyio
async def test_cron_page_create_with_force_run(client):
    pid = await _api_profile(client)
    r = await client.post("/cron", data={
        "title": "forced", "profile_id": pid, "source_path": "/tmp",
        "schedule": "*/5 * * * *", "timezone": "UTC", "workspace_mode": "directory",
        "overlap_policy": "skip_if_active", "priority": "0", "prompt": "",
        "force_run": "1", "extra_dirs_json": "[]",
    }, follow_redirects=False)
    assert r.status_code == 303
    cid = r.headers["location"].rsplit("/", 1)[-1]
    assert (await client.get(f"/api/v1/cron-jobs/{cid}")).json()["force_run"] is True


@pytest.mark.anyio
async def test_api_invalid_schedule_and_tz_return_422(client):
    pid = await _api_profile(client)
    base = {"title": "x", "profile_id": pid, "source_path": "/tmp"}
    assert (await client.post("/api/v1/cron-jobs", json={**base, "schedule": "0 0 * * * *"})).status_code == 422
    assert (await client.post("/api/v1/cron-jobs", json={**base, "schedule": "nope"})).status_code == 422
    assert (await client.post("/api/v1/cron-jobs",
                              json={**base, "schedule": "0 9 * * *", "timezone": "Mars/X"})).status_code == 422


@pytest.mark.anyio
async def test_api_worktree_workspace_rejected_422(client):
    pid = await _api_profile(client)
    r = await client.post("/api/v1/cron-jobs", json={
        "title": "x", "profile_id": pid, "source_path": "/tmp",
        "schedule": "0 9 * * *", "workspace_mode": "git_worktree",
    })
    assert r.status_code == 422


@pytest.mark.anyio
async def test_api_fire_now_creates_queued_ticket(client, session):
    pid = await _api_profile(client)
    cid = (await client.post("/api/v1/cron-jobs", json={
        "title": "x", "profile_id": pid, "source_path": "/tmp", "schedule": "0 9 * * *",
    })).json()["id"]

    r = await client.post(f"/api/v1/cron-jobs/{cid}/fire-now")
    assert r.status_code == 201, r.text
    ticket = r.json()
    assert ticket["status"] == "queued"
    assert ticket["run_now"] is False
    # a fire row was recorded
    assert any(f.ticket_id == ticket["id"] for f in list_fires(session, cid))


@pytest.mark.anyio
async def test_api_fire_now_and_run_creates_run_now_ticket(client, session):
    pid = await _api_profile(client)
    cid = (await client.post("/api/v1/cron-jobs", json={
        "title": "x", "profile_id": pid, "source_path": "/tmp", "schedule": "0 9 * * *",
    })).json()["id"]

    r = await client.post(f"/api/v1/cron-jobs/{cid}/fire-now-and-run")
    assert r.status_code == 201, r.text
    ticket = r.json()
    assert ticket["status"] == "queued"
    assert ticket["run_now"] is True
    assert any(f.ticket_id == ticket["id"] for f in list_fires(session, cid))


@pytest.mark.anyio
async def test_api_fire_now_and_run_unknown_job_404(client):
    assert (await client.post("/api/v1/cron-jobs/nope/fire-now-and-run")).status_code == 404


@pytest.mark.anyio
async def test_api_delete_keeps_generated_tickets(client):
    pid = await _api_profile(client)
    cid = (await client.post("/api/v1/cron-jobs", json={
        "title": "x", "profile_id": pid, "source_path": "/tmp", "schedule": "0 9 * * *",
    })).json()["id"]
    tid = (await client.post(f"/api/v1/cron-jobs/{cid}/fire-now")).json()["id"]

    assert (await client.delete(f"/api/v1/cron-jobs/{cid}")).status_code == 204
    # The generated ticket is an ordinary ticket and survives schedule deletion.
    assert (await client.get(f"/api/v1/tickets/{tid}")).status_code == 200


# --- UI page ------------------------------------------------------------------


@pytest.mark.anyio
async def test_cron_page_renders_and_nav_present(client):
    r = await client.get("/cron")
    assert r.status_code == 200
    assert "Cron jobs" in r.text
    # nav item
    assert 'href="/cron"' in r.text


@pytest.mark.anyio
async def test_cron_page_create_and_list(client):
    pid = await _api_profile(client)
    r = await client.post("/cron", data={
        "title": "ui-job", "profile_id": pid, "source_path": "/tmp",
        "schedule": "*/5 * * * *", "timezone": "UTC",
        "workspace_mode": "directory", "overlap_policy": "skip_if_active",
        "priority": "0", "prompt": "", "additional_dirs": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    page = await client.get("/cron")
    assert "ui-job" in page.text


@pytest.mark.anyio
async def test_cron_page_invalid_schedule_shows_error(client):
    pid = await _api_profile(client)
    r = await client.post("/cron", data={
        "title": "bad", "profile_id": pid, "source_path": "/tmp",
        "schedule": "totally-not-cron", "timezone": "UTC",
        "workspace_mode": "directory", "overlap_policy": "skip_if_active",
        "priority": "0", "prompt": "", "additional_dirs": "",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "cron" in r.text.lower()


@pytest.mark.anyio
async def test_cron_page_enable_disable_fire_delete_forms(client, session):
    pid = await _api_profile(client)
    cid = (await client.post("/api/v1/cron-jobs", json={
        "title": "x", "profile_id": pid, "source_path": "/tmp", "schedule": "0 9 * * *",
    })).json()["id"]

    assert (await client.post(f"/cron/{cid}/disable", follow_redirects=False)).status_code == 303
    assert (await client.get(f"/api/v1/cron-jobs/{cid}")).json()["enabled"] is False
    assert (await client.post(f"/cron/{cid}/enable", follow_redirects=False)).status_code == 303
    assert (await client.get(f"/api/v1/cron-jobs/{cid}")).json()["enabled"] is True
    assert (await client.post(f"/cron/{cid}/fire-now", follow_redirects=False)).status_code == 303
    assert (await client.post(f"/cron/{cid}/delete", follow_redirects=False)).status_code == 303
    assert (await client.get(f"/api/v1/cron-jobs/{cid}")).status_code == 404


@pytest.mark.anyio
async def test_cron_page_fire_now_and_run_form(client, session):
    pid = await _api_profile(client)
    cid = (await client.post("/api/v1/cron-jobs", json={
        "title": "x", "profile_id": pid, "source_path": "/tmp", "schedule": "0 9 * * *",
    })).json()["id"]

    r = await client.post(f"/cron/{cid}/fire-now-and-run", follow_redirects=False)
    assert r.status_code == 303
    fires = list_fires(session, cid)
    assert fires and fires[0].ticket_id is not None
    t = session.get(Ticket, fires[0].ticket_id)
    assert t.run_now is True


@pytest.mark.anyio
async def test_cron_page_view_has_fire_and_run_button(client):
    pid = await _api_profile(client)
    cid = (await client.post("/api/v1/cron-jobs", json={
        "title": "x", "profile_id": pid, "source_path": "/tmp", "schedule": "0 9 * * *",
    })).json()["id"]
    page = await client.get(f"/cron/{cid}")
    assert f"/cron/{cid}/fire-now-and-run" in page.text
    assert "Fire &amp; run" in page.text or "Fire & run" in page.text


@pytest.mark.anyio
async def test_cron_preview_returns_next_fires(client):
    r = await client.post("/cron/preview",
                          data={"schedule": "0 9 * * *", "timezone": "America/New_York"})
    assert r.status_code == 200
    body = r.text
    assert "9:00" in body or "09:00" in body  # next fire rendered


@pytest.mark.anyio
async def test_cron_preview_invalid_schedule_is_inline_error(client):
    r = await client.post("/cron/preview",
                          data={"schedule": "not a cron", "timezone": "UTC"})
    assert r.status_code == 200  # inline error partial, never 500
    assert "invalid" in r.text.lower()
