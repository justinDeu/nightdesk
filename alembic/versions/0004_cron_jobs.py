"""add cron_jobs and cron_job_fires

Revision ID: 0004_cron_jobs
Revises: 0003_config_worktree_base_ref
Create Date: 2026-05-24

Additive. Recurring cron jobs: a ticket template plus a schedule. When the
schedule fires the worker materializes one ordinary queued, run_now=false
ticket from the template, which then flows through the normal scheduler.

`cron_job_fires` is the idempotency + audit table; its unique constraint on
(cron_job_id, fire_at) makes materialization idempotent and a NULL ticket_id
with a skipped_reason records a suppressed (e.g. overlap-skipped) fire.

Written as a NEW additive revision rather than editing the squashed 0001
baseline: 0002/0003 already sit on top of 0001, so real installs are past the
baseline and would never receive cron tables added to it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_cron_jobs"
down_revision: Union[str, Sequence[str], None] = "0003_config_worktree_base_ref"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cron_jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cwd", sa.String(), nullable=False),
        sa.Column("workspace_mode", sa.String(), nullable=False, server_default="directory"),
        sa.Column("additional_dirs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("permission_overrides", sa.JSON(), nullable=True),
        sa.Column("schedule", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False, server_default="UTC"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("misfire_policy", sa.String(), nullable=False, server_default="coalesce"),
        sa.Column("overlap_policy", sa.String(), nullable=False, server_default="skip_if_active"),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ticket_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.ForeignKeyConstraint(["last_ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "cron_job_fires",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("cron_job_id", sa.String(), nullable=False),
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=True),
        sa.Column("skipped_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cron_job_id"], ["cron_jobs.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cron_job_id", "fire_at", name="uq_cron_job_fires_job_fire"),
    )
    op.create_index(
        op.f("ix_cron_job_fires_cron_job_id"), "cron_job_fires", ["cron_job_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_cron_job_fires_cron_job_id"), table_name="cron_job_fires")
    op.drop_table("cron_job_fires")
    op.drop_table("cron_jobs")
