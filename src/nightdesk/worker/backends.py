"""Compatibility shim over :mod:`nightdesk.backends`.

The backend abstraction now lives in the top-level ``nightdesk.backends``
package (one module per harness, see ``backends/base.py``). This module
keeps the old import surface working for callers and tests that still ask
for an *executor* (an object with ``.run(req)``): it adapts the registered
:class:`~nightdesk.backends.base.Backend` into that shape.
"""
from __future__ import annotations

from nightdesk.backends import available_backends, get_backend
from nightdesk.backends.registry import UnknownBackend, register_factory
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult


__all__ = [
    "UnknownBackend",
    "available_backends",
    "get_executor",
    "register",
]


class _BackendExecutor:
    """Adapts a Backend to the legacy ``Executor`` (``.run``) interface."""

    def __init__(self, backend) -> None:
        self._backend = backend

    async def run(self, req: ExecutionRequest) -> ExecutionResult:
        return await self._backend.execute(req)


def get_executor(backend: str):
    return _BackendExecutor(get_backend(backend))


def register(name: str, factory) -> None:
    """Register a backend (test seam). ``factory`` is a zero-arg callable
    returning either a Backend or a legacy executor."""
    obj = factory()
    if hasattr(obj, "execute"):
        from nightdesk.backends.registry import register as _register
        _register(name, obj)
        return
    # Legacy executor: wrap it in a minimal Backend.
    from nightdesk.backends.base import Backend, LaunchContext, LaunchPlan

    class _Adapter(Backend):
        descriptor = None  # not editor-surfaced

        def prepare_launch(self, ctx: LaunchContext) -> LaunchPlan:
            return LaunchPlan(cmd=[])

        async def execute(self, req: ExecutionRequest) -> ExecutionResult:
            return await obj.run(req)

    from nightdesk.backends.registry import register as _register
    _register(name, _Adapter())
