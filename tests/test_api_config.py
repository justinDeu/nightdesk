from datetime import datetime, timezone, timedelta


async def test_get_and_patch_config(client):
    r = await client.get("/api/v1/config")
    assert r.status_code == 200
    body = r.json()
    assert body["max_parallel"] == 2

    r = await client.patch("/api/v1/config", json={"max_parallel": 4})
    assert r.json()["max_parallel"] == 4


async def test_patch_config_valid_window_time(client):
    r = await client.patch("/api/v1/config", json={"window_start": "23:30"})
    assert r.status_code == 200
    assert r.json()["window_start"] == "23:30"


async def test_patch_config_invalid_window_time(client):
    r = await client.patch("/api/v1/config", json={"window_start": "25:99"})
    assert r.status_code == 422


async def test_worker_status_no_heartbeat(client):
    r = await client.get("/api/v1/worker/status")
    assert r.status_code == 200
    body = r.json()
    assert body["pid"] is None
    assert body["host"] is None
    # No heartbeat ever recorded => considered stale.
    assert body["stale"] is True
    # Defaults are reported from the ConfigRow.
    assert body["max_parallel"] >= 1
    assert body["total_running"] == 0
    assert body["normal_running"] == 0
    assert body["run_now_running"] == 0
    assert body["window_start"] == "22:00"
    assert body["window_end"] == "07:00"
    assert "in_window" in body
    # Legacy field kept for back-compat.
    assert body["running_count"] == 0


async def test_worker_status_recent_heartbeat_not_stale(client, session):
    from nightdesk.db.models import WorkerHeartbeat
    hb = WorkerHeartbeat(id=1, host="thorpad", pid=4242,
                          last_seen_at=datetime.now(timezone.utc))
    session.add(hb); session.commit()

    r = await client.get("/api/v1/worker/status")
    assert r.status_code == 200
    body = r.json()
    assert body["host"] == "thorpad"
    assert body["pid"] == 4242
    assert body["stale"] is False


async def test_worker_status_stale_when_heartbeat_old(client, session):
    from nightdesk.db.models import WorkerHeartbeat
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    hb = WorkerHeartbeat(id=1, host="thorpad", pid=4242, last_seen_at=old)
    session.add(hb); session.commit()

    r = await client.get("/api/v1/worker/status")
    body = r.json()
    assert body["stale"] is True


async def test_worker_status_counts_running_with_overflow(client, session):
    """3 normal + 2 run-now (overflow) running, max_parallel=4."""
    from nightdesk.db.models import ConfigRow, Profile, Run, Ticket
    from datetime import datetime, timezone

    # Bump max_parallel to 4.
    cfg = session.get(ConfigRow, 1)
    if cfg is None:
        cfg = ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
                          window_start="22:00", window_end="07:00", max_parallel=4)
        session.add(cfg)
    else:
        cfg.max_parallel = 4
    session.commit()

    p = Profile(name="wsp", fs_read=[], fs_write=[], allowed_tools=[],
                 denied_tools=[], network_mode="off", network_allowlist=[],
                 secret_keys=[])
    session.add(p); session.commit()

    def _add_running(idx: int, *, run_now: bool):
        tid = f"tk{idx}"
        t = Ticket(id=tid, title=f"t{idx}", prompt="",
                    status="running", priority=0, position=idx, profile_id=p.id,
                    permission_overrides=None, additional_dirs=[],
                    cwd="/tmp", run_now=False)
        session.add(t); session.commit()
        r = Run(id=f"r{idx}", ticket_id=tid,
                 started_at=datetime.now(timezone.utc),
                 worktree_path=f"/tmp/w/{idx}", transcript_path=f"/tmp/t/{idx}.log",
                 host="thorpad", started_as_run_now=run_now)
        session.add(r); session.commit()
        t.current_run_id = r.id
        session.commit()

    for i in range(3):
        _add_running(i, run_now=False)
    for i in range(3, 5):
        _add_running(i, run_now=True)

    r = await client.get("/api/v1/worker/status")
    body = r.json()
    assert body["total_running"] == 5
    assert body["run_now_running"] == 2
    assert body["normal_running"] == 3
    assert body["max_parallel"] == 4


async def test_windows_crud_lifecycle(client):
    # Empty to start.
    r = await client.get("/api/v1/config/windows")
    assert r.status_code == 200
    assert r.json() == []

    # Create two windows.
    r = await client.post("/api/v1/config/windows", json={
        "label": "overnight", "day_mask": 127, "start": "22:00",
        "end": "07:00", "max_parallel": 3, "position": 1,
    })
    assert r.status_code == 201
    w1 = r.json()
    assert w1["label"] == "overnight"
    assert w1["max_parallel"] == 3
    assert isinstance(w1["id"], int)

    r = await client.post("/api/v1/config/windows", json={
        "label": "weekday-daytime", "day_mask": 31, "start": "09:00",
        "end": "17:00", "max_parallel": 1, "position": 0,
    })
    assert r.status_code == 201
    w2 = r.json()

    # Listed ordered by position (w2 has position 0).
    r = await client.get("/api/v1/config/windows")
    listed = r.json()
    assert [w["label"] for w in listed] == ["weekday-daytime", "overnight"]

    # ConfigOut surfaces the same windows.
    r = await client.get("/api/v1/config")
    assert [w["label"] for w in r.json()["windows"]] == ["weekday-daytime", "overnight"]

    # Sparse PATCH updates only provided fields.
    r = await client.patch(f"/api/v1/config/windows/{w1['id']}", json={"max_parallel": 9})
    assert r.status_code == 200
    patched = r.json()
    assert patched["max_parallel"] == 9
    assert patched["start"] == "22:00"  # untouched

    # Delete one.
    r = await client.delete(f"/api/v1/config/windows/{w2['id']}")
    assert r.status_code == 204
    r = await client.get("/api/v1/config/windows")
    assert [w["id"] for w in r.json()] == [w1["id"]]


async def test_window_patch_and_delete_404(client):
    r = await client.patch("/api/v1/config/windows/9999", json={"max_parallel": 1})
    assert r.status_code == 404
    r = await client.delete("/api/v1/config/windows/9999")
    assert r.status_code == 404


async def test_window_create_rejects_bad_time_and_day_mask(client):
    r = await client.post("/api/v1/config/windows", json={"start": "25:00"})
    assert r.status_code == 422
    r = await client.post("/api/v1/config/windows", json={"day_mask": 999})
    assert r.status_code == 422


async def test_patch_config_sets_and_clears_worktree_base_ref(client):
    r = await client.patch("/api/v1/config", json={"worktree_base_ref": "develop"})
    assert r.status_code == 200
    assert r.json()["worktree_base_ref"] == "develop"

    # Empty string clears the default (tickets then branch from HEAD).
    r = await client.patch("/api/v1/config", json={"worktree_base_ref": ""})
    assert r.status_code == 200
    assert (r.json()["worktree_base_ref"] or "") == ""
