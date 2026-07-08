"""Kubernetes execution target.

``executor`` imports ``client`` which imports ``kubernetes`` lazily, so this
package is importable (and the k8s executor registrable) without the optional
``[k8s]`` extra installed — only actually talking to a cluster needs it.
"""
from __future__ import annotations

from nightdesk.executors.k8s.executor import K8sExecutor

__all__ = ["K8sExecutor"]
