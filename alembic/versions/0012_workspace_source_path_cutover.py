"""cut over legacy path columns to workspace-first schema

Revision ID: 0012_workspace_source_path_cutover
Revises: 0011_projects
Create Date: 2026-05-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0012_workspace_source_path_cutover"
down_revision: Union[str, Sequence[str], None] = "0011_projects"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_PATH_COL = "c" "wd"


def _recreate_ticket_fts(bind) -> None:
    bind.execute(sa.text("DROP TRIGGER IF EXISTS tickets_au"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS tickets_ad"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS tickets_ai"))
    bind.execute(sa.text("DELETE FROM tickets_fts"))
    bind.execute(sa.text(
        "INSERT INTO tickets_fts(rowid, title, prompt, id) "
        "SELECT rowid, title, prompt, id FROM tickets"
    ))
    bind.execute(sa.text(
        "CREATE TRIGGER tickets_ai AFTER INSERT ON tickets BEGIN "
        "INSERT INTO tickets_fts(rowid, title, prompt, id) "
        "VALUES (new.rowid, new.title, new.prompt, new.id); END;"
    ))
    bind.execute(sa.text(
        "CREATE TRIGGER tickets_ad AFTER DELETE ON tickets BEGIN "
        "DELETE FROM tickets_fts WHERE rowid = old.rowid; END;"
    ))
    bind.execute(sa.text(
        "CREATE TRIGGER tickets_au AFTER UPDATE ON tickets BEGIN "
        "DELETE FROM tickets_fts WHERE rowid = old.rowid; "
        "INSERT INTO tickets_fts(rowid, title, prompt, id) "
        "VALUES (new.rowid, new.title, new.prompt, new.id); END;"
    ))


def _primary_workspace_insert_sql() -> sa.TextClause:
    return sa.text(
        f"""
        INSERT INTO ticket_workspaces (
            id,
            ticket_id,
            run_id,
            role,
            label,
            kind,
            access,
            source_path,
            retention,
            state,
            position,
            created_at,
            updated_at
        )
        SELECT
            lower(hex(randomblob(16))),
            tickets.id,
            NULL,
            'primary',
            'primary',
            CASE
                WHEN tickets.workspace_mode = 'in_place' THEN 'directory'
                WHEN tickets.workspace_mode = 'worktree' THEN 'git_worktree'
                ELSE tickets.workspace_mode
            END,
            'read_write',
            tickets.{OLD_PATH_COL},
            'preserve',
            'pending',
            0,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM tickets
        WHERE NOT EXISTS (
            SELECT 1
            FROM ticket_workspaces tw
            WHERE tw.ticket_id = tickets.id
              AND tw.role = 'primary'
        )
        """
    )


def upgrade() -> None:
    op.execute(_primary_workspace_insert_sql())

    with op.batch_alter_table("projects") as batch:
        batch.alter_column(
            OLD_PATH_COL,
            new_column_name="source_path",
            existing_type=sa.String(),
            existing_nullable=False,
        )

    with op.batch_alter_table("cron_jobs") as batch:
        batch.alter_column(
            OLD_PATH_COL,
            new_column_name="source_path",
            existing_type=sa.String(),
            existing_nullable=False,
        )

    with op.batch_alter_table("tickets") as batch:
        batch.drop_column("workspace_mode")
        batch.drop_column(OLD_PATH_COL)

    _recreate_ticket_fts(op.get_bind())

def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch:
        batch.add_column(sa.Column(OLD_PATH_COL, sa.String(), nullable=True))
        batch.add_column(sa.Column("workspace_mode", sa.String(), nullable=True))

    op.execute(sa.text(
        f"""
        UPDATE tickets
        SET {OLD_PATH_COL} = (
                SELECT tw.source_path
                FROM ticket_workspaces tw
                WHERE tw.ticket_id = tickets.id
                  AND tw.role = 'primary'
                ORDER BY tw.position
                LIMIT 1
            ),
            workspace_mode = (
                SELECT CASE
                    WHEN tw.kind = 'directory' THEN 'in_place'
                    WHEN tw.kind = 'git_worktree' THEN 'git_worktree'
                    ELSE tw.kind
                END
                FROM ticket_workspaces tw
                WHERE tw.ticket_id = tickets.id
                  AND tw.role = 'primary'
                ORDER BY tw.position
                LIMIT 1
            )
        """
    ))

    with op.batch_alter_table("tickets") as batch:
        batch.alter_column(OLD_PATH_COL, existing_type=sa.String(), nullable=False)
        batch.alter_column("workspace_mode", existing_type=sa.String(), nullable=False)
    _recreate_ticket_fts(op.get_bind())

    with op.batch_alter_table("cron_jobs") as batch:
        batch.alter_column(
            "source_path",
            new_column_name=OLD_PATH_COL,
            existing_type=sa.String(),
            existing_nullable=False,
        )

    with op.batch_alter_table("projects") as batch:
        batch.alter_column(
            "source_path",
            new_column_name=OLD_PATH_COL,
            existing_type=sa.String(),
            existing_nullable=False,
        )
