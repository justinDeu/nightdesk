"""Cookie-auth tests for the /settings page (multi-window model).

Covers:
    * GET renders the Work windows editor (not the legacy work-hours inputs).
    * POST persists schedule windows + global timezone, replacing prior windows.
    * Bad timezone falls back to UTC.
    * Unrelated config fields (worktree_base_ref, max_run_duration) still work.
"""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.db.models import ConfigRow, ScheduleWindow


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


async def test_settings_root_redirects_to_scheduling(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.get("/settings", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/settings/scheduling"


async def test_settings_get_renders_windows_editor(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.get("/settings/scheduling")
    assert r.status_code == 200
    body = r.text
    assert 'href="/settings/scheduling"' in body
    assert 'href="/settings/claude"' in body
    assert 'href="/settings/worktrees"' in body
    assert 'href="/settings/notifications"' in body
    # New multi-window UI is present.
    assert "Work windows" in body
    assert 'id="windows-editor"' in body
    assert 'name="windows_json"' in body
    assert 'name="schedule_timezone"' in body
    assert 'data-dirty-ignore="true"' in body
    assert 'id="windows-data"' in body
    assert 'id="resolved-schedule-axis"' in body
    assert 'id="resolved-schedule"' in body
    assert 'id="resolved-schedule-baseline"' in body
    assert 'id="resolved-schedule-tooltip"' in body
    # Legacy single-window form is gone.
    assert 'name="window_start"' not in body
    assert 'name="always_on"' not in body
    assert 'name="tz_offset"' not in body


async def test_settings_get_seeds_windows_data_island(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
                          schedule_timezone="America/New_York"))
    session.add(ScheduleWindow(label="overnight", day_mask=31, start="22:00",
                               end="07:00", max_parallel=3, position=0))
    session.commit()

    r = await cookie_client.get("/settings/scheduling")
    assert r.status_code == 200
    assert "overnight" in r.text


async def test_settings_get_renders_claude_category(cookie_client, session, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/cc/bin/claude" if name == "claude" else None)
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.get("/settings/claude")

    assert r.status_code == 200
    assert "Claude Code" in r.text
    assert 'name="claude_binary_path"' in r.text
    assert "/opt/cc/bin/claude" in r.text


async def test_settings_get_renders_worktrees_category(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
                          worktree_base_ref="develop"))
    session.commit()

    r = await cookie_client.get("/settings/worktrees")

    assert r.status_code == 200
    assert "Git worktrees" in r.text
    assert 'name="worktree_base_ref"' in r.text
    assert "develop" in r.text


async def test_settings_post_persists_windows_and_timezone(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    windows = [{"label": "overnight", "day_mask": 31, "start": "22:00",
                "end": "07:00", "max_parallel": 3, "position": 0}]
    r = await cookie_client.post(
        "/settings/scheduling",
        data={
            "polling_interval_seconds": "5",
            "claude_binary_path": "",
            "cc_minimum_version": "2.1.80",
            "windows_json": json.dumps(windows),
            "schedule_timezone": "America/New_York",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.schedule_timezone == "America/New_York"
    rows = session.query(ScheduleWindow).all()
    assert [w.label for w in rows] == ["overnight"]
    assert rows[0].day_mask == 31
    assert rows[0].max_parallel == 3


async def test_settings_post_replaces_existing_windows(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.add(ScheduleWindow(label="old", day_mask=127, start="00:00",
                               end="00:00", max_parallel=1, position=0))
    session.commit()

    windows = [{"label": "new", "day_mask": 96, "start": "00:00",
                "end": "00:00", "max_parallel": 2, "position": 0}]
    r = await cookie_client.post(
        "/settings/scheduling",
        data={
            "polling_interval_seconds": "5",
            "cc_minimum_version": "2.1.80",
            "windows_json": json.dumps(windows),
            "schedule_timezone": "UTC",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    rows = session.query(ScheduleWindow).all()
    assert [w.label for w in rows] == ["new"]


async def test_settings_post_empty_windows_clears_all(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.add(ScheduleWindow(label="x", day_mask=127, start="00:00",
                               end="00:00", max_parallel=1, position=0))
    session.commit()

    r = await cookie_client.post(
        "/settings/scheduling",
        data={
            "polling_interval_seconds": "5",
            "cc_minimum_version": "2.1.80",
            "windows_json": "[]",
            "schedule_timezone": "UTC",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    assert session.query(ScheduleWindow).all() == []


async def test_settings_post_bad_windows_json_keeps_existing_rows(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.add(ScheduleWindow(label="kept", day_mask=127, start="00:00",
                               end="00:00", max_parallel=1, position=0))
    session.commit()

    r = await cookie_client.post(
        "/settings/scheduling",
        data={
            "polling_interval_seconds": "5",
            "windows_json": "{bad json",
            "schedule_timezone": "UTC",
        },
    )

    assert r.status_code == 422
    session.expire_all()
    rows = session.query(ScheduleWindow).all()
    assert [w.label for w in rows] == ["kept"]


async def test_settings_post_bad_timezone_falls_back_to_utc(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.post(
        "/settings/scheduling",
        data={
            "polling_interval_seconds": "5",
            "cc_minimum_version": "2.1.80",
            "windows_json": "[]",
            "schedule_timezone": "Mars/Phobos",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.schedule_timezone == "UTC"


async def test_settings_binary_path_shows_resolved_default(cookie_client, session, monkeypatch):
    """The Claude binary path field shows the actual resolved PATH binary as its
    placeholder/hint, not the old generic '(use PATH lookup)' text."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/cc/bin/claude" if name == "claude" else None)

    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.get("/settings/claude")
    assert r.status_code == 200
    body = r.text
    assert "(use PATH lookup)" not in body
    assert "/opt/cc/bin/claude" in body
    # Dropped fields stay dropped.
    assert 'name="max_run_duration_seconds"' not in body
    assert 'name="run_token_grace_seconds"' not in body
    assert "Storage paths" not in body


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
        "/settings/scheduling",
        data={
            "polling_interval_seconds": "5",
            "windows_json": "[]",
            "schedule_timezone": "UTC",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.max_run_duration_seconds == 86400
    assert cfg.run_token_grace_seconds == 300


async def test_settings_get_renders_worktree_base_ref_field(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t",
                          worktree_base_ref="develop"))
    session.commit()

    r = await cookie_client.get("/settings/worktrees")
    assert r.status_code == 200
    body = r.text
    assert 'name="worktree_base_ref"' in body
    assert "develop" in body


async def test_settings_post_persists_worktree_base_ref(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.post(
        "/settings/worktrees",
        data={
            "worktree_base_ref": "develop",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.worktree_base_ref == "develop"

    # Blank submission clears it back to NULL.
    r = await cookie_client.post(
        "/settings/worktrees",
        data={
            "worktree_base_ref": "",
        },
    )
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.worktree_base_ref is None


async def test_worktrees_pane_has_no_toolchain_section(cookie_client, session):
    session.add(ConfigRow(
        id=1,
        worktree_root="/tmp/w",
        transcript_root="/tmp/t",
        toolchain_presets={"go-user-tools": ["~/go/bin"]},
    ))
    session.commit()

    r = await cookie_client.get("/settings/worktrees")
    assert r.status_code == 200
    body = r.text
    # The pane itself must not render the presets form; the side-nav link to
    # the new External tools page is allowed.
    assert 'data-preset-card' not in body
    assert 'name="preset_name"' not in body
    assert 'name="preset_paths_json"' not in body


async def test_worktrees_post_does_not_clear_toolchain_presets(cookie_client, session):
    session.add(ConfigRow(
        id=1,
        worktree_root="/tmp/w",
        transcript_root="/tmp/t",
        toolchain_presets={"go-user-tools": ["~/go/bin"]},
    ))
    session.commit()

    r = await cookie_client.post("/settings/worktrees", data={"worktree_base_ref": "develop"})
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.worktree_base_ref == "develop"
    assert cfg.toolchain_presets == {"go-user-tools": ["~/go/bin"]}


async def test_external_tools_pane_renders_preset_cards(cookie_client, session):
    session.add(ConfigRow(
        id=1,
        worktree_root="/tmp/w",
        transcript_root="/tmp/t",
        toolchain_presets={"go-user-tools": ["~/go/bin", "/opt/go/bin"]},
    ))
    session.commit()

    r = await cookie_client.get("/settings/external-tools")
    assert r.status_code == 200
    body = r.text
    assert 'data-preset-card' in body
    assert 'name="preset_name"' in body
    assert 'name="preset_paths_json"' in body
    assert 'go-user-tools' in body
    assert '~/go/bin' in body
    assert '/opt/go/bin' in body


async def test_external_tools_post_persists_presets_from_card_form(cookie_client, session):
    import json

    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.post(
        "/settings/external-tools",
        data={
            "preset_name": ["go-user-tools", "node-user-tools"],
            "preset_paths_json": [
                json.dumps(["~/go/bin", "/opt/go/bin"]),
                json.dumps(["~/.npm-global/bin"]),
            ],
        },
    )
    assert r.status_code == 200, r.text
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.toolchain_presets == {
        "go-user-tools": ["~/go/bin", "/opt/go/bin"],
        "node-user-tools": ["~/.npm-global/bin"],
    }

    # Submitting with no preset cards clears the dict.
    r = await cookie_client.post("/settings/external-tools", data={})
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.toolchain_presets == {}


async def test_external_tools_post_rejects_invalid_preset_paths_json(cookie_client, session):
    session.add(ConfigRow(id=1, worktree_root="/tmp/w", transcript_root="/tmp/t"))
    session.commit()

    r = await cookie_client.post(
        "/settings/external-tools",
        data={
            "preset_name": ["broken"],
            "preset_paths_json": ["{not json"],
        },
    )
    assert r.status_code == 422
    assert "broken" in r.text

    # Non-list payload is also rejected.
    r = await cookie_client.post(
        "/settings/external-tools",
        data={
            "preset_name": ["shape"],
            "preset_paths_json": ['"a string"'],
        },
    )
    assert r.status_code == 422


async def test_external_tools_post_skips_unnamed_preset_cards(cookie_client, session):
    import json

    session.add(ConfigRow(
        id=1,
        worktree_root="/tmp/w",
        transcript_root="/tmp/t",
        toolchain_presets={"stale": ["~/old"]},
    ))
    session.commit()

    r = await cookie_client.post(
        "/settings/external-tools",
        data={
            "preset_name": ["", "kept"],
            "preset_paths_json": [
                json.dumps(["~/orphan/bin"]),
                json.dumps(["~/kept/bin"]),
            ],
        },
    )
    assert r.status_code == 200
    session.expire_all()
    cfg = session.get(ConfigRow, 1)
    assert cfg.toolchain_presets == {"kept": ["~/kept/bin"]}

