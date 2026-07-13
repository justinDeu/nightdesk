"""Index runs.ticket_id.

The unified project activity feed (docs/design/project-control-plane.md §History)
joins ``runs`` to ``tickets`` on ``ticket_id`` and filters by the project on
every request. Unlike ``ticket_events.ticket_id`` and ``external_links.ticket_id``
(both indexed), ``runs.ticket_id`` had no index, so the feed's runs source
full-scanned the runs table — cost growing with the whole instance's run
history. This adds the matching ``ix_runs_ticket_id`` index the ORM model now
declares (``db.models.Run.ticket_id`` ``index=True``).

Additive and idempotent: a no-op on fresh DBs created from the current metadata
(SQLAlchemy's ``create_all`` already emits the index) and on DBs where it has
already been applied.

Revision ID: 0032_runs_ticket_id_index
Revises: 0031_runs_backend_nullable
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0032_runs_ticket_id_index"
down_revision: Union[str, None] = "0031_runs_backend_nullable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_runs_ticket_id"


def _has_index(inspector) -> bool:
    return any(ix["name"] == INDEX_NAME for ix in inspector.get_indexes("runs"))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_index(inspector):
        return
    with op.batch_alter_table("runs") as batch_op:
        batch_op.create_index(INDEX_NAME, ["ticket_id"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_index(inspector):
        return
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_index(INDEX_NAME)
