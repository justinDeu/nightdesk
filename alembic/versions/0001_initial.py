"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-19

Squashed baseline. Replaces the historical 0001..0013 chain that built up
the v1-shipping schema across many incremental migrations during pre-share
development. Fresh installs get the final schema in one step.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

OLD_PATH_COL = "c" "wd"

revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("fs_read", sa.JSON(), nullable=False),
        sa.Column("fs_write", sa.JSON(), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("denied_tools", sa.JSON(), nullable=False),
        sa.Column("network_mode", sa.String(), nullable=False),
        sa.Column("network_allowlist", sa.JSON(), nullable=False),
        sa.Column("secret_keys", sa.JSON(), nullable=False),
        sa.Column("default_model", sa.String(), nullable=True),
        sa.Column("backend", sa.String(), nullable=False, server_default="claude_sdk"),
        sa.Column("claude_credentials", sa.Text(), nullable=True),
        sa.Column("claude_binary_path", sa.String(), nullable=True),
        sa.Column("env", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("permission_mode", sa.String(), nullable=True),
        sa.Column("cc_settings_passthrough", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("run_token_scopes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "tickets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("permission_overrides", sa.JSON(), nullable=True),
        sa.Column("additional_dirs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(OLD_PATH_COL, sa.String(), nullable=False),
        sa.Column("workspace_mode", sa.String(), nullable=False, server_default="in_place"),
        sa.Column("run_now", sa.Boolean(), nullable=False),
        sa.Column("scheduled_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_run_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_run_context", sa.Text(), nullable=True),
        sa.Column("next_run_context_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.ForeignKeyConstraint(["current_run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tickets_status"), "tickets", ["status"], unique=False)
    op.create_index(op.f("ix_tickets_priority"), "tickets", ["priority"], unique=False)
    op.create_index(op.f("ix_tickets_created_at"), "tickets", ["created_at"], unique=False)

    op.create_table(
        "runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_status", sa.String(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("worktree_path", sa.String(), nullable=False),
        sa.Column("transcript_path", sa.String(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("started_as_run_now", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("intent", sa.String(), nullable=False, server_default="first_run"),
        sa.Column("parent_run_id", sa.String(), nullable=True),
        sa.Column("headless_policy_version", sa.String(), nullable=True),
        sa.Column("restart_workspace_policy", sa.String(), nullable=True),
        sa.Column("failure_kind", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("model_used", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.ForeignKeyConstraint(["parent_run_id"], ["runs.id"], name="fk_runs_parent_run_id_runs"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "ticket_workspaces",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False, server_default="linked"),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("access", sa.String(), nullable=False, server_default="read_write"),
        sa.Column("source_path", sa.String(), nullable=True),
        sa.Column("resolved_path", sa.String(), nullable=True),
        sa.Column("repo_root", sa.String(), nullable=True),
        sa.Column("git_common_dir", sa.String(), nullable=True),
        sa.Column("relative_path", sa.String(), nullable=True),
        sa.Column("worktree_name", sa.String(), nullable=True),
        sa.Column("worktree_path", sa.String(), nullable=True),
        sa.Column("branch", sa.String(), nullable=True),
        sa.Column("base_ref", sa.String(), nullable=True),
        sa.Column("base_sha", sa.String(), nullable=True),
        sa.Column("head_sha", sa.String(), nullable=True),
        sa.Column("retention", sa.String(), nullable=False, server_default="preserve"),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ticket_workspaces_ticket_id"), "ticket_workspaces", ["ticket_id"], unique=False)
    op.create_index(op.f("ix_ticket_workspaces_run_id"), "ticket_workspaces", ["run_id"], unique=False)

    op.create_table(
        "run_tokens",
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("scope_data", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(op.f("ix_run_tokens_run_id"), "run_tokens", ["run_id"], unique=False)
    op.create_index(op.f("ix_run_tokens_ticket_id"), "run_tokens", ["ticket_id"], unique=False)

    op.create_table(
        "config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(), nullable=False),
        sa.Column("window_end", sa.String(), nullable=False),
        sa.Column("max_parallel", sa.Integer(), nullable=False),
        sa.Column("worktree_root", sa.String(), nullable=False),
        sa.Column("transcript_root", sa.String(), nullable=False),
        sa.Column("polling_interval_seconds", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("max_run_duration_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("run_token_grace_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("claude_binary_path", sa.String(), nullable=True),
        sa.Column("session_cookie_ttl_seconds", sa.Integer(), nullable=False, server_default="2592000"),
        sa.Column("cc_minimum_version", sa.String(), nullable=False, server_default="2.1.80"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "daemon_status",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cc_binary_path", sa.String(), nullable=True),
        sa.Column("cc_version", sa.String(), nullable=True),
        sa.Column("cc_check_status", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("cc_check_message", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "worker_heartbeat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "CREATE VIRTUAL TABLE tickets_fts USING fts5("
            "title, prompt, id UNINDEXED)"
        )
    )
    bind.execute(
        sa.text(
            "CREATE TRIGGER tickets_ai AFTER INSERT ON tickets BEGIN "
            "INSERT INTO tickets_fts(rowid, title, prompt, id) "
            "VALUES (new.rowid, new.title, new.prompt, new.id); END;"
        )
    )
    bind.execute(
        sa.text(
            "CREATE TRIGGER tickets_ad AFTER DELETE ON tickets BEGIN "
            "DELETE FROM tickets_fts WHERE rowid = old.rowid; END;"
        )
    )
    bind.execute(
        sa.text(
            "CREATE TRIGGER tickets_au AFTER UPDATE ON tickets BEGIN "
            "DELETE FROM tickets_fts WHERE rowid = old.rowid; "
            "INSERT INTO tickets_fts(rowid, title, prompt, id) "
            "VALUES (new.rowid, new.title, new.prompt, new.id); END;"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DROP TRIGGER IF EXISTS tickets_au"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS tickets_ad"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS tickets_ai"))
    bind.execute(sa.text("DROP TABLE IF EXISTS tickets_fts"))

    op.drop_table("worker_heartbeat")
    op.drop_table("daemon_status")
    op.drop_table("config")
    op.drop_index(op.f("ix_run_tokens_ticket_id"), table_name="run_tokens")
    op.drop_index(op.f("ix_run_tokens_run_id"), table_name="run_tokens")
    op.drop_table("run_tokens")
    op.drop_index(op.f("ix_ticket_workspaces_run_id"), table_name="ticket_workspaces")
    op.drop_index(op.f("ix_ticket_workspaces_ticket_id"), table_name="ticket_workspaces")
    op.drop_table("ticket_workspaces")
    op.drop_table("runs")
    op.drop_index(op.f("ix_tickets_created_at"), table_name="tickets")
    op.drop_index(op.f("ix_tickets_priority"), table_name="tickets")
    op.drop_index(op.f("ix_tickets_status"), table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("profiles")
