"""End-to-end Inbox lifecycle: capture -> triage -> promote.

The per-surface suites cover each piece in isolation (test_inbox_ui.py for the
domain boundary + capture/decline routes, test_inbox_promote.py for the promote
modal, test_inbox_keyboard.py for the keyboard triage flow). This module ties
them together and asserts the cross-cutting guarantees the integration branch is
responsible for, driving everything through the HTTP surface a real client uses:

  1. Capture: an under-specified item (no workspace) is accepted into the inbox
     and does NOT break the normal ticket flow or leak onto the board.
  2. Triage: metadata edits (priority, project) reuse the shared board metadata
     routes; the item stays in the inbox throughout and stays off the board.
  3. Promotion enforces the runnable-field boundary: a promote with the workspace
     still missing is rejected (422) and the item stays in the inbox.
  4. Once fleshed out, promotion moves the item onto the board and makes it
     schedulable; the inbox empties.
  5. Project-scoped inbox: capture into a project, see it only under that
     project's view, and promote it from there.
"""
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import (
    get_ticket,
    list_inbox,
    request_run_now,
)
from nightdesk.worker.scheduler import pick_eligible


# ---------------------------------------------------------------------------
# Fixtures (reuse conftest `app`/`client`; add a cookie client for the HTMX
# inbox/board routes, which authenticate via cookie-or-bearer).
# ---------------------------------------------------------------------------


@pytest.fixture
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


