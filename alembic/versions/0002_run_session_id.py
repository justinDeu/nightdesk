"""add runs.session_id

Revision ID: 0002_run_session_id
Revises: 0001_initial
Create Date: 2026-05-20

Additive: stores the Claude session id reported by the SDK at run completion
so a finished run can be resumed (`claude --resume <session_id>` / SDK
resume=). Nullable — pre-existing runs and runs where the SDK reported no
session id simply carry NULL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_run_session_id"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("session_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "session_id")
