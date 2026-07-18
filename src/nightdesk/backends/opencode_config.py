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

Exception: ``openai_codex`` endpoints (see :data:`NATIVE_PROVIDER_IDS`).
opencode's Codex OAuth handling — token refresh, the Responses-shaped
chatgpt-backend routing, the ``originator``/``ChatGPT-Account-Id`` headers —
lives in a plugin bundled with the opencode binary itself, hard-wired to the
literal provider id ``"openai"`` (verified against opencode 1.16.2 source,
``packages/opencode/src/plugin/openai/codex.ts``: the plugin's fetch/auth
loader and its Responses-routing hook both gate on
``model.providerID === "openai"``). A generic ``@ai-sdk/openai`` block
rendered under ``nd_<eid>`` never reaches that plugin — it only knows
``options.apiKey``, so it silently fails with an opaque
``AI_LoadAPIKeyError`` even when the endpoint's own oauth credential is
present and valid. So a Codex endpoint's provider block, auth entry, and
every model reference render onto ``"openai"`` instead of its own
``nd_<eid>`` namespace — see :func:`resolve_provider_ids`.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Optional, TYPE_CHECKING

from nightdesk.domain.permissions import GIT_PUSH_DENY_RULES, PermissionSpec

if TYPE_CHECKING:
    from nightdesk.backends.base import Assignment
    from nightdesk.domain.providers import ResolvedEndpoint


log = logging.getLogger(__name__)


def block_id(endpoint_id: str) -> str:
    """The nd_-namespaced opencode provider-block id for an endpoint.

    NOT the final rendered provider id for every protocol — see
    :func:`resolve_provider_ids`, which routes ``openai_codex`` endpoints
    onto opencode's bundled ``"openai"`` provider instead.
    """
    return f"nd_{endpoint_id}"


# protocol_kind -> the LITERAL opencode provider id nightdesk must render an
# endpoint's block/auth/model refs onto, instead of its own nd_<endpoint_id>
# namespace. Only protocols whose credential handling is a plugin bundled
# with the opencode binary itself (hard-wired to that exact provider id) need
# this — see the module docstring. Every other protocol's credential is a
# plain inline apiKey a generic ai-sdk package accepts under any block id, so
# nd_ namespacing (letting N endpoints of the same vendor coexist in one run)
# stays correct there.
NATIVE_PROVIDER_IDS: dict[str, str] = {"openai_codex": "openai"}


def resolve_provider_ids(
    endpoints: "dict[str, ResolvedEndpoint]",
    assignments: "dict[str, Assignment]",
) -> dict[str, str]:
    """endpoint id -> the opencode provider id its block/auth/model refs
    render onto: ``block_id(eid)`` normally, or the endpoint's
    :data:`NATIVE_PROVIDER_IDS` entry when its protocol requires opencode's
    bundled provider.

    Collision: opencode has exactly one provider slot per native id, so two
    endpoints of the same native-mapped protocol (e.g. two ``openai_codex``
    endpoints) in one profile can't both render there. The endpoint backing
    the ``primary`` assignment wins; the loser is dropped from the returned
    map (a caller keying its own output off this map simply omits that
    endpoint's block/auth/model refs — the run then either falls back to a
    slot with no assignment, the design doc's "unpinned state", or fails
    clearly at the missing provider, never silently talks to the wrong
    account). A warning is logged once per collision. This is a known
    limitation, not a supported multi-account Codex setup within one run.
    """
    primary = (assignments or {}).get("primary")
    primary_eid = primary.endpoint_id if primary is not None else None
    # Primary-first so it always claims a contested native id; ties among
    # non-primary endpoints resolve by original (insertion) order.
    ordered = sorted(endpoints.items(), key=lambda kv: kv[0] != primary_eid)
    claimed_by: dict[str, str] = {}
    out: dict[str, str] = {}
    for eid, ep in ordered:
        native = NATIVE_PROVIDER_IDS.get(ep.protocol_kind)
        pid = native if native is not None else block_id(eid)
        if native is not None and pid in claimed_by:
            log.warning(
                "opencode: endpoints %s and %s both map to the native "
                "provider id %r (protocol %r allows only one per run); "
                "%s wins, %s is dropped from this run's config/auth",
                claimed_by[pid], eid, pid, ep.protocol_kind, claimed_by[pid], eid,
            )
            continue
        claimed_by[pid] = eid
        out[eid] = pid
    return out


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
    if endpoint.protocol_kind in NATIVE_PROVIDER_IDS:
        # Extending opencode's own bundled provider (e.g. its "openai" Codex
        # plugin), not declaring a new one: emit ONLY the model menu (plus a
        # display name) — no ``npm`` override (config-provider's merge
        # REPLACES the provider's ``api``/aisdk wiring wholesale when set,
        # which would tear out the plugin's oauth-aware fetch/routing) and no
        # ``options.apiKey`` (the bundled provider's auth loader reads the
        # oauth entry from OPENCODE_AUTH_CONTENT directly; it never consults
        # options.apiKey). See the module docstring and
        # ``resolve_provider_ids``.
        block: dict[str, object] = {}
        label = endpoint.provider_name or endpoint.label
        if label:
            block["name"] = label
        models = {m: {} for m in (endpoint.models or [])}
        if models:
            block["models"] = models
        return block

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


