"""add schedule_windows table

Revision ID: 0004_schedule_windows
Revises: 0003_config_worktree_base_ref
Create Date: 2026-05-24

Introduces a named multi-window schedule model. Each row is a time window with
its own ``max_parallel`` cap and a ``day_mask`` bitmask (Mon=1..Sun=64). The
scheduler unions all matching windows for the current time/day and uses the
most permissive cap.

To preserve existing config on upgrade, this migration seeds a single window
from the current ConfigRow ``window_start``/``window_end``/``max_parallel``
values. The old columns stay on ConfigRow as dead storage (the legacy PATCH
fields keep working); removing them is a follow-up.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_schedule_windows"
down_revision: Union[str, Sequence[str], None] = "0003_config_worktree_base_ref"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedule_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("day_mask", sa.Integer(), nullable=False, server_default="127"),
        sa.Column("start", sa.String(), nullable=False, server_default="00:00"),
        sa.Column("end", sa.String(), nullable=False, server_default="00:00"),
        sa.Column("max_parallel", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Seed one window from the existing ConfigRow so no schedule is lost on
    # upgrade. There is at most one config row (id=1); skip if absent.
    bind = op.get_bind()
    cfg = bind.execute(
        sa.text(
            "SELECT window_start, window_end, max_parallel FROM config WHERE id = 1"
        )
    ).fetchone()
    if cfg is not None:
        window_start, window_end, max_parallel = cfg
        bind.execute(
            sa.text(
                "INSERT INTO schedule_windows "
                "(label, day_mask, start, end, max_parallel, position) "
                "VALUES (:label, 127, :start, :end, :max_parallel, 0)"
            ),
            {
                "label": "default",
                "start": window_start if window_start is not None else "00:00",
                "end": window_end if window_end is not None else "00:00",
                "max_parallel": max_parallel if max_parallel is not None else 1,
            },
        )


def downgrade() -> None:
    op.drop_table("schedule_windows")
