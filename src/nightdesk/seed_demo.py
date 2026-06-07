"""Seed a demo database with realistic tickets, runs, and transcripts.

Usage::

    nightdesk-seed-demo                       # seed defaults
    nightdesk-seed-demo --reset               # wipe and re-seed
    nightdesk-seed-demo --db-path /tmp/d.db   # custom DB location
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from nightdesk.db.models import Ticket, WorkerHeartbeat
from nightdesk.domain.labels import LabelNameTaken, create_label, list_labels
from nightdesk.db.session import make_engine, session_factory
from nightdesk.domain.profiles import seed_default_profiles
from nightdesk.domain.projects import ProjectNameTaken, create_project
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import create_ticket
from nightdesk.transcript import now_iso, write_event


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_DEMO_DIR = Path(
    os.path.expanduser("~/.local/share/nightdesk-demo")
)
DEFAULT_DB_PATH = DEFAULT_DEMO_DIR / "nightdesk.db"
DEFAULT_TRANSCRIPT_ROOT = DEFAULT_DEMO_DIR / "transcripts"


# ---------------------------------------------------------------------------
# Ticket specifications
# ---------------------------------------------------------------------------

_TICKET_SPECS: list[dict] = [
    # --- Inbox (under-specified triage items) ---
    # "incomplete": True items are captured with NO workspace, so they cannot be
    # promoted until fleshed out — they exercise the validation boundary in the
    # Inbox surface. Items without the flag are complete and promotable.
    {
        "title": "Investigate flaky integration test",
        "prompt": (
            "Someone mentioned test_worker_picks_eligible flakes in CI. "
            "Need to reproduce and narrow down before this becomes a ticket."
        ),
        "status": "inbox",
        "priority": 3,
        "labels": ["bug"],
        "incomplete": True,
    },
    {
        "title": "Idea: keyboard-only ticket triage mode",
        "prompt": "",
        "status": "inbox",
        "priority": 0,
        "labels": ["ui/ux"],
        "incomplete": True,
    },
    {
        "title": "Add OpenAPI examples to ticket endpoints",
        "prompt": (
            "Flesh out request/response examples in the generated openapi.json "
            "so the docs surface is usable. Workspace is known; ready to promote."
        ),
        "status": "inbox",
        "priority": 1,
        "labels": ["docs"],
    },
    # --- Draft ---
    {
        "title": "Add dark mode toggle",
        "prompt": (
            "Implement a dark mode toggle in the settings page. "
            "Store the preference in localStorage. "
            "Apply a `dark` class to the document root so Tailwind dark: "
            "variants kick in automatically."
        ),
        "status": "draft",
        "priority": 2,
        "labels": ["ui/ux"],
    },
    {
        "title": "Write onboarding guide",
        "prompt": (
            "Create a getting-started guide in docs/onboarding.md. "
            "Cover installation, first run, profile setup, and creating "
            "your first ticket. Keep it under 200 words."
        ),
        "status": "draft",
        "priority": 0,
        "labels": ["docs"],
    },
    # --- Queued ---
    {
        "title": "Fix pagination on archive view",
        "prompt": (
            "The archive page pagination breaks when the total count exceeds "
            "200 items. The Next button sends an invalid cursor. "
            "Reproduce with > 200 archived tickets and fix the cursor encoding."
        ),
        "status": "queued",
        "priority": 3,
        "labels": ["backend", "cleanup"],
    },
    {
        "title": "Add rate-limit banner component",
        "prompt": (
            "Add a dismissible banner to the top of the board that shows the "
            "current rate-limit status pulled from the /api/v1/rate-limit "
            "endpoint. Show utilization percentage and time until reset."
        ),
        "status": "queued",
        "priority": 1,
        "labels": ["ui/ux", "backend"],
    },
    # --- Running ---
    {
        "title": "Refactor authentication middleware",
        "prompt": (
            "Refactor the auth middleware to support multiple auth providers "
            "(bearer token, session cookie, OAuth). Extract provider-specific "
            "logic into separate strategies behind a common AuthProvider "
            "interface. Maintain backward compatibility with the existing "
            "bearer-token flow."
        ),
        "status": "running",
        "priority": 4,
        "labels": ["backend", "infra"],
        "run": {
            "intent": "first_run",
            "exit_status": None,
            "transcript": "running_success",
        },
    },
    # --- Review ---
    {
        "title": "Implement CSV export for reports",
        "prompt": (
            "Add CSV export to the reports page. Each row should include "
            "ticket id, title, status, profile name, created_at, and "
            "run count. Use streaming response so large exports don't OOM."
        ),
        "status": "review",
        "priority": 2,
        "labels": ["backend"],
        "run": {
            "intent": "first_run",
            "exit_status": "success",
            "transcript": "review_success",
        },
    },
    {
        "title": "Migrate to pydantic v2 models",
        "prompt": (
            "Update all API request/response schemas from pydantic v1 "
            "compatibility mode to native v2. Replace validator decorators "
            "with model_validator. Run the test suite and fix any breakage."
        ),
        "status": "review",
        "priority": 3,
        "labels": ["backend", "cleanup"],
        "run": {
            "intent": "first_run",
            "exit_status": "failed",
            "error_summary": "Runner exited with code 1: test suite failure",
            "transcript": "review_failed",
        },
    },
    {
        "title": "Upgrade to SQLAlchemy 2.1",
        "prompt": (
            "Bump the SQLAlchemy dependency to 2.1 and update the session "
            "factory to use the new async-compatible engine API. "
            "Keep the existing sync session paths working."
        ),
        "status": "review",
        "priority": 1,
        "labels": ["backend", "infra"],
        "run": {
            "intent": "retry",
            "exit_status": "cancelled",
            "error_summary": "Run cancelled by user.",
            "transcript": "review_cancelled",
        },
    },
    # --- Archived ---
    {
        "title": "Update dependency versions",
        "prompt": (
            "Bump all pinned dependencies in pyproject.toml to their latest "
            "stable releases. Run the full test suite and fix any "
            "deprecation warnings."
        ),
        "status": "archived",
        "priority": 0,
        "labels": ["cleanup"],
        "run": {
            "intent": "first_run",
            "exit_status": "success",
            "transcript": "archived_success",
        },
    },
    {
        "title": "Add health-check endpoint",
        "prompt": (
            "Add GET /healthz that returns 200 with {\"status\": \"ok\"}. "
            "Include DB connectivity check. Document in the API reference."
        ),
        "status": "archived",
        "priority": 1,
        "labels": ["backend", "docs"],
        "run": {
            "intent": "first_run",
            "exit_status": "success",
            "transcript": "archived_success_short",
        },
    },
]


# ---------------------------------------------------------------------------
# Label specifications
# ---------------------------------------------------------------------------

_LABEL_SPECS: list[dict] = [
    {"name": "backend",  "color": "#3b82f6"},  # blue
    {"name": "ui/ux",    "color": "#8b5cf6"},  # violet
    {"name": "research", "color": "#f59e0b"},  # amber
    {"name": "infra",    "color": "#ef4444"},  # red
    {"name": "docs",     "color": "#10b981"},  # emerald
    {"name": "cleanup",  "color": "#6b7280"},  # gray
]


# Demo projects so the board's group-by-project view has content. Tickets are
# assigned to these round-robin in the seeder (with some left unassigned).
_PROJECT_SPECS: list[dict] = [
    {"name": "Web App",   "slug": "web-app",  "color": "#2563eb"},
    {"name": "Platform",  "slug": "platform", "color": "#db2777"},
]


# ---------------------------------------------------------------------------
# Synthetic transcript builders
# ---------------------------------------------------------------------------

def _seq(counter: list[int]) -> int:
    v = counter[0]
    counter[0] = v + 1
    return v


def _write_transcript(
    path: Path,
    run_id: str,
    ticket_id: str,
    variant: str,
) -> None:
    """Write a synthetic NDJSON transcript to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    counter = [0]
    ts_base = datetime.now(timezone.utc) - timedelta(minutes=15)

    def _ts(seq_num: int) -> str:
        return (ts_base + timedelta(seconds=seq_num * 2)).isoformat(
            timespec="microseconds"
        )

    def _evt(extra: dict) -> dict:
        s = _seq(counter)
        return {"type": extra.pop("_type"), "ts": _ts(s), "seq": s, **extra}

    events: list[dict] = []

    # --- meta ---
    events.append(_evt({"_type": "meta", "run_id": run_id, "ticket_id": ticket_id}))

    if variant in (
        "running_success",
        "review_success",
        "archived_success",
        "archived_success_short",
    ):
        _add_success_transcript(events, _evt, variant)
    elif variant == "review_failed":
        _add_failed_transcript(events, _evt)
    elif variant == "review_cancelled":
        _add_cancelled_transcript(events, _evt)

    with path.open("ab") as fh:
        for evt in events:
            write_event(fh, evt)


