"""add ticket_dependencies table

Revision ID: 0004_ticket_dependencies
Revises: 0003_config_worktree_base_ref
Create Date: 2026-05-24

Additive: new table for run-after-success chaining. Each row is a directed
edge (ticket_id -> depends_on_id). A dependency is satisfied when the
upstream ticket's most-recent run has exit_status='success' AND the upstream
is in 'review' or 'archived'. Unique constraint prevents duplicate edges.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_ticket_dependencies"
down_revision: Union[str, Sequence[str], None] = "0003_config_worktree_base_ref"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ticket_dependencies",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ticket_id", sa.String(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("depends_on_id", sa.String(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ticket_id", "depends_on_id", name="uq_ticket_dep_pair"),
    )


def downgrade() -> None:
    op.drop_table("ticket_dependencies")
