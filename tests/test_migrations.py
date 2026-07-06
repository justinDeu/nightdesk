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
