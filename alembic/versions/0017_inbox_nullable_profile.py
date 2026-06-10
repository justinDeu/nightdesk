"""Allow inbox tickets to be created without a profile (profile_id nullable)

Revision ID: 0017_inbox_nullable_profile
Revises: 0016_normalize_priority_scale
Create Date: 2026-06-09

The inbox is a capture queue for under-specified work. Requiring a profile
before an item can even be captured creates friction and contradicts the
"capture now, triage later" intent. Promotion to the runnable board still
enforces profile completeness (ticket_completeness / IncompleteTicket).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0017_inbox_nullable_profile"
down_revision: Union[str, Sequence[str], None] = "0016_normalize_priority_scale"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _recreate_ticket_fts(bind) -> None:
    # batch_alter_table rewrites the tickets table; SQLite drops the FTS sync
    # triggers (tickets_ai/tickets_ad/tickets_au) along with the old table,
    # and the rewrite can change the rowids tickets_fts references. Recreate
    # the triggers and rebuild the index, exactly as 0011_projects and
    # 0012_workspace_source_path_cutover do after their tickets rewrites.
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
    # SQLite does not support ALTER COLUMN directly; batch mode rewrites the
    # table and is safe for the typical single-writer setup nightdesk targets.
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.alter_column("profile_id", nullable=True)
    _recreate_ticket_fts(op.get_bind())


def downgrade() -> None:
    # Re-tighten the constraint.  Any inbox tickets lacking a profile will
    # cause this to fail on databases that enforce NOT NULL at the DDL level
    # (SQLite in strict mode, PostgreSQL).  Callers must NULL-clean first.
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.alter_column("profile_id", nullable=False)
    _recreate_ticket_fts(op.get_bind())