def _add_success_transcript(
    events: list[dict], _evt, variant: str
) -> None:
    events.append(_evt({"_type": "thinking", "text": "Let me look at the current implementation first."}))
    events.append(_evt({"_type": "assistant_text", "text": "I'll start by examining the existing code to understand the current structure."}))

    # Read tool use
    events.append(_evt({
        "_type": "tool_use",
        "id": "tu_001",
        "tool": "Read",
        "input": {"file_path": "/project/src/main.py"},
    }))
    events.append(_evt({
        "_type": "tool_result",
        "tool_use_id": "tu_001",
        "output": "1  from fastapi import FastAPI\n2  \n3  app = FastAPI()\n4  \n5  @app.get(\"/\")\n6  def root():\n7      return {\"status\": \"ok\"}",
        "is_error": False,
    }))

    if variant != "archived_success_short":
        # Grep tool use
        events.append(_evt({
            "_type": "tool_use",
            "id": "tu_002",
            "tool": "Grep",
            "input": {"pattern": "def export", "path": "/project/src"},
        }))
        events.append(_evt({
            "_type": "tool_result",
            "tool_use_id": "tu_002",
            "output": "src/reports.py:45:def export_csv(query):\nsrc/reports.py:78:def export_json(query):",
            "is_error": False,
        }))

        # Edit tool use
        events.append(_evt({
            "_type": "tool_use",
            "id": "tu_003",
            "tool": "Edit",
            "input": {
                "file_path": "/project/src/reports.py",
                "old_string": "def export_csv(query):\n    return []",
                "new_string": "def export_csv(query):\n    rows = query.all()\n    return [format_row(r) for r in rows]",
            },
        }))
        events.append(_evt({
            "_type": "tool_result",
            "tool_use_id": "tu_003",
            "output": "Successfully edited file.",
            "is_error": False,
        }))

        events.append(_evt({"_type": "thinking", "text": "The edit looks correct. Let me verify by running the tests."}))
        events.append(_evt({"_type": "assistant_text", "text": "I've made the changes. Let me verify the tests pass."}))

        # Bash tool use
        events.append(_evt({
            "_type": "tool_use",
            "id": "tu_004",
            "tool": "Bash",
            "input": {"command": "cd /project && python -m pytest tests/ -x -q"},
        }))
        events.append(_evt({
            "_type": "tool_result",
            "tool_use_id": "tu_004",
            "output": "42 passed in 3.14s",
            "is_error": False,
        }))
    else:
        events.append(_evt({"_type": "assistant_text", "text": "The health check endpoint already exists at /healthz. The implementation returns 200 with {\"status\": \"ok\"} and includes a DB connectivity check. Nothing to change here."}))

    events.append(_evt({"_type": "assistant_text", "text": "All changes are in place and tests pass."}))

    # Stats event
    events.append(_evt({
        "_type": "stats",
        "scope": "turn",
        "model": "claude-sonnet-4-20250514",
        "input_tokens": 8420,
        "output_tokens": 623,
        "cache_read_tokens": 6100,
        "cache_creation_tokens": 280,
    }))

    # Result
    events.append(_evt({
        "_type": "result",
        "subtype": "success",
        "summary": "Implementation complete. All tests passing.",
    }))


