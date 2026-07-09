"""external integrations: connections, repo_links, project_repo_links, external_links

Revision ID: 0029_integrations
Revises: 0026_session_kind
Create Date: 2026-07-08

GitLab integration v1 (read + link + import). See
docs/design/gitlab-jira-integrations.md §3.

RE-CHAIN NOTE: this revision is numbered 0029 but chains directly off
``0026_session_kind`` because it was authored in parallel with two sibling
branches that own the intervening numbers (``0027_*`` token-perms and
``0028_*`` ack-flow). The gap is intentional. When all three branches merge,
the final-integration pass linearizes them (0027 -> 0028 -> 0029) by editing
this file's ``down_revision`` to point at whatever true head exists at merge
time; nothing in this migration depends on 0027/0028, so the reorder is a
one-line ``down_revision`` change. Do NOT collapse the gap before the siblings
land or their down_revisions will dangle.

All four tables are new and additive; downgrade drops them in FK-safe order.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0029_integrations"
down_revision: Union[str, Sequence[str], None] = "0026_session_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=False),
        sa.Column("auth_kind", sa.String(), nullable=False, server_default="pat"),
        sa.Column("credential", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="unchecked"),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_connections_name"),
    )

    op.create_table(
        "repo_links",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("external_kind", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("external_path", sa.String(), nullable=False, server_default=""),
        sa.Column("display_name", sa.String(), nullable=False, server_default=""),
        sa.Column("git_remote_url", sa.String(), nullable=True),
        sa.Column("web_url", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connections.id"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "connection_id", "external_kind", "external_id",
            name="uq_repo_links_connection_kind_external_id",
        ),
    )
    op.create_index("ix_repo_links_connection_id", "repo_links", ["connection_id"])

    op.create_table(
        "project_repo_links",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("repo_link_id", sa.String(), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repo_link_id"], ["repo_links.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_project_repo_links_repo_link_id", "project_repo_links", ["repo_link_id"],
    )

    op.create_table(
        "external_links",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("repo_link_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("external_iid", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="references"),
        sa.Column("url", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("state_detail", sa.JSON(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("author_kind", sa.String(), nullable=False, server_default="admin"),
        sa.Column("author_run_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["repo_link_id"], ["repo_links.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["author_run_id"], ["runs.id"]),
        sa.UniqueConstraint(
            "ticket_id", "repo_link_id", "kind", "external_iid",
            name="uq_external_links_ticket_repo_kind_iid",
        ),
    )
    op.create_index("ix_external_links_ticket_id", "external_links", ["ticket_id"])
    op.create_index("ix_external_links_repo_link_id", "external_links", ["repo_link_id"])


def downgrade() -> None:
    op.drop_index("ix_external_links_repo_link_id", table_name="external_links")
    op.drop_index("ix_external_links_ticket_id", table_name="external_links")
    op.drop_table("external_links")
    op.drop_index("ix_project_repo_links_repo_link_id", table_name="project_repo_links")
    op.drop_table("project_repo_links")
    op.drop_index("ix_repo_links_connection_id", table_name="repo_links")
    op.drop_table("repo_links")
    op.drop_table("connections")
