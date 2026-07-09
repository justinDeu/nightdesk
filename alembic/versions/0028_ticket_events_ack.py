"""ticket_events log + acknowledged flag (post-review acknowledgement flow)

Revision ID: 0028_ticket_events_ack
Revises: 0027_api_tokens
Create Date: 2026-07-08

Layers 0 + 1 of ``docs/design/post-review-acknowledge-flow.md``.

Layer 0 — ``ticket_events``: an append-only transition log with the acting
principal, written at the single choke point every status change funnels
through (``domain.tickets.transition_with_position``). It powers the
who-archived-this answer, the digest's real archived-at timestamp (instead of
leaning on ``tickets.updated_at``), and the review-column agent-reviewed chip.
``actor_kind`` is ``admin`` | ``run`` | ``token``; ``token`` is reserved for the
sibling scoped-agent-token work and is never written on this branch.

Layer 1 — ``tickets.acknowledged_at`` / ``acknowledged_by``: "a human has seen
this ticket's current outcome". Human-initiated transitions auto-ack; run/agent
transitions never do; re-entering an active state clears it. Nothing is
backfilled — pre-existing archived/review tickets start unacknowledged and land
in the digest, which is the intended "here is everything you never explicitly
saw" behavior on first upgrade.

Re-chained at final integration: ``down_revision`` now points at
``0027_api_tokens`` (token-perms), which chains from ``0026_sessions``
(resident-agents' in-place rework of the abandoned ``0026_session_kind``).
The linear chain is 0026_sessions -> 0027_api_tokens -> 0028_ticket_events_ack
-> 0029_integrations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0028_ticket_events_ack"
down_revision: Union[str, Sequence[str], None] = "0027_api_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "ticket_id",
            sa.String(),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), nullable=False),
        # admin | run | token
        sa.Column("actor_kind", sa.String(), nullable=False),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=True,
        ),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_ticket_events_ticket_id", "ticket_events", ["ticket_id"],
    )
    # The digest/archived-at query filters events by (ticket, to_status) and
    # orders by time, so index the pair plus the timestamp.
    op.create_index(
        "ix_ticket_events_ticket_to_status",
        "ticket_events",
        ["ticket_id", "to_status", "created_at"],
    )

    op.add_column(
        "tickets",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("acknowledged_by", sa.String(), nullable=True),
    )
    # Human-facing what/why, split from ``prompt`` (the agent instructions that
    # actually run). Nullable, no backfill, no mirroring between the two fields.
    # Never injected into the agent's context — see the run-prompt builder.
    op.add_column(
        "tickets",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tickets", "description")
    op.drop_column("tickets", "acknowledged_by")
    op.drop_column("tickets", "acknowledged_at")
    op.drop_index("ix_ticket_events_ticket_to_status", table_name="ticket_events")
    op.drop_index("ix_ticket_events_ticket_id", table_name="ticket_events")
    op.drop_table("ticket_events")
