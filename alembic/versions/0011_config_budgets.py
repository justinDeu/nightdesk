"""add config.daily_budget_usd and config.monthly_budget_usd

Revision ID: 0011_config_budgets
Revises: 0010_cron_force_run
Create Date: 2026-05-24

Additive: budget guardrails for the cost dashboard. Both columns are nullable;
NULL means "unlimited" so pre-existing configs keep dispatching without a cap.
When set, the scheduler pauses normal picks once the day/month spend estimate
reaches the budget (run_now still bypasses).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011_config_budgets"
down_revision: Union[str, Sequence[str], None] = "0010_cron_force_run"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("config", sa.Column("daily_budget_usd", sa.Float(), nullable=True))
    op.add_column("config", sa.Column("monthly_budget_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("config", "monthly_budget_usd")
    op.drop_column("config", "daily_budget_usd")
