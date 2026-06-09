async def _create_profile(client):
    r = await client.post("/api/v1/profiles", json={
        "name": "tickets-test",
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
        "default_model": None,
        "claude_credentials": {"source": "inherit"},
    })
    return r.json()["id"]


async def test_full_ticket_lifecycle(client):
    pid = await _create_profile(client)

    r = await client.post("/api/v1/tickets", json={
        "title": "do thing", "prompt": "p",
        "priority": 1, "profile_id": pid, "source_path": "/tmp", "run_now": False,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    tid = body["id"]
    # v2: tickets default to draft.
    assert body["status"] == "draft"

    r = await client.get("/api/v1/tickets")
    assert r.status_code == 200
    assert any(t["id"] == tid for t in r.json())

    # Priority uses the named 0-4 scale; out-of-range values are rejected.
    r = await client.patch(f"/api/v1/tickets/{tid}", json={"priority": 9})
    assert r.status_code == 422

    r = await client.patch(f"/api/v1/tickets/{tid}", json={"priority": 4})
    assert r.json()["priority"] == 4

    # Run-now on a draft ticket must do BOTH: set the flag AND transition
    # to queued. The scheduler's WHERE status='queued' clause would
    # otherwise filter a flag-only ticket out forever.
    r = await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert r.status_code == 200
    body = r.json()
    assert body["run_now"] is True
    assert body["status"] == "queued"

    r = await client.delete(f"/api/v1/tickets/{tid}")
    assert r.status_code == 204


async def test_run_now_transitions_draft_to_queued(client):
    """Regression: the JSON Run-now endpoint used to only flip the flag,
    leaving draft tickets parked forever (scheduler filters by
    status='queued')."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "draft-then-run-now", "prompt": "p",
        "priority": 1, "profile_id": pid, "source_path": "/tmp",
    })
    assert r.status_code == 201
    tid = r.json()["id"]
    assert r.json()["status"] == "draft"

    r = await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["run_now"] is True


async def test_run_now_on_queued_is_idempotent(client):
    """Queued + run_now is the scheduler's pick-now trigger; the flag flip
    should be safe to repeat without changing status."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "already-queued", "prompt": "p",
        "profile_id": pid, "source_path": "/tmp", "status": "queued",
    })
    assert r.status_code == 201
    tid = r.json()["id"]

    for _ in range(2):
        r = await client.post(f"/api/v1/tickets/{tid}/run-now")
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["run_now"] is True


async def test_run_now_on_running_returns_409(client):
    """Don't let an accidental click restart a live run."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "live", "prompt": "p",
        "profile_id": pid, "source_path": "/tmp", "status": "running",
    })
    assert r.status_code == 201
    tid = r.json()["id"]

    r = await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert r.status_code == 409


async def test_run_now_transitions_review_to_queued(client):
    """Review tickets (post-run audit state) can be re-run, and Run-now is
    the one-click way to do it."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "rev", "profile_id": pid, "source_path": "/tmp", "status": "queued",
    })
    tid = r.json()["id"]
    # queued -> running -> review via the transition endpoint.
    await client.post(f"/api/v1/tickets/{tid}/transition",
                       json={"status": "running"})
    await client.post(f"/api/v1/tickets/{tid}/transition",
                       json={"status": "review"})

    r = await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["run_now"] is True
