"""Execution targets for nightdesk runs.

Importing this package registers every built-in executor. ``run_one`` resolves
the target once via :func:`get_executor`; the worker has no ``if target == ...``
branching. See ``docs/design/session-suite/k8s-executor.md``.

Import arrow: ``worker -> executors -> backends -> domain``. Executors never
import ``worker.run_one``.
"""
from __future__ import annotations

from nightdesk.executors.base import (
    Executor,
    ExecutionOutcome,
    ProvisionContext,
    ProvisionOutcome,
    ResolvedWorkspace,
    RunContext,
)
from nightdesk.executors.registry import (
    UnknownExecutor,
    available_executors,
    get_executor,
    register,
)


def _register_builtins() -> None:
    # Importing the module runs its module-level ``register(...)`` call.
    from nightdesk.executors import local  # noqa: F401

    # K8sExecutor is always registered so run_one can select it by target; it
    # validates its config (runner image, cluster-routable API) at provision
    # time and fails fast when unconfigured. Its client imports ``kubernetes``
    # lazily, so registration never requires the optional [k8s] extra.
    from nightdesk.executors.k8s import K8sExecutor

    register(K8sExecutor.code, K8sExecutor())


_register_builtins()


__all__ = [
    "Executor",
    "ExecutionOutcome",
    "ProvisionContext",
    "ProvisionOutcome",
    "ResolvedWorkspace",
    "RunContext",
    "UnknownExecutor",
    "available_executors",
    "get_executor",
    "register",
]
