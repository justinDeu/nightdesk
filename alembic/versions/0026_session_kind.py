"""add ticket.kind (session vs board ticket)

Revision ID: 0026_session_kind
Revises: 0022_providers_and_endpoints
Create Date: 2026-07-08

Additive discriminator column for interactive (ticketless) sessions. A session
is a ``Ticket`` with ``kind='session'``: it reuses the entire run pipeline,
conversation model, transcript streaming, run tokens, and continue/resume
semantics unchanged, and is only excluded from the board / inbox / analytics /
search surfaces via ``kind`` filters.

``server_default='ticket'`` backfills every pre-existing row as a normal board
ticket. Indexed because the exclusion filter (``kind == 'ticket'``) rides on
almost every ticket-list query.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0026_session_kind"
down_revision: Union[str, Sequence[str], None] = "0025_execution_target"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("kind", sa.String(), nullable=False, server_default="ticket"),
    )
    op.create_index("ix_tickets_kind", "tickets", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_tickets_kind", table_name="tickets")
    op.drop_column("tickets", "kind")
