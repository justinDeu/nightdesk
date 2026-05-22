"""add config.worktree_base_ref

Revision ID: 0003_config_worktree_base_ref
Revises: 0002_run_session_id
Create Date: 2026-05-22

Additive: stores a global default base ref for git_worktree tickets. When set,
new tickets created without an explicit base_ref branch from this ref instead
of HEAD. Nullable — pre-existing configs and the "no global default" case carry
NULL and fall back to HEAD.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_config_worktree_base_ref"
down_revision: Union[str, Sequence[str], None] = "0002_run_session_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("config", sa.Column("worktree_base_ref", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("config", "worktree_base_ref")