def _add_failed_transcript(events: list[dict], _evt) -> None:
    events.append(_evt({"_type": "thinking", "text": "Let me check the current test results."}))
    events.append(_evt({"_type": "assistant_text", "text": "Running the test suite to see what breaks."}))

    events.append(_evt({
        "_type": "tool_use",
        "id": "tu_010",
        "tool": "Bash",
        "input": {"command": "cd /project && python -m pytest tests/ -x -q"},
    }))
    events.append(_evt({
        "_type": "tool_result",
        "tool_use_id": "tu_010",
        "output": (
            "FAILED tests/test_schemas.py::TestUserSchema::test_email_validation\n"
            "FAILED tests/test_schemas.py::TestUserSchema::test_serialization\n"
            "1 failed, 38 passed in 2.81s"
        ),
        "is_error": True,
    }))

    events.append(_evt({"_type": "assistant_text", "text": "Two test failures in the schema validation module. Let me look at the failing tests."}))

    events.append(_evt({
        "_type": "tool_use",
        "id": "tu_011",
        "tool": "Read",
        "input": {"file_path": "/project/tests/test_schemas.py"},
    }))
    events.append(_evt({
        "_type": "tool_result",
        "tool_use_id": "tu_011",
        "output": "1  from pydantic import BaseModel, validator\n2  \n3  class UserSchema(BaseModel):\n4      email: str\n5  \n6      @validator('email')\n7      def validate_email(cls, v):\n8          if '@' not in v:\n9              raise ValueError('invalid email')\n10          return v",
        "is_error": False,
    }))

    events.append(_evt({"_type": "thinking", "text": "The tests use pydantic v1 @validator syntax. Need to migrate to v2 model_validator."}))

    # Worker error as the terminal event
    events.append(_evt({
        "_type": "worker_error",
        "kind": "runner_exit_nonzero",
        "summary": "Runner exited with code 1: test suite failure",
        "traceback": (
            "Traceback (most recent call last):\n"
            "  File \"worker/run_one.py\", line 142, in _run_process\n"
            "    raise RunnerExitNonZero(proc.returncode)\n"
            "nightdesk.worker.errors.RunnerExitNonZero: exit code 1"
        ),
    }))


