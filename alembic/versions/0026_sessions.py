"""resident interactive agents: sessions / session_turns / pending_inputs

Revision ID: 0026_sessions
Revises: 0025_execution_target
Create Date: 2026-07-08

Reworks the abandoned ``0026_session_kind`` in place (no prod DB shipped the
kind discriminator). v3 makes interactive agents ("Agents" in the UI) their own
owned tables, fully decoupled from tickets — see
``docs/design/session-suite/resident-agents-v3.md``.

Three tables:

- ``sessions`` — one resident agent. Liveness is derived from ``host_pid`` +
  turn/pending state; ``status`` is a coarse persisted hint.
- ``session_turns`` — the inbox AND per-turn record (user / interrupt / answer).
- ``pending_inputs`` — one open human-input request, guarded by a partial
  unique index so at most one is ``pending`` per agent.

Also drops ``tickets.kind`` (the reverted v1 discriminator) and adds the four
mutable session config knobs to ``config``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0026_sessions"
down_revision: Union[str, Sequence[str], None] = "0025_execution_target"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False, server_default="Agent"),
        sa.Column("profile_id", sa.String(), sa.ForeignKey("profiles.id"), nullable=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("backend", sa.String(), nullable=False, server_default="claude"),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="idle"),
        sa.Column("workspace_kind", sa.String(), nullable=False, server_default="directory"),
        sa.Column("workspace_access", sa.String(), nullable=False, server_default="read_write"),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("posture", sa.String(), nullable=False, server_default="trusted"),
        sa.Column("host", sa.String(), nullable=True),
        sa.Column("host_pid", sa.Integer(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_timeout_s", sa.Integer(), nullable=True),
        sa.Column("resume_handle", sa.JSON(), nullable=True),
        sa.Column("env", sa.JSON(), nullable=True),
        sa.Column("transcript_path", sa.String(), nullable=False),
        sa.Column("pricing_snapshot", sa.JSON(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sessions_project_id", "sessions", ["project_id"])
    op.create_index("ix_sessions_status", "sessions", ["status"])
    op.create_index("ix_sessions_created_at", "sessions", ["created_at"])

    op.create_table(
        "session_turns",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "session_id", sa.String(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("ref_request_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("model_used", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("includes_resume", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_session_turns_session_id", "session_turns", ["session_id"])
    op.create_index("ix_session_turns_status", "session_turns", ["status"])
    op.create_index("ix_session_turns_created_at", "session_turns", ["created_at"])

    op.create_table(
        "pending_inputs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "session_id", sa.String(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("tool", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("answer", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pending_inputs_session_id", "pending_inputs", ["session_id"])
    op.create_index("ix_pending_inputs_status", "pending_inputs", ["status"])
    # At most one OPEN request per agent: a buggy double-emit cannot create two.
    op.create_index(
        "uq_pending_inputs_open",
        "pending_inputs",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Note: the abandoned v1 ``tickets.kind`` discriminator is reverted by
    # reworking this revision in place — the column is simply never created, so
    # there is nothing to drop here (the 0026_session_kind migration was removed
    # and Ticket.kind is gone from the model).

    # Mutable session config knobs.
    with op.batch_alter_table("config") as batch:
        batch.add_column(sa.Column(
            "session_idle_timeout_s", sa.Integer(), nullable=False, server_default="300"))
        batch.add_column(sa.Column(
            "max_live_sessions", sa.Integer(), nullable=False, server_default="4"))
        batch.add_column(sa.Column(
            "max_queued_turns", sa.Integer(), nullable=False, server_default="20"))
        batch.add_column(sa.Column(
            "max_turn_seconds", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("config") as batch:
        batch.drop_column("max_turn_seconds")
        batch.drop_column("max_queued_turns")
        batch.drop_column("max_live_sessions")
        batch.drop_column("session_idle_timeout_s")

    op.drop_index("uq_pending_inputs_open", table_name="pending_inputs")
    op.drop_index("ix_pending_inputs_status", table_name="pending_inputs")
    op.drop_index("ix_pending_inputs_session_id", table_name="pending_inputs")
    op.drop_table("pending_inputs")

    op.drop_index("ix_session_turns_created_at", table_name="session_turns")
    op.drop_index("ix_session_turns_status", table_name="session_turns")
    op.drop_index("ix_session_turns_session_id", table_name="session_turns")
    op.drop_table("session_turns")

    op.drop_index("ix_sessions_created_at", table_name="sessions")
    op.drop_index("ix_sessions_status", table_name="sessions")
    op.drop_index("ix_sessions_project_id", table_name="sessions")
    op.drop_table("sessions")