@pytest.fixture
def profile(session):
    return create_profile(
        session,
        name="e2e-test",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


def _only_inbox_id(session):
    items = list_inbox(session)
    assert len(items) == 1, f"expected exactly one inbox item, got {len(items)}"
    return items[0].id


def _promote_modal_form(**overrides):
    """A complete promote-modal submission: workspace filled in, target queued."""
    form = {
        "workspace_form": "1",
        "title": "Capture turned runnable",
        "prompt": "Now it has a real prompt and a workspace.",
        "source_path": "/tmp/e2e-project",
        "primary_kind": "directory",
        "priority": "4",
        "target": "queued",
        "project": "",
    }
    form.update(overrides)
    return form


# ---------------------------------------------------------------------------
# Full lifecycle: capture -> triage -> promote (loose item, explicit workspace)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_capture_triage_promote_lifecycle(cookie_client, client, session, profile):
    # 1. CAPTURE — an under-specified item, only a title + prompt, no workspace.
    r = await cookie_client.post(
        "/inbox/capture",
        data={"title": "ETETITLE vague idea", "prompt": "figure this out later"},
    )
    assert r.status_code in (200, 303)
    session.expire_all()
    tid = _only_inbox_id(session)
    captured = get_ticket(session, tid)
    assert captured.status == "inbox"
    assert captured.workspaces == []  # genuinely under-specified

    # ...and it must NOT have leaked onto the runnable board.
    board = await cookie_client.get("/board/columns")
    assert board.status_code == 200
    assert "ETETITLE" not in board.text

    # ...and the scheduler must never pick an inbox item.
    assert pick_eligible(session, now=datetime.now(timezone.utc), total_running=0) == []

    # 2. TRIAGE — edit priority via the shared board metadata route (JSON API).
    r = await client.patch(f"/api/v1/tickets/{tid}/priority", json={"priority": 4})
    assert r.status_code == 200
    assert r.json()["priority"] == 4
    session.expire_all()
    assert get_ticket(session, tid).status == "inbox"  # triage never leaves the inbox

    # Still off the board after triage.
    board = await cookie_client.get("/board/columns")
    assert "ETETITLE" not in board.text

    # 3. PROMOTION ENFORCES RUNNABLE FIELDS — promoting with the workspace still
    #    missing is rejected, and the item stays put.
    r = await cookie_client.post(
        f"/inbox/tickets/{tid}/promote",
        data={"target": "draft"},
    )
    assert r.status_code == 422
    session.expire_all()
    assert get_ticket(session, tid).status == "inbox"

    # 4. FLESH OUT + PROMOTE — the modal posts the full edit form (workspace
    #    included) and crosses the completeness boundary in one step.
    r = await cookie_client.post(
        f"/inbox/tickets/{tid}/promote",
        data=_promote_modal_form(title="ETETITLE now runnable"),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    promoted = get_ticket(session, tid)
    assert promoted.status == "queued"
    assert promoted.workspaces, "promotion should have persisted the workspace"
    # The inbox is now empty.
    assert list_inbox(session) == []

    # It now lives on the board's queued column.
    board = await cookie_client.get("/board/columns")
    assert "ETETITLE" in board.text

    # And it is schedulable: flag run-now and the scheduler picks it up.
    request_run_now(session, tid)
    session.commit()
    picked = pick_eligible(session, now=datetime.now(timezone.utc), total_running=0)
    assert tid in {t.id for t in picked}


# ---------------------------------------------------------------------------
# Under-specified inbox items don't break the normal (non-inbox) ticket flow
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_underspecified_inbox_does_not_break_board_flow(
    cookie_client, client, session, profile
):
    # An under-specified inbox item sitting in the queue...
    await cookie_client.post("/inbox/capture", data={"title": "LOOSEITEM", "prompt": ""})

    # ...does not stop a normal ticket from being created, queued and scheduled.
    r = await client.post(
        "/api/v1/tickets",
        json={
            "title": "NORMALTICKET",
            "prompt": "do the thing",
            "profile_id": profile.id,
            "status": "queued",
            "workspaces": [
                {
                    "role": "primary",
                    "label": "primary",
                    "kind": "directory",
                    "access": "read_write",
                    "source_path": "/tmp/normal",
                    "retention": "preserve",
                }
            ],
        },
    )
    assert r.status_code in (200, 201)
    normal_id = r.json()["id"]

    session.expire_all()
    # The normal ticket is scheduler-eligible; the inbox item is not.
    request_run_now(session, normal_id)
    session.commit()
    picked = {t.id for t in pick_eligible(
        session, now=datetime.now(timezone.utc), total_running=0
    )}
    assert normal_id in picked
    inbox_ids = {t.id for t in list_inbox(session)}
    assert inbox_ids and not (inbox_ids & picked)


# ---------------------------------------------------------------------------
# Project-scoped inbox: capture into a project, view + promote from its scope
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_project_scoped_capture_view_and_promote(cookie_client, session, profile):
    project = create_project(session, name="Scoped Proj", source_path="/tmp/scoped")
    session.commit()

    # Capture one scoped item and one loose item.
    await cookie_client.post(
        "/inbox/capture",
        data={"title": "SCOPEDITEM", "prompt": "", "project_id": project.id},
    )
    await cookie_client.post("/inbox/capture", data={"title": "LOOSEITEM", "prompt": ""})
    session.expire_all()

    # The project-scoped view shows only the scoped item.
    scoped = await cookie_client.get(f"/inbox?project={project.slug}")
    assert scoped.status_code == 200
    assert "SCOPEDITEM" in scoped.text and "LOOSEITEM" not in scoped.text

    # The "no project" view shows only the loose item.
    loose = await cookie_client.get("/inbox?project=none")
    assert "LOOSEITEM" in loose.text and "SCOPEDITEM" not in loose.text

    # The scoped item inherits the project's workspace, so it is already
    # complete and can be promoted straight from its scoped view.
    scoped_id = next(t.id for t in list_inbox(session, project_id=project.id))
    r = await cookie_client.post(
        f"/inbox/tickets/{scoped_id}/promote",
        data={"target": "draft", "project": project.slug},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    assert get_ticket(session, scoped_id).status == "draft"
    # It left the project-scoped inbox; the loose item is untouched.
    assert scoped_id not in {t.id for t in list_inbox(session, project_id=project.id)}
    assert "LOOSEITEM" in (await cookie_client.get("/inbox?project=none")).text
