"""add ticket.commit_on_finish

Revision ID: 0020_ticket_commit_on_finish
Revises: 0019_conversation_model
Create Date: 2026-07-03

Additive opt-in per-ticket flag. When true, on a successful run the worker
auto-commits the ticket's working-tree changes onto its git_worktree branch so
that dependent (stacked) tickets whose ``base_ref`` points at that branch
actually receive this ticket's work. Without it, runs leave work uncommitted
and the branch ref never advances past the base commit, so ``base_ref``
stacking silently provisions the dependent from an empty prerequisite.

Nullable; pre-existing tickets and the default ("off") carry NULL/false.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0020_ticket_commit_on_finish"
down_revision: Union[str, Sequence[str], None] = "0019_conversation_model"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("commit_on_finish", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "commit_on_finish")
