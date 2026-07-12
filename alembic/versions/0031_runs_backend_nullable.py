"""Relax orphan NOT NULL on runs.backend to match the ORM model.

Production's DB carries runs.backend as VARCHAR NOT NULL DEFAULT
'claude_sdk' — an orphan from the abandoned pre-providers branch that
0022 deliberately left untouched. The ORM (db/models.py) declares the
column nullable and run creation inserts NULL for legacy profiles, so
every INSERT violates the constraint and all runs fail at start. A
clean DB created by 0022 already has the column nullable; the guard
makes this a no-op there.

Revision ID: 0031_runs_backend_nullable
Revises: 0030_session_seen
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0031_runs_backend_nullable"
down_revision: Union[str, None] = "0030_session_seen"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backend_not_null(inspector) -> bool:
    for col in inspector.get_columns("runs"):
        if col["name"] == "backend":
            return not col["nullable"]
    return False


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _backend_not_null(inspector):
        with op.batch_alter_table("runs") as batch:
            batch.alter_column(
                "backend",
                existing_type=sa.String(),
                nullable=True,
                existing_server_default=sa.text("'claude_sdk'"),
            )


def downgrade() -> None:
    # The NOT NULL variant only ever existed as a production orphan;
    # nothing depends on restoring it.
    pass
