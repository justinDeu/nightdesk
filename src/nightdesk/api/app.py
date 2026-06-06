from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine

from nightdesk.api.deps import get_session_dep
from nightdesk.api.routes import archive as archive_routes
from nightdesk.api.routes import auth as auth_routes
from nightdesk.api.routes import board as board_routes
from nightdesk.api.routes import config as config_routes
from nightdesk.api.routes import cron_jobs as cron_jobs_routes
from nightdesk.api.routes import cron_page as cron_page_routes
from nightdesk.api.routes import diagnostics as diagnostics_routes
from nightdesk.api.routes import fs as fs_routes
from nightdesk.api.routes import header as header_routes
from nightdesk.api.routes import health
from nightdesk.api.routes import labels as labels_routes
from nightdesk.api.routes import profiles as profiles_routes
from nightdesk.api.routes import projects as projects_routes
from nightdesk.api.routes import runs as runs_routes
from nightdesk.api.routes import search as search_routes
from nightdesk.api.routes import ticket_page as ticket_page_routes
from nightdesk.api.routes import tickets as tickets_routes
from nightdesk.api.routes import transcript as transcript_routes
from nightdesk.api.routes import ui as ui_routes


def create_app(
    *,
    engine: Engine,
    bearer_token: str,
    static_root: Path,
    transcript_root: Path,
    worktree_root: Path,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8765,
) -> FastAPI:
    app = FastAPI(title="nightdesk", version="0.1.0")
    app.state.engine = engine
    app.state.bearer_token = bearer_token
    app.state.transcript_root = transcript_root
    app.state.worktree_root = worktree_root
    app.state.one_shot_store = auth_routes.OneShotStore()

    app.include_router(health.router)

    get_session = get_session_dep(engine)
    app.include_router(profiles_routes.build_router(get_session, bearer_token))
    app.include_router(projects_routes.build_router(get_session, bearer_token))
    app.include_router(tickets_routes.build_router(get_session, bearer_token))
    app.include_router(runs_routes.build_router(get_session, bearer_token))
    app.include_router(config_routes.build_router(
        get_session, bearer_token,
        worktree_root=str(worktree_root), transcript_root=str(transcript_root),
    ))
    app.include_router(search_routes.build_router(get_session, bearer_token))
    app.include_router(labels_routes.build_router(get_session, bearer_token))
    app.include_router(transcript_routes.build_router(get_session, bearer_token))
    app.include_router(cron_jobs_routes.build_router(get_session, bearer_token))

    templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
    # Expose the transcript-renderer helpers so the Jinja macros can compute
    # diff rows / bash elision / fenced-code segments without baking it into
    # each template. The JS live-tail mirrors the same logic in JS.
    from nightdesk import transcript_view as _tv  # local import: avoid cycles
    templates.env.globals["tv"] = _tv

    # Cache-busting helper for our own static assets. Appends the file's mtime
    # as ?v= so a changed app.css / *.js is refetched instead of served stale
    # from the browser cache. Falls back to the bare path if the file is
    # missing (e.g. in tests with an empty static_root).
    def _static_v(path: str) -> str:
        try:
            mtime = int((static_root / path).stat().st_mtime)
        except OSError:
            return f"/static/{path}"
        return f"/static/{path}?v={mtime}"

    templates.env.globals["static_v"] = _static_v

    # UTC ISO string for client-side localization (localize_time.js). Run/
    # ticket datetimes are UTC but come back from SQLite naive; emitting a bare
    # isoformat() would make the browser parse them as local time. Force a UTC
    # offset so ``new Date()`` interprets them correctly, then renders in the
    # viewer's own timezone.
    def _iso_utc(dt: datetime | None) -> str:
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    templates.env.globals["iso_utc"] = _iso_utc
    app.state.templates = templates
    app.include_router(ui_routes.build_router(get_session, bearer_token, templates,
                                                          transcript_root=transcript_root))
    app.include_router(profiles_routes.build_html_router(get_session, bearer_token, templates))
    app.include_router(board_routes.build_router(
        get_session, bearer_token, templates,
        transcript_root=transcript_root, worktree_root=worktree_root,
    ))
    app.include_router(archive_routes.build_router(get_session, bearer_token, templates))
    app.include_router(ticket_page_routes.build_router(get_session, bearer_token, templates))
    app.include_router(cron_page_routes.build_router(get_session, bearer_token, templates))
    app.include_router(header_routes.build_router(
        get_session, bearer_token, templates,
        worktree_root=str(worktree_root), transcript_root=str(transcript_root),
    ))
    app.include_router(fs_routes.build_router(get_session, bearer_token, templates))
    app.include_router(auth_routes.build_router(
        bearer_token=bearer_token,
        one_shot_store=app.state.one_shot_store,
        templates=templates,
        session_cookie_max_age=60 * 60 * 24 * 30,
        bind_host=bind_host,
        bind_port=bind_port,
    ))
    app.include_router(diagnostics_routes.build_router(
        bearer_token=bearer_token, engine=engine, templates=templates,
    ))

    static_root.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_root)), name="static")
    return app
