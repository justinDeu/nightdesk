"""Executor abstraction — "run a resolved run in an isolated environment and
produce its artifacts."

The executor is the orthogonal axis to backends (see
``docs/design/session-suite/k8s-executor.md``): a *backend* is *which* harness
(claude_sdk/opencode), an *executor* is *where* the isolated environment lives
(local bwrap sandbox today; a Kubernetes pod later). ``run_one`` stays the
target-agnostic orchestrator of DB state and selects an executor once via
``get_executor(...)``; the executor owns launch composition and running the
agent to completion.

Import arrow: ``worker -> executors -> backends -> domain``. Executors must
never import ``worker.run_one`` (they may import the lower ``worker`` leaf
modules such as ``worker.executor``/``worker.sandbox`` that carry no scheduler
state — see this module's own imports).
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, ClassVar, Optional

from nightdesk.backends import Assignment, Backend, LaunchContext
from nightdesk.domain.permissions import PermissionSpec
from nightdesk.domain.providers import ResolvedEndpoint
from nightdesk.worker.executor import ExecutionResult
from nightdesk.worker.executor import Executor as RunExecutor

if TYPE_CHECKING:  # avoid a runtime import cycle for a type-only reference
    from sqlalchemy.orm import Session

    from nightdesk.db.models import Run


@dataclass
class RunContext:
    """Everything an executor needs to launch and run one turn to completion.

    ``run_one`` resolves all of this (profile->spec merge, endpoint resolution,
    model assignments, workspace bundle, pricing snapshot, run token, base env)
    and hands the executor a fully-prepared context. The shape is broader than
    the design doc's sketch because the extracted launch/execute body reads
    already-resolved objects (``spec``/``backend``/``base_env``/``git_dirs``/
    ``prior_turn``) rather than re-deriving them — reality wins over the doc
    (see the module docstring reference).
    """

    run_id: str
    ticket_id: str
    ticket_title: str
    base_prompt: str
    run_intent: str

    spec: PermissionSpec
    backend: Backend
    primary: Optional[ResolvedEndpoint]
    endpoints: dict[str, ResolvedEndpoint]
    model_assignments: dict[str, Assignment]

    # Resolved primary workspace directory (host path the agent runs in) and
    # the worktree root the per-run scratch/session dirs anchor beside.
    workspace_dir: Path
    worktree_root: Path

    transcript_path: str
    # Precomputed by run_one so the executor needs neither ``_build_env`` nor
    # ``_git_metadata_dirs`` (both stay in run_one; also imported by cli/tests).
    base_env: dict[str, str]
    git_dirs: list[str]

    # Continue/resume inputs.
    conversation_session_id: Optional[str]
    prior_turn: Optional["Run"]
    next_run_context: Optional[str]

    cancel_event: asyncio.Event
    on_session_id: Optional[Callable[[str], None]]
    # For the in-executor cancel watcher (polls the ticket for a status change).
    session_factory: Callable[[], "Session"]

    # Test seam: ``RunOneConfig.executor`` injection. When set, the executor
    # dispatches to it in place of ``backend.execute`` exactly as run_one did.
    override_executor: Optional[RunExecutor] = None


@dataclass
class ResolvedWorkspace:
    """A workspace as an executor resolved it — the shape ``run_one`` writes
    into ``TicketWorkspace`` rows. Populated by remote executors (k8s) that
    provision off-host; the ``LocalExecutor`` leaves it empty because run_one
    still records host workspace resolution itself in Phase 1.
    """

    kind: Optional[str] = None
    access: Optional[str] = None
    source_path: Optional[str] = None
    resolved_path: Optional[str] = None
    repo_root: Optional[str] = None
    git_common_dir: Optional[str] = None
    relative_path: Optional[str] = None
    worktree_path: Optional[str] = None
    branch: Optional[str] = None
    base_ref: Optional[str] = None
    base_sha: Optional[str] = None
    run_start_sha: Optional[str] = None
    retention: Optional[str] = None
    state: str = "active"


@dataclass
class ExecutionOutcome:
    """What an executor returns to ``run_one`` after running the agent.

    ``launch_ctx`` is carried back so run_one can still call the backend's
    ``after_run`` hook (a deviation from the design doc's leaner outcome; the
    hook stays in run_one to avoid reordering it relative to ``finish_run``).
    ``workspaces``/``diff_uploaded`` are the forward-looking k8s fields; the
    ``LocalExecutor`` returns ``workspaces=[]`` and ``diff_uploaded=False``.
    """

    result: ExecutionResult
    launch_ctx: LaunchContext
    workspaces: list[ResolvedWorkspace] = field(default_factory=list)
    diff_uploaded: bool = False


class Executor(ABC):
    """Provision the isolated environment, run the agent to completion, produce
    the run's transcript (+ diff) artifacts."""

    code: ClassVar[str]

    @abstractmethod
    async def execute(self, ctx: RunContext) -> ExecutionOutcome:
        ...

    def reconcile_orphans(self, session: "Session", *, host: str) -> None:
        """Adopt/GC executor-owned work left behind by a crashed worker.

        No-op for the local executor (host pid-liveness recovery already covers
        it); remote executors (k8s) override this.
        """
        return None
