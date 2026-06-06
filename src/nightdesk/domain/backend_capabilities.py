"""Backend capability descriptors.

A profile's ``backend`` decides *which executor* runs the ticket (see
``nightdesk.worker.backends``). Different backends consume different
configuration: the Claude Code SDK backend reads credentials, a binary
path, a permission mode and a pile of CC-specific env knobs, while an
OMP/RPC backend talks to a remote endpoint and ignores all of that.

Historically the profile editor rendered every field unconditionally and
hard-coded the assumption "everything below is Claude Code". This module
makes that knowledge declarative: each backend declares the *field groups*
it consumes, and the editor / preview render against the declaration
instead of branching on string literals scattered across the codebase.

Design:

- :class:`FieldGroup` — a coherent cluster of form fields (e.g. the
  Authentication fieldset, or the filesystem mounts). Each group is either
  ``shared`` (every backend consumes it: the sandbox/runtime surface) or
  backend-specific.
- :class:`BackendCapability` — one per backend. Lists the field-group keys
  it consumes, plus presentation metadata (label, summary, whether it is
  selectable yet).

The two are intentionally separate from the executor registry: a backend
can be *described* here (so its form renders) before its executor is fully
wired for production runs.
"""
from __future__ import annotations

from dataclasses import dataclass


# --- field groups ----------------------------------------------------------


@dataclass(frozen=True)
class FieldGroup:
    """A cluster of related profile-editor fields.

    ``shared`` groups make up the common sandbox/runtime surface every
    backend honours (filesystem reach, tool allow/deny, network,
    secrets/env, system prompt). Backend-specific groups render only when a
    backend that consumes them is selected.
    """

    key: str
    label: str
    description: str
    shared: bool = False


# The shared sandbox/runtime surface. Every backend consumes these; the
# editor always renders them regardless of the selected backend.
FILESYSTEM = FieldGroup(
    "filesystem", "Filesystem reach",
    "Read/write mounts the sandbox exposes to the agent.", shared=True,
)
TOOLS = FieldGroup(
    "tools", "Tool allow / deny",
    "Which tools the agent may or may not call.", shared=True,
)
NETWORK = FieldGroup(
    "network", "Network",
    "Outbound network posture for the sandbox.", shared=True,
)
ENV = FieldGroup(
    "env", "Secrets / environment",
    "Custom environment variables injected into the run (encrypted at rest).",
    shared=True,
)
SYSTEM_PROMPT = FieldGroup(
    "system_prompt", "System prompt",
    "Text prepended to the agent's system prompt.", shared=True,
)
RUN_TOKEN_SCOPES = FieldGroup(
    "run_token_scopes", "Run-token scopes",
    "Extra Nightdesk API capabilities granted to the per-run token.",
    shared=True,
)

# Claude Code (claude_sdk) specific groups.
CLAUDE_AUTH = FieldGroup(
    "claude_auth", "Authentication",
    "How the sandboxed Claude Code authenticates to Anthropic "
    "(host file, auth token, or API key) plus an optional custom endpoint.",
)
CLAUDE_BINARY = FieldGroup(
    "claude_binary", "Claude Code binary",
    "Override for the `claude` binary the worker invokes.",
)
PERMISSION_MODE = FieldGroup(
    "permission_mode", "Permission mode",
    "How Claude Code handles tool-permission prompts.",
)
CLAUDE_MODELS = FieldGroup(
    "claude_models", "Models",
    "Default model plus per-alias overrides (ANTHROPIC_*_MODEL).",
)
CLAUDE_BEHAVIOR = FieldGroup(
    "claude_behavior", "Behavior",
    "Claude Code env toggles (telemetry, autoupdater, thinking budget…).",
)

# OMP / RPC (omp_rpc) specific groups.
OMP_CONNECTION = FieldGroup(
    "omp_connection", "OMP / RPC connection",
    "Remote model endpoint, auth token, and model name the RPC backend dials.",
)


# Registry of every known field group, keyed by ``key``.
_FIELD_GROUPS: tuple[FieldGroup, ...] = (
    FILESYSTEM,
    TOOLS,
    NETWORK,
    ENV,
    SYSTEM_PROMPT,
    RUN_TOKEN_SCOPES,
    CLAUDE_AUTH,
    CLAUDE_BINARY,
    PERMISSION_MODE,
    CLAUDE_MODELS,
    CLAUDE_BEHAVIOR,
    OMP_CONNECTION,
)

FIELD_GROUPS: dict[str, FieldGroup] = {g.key: g for g in _FIELD_GROUPS}

