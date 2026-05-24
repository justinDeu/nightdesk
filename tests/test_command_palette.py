"""Smoke tests for the command palette + keyboard-shortcut wiring.

The palette is pure client JS (``static/command_palette.js``) plus a small
template partial included by ``base.html``. There's no server route to add,
so these tests assert the markup and script tag are wired into every page
that extends ``base.html`` (``/profiles`` needs no fixtures).
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app


@pytest.fixture
def app(engine, tmp_path):
    return create_app(engine=engine, bearer_token="t",
                       static_root=tmp_path / "static",
                       transcript_root=tmp_path / "transcripts",
                       worktree_root=tmp_path / "work")


@pytest.fixture
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                              cookies={"nightdesk_token": "t"}) as ac:
        yield ac


async def test_base_includes_command_palette_script(cookie_client):
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert "command_palette.js" in r.text


async def test_base_includes_palette_dialog(cookie_client):
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert 'id="nd-command-palette"' in r.text
    assert 'id="nd-cmdk-input"' in r.text
    assert 'id="nd-cmdk-list"' in r.text


async def test_base_includes_cheatsheet_dialog(cookie_client):
    r = await cookie_client.get("/profiles")
    assert r.status_code == 200
    assert 'id="nd-shortcuts-cheatsheet"' in r.text
    # The cheat sheet documents the global shortcuts.
    assert "Go to board" in r.text
    assert "Go to archive" in r.text
    # JS-free fallback note must be present.
    assert "reachable by mouse" in r.text


async def test_palette_present_on_board(cookie_client):
    # The board is the primary surface for the palette; make sure the partial
    # rides along there too (board.html extends base.html).
    r = await cookie_client.get("/")
    assert r.status_code == 200
    assert 'id="nd-command-palette"' in r.text
    assert "command_palette.js" in r.text
