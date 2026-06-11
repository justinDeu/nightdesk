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

from nightdesk.db.models import ConfigRow, Profile, Project, Run, Ticket, WorkerHeartbeat
from nightdesk.db.session import make_engine, session_factory
from nightdesk.domain.profiles import seed_default_profiles
from nightdesk.domain.projects import create_project
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import add_dependency, create_ticket
from nightdesk.transcript import now_iso, write_event


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_DEMO_DIR = Path(
    os.path.expanduser("~/.local/share/nightdesk-demo")
)
DEFAULT_DB_PATH = DEFAULT_DEMO_DIR / "nightdesk.db"
DEFAULT_TRANSCRIPT_ROOT = DEFAULT_DEMO_DIR / "transcripts"
DEFAULT_SOURCE_PATH = "/demo/nightdesk"


# ---------------------------------------------------------------------------
# Ticket specifications
# ---------------------------------------------------------------------------

_TICKET_SPECS: list[dict] = [
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
    },
    {
        "title": "Write onboarding guide",
        "prompt": (
            "Create a getting-started guide in docs/onboarding.md. "
            "Cover installation, first run, profile setup, and creating "
            "your first ticket. Keep it under 200 words."
        ),
        "status": "draft",
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
    },
    {
        "title": "Add rate-limit banner component",
        "prompt": (
            "Add a dismissible banner to the top of the board that shows the "
            "current rate-limit status pulled from the /api/v1/rate-limit "
            "endpoint. Show utilization percentage and time until reset."
        ),
        "status": "queued",
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
        "run": {
            "intent": "first_run",
            "exit_status": "success",
            "transcript": "archived_success_short",
        },
    },
]

