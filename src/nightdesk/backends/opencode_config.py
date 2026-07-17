"""Pure renderers for the opencode backend.

opencode is configured entirely through two env vars the worker injects —
``OPENCODE_CONFIG_CONTENT`` (inline JSON config) and ``OPENCODE_AUTH_CONTENT``
(inline credentials) — so no files are written into, or mounted from, the
sandbox. These functions turn a nightdesk ``PermissionSpec`` plus the run's
resolved endpoints/model assignments into those JSON documents, and map
nightdesk's Claude-shaped tool names onto opencode's permission keys. They
are deliberately side-effect-free so the mapping is unit-testable without a
running server.

Multi-endpoint shape (see ``docs/design/providers-and-endpoints.md``, "one
provider block per endpoint"): every endpoint a profile's run touches (the
primary plus any per-agent endpoint) gets its own opencode provider block,
keyed ``nd_<endpoint_id>`` via :func:`block_id`. Model strings are always
``nd_<endpoint_id>/<model>`` so opencode routes each slot through the right
block regardless of how many vendors a run spans.
"""
from __future__ import annotations

import json
import logging
from typing import Optional, TYPE_CHECKING

from nightdesk.domain.permissions import GIT_PUSH_DENY_RULES, PermissionSpec

if TYPE_CHECKING:
    from nightdesk.backends.base import Assignment
    from nightdesk.domain.providers import ResolvedEndpoint


log = logging.getLogger(__name__)


def block_id(endpoint_id: str) -> str:
    """The opencode provider-block id nightdesk renders for an endpoint."""
    return f"nd_{endpoint_id}"


# nightdesk stores Claude-shaped tool names; opencode keys its permissions
# differently. ``edit`` covers edit/write/patch on the opencode side.
CLAUDE_TO_OPENCODE_TOOL = {
    "bash": "bash",
    "read": "read",
    "edit": "edit",
    "multiedit": "edit",
    "write": "edit",
    "notebookedit": "edit",
    "glob": "glob",
    "grep": "grep",
    "webfetch": "webfetch",
    "websearch": "websearch",
    "task": "task",
    "agent": "task",
    "todowrite": "todowrite",
    "skill": "skill",
}

# Every permission key opencode understands. Used to deny the complement when
# the profile pins an allowlist.
OPENCODE_PERMISSION_KEYS = (
    "bash", "edit", "read", "glob", "grep", "task", "todowrite",
    "skill", "webfetch", "websearch", "question", "external_directory",
    "doom_loop",
)

# protocol_kind -> (ai-sdk npm package, default base URL or None).
_KIND_NPM = {
    "anthropic": ("@ai-sdk/anthropic", None),
    "anthropic_compat": ("@ai-sdk/anthropic", None),
    "openai": ("@ai-sdk/openai", None),
    "openai_compat": ("@ai-sdk/openai-compatible", None),
    "openai_codex": ("@ai-sdk/openai", None),  # Responses-shaped OAuth surface
    "openrouter": ("@ai-sdk/openai-compatible", "https://openrouter.ai/api/v1"),
    "ollama": ("@ai-sdk/openai-compatible", "http://localhost:11434/v1"),
}

# Credential sources that render inline into the provider block's
# options.apiKey. oauth_file / subscription_file route through render_auth's
# auth-blob path instead (see there).
_INLINE_KEY_SOURCES = ("api_key", "env_var")


def _tool_key(name: str) -> Optional[str]:
    return CLAUDE_TO_OPENCODE_TOOL.get(str(name).strip().lower())


def _permission_from_tools(tools: list) -> dict:
    allowed = {_tool_key(t) for t in tools}
    allowed.discard(None)
    return {key: ("allow" if key in allowed else "deny") for key in OPENCODE_PERMISSION_KEYS}


