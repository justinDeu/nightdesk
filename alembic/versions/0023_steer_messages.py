"""mid-run steering: steer_messages queue

Revision ID: 0023_steer_messages
Revises: 0022_providers_and_endpoints
Create Date: 2026-07-07

Introduces the ``steer_messages`` table described in
docs/design/session-suite/mid-run-steering.md: a per-Conversation queue of
follow-ups the user types while a run is live, delivered into the same run
(inject-capable backends) or the next turn (queue-only backends).

Guard-based upgrade/downgrade (inspector check before create/drop) matching
the repo's migration style, so the revision is correct on a clean DB and a
no-op on a DB that already carries the table. Indexes cover the columns the
watcher and the API query on: conversation_id, ticket_id, state, created_at.

This is a purely additive revision on a table nothing else references; it
linearizes cleanly after 0022 and against the other session-suite feature
migrations at master merge.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0023_steer_messages"
down_revision: Union[str, Sequence[str], None] = "0022_providers_and_endpoints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "steer_messages"):
        op.create_table(
            "steer_messages",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("ticket_id", sa.String(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("state", sa.String(), nullable=False, server_default="pending"),
            sa.Column("delivery_mode", sa.String(), nullable=False, server_default="at_turn"),
            sa.Column("delivered_run_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["delivered_run_id"], ["runs.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_steer_messages_conversation_id", "steer_messages", ["conversation_id"],
        )
        op.create_index("ix_steer_messages_ticket_id", "steer_messages", ["ticket_id"])
        op.create_index("ix_steer_messages_state", "steer_messages", ["state"])
        op.create_index("ix_steer_messages_created_at", "steer_messages", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "steer_messages"):
        existing = {ix["name"] for ix in inspector.get_indexes("steer_messages")}
        for name in (
            "ix_steer_messages_created_at",
            "ix_steer_messages_state",
            "ix_steer_messages_ticket_id",
            "ix_steer_messages_conversation_id",
        ):
            if name in existing:
                op.drop_index(name, table_name="steer_messages")
        op.drop_table("steer_messages")