def model_str(assignment: "Assignment", provider_ids: "dict[str, str]") -> str:
    """The ``<provider_id>/<model>`` string opencode expects for a slot.

    ``provider_ids`` is the endpoint id -> rendered provider id map from
    :func:`resolve_provider_ids` — normally ``nd_<endpoint_id>``, or the
    native provider id for a protocol like ``openai_codex``. Falls back to
    the plain nd_ block id for an endpoint id absent from the map (a
    collision loser, or a caller that didn't build the map from the same
    ``endpoints``/``assignments`` this assignment came from).
    """
    pid = provider_ids.get(assignment.endpoint_id) or block_id(assignment.endpoint_id)
    return f"{pid}/{assignment.model}"


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
    provider_ids: "dict[str, str]",
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
        entry: dict[str, object] = {"model": model_str(assignment, provider_ids)}
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
    provider_ids = resolve_provider_ids(endpoints, assignments)
    if endpoints:
        cfg["provider"] = {
            provider_ids[eid]: _provider_block(ep)
            for eid, ep in endpoints.items() if eid in provider_ids
        }

    primary = assignments.get("primary")
    if primary is not None:
        cfg["model"] = model_str(primary, provider_ids)
    small = assignments.get("small_model")
    if small is not None:
        cfg["small_model"] = model_str(small, provider_ids)

    agents_cfg = _render_agents(spec.backend_config, assignments, provider_ids)
    if agents_cfg:
        cfg["agent"] = agents_cfg

    if getattr(spec, "system_prompt", None):
        cfg["instructions"] = []  # extra system text is sent per-prompt instead
    return cfg


def _jwt_exp_ms(token: str) -> int:
    """The ``exp`` claim of a JWT access token, in milliseconds — 0 when the
    token isn't a decodable JWT or carries no numeric ``exp``. No signature
    verification: the value only schedules opencode's refresh, it grants
    nothing."""
    parts = token.split(".")
    if len(parts) != 3:
        return 0
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return 0
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if not isinstance(exp, (int, float)) or exp <= 0:
        return 0
    return int(exp * 1000)


def _parse_codex_oauth(raw: str, *, endpoint_id: str) -> Optional[dict]:
    """Parse a Codex ``~/.codex/auth.json`` blob into opencode's oauth auth
    shape (verified against opencode 1.16.2 source,
    ``packages/opencode/src/auth/index.ts`` and
    ``packages/opencode/src/plugin/openai/codex.ts``). Fail-soft: any
    parse/shape problem logs a warning and returns None so the block simply
    carries no auth (the run then fails at the provider with a clear auth
    error rather than crashing here).

    Field mapping notes:
    - ``accountId`` (camelCase) is opencode's key; Codex's own auth.json
      carries the same value as ``tokens.account_id`` (snake_case) —
      confirmed against a live ``~/.codex/auth.json`` and opencode's own
      ``~/.local/share/opencode/auth.json`` entry for its bundled ``openai``
      provider, which are structurally identical apart from that renaming.
    - ``expires`` is REQUIRED by opencode's auth schema (a non-negative int,
      milliseconds — confirmed against opencode's own auth store, whose
      entries are 13-digit epoch values). It is decoded from the access
      token's JWT ``exp`` claim (seconds -> ms). A forced refresh must be
      the LAST resort, not the default: ChatGPT oauth refresh tokens rotate
      on use, so a refresh consumes the single-use refresh token stored in
      ``~/.codex/auth.json`` and strands the rotated replacement in that one
      process's memory — the next process re-reads the file, presents the
      stale token, and gets a 401 (this took down real runs). ``expires: 0``
      (eager refresh on first request) is only the fallback when the access
      token isn't a decodable JWT.

    Known gap: when the access token genuinely IS expired, the plugin's
    refresh still rotates the file's refresh token and nightdesk persists
    nothing — an ``oauth_file`` endpoint goes stale after one refresh cycle
    and needs a re-login/re-import. The real fix (nightdesk-owned refresh
    with write-back to the endpoint credential) is tracked separately.
    """
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
    entry: dict[str, object] = {
        "type": "oauth", "access": access, "expires": _jwt_exp_ms(access),
    }
    refresh = tokens.get("refresh_token")
    if isinstance(refresh, str) and refresh:
        entry["refresh"] = refresh
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id:
        entry["accountId"] = account_id
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


def render_auth(
    endpoints: "dict[str, ResolvedEndpoint]",
    assignments: "Optional[dict[str, Assignment]]" = None,
) -> Optional[dict]:
    """Build ``OPENCODE_AUTH_CONTENT``: one entry per endpoint that has a
    credential, keyed by that endpoint's RENDERED provider id (see
    :func:`resolve_provider_ids` — ``nd_<endpoint_id>`` normally, or the
    native provider id for a protocol like ``openai_codex``). Returns None
    when nothing resolves (e.g. every endpoint is credential-less, as with a
    local ollama). ``assignments`` should be the same map passed to
    :func:`render_config` for this run so a native-provider collision (see
    ``resolve_provider_ids``) resolves identically in both documents;
    omitting it only matters when more than one endpoint maps to the same
    native id, and defaults to "no primary preference" in that rare case.

    ``OPENCODE_AUTH_CONTENT`` fully REPLACES opencode's on-disk auth store
    for this process (confirmed against opencode 1.16.2 source,
    ``packages/opencode/src/auth/index.ts``: when the env var is set its
    JSON is returned as-is, the on-disk file is never even read) — there is
    no merge to rely on, so every endpoint's credential the run touches must
    be represented here.
    """
    provider_ids = resolve_provider_ids(endpoints, assignments or {})
    auth: dict[str, object] = {}
    for eid, ep in endpoints.items():
        pid = provider_ids.get(eid)
        if pid is None:
            continue
        entry = _render_auth_entry(ep)
        if entry is not None:
            auth[pid] = entry
    return auth or None
