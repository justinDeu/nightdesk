"""Backend registry.

Maps a backend ``code`` to its :class:`Backend` instance. Adding a backend
is: write a module with a ``Backend`` subclass, then ``register()`` it here
(or let ``nightdesk.backends`` import it). The worker only ever calls
``get_backend(code)`` — it never imports a concrete backend.
"""
from __future__ import annotations

from typing import Callable

from nightdesk.backends.base import Backend


class UnknownBackend(Exception):
    pass


_REGISTRY: dict[str, Backend] = {}


def register(code: str, backend: Backend) -> None:
    _REGISTRY[code] = backend


def register_factory(code: str, factory: Callable[[], Backend]) -> None:
    """Compat shim for tests that register a backend lazily."""
    _REGISTRY[code] = factory()


def get_backend(code: str | None) -> Backend:
    backend = _REGISTRY.get(code or "")
    if backend is None:
        raise UnknownBackend(
            f"unknown backend {code!r}; known: {sorted(_REGISTRY)}"
        )
    return backend


def all_backends() -> list[Backend]:
    return list(_REGISTRY.values())


def available_backends() -> list[str]:
    return sorted(_REGISTRY)
