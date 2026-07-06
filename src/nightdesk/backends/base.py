"""Backend contracts.

A *backend* is the agent harness that runs inside the per-ticket bwrap
sandbox. This module defines the interface every backend implements so the
worker can dispatch a run without knowing which harness it is driving:
``run_one.py`` contains no ``if backend == ...`` branching — it asks the
backend to ``prepare_launch`` (compose the sandbox command), ``execute``
(drive the agent and translate its output into canonical transcript
events), and optionally ``after_run`` / ``resume_descriptor``.

Layering: this package imports only ``nightdesk.domain`` and
``nightdesk.worker.executor`` (pure data + the run loop primitives). It
never imports ``run_one`` or the scheduler, so the dependency arrow points
one way: worker -> backends -> domain.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar, Literal, Optional, TYPE_CHECKING

from nightdesk.domain.backend_capabilities import BackendCapability, Capability
from nightdesk.domain.permissions import PermissionSpec
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult

if TYPE_CHECKING:  # avoid importing the DB layer at runtime
    from nightdesk.domain.providers import ResolvedEndpoint
    from nightdesk.db.models import Run


@dataclass(frozen=True)
class Mount:
    """A host path bound into the sandbox.

    ``mode`` is ``"ro"`` (read-only) or ``"rw"`` (read-write). Consumed by
    ``worker.sandbox.build_bwrap_argv`` via duck typing — no import needed
    there, which keeps the worker->backends arrow one-directional.
    """

    host: str
    sandbox: str
    mode: Literal["ro", "rw"] = "ro"


@dataclass(frozen=True)
class StdioTransport:
    """The backend is driven over the child process's stdin/stdout."""


@dataclass(frozen=True)
class HttpTransport:
    """The backend serves HTTP on ``127.0.0.1:port`` (shared net namespace)."""

    port: int
    ready_path: str = "/"
    ready_timeout: float = 20.0


Transport = "StdioTransport | HttpTransport"


@dataclass
class LaunchPlan:
    """Everything the worker needs to assemble the sandboxed invocation."""

    cmd: list[str]                                   # argv run inside the sandbox
    env: dict[str, str] = field(default_factory=dict)  # merged over the base run env
    transport: object = field(default_factory=StdioTransport)
    mounts: list[Mount] = field(default_factory=list)  # extra binds (binaries, scratch)
    needs_claude_binary: bool = False                # bind the host `claude` binary
    # Per-run host dir bound as the sandbox CC session store. Claude-only; left
    # None by other backends. Kept as a first-class field because the sandbox
    # builder already special-cases its destination.
    cc_sessions_dir: Optional[str] = None
    # env keys whose values derive from a credential (endpoint secret, vendor
    # quirk headers from ``extra["env"]``/``extra["options"]``, server
    # passwords). The effective-config dry-run preview masks exactly these
    # keys rather than keeping its own list of secret names — see
    # ``docs/design/providers-and-endpoints.md``, "Effective-config preview
    # redacts secrets".
    secret_env_keys: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Assignment:
    """A model slot filled with a specific (endpoint, model) pair."""

    endpoint_id: str
    model: str


@dataclass
class LaunchContext:
    """Inputs handed to ``prepare_launch`` (and back to ``after_run``).

    ``backend_state`` is scratch space a backend uses to thread data from
    ``prepare_launch`` to ``execute`` / ``after_run`` (e.g. the session dir it
    created) without the worker needing to understand it.
    """

    spec: PermissionSpec
    # The primary endpoint. None means legacy/ambient credentials — the
    # backend's dispatch never runs and it falls back to its pre-endpoint path.
    endpoint: "Optional[ResolvedEndpoint]"
    run_id: str
    ticket_id: str
    workspace_dir: Path
    scratch_root: Path                  # per-run, sibling of worktree_root
    http_port: Optional[int] = None
    backend_state: dict = field(default_factory=dict)
    # True for the effective-config dry-run preview: ``prepare_launch`` must
    # have zero side effects (no directory creation, no requirement that
    # ``http_port`` be set even for ``wants_http`` backends). Writes to
    # ``backend_state`` are still fine — it's discarded after the preview.
    dry_run: bool = False
    # Every endpoint this run's profile references, keyed by id — the primary
    # (if any) plus every per-agent endpoint. Empty for legacy/ambient runs.
    endpoints: "dict[str, ResolvedEndpoint]" = field(default_factory=dict)
    # Slot name -> Assignment. Partial: an unpinned slot is simply absent, so
    # renderers iterate what is present rather than indexing every declared
    # slot (see the design doc's "unpinned state").
    model_assignments: "dict[str, Assignment]" = field(default_factory=dict)


class IncompatibleEndpoint(Exception):
    """Raised when a backend's renderer dispatch sees a protocol it does not
    speak. Should not happen in practice — the worker's compatibility gate
    (``endpoint_compatible``) runs before launch — but the renderer dispatch
    checks again so a bug in the gate fails loudly instead of mis-rendering."""

    def __init__(self, backend_code: str, protocol_kind: str):
        super().__init__(
            f"{backend_code} does not speak protocol {protocol_kind!r}"
        )
        self.backend_code = backend_code
        self.protocol_kind = protocol_kind


class Backend(ABC):
    """One agent harness. Subclasses live in sibling modules and register
    themselves in ``nightdesk.backends.registry``."""

    #: Editor / capability descriptor (from ``domain.backend_capabilities``).
    descriptor: ClassVar[BackendCapability]
    #: True when the backend serves HTTP and needs a port pre-allocated.
    wants_http: ClassVar[bool] = False
    #: protocol_kind -> renderer(ctx) -> LaunchPlan. Populated by subclasses
    #: that dispatch on the primary endpoint's protocol.
    _renderers: ClassVar["dict[str, Callable[[LaunchContext], LaunchPlan]]"] = {}

    @property
    def code(self) -> str:
        return self.descriptor.code

    def dispatch_renderer(self, ctx: LaunchContext):
        """Look up the renderer for ``ctx.endpoint``'s protocol.

        Returns the renderer's ``LaunchPlan`` output, ``NotImplemented`` when
        ``ctx.endpoint`` is None (the caller should use its own legacy path),
        or raises :class:`IncompatibleEndpoint` for an unknown protocol.
        """
        if ctx.endpoint is None:
            return NotImplemented
        renderer = self._renderers.get(ctx.endpoint.protocol_kind)
        if renderer is None:
            raise IncompatibleEndpoint(self.code, ctx.endpoint.protocol_kind)
        return renderer(ctx)

    def provides(self, capability: Capability) -> bool:
        return self.descriptor.provides(capability)

    @abstractmethod
    def prepare_launch(self, ctx: LaunchContext) -> LaunchPlan:
        """Compose the sandbox command, env, mounts, and transport."""

    @abstractmethod
    async def execute(self, req: ExecutionRequest) -> ExecutionResult:
        """Drive the agent to completion, writing canonical transcript events."""

    def after_run(self, ctx: LaunchContext, result: ExecutionResult) -> None:
        """Optional post-run hook (e.g. publish a resumable session). Best
        effort: must never raise in a way that fails an otherwise-good run."""

    def resume_descriptor(self, run: "Run") -> "Optional[ResumeDescriptor]":
        """Shell command to resume this run's conversation, or None."""
        return None

    def validate_profile(self, fields: dict) -> list[str]:
        """Editor-side validation messages for a profile using this backend."""
        return []


@dataclass(frozen=True)
class ResumeDescriptor:
    """A user-facing way to resume a finished run's conversation locally."""

    backend: str
    label: str
    command: str
    available: bool = True
