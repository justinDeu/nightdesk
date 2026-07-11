"""sessions.last_seen_at — unread-reply tracking for resident agents

Revision ID: 0030_session_seen
Revises: 0029_integrations
Create Date: 2026-07-11

One nullable column: the high-water mark of what the human has seen of an
agent's conversation. NULL = never viewed (or explicitly marked unread). A
session is "unread" when its newest completed user turn finished after this
stamp — that feeds GET /agents/attention (nav badge + Desk band) alongside the
existing structured pending-input rows. No backfill: existing sessions start
with a NULL stamp, so any past reply shows as unread once on upgrade, which is
the honest reading ("you never explicitly saw this").
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0030_session_seen"
down_revision: Union[str, Sequence[str], None] = "0029_integrations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "last_seen_at")
