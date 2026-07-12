"""API tests for v2-only ticket endpoints."""
from __future__ import annotations

import pytest


async def _create_profile(client):
    r = await client.post("/api/v1/profiles", json={
        "name": "v2-tickets",
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
        "default_model": None,
        "claude_credentials": {"source": "inherit"},
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_ticket(client, pid, **kw):
    body = {"title": "t", "profile_id": pid, "source_path": "/tmp"}
    body.update(kw)
    r = await client.post("/api/v1/tickets", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _get(client, tid):
    r = await client.get(f"/api/v1/tickets/{tid}")
    assert r.status_code == 200, r.text
    return r.json()


async def test_create_defaults_to_draft(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid)
    assert t["status"] == "draft"
    assert t["position"] == 0


async def test_create_accepts_additional_dirs(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid, additional_dirs=[
        {"path": "/srv/repo", "mode": "rw"},
    ])
    assert t["additional_dirs"] == [{"path": "/srv/repo", "mode": "rw"}]


async def test_additional_dirs_rejects_relative_path(client):
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "t", "profile_id": pid,
        "additional_dirs": [{"path": "relative/path", "mode": "rw"}],
    })
    assert r.status_code == 422, r.text


async def test_additional_dirs_rejects_bad_mode(client):
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "t", "profile_id": pid,
        "additional_dirs": [{"path": "/x", "mode": "exec"}],
    })
    assert r.status_code == 422, r.text


async def test_update_accepts_additional_dirs(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid)
    r = await client.patch(f"/api/v1/tickets/{t['id']}", json={
        "additional_dirs": [{"path": "/a", "mode": "rw"}],
    })
    assert r.status_code == 200
    assert r.json()["additional_dirs"] == [{"path": "/a", "mode": "rw"}]


async def test_commit_on_finish_round_trips_through_api(client):
    """commit_on_finish is the opt-in that makes base_ref stacking work, so its
    API contract (create -> read -> patch) must hold: defaults to None, persists
    True on create, and can be flipped via PATCH."""
    pid = await _create_profile(client)

    # Default when omitted.
    t = await _create_ticket(client, pid)
    assert t["commit_on_finish"] is None

    # Set True at creation.
    t = await _create_ticket(client, pid, commit_on_finish=True)
    assert t["commit_on_finish"] is True

    # Flip it on via PATCH on a ticket created without it.
    r = await client.patch(f"/api/v1/tickets/{t['id']}", json={
        "commit_on_finish": True,
    })
    assert r.status_code == 200
    assert r.json()["commit_on_finish"] is True

    # Flip it back off explicitly (False must survive the non-None filter).
    r = await client.patch(f"/api/v1/tickets/{t['id']}", json={
        "commit_on_finish": False,
    })
    assert r.status_code == 200
    assert r.json()["commit_on_finish"] is False


async def test_create_accepts_workspace_list(client):
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "multi workspace",
        "profile_id": pid,
        "workspaces": [
            {
                "role": "primary",
                "label": "nightdesk",
                "kind": "git_worktree",
                "access": "read_write",
                "source_path": "/home/thor/fun/nightdesk",
                "worktree_name": "workspace-support",
            },
            {
                "role": "linked",
                "label": "docs",
                "kind": "directory",
                "access": "read_only",
                "source_path": "/home/thor/docs",
            },
        ],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["workspaces"][0]["source_path"] == "/home/thor/fun/nightdesk"
    assert body["workspaces"][0]["kind"] == "git_worktree"
    assert body["workspaces"][0]["role"] == "primary"
    assert body["workspaces"][0]["worktree_name"] == "workspace-support"
    assert body["workspaces"][1]["access"] == "read_only"


async def test_linked_git_worktree_must_be_read_write(client):
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "bad linked git access",
        "profile_id": pid,
        "workspaces": [
            {
                "role": "primary",
                "label": "app",
                "kind": "git_worktree",
                "access": "read_write",
                "source_path": "/home/thor/fun/app",
                "worktree_name": "feature",
            },
            {
                "role": "linked",
                "label": "api",
                "kind": "git_worktree",
                "access": "read_only",
                "source_path": "/home/thor/fun/api",
            },
        ],
    })

    assert r.status_code == 422, r.text
    assert "linked git workspaces must be read_write" in r.text


