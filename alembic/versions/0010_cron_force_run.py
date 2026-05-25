"""add cron_jobs.force_run

Revision ID: 0010_cron_force_run
Revises: 0009_config_timezone
Create Date: 2026-05-25

Adds a per-cron-job flag. When set, the job materializes ``run_now=True``
tickets, which the scheduler dispatches unconditionally — past a full queue and
outside the active-hours window. Default false preserves existing behavior
(generated tickets wait for the normal window + capacity).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010_cron_force_run"
down_revision: Union[str, Sequence[str], None] = "0009_config_timezone"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cron_jobs",
        sa.Column("force_run", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("cron_jobs", "force_run")
