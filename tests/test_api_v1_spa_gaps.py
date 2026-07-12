"""Tests for the JSON API surface added for the SPA rebuild (ui/ground-up).

Covers:
  * /api/v1 admin auth now accepts the cookie-or-bearer dependency (session
    cookie works, not just Authorization: Bearer).
  * Inbox JSON: list + blockers, promote (with completeness gate), decline,
    count.
  * Saved views: create/patch/delete/reorder.
  * Analytics JSON: summary/spend/tokens/latency.
  * Conversation/run actions: resume, retry, restart, clone,
    next-run-context (set + merge), additional-dirs add/remove.
  * Bulk: labels, archive, unarchive.
  * Profiles: copy, export, import, import-from-cc.
  * Helpers: worktree-name preview, cron preview, webhook test, project
    activity feed, diagnostics.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.db.models import Run, RunLatency
from nightdesk.domain.labels import create_label
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import create_ticket, transition_status


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine, bearer_token="t", static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts", worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": "Bearer t"},
    ) as ac:
        yield ac


@pytest.fixture
async def cookie_client(app):
    """Cookie-only client (no Authorization header) — proves the /api/v1
    admin dep now accepts the session cookie the way HTML routes always
    have."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


def _make_profile(session, **overrides):
    fields = dict(
        name="p", fs_read=[], fs_write=["/tmp"], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
        claude_credentials=None,
    )
    fields.update(overrides)
    fields.pop("claude_credentials", None)
    return create_profile(session, **fields)


def _review_ticket(session, *, profile_id):
    t = create_ticket(
        session, title="do thing", prompt="fix it", priority=0,
        profile_id=profile_id, status="queued", run_now=False, source_path="/tmp",
    )
    transition_status(session, t.id, "running")
    run = start_run(session, ticket_id=t.id, worktree_path="/tmp/w",
                     transcript_path="/tmp/p.log", pid=None, host="h")
    finish_run(session, run.id, exit_status="success", error_summary=None,
               session_id="sess-1")
    transition_status(session, t.id, "review")
    return t


# --- Auth: cookie-or-bearer on /api/v1 --------------------------------------


