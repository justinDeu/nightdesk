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