# The shared surface, in render order. Used by callers that want to assert
# "these are common to every backend".
SHARED_GROUP_KEYS: tuple[str, ...] = tuple(
    g.key for g in _FIELD_GROUPS if g.shared
)


# --- backend capabilities ---------------------------------------------------


@dataclass(frozen=True)
class BackendCapability:
    """What a single backend consumes and how the editor presents it."""

    code: str
    label: str
    summary: str
    # Field-group keys this backend consumes, in render order. Always a
    # superset of the shared groups.
    group_keys: tuple[str, ...]
    # Selectable in the editor dropdown. A described-but-disabled backend
    # shows up greyed out rather than vanishing, so the roadmap is visible.
    enabled: bool = True

    def consumes(self, group_key: str) -> bool:
        return group_key in self.group_keys

    @property
    def groups(self) -> tuple[FieldGroup, ...]:
        return tuple(FIELD_GROUPS[k] for k in self.group_keys if k in FIELD_GROUPS)

    @property
    def specific_groups(self) -> tuple[FieldGroup, ...]:
        """Backend-specific (non-shared) groups this backend consumes."""
        return tuple(g for g in self.groups if not g.shared)

    @property
    def inert_groups(self) -> tuple[FieldGroup, ...]:
        """Backend-specific groups this backend does NOT consume.

        These are the fields that, were they shown, would be ignored at run
        time — the editor hides them and the preview can list them as inert
        so the user understands why a field they saw on another backend is
        missing here.
        """
        return tuple(
            g for g in _FIELD_GROUPS
            if not g.shared and g.key not in self.group_keys
        )


_SHARED_TAIL = SHARED_GROUP_KEYS

CLAUDE_SDK = BackendCapability(
    code="claude_sdk",
    label="Claude Code",
    summary=(
        "Runs the Claude Code agent inside the bubblewrap sandbox. Consumes "
        "the Anthropic credentials, binary path, permission mode, and the "
        "model / behavior env knobs."
    ),
    group_keys=(
        # Claude-specific groups first (they dominate the form), then shared.
        CLAUDE_AUTH.key,
        CLAUDE_BINARY.key,
        PERMISSION_MODE.key,
        CLAUDE_MODELS.key,
        CLAUDE_BEHAVIOR.key,
        *_SHARED_TAIL,
    ),
    enabled=True,
)

OMP_RPC = BackendCapability(
    code="omp_rpc",
    label="OMP / RPC",
    summary=(
        "Dispatches the run to a remote Open-Model-Protocol endpoint over "
        "RPC. Consumes the endpoint URL, an auth token, and a model name; "
        "Claude-specific credentials, binary, and permission mode do not "
        "apply."
    ),
    group_keys=(
        OMP_CONNECTION.key,
        *_SHARED_TAIL,
    ),
    enabled=True,
)


_BACKENDS: tuple[BackendCapability, ...] = (CLAUDE_SDK, OMP_RPC)

CAPABILITIES: dict[str, BackendCapability] = {b.code: b for b in _BACKENDS}

DEFAULT_BACKEND = CLAUDE_SDK.code


# --- helpers ----------------------------------------------------------------


def all_capabilities() -> tuple[BackendCapability, ...]:
    """Every described backend, in display order."""
    return _BACKENDS


def get_capability(code: str | None) -> BackendCapability | None:
    """Return the capability for ``code`` (None if unknown)."""
    if not code:
        return None
    return CAPABILITIES.get(code)


def capability_or_default(code: str | None) -> BackendCapability:
    """Like :func:`get_capability` but falls back to the default backend.

    The editor renders against a capability even for legacy/unknown backend
    strings; defaulting keeps the form usable instead of crashing.
    """
    return get_capability(code) or CAPABILITIES[DEFAULT_BACKEND]


def backend_choices() -> tuple[tuple[str, str, bool], ...]:
    """``(code, label, enabled)`` triples for the editor's backend dropdown."""
    return tuple((b.code, b.label, b.enabled) for b in _BACKENDS)


def enabled_backends() -> frozenset[str]:
    """Codes selectable in the editor (and accepted by the form POST)."""
    return frozenset(b.code for b in _BACKENDS if b.enabled)


def consumes(code: str | None, group_key: str) -> bool:
    """Does ``code``'s backend consume the given field group?

    Shared groups are consumed by every backend (including unknown ones, which
    fall back to the default). Backend-specific groups are consumed only when
    declared.
    """
    cap = capability_or_default(code)
    return cap.consumes(group_key)
