"""add labels and ticket_labels tables

Revision ID: 0015_labels
Revises: 0014_workspace_run_start_sha
Create Date: 2026-06-06

The labels UX foundation (ux/labels-ui) added the ``Label`` model and the
``ticket_labels`` association but shipped without a migration. Production
builds its schema with ``alembic upgrade head``, so without this revision the
labels feature has no tables at runtime. This backfills the schema.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015_labels"
down_revision: Union[str, Sequence[str], None] = "0014_workspace_run_start_sha"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "labels",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("color", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "ticket_labels",
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("label_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["label_id"], ["labels.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("ticket_id", "label_id"),
    )
    op.create_index("ix_ticket_labels_label_id", "ticket_labels", ["label_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_labels_label_id", table_name="ticket_labels")
    op.drop_table("ticket_labels")
    op.drop_table("labels")
