"""run_latency: cached per-run latency summary table

Revision ID: 0021_run_latency
Revises: 0020_ticket_commit_on_finish
Create Date: 2026-07-03

Adds the ``run_latency`` derived cache table (one row per run) populated when a
run finishes by ``domain.latency.populate_run_latency``. The analytics dashboard
aggregates these rows instead of scanning transcript files on every load. No
backfill: rows are filled in as runs complete (and on demand via the idempotent
populate path); runs that finished before this migration simply have no latency
row and do not contribute to latency stats until re-derived.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0021_run_latency"
down_revision: Union[str, Sequence[str], None] = "0020_ticket_commit_on_finish"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_latency",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("total_model_seconds", sa.Float(), nullable=False),
        sa.Column("total_tool_seconds", sa.Float(), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("ttft_seconds", sa.Float(), nullable=True),
        # SQLite stores JSON as TEXT; the ORM serializes/deserializes it.
        sa.Column("turn_latencies", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_run_latency_model", "run_latency", ["model"])


def downgrade() -> None:
    op.drop_index("ix_run_latency_model", table_name="run_latency")
    op.drop_table("run_latency")
