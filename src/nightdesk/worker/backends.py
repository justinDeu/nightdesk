"""Executor backend registry.

Each profile declares a ``backend`` string (e.g. ``"claude_sdk"``). The
worker uses ``get_executor(backend)`` to dispatch to the right
``Executor`` implementation at run time. Backend-specific options like
``default_model`` and ``permission_mode`` live as first-class columns on
the Profile; the executor reads them off the resolved ``PermissionSpec``.

Adding a new backend is two steps:
1. Implement an ``Executor`` (see ``nightdesk.worker.executor.Executor``).
2. Register it here in ``_REGISTRY``.
"""
from __future__ import annotations

from typing import Callable

from nightdesk.worker.claude_executor import ClaudeExecutor
from nightdesk.worker.executor import DummyExecutor, Executor


_Factory = Callable[[], Executor]


_REGISTRY: dict[str, _Factory] = {
    "claude_sdk": lambda: ClaudeExecutor(),
    # ``dummy`` is the test-mode backend used in conftest / CI smoke tests.
    "dummy": lambda: DummyExecutor(),
}


class UnknownBackend(Exception):
    pass


def register(name: str, factory: _Factory) -> None:
    """Register a backend factory. Intended for tests; production backends
    should be added directly to ``_REGISTRY``."""
    _REGISTRY[name] = factory


def get_executor(backend: str) -> Executor:
    factory = _REGISTRY.get(backend)
    if factory is None:
        raise UnknownBackend(
            f"unknown backend {backend!r}; known: {sorted(_REGISTRY)}"
        )
    return factory()


def available_backends() -> list[str]:
    return sorted(_REGISTRY)