def render_permission(spec: PermissionSpec) -> dict:
    """Map allow/deny tool lists to an opencode ``permission`` block.

    Headless never-ask: every key resolves to ``allow`` or ``deny`` so no
    ``ask`` can stall an unattended run. An allowlist (``allowed_tools`` set)
    denies the complement; otherwise everything is allowed except ``denied``.
    ``git push`` denial maps onto a bash pattern deny.
    """
    allowed = {_tool_key(t) for t in spec.allowed_tools}
    allowed.discard(None)
    denied = {_tool_key(t) for t in spec.denied_tools}
    denied.discard(None)

    perm: dict[str, object] = {}
    if allowed:
        for key in OPENCODE_PERMISSION_KEYS:
            perm[key] = "allow" if key in allowed else "deny"
    else:
        for key in OPENCODE_PERMISSION_KEYS:
            perm[key] = "allow"
    for key in denied:
        perm[key] = "deny"

    # Safe headless defaults regardless of the lists above.
    perm["question"] = "deny"          # cannot answer interactive prompts
    perm["external_directory"] = "deny"  # sandbox already blocks; be explicit

    # git push stays blocked unless the profile opted in, mirroring the claude
    # backend's GIT_PUSH_DENY_RULES. Only meaningful when bash is otherwise
    # allowed; a fully-denied bash already blocks it.
    if (
        perm.get("bash") == "allow"
        and not getattr(spec, "allow_git_push", False)
        and GIT_PUSH_DENY_RULES
    ):
        perm["bash"] = {"*": "allow", "git push*": "deny"}
    return perm


def resolve_model(spec: PermissionSpec, endpoint: "Optional[ResolvedEndpoint]") -> Optional[str]:
    """The bare model id (without provider prefix), or None for the default.

    Legacy single-endpoint path, used when no ``model_assignments`` were
    resolved for the run (pre-provider profiles, or a primary with nothing
    pinned)."""
    model = spec.backend_config.get("model") or spec.default_model
    if not model and endpoint is not None:
        model = endpoint.default_model
    if not model:
        return None
    model = str(model)
    # Accept "provider/model" and strip the prefix; the block-id re-prefix
    # happens at the call site.
    return model.split("/", 1)[1] if "/" in model else model


def _provider_block(endpoint: "ResolvedEndpoint") -> dict:
    npm, default_base = _KIND_NPM.get(
        endpoint.protocol_kind, ("@ai-sdk/openai-compatible", None),
    )
    options: dict[str, object] = {}
    base_url = endpoint.base_url or default_base
    if base_url:
        options["baseURL"] = base_url
    if endpoint.credential and endpoint.credential_source in _INLINE_KEY_SOURCES:
        options["apiKey"] = endpoint.credential
    # Vendor quirks win over anything nightdesk derived above.
    options.update((endpoint.extra or {}).get("options", {}) or {})

    models = {m: {} for m in (endpoint.models or [])}
    label = endpoint.provider_name or endpoint.label
    block: dict[str, object] = {"npm": npm, "name": label}
    if options:
        block["options"] = options
    if models:
        block["models"] = models
    return block


def model_str(assignment: "Assignment") -> str:
    """The ``nd_<endpoint_id>/<model>`` string opencode expects for a slot."""
    return f"{block_id(assignment.endpoint_id)}/{assignment.model}"


def build_prompt_body(
    text: str, *, system: Optional[str] = None, model: Optional[str] = None,
) -> dict:
    """The ``POST /session/{id}/prompt_async`` body for one user turn.

    Shared by the ticket driver (``opencode_driver._post_text``) and the
    resident handle (``worker.resident_backends``) so the prompt wire shape —
    the ``build`` agent, the text part, the optional per-prompt system text,
    and the ``nd_<endpoint>/<model>`` split into ``{providerID, modelID}`` —
    lives in exactly one place. ``model`` without a ``/`` is left unset so
    opencode falls back to the config's own model."""
    body: dict = {"agent": "build", "parts": [{"type": "text", "text": text}]}
    if system:
        body["system"] = system
    if model and "/" in model:
        provider_id, model_id = model.split("/", 1)
        body["model"] = {"providerID": provider_id, "modelID": model_id}
    return body