async def test_linked_git_worktree_name_must_match_primary(client):
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "bad linked git name",
        "profile_id": pid,
        "workspaces": [
            {
                "role": "primary",
                "label": "app",
                "kind": "git_worktree",
                "access": "read_write",
                "source_path": "/home/thor/fun/app",
                "worktree_name": "feature",
            },
            {
                "role": "linked",
                "label": "api",
                "kind": "git_worktree",
                "access": "read_write",
                "source_path": "/home/thor/fun/api",
                "worktree_name": "different",
            },
        ],
    })

    assert r.status_code == 422, r.text
    assert "linked git worktree name must match primary" in r.text

async def test_workspace_rejects_relative_source_path(client):
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "bad workspace",
        "profile_id": pid,
        "workspaces": [
            {
                "role": "primary",
                "label": "repo",
                "kind": "directory",
                "access": "read_write",
                "source_path": "relative/path",
            },
        ],
    })
    assert r.status_code == 422, r.text

async def test_patch_worktree_name_preserves_linked_workspaces(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid, workspaces=[
        {
            "role": "primary",
            "label": "primary",
            "kind": "git_worktree",
            "access": "read_write",
            "source_path": "/tmp",
            "worktree_name": "old",
        },
        {
            "role": "linked",
            "label": "docs",
            "kind": "directory",
            "access": "read_only",
            "source_path": "/srv/docs",
        },
    ])

    r = await client.patch(f"/api/v1/tickets/{t['id']}", json={
        "worktree_name": "new-name",
    })

    assert r.status_code == 200, r.text
    body = r.json()
    assert [w["role"] for w in body["workspaces"]] == ["primary", "linked"]
    assert body["workspaces"][0]["worktree_name"] == "new-name"
    assert body["workspaces"][1]["source_path"] == "/srv/docs"

async def test_transition_endpoint(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid)
    r = await client.post(f"/api/v1/tickets/{t['id']}/transition",
                           json={"status": "queued"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"


async def test_transition_invalid_status_422(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid)
    r = await client.post(f"/api/v1/tickets/{t['id']}/transition",
                           json={"status": "nope"})
    assert r.status_code == 422


async def test_transition_invalid_jump_409(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid)  # draft
    r = await client.post(f"/api/v1/tickets/{t['id']}/transition",
                           json={"status": "review"})
    assert r.status_code == 409


async def test_transition_to_running_does_not_set_run_now(client):
    """A bare transition to running must not invent run-now intent. run_now
    means "user bypassed the queue" and is set only via the run-now endpoint."""
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid)
    r = await client.post(f"/api/v1/tickets/{t['id']}/transition",
                           json={"status": "running"})
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert r.json()["run_now"] is False


async def test_reorder_endpoint(client):
    pid = await _create_profile(client)
    a = await _create_ticket(client, pid, title="a", status="queued")
    b = await _create_ticket(client, pid, title="b", status="queued")
    c = await _create_ticket(client, pid, title="c", status="queued")
    r = await client.post("/api/v1/tickets/reorder", json={
        "status": "queued", "ticket_ids": [c["id"], a["id"], b["id"]],
    })
    assert r.status_code == 200, r.text
    ids = [t["id"] for t in r.json()]
    assert ids[:3] == [c["id"], a["id"], b["id"]]
    positions = [t["position"] for t in r.json()][:3]
    assert positions == [0, 1, 2]


async def test_reorder_rejects_unknown_status(client):
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets/reorder", json={
        "status": "bogus", "ticket_ids": [],
    })
    assert r.status_code == 422


async def test_archive_and_unarchive(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid, status="queued")
    # queued -> running -> review.
    await client.post(f"/api/v1/tickets/{t['id']}/transition",
                       json={"status": "running"})
    await client.post(f"/api/v1/tickets/{t['id']}/transition",
                       json={"status": "review"})

    r = await client.post(f"/api/v1/tickets/{t['id']}/archive")
    assert r.status_code == 200
    assert r.json()["status"] == "archived"

    r = await client.post(f"/api/v1/tickets/{t['id']}/unarchive")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"


