"""normalize ticket priorities to the fixed 0..4 scale

Revision ID: 0016_normalize_priority_scale
Revises: 0015_labels
Create Date: 2026-06-07
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0016_normalize_priority_scale"
down_revision: Union[str, Sequence[str], None] = "0015_labels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Queue order lives in Ticket.position. Priority is now only the fixed 0..4
    # metadata scale, so legacy rank-like values reset to No priority.
    op.execute("UPDATE tickets SET priority = 0 WHERE priority < 0 OR priority > 4")
    op.execute("UPDATE cron_jobs SET priority = 0 WHERE priority < 0 OR priority > 4")


def downgrade() -> None:
    # Intentional no-op: legacy priority rank values cannot be reconstructed
    # once they have been clamped to the 0..4 scale.  Downgrading this revision
    # leaves the data as-is — tickets retain their (now normalised) 0..4 values
    # rather than regaining meaningless large integers.
    pass
