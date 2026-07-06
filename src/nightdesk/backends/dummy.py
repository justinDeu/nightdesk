"""Dummy backend used by the test suite and CI smoke runs.

Not surfaced in the editor (absent from ``CAPABILITIES``); registered only
so ``backend="dummy"`` profiles dispatch to a no-network executor.
"""
from __future__ import annotations

import sys

from nightdesk.backends.base import Backend, LaunchContext, LaunchPlan, StdioTransport
from nightdesk.domain.backend_capabilities import BackendCapability, SHARED_GROUP_KEYS
from nightdesk.worker.executor import DummyExecutor, ExecutionRequest, ExecutionResult


DUMMY = BackendCapability(
    code="dummy",
    label="Dummy (test)",
    summary="No-op executor for tests and CI smoke runs.",
    group_keys=SHARED_GROUP_KEYS,
    enabled=False,
)


class DummyBackend(Backend):
    descriptor = DUMMY
    wants_http = False

    def prepare_launch(self, ctx: LaunchContext) -> LaunchPlan:
        return LaunchPlan(
            cmd=[sys.executable, "-c", "pass"],
            transport=StdioTransport(),
            needs_claude_binary=False,
        )

    async def execute(self, req: ExecutionRequest) -> ExecutionResult:
        return await DummyExecutor().run(req)
