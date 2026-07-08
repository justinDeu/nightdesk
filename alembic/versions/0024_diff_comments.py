"""diff_comments: line-anchored review comments on run diffs

Revision ID: 0024_diff_comments
Revises: 0022_providers_and_endpoints
Create Date: 2026-07-07

Adds the ``diff_comments`` table: review comments anchored to a line of a run's
diff, with one-level threading via a nullable self-FK ``parent_id`` (root =
anchor + resolution state; reply = body only). Anchors store ``anchor_head_sha``
+ ``anchor_text`` so a thread can be marked *outdated* (rendered against its
captured text) once a later run advances the worktree head. Additive; the
downgrade drops the table.

``down_revision`` is ``0022_providers_and_endpoints`` (the current head on this
branch); integration re-parents it when linearizing after 0023.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0024_diff_comments"
down_revision: Union[str, Sequence[str], None] = "0023_steer_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "diff_comments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("file_path", sa.String(), nullable=True),
        sa.Column("side", sa.String(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("anchor_head_sha", sa.String(), nullable=True),
        sa.Column("anchor_text", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_kind", sa.String(), nullable=False, server_default="admin"),
        sa.Column("author_run_id", sa.String(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["diff_comments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_diff_comments_run_id", "diff_comments", ["run_id"])
    op.create_index("ix_diff_comments_ticket_id", "diff_comments", ["ticket_id"])
    op.create_index("ix_diff_comments_conversation_id", "diff_comments", ["conversation_id"])
    op.create_index("ix_diff_comments_parent_id", "diff_comments", ["parent_id"])


def downgrade() -> None:
    op.drop_index("ix_diff_comments_parent_id", table_name="diff_comments")
    op.drop_index("ix_diff_comments_conversation_id", table_name="diff_comments")
    op.drop_index("ix_diff_comments_ticket_id", table_name="diff_comments")
    op.drop_index("ix_diff_comments_run_id", table_name="diff_comments")
    op.drop_table("diff_comments")
