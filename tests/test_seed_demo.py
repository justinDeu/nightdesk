"""Tests for nightdesk-seed-demo."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from nightdesk.db.models import Base, Run, Ticket, WorkerHeartbeat
from nightdesk.domain.profiles import seed_default_profiles
from nightdesk.seed_demo import seed as seed_demo


@pytest.fixture
def demo_engine(tmp_path):
    """In-memory engine with full schema (including FTS triggers from migration)."""
    db_path = tmp_path / "demo.db"
    # Use alembic migrations for a real schema (includes FTS triggers)
    from nightdesk.seed_demo import run_migrations
    run_migrations(db_path)
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        future=True,
    )
    yield engine
    engine.dispose()


@pytest.fixture
def demo_session(demo_engine):
    with Session(demo_engine) as s:
        yield s


@pytest.fixture
def demo_transcript_root(tmp_path):
    return tmp_path / "transcripts"


def test_all_five_statuses_present(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    statuses = set(
        demo_session.execute(
            text("SELECT DISTINCT status FROM tickets")
        ).scalars()
    )
    # Inbox triage items are now seeded alongside the runnable lifecycle states.
    assert statuses == {"inbox", "draft", "queued", "running", "review", "archived"}


def test_projects_seeded_and_assigned(demo_engine, demo_session, demo_transcript_root, tmp_path):
    """Demo seeds projects and assigns tickets so group-by-project has content,
    while leaving some tickets unassigned for the No-project bucket."""
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    slugs = set(
        demo_session.execute(text("SELECT slug FROM projects")).scalars()
    )
    assert {"web-app", "platform"} <= slugs
    assigned = demo_session.execute(
        text("SELECT COUNT(*) FROM tickets WHERE project_id IS NOT NULL")
    ).scalar()
    unassigned = demo_session.execute(
        text("SELECT COUNT(*) FROM tickets WHERE project_id IS NULL")
    ).scalar()
    assert assigned > 0
    assert unassigned > 0


def test_profiles_seeded(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    names = set(
        demo_session.execute(
            text("SELECT name FROM profiles")
        ).scalars()
    )
    assert "Read only" in names
    assert "Edit workspace" in names
    assert "Full workspace" in names


def test_project_context_seeded(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    project = demo_session.execute(
        text(
            "SELECT id, default_workspace_mode, default_worktree_name_template "
            "FROM projects WHERE slug = 'nightdesk'"
        )
    ).first()
    assert project is not None
    project_id, mode, name_template = project
    assert mode == "git_worktree"
    assert name_template == "nd-{slug}"

    project_ticket_count = demo_session.execute(
        text("SELECT count(*) FROM tickets WHERE project_id = :project_id"),
        {"project_id": project_id},
    ).scalar_one()
    assert project_ticket_count >= 1

    linked_workspace_count = demo_session.execute(
        text(
            "SELECT count(*) FROM ticket_workspaces "
            "WHERE role = 'linked' AND label = 'docs' AND access = 'read_only'"
        )
    ).scalar_one()
    assert linked_workspace_count >= 1


def test_config_paths_are_demo_safe(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path="/demo/nightdesk",
    )
    cfg = demo_session.execute(
        text(
            "SELECT worktree_root, transcript_root, worktree_base_ref, claude_binary_path "
            "FROM config WHERE id = 1"
        )
    ).one()
    assert cfg == (
        "/demo/nightdesk-worktrees",
        "/demo/nightdesk/transcripts",
        "main",
        "/usr/local/bin/claude",
    )


def test_profile_paths_are_demo_safe(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path="/demo/nightdesk",
    )
    rows = demo_session.execute(
        text("SELECT fs_read, fs_write, claude_binary_path FROM profiles")
    ).fetchall()
    assert rows
    for fs_read, fs_write, claude_binary_path in rows:
        assert "/demo/nightdesk" in fs_read
        assert "/tmp/" not in str(fs_read)
        assert "/home/" not in str(fs_read)
        assert "/tmp/" not in str(fs_write)
        assert "/home/" not in str(fs_write)
        assert claude_binary_path is None


def test_dependencies_seeded(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    dependency_count = demo_session.execute(
        text("SELECT count(*) FROM ticket_dependencies")
    ).scalar_one()
    assert dependency_count >= 2


def test_usage_history_seeded_for_analytics(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    history = demo_session.execute(
        text(
            "SELECT count(*), count(DISTINCT model_used), sum(cost_usd), "
            "count(DISTINCT tickets.title) "
            "FROM runs JOIN tickets ON runs.ticket_id = tickets.id "
            "WHERE transcript_path LIKE '%history-%'"
        )
    ).one()
    count, model_count, cost, title_count = history
    assert count == 28
    assert model_count >= 3
    assert cost > 0
    assert title_count == count


def test_usage_history_has_non_cyclic_daily_shape(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    totals = [
        row[0] for row in demo_session.execute(
            text(
                "SELECT input_tokens + output_tokens + cache_read_tokens + cache_write_tokens AS total "
                "FROM runs WHERE transcript_path LIKE '%history-%' ORDER BY started_at DESC"
            )
        ).fetchall()
    ]
    assert len(totals) == 28
    assert len(set(totals)) == len(totals)
    first_half_deltas = [b - a for a, b in zip(totals[:13], totals[1:14])]
    second_half_deltas = [b - a for a, b in zip(totals[14:27], totals[15:28])]
    assert first_half_deltas != second_half_deltas


def test_running_ticket_has_unfinished_run(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    # There should be at least one run with finished_at IS NULL
    unfinished = demo_session.execute(
        text("SELECT id FROM runs WHERE finished_at IS NULL")
    ).scalars().all()
    assert len(unfinished) >= 1

    # The running ticket's current_run_id should point to an unfinished run
    running_tickets = demo_session.execute(
        text("SELECT id, current_run_id FROM tickets WHERE status = 'running'")
    ).fetchall()
    assert len(running_tickets) >= 1
    for tid, cur_run_id in running_tickets:
        assert cur_run_id is not None
        run = demo_session.get(Run, cur_run_id)
        assert run is not None
        assert run.finished_at is None


def test_worker_heartbeat_for_running_ticket(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    heartbeat = demo_session.execute(
        text("SELECT id, host FROM worker_heartbeat WHERE id = 1")
    ).one()
    assert heartbeat == (1, "demo-host")


def test_transcripts_written_and_parseable(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    runs = demo_session.execute(
        text("SELECT id, transcript_path FROM runs")
    ).fetchall()

    assert len(runs) >= 1

    for run_id, tpath in runs:
        p = Path(tpath)
        assert p.exists(), f"Transcript file missing: {p}"

        # Parse as NDJSON
        events = []
        with p.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                evt = json.loads(line)
                assert "type" in evt, f"Event missing 'type' key: {evt}"
                assert "ts" in evt, f"Event missing 'ts' key: {evt}"
                assert "seq" in evt, f"Event missing 'seq' key: {evt}"
                events.append(evt)

        assert len(events) >= 2, f"Transcript for run {run_id} has too few events"
        assert events[0]["type"] == "meta", "First event should be 'meta'"


def test_transcript_has_rich_events(demo_engine, demo_session, demo_transcript_root, tmp_path):
    """Check that success transcripts include the rich event types."""
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )

    # Find a success transcript (review_success or archived_success)
    success_runs = demo_session.execute(
        text(
            "SELECT r.id, r.transcript_path FROM runs r "
            "JOIN tickets t ON r.ticket_id = t.id "
            "WHERE r.exit_status = 'success' AND t.status IN ('review', 'archived') "
            "LIMIT 1"
        )
    ).fetchall()

    assert len(success_runs) >= 1, "No successful finished runs found"
    run_id, tpath = success_runs[0]
    p = Path(tpath)
    assert p.exists()

    events = []
    with p.open("r") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    event_types = {e["type"] for e in events}
    assert "meta" in event_types
    assert "assistant_text" in event_types
    assert "tool_use" in event_types
    assert "tool_result" in event_types
    assert "result" in event_types
    # At least one thinking event
    assert "thinking" in event_types


def test_failed_run_has_worker_error(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )

    failed_runs = demo_session.execute(
        text(
            "SELECT r.id, r.transcript_path FROM runs r "
            "WHERE r.exit_status = 'failed' LIMIT 1"
        )
    ).fetchall()

    assert len(failed_runs) >= 1
    run_id, tpath = failed_runs[0]
    events = []
    with Path(tpath).open("r") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    event_types = {e["type"] for e in events}
    assert "worker_error" in event_types


def test_cancelled_run_has_cancelled_event(demo_engine, demo_session, demo_transcript_root, tmp_path):
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )

    cancelled_runs = demo_session.execute(
        text(
            "SELECT r.id, r.transcript_path FROM runs r "
            "WHERE r.exit_status = 'cancelled' LIMIT 1"
        )
    ).fetchall()

    assert len(cancelled_runs) >= 1
    run_id, tpath = cancelled_runs[0]
    events = []
    with Path(tpath).open("r") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    event_types = {e["type"] for e in events}
    assert "cancelled" in event_types


def test_fts_populated(demo_engine, demo_session, demo_transcript_root, tmp_path):
    """The FTS trigger (from migration) should populate tickets_fts."""
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )

    # Search for "dark mode" (matches the draft ticket "Add dark mode toggle")
    hits = demo_session.execute(
        text(
            "SELECT t.id, t.title FROM tickets_fts fts "
            "JOIN tickets t ON t.rowid = fts.rowid "
            "WHERE tickets_fts MATCH :q"
        ),
        {"q": '"dark mode"'},
    ).fetchall()

    assert len(hits) >= 1
    titles = [row[1] for row in hits]
    assert any("dark mode" in t.lower() for t in titles)


def test_idempotent_second_run(demo_engine, demo_session, demo_transcript_root, tmp_path):
    """Running seed twice should not crash (profiles are idempotent, tickets accumulate)."""
    db_path = Path(demo_engine.url.database)
    seed_demo(db_path=db_path, transcript_root=demo_transcript_root, source_path=str(tmp_path))
    first_count = demo_session.execute(
        text("SELECT COUNT(*) FROM tickets")
    ).scalar()

    seed_demo(db_path=db_path, transcript_root=demo_transcript_root, source_path=str(tmp_path))
    second_count = demo_session.execute(
        text("SELECT COUNT(*) FROM tickets")
    ).scalar()

    # Second seed adds more tickets (it's additive, not truly idempotent
    # at the ticket level — but profiles are idempotent and it doesn't crash)
    assert second_count >= first_count


def test_labels_seeded_with_colors(demo_engine, demo_session, demo_transcript_root, tmp_path):
    """Demo seeds a rich set of labels with distinct colors."""
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    rows = demo_session.execute(
        text("SELECT name, color FROM labels ORDER BY name")
    ).fetchall()
    names = {r[0] for r in rows}
    # Core labels used across ticket specs
    assert {"backend", "ui/ux", "bug", "research", "infra", "docs", "cleanup", "feature"} <= names
    # Every label has a non-empty color
    for name, color in rows:
        assert color, f"Label {name!r} has no color"


def test_inbox_items_seeded(demo_engine, demo_session, demo_transcript_root, tmp_path):
    """Inbox items exist including incomplete (under-specified) ones."""
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    inbox = demo_session.execute(
        text("SELECT id, title FROM tickets WHERE status = 'inbox'")
    ).fetchall()
    assert len(inbox) >= 3, f"Expected ≥3 inbox items, got {len(inbox)}"
    # At least one inbox item should have NO workspaces (under-specified)
    incomplete = demo_session.execute(text(
        "SELECT COUNT(*) FROM tickets t "
        "LEFT JOIN ticket_workspaces tw ON tw.ticket_id = t.id "
        "WHERE t.status = 'inbox' AND tw.id IS NULL"
    )).scalar()
    assert incomplete >= 1, "Expected at least one incomplete inbox item"


def test_tickets_have_labels(demo_engine, demo_session, demo_transcript_root, tmp_path):
    """Multiple tickets are labelled so grouping/filtering demos have content."""
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    labelled = demo_session.execute(text(
        "SELECT COUNT(DISTINCT tl.ticket_id) FROM ticket_labels tl"
    )).scalar()
    assert labelled >= 5, f"Expected ≥5 labelled tickets, got {labelled}"


def test_bug_label_assigned_to_inbox_item(demo_engine, demo_session, demo_transcript_root, tmp_path):
    """The 'bug' label is created and assigned to the flaky-test inbox item."""
    seed_demo(
        db_path=Path(demo_engine.url.database),
        transcript_root=demo_transcript_root,
        source_path=str(tmp_path),
    )
    bug_label = demo_session.execute(text(
        "SELECT id FROM labels WHERE name = 'bug'"
    )).scalar()
    assert bug_label is not None, "'bug' label not seeded"
    # Check that some ticket has the bug label
    has_bug = demo_session.execute(text(
        "SELECT COUNT(*) FROM ticket_labels WHERE label_id = :lid"
    ), {"lid": bug_label}).scalar()
    assert has_bug >= 1, "No ticket assigned the 'bug' label"