async def test_unarchive_incomplete_inbox_ticket_returns_to_inbox(client):
    """Regression: an incomplete inbox item (no profile/workspace) is archivable
    (archive allows any non-running status). Unarchive must send it back to
    inbox, NOT queued — otherwise the scheduler picks it and the run fails on
    the missing fields."""
    # No profile_id, no source_path/workspaces: a captured-but-incomplete
    # inbox item. (profile_id and workspaces are optional for status=inbox.)
    r = await client.post("/api/v1/tickets", json={"title": "stale triage", "status": "inbox"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["status"] == "inbox"

    ar = await client.post(f"/api/v1/tickets/{tid}/archive")
    assert ar.status_code == 200
    assert ar.json()["status"] == "archived"

    ur = await client.post(f"/api/v1/tickets/{tid}/unarchive")
    assert ur.status_code == 200
    assert ur.json()["status"] == "inbox"  # not queued


async def test_archive_from_draft(client):
    """draft -> archived is the non-destructive discard path for a ticket
    that will never run."""
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid)
    r = await client.post(f"/api/v1/tickets/{t['id']}/archive")
    assert r.status_code == 200
    assert r.json()["status"] == "archived"


async def test_archive_from_queued(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid, status="queued")
    r = await client.post(f"/api/v1/tickets/{t['id']}/archive")
    assert r.status_code == 200
    assert r.json()["status"] == "archived"


async def test_archive_rejects_running_409(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid, status="queued")
    await client.post(f"/api/v1/tickets/{t['id']}/transition",
                       json={"status": "running"})
    r = await client.post(f"/api/v1/tickets/{t['id']}/archive")
    assert r.status_code == 409


async def test_archive_from_inbox(client):
    """inbox -> archived: a triage item is archivable directly via /archive,
    not only via decline or after promotion to the board."""
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid)  # draft
    r = await client.post(f"/api/v1/tickets/{t['id']}/send-to-inbox")
    assert r.status_code == 200
    assert r.json()["status"] == "inbox"
    r = await client.post(f"/api/v1/tickets/{t['id']}/archive")
    assert r.status_code == 200
    assert r.json()["status"] == "archived"


async def test_archive_idempotent_when_already_archived(client):
    """Archiving an already-archived ticket is a no-op 200, not a 409."""
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid)
    r = await client.post(f"/api/v1/tickets/{t['id']}/archive")
    assert r.status_code == 200
    r2 = await client.post(f"/api/v1/tickets/{t['id']}/archive")
    assert r2.status_code == 200
    assert r2.json()["status"] == "archived"


async def test_archive_clears_run_now(client):
    """Archiving a queued+run_now ticket clears the scheduler-bypass flag so it
    cleanly leaves the queue and does not auto-run on a later unarchive."""
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid, status="queued")
    await client.post(f"/api/v1/tickets/{t['id']}/run-now")
    assert (await _get(client, t["id"]))["run_now"] is True
    r = await client.post(f"/api/v1/tickets/{t['id']}/archive")
    assert r.status_code == 200
    assert r.json()["run_now"] is False


async def test_bulk_archive_skips_running_archives_rest(client):
    """Bulk archive moves every non-running ticket to archived and skips
    running ones with a reason instead of failing the batch."""
    pid = await _create_profile(client)
    draft = await _create_ticket(client, pid)
    queued = await _create_ticket(client, pid, status="queued")
    running = await _create_ticket(client, pid, status="queued")
    await client.post(f"/api/v1/tickets/{running['id']}/transition",
                      json={"status": "running"})
    r = await client.post("/api/v1/tickets/bulk/archive",
                          json={"ticket_ids": [draft["id"], queued["id"], running["id"]]})
    assert r.status_code == 200
    body = r.json()
    updated_ids = {t["id"] for t in body["updated"]}
    skipped_ids = {s["ticket_id"] for s in body["skipped"]}
    assert {draft["id"], queued["id"]} <= updated_ids
    assert running["id"] in skipped_ids
    assert (await _get(client, draft["id"]))["status"] == "archived"
    assert (await _get(client, queued["id"]))["status"] == "archived"
    assert (await _get(client, running["id"]))["status"] == "running"


async def test_cancel_moves_running_to_review(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid, status="queued")
    await client.post(f"/api/v1/tickets/{t['id']}/transition",
                       json={"status": "running"})
    r = await client.post(f"/api/v1/tickets/{t['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "review"


async def test_requeue_from_review(client):
    pid = await _create_profile(client)
    t = await _create_ticket(client, pid, status="queued")
    await client.post(f"/api/v1/tickets/{t['id']}/transition",
                       json={"status": "running"})
    await client.post(f"/api/v1/tickets/{t['id']}/transition",
                       json={"status": "review"})
    r = await client.post(f"/api/v1/tickets/{t['id']}/requeue")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
