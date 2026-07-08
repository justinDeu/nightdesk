"""Kubernetes executor configuration, read from the ConfigRow.

A small typed view over the ``config.k8s_*`` columns plus the run's
cluster-routable API address. ``from_config_row`` builds it; ``validate``
enforces the two hard requirements a k8s run cannot proceed without — a runner
image and an API address the cluster can actually reach (the default
``127.0.0.1`` bind is unreachable from a pod). Validation is deliberately
host-side and fail-fast so a misconfigured profile fails at preflight rather
than stranding a pod.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse


class K8sConfigError(Exception):
    """Raised when the k8s executor is selected but not runnably configured."""


_UNREACHABLE_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1", ""})


@dataclass(frozen=True)
class K8sConfig:
    api_url: str
    namespace: str = "nightdesk"
    runner_image: Optional[str] = None
    kubeconfig_path: Optional[str] = None
    in_cluster: bool = False
    cpu_request: Optional[str] = None
    cpu_limit: Optional[str] = None
    mem_request: Optional[str] = None
    mem_limit: Optional[str] = None
    node_selector: dict = field(default_factory=dict)
    runtime_class: Optional[str] = None
    git_credentials_secret: Optional[str] = None

    @classmethod
    def from_config_row(cls, row, *, api_url: str) -> "K8sConfig":
        return cls(
            api_url=api_url,
            namespace=getattr(row, "k8s_namespace", None) or "nightdesk",
            runner_image=getattr(row, "k8s_runner_image", None) or None,
            kubeconfig_path=getattr(row, "k8s_kubeconfig_path", None) or None,
            in_cluster=bool(getattr(row, "k8s_in_cluster", False)),
            cpu_request=getattr(row, "k8s_cpu_request", None) or None,
            cpu_limit=getattr(row, "k8s_cpu_limit", None) or None,
            mem_request=getattr(row, "k8s_mem_request", None) or None,
            mem_limit=getattr(row, "k8s_mem_limit", None) or None,
            node_selector=dict(getattr(row, "k8s_node_selector", None) or {}),
            runtime_class=getattr(row, "k8s_runtime_class", None) or None,
            git_credentials_secret=getattr(row, "k8s_git_credentials_secret", None) or None,
        )

    def validate(self) -> None:
        if not self.runner_image:
            raise K8sConfigError(
                "k8s execution target selected but no runner image is configured "
                "(Settings -> Cloud sandbox -> runner image)"
            )
        host = (urlparse(self.api_url).hostname or "").lower()
        if host in _UNREACHABLE_HOSTS:
            raise K8sConfigError(
                f"k8s runs need a cluster-routable API address; {self.api_url!r} "
                "is unreachable from a pod. Set a routable bind host / external "
                "URL (or a Service/tunnel) before using the k8s executor."
            )
