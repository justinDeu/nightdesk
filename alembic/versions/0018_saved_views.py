"""Add saved_views table

Revision ID: 0018_saved_views
Revises: 0017_inbox_nullable_profile
Create Date: 2026-06-09

Each saved view stores a name, a surface (board | list), and a JSON dict of
URL params (q, group, order) so the user can navigate back to a recurring
query slice without bookmarking a URL manually.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0018_saved_views"
down_revision: Union[str, Sequence[str], None] = "0017_inbox_nullable_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "saved_views",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("saved_views")
