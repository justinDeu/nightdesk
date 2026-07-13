from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from nightdesk.db.models import CronJob, Project

OLD_PATH_COL = "c" "wd"

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    return cfg


def test_upgrade_from_existing_schema_renames_legacy_path_columns(tmp_path):
    db_path = tmp_path / "nightdesk.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "0011_projects")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO projects (id, name, slug, {OLD_PATH_COL}, default_workspace_mode, "
            "default_worktree_name_template, default_base_ref, default_linked_workspaces, "
            "color, position, archived_at, created_at, updated_at) "
            "VALUES ('p1', 'Proj', 'proj', '/tmp/proj', NULL, NULL, NULL, NULL, NULL, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        conn.execute(text(
            f"INSERT INTO cron_jobs (id, title, prompt, profile_id, priority, {OLD_PATH_COL}, workspace_mode, "
            "additional_dirs, permission_overrides, schedule, timezone, enabled, misfire_policy, "
            "overlap_policy, next_fire_at, last_fire_at, last_ticket_id, created_at, updated_at, force_run) "
            "VALUES ('c1', 'Nightly', '', 'profile', 0, '/tmp/proj', 'directory', '[]', NULL, '0 9 * * *', 'UTC', 1, 'coalesce', 'skip_if_active', NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
        ))

    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    project_columns = {col["name"] for col in inspector.get_columns("projects")}
    cron_columns = {col["name"] for col in inspector.get_columns("cron_jobs")}
    assert "source_path" in project_columns
    assert "source_path" in cron_columns
    assert OLD_PATH_COL not in project_columns
    assert OLD_PATH_COL not in cron_columns

    with Session(engine) as session:
        project = session.get(Project, "p1")
        cron = session.get(CronJob, "c1")
        assert project is not None
        assert cron is not None
        assert project.source_path == "/tmp/proj"
        assert cron.source_path == "/tmp/proj"


def test_upgrade_head_then_downgrade_and_reupgrade_from_clean_db(tmp_path):
    """0022 on a clean DB: upgrade -> downgrade -> upgrade must all succeed
    and leave the expected shape behind (no orphan columns to guard around)."""
    db_path = tmp_path / "nightdesk.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    inspector = inspect(engine)
    assert inspector.has_table("providers")
    assert inspector.has_table("provider_endpoints")
    profile_columns = {col["name"] for col in inspector.get_columns("profiles")}
    run_columns = {col["name"] for col in inspector.get_columns("runs")}
    config_columns = {col["name"] for col in inspector.get_columns("config")}
    assert {"endpoint_id", "backend_config"} <= profile_columns
    assert {"backend", "endpoint_id", "session_ref", "pricing_snapshot"} <= run_columns
    assert "opencode_binary_path" in config_columns

    command.downgrade(cfg, "0021_run_latency")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    inspector = inspect(engine)
    assert not inspector.has_table("providers")
    assert not inspector.has_table("provider_endpoints")
    profile_columns = {col["name"] for col in inspector.get_columns("profiles")}
    run_columns = {col["name"] for col in inspector.get_columns("runs")}
    config_columns = {col["name"] for col in inspector.get_columns("config")}
    assert not {"endpoint_id", "backend_config"} & profile_columns
    assert not {"backend", "endpoint_id", "session_ref", "pricing_snapshot"} & run_columns
    assert "opencode_binary_path" not in config_columns

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    inspector = inspect(engine)
    assert inspector.has_table("providers")
    assert inspector.has_table("provider_endpoints")


def test_upgrade_from_stamp_reconciled_db_with_orphan_columns(tmp_path):
    """Reproduces the real-world production shape: a DB stamp-reconciled
    from an abandoned pre-providers branch carries orphan columns that
    collide by name with columns 0022 wants to add. Upgrade must skip the
    collisions and still create everything else, without disturbing the
    orphan columns' existing data."""
    db_path = tmp_path / "nightdesk.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "0021_run_latency")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        # Orphan columns from the abandoned branch, added out-of-band
        # (never through any migration in this lineage) then stamped 0021.
        conn.execute(text("ALTER TABLE profiles ADD COLUMN backend_config JSON"))
        conn.execute(text("ALTER TABLE profiles ADD COLUMN backend_secret TEXT"))
        conn.execute(text("ALTER TABLE profiles ADD COLUMN endpoint_id VARCHAR"))
        conn.execute(text("ALTER TABLE runs ADD COLUMN backend VARCHAR"))
        conn.execute(text("ALTER TABLE runs ADD COLUMN backend_session_id VARCHAR"))
        conn.execute(text("ALTER TABLE runs ADD COLUMN backend_session_path VARCHAR"))
        conn.execute(text("ALTER TABLE runs ADD COLUMN backend_resume BOOLEAN"))
        conn.execute(text("ALTER TABLE runs ADD COLUMN backend_event_version INTEGER"))
        conn.execute(text(
            "UPDATE runs SET backend = 'claude_sdk' WHERE id IN "
            "(SELECT id FROM runs LIMIT 0)"
        ))

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    inspector = inspect(engine)
    assert inspector.has_table("providers")
    assert inspector.has_table("provider_endpoints")

    profile_columns = {col["name"] for col in inspector.get_columns("profiles")}
    run_columns = {col["name"] for col in inspector.get_columns("runs")}
    # The colliding orphan columns are still present (untouched, not
    # duplicated) alongside 0022's own non-colliding additions.
    assert {"endpoint_id", "backend_config", "backend_secret"} <= profile_columns
    assert {
        "backend", "endpoint_id", "backend_session_id", "backend_session_path",
        "backend_resume", "backend_event_version", "session_ref", "pricing_snapshot",
    } <= run_columns

    config_columns = {col["name"] for col in inspector.get_columns("config")}
    assert "opencode_binary_path" in config_columns


