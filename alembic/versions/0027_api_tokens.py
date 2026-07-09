"""durable scoped api tokens (api_tokens)

Revision ID: 0027_api_tokens
Revises: 0026_sessions
Create Date: 2026-07-08

Additive table backing durable ``ndk_`` agent tokens (and, in Phase 2, run
tokens). Only sha256 hashes are stored; cleartext is shown once at mint. See
``docs/design/agent-token-permissions.md`` §3.

Re-chaining note: this branch was cut against head ``0026_session_kind``, which
resident-agents reworked in place into ``0026_sessions``. At the final
integration merge the down_revision is re-pointed to ``0026_sessions`` and the
sibling 0027-0029 revisions are linearized into one chain
(0026_sessions -> 0027 -> 0028 -> 0029).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0027_api_tokens"
down_revision: Union[str, Sequence[str], None] = "0026_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("prefix_hint", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="agent"),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("bundle", sa.String(), nullable=True),
        sa.Column("scope_data", sa.JSON(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("ticket_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False, server_default="admin"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens")
    op.drop_table("api_tokens")
