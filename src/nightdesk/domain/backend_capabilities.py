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

from dataclasses import dataclass, field
from enum import Enum


# --- runtime capabilities --------------------------------------------------


class Capability(str, Enum):
    """A run-time feature a backend may or may not provide.

    Distinct from :class:`FieldGroup` (which is editor configuration): these
    describe what a backend can *do at run time*, so the product surface
    degrades per-feature instead of assuming Claude parity. The required
    floor — stream canonical transcript events, end with a terminal result,
    run headless, die on SIGTERM — is not listed here; it is mandatory for
    every backend. Everything below is optional and degrades when absent.
    """

    COST_USAGE = "cost_usage"             # token counts + dollar cost
    SESSION_RESUME = "session_resume"     # resumable conversation handle
    TOOL_POLICY = "tool_policy"           # enforces allow/deny tool lists
    PERMISSION_MODES = "permission_modes"  # claude-style permission modes
    MODEL_SELECT = "model_select"         # honours a chosen model
    SYSTEM_PROMPT = "system_prompt"       # injects a custom system prompt
    FOLLOW_UP_CONTEXT = "follow_up_context"  # continues a prior run's session
    SUBAGENTS = "subagents"               # emits subagent lifecycle events
    THINKING = "thinking"                 # emits extended-thinking blocks
    RATE_LIMIT_SIGNAL = "rate_limit_signal"  # emits rate-limit events
    STEER_QUEUE = "steer_queue"    # accepts a queued follow-up delivered at the next turn boundary
    STEER_INJECT = "steer_inject"  # delivers a queued follow-up into the SAME live run without a new Run


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

# Provider selection. Shared in spirit — any backend can dial a configured
# inference Provider — but rendered as its own fieldset (a row picker plus an
# optional per-profile model override), so it is not a ``shared`` runtime
# group like filesystem/tools.
PROVIDER = FieldGroup(
    "provider", "Inference provider",
    "Which configured provider (endpoint + credential) this profile dials, "
    "plus an optional model override.",
)


# Registry of every known field group, keyed by ``key``.
_FIELD_GROUPS: tuple[FieldGroup, ...] = (
    FILESYSTEM,
    TOOLS,
    NETWORK,
    ENV,
    SYSTEM_PROMPT,
    RUN_TOKEN_SCOPES,
    PROVIDER,
    CLAUDE_AUTH,
    CLAUDE_BINARY,
    PERMISSION_MODE,
    CLAUDE_MODELS,
    CLAUDE_BEHAVIOR,
)

FIELD_GROUPS: dict[str, FieldGroup] = {g.key: g for g in _FIELD_GROUPS}

# The shared surface, in render order. Used by callers that want to assert
# "these are common to every backend".
SHARED_GROUP_KEYS: tuple[str, ...] = tuple(
    g.key for g in _FIELD_GROUPS if g.shared
)


# --- backend capabilities ---------------------------------------------------


@dataclass(frozen=True)
class ModelSlot:
    """One model position a harness exposes.

    The slot-to-native mapping (which env var, which config key) is
    renderer-internal — the generic layer never interprets ``name``, so no
    mapping lives here. See ``docs/design/providers-and-endpoints.md``,
    "Model slots".
    """

    name: str
    label: str
    required: bool = False


