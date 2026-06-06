"""record each run's start commit on its workspace

The run diff must show only what THIS run changed (its own commits plus
uncommitted working-tree changes), not the whole branch versus its target.
That requires capturing the worktree's HEAD at the moment the run starts.

Revision ID: 0014_workspace_run_start_sha
Revises: 0013_toolchain_context
Create Date: 2026-06-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0014_workspace_run_start_sha"
down_revision: Union[str, Sequence[str], None] = "0013_toolchain_context"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("ticket_workspaces") as batch:
        batch.add_column(sa.Column("run_start_sha", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ticket_workspaces") as batch:
        batch.drop_column("run_start_sha")
