"""providers and endpoints: Provider + ProviderEndpoint, profile/run rewire

Revision ID: 0022_providers_and_endpoints
Revises: 0021_run_latency
Create Date: 2026-07-05

Introduces the final Provider + ProviderEndpoint split described in
docs/design/providers-and-endpoints.md: Provider slims to vendor identity
(pricing anchor); ProviderEndpoint carries protocol, base URL, credential,
credential source, optional harness lock, and model menu.

This descends from a prior branch's ``0019_providers`` (a single-table
Provider with kind/base_url/credential on one row, never merged, never seen
by production data), reshaped in place to the final schema per the
no-throwaway-migrations rule and renumbered to slot after main's
0021_run_latency.

profiles.provider_id -> profiles.endpoint_id (FK provider_endpoints.id).
runs.provider_id -> runs.endpoint_id (FK provider_endpoints.id); runs also
gains pricing_snapshot (JSON) so a run's cost is computed once, at run time,
from the prices in effect then -- later price changes never rewrite history.

No data migration: no production data has ever seen a `providers` table, so
there is nothing to carry forward.

Guard-based upgrade/downgrade: production's real DB was stamp-reconciled
from an abandoned pre-providers branch without ever downgrading it, so it
carries orphan columns this revision's lineage never created --
profiles.backend_config/backend_secret/endpoint_id and
runs.backend/backend_session_id/backend_session_path/backend_resume/
backend_event_version. `backend_config` and `endpoint_id` collide by name
with columns this revision adds to profiles, and `backend` collides on
runs. Every create_table/add_column below is guarded with an inspector
check so this revision is correct both on a clean DB (nothing pre-exists,
everything gets created) and on a stamp-reconciled production DB (the
colliding columns already exist and are left untouched --
profiles.endpoint_id and .backend_config are already all-NULL/'{}' on
production, and runs.backend already holds semantically-compatible
'claude_sdk' strings). downgrade() mirrors this: it only drops a
column/table that this revision would have created on a clean DB, so it
never removes the pre-existing orphan columns it didn't add.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0022_providers_and_endpoints"
down_revision: Union[str, Sequence[str], None] = "0021_run_latency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def _has_column(inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "providers"):
        op.create_table(
            "providers",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("vendor", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )

    if not _has_table(inspector, "provider_endpoints"):
        op.create_table(
            "provider_endpoints",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("provider_id", sa.String(), nullable=False),
            sa.Column("label", sa.String(), nullable=False, server_default=""),
            sa.Column("protocol_kind", sa.String(), nullable=False),
            sa.Column("base_url", sa.String(), nullable=True),
            sa.Column("credential_source", sa.String(), nullable=False, server_default="api_key"),
            sa.Column("credential", sa.Text(), nullable=True),
            sa.Column("harness_lock", sa.String(), nullable=True),
            sa.Column("default_model", sa.String(), nullable=True),
            sa.Column("models", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("models_pulled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("extra", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_provider_endpoints_provider_id", "provider_endpoints", ["provider_id"],
        )
    else:
        inspector = sa.inspect(bind)
        existing_indexes = {ix["name"] for ix in inspector.get_indexes("provider_endpoints")}
        if "ix_provider_endpoints_provider_id" not in existing_indexes:
            op.create_index(
                "ix_provider_endpoints_provider_id", "provider_endpoints", ["provider_id"],
            )

    inspector = sa.inspect(bind)

    if not _has_column(inspector, "profiles", "endpoint_id"):
        op.add_column("profiles", sa.Column("endpoint_id", sa.String(), nullable=True))
    if not _has_column(inspector, "profiles", "backend_config"):
        op.add_column("profiles", sa.Column("backend_config", sa.JSON(), nullable=True))
        op.execute("UPDATE profiles SET backend_config = '{}' WHERE backend_config IS NULL")

    if not _has_column(inspector, "runs", "backend"):
        op.add_column("runs", sa.Column("backend", sa.String(), nullable=True))
    if not _has_column(inspector, "runs", "endpoint_id"):
        op.add_column("runs", sa.Column("endpoint_id", sa.String(), nullable=True))
    if not _has_column(inspector, "runs", "session_ref"):
        op.add_column("runs", sa.Column("session_ref", sa.JSON(), nullable=True))
    if not _has_column(inspector, "runs", "pricing_snapshot"):
        op.add_column("runs", sa.Column("pricing_snapshot", sa.JSON(), nullable=True))

    # Harness-global runtime config (Layer 2): the opencode binary path,
    # alongside the existing claude_binary_path. Empty/NULL means
    # auto-discover from PATH / ~/.opencode/bin.
    if not _has_column(inspector, "config", "opencode_binary_path"):
        op.add_column("config", sa.Column("opencode_binary_path", sa.String(), nullable=True))

    # Note: no DB-level FK constraints added for the new *_id columns --
    # SQLite cannot ALTER ADD CONSTRAINT and FK enforcement is off by default
    # (same pattern as 0019_conversation_model). The ORM models declare the
    # FKs, so create_all (the in-memory test DB) still enforces the shape.


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "config", "opencode_binary_path"):
        op.drop_column("config", "opencode_binary_path")

    if _has_column(inspector, "runs", "pricing_snapshot"):
        op.drop_column("runs", "pricing_snapshot")
    if _has_column(inspector, "runs", "session_ref"):
        op.drop_column("runs", "session_ref")
    if _has_column(inspector, "runs", "endpoint_id"):
        op.drop_column("runs", "endpoint_id")
    if _has_column(inspector, "runs", "backend"):
        op.drop_column("runs", "backend")

    if _has_column(inspector, "profiles", "backend_config"):
        op.drop_column("profiles", "backend_config")
    if _has_column(inspector, "profiles", "endpoint_id"):
        op.drop_column("profiles", "endpoint_id")

    if _has_table(inspector, "provider_endpoints"):
        existing_indexes = {ix["name"] for ix in inspector.get_indexes("provider_endpoints")}
        if "ix_provider_endpoints_provider_id" in existing_indexes:
            op.drop_index("ix_provider_endpoints_provider_id", table_name="provider_endpoints")
        op.drop_table("provider_endpoints")
    if _has_table(inspector, "providers"):
        op.drop_table("providers")
