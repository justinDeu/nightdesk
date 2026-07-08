"""Executor registry — mirrors ``backends/registry.py``.

``run_one`` resolves the execution target once via :func:`get_executor`; there
is no ``if target == ...`` branching in the worker. ``LocalExecutor`` is always
registered; remote executors (k8s) register when their config is present.
"""
from __future__ import annotations

from nightdesk.executors.base import Executor


class UnknownExecutor(KeyError):
    """Raised when no executor is registered under a code."""


_REGISTRY: dict[str, Executor] = {}


def register(code: str, executor: Executor) -> None:
    _REGISTRY[code] = executor


def get_executor(code: str) -> Executor:
    try:
        return _REGISTRY[code]
    except KeyError:
        raise UnknownExecutor(
            f"no executor registered for {code!r}; "
            f"known: {sorted(_REGISTRY)}"
        ) from None


def available_executors() -> list[str]:
    return sorted(_REGISTRY)