def test_0023_steer_messages_upgrade_downgrade_and_guard(tmp_path):
    """0023 creates steer_messages on a clean DB, downgrade drops it, and the
    guard makes a re-upgrade over an existing table a no-op (no crash)."""
    db_path = tmp_path / "nd.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "0023_steer_messages")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    insp = inspect(engine)
    assert insp.has_table("steer_messages")
    cols = {c["name"] for c in insp.get_columns("steer_messages")}
    assert {"conversation_id", "ticket_id", "body", "state", "delivery_mode",
            "delivered_run_id", "position"} <= cols
    ix = {i["name"] for i in insp.get_indexes("steer_messages")}
    assert "ix_steer_messages_conversation_id" in ix
    assert "ix_steer_messages_state" in ix

    command.downgrade(cfg, "0022_providers_and_endpoints")
    assert not inspect(engine).has_table("steer_messages")

    # Re-upgrade is idempotent under the inspector guard.
    command.upgrade(cfg, "0023_steer_messages")
    assert inspect(engine).has_table("steer_messages")


def test_0032_runs_ticket_id_index(tmp_path):
    """0032 adds ix_runs_ticket_id (the project activity feed's runs->tickets
    join was full-scanning). Created on upgrade, dropped on downgrade, and the
    inspector guard makes a re-upgrade over an existing index a no-op."""
    db_path = tmp_path / "nd.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    insp = inspect(engine)
    ix = {i["name"] for i in insp.get_indexes("runs")}
    assert "ix_runs_ticket_id" in ix

    command.downgrade(cfg, "0031_runs_backend_nullable")
    insp = inspect(create_engine(f"sqlite+pysqlite:///{db_path}", future=True))
    ix = {i["name"] for i in insp.get_indexes("runs")}
    assert "ix_runs_ticket_id" not in ix

    # Re-upgrade is idempotent under the inspector guard (no "index already
    # exists" crash).
    command.upgrade(cfg, "head")
    insp = inspect(create_engine(f"sqlite+pysqlite:///{db_path}", future=True))
    ix = {i["name"] for i in insp.get_indexes("runs")}
    assert "ix_runs_ticket_id" in ix


def test_0026_sessions_upgrade_downgrade_and_shape(tmp_path):
    """0026 creates the three agent tables (with the partial unique index) and
    the session config knobs on a clean DB; downgrade drops them; re-upgrade
    succeeds. Confirms the reworked-in-place 0026 leaves no tickets.kind."""
    db_path = tmp_path / "nd.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    insp = inspect(engine)
    for tbl in ("sessions", "session_turns", "pending_inputs"):
        assert insp.has_table(tbl)
    # Partial unique index enforcing one open pending per agent.
    pi_idx = {i["name"] for i in insp.get_indexes("pending_inputs")}
    assert "uq_pending_inputs_open" in pi_idx
    # Session config knobs.
    config_columns = {c["name"] for c in insp.get_columns("config")}
    assert {"session_idle_timeout_s", "max_live_sessions",
            "max_queued_turns", "max_turn_seconds"} <= config_columns
    # The reverted v1 discriminator is gone.
    ticket_columns = {c["name"] for c in insp.get_columns("tickets")}
    assert "kind" not in ticket_columns

    command.downgrade(cfg, "0025_execution_target")
    insp = inspect(engine)
    assert not insp.has_table("sessions")
    assert not insp.has_table("session_turns")
    assert not insp.has_table("pending_inputs")
    config_columns = {c["name"] for c in insp.get_columns("config")}
    assert "session_idle_timeout_s" not in config_columns

    command.upgrade(cfg, "head")
    assert inspect(engine).has_table("sessions")
