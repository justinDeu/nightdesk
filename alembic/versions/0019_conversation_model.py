"""Conversation model: conversations table + runs/ticket_workspaces FKs

Revision ID: 0019_conversation_model
Revises: 0018_saved_views
Create Date: 2026-06-30

Introduces the Conversation as the primary unit of work. A Ticket owns many
Conversations (1:N, ordered); exactly one is active (tickets.current_conversation_id).
A Conversation owns many Turns (the existing runs rows, kept in place) and the
authoritative resume handle (session_id, lifted off Run). TicketWorkspace gains
conversation_id so a conversation owns its workspaces.

Backfill (developer-local DB, effectively empty — best-effort): for each ticket
that has runs, materialize ONE conversation from its runs ordered by started_at,
set it current, backfill runs.conversation_id + position, and copy the
authoritative session_id / cumulative cost from the latest run. Tickets with no
runs get a null current_conversation_id. parent_run_id is intentionally kept
(not dropped) so existing transcript paths, run logs, and RunToken.run_id
survive with zero migration risk; its consumers are migrated off it in code.
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "0019_conversation_model"
down_revision: Union[str, Sequence[str], None] = "0018_saved_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("profile_id", sa.String(), nullable=True),
        sa.Column("backend", sa.String(), nullable=False, server_default="claude_sdk"),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transcript_path", sa.String(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("model_used", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_ticket_id", "conversations", ["ticket_id"])

    op.add_column(
        "runs",
        sa.Column("conversation_id", sa.String(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_runs_conversation_id", "runs", ["conversation_id"])

    op.add_column(
        "ticket_workspaces",
        sa.Column("conversation_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_ticket_workspaces_conversation_id", "ticket_workspaces", ["conversation_id"],
    )

    op.add_column(
        "tickets",
        sa.Column("current_conversation_id", sa.String(), nullable=True),
    )

    # Note: no DB-level FK constraints are added — SQLite cannot ALTER ADD
    # CONSTRAINT, and SQLite FK enforcement is off by default. The ORM models
    # declare these FKs, so create_all (used by the in-memory test DB) gets
    # them; migrated SQLite DBs carry the columns as logical FKs.

    _backfill_conversations()


def _backfill_conversations() -> None:
    """Materialize one conversation per ticket-from-runs, best-effort.

    Idempotent-ish: skips tickets that already have a current_conversation_id
    or whose runs are already bound to a conversation.
    """
    bind = op.get_bind()
    has_runs = bind.execute(sa.text("SELECT count(*) FROM runs")).scalar()
    if not has_runs:
        return

    tickets = bind.execute(sa.text(
        "SELECT id, profile_id FROM tickets "
        "WHERE current_conversation_id IS NULL "
        "AND EXISTS (SELECT 1 FROM runs WHERE runs.ticket_id = tickets.id)"
    )).fetchall()

    for t_id, profile_id in tickets:
        runs = bind.execute(sa.text(
            "SELECT id, started_at, session_id, transcript_path, cost_usd, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
            "model_used, intent FROM runs WHERE ticket_id = :tid "
            "ORDER BY started_at ASC"
        ), {"tid": t_id}).fetchall()
        if not runs:
            continue

        conv_id = str(uuid.uuid4())
        latest = runs[-1]
        # session_id: best-effort from the last run that had one.
        session_id = None
        for r in reversed(runs):
            if r[2]:
                session_id = r[2]
                break
        started_at = runs[0][1]
        transcript_path = latest[3] or f"{conv_id}.log"
        bind.execute(sa.text(
            "INSERT INTO conversations (id, ticket_id, profile_id, backend, "
            "session_id, status, started_at, finished_at, position, transcript_path, "
            "cost_usd, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, model_used, created_at, updated_at) VALUES "
            "(:id, :tid, :pid, 'claude_sdk', :sid, 'done', :st, :ft, 0, :tp, "
            ":cost, :it, :ot, :cr, :cw, :model, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {
            "id": conv_id, "tid": t_id, "pid": profile_id, "sid": session_id,
            "st": started_at, "ft": latest[1] if latest[1] else started_at,
            "tp": transcript_path, "cost": latest[4], "it": latest[5],
            "ot": latest[6], "cr": latest[7], "cw": latest[8], "model": latest[9],
        })
        for pos, r in enumerate(runs):
            bind.execute(sa.text(
                "UPDATE runs SET conversation_id = :cid, position = :pos WHERE id = :rid"
            ), {"cid": conv_id, "pos": pos, "rid": r[0]})
        bind.execute(sa.text(
            "UPDATE tickets SET current_conversation_id = :cid WHERE id = :tid"
        ), {"cid": conv_id, "tid": t_id})


def downgrade() -> None:
    op.drop_column("tickets", "current_conversation_id")
    op.drop_index("ix_ticket_workspaces_conversation_id", table_name="ticket_workspaces")
    op.drop_column("ticket_workspaces", "conversation_id")
    op.drop_index("ix_runs_conversation_id", table_name="runs")
    op.drop_column("runs", "position")
    op.drop_column("runs", "conversation_id")
    op.drop_index("ix_conversations_ticket_id", table_name="conversations")
    op.drop_table("conversations")
