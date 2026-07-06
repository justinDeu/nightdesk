"""Backend harnesses for nightdesk runs.

Importing this package registers every built-in backend. The worker and API
import from here; concrete backend modules are never imported directly.
"""
from __future__ import annotations

from nightdesk.backends.base import (
    Assignment,
    Backend,
    HttpTransport,
    IncompatibleEndpoint,
    LaunchContext,
    LaunchPlan,
    Mount,
    ResumeDescriptor,
    StdioTransport,
)
from nightdesk.backends.registry import (
    UnknownBackend,
    all_backends,
    available_backends,
    get_backend,
    register,
)


def _register_builtins() -> None:
    from nightdesk.backends.claude_code import ClaudeBackend
    from nightdesk.backends.dummy import DummyBackend
    from nightdesk.backends.opencode import OpencodeBackend

    register("claude_sdk", ClaudeBackend())
    register("opencode", OpencodeBackend())
    register("dummy", DummyBackend())


_register_builtins()


__all__ = [
    "Assignment",
    "Backend",
    "HttpTransport",
    "IncompatibleEndpoint",
    "LaunchContext",
    "LaunchPlan",
    "Mount",
    "ResumeDescriptor",
    "StdioTransport",
    "UnknownBackend",
    "all_backends",
    "available_backends",
    "get_backend",
    "register",
]