def _render_agents(
    backend_config: dict,
    assignments: "dict[str, Assignment]",
) -> dict:
    agents_cfg: dict[str, object] = {}
    for agent in (backend_config or {}).get("agents", []) or []:
        if not isinstance(agent, dict):
            continue
        name = agent.get("name")
        if not name:
            continue
        assignment = assignments.get(f"agent:{name}")
        if assignment is None:
            log.warning("opencode agent %r has no resolved model assignment; skipping", name)
            continue
        entry: dict[str, object] = {"model": model_str(assignment)}
        tools = agent.get("tools")
        if tools:
            entry["permission"] = _permission_from_tools(tools)
        prompt = agent.get("prompt")
        if prompt:
            entry["prompt"] = prompt
        agents_cfg[name] = entry
    return agents_cfg


def render_config(
    spec: PermissionSpec,
    endpoints: "dict[str, ResolvedEndpoint]",
    assignments: "dict[str, Assignment]",
) -> dict:
    """Build the inline ``OPENCODE_CONFIG_CONTENT`` document.

    ``endpoints`` is every endpoint the run touches (primary plus per-agent),
    keyed by endpoint id; ``assignments`` is the partial slot -> Assignment
    map (see ``LaunchContext.model_assignments`` / the design doc's "unpinned
    state") — an absent slot renders nothing, it is never defaulted here.
    """
    cfg: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "permission": render_permission(spec),
    }
    if endpoints:
        cfg["provider"] = {block_id(eid): _provider_block(ep) for eid, ep in endpoints.items()}

    primary = assignments.get("primary")
    if primary is not None:
        cfg["model"] = model_str(primary)
    small = assignments.get("small_model")
    if small is not None:
        cfg["small_model"] = model_str(small)

    agents_cfg = _render_agents(spec.backend_config, assignments)
    if agents_cfg:
        cfg["agent"] = agents_cfg

    if getattr(spec, "system_prompt", None):
        cfg["instructions"] = []  # extra system text is sent per-prompt instead
    return cfg


def _parse_codex_oauth(raw: str, *, endpoint_id: str) -> Optional[dict]:
    """Parse a Codex ``~/.codex/auth.json`` blob into opencode's oauth auth
    shape. Fail-soft: any parse/shape problem logs a warning and returns
    None so the block simply carries no auth (the run then fails at the
    provider with a clear auth error rather than crashing here)."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        log.warning("endpoint %s: codex oauth credential is not valid JSON: %s", endpoint_id, exc)
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        log.warning("endpoint %s: codex oauth credential missing 'tokens'", endpoint_id)
        return None
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        log.warning("endpoint %s: codex oauth credential missing access_token", endpoint_id)
        return None
    entry: dict[str, object] = {"type": "oauth", "access": access}
    refresh = tokens.get("refresh_token")
    if isinstance(refresh, str) and refresh:
        entry["refresh"] = refresh
    # Codex's auth.json carries no numeric expiry we can trust (the access
    # token's own exp claim would need JWT decoding, and last_refresh is a
    # refresh timestamp, not an expiry) — omit rather than guess.
    return entry


def _render_auth_entry(endpoint: "ResolvedEndpoint") -> Optional[dict]:
    source = endpoint.credential_source
    if source in _INLINE_KEY_SOURCES:
        if not endpoint.credential:
            return None
        return {"type": "api", "key": endpoint.credential}
    if source == "oauth_file":
        if not endpoint.credential:
            return None
        return _parse_codex_oauth(endpoint.credential, endpoint_id=endpoint.id)
    # subscription_file endpoints are harness-locked to claude_sdk and should
    # never reach an opencode profile (the compatibility gate blocks it), but
    # render nothing rather than leak the subscription token if one slips
    # through.
    return None


def render_auth(endpoints: "dict[str, ResolvedEndpoint]") -> Optional[dict]:
    """Build ``OPENCODE_AUTH_CONTENT``: one entry per endpoint that has a
    credential, keyed by that endpoint's provider-block id. Returns None
    when nothing resolves (e.g. every endpoint is credential-less, as with
    a local ollama)."""
    auth: dict[str, object] = {}
    for eid, ep in endpoints.items():
        entry = _render_auth_entry(ep)
        if entry is not None:
            auth[block_id(eid)] = entry
    return auth or None
