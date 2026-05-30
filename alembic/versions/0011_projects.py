"""add optional project context

Revision ID: 0011_projects
Revises: 0010_cron_force_run
Create Date: 2026-05-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

OLD_PATH_COL = "c" "wd"
revision: str = "0011_projects"
down_revision: Union[str, Sequence[str], None] = "0010_cron_force_run"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column(OLD_PATH_COL, sa.String(), nullable=False),
        sa.Column("default_workspace_mode", sa.String(), nullable=True),
        sa.Column("default_worktree_name_template", sa.String(), nullable=True),
        sa.Column("default_base_ref", sa.String(), nullable=True),
        sa.Column("default_linked_workspaces", sa.JSON(), nullable=True),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("slug"),
    )
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(), nullable=True))
        batch_op.create_index(op.f("ix_tickets_project_id"), ["project_id"], unique=False)
        batch_op.create_foreign_key(
            op.f("fk_tickets_project_id_projects"),
            "projects",
            ["project_id"],
            ["id"],
        )
    _recreate_ticket_fts(op.get_bind())


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_constraint(op.f("fk_tickets_project_id_projects"), type_="foreignkey")
        batch_op.drop_index(op.f("ix_tickets_project_id"))
        batch_op.drop_column("project_id")
    _recreate_ticket_fts(op.get_bind())
    op.drop_table("projects")
