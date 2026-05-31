"""add project and ticket toolchain context

Revision ID: 0013_toolchain_context
Revises: 0012_workspace_source_path_cutover
Create Date: 2026-05-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0013_toolchain_context"
down_revision: Union[str, Sequence[str], None] = "0012_workspace_source_path_cutover"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("default_toolchains", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("default_tool_paths", sa.JSON(), nullable=False, server_default="[]"))
    with op.batch_alter_table("tickets") as batch:
        batch.add_column(sa.Column("toolchain_overrides", sa.JSON(), nullable=True))
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("sandbox_tool_paths", sa.JSON(), nullable=True))
    with op.batch_alter_table("config") as batch:
        batch.add_column(sa.Column("toolchain_presets", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("config") as batch:
        batch.drop_column("toolchain_presets")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("sandbox_tool_paths")
    with op.batch_alter_table("tickets") as batch:
        batch.drop_column("toolchain_overrides")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("default_tool_paths")
        batch.drop_column("default_toolchains")
