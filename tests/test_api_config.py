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
    # No windows configured: capacity is 0 and no active window.
    assert body["max_parallel"] == 0
    assert body["total_running"] == 0
    assert body["normal_running"] == 0
    assert body["run_now_running"] == 0
    # window_start/window_end are legacy fields now returned as None.
    assert body["window_start"] is None
    assert body["window_end"] is None
    assert body["in_window"] is False
    assert body["active_window"] is None
    assert body["schedule_timezone"] == "UTC"
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
                    permission_overrides=None, additional_dirs=[], run_now=False)
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
    # max_parallel now reflects ScheduleWindow capacity, not ConfigRow.max_parallel.
    # No windows are configured in this test, so capacity resolves to 0.
    assert body["max_parallel"] == 0


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


async def test_windows_bulk_replace_is_atomic(client):
    await client.post("/api/v1/config/windows",
                      json={"label": "a", "day_mask": 127, "start": "00:00", "end": "00:00",
                            "max_parallel": 1, "position": 0})
    r = await client.put("/api/v1/config/windows", json={
        "timezone": "America/New_York",
        "windows": [{"label": "overnight", "day_mask": 31, "start": "22:00",
                     "end": "07:00", "max_parallel": 3, "position": 0}],
    })
    assert r.status_code == 200
    listed = (await client.get("/api/v1/config/windows")).json()
    assert [w["label"] for w in listed] == ["overnight"]
    cfg = (await client.get("/api/v1/config")).json()
    assert cfg["schedule_timezone"] == "America/New_York"


async def test_windows_bulk_replace_empty_clears_all(client):
    await client.post("/api/v1/config/windows",
                      json={"label": "a", "day_mask": 127, "start": "00:00", "end": "00:00",
                            "max_parallel": 1, "position": 0})
    r = await client.put("/api/v1/config/windows",
                         json={"timezone": "UTC", "windows": []})
    assert r.status_code == 200
    assert (await client.get("/api/v1/config/windows")).json() == []


async def test_windows_bulk_replace_rejects_bad_timezone(client):
    r = await client.put("/api/v1/config/windows",
                         json={"timezone": "Mars/Phobos", "windows": []})
    assert r.status_code == 422


async def test_worker_status_reports_active_window(client):
    # all-day, every-day window so it always matches "now"
    await client.put("/api/v1/config/windows", json={
        "timezone": "UTC",
        "windows": [{"label": "always", "day_mask": 127, "start": "00:00",
                     "end": "00:00", "max_parallel": 4, "position": 0}],
    })
    body = (await client.get("/api/v1/worker/status")).json()
    assert body["in_window"] is True
    assert body["active_window"] == "always"
    assert body["max_parallel"] == 4
    assert body["schedule_timezone"] == "UTC"


async def test_worker_status_reports_day_spend(client, session):
    """The status endpoint surfaces today's completed-run spend for the pill."""
    from datetime import datetime, timezone
    from nightdesk.db.models import ConfigRow, Profile, Run, Ticket

    cfg = session.get(ConfigRow, 1)
    if cfg is None:
        cfg = ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t")
        session.add(cfg)
    session.commit()

    p = Profile(name="bp", fs_read=[], fs_write=[], allowed_tools=[],
                denied_tools=[], network_mode="off", network_allowlist=[],
                secret_keys=[])
    session.add(p); session.commit()
    t = Ticket(id="tb", title="t", prompt="", status="review", priority=0,
               profile_id=p.id)
    session.add(t); session.commit()
    r = Run(id="rb", ticket_id="tb", started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc), exit_status="success",
            worktree_path="/w", transcript_path="/x", host="h", cost_usd=7.5)
    session.add(r); session.commit()

    body = (await client.get("/api/v1/worker/status")).json()
    assert body["day_spend_usd"] == 7.5


async def test_worker_status_server_now_is_recent(client):
    """server_now is the backend's own clock as an aware UTC instant."""
    from datetime import datetime, timezone

    before = datetime.now(timezone.utc)
    body = (await client.get("/api/v1/worker/status")).json()
    after = datetime.now(timezone.utc)
    assert "server_now" in body
    parsed = datetime.fromisoformat(body["server_now"])
    # The instant should sit between the two samples (tolerant of tz suffix).
    assert before - timedelta(seconds=1) <= parsed <= after + timedelta(seconds=1)


async def test_worker_status_lists_running_tickets(client, session):
    """Each in-flight run surfaces its ticket as a popover link target."""
    from datetime import datetime, timezone
    from nightdesk.db.models import ConfigRow, Profile, Run, Ticket

    cfg = session.get(ConfigRow, 1)
    if cfg is None:
        cfg = ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t")
        session.add(cfg)
    session.commit()

    p = Profile(name="rp", fs_read=[], fs_write=[], allowed_tools=[],
                denied_tools=[], network_mode="off", network_allowlist=[],
                secret_keys=[])
    session.add(p); session.commit()

    def _add(idx, *, title, run_now):
        tid = f"rk{idx}"
        t = Ticket(id=tid, title=title, prompt="", status="running", priority=0,
                   position=idx, profile_id=p.id)
        session.add(t); session.commit()
        r = Run(id=f"rrk{idx}", ticket_id=tid,
                started_at=datetime.now(timezone.utc) - timedelta(minutes=idx),
                worktree_path=f"/tmp/w/{idx}", transcript_path=f"/tmp/t/{idx}.log",
                host="thorpad", started_as_run_now=run_now)
        session.add(r); session.commit()

    _add(0, title="normal job", run_now=False)
    _add(1, title="run-now job", run_now=True)
    # A finished run must NOT appear.
    t2 = Ticket(id="rk-done", title="done job", prompt="", status="review",
                priority=0, profile_id=p.id)
    session.add(t2); session.commit()
    session.add(Run(id="rrk-done", ticket_id="rk-done",
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc), exit_status="success",
                    worktree_path="/w", transcript_path="/x", host="h"))
    session.commit()

    body = (await client.get("/api/v1/worker/status")).json()
    items = body["running_tickets"]
    # Newest first: rk0 (0 min ago) before rk1 (1 min ago); finished run absent.
    assert [i["id"] for i in items] == ["rk0", "rk1"]
    by_id = {i["id"]: i for i in items}
    assert by_id["rk0"]["title"] == "normal job"
    assert by_id["rk0"]["run_now"] is False
    assert by_id["rk1"]["run_now"] is True
    assert all("started_at" in i for i in items)


