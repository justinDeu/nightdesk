"""add config.notify_webhook_url

Revision ID: 0006_config_notify_webhook_url
Revises: 0005_run_prompt
Create Date: 2026-05-24

Additive: stores an optional webhook URL posted to on run completion (the
run-completion notifications feature). Nullable — pre-existing configs carry
NULL, which disables webhook delivery while desktop notifications still work.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_config_notify_webhook_url"
down_revision: Union[str, Sequence[str], None] = "0005_run_prompt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("config", sa.Column("notify_webhook_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("config", "notify_webhook_url")
