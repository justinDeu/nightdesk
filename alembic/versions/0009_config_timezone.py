"""add config.schedule_timezone

Revision ID: 0009_config_timezone
Revises: 0008_cron_jobs
Create Date: 2026-05-25

Adds a global IANA timezone used to evaluate ScheduleWindow rows in local
wall-clock time (fixes day_mask being evaluated against the UTC weekday).
Default "UTC" preserves existing behavior; existing window rows keep their
stored HH:MM values and a non-UTC user re-saves once in the new settings UI.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009_config_timezone"
down_revision: Union[str, Sequence[str], None] = "0008_cron_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "config",
        sa.Column("schedule_timezone", sa.String(), nullable=False, server_default="UTC"),
    )


def downgrade() -> None:
    op.drop_column("config", "schedule_timezone")