@dataclass(frozen=True)
class BackendCapability:
    """What a single backend consumes and how the editor presents it."""

    code: str
    label: str
    summary: str
    # Field-group keys this backend consumes, in render order. Always a
    # superset of the shared groups.
    group_keys: tuple[str, ...]
    # Run-time features this backend provides. Absent capabilities degrade
    # the product surface gracefully (see :class:`Capability`).
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    # Protocol kinds (endpoint ``protocol_kind`` values) this backend speaks.
    # Empty means the backend does not dial a configured endpoint (uses
    # ambient credentials only).
    protocol_kinds: frozenset[str] = field(default_factory=frozenset)
    # Whether a single run can span more than one endpoint (primary + one per
    # named agent). False backends (claude_sdk) are pinned to one endpoint per
    # run — their model positions are env vars against a single
    # ANTHROPIC_BASE_URL and cannot span providers.
    multi_endpoint: bool = False
    # Static per-harness model positions. Dynamic (per-agent) slots are
    # enumerated via ``slots_for``, never appended here.
    model_slots: tuple[ModelSlot, ...] = ()
    # When True a run cannot start without an endpoint attached to the profile.
    requires_provider: bool = False
    # Protocol kinds where the harness's own alias/model resolution should be
    # left alone (see ``domain.model_assignment.compute_model_assignments``).
    # Only claude_sdk on first-party ``anthropic`` sets this — its opus/sonnet/
    # haiku aliases resolve natively. Every other (harness, protocol) pair
    # pins explicitly since the harness has no such native resolution.
    alias_native_protocols: frozenset[str] = field(default_factory=frozenset)
    # Selectable in the editor dropdown. A described-but-disabled backend
    # shows up greyed out rather than vanishing, so the roadmap is visible.
    enabled: bool = True
    # Whether the backend's executor is fully wired and can actually run tickets.
    # False means the executor returns a "not yet wired" failure immediately, so
    # the editor and view pane can surface a clear warning rather than letting
    # users dispatch runs that silently fail.
    executable: bool = True

    def consumes(self, group_key: str) -> bool:
        return group_key in self.group_keys

    def provides(self, capability: Capability) -> bool:
        return capability in self.capabilities

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
        PROVIDER.key,
        CLAUDE_AUTH.key,
        CLAUDE_BINARY.key,
        PERMISSION_MODE.key,
        CLAUDE_MODELS.key,
        CLAUDE_BEHAVIOR.key,
        *_SHARED_TAIL,
    ),
    # Every capability EXCEPT STEER_INJECT. claude_sdk's one-shot ``query()``
    # closes stdin right after the runner spec is written, so there is no live
    # input channel into a running turn: mid-run follow-ups are queued and
    # delivered as the NEXT turn (STEER_QUEUE), never injected into the same
    # run. This is the one place the "claude has every capability" shortcut is
    # deliberately broken so STEER_INJECT actually gates (see the mid-run
    # steering design). STEER_INJECT for claude needs a ClaudeSDKClient
    # streaming-input rework (deferred follow-up).
    capabilities=frozenset(Capability) - {Capability.STEER_INJECT},
    protocol_kinds=frozenset(("anthropic", "anthropic_compat")),
    multi_endpoint=False,
    model_slots=(
        ModelSlot("primary", "Primary model", required=False),
        ModelSlot("opus_alias", "Opus alias (hard tasks)"),
        ModelSlot("sonnet_alias", "Sonnet alias"),
        ModelSlot("haiku_alias", "Haiku alias (background work)"),
        ModelSlot("small_fast", "Small/fast model (legacy)"),
        ModelSlot("subagent", "Subagent model"),
    ),
    requires_provider=False,  # falls back to claude_credentials
    alias_native_protocols=frozenset(("anthropic",)),
    enabled=True,
)

OPENCODE = BackendCapability(
    code="opencode",
    label="opencode",
    summary=(
        "Runs the opencode agent against any configured provider. A per-run "
        "opencode server is launched inside the sandbox and driven over "
        "localhost HTTP. Tool policy, model selection, cost tracking, and "
        "session resume are honoured; Claude-specific auth / binary / "
        "permission-mode knobs do not apply."
    ),
    group_keys=(
        PROVIDER.key,
        *_SHARED_TAIL,
    ),
    capabilities=frozenset({
        Capability.COST_USAGE,
        Capability.SESSION_RESUME,
        Capability.TOOL_POLICY,
        Capability.MODEL_SELECT,
        Capability.SYSTEM_PROMPT,
        Capability.FOLLOW_UP_CONTEXT,
        # opencode drives a persistent localhost HTTP session and finishes on
        # session.idle; POSTing another prompt before idle keeps the same
        # session/run alive, so a queued follow-up injects into the SAME run.
        Capability.STEER_QUEUE,
        Capability.STEER_INJECT,
    }),
    protocol_kinds=frozenset(
        ("anthropic", "anthropic_compat", "openai", "openai_compat",
         "openai_codex", "openrouter", "ollama")
    ),
    multi_endpoint=True,
    model_slots=(
        ModelSlot("primary", "Primary model (model)"),
        ModelSlot("small_model", "Small model"),
    ),
    requires_provider=True,
    enabled=True,
)


_BACKENDS: tuple[BackendCapability, ...] = (CLAUDE_SDK, OPENCODE)

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


def slots_for(capability: BackendCapability, backend_config: dict) -> tuple[ModelSlot, ...]:
    """The full model-slot set for a profile: static slots plus dynamic ones.

    opencode names agents in ``backend_config["agents"]``; each named agent
    gets a synthetic ``agent:<name>`` slot. The editor, the worker's
    assignment resolution, and validation all enumerate through this hook
    rather than indexing ``capability.model_slots`` directly, so dynamic
    slots are never missed.
    """
    slots = list(capability.model_slots)
    if capability.code == "opencode":
        for agent in (backend_config or {}).get("agents", []) or []:
            name = agent.get("name") if isinstance(agent, dict) else None
            if name:
                slots.append(ModelSlot(f"agent:{name}", f"Agent: {name}"))
    return tuple(slots)


def consumes(code: str | None, group_key: str) -> bool:
    """Does ``code``'s backend consume the given field group?

    Shared groups are consumed by every backend (including unknown ones, which
    fall back to the default). Backend-specific groups are consumed only when
    declared.
    """
    cap = capability_or_default(code)
    return cap.consumes(group_key)
