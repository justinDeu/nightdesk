"""Cookie-auth tests for the rewritten /settings page.

Covers:
    * GET renders Work hours section with HTML5 time pickers.
    * POST with ``always_on=on`` writes 00:00/00:00.
    * POST with explicit times persists them to ConfigRow.
    * Form no longer surfaces max_run_duration_seconds.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.db.models import ConfigRow


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
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


async def test_settings_get_renders_work_hours_with_time_inputs(cookie_client, session):
    cfg = ConfigRow(
        id=1,
        worktree_root="/tmp/w",
        transcript_root="/tmp/t",
        window_start="22:00",
        window_end="07:00",
    )
    session.add(cfg)
    session.commit()

    r = await cookie_client.get("/settings")
    assert r.status_code == 200
    body = r.text
    assert "Work hours" in body
    assert 'type="time"' in body


async def test_settings_binary_path_shows_resolved_default(cookie_client, session, monkeypatch):
    """The Claude binary path field shows the actual resolved PATH binary as its
    placeholder/hint, not the old generic '(use PATH lookup)' text."""
    import nightdesk.api.routes.ui as ui_module  # noqa: F401
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/cc/bin/claude" if name == "claude" else None)

    r = await cookie_client.get("/settings")
    assert r.status_code == 200
    body = r.text
    assert "(use PATH lookup)" not in body
    assert "/opt/cc/bin/claude" in body
    assert 'name="window_start"' in body
    assert 'name="window_end"' in body
    assert 'name="always_on"' in body
    # Drop confirmed: no max_run_duration_seconds form field.
    assert 'name="max_run_duration_seconds"' not in body
    assert 'name="run_token_grace_seconds"' not in body
    # Storage paths section is gone.
    assert "Storage paths" not in body


async def test_settings_post_always_on_writes_zero_window(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.post(
        "/settings",
        data={
            "max_parallel": "2",
            "polling_interval_seconds": "5",
            "always_on": "on",
            "window_start": "22:00",
            "window_end": "07:00",
            "claude_binary_path": "",
            "cc_minimum_version": "2.1.80",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.window_start == "00:00"
    assert cfg.window_end == "00:00"


async def test_settings_post_explicit_times_persist(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.post(
        "/settings",
        data={
            "max_parallel": "3",
            "polling_interval_seconds": "10",
            "window_start": "21:30",
            "window_end": "06:15",
            "claude_binary_path": "",
            "cc_minimum_version": "2.1.80",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.window_start == "21:30"
    assert cfg.window_end == "06:15"
    assert cfg.max_parallel == 3
    assert cfg.polling_interval_seconds == 10


async def test_settings_post_does_not_touch_max_run_duration(cookie_client, session):
    """The dropped field stays at its DB default after a save round-trip."""
    session.add(ConfigRow(
        id=1,
        worktree_root="/tmp/w",
        transcript_root="/tmp/t",
        max_run_duration_seconds=86400,
        run_token_grace_seconds=300,
    ))
    session.commit()

    r = await cookie_client.post(
        "/settings",
        data={
            "max_parallel": "2",
            "polling_interval_seconds": "5",
            "window_start": "22:00",
            "window_end": "07:00",
            "claude_binary_path": "",
            "cc_minimum_version": "2.1.80",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.max_run_duration_seconds == 86400
    assert cfg.run_token_grace_seconds == 300
