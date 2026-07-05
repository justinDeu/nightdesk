"""Tests for run-completion notifications.

Covers:
    * Webhook fires when a run completes with a configured notify_webhook_url.
    * Webhook does NOT fire when the URL is empty/NULL.
    * fire_webhook logs but does not raise on network failure.
    * build_run_completion_payload computes duration correctly.
    * PATCH /api/v1/config accepts notify_webhook_url.

The settings-page and test-webhook-button HTML routes were removed with the
HTMX rip-out; their JSON equivalents (PATCH /api/v1/config,
POST /api/v1/notifications/test) are covered here and in
tests/test_api_v1_spa_gaps.py respectively.
"""
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.db.models import ConfigRow, Run, Ticket, Profile
from nightdesk.domain.notifications import (
    RunCompletionPayload,
    build_run_completion_payload,
    build_test_payload,
    fire_webhook,
)


# -- fixtures -------------------------------------------------------------

@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine,
        bearer_token="t",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def bearer_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer t"},
    ) as ac:
        yield ac


def _seed(session, **cfg_overrides):
    """Insert a ConfigRow and a minimal ticket with a completed run."""
    p = Profile(
        name="default",
        fs_read=["/tmp"], fs_write=["/tmp"],
        allowed_tools=["Read"], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(p)
    session.flush()

    t = Ticket(
        title="Test ticket",
        prompt="do something",
        status="running",
        profile_id=p.id,
    )
    session.add(t)
    session.flush()

    started = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    finished = datetime(2025, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
    r = Run(
        ticket_id=t.id,
        started_at=started,
        finished_at=finished,
        exit_status="success",
        cost_usd=0.42,
        worktree_path="/tmp/w",
        transcript_path="/tmp/t.log",
        pid=1234,
        host="test",
    )
    session.add(r)
    session.flush()
    t.current_run_id = r.id

    defaults = dict(
        id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
    )
    defaults.update(cfg_overrides)
    cfg = ConfigRow(**defaults)
    session.add(cfg)
    session.commit()

    return p, t, r


# -- payload tests --------------------------------------------------------

class TestBuildPayload:
    def test_duration_computed(self):
        p = build_run_completion_payload(
            ticket_id="t1", title="T", run_id="r1",
            exit_status="success", error_summary=None, cost_usd=1.0,
            started_at=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            finished_at=datetime(2025, 1, 1, 12, 5, tzinfo=timezone.utc),
            base_url="http://localhost:8765",
        )
        assert p.duration_seconds == 300.0
        assert p.url == "http://localhost:8765/tickets/t1"
        d = p.to_dict()
        assert d["cost_usd"] == 1.0
        assert d["exit_status"] == "success"

    def test_no_duration_without_times(self):
        p = build_run_completion_payload(
            ticket_id="t1", title="T", run_id="r1",
            exit_status="failed", error_summary="boom", cost_usd=None,
            started_at=None, finished_at=None,
            base_url="http://localhost:8765",
        )
        assert p.duration_seconds is None
        assert p.error_summary == "boom"

    def test_test_payload(self):
        p = build_test_payload("http://localhost:8765")
        assert p.exit_status == "success"
        assert p.duration_seconds == 42.0
        assert "test" in p.ticket_id


# -- fire_webhook tests ---------------------------------------------------

class TestFireWebhook:
    def test_no_url_noop(self):
        # Should not raise or make any network call.
        fire_webhook("", RunCompletionPayload(
            ticket_id="t", title="T", run_id="r",
            exit_status="success", error_summary=None,
            cost_usd=0, duration_seconds=1, url="http://x",
        ))

    def test_none_url_noop(self):
        fire_webhook(None, RunCompletionPayload(
            ticket_id="t", title="T", run_id="r",
            exit_status="success", error_summary=None,
            cost_usd=0, duration_seconds=1, url="http://x",
        ))

    @patch("nightdesk.domain.notifications.urllib.request.urlopen")
    def test_successful_post(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        payload = RunCompletionPayload(
            ticket_id="t1", title="T", run_id="r1",
            exit_status="success", error_summary=None,
            cost_usd=0.5, duration_seconds=60, url="http://x/t1",
        )
        fire_webhook("https://hooks.example.com/abc", payload)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body["ticket_id"] == "t1"
        assert body["exit_status"] == "success"
        assert body["cost_usd"] == 0.5

    @patch("nightdesk.domain.notifications.urllib.request.urlopen")
    def test_network_failure_logged_not_raised(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        payload = RunCompletionPayload(
            ticket_id="t", title="T", run_id="r",
            exit_status="success", error_summary=None,
            cost_usd=0, duration_seconds=1, url="http://x",
        )
        # Must not raise.
        fire_webhook("https://hooks.example.com/abc", payload)


# -- PATCH /api/v1/config webhook field tests -----------------------------

class TestConfigApiWebhook:
    async def test_patch_config_sets_webhook_url(self, bearer_client, session):
        session.add(ConfigRow(
            id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
        ))
        session.commit()

        r = await bearer_client.patch(
            "/api/v1/config",
            json={"notify_webhook_url": "https://hooks.slack.com/xxx"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["notify_webhook_url"] == "https://hooks.slack.com/xxx"

    async def test_patch_config_clears_webhook_url(self, bearer_client, session):
        session.add(ConfigRow(
            id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
            notify_webhook_url="https://old.example.com",
        ))
        session.commit()

        r = await bearer_client.patch(
            "/api/v1/config",
            json={"notify_webhook_url": ""},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["notify_webhook_url"] is None

    async def test_get_config_includes_webhook_url(self, bearer_client, session):
        session.add(ConfigRow(
            id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
            notify_webhook_url="https://ntfy.sh/topic",
        ))
        session.commit()

        r = await bearer_client.get("/api/v1/config")
        assert r.status_code == 200
        assert r.json()["notify_webhook_url"] == "https://ntfy.sh/topic"
