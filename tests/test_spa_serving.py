"""Tests for mounting the built SPA (frontend/dist) at the app root ``/``.

Uses a tiny fake dist tree in tmp_path rather than a real Vite/npm build —
these tests only exercise nightdesk.api.spa's routing/caching contract, not
the frontend build itself.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.api import spa as spa_app


def _write_fake_dist(dist_root, *, asset_name="index-abc123.js"):
    assets_dir = dist_root / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / asset_name).write_text("console.log('hi');")
    (dist_root / "index.html").write_text(
        f'<html><head><script src="/assets/{asset_name}"></script>'
        "</head><body><div id='root'></div></body></html>"
    )
    (dist_root / "favicon.ico").write_text("fake-icon")
    return dist_root


@pytest.fixture
def app_with_spa(engine, tmp_path):
    dist_root = _write_fake_dist(tmp_path / "dist")
    app = create_app(
        engine=engine, bearer_token="t", static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts", worktree_root=tmp_path / "work",
        spa_dist_root=dist_root,
    )
    return app, dist_root


@pytest.fixture
def app_without_spa(engine, tmp_path):
    # Points at a directory that has no build output at all.
    return create_app(
        engine=engine, bearer_token="t", static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts", worktree_root=tmp_path / "work",
        spa_dist_root=tmp_path / "no-such-dist",
    )


async def _client(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestSpaMountPresent:
    async def test_root_serves_index(self, app_with_spa):
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "<div id='root'>" in r.text

    async def test_client_side_route_falls_back_to_index(self, app_with_spa):
        """A deep client-side route (e.g. /tickets/abc123) has no matching
        file on disk — it must still resolve to index.html so the client
        router owns the URL, not a 404."""
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/tickets/abc123")
        assert r.status_code == 200
        assert "<div id='root'>" in r.text

    async def test_index_is_no_cache(self, app_with_spa):
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/")
        assert r.headers["cache-control"] == "no-cache"

    async def test_hashed_asset_is_served_with_immutable_cache(self, app_with_spa):
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/assets/index-abc123.js")
        assert r.status_code == 200
        assert r.text == "console.log('hi');"
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"

    async def test_unknown_asset_404s_instead_of_falling_back(self, app_with_spa):
        """Assets are content-addressed; a miss under /assets/ is a real
        404, not a silent index.html fallback (that would mask a broken
        build reference as a working page load)."""
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/assets/does-not-exist.js")
        assert r.status_code == 404

    async def test_top_level_static_file_served_as_is(self, app_with_spa):
        """A non-hashed file at the build root (favicon, manifest, ...) is
        served directly, not swapped for index.html."""
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/favicon.ico")
        assert r.status_code == 200
        assert r.text == "fake-icon"
        assert r.headers["cache-control"] == "no-cache"

    async def test_path_traversal_is_rejected(self, app_with_spa):
        app, dist_root = app_with_spa
        # Write a secret file just outside the dist root to prove it can't
        # be reached via a traversal attempt through /assets/. The client
        # normalizes the ".." segments before the request is even sent, so
        # this lands on the root catch-all (-> index.html, 200) rather than
        # the /assets/{path} route (-> 404); either is fine, only leaking
        # the secret's content would not be.
        secret = dist_root.parent / "secret.txt"
        secret.write_text("do not serve me")
        async with await _client(app) as ac:
            r = await ac.get("/assets/../../secret.txt")
        assert r.status_code in (200, 400, 404)
        assert "do not serve me" not in r.text

    async def test_direct_traversal_segment_is_rejected(self, app_with_spa):
        """A raw (unnormalized) traversal segment reaching the asset handler
        is still refused by ``_safe_join``'s resolved-path prefix check."""
        app, dist_root = app_with_spa
        secret = dist_root.parent / "secret.txt"
        secret.write_text("do not serve me")
        target = spa_app._safe_join(dist_root / "assets", "../../secret.txt")
        assert target is None

    async def test_legacy_app_root_redirects_to_root(self, app_with_spa):
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/app", follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "/"

    async def test_legacy_app_path_redirects_to_equivalent_root_path(self, app_with_spa):
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/app/tickets/abc123", follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "/tickets/abc123"

    async def test_legacy_app_assets_path_redirects(self, app_with_spa):
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/app/assets/index-abc123.js", follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "/assets/index-abc123.js"

    async def test_legacy_app_redirect_actually_resolves(self, app_with_spa):
        """Following the redirect lands on the real page, end to end."""
        app, _ = app_with_spa
        async with await _client(app) as ac:
            r = await ac.get("/app/tickets/abc123", follow_redirects=True)
        assert r.status_code == 200
        assert "<div id='root'>" in r.text


class TestSpaMountAbsent:
    async def test_no_dist_dir_means_no_root_index(self, app_without_spa):
        async with await _client(app_without_spa) as ac:
            r = await ac.get("/")
        assert r.status_code == 404

    async def test_no_dist_dir_still_redirects_app_legacy_path(self, app_without_spa):
        """The /app redirect shim is unconditional — registered even when
        there's no build to redirect to, so it never depends on mount order
        with the (absent) SPA root router."""
        async with await _client(app_without_spa) as ac:
            r = await ac.get("/app", follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "/"

    async def test_no_dist_dir_leaves_other_routes_working(self, app_without_spa):
        async with await _client(app_without_spa) as ac:
            r = await ac.get(
                "/api/v1/tickets", headers={"Authorization": "Bearer t"},
            )
        assert r.status_code == 200


class TestDefaultDistRootResolution:
    def test_default_dist_root_is_repo_frontend_dist(self):
        root = spa_app.default_dist_root()
        assert root.parts[-2:] == ("frontend", "dist")

    def test_build_router_returns_none_without_index_html(self, tmp_path):
        empty = tmp_path / "empty-dist"
        empty.mkdir()
        assert spa_app.build_router(empty) is None

    def test_build_router_returns_none_for_missing_dir(self, tmp_path):
        assert spa_app.build_router(tmp_path / "does-not-exist") is None

    def test_build_router_returns_router_when_index_html_present(self, tmp_path):
        dist_root = _write_fake_dist(tmp_path / "dist2")
        assert spa_app.build_router(dist_root) is not None