_HISTORY_RUN_SPECS: list[dict] = [
    {
        "title": "Tighten transcript pagination",
        "prompt": "Fix cursor handling in the transcript viewer for long tool-result streams.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (18_400, 1_920, 31_600, 1_200),
        "cost": 0.42,
    },
    {
        "title": "Audit sandbox mount policy",
        "prompt": "Review bind mounts and document why each host path is exposed to the sandbox.",
        "model": "claude-opus-4-20250514",
        "outcome": "success",
        "tokens": (44_900, 4_860, 68_300, 3_100),
        "cost": 1.86,
    },
    {
        "title": "Improve command palette search",
        "prompt": "Add ranked matching for ticket titles, profile names, and project slugs.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (25_700, 2_740, 22_500, 1_850),
        "cost": 0.58,
    },
    {
        "title": "Investigate stale worker heartbeat",
        "prompt": "Find why the header sometimes reports offline while a worker is running.",
        "model": "claude-haiku-3.5-20241022",
        "outcome": "failed",
        "tokens": (9_800, 740, 7_400, 520),
        "cost": 0.08,
    },
    {
        "title": "Draft webhook notification examples",
        "prompt": "Write examples for Slack, Discord, and ntfy webhook payloads.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (13_200, 2_180, 4_600, 900),
        "cost": 0.31,
    },
    {
        "title": "Add project filter chips",
        "prompt": "Expose saved projects as first-class search chips on the board.",
        "model": "claude-opus-4-20250514",
        "outcome": "cancelled",
        "tokens": (37_600, 1_120, 18_900, 2_400),
        "cost": 1.10,
    },
    {
        "title": "Document run-token scopes",
        "prompt": "Explain scoped run tokens and their limits in the API reference.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (16_900, 3_240, 40_800, 1_350),
        "cost": 0.49,
    },
    {
        "title": "Profile import validation pass",
        "prompt": "Harden imported Claude Code settings so forbidden keys cannot persist.",
        "model": "claude-haiku-3.5-20241022",
        "outcome": "success",
        "tokens": (11_500, 1_050, 26_200, 760),
        "cost": 0.10,
    },
    {
        "title": "Optimize archive count query",
        "prompt": "Replace the archive count subquery with a cheaper aggregate path.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (31_200, 2_450, 55_400, 2_700),
        "cost": 0.73,
    },
    {
        "title": "Rework cron preview layout",
        "prompt": "Make the cron preview readable on narrow screens without horizontal scroll.",
        "model": "claude-opus-4-20250514",
        "outcome": "failed",
        "tokens": (52_300, 3_020, 12_600, 3_900),
        "cost": 1.74,
    },
    {
        "title": "Add worker log download link",
        "prompt": "Expose per-run worker logs from the run history table.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (14_700, 1_580, 33_900, 1_100),
        "cost": 0.38,
    },
    {
        "title": "Refine dependency warning copy",
        "prompt": "Clarify blocked-ticket messaging when an upstream run failed.",
        "model": "claude-haiku-3.5-20241022",
        "outcome": "success",
        "tokens": (6_100, 880, 3_200, 410),
        "cost": 0.04,
    },
    {
        "title": "Patch settings dirty-state guard",
        "prompt": "Prevent false dirty prompts after schedule window hydration.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (22_800, 1_990, 47_600, 1_540),
        "cost": 0.54,
    },
    {
        "title": "Prototype child-ticket creation",
        "prompt": "Use run-token callbacks to create follow-up tickets from an agent run.",
        "model": "claude-opus-4-20250514",
        "outcome": "cancelled",
        "tokens": (61_400, 2_770, 72_100, 4_600),
        "cost": 2.24,
    },
    {
        "title": "Normalize project slug routing",
        "prompt": "Make project filters preserve slug casing in URLs and forms.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (17_300, 1_460, 14_800, 990),
        "cost": 0.34,
    },
    {
        "title": "Add analytics cache legend",
        "prompt": "Explain cache read and cache write segments in the analytics chart.",
        "model": "claude-haiku-3.5-20241022",
        "outcome": "success",
        "tokens": (8_200, 1_210, 19_700, 620),
        "cost": 0.07,
    },
    {
        "title": "Review API PATCH semantics",
        "prompt": "Check partial update behavior for profiles, tickets, and projects.",
        "model": "claude-opus-4-20250514",
        "outcome": "success",
        "tokens": (48_100, 4_420, 92_300, 3_300),
        "cost": 2.05,
    },
    {
        "title": "Fix sidebar selection highlight",
        "prompt": "Keep the selected card highlighted after HTMX column refreshes.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (19_600, 1_670, 29_400, 1_260),
        "cost": 0.41,
    },
    {
        "title": "Harden path suggestion endpoint",
        "prompt": "Avoid leaking protected Nightdesk directories in path suggestions.",
        "model": "claude-haiku-3.5-20241022",
        "outcome": "failed",
        "tokens": (12_400, 930, 5_500, 840),
        "cost": 0.09,
    },
    {
        "title": "Add cost chip hover details",
        "prompt": "Show today and month spend in the header hover panel.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (21_100, 1_890, 44_200, 1_480),
        "cost": 0.51,
    },
    {
        "title": "Migrate schedule windows tests",
        "prompt": "Cover overlapping windows, timezone changes, and run-now bypasses.",
        "model": "claude-opus-4-20250514",
        "outcome": "success",
        "tokens": (57_800, 5_260, 81_900, 4_200),
        "cost": 2.32,
    },
    {
        "title": "Triage transcript SSE reconnects",
        "prompt": "Make transcript streaming resume cleanly after a network drop.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (28_900, 2_310, 63_500, 2_050),
        "cost": 0.69,
    },
    {
        "title": "Clean up profile export schema",
        "prompt": "Remove UI-only fields from exported profiles and document the format.",
        "model": "claude-haiku-3.5-20241022",
        "outcome": "success",
        "tokens": (7_900, 1_340, 15_100, 530),
        "cost": 0.06,
    },
    {
        "title": "Fix run-again workspace policy",
        "prompt": "Respect restart workspace policy when a review ticket is run again.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "failed",
        "tokens": (34_600, 1_870, 10_200, 2_640),
        "cost": 0.64,
    },
    {
        "title": "Add diagnostics version block",
        "prompt": "Show Python, Nightdesk, Claude Code, and bubblewrap versions together.",
        "model": "claude-opus-4-20250514",
        "outcome": "success",
        "tokens": (41_200, 3_880, 74_700, 3_080),
        "cost": 1.79,
    },
    {
        "title": "Improve mobile board density",
        "prompt": "Tune card spacing and column controls on small screens.",
        "model": "claude-sonnet-4-20250514",
        "outcome": "success",
        "tokens": (15_400, 1_520, 9_700, 1_010),
        "cost": 0.30,
    },
    {
        "title": "Verify encrypted env rotation",
        "prompt": "Test that profile env secrets rotate without exposing plaintext values.",
        "model": "claude-haiku-3.5-20241022",
        "outcome": "success",
        "tokens": (10_600, 1_180, 23_900, 710),
        "cost": 0.09,
    },
    {
        "title": "Refactor run diff panel",
        "prompt": "Split diff summary, changed files, and raw patch rendering into partials.",
        "model": "claude-opus-4-20250514",
        "outcome": "success",
        "tokens": (68_500, 6_140, 104_200, 5_100),
        "cost": 2.78,
    },
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
        _ensure_demo_config(session)
        _sanitize_demo_profiles(session)
        project = _ensure_demo_project(session, source_path=source_path)

        # Fetch profile IDs in-session to avoid DetachedInstanceError
        profile_ids = [
            row[0] for row in session.execute(
                text("SELECT id FROM profiles ORDER BY name")
            ).fetchall()
        ]
        if not profile_ids:
            print("No profiles found after seeding.", file=sys.stderr)
            return

        tickets_by_status: dict[str, list[Ticket]] = {}

        for idx, raw_spec in enumerate(_TICKET_SPECS):
            spec = dict(raw_spec)  # copy to avoid mutating the module-level list
            run_spec = spec.pop("run", None)
            profile_id = profile_ids[idx % len(profile_ids)]

            ticket = create_ticket(
                session,
                title=spec["title"],
                prompt=spec["prompt"],
                status=spec["status"],
                profile_id=profile_id,
                project_id=project.id if idx % 2 == 0 else None,
                source_path=source_path,
                priority=idx % 3,
                next_run_context=(
                    "Prefer small, reviewable patches. Preserve existing CLI behavior."
                    if spec["status"] in {"queued", "review"}
                    else None
                ),
            )

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

        _seed_usage_history(session, transcript_root, source_path, tickets_by_status)

        # For the running ticket: insert a WorkerHeartbeat so the pill shows alive
        running_tickets = tickets_by_status.get("running", [])
        if running_tickets:
            hb = session.get(WorkerHeartbeat, 1)
            if hb is None:
                hb = WorkerHeartbeat(id=1, host="demo-host", pid=12345)
                session.add(hb)
            hb.host = "demo-host"
            hb.pid = 12345
            hb.last_seen_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            session.commit()

        _seed_dependencies(session, tickets_by_status)

    engine.dispose()


def _ensure_demo_config(session: Session) -> None:
    cfg = session.get(ConfigRow, 1)
    if cfg is None:
        cfg = ConfigRow(
            id=1,
            worktree_root="/demo/nightdesk-worktrees",
            transcript_root="/demo/nightdesk/transcripts",
            worktree_base_ref="main",
            claude_binary_path="/usr/local/bin/claude",
            polling_interval_seconds=5,
            max_parallel=3,
        )
        session.add(cfg)
    else:
        cfg.worktree_root = "/demo/nightdesk-worktrees"
        cfg.transcript_root = "/demo/nightdesk/transcripts"
        cfg.worktree_base_ref = "main"
        cfg.claude_binary_path = "/usr/local/bin/claude"
        cfg.polling_interval_seconds = 5
        cfg.max_parallel = 3
    session.commit()


def _sanitize_demo_profiles(session: Session) -> None:
    profiles = session.execute(text("SELECT id, name FROM profiles ORDER BY name")).fetchall()
    for idx, (profile_id, name) in enumerate(profiles):
        profile = session.get(Profile, profile_id)
        if profile is None:
            continue
        profile.claude_binary_path = None
        profile.fs_read = ["/demo/nightdesk"]
        profile.fs_write = [] if name == "Read only" else ["/demo/nightdesk"]
        if name == "Read only":
            profile.default_model = "claude-haiku-3.5-20241022"
        elif name == "Edit workspace":
            profile.default_model = "claude-sonnet-4-20250514"
        else:
            profile.default_model = "claude-opus-4-20250514"
        if idx == 0:
            profile.run_token_scopes = []
    session.commit()


def _ensure_demo_project(session: Session, *, source_path: str) -> Project:
    project = session.scalar(text("SELECT id FROM projects WHERE slug = 'nightdesk'"))
    if project is not None:
        existing = session.get(Project, project)
        if existing is not None:
            return existing

    return create_project(
        session,
        name="Nightdesk",
        slug="nightdesk",
        source_path=source_path,
        default_workspace_mode="git_worktree",
        default_worktree_name_template="nd-{slug}",
        default_base_ref="main",
        default_linked_workspaces=[{
            "role": "linked",
            "label": "docs",
            "kind": "directory",
            "access": "read_only",
            "source_path": str(Path(source_path) / "docs"),
        }],
        default_tool_paths=[str(Path(source_path) / "scripts")],
    )


def _seed_usage_history(
    session: Session,
    transcript_root: Path,
    source_path: str,
    tickets_by_status: dict[str, list[Ticket]],
) -> None:
    archived = tickets_by_status.get("archived", [])
    if not archived:
        return

    now = datetime.now(timezone.utc)
    profile_ids = [
        row[0] for row in session.execute(
            text("SELECT id FROM profiles ORDER BY name")
        ).fetchall()
    ]
    project_id = session.scalar(text("SELECT id FROM projects WHERE slug = 'nightdesk'"))
    for idx, spec in enumerate(_HISTORY_RUN_SPECS):
        ticket = create_ticket(
            session,
            title=spec["title"],
            prompt=spec["prompt"],
            status="archived",
            profile_id=profile_ids[idx % len(profile_ids)],
            project_id=project_id if idx % 3 != 1 else None,
            source_path=source_path,
            priority=idx % 3,
        )
        started = now - timedelta(days=idx, hours=(idx % 5) + 1)
        duration = timedelta(minutes=8 + (idx % 6) * 7)
        outcome = spec["outcome"]
        model = spec["model"]
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens = spec["tokens"]
        transcript_path = transcript_root / f"history-{idx:02d}.log"
        run = Run(
            ticket_id=ticket.id,
            started_at=started,
            finished_at=started + duration,
            exit_status=outcome,
            error_summary=("Synthetic demo failure" if outcome == "failed" else None),
            worktree_path=source_path,
            transcript_path=str(transcript_path),
            pid=None,
            host="demo-host",
            intent="retry" if idx % 4 == 0 else "first_run",
            model_used=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cost_usd=spec["cost"],
            session_id=f"sess-demo-history-{idx:02d}" if outcome == "success" else None,
        )
        session.add(run)
        session.flush()
        _write_transcript(
            transcript_path,
            run_id=run.id,
            ticket_id=ticket.id,
            variant="archived_success_short",
        )
    session.commit()


def _seed_dependencies(
    session: Session,
    tickets_by_status: dict[str, list[Ticket]],
) -> None:
    queued = tickets_by_status.get("queued", [])
    review = tickets_by_status.get("review", [])
    archived = tickets_by_status.get("archived", [])
    if len(queued) >= 2 and archived:
        add_dependency(session, queued[1].id, archived[0].id)
    if review and archived:
        add_dependency(session, review[0].id, archived[-1].id)


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
        "--source-path",
        default=DEFAULT_SOURCE_PATH,
        help=f"Workspace path shown in demo tickets (default: {DEFAULT_SOURCE_PATH})",
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

    source_path = str(args.source_path)

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
