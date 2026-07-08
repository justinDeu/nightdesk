"""execution target: profile.execution_target + config.k8s_* columns

Revision ID: 0025_execution_target
Revises: 0022_providers_and_endpoints
Create Date: 2026-07-08

Adds the Kubernetes cloud-sandbox executor's persistence
(docs/design/session-suite/k8s-executor.md, Phase 4). Additive only:

- ``profiles.execution_target`` TEXT NOT NULL DEFAULT 'local' — where runs of
  this profile execute ('local' bwrap sandbox, default; or 'k8s' per-run pod).
- ``config`` gains the k8s connection + pod-shape knobs (kubeconfig/in-cluster,
  namespace, runner image, cpu/mem requests+limits, node selector, runtime
  class, git-credentials Secret name).

Every add_column is inspector-guarded: correct on a clean DB (columns created)
and on a stamp-reconciled production DB that may already carry a colliding
column (left untouched). ``down_revision`` is 0022; re-parent at the
session-suite master merge if another 0023/0024 lands ahead of it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0025_execution_target"
down_revision: Union[str, Sequence[str], None] = "0024_diff_comments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


# (column name, SQLAlchemy type, server_default) for each config knob.
_CONFIG_COLUMNS = (
    ("k8s_kubeconfig_path", sa.String(), None),
    ("k8s_in_cluster", sa.Boolean(), sa.text("0")),
    ("k8s_namespace", sa.String(), sa.text("'nightdesk'")),
    ("k8s_runner_image", sa.String(), None),
    ("k8s_cpu_request", sa.String(), None),
    ("k8s_cpu_limit", sa.String(), None),
    ("k8s_mem_request", sa.String(), None),
    ("k8s_mem_limit", sa.String(), None),
    ("k8s_node_selector", sa.JSON(), sa.text("'{}'")),
    ("k8s_runtime_class", sa.String(), None),
    ("k8s_git_credentials_secret", sa.String(), None),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "profiles", "execution_target"):
        op.add_column("profiles", sa.Column(
            "execution_target", sa.String(),
            nullable=False, server_default="local",
        ))

    for name, coltype, default in _CONFIG_COLUMNS:
        if _has_column(inspector, "config", name):
            continue
        kwargs = {}
        if name in ("k8s_in_cluster", "k8s_namespace", "k8s_node_selector"):
            kwargs["nullable"] = False
        else:
            kwargs["nullable"] = True
        if default is not None:
            kwargs["server_default"] = default
        op.add_column("config", sa.Column(name, coltype, **kwargs))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for name, _coltype, _default in reversed(_CONFIG_COLUMNS):
        if _has_column(inspector, "config", name):
            op.drop_column("config", name)
    if _has_column(inspector, "profiles", "execution_target"):
        op.drop_column("profiles", "execution_target")