def _add_cancelled_transcript(events: list[dict], _evt) -> None:
    events.append(_evt({"_type": "thinking", "text": "Starting the migration. Let me first check the current dependency version."}))
    events.append(_evt({"_type": "assistant_text", "text": "I'll start by checking the current SQLAlchemy version and reviewing the changelog for breaking changes."}))

    events.append(_evt({
        "_type": "tool_use",
        "id": "tu_020",
        "tool": "Bash",
        "input": {"command": "cd /project && pip show sqlalchemy | head -3"},
    }))

    # The cancelled event cuts off mid-stream
    events.append(_evt({
        "_type": "cancelled",
        "message": "Run cancelled by user.",
    }))


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------


def run_migrations(db_path: Path) -> None:
    """Run alembic upgrade head against the given DB path."""
    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    if not alembic_ini.exists():
        from nightdesk.db.models import Base
        engine = make_engine(db_path)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        return

    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_cmd

    engine = make_engine(db_path)
    try:
        alembic_cfg = AlembicConfig(str(alembic_ini))
        alembic_cfg.set_main_option(
            "sqlalchemy.url", str(engine.url)
        )
        alembic_cmd.upgrade(alembic_cfg, "head")
    finally:
        engine.dispose()


def seed(
    db_path: Path,
    transcript_root: Path,
    *,
    source_path: str,
) -> None:
    """Seed the demo database. Creates profiles, tickets, runs, transcripts."""
    engine = make_engine(db_path)
    SessionLocal = session_factory(engine)

    # Seed profiles (returns detached objects, so we query IDs below)
    seed_default_profiles(engine)

    with SessionLocal() as session:
        # Fetch profile IDs in-session to avoid DetachedInstanceError
        profile_ids = [
            row[0] for row in session.execute(
                text("SELECT id FROM profiles ORDER BY name")
            ).fetchall()
        ]
        if not profile_ids:
            print("No profiles found after seeding.", file=sys.stderr)
            return

        # --- Seed labels ---
        # Idempotent: reuse any label already present (the demo seeder is
        # designed to be safe to re-run), creating only the missing ones.
        label_by_name: dict[str, object] = {
            lbl.name: lbl for lbl in list_labels(session)
        }
        for spec in _LABEL_SPECS:
            if spec["name"] in label_by_name:
                continue
            lbl = create_label(session, name=spec["name"], color=spec["color"])
            label_by_name[lbl.name] = lbl

        # --- Seed projects ---
        # A couple of demo projects so the board's group-by-project view (and
        # the project facet) has something to show. Idempotent like labels.
        from nightdesk.domain.projects import (
            create_project as _create_project,
            list_projects as _list_projects,
        )
        project_by_slug: dict[str, object] = {
            p.slug: p for p in _list_projects(session)
        }
        seeded_projects: list = []
        for spec in _PROJECT_SPECS:
            existing = project_by_slug.get(spec["slug"])
            if existing is None:
                existing = _create_project(
                    session,
                    name=spec["name"],
                    slug=spec["slug"],
                    source_path=source_path,
                    color=spec.get("color"),
                )
                project_by_slug[existing.slug] = existing
            seeded_projects.append(existing)

        tickets_by_status: dict[str, list[Ticket]] = {}

        for idx, raw_spec in enumerate(_TICKET_SPECS):
            spec = dict(raw_spec)  # copy to avoid mutating the module-level list
            run_spec = spec.pop("run", None)
            label_names = spec.pop("labels", [])
            # Under-specified inbox items are captured without a workspace so the
            # Inbox surface can demo the "flesh out before promoting" boundary.
            incomplete = spec.pop("incomplete", False)
            profile_id = profile_ids[idx % len(profile_ids)]
            # Assign most tickets to a project (round-robin), leaving every
            # third one unassigned so the "No project" bucket is populated too.
            project_id = None
            if seeded_projects and idx % 3 != 2:
                project_id = seeded_projects[idx % len(seeded_projects)].id
            # Assigning a project injects that project's default workspace, which
            # would make an "incomplete" inbox item complete. Keep these
            # project-less so they stay genuinely under-specified for the demo.
            if incomplete:
                project_id = None

            create_kwargs = dict(
                title=spec["title"],
                prompt=spec["prompt"],
                status=spec["status"],
                profile_id=profile_id,
                project_id=project_id,
                priority=spec.get("priority", idx % 3),
            )
            if not incomplete:
                create_kwargs["source_path"] = source_path
            ticket = create_ticket(session, **create_kwargs)

            # Assign labels to the ticket.
            if label_names and label_by_name:
                from nightdesk.domain.labels import set_ticket_labels
                label_ids = [
                    label_by_name[n].id for n in label_names
                    if n in label_by_name
                ]
                if label_ids:
                    set_ticket_labels(session, ticket.id, label_ids)

            tickets_by_status.setdefault(ticket.status, []).append(ticket)

            if run_spec is not None:
                trans_path = str(transcript_root / f"demo-{ticket.id}.log")

                run = start_run(
                    session,
                    ticket_id=ticket.id,
                    worktree_path=source_path,
                    transcript_path=trans_path,
                    pid=10_000 + idx,
                    host="demo-host",
                    intent=run_spec["intent"],
                )

                # Write transcript
                _write_transcript(
                    Path(trans_path),
                    run_id=run.id,
                    ticket_id=ticket.id,
                    variant=run_spec["transcript"],
                )

                if run_spec["exit_status"] is not None:
                    finish_run(
                        session,
                        run.id,
                        exit_status=run_spec["exit_status"],
                        error_summary=run_spec.get("error_summary"),
                    )
                    # Update token counts on finished runs for realism
                    from nightdesk.db.models import Run
                    db_run = session.get(Run, run.id)
                    if db_run is not None:
                        db_run.input_tokens = 8420 + (idx * 100)
                        db_run.output_tokens = 623 + (idx * 50)
                        db_run.cache_read_tokens = 6100
                        db_run.model_used = "claude-sonnet-4-20250514"
                        db_run.cost_usd = round(0.012 + (idx * 0.003), 4)
                        if run_spec["exit_status"] == "success":
                            db_run.session_id = f"sess-demo-{ticket.id[:8]}"
                        session.commit()

        # For the running ticket: insert a WorkerHeartbeat so the pill shows alive
        running_tickets = tickets_by_status.get("running", [])
        if running_tickets:
            hb = WorkerHeartbeat(
                host="demo-host",
                pid=12345,
                last_seen_at=datetime.now(timezone.utc),
            )
            session.add(hb)
            session.commit()

    engine.dispose()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point: nightdesk-seed-demo."""
    parser = argparse.ArgumentParser(
        prog="nightdesk-seed-demo",
        description="Seed a demo database with realistic tickets, runs, and transcripts.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the demo database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--transcript-root",
        type=Path,
        default=DEFAULT_TRANSCRIPT_ROOT,
        help=f"Root directory for transcript files (default: {DEFAULT_TRANSCRIPT_ROOT})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the demo DB and transcripts before seeding.",
    )
    args = parser.parse_args()

    db_path: Path = args.db_path
    transcript_root: Path = args.transcript_root

    if args.reset:
        if db_path.exists():
            db_path.unlink()
            print(f"Deleted {db_path}")
        # Clean WAL/SHM files
        for suffix in ("-wal", "-shm"):
            p = db_path.parent / (db_path.name + suffix)
            if p.exists():
                p.unlink()
        if transcript_root.exists():
            import shutil
            shutil.rmtree(transcript_root)
            print(f"Deleted {transcript_root}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_root.mkdir(parents=True, exist_ok=True)

    # Run migrations
    print(f"Running migrations against {db_path} ...")
    run_migrations(db_path)

    # Determine source path: prefer repo root, fall back to /tmp
    source_path = str(Path(__file__).parent.parent.parent)
    if not Path(source_path).is_dir():
        source_path = "/tmp"

    # Seed
    print("Seeding demo data ...")
    seed(db_path, transcript_root, source_path=source_path)

    # Print instructions
    print()
    print("Demo database seeded.")
    print(f"  DB:            {db_path}")
    print(f"  Transcripts:   {transcript_root}")
    print()
    print("To launch against the demo DB, set in config.toml:")
    print(f'  db_path = "{db_path}"')
    print(f'  transcript_root = "{transcript_root}"')
    print()
    print("Or run:")
    print(f"  nightdesk-dev  # then edit config.toml to point at the demo DB")
