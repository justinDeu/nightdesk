from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.engine import Engine

from nightdesk.api.deps import get_session_dep
from nightdesk.api.routes import analytics as analytics_routes
from nightdesk.api.routes import auth as auth_routes
from nightdesk.api.routes import backends as backends_routes
from nightdesk.api.routes import config as config_routes
from nightdesk.api.routes import cron_jobs as cron_jobs_routes
from nightdesk.api.routes import effective_config as effective_config_routes
from nightdesk.api.routes import fs as fs_routes
from nightdesk.api.routes import health
from nightdesk.api.routes import helpers as helpers_routes
from nightdesk.api.routes import inbox as inbox_routes
from nightdesk.api.routes import labels as labels_routes
from nightdesk.api.routes import profiles as profiles_routes
from nightdesk.api.routes import projects as projects_routes
from nightdesk.api.routes import providers as providers_routes
from nightdesk.api.routes import runs as runs_routes
from nightdesk.api.routes import search as search_routes
from nightdesk.api.routes import tickets as tickets_routes
from nightdesk.api.routes import transcript as transcript_routes
from nightdesk.api.routes import saved_views as saved_views_routes
from nightdesk.api import spa as spa_app
from nightdesk.domain.pricing import DEFAULT_PRICING_URL


def create_app(
    *,
    engine: Engine,
    bearer_token: str,
    # Unused now that the Jinja/HTMX surface is gone (there's nothing left to
    # serve under /static). Kept as a parameter rather than removed outright
    # so the many existing call sites/fixtures across the codebase that still
    # pass it don't all need updating; the value is never read.
    static_root: Path | None = None,
    transcript_root: Path | None = None,
    worktree_root: Path | None = None,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8765,
    data_dir: Path | None = None,
    # Default is to fetch live and fall back to the bundled table; config only
    # overrides the URL. Pass an empty string to disable live resolution.
    pricing_url: str | None = DEFAULT_PRICING_URL,
    # Vite build output (frontend/dist) mounted at / when present; see
    # nightdesk.api.spa for the exact cache policy and the required Vite
    # `base: "/"` setting. None resolves to spa.default_dist_root(); when
    # that path has no build either, nothing is mounted and every /api/v1
    # route is unaffected either way.
    spa_dist_root: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="nightdesk", version="0.1.0")
    app.state.engine = engine
    app.state.bearer_token = bearer_token
    app.state.transcript_root = transcript_root
    app.state.worktree_root = worktree_root
    app.state.data_dir = data_dir
    app.state.pricing_url = pricing_url
    app.state.one_shot_store = auth_routes.OneShotStore()

    app.include_router(health.router)

    get_session = get_session_dep(engine)
    app.include_router(profiles_routes.build_router(get_session, bearer_token))
    app.include_router(providers_routes.build_router(get_session, bearer_token))
    app.include_router(backends_routes.build_router(bearer_token))
    app.include_router(projects_routes.build_router(get_session, bearer_token))
    app.include_router(tickets_routes.build_router(get_session, bearer_token))
    app.include_router(runs_routes.build_router(get_session, bearer_token))
    app.include_router(config_routes.build_router(
        get_session, bearer_token,
        worktree_root=str(worktree_root), transcript_root=str(transcript_root),
    ))
    app.include_router(search_routes.build_router(get_session, bearer_token))
    app.include_router(effective_config_routes.build_api_router(get_session, bearer_token))
    app.include_router(labels_routes.build_router(get_session, bearer_token))
    app.include_router(transcript_routes.build_router(get_session, bearer_token))
    app.include_router(cron_jobs_routes.build_router(get_session, bearer_token))
    app.include_router(saved_views_routes.build_api_router(get_session, bearer_token))
    app.include_router(inbox_routes.build_api_router(get_session, bearer_token))
    app.include_router(analytics_routes.build_router(get_session, bearer_token))
    app.include_router(helpers_routes.build_router(
        get_session, bearer_token, worktree_root=worktree_root,
    ))
    app.include_router(fs_routes.build_router(bearer_token))
    app.include_router(auth_routes.build_router(
        bearer_token=bearer_token,
        one_shot_store=app.state.one_shot_store,
        session_cookie_max_age=60 * 60 * 24 * 30,
        bind_host=bind_host,
        bind_port=bind_port,
    ))

    # SPA serving. The legacy-redirect router (/app/* -> 308 -> /*) must be
    # registered before the SPA root router below, and the SPA root router
    # must be registered LAST of all — its GET /{path:path} catch-all would
    # otherwise shadow every route after it. See nightdesk.api.spa's module
    # docstring for the full reasoning.
    app.include_router(spa_app.build_app_redirect_router())
    spa_router = spa_app.build_router(spa_dist_root or spa_app.default_dist_root())
    if spa_router is not None:
        app.include_router(spa_router)

    return app
