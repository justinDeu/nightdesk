"""add runs.prompt

Revision ID: 0005_run_prompt
Revises: 0004_ticket_dependencies
Create Date: 2026-05-24

Additive: stores the base prompt (ticket.prompt at launch time) on each run
so the transcript panel can display what each run actually used instead of the
current ticket-level prompt. Nullable — runs created before this column carry
NULL and fall back to ticket.prompt at display time.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_run_prompt"
down_revision: Union[str, Sequence[str], None] = "0004_ticket_dependencies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "prompt")