class TestApiV1CookieAuth:
    async def test_cookie_accepted_on_tickets_list(self, cookie_client):
        r = await cookie_client.get("/api/v1/tickets")
        assert r.status_code == 200

    async def test_no_auth_rejected(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/api/v1/tickets")
        assert r.status_code == 401

    async def test_cookie_accepted_on_profiles(self, cookie_client):
        r = await cookie_client.get("/api/v1/profiles")
        assert r.status_code == 200

    async def test_cookie_accepted_on_inbox(self, cookie_client):
        r = await cookie_client.get("/api/v1/inbox")
        assert r.status_code == 200


# --- Inbox -------------------------------------------------------------------


class TestInboxApi:
    async def test_list_reports_blockers_for_incomplete_item(self, client, session):
        t = create_ticket(session, title="idea", prompt="", status="inbox",
                           profile_id=None)
        r = await client.get("/api/v1/inbox")
        assert r.status_code == 200
        items = r.json()
        assert any(i["ticket"]["id"] == t.id for i in items)
        row = next(i for i in items if i["ticket"]["id"] == t.id)
        assert "a profile is required" in row["blockers"]

    async def test_count(self, client, session):
        create_ticket(session, title="a", prompt="", status="inbox", profile_id=None)
        create_ticket(session, title="b", prompt="", status="inbox", profile_id=None)
        r = await client.get("/api/v1/inbox/count")
        assert r.status_code == 200
        assert r.json()["count"] == 2

    async def test_promote_blocked_when_incomplete(self, client, session):
        t = create_ticket(session, title="idea", prompt="", status="inbox",
                           profile_id=None)
        r = await client.post(f"/api/v1/tickets/{t.id}/promote", json={"target": "draft"})
        assert r.status_code == 422

    async def test_promote_succeeds_when_complete(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="idea", prompt="p", status="inbox",
                           profile_id=p.id, source_path="/tmp")
        r = await client.post(f"/api/v1/tickets/{t.id}/promote", json={"target": "queued"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "queued"

    async def test_decline(self, client, session):
        t = create_ticket(session, title="junk", prompt="", status="inbox",
                           profile_id=None)
        r = await client.post(f"/api/v1/tickets/{t.id}/decline")
        assert r.status_code == 200
        assert r.json()["status"] == "archived"


class TestInboxCreateProfileOptional:
    """profile_id is required for every create EXCEPT status="inbox" — the
    JSON schema no longer hard-requires it, and the domain layer enforces
    the same exception the completeness gate (ticket_completeness) already
    checks at promotion time."""

    async def test_inbox_create_without_profile_id_succeeds(self, client):
        r = await client.post("/api/v1/tickets", json={
            "title": "capture me", "prompt": "", "status": "inbox",
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "inbox"
        assert body["profile_id"] is None

    async def test_draft_create_without_profile_id_rejected(self, client):
        r = await client.post("/api/v1/tickets", json={
            "title": "no profile", "prompt": "p", "source_path": "/tmp",
        })
        assert r.status_code == 422
        assert "profile_id" in r.text

    async def test_queued_create_without_profile_id_rejected(self, client):
        r = await client.post("/api/v1/tickets", json={
            "title": "no profile", "prompt": "p", "source_path": "/tmp",
            "status": "queued",
        })
        assert r.status_code == 422

    async def test_promote_still_blocked_until_profile_set(self, client, session):
        r = await client.post("/api/v1/tickets", json={
            "title": "capture me", "prompt": "", "status": "inbox",
        })
        tid = r.json()["id"]

        r = await client.post(f"/api/v1/tickets/{tid}/promote", json={"target": "draft"})
        assert r.status_code == 422
        assert "profile is required" in r.text

        p = _make_profile(session)
        r = await client.patch(f"/api/v1/tickets/{tid}", json={
            "profile_id": p.id, "source_path": "/tmp",
        })
        assert r.status_code == 200, r.text

        r = await client.post(f"/api/v1/tickets/{tid}/promote", json={"target": "draft"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "draft"


class TestSendToInbox:
    """POST /api/v1/tickets/{tid}/send-to-inbox: draft -> inbox only. The
    generic /transition endpoint refuses "inbox" as a target, so this is
    the only JSON path back into the inbox for an existing ticket."""

    async def test_draft_ticket_can_be_sent_to_inbox(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="oops too soon", prompt="p",
                           profile_id=p.id, source_path="/tmp")
        assert t.status == "draft"

        r = await client.post(f"/api/v1/tickets/{t.id}/send-to-inbox")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "inbox"

        # Shows up in the inbox listing afterwards, with blockers computed
        # (it has a profile and workspace already, so none here — but the
        # endpoint must still be the one that surfaces it).
        r = await client.get("/api/v1/inbox")
        assert r.status_code == 200
        assert any(i["ticket"]["id"] == t.id for i in r.json())

    async def test_inbox_listing_recomputes_blockers_for_the_returned_ticket(
        self, client, session,
    ):
        """The inbox listing's ``blockers`` are computed live from the
        ticket's current fields, not cached from whenever it first entered
        the inbox — a well-specified draft that's sent back has none."""
        p = _make_profile(session)
        t = create_ticket(session, title="needs rework", prompt="p",
                           profile_id=p.id, source_path="/tmp")
        r = await client.post(f"/api/v1/tickets/{t.id}/send-to-inbox")
        assert r.status_code == 200
        r = await client.get("/api/v1/inbox")
        row = next(i for i in r.json() if i["ticket"]["id"] == t.id)
        assert row["blockers"] == []

    async def test_queued_ticket_rejected(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="t", prompt="p", profile_id=p.id,
                           source_path="/tmp")
        transition_status(session, t.id, "queued")
        r = await client.post(f"/api/v1/tickets/{t.id}/send-to-inbox")
        assert r.status_code == 409

    async def test_running_ticket_rejected(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="t", prompt="p", profile_id=p.id,
                           source_path="/tmp")
        transition_status(session, t.id, "queued")
        transition_status(session, t.id, "running")
        r = await client.post(f"/api/v1/tickets/{t.id}/send-to-inbox")
        assert r.status_code == 409

    async def test_review_ticket_rejected(self, client, session):
        p = _make_profile(session)
        t = _review_ticket(session, profile_id=p.id)
        r = await client.post(f"/api/v1/tickets/{t.id}/send-to-inbox")
        assert r.status_code == 409

    async def test_archived_ticket_rejected(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="t", prompt="p", profile_id=p.id,
                           source_path="/tmp")
        transition_status(session, t.id, "queued")
        transition_status(session, t.id, "running")
        transition_status(session, t.id, "review")
        transition_status(session, t.id, "archived")
        r = await client.post(f"/api/v1/tickets/{t.id}/send-to-inbox")
        assert r.status_code == 409

    async def test_unknown_ticket_404s(self, client):
        r = await client.post("/api/v1/tickets/does-not-exist/send-to-inbox")
        assert r.status_code == 404

    async def test_generic_transition_endpoint_still_refuses_inbox_target(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="t", prompt="p", profile_id=p.id,
                           source_path="/tmp")
        r = await client.post(f"/api/v1/tickets/{t.id}/transition",
                              json={"status": "inbox"})
        assert r.status_code == 422


# --- Saved views ---------------------------------------------------------------


class TestSavedViewsApi:
    async def test_create_patch_delete(self, client):
        r = await client.post("/api/v1/views", json={
            "name": "My view", "surface": "board", "params": {"q": "status:draft"},
        })
        assert r.status_code == 201, r.text
        vid = r.json()["id"]

        r = await client.get("/api/v1/views")
        assert any(v["id"] == vid for v in r.json())

        r = await client.patch(f"/api/v1/views/{vid}", json={"name": "Renamed"})
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"

        r = await client.delete(f"/api/v1/views/{vid}")
        assert r.status_code == 204

        r = await client.get("/api/v1/views")
        assert not any(v["id"] == vid for v in r.json())

    async def test_reorder(self, client):
        ids = []
        for name in ("a", "b", "c"):
            r = await client.post("/api/v1/views", json={
                "name": name, "surface": "list", "params": {},
            })
            ids.append(r.json()["id"])
        r = await client.post("/api/v1/views/reorder", json={
            "view_ids": list(reversed(ids)),
        })
        assert r.status_code == 200
        assert [v["id"] for v in r.json()] == list(reversed(ids))


# --- Analytics -----------------------------------------------------------------


class TestAnalyticsApi:
    async def test_summary(self, client, session):
        p = _make_profile(session)
        _review_ticket(session, profile_id=p.id)
        r = await client.get("/api/v1/analytics/summary")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "today" in body and "last_30d" in body

    async def test_spend_range(self, client, session):
        p = _make_profile(session)
        _review_ticket(session, profile_id=p.id)
        r = await client.get("/api/v1/analytics/spend", params={"range": "7d"})
        assert r.status_code == 200
        assert r.json()["range"] == "7d"

    async def test_tokens(self, client, session):
        r = await client.get("/api/v1/analytics/tokens")
        assert r.status_code == 200
        assert "daily_series" in r.json()

    async def test_latency(self, client, session):
        r = await client.get("/api/v1/analytics/latency")
        assert r.status_code == 200
        assert "latency_by_model" in r.json()

    async def test_summary_includes_by_project_rollup(self, client, session):
        proj = create_project(session, name="proj-a", slug="proj-a", source_path="/tmp")
        p = _make_profile(session)
        t = _review_ticket(session, profile_id=p.id)
        t.project_id = proj.id
        session.commit()

        r = await client.get("/api/v1/analytics/summary")
        assert r.status_code == 200, r.text
        by_project = r.json()["by_project"]
        assert any(row["project_id"] == proj.id for row in by_project)
        row = next(row for row in by_project if row["project_id"] == proj.id)
        assert row["project_name"] == "proj-a"
        assert row["run_count"] == 1

    async def test_project_id_filter_scopes_all_endpoints(self, client, session):
        proj_a = create_project(session, name="proj-a", slug="proj-a", source_path="/tmp")
        proj_b = create_project(session, name="proj-b", slug="proj-b", source_path="/tmp")
        p = _make_profile(session)
        ta = _review_ticket(session, profile_id=p.id)
        ta.project_id = proj_a.id
        tb = _review_ticket(session, profile_id=p.id)
        tb.project_id = proj_b.id
        session.commit()

        r = await client.get("/api/v1/analytics/summary", params={"project_id": proj_a.id})
        assert r.status_code == 200, r.text
        assert r.json()["last_30d"]["run_count"] == 1

        r = await client.get("/api/v1/analytics/spend", params={"project_id": proj_a.id})
        assert r.status_code == 200
        assert r.json()["project_id"] == proj_a.id

        r = await client.get("/api/v1/analytics/tokens", params={"project_id": proj_a.id})
        assert r.status_code == 200

        r = await client.get("/api/v1/analytics/latency", params={"project_id": proj_a.id})
        assert r.status_code == 200

    async def test_project_id_filter_404_for_unknown_project(self, client):
        r = await client.get("/api/v1/analytics/summary", params={"project_id": "nope"})
        assert r.status_code == 404

    async def test_tokens_daily_series_has_cache_and_run_breakdown(self, client, session):
        p = _make_profile(session)
        _review_ticket(session, profile_id=p.id)
        r = await client.get("/api/v1/analytics/tokens")
        assert r.status_code == 200
        series = r.json()["daily_series"]
        assert series
        today = series[-1]
        for key in (
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "run_count", "cost", "by_model",
        ):
            assert key in today

    async def test_spend_includes_daily_series(self, client, session):
        p = _make_profile(session)
        _review_ticket(session, profile_id=p.id)
        r = await client.get("/api/v1/analytics/spend")
        assert r.status_code == 200
        body = r.json()
        assert "daily_series" in body
        assert "by_project" in body

    async def _seed_today_and_old_run(self, session, p):
        """One project with a run today, one with a run backdated 2 days.

        Mirrors the bug report: a run 2 days old is outside "today" but
        inside "7d"/"30d".
        """
        proj_today = create_project(
            session, name="today-proj", slug="today-proj", source_path="/tmp")
        proj_old = create_project(
            session, name="old-proj", slug="old-proj", source_path="/tmp")

        t_today = _review_ticket(session, profile_id=p.id)
        t_today.project_id = proj_today.id
        t_old = _review_ticket(session, profile_id=p.id)
        t_old.project_id = proj_old.id
        session.commit()

        # Stamp a (dummy) pricing snapshot on each run so its stored `cost_usd`
        # is used as-is instead of being repriced from current token counts
        # (which are 0 here, since `_review_ticket` doesn't record usage).
        run_today = session.query(Run).filter(Run.ticket_id == t_today.id).one()
        run_today.cost_usd = 5.0
        run_today.pricing_snapshot = {"m": {"input": 0.0, "output": 0.0,
                                             "cache_read": 0.0, "cache_write": 0.0}}
        run_old = session.query(Run).filter(Run.ticket_id == t_old.id).one()
        run_old.started_at = run_old.started_at - timedelta(days=2)
        run_old.cost_usd = 30.0
        run_old.pricing_snapshot = {"m": {"input": 0.0, "output": 0.0,
                                           "cache_read": 0.0, "cache_write": 0.0}}
        session.commit()
        return proj_today, proj_old

    async def test_spend_breakdowns_respect_range_today(self, client, session):
        # Regression: by_project/by_model/by_profile/by_ticket used a fixed
        # 30-day window regardless of `range`, so switching the picker from
        # 7d -> today never changed the breakdowns even though `totals` did.
        p = _make_profile(session)
        proj_today, proj_old = await self._seed_today_and_old_run(session, p)

        r = await client.get("/api/v1/analytics/spend", params={"range": "today"})
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["cost"] == pytest.approx(5.0)

        project_names = {row["project_name"] for row in body["by_project"]}
        assert project_names == {"today-proj"}
        assert sum(row["cost"] for row in body["by_project"]) == pytest.approx(5.0)
        assert sum(row["cost"] for row in body["by_model"]) == pytest.approx(5.0)
        assert sum(row["cost"] for row in body["by_profile"]) == pytest.approx(5.0)
        assert sum(row["cost"] for row in body["by_ticket"]) == pytest.approx(5.0)

    async def test_spend_breakdowns_respect_range_7d(self, client, session):
        p = _make_profile(session)
        await self._seed_today_and_old_run(session, p)

        r = await client.get("/api/v1/analytics/spend", params={"range": "7d"})
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["cost"] == pytest.approx(35.0)

        project_names = {row["project_name"] for row in body["by_project"]}
        assert project_names == {"today-proj", "old-proj"}
        assert sum(row["cost"] for row in body["by_project"]) == pytest.approx(35.0)

    async def test_tokens_by_model_respects_range(self, client, session):
        p = _make_profile(session)
        await self._seed_today_and_old_run(session, p)

        r_today = await client.get("/api/v1/analytics/tokens", params={"range": "today"})
        r_7d = await client.get("/api/v1/analytics/tokens", params={"range": "7d"})
        cost_today = sum(row["cost"] for row in r_today.json()["by_model"])
        cost_7d = sum(row["cost"] for row in r_7d.json()["by_model"])
        assert cost_today == pytest.approx(5.0)
        assert cost_7d == pytest.approx(35.0)

    async def test_latency_breakdowns_respect_range(self, client, session):
        p = _make_profile(session)
        t_today = _review_ticket(session, profile_id=p.id)
        t_old = _review_ticket(session, profile_id=p.id)

        run_today = session.query(Run).filter(Run.ticket_id == t_today.id).one()
        run_today.model_used = "claude-opus-4-7"
        run_old = session.query(Run).filter(Run.ticket_id == t_old.id).one()
        run_old.model_used = "claude-sonnet-4-6"
        run_old.started_at = run_old.started_at - timedelta(days=2)
        session.commit()

        session.add(RunLatency(
            run_id=run_today.id, model="claude-opus-4-7",
            total_model_seconds=4.0, total_tool_seconds=0.0,
            turn_count=1, turn_latencies=[4.0],
        ))
        session.add(RunLatency(
            run_id=run_old.id, model="claude-sonnet-4-6",
            total_model_seconds=8.0, total_tool_seconds=0.0,
            turn_count=1, turn_latencies=[8.0],
        ))
        session.commit()

        r_today = await client.get("/api/v1/analytics/latency", params={"range": "today"})
        models_today = {row["model"] for row in r_today.json()["latency_by_model"]}
        assert models_today == {"claude-opus-4-7"}

        r_7d = await client.get("/api/v1/analytics/latency", params={"range": "7d"})
        models_7d = {row["model"] for row in r_7d.json()["latency_by_model"]}
        assert models_7d == {"claude-opus-4-7", "claude-sonnet-4-6"}

    async def test_spend_project_id_filter_respects_range(self, client, session):
        p = _make_profile(session)
        proj_today, proj_old = await self._seed_today_and_old_run(session, p)

        # proj_old has no activity "today" -> filtering to it for range=today
        # yields an empty (or zero-cost) breakdown, not its 30-day total.
        r = await client.get(
            "/api/v1/analytics/spend",
            params={"range": "today", "project_id": proj_old.id},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["cost"] == pytest.approx(0.0)
        assert all(row["cost"] == pytest.approx(0.0) for row in body["by_project"])

        r = await client.get(
            "/api/v1/analytics/spend",
            params={"range": "7d", "project_id": proj_old.id},
        )
        body_7d = r.json()
        assert body_7d["totals"]["cost"] == pytest.approx(30.0)
        assert sum(row["cost"] for row in body_7d["by_project"]) == pytest.approx(30.0)


# --- Conversation / run actions -------------------------------------------------


class TestConversationActionsApi:
    async def test_resume(self, client, session):
        p = _make_profile(session)
        t = _review_ticket(session, profile_id=p.id)
        r = await client.post(f"/api/v1/tickets/{t.id}/resume", json={"message": "go"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "queued"

    async def test_retry(self, client, session):
        p = _make_profile(session)
        t = _review_ticket(session, profile_id=p.id)
        r = await client.post(f"/api/v1/tickets/{t.id}/retry", json={})
        assert r.status_code == 200, r.text

    async def test_restart_requires_workspace_policy(self, client, session):
        p = _make_profile(session)
        t = _review_ticket(session, profile_id=p.id)
        r = await client.post(f"/api/v1/tickets/{t.id}/restart", json={})
        assert r.status_code == 422

    async def test_restart(self, client, session):
        p = _make_profile(session)
        t = _review_ticket(session, profile_id=p.id)
        r = await client.post(f"/api/v1/tickets/{t.id}/restart", json={
            "workspace_policy": "fresh_path",
        })
        assert r.status_code == 200, r.text

    async def test_clone(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="orig", prompt="p", profile_id=p.id,
                           source_path="/tmp")
        r = await client.post(f"/api/v1/tickets/{t.id}/clone", json={"title": "copy"})
        assert r.status_code == 201, r.text
        assert r.json()["title"] == "copy"
        assert r.json()["id"] != t.id

    async def test_next_run_context_set_and_merge(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="orig", prompt="p", profile_id=p.id,
                           source_path="/tmp")
        r = await client.post(f"/api/v1/tickets/{t.id}/next-run-context",
                              json={"body": "steer this"})
        assert r.status_code == 200
        assert r.json()["next_run_context"] == "steer this"

        r = await client.post(f"/api/v1/tickets/{t.id}/merge-next-run-context")
        assert r.status_code == 200
        body = r.json()
        assert body["next_run_context"] is None
        assert "steer this" in body["prompt"]

    async def test_additional_dirs_add_and_remove(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="orig", prompt="p", profile_id=p.id,
                           source_path="/tmp")
        r = await client.post(f"/api/v1/tickets/{t.id}/additional-dirs",
                              json={"path": "/opt/extra", "mode": "ro"})
        assert r.status_code == 200, r.text
        assert {"path": "/opt/extra", "mode": "ro"} in r.json()["additional_dirs"]

        r = await client.request(
            "DELETE", f"/api/v1/tickets/{t.id}/additional-dirs",
            params={"path": "/opt/extra"},
        )
        assert r.status_code == 200
        assert r.json()["additional_dirs"] == []

    async def test_additional_dirs_rejects_relative_path(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="orig", prompt="p", profile_id=p.id,
                           source_path="/tmp")
        r = await client.post(f"/api/v1/tickets/{t.id}/additional-dirs",
                              json={"path": "relative/path"})
        assert r.status_code == 422


# --- Bulk ------------------------------------------------------------------------


class TestBulkApi:
    async def test_bulk_labels(self, client, session):
        p = _make_profile(session)
        t1 = create_ticket(session, title="a", prompt="p", profile_id=p.id, source_path="/tmp")
        t2 = create_ticket(session, title="b", prompt="p", profile_id=p.id, source_path="/tmp")
        lbl = create_label(session, name="urgent", color="#ff0000")
        r = await client.patch("/api/v1/tickets/bulk/labels", json={
            "ticket_ids": [t1.id, t2.id], "label_ids": [lbl.id],
        })
        assert r.status_code == 200, r.text
        assert len(r.json()["updated"]) == 2
        for t in r.json()["updated"]:
            assert any(l["id"] == lbl.id for l in t["labels"])

    async def test_bulk_archive_and_unarchive(self, client, session):
        p = _make_profile(session)
        t = create_ticket(session, title="a", prompt="p", profile_id=p.id, source_path="/tmp")
        r = await client.post("/api/v1/tickets/bulk/archive", json={"ticket_ids": [t.id]})
        assert r.status_code == 200
        assert r.json()["updated"][0]["status"] == "archived"

        r = await client.post("/api/v1/tickets/bulk/unarchive", json={"ticket_ids": [t.id]})
        assert r.status_code == 200
        assert r.json()["updated"][0]["status"] == "queued"


# --- Profiles: copy/export/import/import-from-cc --------------------------------


class TestProfilesImportExportApi:
    async def test_copy(self, client, session):
        r = await client.post("/api/v1/profiles", json={
            "name": "source", "fs_read": [], "fs_write": [], "allowed_tools": [],
            "denied_tools": [], "network_mode": "off", "network_allowlist": [],
            "secret_keys": [], "default_model": None,
            "claude_credentials": {"source": "inherit"},
        })
        pid = r.json()["id"]
        r = await client.post(f"/api/v1/profiles/{pid}/copy")
        assert r.status_code == 201, r.text
        assert r.json()["name"] == "source (copy)"
        assert r.json()["id"] != pid

    async def test_export_redacts_secrets(self, client, session):
        r = await client.post("/api/v1/profiles", json={
            "name": "withsecret", "fs_read": [], "fs_write": [], "allowed_tools": [],
            "denied_tools": [], "network_mode": "off", "network_allowlist": [],
            "secret_keys": [], "default_model": None,
            "claude_credentials": {"source": "api_key", "value": "sk-super-secret"},
        })
        pid = r.json()["id"]
        r = await client.get(f"/api/v1/profiles/{pid}/export")
        assert r.status_code == 200
        body = r.json()
        assert "id" not in body
        assert body["claude_credentials"]["value"] is None
        assert "sk-super-secret" not in r.text

    async def test_import_round_trip(self, client, session):
        payload = {
            "name": "imported", "fs_read": [], "fs_write": ["/tmp"],
            "allowed_tools": [], "denied_tools": [], "network_mode": "off",
            "network_allowlist": [], "secret_keys": [], "default_model": None,
            "backend": "claude_sdk", "claude_credentials": {"source": "inherit"},
        }
        r = await client.post("/api/v1/profiles/import", json={"payload": payload})
        assert r.status_code == 201, r.text
        assert r.json()["dropped_fields"] == []

        r = await client.get(f"/api/v1/profiles/{r.json()['id']}")
        assert r.json()["name"] == "imported"

    async def test_import_drops_forbidden_fields(self, client, session):
        payload = {
            "name": "sneaky", "fs_read": [], "fs_write": [], "allowed_tools": [],
            "denied_tools": [], "network_mode": "off", "network_allowlist": [],
            "secret_keys": [], "default_model": None,
            "claude_credentials": {"source": "inherit"},
            "hooks": {"PreToolUse": []},
        }
        r = await client.post("/api/v1/profiles/import", json={"payload": payload})
        assert r.status_code == 201, r.text
        assert "hooks" in r.json()["dropped_fields"]

    async def test_import_from_cc(self, client, session):
        settings = {
            "model": "claude-sonnet-4-5",
            "permissions": {"allow": ["Read", "Write"], "defaultMode": "acceptEdits"},
        }
        r = await client.post("/api/v1/profiles/import-from-cc", json={
            "settings": settings, "name": "cc-import",
        })
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        r = await client.get(f"/api/v1/profiles/{pid}")
        assert r.json()["name"] == "cc-import"


# --- Helpers ----------------------------------------------------------------------


class TestHelpersApi:
    async def test_worktree_name_preview(self, client, tmp_path):
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        r = await client.post("/api/v1/preview/worktree-name", json={
            "source_path": str(repo), "name": "my-ticket",
        })
        assert r.status_code == 200, r.text
        assert "my-ticket" in r.json()["path"]

    async def test_worktree_name_preview_requires_source(self, client):
        r = await client.post("/api/v1/preview/worktree-name", json={"source_path": ""})
        assert r.status_code == 422

    async def test_cron_preview(self, client):
        r = await client.post("/api/v1/preview/cron", json={
            "schedule": "0 9 * * *", "timezone": "UTC", "count": 3,
        })
        assert r.status_code == 200, r.text
        assert len(r.json()["next_fire_times"]) == 3

    async def test_cron_preview_rejects_invalid_expression(self, client):
        r = await client.post("/api/v1/preview/cron", json={"schedule": "not a cron"})
        assert r.status_code == 422

    @patch("nightdesk.api.routes.helpers.fire_webhook")
    async def test_notifications_test_fires_payload(self, mock_fire, client):
        r = await client.post("/api/v1/notifications/test", json={
            "url": "https://ntfy.sh/test-topic",
        })
        assert r.status_code == 204
        mock_fire.assert_called_once()

    async def test_notifications_test_rejects_bad_url(self, client):
        r = await client.post("/api/v1/notifications/test", json={"url": "not-a-url"})
        assert r.status_code == 422

    async def test_project_activity(self, client, session):
        proj = create_project(session, name="proj", slug="proj", source_path="/tmp")
        p = _make_profile(session)
        t = create_ticket(session, title="a", prompt="p", profile_id=p.id,
                           project_id=proj.id, source_path="/tmp")
        transition_status(session, t.id, "queued")
        transition_status(session, t.id, "running")
        run = start_run(session, ticket_id=t.id, worktree_path="/tmp/w",
                         transcript_path="/tmp/p.log", pid=None, host="h")
        finish_run(session, run.id, exit_status="success", error_summary=None)

        r = await client.get(f"/api/v1/projects/{proj.id}/activity")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert any(row["ticket_id"] == t.id for row in rows)

    async def test_project_activity_404_for_unknown_project(self, client):
        r = await client.get("/api/v1/projects/does-not-exist/activity")
        assert r.status_code == 404

    async def test_diagnostics(self, client):
        r = await client.get("/api/v1/diagnostics")
        assert r.status_code == 200
        body = r.json()
        assert "python_version" in body
        assert "nightdesk_version" in body