async def test_worker_status_day_tokens(client, session):
    """day_tokens rolls up every token column for runs started today."""
    from datetime import datetime, timezone
    from nightdesk.db.models import ConfigRow, Profile, Run, Ticket

    cfg = session.get(ConfigRow, 1)
    if cfg is None:
        cfg = ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t")
        session.add(cfg)
    session.commit()

    p = Profile(name="tp", fs_read=[], fs_write=[], allowed_tools=[],
                denied_tools=[], network_mode="off", network_allowlist=[],
                secret_keys=[])
    session.add(p); session.commit()
    t = Ticket(id="tt", title="t", prompt="", status="review", priority=0,
               profile_id=p.id)
    session.add(t); session.commit()
    session.add(Run(id="rt", ticket_id="tt", started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc), exit_status="success",
                    worktree_path="/w", transcript_path="/x", host="h",
                    input_tokens=100, output_tokens=40, cache_read_tokens=10,
                    cache_write_tokens=5))
    session.commit()

    body = (await client.get("/api/v1/worker/status")).json()
    # 100 + 40 + 10 + 5 = 155
    assert body["day_tokens"] == 155


async def test_patch_config_sets_and_clears_worktree_base_ref(client):
    r = await client.patch("/api/v1/config", json={"worktree_base_ref": "develop"})
    assert r.status_code == 200
    assert r.json()["worktree_base_ref"] == "develop"

    # Empty string clears the default (tickets then branch from HEAD).
    r = await client.patch("/api/v1/config", json={"worktree_base_ref": ""})
    assert r.status_code == 200
    assert (r.json()["worktree_base_ref"] or "") == ""

async def test_patch_config_sets_and_clears_harness_binary_paths(client):
    r = await client.get("/api/v1/config")
    assert r.status_code == 200
    assert r.json()["claude_binary_path"] is None
    assert r.json()["opencode_binary_path"] is None

    r = await client.patch("/api/v1/config", json={
        "claude_binary_path": "/usr/local/bin/claude",
        "opencode_binary_path": "/usr/local/bin/opencode",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["claude_binary_path"] == "/usr/local/bin/claude"
    assert body["opencode_binary_path"] == "/usr/local/bin/opencode"

    # Empty string clears the override (falls back to auto-discovery).
    r = await client.patch("/api/v1/config", json={
        "claude_binary_path": "", "opencode_binary_path": "",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["claude_binary_path"] is None
    assert body["opencode_binary_path"] is None


async def test_config_api_persists_global_toolchain_presets(client):
    response = await client.patch("/api/v1/config", json={
        "toolchain_presets": {
            "user-python-tools": ["~/.local/bin"],
            "project-python": ["./.venv/bin"],
        },
    })

    assert response.status_code == 200, response.text
    assert response.json()["toolchain_presets"] == {
        "user-python-tools": ["~/.local/bin"],
        "project-python": ["./.venv/bin"],
    }


async def test_project_api_accepts_toolchain_defaults_and_ticket_overrides(client):
    profile = await client.post("/api/v1/profiles", json={
        "name": "toolchain-api",
        "claude_credentials": {"source": "inherit"},
    })
    assert profile.status_code == 201, profile.text

    project_response = await client.post("/api/v1/projects", json={
        "name": "Toolchain API",
        "source_path": "/tmp/toolchain-api",
        "default_toolchains": ["user-python-tools"],
        "default_tool_paths": ["./.venv/bin"],
    })
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    assert project["default_toolchains"] == ["user-python-tools"]
    assert project["default_tool_paths"] == ["./.venv/bin"]

    ticket_response = await client.post("/api/v1/tickets", json={
        "title": "Use tools",
        "profile_id": profile.json()["id"],
        "project_id": project["id"],
        "toolchain_overrides": {
            "enable": ["rust-user-tools"],
            "disable": ["user-python-tools"],
            "extra_paths": ["./scripts/bin"],
        },
    })
    assert ticket_response.status_code == 201, ticket_response.text
    assert ticket_response.json()["toolchain_overrides"] == {
        "enable": ["rust-user-tools"],
        "disable": ["user-python-tools"],
        "extra_paths": ["./scripts/bin"],
    }

async def test_project_api_rejects_unknown_toolchain_name(client):
    response = await client.post("/api/v1/projects", json={
        "name": "Bad Toolchain",
        "source_path": "/tmp/bad-toolchain",
        "default_toolchains": ["pyhton-project"],
    })

    assert response.status_code == 400
    assert "unknown toolchain" in response.text
