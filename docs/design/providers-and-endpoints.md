# Providers, Endpoints, Harnesses, and Profiles

Status: design (revised 2026-07-05, second draft)
Working branch: `multi-backend` at `/home/thor/.local/share/nightdesk-worktrees/nightdesk/multi-backend`, HEAD `14ed5a3`, merge-base `b3036ec`. The branch ships a clean Backend interface, a first-cut Provider entity (`0019_providers`), an opencode backend, and `docs/backends.md`. This doc supersedes both the provider model in that branch and the first draft of this doc.

Changes from the first draft, all resolved with the user:

1. Credentials move from Provider to ProviderEndpoint. Provider slims to vendor identity plus the pricing anchor.
2. `claude_subscription` stops being a protocol. Subscription restriction is explicit data: `harness_lock` on the endpoint.
3. `openai_codex` is a real protocol kind (Responses-shaped, OAuth), not an `openai_compat` catalog entry. Verified against opencode's provider surface and blubblub's `ProviderKind` enum.
4. Profiles can span multiple providers when the harness supports it (opencode, blubblub). One harness, one primary endpoint, N per-agent endpoints.
5. Run cost is stamped at run time: the run row snapshots the prices in effect when it ran. Later price changes never rewrite history.

## Purpose

nightdesk runs one agent harness per ticket against one or more inference endpoints. The product has to let a user say "Claude Code harness, my ZAI subscription, GLM 5.2 everywhere" — or "opencode, GPT 5.4 primary, GLM 5.2 researcher subagent" — and have it work, price correctly at the time it ran, and degrade cleanly when a harness cannot do something.

The design makes five commitments up front.

1. The configuration surface is three layers: providers, harnesses, profiles.
2. The provider/endpoint split separates vendor identity from protocol. The compatibility join is protocol intersection plus an explicit per-endpoint harness lock, never vendor.
3. Credentials live on endpoints, because one vendor's surfaces authenticate differently (Anthropic: API key vs subscription file; OpenAI: API key vs Codex OAuth).
4. Per-harness configuration is owned by each harness integration. There is no generic model-slot schema. The shared layer is the join, not the form.
5. A multi-endpoint harness maps agents to (endpoint, model) pairs inside one profile. Single-endpoint harnesses (Claude Code) are the degenerate case, not the design center.

## The three configuration layers

Each layer is a distinct thing the user configures, with a distinct surface and a distinct responsibility. Settings live at the lowest layer that gives them meaning.

### Layer 1: Providers

A provider is a vendor identity. It groups the endpoints that vendor exposes and anchors pricing. Configured once, reused by every compatible harness.

What the user does here: registers "I have a ZAI subscription" by picking the vendor from the catalog, entering a credential once (the create flow seeds it into every selected endpoint), then pulls or curates each endpoint's model menu.

What lives here: vendor identity (for pricing), the user's label, and the set of owned endpoints. Each endpoint carries its own protocol, base URL, credential, credential source, optional harness lock, and model menu.

What does not live here: anything harness-specific. The provider does not know about Claude Code's env vars or opencode's JSON shape. It supplies raw inputs only.

### Layer 2: Harnesses

A harness is the agent runtime the sandbox drives. claude_sdk, opencode, blubblub, dummy. This layer is thin and mostly implicit.

What the user does here: almost nothing, most of the time. A harness appears in configuration only for harness-global runtime concerns such as the path to the `claude` or `opencode` binary. These live in the existing `ConfigRow` global-config row (which already holds `claude_binary_path`). Most users never open this surface.

What lives here: capability declaration, the set of protocols the harness speaks, whether it can span multiple endpoints in one run, the renderer code that turns resolved endpoints into native config, and a small set of harness-global defaults.

What does not live here: model choices, tool allow/deny, filesystem reach, permission mode. Those are per-run-shape and live on profiles.

### Layer 3: Profiles

A profile is the unit a ticket runs against. It joins a harness to a primary endpoint (plus optional per-agent endpoints) and carries the per-harness overrides that decide what the run looks like.

What the user does here: picks a harness, picks a provider (the primary endpoint resolves by protocol, with manual override when more than one matches), and configures the harness-specific options. Model selection happens here. On multi-endpoint harnesses, per-agent provider selection happens here too.

What lives here: the harness choice, the resolved primary endpoint, model assignments, per-agent endpoint references, tool policy, filesystem reach, network posture, permission mode, system prompt, run-token scopes.

Canonical examples:

- `claude-code-zai-smart`: harness=claude_sdk, provider=zai, endpoint=anthropic_compat (auto), model override = glm-5.2 everywhere.
- `claude-code-zai-fast`: same, model override = glm-4.5 everywhere.
- `opencode-zai-fast`: same ZAI provider, `openai_compat` endpoint, opencode harness. Nothing about ZAI changed.
- `opencode-mixed`: harness=opencode, primary = OpenAI Codex endpoint running gpt-5.4, plus a `researcher` agent on the ZAI `openai_compat` endpoint running glm-5.2. Two providers, one profile, one run.

## Distinctions that anchor the design

### Vendor and protocol are orthogonal

A vendor is who sells inference (ZAI, OpenRouter, Anthropic, OpenAI). A protocol is the API shape an endpoint speaks. One vendor may expose several protocols. ZAI exposes an Anthropic-compatible endpoint and an OpenAI-compatible endpoint against the same account.

`anthropic_compat` is a protocol kind. It is not a vendor. ZAI, Requesty, and others all expose `anthropic_compat` endpoints. Conflating the two is the core mistake this design corrects.

### Protocol and credential source are also orthogonal

The first draft committed the sibling mistake: `claude_subscription` as a protocol kind. A Claude subscription speaks the Anthropic Messages protocol; what differs is how it authenticates (host auth file, `ANTHROPIC_AUTH_TOKEN`) and who is allowed to drive it (Claude Code only, per Anthropic's terms).

So the model is: `protocol_kind` is pure API shape. `credential_source` says how the endpoint authenticates. `harness_lock` says which harness, if any, is exclusively allowed to use the endpoint. Three independent axes.

The Claude subscription endpoint is `(protocol_kind=anthropic, credential_source=subscription_file, harness_lock=claude_sdk)`. The lock is a product rule backed by Anthropic's ToS — opencode itself removed its bundled Claude-subscription plugins in 1.3.0 for the same reason.

### `openai_codex` is a distinct protocol, verified

The ChatGPT-subscription (Codex) surface is not Chat Completions with OAuth on top. Evidence:

- opencode's own provider surface offers "ChatGPT Plus/Pro" OAuth as a distinct option from OpenAI-API-key, and separately distinguishes `/v1/chat/completions` (`@ai-sdk/openai-compatible`) from `/v1/responses` (`@ai-sdk/openai`) endpoints.
- blubblub's `ProviderKind` enum is `{openai-compat, openai-codex, anthropic}` — Codex is its own kind with a hardcoded endpoint, no base_url required.

So the protocol vocabulary gains `openai_codex`: the Responses-shaped, OAuth-authenticated subscription surface. It carries no harness lock — using it from opencode and blubblub is an approved, working pattern. Harnesses that implement it declare it in `protocol_kinds`; protocol intersection does the rest. claude_sdk never declares it, so a Codex provider is simply never offered to a Claude Code profile.

Policy: nightdesk mirrors opencode's provider options as the compatibility target, since blubblub tracks the same set. When opencode grows a provider surface we care about, it gets a protocol kind or catalog entry here.

### The join is `prepare_launch`, not a registry

The thing that marries a harness to its endpoints is the existing `prepare_launch` boundary. Each backend implements:

```
prepare_launch(LaunchContext{spec, endpoints, model_assignments, ...}) -> LaunchPlan
```

Resolved endpoints go in. `LaunchPlan{cmd, env, mounts, ...}` comes out. The worker never branches on backend or vendor.

There is deliberately no separate `ProviderAdapter` class or central mapping table. The output shapes diverge too much to share a vocabulary: claude_sdk wants ANTHROPIC_* env vars, opencode wants an inline JSON document, blubblub wants whatever its integration defines. A shared interface would either be a featureless dict or leak one harness's model into the others. The renderer is the mapping, and it lives in the harness package. The contract is `LaunchPlan`, full stop.

The UI still gets the enumerability a registry would offer by asking the harness to render in dry-run mode against chosen endpoints. One source of truth, no second copy that drifts.

### Compatibility is protocol intersection plus the lock

A harness declares the protocols it speaks. A provider offers endpoints tagged with a protocol and optionally locked to a harness. A (harness, endpoint) pair is compatible when the protocol is in the harness's set and the lock, if present, names that harness.

```
compatible(harness, endpoint) = (
    endpoint.protocol_kind in harness.protocol_kinds
    and (endpoint.harness_lock is None or endpoint.harness_lock == harness.code)
)
compatible(harness, provider) = any(compatible(harness, e) for e in provider.endpoints)
```

On multi-endpoint profiles the gate runs per assignment: every referenced endpoint — primary and per-agent — must pass. A Claude subscription endpoint can never ride along as a subagent provider in an opencode profile.

The harness never names vendors. A new harness that declares `protocol_kinds = {openai_compat}` and ships one renderer is immediately compatible with every provider that exposes an unlocked `openai_compat` endpoint, present or future.

### Configuration is per-harness, not generic

The model positions a harness exposes are harness-specific. Claude Code has six env-var-driven model positions. opencode has `model`, `small_model`, and a per-agent (endpoint, model) pair for each named agent. blubblub resolves (provider, model) independently per child agent with parent inheritance (`resolve_child_provider_model`). There is no generic `slot_map` schema that meaningfully covers these.

The per-harness configuration lives in `profiles.backend_config`, a JSON blob whose shape each harness defines and whose UI each harness renders. Cross-harness concerns (the "fill every position with one model" convenience) are handled by a shorthand, not by a shared schema.

Slot enumeration is dynamic where it must be: opencode's per-agent slots exist only once the profile names agents. The descriptor declares the static slots; the harness exposes `slots_for(backend_config)` to enumerate the full set for a given profile. The editor, the worker's assignment resolution, and validation all call that, never the static tuple directly.

### Pricing keys off vendor, and cost is stamped at run time

Cost is a property of (vendor, model) at a point in time. ZAI's glm-5.2 is the same price whether reached through its Anthropic endpoint or its OpenAI endpoint. OpenRouter charges a different price for the same model because OpenRouter is a different vendor with its own markup. The protocol is a transport detail.

The point-in-time part matters as much as the vendor part. If a vendor halves its prices next week, last week's runs still cost what they cost. So the run row snapshots the applicable prices at run time and the stored cost is computed from that snapshot, once. Analytics reads stored values; it never re-derives historical cost from current prices. Details under Pricing integration.

## Layer 1: Providers

### Entities

```
Provider
  id            str (pk)
  name          str (unique)      # "ZAI", "OpenRouter", "Codex"
  vendor        str               # canonical tag for pricing: zai, openrouter,
                                  # anthropic, openai, ollama, custom
  created_at, updated_at

ProviderEndpoint
  id                str (pk)
  provider_id       FK providers.id
  label             str            # "Anthropic-compatible", "Coding plan", ...
  protocol_kind     str            # anthropic | anthropic_compat | openai |
                                   # openai_compat | openai_codex |
                                   # openrouter | ollama
  base_url          str (nullable) # null where the protocol implies it (openai_codex)
  credential_source str            # api_key | oauth_file | subscription_file | env_var | none
  credential        str (encrypted, nullable)  # the secret, or a path/reference to it
  harness_lock      str (nullable) # harness code exclusively allowed to use this
                                   # endpoint (ToS restrictions), or null
  default_model     str (nullable)
  models            JSON list[str] # the model menu this surface exposes
  models_pulled_at  datetime (nullable)
  extra             JSON           # vendor-quirk overrides, interpreted per
                                   # protocol by the renderer (headers, env, options)
  created_at, updated_at
```

`vendor` is the pricing key. It is distinct from `name` (the user's label) and from any endpoint's protocol. Two providers can share a vendor (two ZAI keys for two teams) and still resolve prices from the same source.

There is deliberately no `UNIQUE(provider_id, protocol_kind)`. Real vendors expose two surfaces on one protocol (ZAI's coding-plan vs pay-as-you-go base URLs; blubblub ships `zai` and `zai-coding` as separate built-ins). Multi-match already has a resolution path: the user picks.

The credential lives on the endpoint because one vendor's surfaces authenticate differently. The Anthropic vendor proves it: the API endpoint takes an `api_key`, the subscription endpoint takes a `subscription_file`. One provider-level credential cannot represent that. The registration flow keeps the ZAI convenience — paste the key once, it seeds every endpoint selected at create time. "Rotate credential" at the provider level updates every endpoint whose stored secret matches the old value.

`extra` is the escape hatch for vendor quirks no protocol renderer can derive (a required routing header, a nonstandard option). Each renderer decides what it means for its output shape — headers/env for claude_sdk, provider-block `options` for opencode. Values inside `extra` may be secrets (routing tokens); the column is encrypted with the same scheme as `credential`.

### Credential sources

- `api_key`: a pasted secret. ZAI, OpenRouter, OpenAI, Anthropic API.
- `oauth_file`: a reference to a token file on disk. Codex reads `~/.codex/auth.json`. The endpoint stores the path; the worker reads and, where the flow allows, refreshes the token at run time.
- `subscription_file`: a host auth file. Claude subscription credentials read from the user's claude config directory. Always paired with `harness_lock=claude_sdk`.
- `env_var`: read from a named environment variable at run time.
- `none`: no credential (local ollama).

At run time the worker resolves the credential per source, before the renderer sees it. The renderer receives a resolved secret plus the source tag — it still needs the tag, because e.g. claude_sdk emits `ANTHROPIC_AUTH_TOKEN` for subscription tokens and `ANTHROPIC_API_KEY` for keys (the branch's `claude_code.py` already branches this way; keep it).

### Model discovery

Models are pulled dynamically when the endpoint supports a list operation. ZAI's OpenAI-compatible endpoint exposes `/v1/models`. OpenRouter exposes `/api/v1/models`. For endpoints with no list operation (Codex, subscription), the menu is curated by hand or seeded from a shipped catalog. `models_pulled_at` drives a "refresh models" action in the UI and a periodic refresh worker.

### Known vendor catalog

nightdesk ships a small catalog of known vendors and the endpoints they typically expose, to seed registration:

| vendor | endpoints |
|---|---|
| zai | `anthropic_compat` (api.z.ai/api/anthropic), `openai_compat` (api.z.ai/api/paas/v4); coding-plan variants offered as a second pair |
| anthropic | `anthropic` + api_key; `anthropic` + subscription_file + harness_lock=claude_sdk |
| openai | `openai` + api_key; `openai_codex` + oauth_file (~/.codex/auth.json) |
| openrouter | `openrouter` + api_key |
| ollama | `ollama` + none |

The user picks a vendor, picks/pastes credentials, and confirms which endpoints to register. Unknown vendors fall back to manual entry of protocol, base_url, and credential.

## Layer 2: Harnesses

### Capability descriptor

```
BackendCapability
  code            str              # "claude_sdk", "opencode", "blubblub"
  label           str
  summary         str
  protocol_kinds  frozenset[str]   # protocols this harness speaks
  multi_endpoint  bool             # can one run span multiple endpoints?
  capabilities    frozenset[Capability]
  model_slots     tuple[ModelSlot] # static per-harness model positions
  group_keys      tuple[str]
  requires_provider  bool
  enabled         bool
```

`protocol_kinds` replaces the branch's `provider_kinds`. Same idea, honest name.

Declared sets:

- claude_sdk: `{anthropic, anthropic_compat}`, `multi_endpoint=False`. Its model positions are env vars against a single `ANTHROPIC_BASE_URL`; it physically cannot span providers.
- opencode: `{anthropic, anthropic_compat, openai, openai_compat, openai_codex, openrouter, ollama}`, `multi_endpoint=True`.
- blubblub (future): mirrors opencode.

### Model slots

```
ModelSlot
  name      str    # "primary", "haiku_alias", "small_model", ...
  label     str    # UI label
  required  bool
```

The slot-to-native mapping (which env var, which config key) is renderer-internal — the generic layer never interprets it, so it does not belong in the declaration. The first draft's `native_target` string micro-DSL is dropped.

Claude Code declares six static slots (see `src/nightdesk/domain/cc_env_catalog.py`): primary (`ANTHROPIC_MODEL`), opus_alias, sonnet_alias, haiku_alias, small_fast (legacy), subagent (`CLAUDE_CODE_SUBAGENT_MODEL`).

opencode declares two static slots (`model`, `small_model`) plus dynamic per-agent slots. Because agents are named in `backend_config`, the full slot set for a profile comes from the harness hook:

```
slots_for(backend_config) -> tuple[ModelSlot, ...]
```

For static-slot harnesses this returns the descriptor tuple. For opencode it appends one `agent:<name>` slot per configured agent. Editor, worker, and validation all enumerate through this hook.

### The CC alias-escape pitfall, and the unpinned state

CC escalates between aliases internally. A normal turn uses the primary slot. A hard task may resolve the opus alias. Background work uses the haiku alias. If a profile points CC at ZAI and fills only the primary slot, the moment CC resolves an unfilled alias it falls back to a Claude model the ZAI endpoint cannot serve, and the run breaks.

Full-pin (every slot set to the same model) is therefore the safe default **on `*_compat` endpoints**. Partial pinning is the dangerous middle that works until CC picks an unfilled alias. The CC profile editor defaults to full-pin there and requires a deliberate action to set a per-slot override.

On first-party endpoints (`protocol_kind=anthropic`, key or subscription) the correct default is the opposite: pin nothing and let CC's own alias resolution work. So the rendering contract has an explicit unpinned state — a slot with no assignment emits no env var. `model_assignments` is a partial map, not a total one, and renderers iterate what is present rather than indexing every declared slot.

**Rule revision (2026-07-18):** the unpinned state is not "any non-`*_compat` endpoint" — it is per-(harness, protocol), gated by `BackendCapability.alias_native_protocols`. Only a harness with its own native alias resolution should ever go unpinned, and only on the protocol where that resolution applies. `claude_sdk` declares `alias_native_protocols={"anthropic"}`: first-party Anthropic stays unpinned (CC's own aliases work), `anthropic_compat` still full-pins as above. `opencode` declares no native protocols at all — it has no alias-escape-hatch of its own — so it full-pins on every protocol it can dial, including `openai_codex`. Without this, a profile on an `openai_codex` endpoint with a `default_model` set got no primary assignment, `render_config` emitted no `model` key, and opencode silently fell back to its own global default model instead of the configured endpoint.

### Harness-global runtime config

Harness-global runtime defaults (binary paths, opencode autoupdate toggle) live in the existing `ConfigRow` global-config row, which already carries `claude_binary_path`. New harnesses add columns or a small `harness_config` JSON map there. These seed new profiles and rarely change.

## Layer 3: Profiles

### Entity

```
Profile
  id            str (pk)
  name          str (unique)
  description   str
  backend       str               # harness code
  endpoint_id   FK provider_endpoints.id (nullable)  # the primary endpoint
  default_model str (nullable)    # "fill every slot" shorthand (compat endpoints)
  backend_config JSON             # per-harness config: slot overrides, agents,
                                  # per-agent endpoint refs
  # existing run-shape fields stay:
  fs_read, fs_write, allowed_tools, denied_tools, network_mode,
  network_allowlist, secret_keys, env, system_prompt, permission_mode,
  run_token_scopes
  created_at, updated_at
```

The profile references an endpoint, not a provider. The provider is reached through the endpoint. This makes the protocol choice explicit: a CC profile against ZAI references the `anthropic_compat` endpoint; an opencode profile against ZAI references the `openai_compat` endpoint.

`endpoint_id` stays nullable: claude_sdk profiles that predate providers keep working through the legacy `claude_credentials` fallback until migrated, and `requires_provider=False` backends allow ambient-credential runs.

`default_model` is the cross-harness "fill every slot" shorthand. `backend_config` holds per-slot overrides on top of that. When both a profile `default_model` and an endpoint `default_model` exist, the profile wins; the endpoint's is a fallback for profiles that set nothing.

### backend_config shape

Each harness defines its own shape. Examples.

claude_sdk, full-pin smart profile: `{}` with `default_model = "glm-5.2"`. The renderer fills every declared slot with the default.

claude_sdk, partial override: `{"primary": "glm-4.5"}` with `default_model = "glm-4.5-flash"`.

claude_sdk, first-party subscription profile: `{}` with `default_model = null`. Nothing is pinned; CC resolves its own aliases.

opencode, multi-provider (the GPT-primary + GLM-researcher case):

```json
{
  "small_model": "gpt-5.4-mini",
  "agents": [
    {
      "name": "researcher",
      "endpoint_id": "ep_zai_openai_compat",
      "model": "glm-5.2",
      "tools": ["webfetch", "websearch"]
    }
  ]
}
```

with the profile's primary `endpoint_id` pointing at the OpenAI Codex endpoint and `default_model = "gpt-5.4"`. An agent entry with no `endpoint_id` inherits the primary — mirroring blubblub's parent-inheritance resolution and opencode's native behavior.

### Endpoint resolution

When a user picks a provider for a profile, nightdesk resolves the endpoint by intersecting the harness's `protocol_kinds` with the provider's compatible (unlocked-or-matching) endpoints. If exactly one matches, the profile takes it. If more than one matches, the user picks. If none match, the editor blocks save with a compatibility message. Changing the harness on an existing profile re-runs resolution; an endpoint that no longer passes clears with a warning.

### Validation

- Every model assignment should be in its endpoint's `models` menu. Warn, don't block, with an explicit override — vendors serve underdocumented models. The worker re-checks at run time and logs.
- Every referenced endpoint (primary and per-agent) must pass the compatibility gate. This blocks save.
- Per-agent `endpoint_id` references live inside JSON, so relational integrity is enforced procedurally: endpoint/provider deletion is blocked while any profile references the endpoint either in `endpoint_id` or inside `backend_config` (SQLite `json_each` scan; profile counts are small).

## The rendering contract

### LaunchContext

```
LaunchContext
  spec              PermissionSpec
  endpoint          Optional[ResolvedEndpoint]        # primary (None = legacy/ambient)
  endpoints         dict[str, ResolvedEndpoint]       # every referenced endpoint by id,
                                                      # primary included
  model_assignments dict[str, Assignment]             # slot name -> (endpoint_id, model);
                                                      # partial — unpinned slots absent
  run_id, ticket_id, workspace_dir, scratch_root, http_port, backend_state
```

`ResolvedEndpoint` is the run-time, decrypted view: protocol_kind, base_url, credential (resolved per source), credential_source, extra, plus the owning provider's id/name/vendor. It replaces the branch's `ResolvedProvider` and stays stdlib-only so the backends package never imports the DB layer.

The worker computes `model_assignments` before launch: enumerate `slots_for(backend_config)`, fill from `default_model` where the full-pin rule applies (compat endpoints), apply `backend_config` overrides, tag each with its endpoint. Slots left unpinned are simply absent.

### Per-protocol dispatch

Each backend owns a dict of renderers keyed by protocol. `prepare_launch` dispatches on the primary endpoint:

```
class ClaudeBackend(Backend):
    descriptor = bc.CLAUDE_SDK
    _renderers = {
        "anthropic":        _render_anthropic,
        "anthropic_compat": _render_anthropic,
    }

    def prepare_launch(self, ctx):
        if ctx.endpoint is None:
            return self._render_legacy(ctx)   # claude_credentials / ambient path
        renderer = self._renderers.get(ctx.endpoint.protocol_kind)
        if renderer is None:
            raise IncompatibleEndpoint(self.code, ctx.endpoint.protocol_kind)
        return renderer(ctx)
```

The null-endpoint path is explicit: legacy CC profiles and ambient-credential runs never reach the dispatch table.

The renderer produces the harness-specific output. The CC anthropic renderer, corrected for credential source and the unpinned state:

```
_SLOT_ENV = {
    "primary":      "ANTHROPIC_MODEL",
    "opus_alias":   "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet_alias": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku_alias":  "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "small_fast":   "ANTHROPIC_SMALL_FAST_MODEL",
    "subagent":     "CLAUDE_CODE_SUBAGENT_MODEL",
}

def _render_anthropic(ctx) -> LaunchPlan:
    ep = ctx.endpoint
    env = {}
    if ep.base_url:
        env["ANTHROPIC_BASE_URL"] = ep.base_url
    if ep.credential:
        if ep.credential_source == "subscription_file":
            env["ANTHROPIC_AUTH_TOKEN"] = ep.credential
        else:
            env["ANTHROPIC_API_KEY"] = ep.credential
    for slot, assignment in ctx.model_assignments.items():
        env[_SLOT_ENV[slot]] = assignment.model      # unpinned slots absent
    env.update(ep.extra.get("env", {}))              # vendor quirks win
    return LaunchPlan(cmd=[...], env=env, ...)
```

This is where "CC + ZAI sets ANTHROPIC_MODEL" lives: a CC-owned rule keyed on protocol, fed by endpoint data. ZAI never tells CC about `ANTHROPIC_MODEL`.

### opencode: one provider block per endpoint

`opencode_config.py` currently renders a single hardcoded `ndprovider` block. Under multi-endpoint profiles it renders one block per referenced endpoint, keyed `nd_<endpoint_id>`:

- provider block: npm package chosen by protocol_kind, baseURL, options from `extra`.
- `OPENCODE_AUTH_CONTENT`: one entry per block (api key or oauth blob per credential_source).
- model strings: `nd_<endpoint_id>/<model>` for the primary, small_model, and each agent.
- agent entries render into opencode's native per-agent config with their own provider/model. Implementation note (verified against opencode docs + SDK 1.17.13 types): per-agent tool restriction renders as the agent's `permission` block — opencode deprecated the agent-level `tools` key this doc originally sketched.

Single-endpoint profiles degenerate to one block; the rendering path is the same.

Subagent lifecycle events: skipped deliberately. opencode models Task-tool children as real child sessions on the SSE stream (`session.created` carries `parentID`), but the parent-side `subtask` message part carries no join key to the child session id, and nightdesk's canonical subagent events require tying back to the specific parent tool_use. Correlation by timing would be fabrication. Until opencode exposes a join key, the OPENCODE descriptor does not claim `SUBAGENTS` and the product degrades per-capability as designed.

### Shared protocol helpers

One thin shared bottom lives in `domain/protocols.py`: the per-protocol conventions no harness should reinvent ("Anthropic Messages authenticates with `x-api-key`; OpenAI Chat with `Authorization: Bearer`"). Renderers call these helpers. The final config shape stays harness-owned.

### Effective-config preview redacts secrets

The existing effective-config preview calls the harness renderer in dry-run mode against the chosen endpoints and assignments. Renderer output contains credentials (`ANTHROPIC_API_KEY`, opencode `apiKey`); the preview layer masks every value sourced from a credential before it reaches HTML. The renderer marks credential-derived keys in the dry-run result so the preview does not keep its own list of secret names.

## Pricing integration

The live pricing chain on `main` (live fetch, on-disk cache, bundled table fallback) is structurally sound. Four changes connect it to the provider model and make cost historical-safe.

### Drop the Anthropic-only filter

`src/nightdesk/domain/pricing.py` hard-filters to `litellm_provider == "anthropic"` (`_ANTHROPIC_PROVIDERS`). This discards every ZAI row. Replace it with per-vendor resolution.

LiteLLM publishes GLM pricing only under reseller keys today (`cloudflare/@cf/zai-org/glm-5.2`, `fireworks_ai/glm-5p2`). The reseller numbers match z.ai's official page, so they serve as cross-check and fallback. The bug is the filter, not the source.

### Vendor-aware resolution

Price resolution is a function of `(vendor, model)`:

- `vendor = anthropic`: LiteLLM anthropic entries plus the bundled Claude table.
- `vendor = zai`: a curated z.ai price set (the GLM models from docs.z.ai), LiteLLM reseller data as cross-check. When only resellers price a model, prefer the source that matches the vendor's published number; absent that, the median reseller — never the cheapest, which systematically underestimates what the user pays.
- `vendor = openrouter`: OpenRouter's `/api/v1/models`, whose `data` shape the normalizer already speaks.
- `vendor = openai`: LiteLLM openai entries. Subscription (Codex) runs price at the metered equivalent, clearly labeled as an estimate.
- `vendor = ollama`: passthrough, no cost.

The bundled `_PRICE_TABLE` in `domain/cost.py` gets seeded with the z.ai rows (cache_write seeded 0 while z.ai's "limited-time free" holds, with a comment that it will flip) so a cold cache is never blank. `PRICES_AS_OF` bumps.

### Model id normalization — at lookup, never at write

The recorded model id is not always clean: CC appends context-variant suffixes (`glm-5.2[1m]`), some sources prefix the vendor (`anthropic/glm-5.2`). The matcher strips both **at lookup time**. `model_used` on the run row stays raw: the `[1m]` suffix is pricing-relevant (long-context tiers carry different rates) and stripping at write time would destroy it. Where a variant has its own published rate, the matcher tries the exact id before the stripped one.

### Stamp prices on the run

A run can span vendors (multi-endpoint profiles) and prices drift. So at launch the worker stamps the run row with a pricing snapshot:

```
Run
  ...existing: input_tokens, output_tokens, cache_read_tokens,
  cache_write_tokens, cost_usd, model_used...
  endpoint_id       FK provider_endpoints.id (nullable)   # replaces provider_id
  pricing_snapshot  JSON (nullable)
    # { "<model_id>": { "vendor": "zai", "input": 1.4, "output": 4.4,
    #                   "cache_read": 0.26, "cache_write": 0.0,
    #                   "source": "live|cache|bundled", "as_of": "..." } }
```

The snapshot covers every model reachable by the profile (all assignments across all endpoints). At run finish, `cost_usd` is computed once from token usage × snapshot and stored. Analytics reads stored `cost_usd`; it never re-derives historical cost from current prices. A price collapse next week does not make last week's runs retroactively cheap — they cost what they cost.

Consequences:

- The harness-reported cost (CC's own estimate, which assumes Claude prices and is wrong for GLM-behind-anthropic_compat) is kept only as a cross-check, not as the stored figure.
- Multi-model runs need per-model token attribution. Transcript usage events carry model ids; the cost pass groups usage by model and prices each group from the snapshot. Aggregate-only tokens fall back to the primary model's rates.
- Historical runs with no snapshot (everything pre-merge) price through the current chain, labeled "estimated at current prices" in analytics. A backfill script can stamp snapshots from the bundled table's history if that ever matters.
- Vendor attribution for analytics comes from the snapshot, not from walking `run.endpoint_id -> provider.vendor` through rows that may since have been edited or deleted. The FK stays for navigation, not for pricing.

## Schema and migration

### Tables

`providers` (slim) and `provider_endpoints` as specified under Layer 1.

`profiles` changes:
- drop the branch's `provider_id` FK; add nullable `endpoint_id` FK to `provider_endpoints`.
- keep `default_model` (fill-all-slots shorthand) and `backend_config` (per-harness blob, now including per-agent endpoint refs).
- legacy `claude_credentials` stays as a fallback until every CC profile has an endpoint, then is removed in a later revision.

`runs` changes:
- `provider_id` becomes `endpoint_id`.
- add `pricing_snapshot` JSON.

### Migration

The branch's `0019_providers.py` descends from `0018_saved_views`. Main has since added `0019_conversation_model`, `0020_ticket_commit_on_finish`, `0021_run_latency`.

No production data has ever seen `0019_providers` (the branch is unmerged), so per the no-throwaway-migrations rule the revision is **reshaped in place** to the final Provider + ProviderEndpoint schema — there is no provider→endpoint data migration for data that does not exist. At merge time it is renumbered to `0022_providers_and_endpoints` with `down_revision = "0021_run_latency"`. Single-head invariant checked before and after. Same collision class as the 0014 divergence; same fix.

One production wrinkle, found by migrating a copy of the live DB: the live database carries **orphan columns** from the abandoned pre-providers branch (`profiles.backend_config`, `backend_secret`, `endpoint_id`; `runs.backend`, `backend_session_*`) that survived the earlier stamp-reconciliation. A plain `ADD COLUMN` collides. 0022 therefore uses inspector guards (create/add only what is absent), making the same revision correct on both clean and reconciled databases. Orphan contents verified harmless (all NULL/empty; `runs.backend` values are compatible `claude_sdk` strings).

## UI surfaces

### Providers page

Lists providers with their endpoints. Create flow: pick a known vendor from the catalog or "custom", pick credential source(s), enter the credential once (seeded into every selected endpoint), confirm endpoints (catalog defaults pre-checked), pull or curate models per endpoint. Endpoints carrying a `harness_lock` render the restriction inline ("restricted to Claude Code by Anthropic's terms"). Edit flow: refresh models, rotate credential (provider-level rotate updates all matching endpoints), add/remove endpoints. Endpoint and provider deletion is blocked while referenced by any profile, including references inside `backend_config`.

### Harness configuration

Minimal. A list of harnesses with their global runtime defaults (binary paths), stored in `ConfigRow`. Reachable from settings, not from the run path.

### Profile editor

The primary configuration surface. Sections:

1. Harness picker. Filters the form to the harness's `group_keys` and slots.
2. Provider picker. Resolves the primary endpoint by protocol intersection plus lock check. Multi-match exposes the choice; no match blocks save with a compatibility message.
3. Model section. Renders `slots_for(backend_config)`. On compat endpoints, defaults to full-pin from `default_model`; a deliberate "customize per slot" action reveals per-slot fields. On first-party endpoints, defaults to unpinned. On `multi_endpoint` harnesses, agent rows carry their own provider picker (same gate per row). Validation against each endpoint's menu, warn-only.
4. Run-shape sections. Tools, filesystem reach, network, permission mode, system prompt, run-token scopes. Unchanged.

### Effective-config preview

Dry-run render against the chosen endpoints and assignments, credentials masked. Shows "CC + ZAI will set ANTHROPIC_MODEL=glm-5.2, ANTHROPIC_BASE_URL=..." before save. The renderer is the single source of truth.

## Code layout

```
src/nightdesk/
  domain/
    providers.py            # Provider + ProviderEndpoint entities, ResolvedEndpoint,
                            # resolve_endpoint(s), credential-source resolution,
                            # compatibility gate, deletion guards (json_each scan).
    backend_capabilities.py # protocol_kinds (renamed), multi_endpoint, ModelSlot,
                            # slots_for hook.
    protocols.py            # (new) shared per-protocol auth/shape helpers.
    pricing.py              # vendor-aware resolution, drop Anthropic-only filter,
                            # lookup-time normalization, snapshot builder.
    cost.py                 # bundled table seeded with z.ai rows; snapshot-based
                            # cost computation.
  backends/
    base.py                 # LaunchContext: endpoints map + partial assignments.
                            # Renderer dispatch with null-endpoint path.
    claude_code.py          # anthropic/anthropic_compat renderer, slot-env map,
                            # credential_source branch, legacy path.
    opencode.py             # renderer dispatch, agents support.
    opencode_config.py      # per-endpoint provider blocks, per-agent rendering.
    opencode_translate.py   # surface subagent lifecycle events to transcript.
    registry.py             # unchanged.
  api/routes/
    providers.py            # provider + endpoint CRUD, model-pull action.
    profiles.py             # endpoint resolution, per-assignment validation.
  db/models.py              # Provider, ProviderEndpoint; profile + run changes.
alembic/versions/
  0022_providers_and_endpoints.py   # 0019 reshaped in place, renumbered at merge.
```

## State of the branch (gap analysis)

Ships and keeps:
- Clean Backend interface and registry; `run_one.py` free of backend branching.
- opencode backend driving a localhost HTTP server.
- Capability descriptors and field groups.
- The `claude_code.py` credential-source branch (AUTH_TOKEN vs API_KEY) — the first draft's renderer pseudocode regressed it; the code was right.

Ships but needs revision:
- `Provider` is a single kind + base_url + credential. Splits into slim Provider + ProviderEndpoint with per-endpoint credentials.
- `provider_kinds` renames to `protocol_kinds`; `claude_subscription` kind is deleted in favor of credential_source + harness_lock.
- `claude_code.prepare_launch` moves to renderer dispatch with an explicit legacy path.
- `opencode_config.render_config`: single `ndprovider` block becomes one block per endpoint; `model`/`small_model` decouple; per-agent (endpoint, model) config.
- Migration `0019_providers`: reshape in place, renumber to 0022 at merge.

Missing entirely:
- ProviderEndpoint table, vendor/protocol/credential-source split, harness_lock.
- `openai_codex` protocol kind and its opencode rendering.
- Multi-endpoint profiles: per-agent endpoint refs, per-assignment gate, multi_endpoint flag.
- `slots_for` dynamic slot enumeration; unpinned-state rendering.
- Known-vendor catalog and model-pull action.
- Pricing: filter drop, vendor resolution, lookup-time normalization, run pricing snapshots, per-model attribution, z.ai bundled rows.
- opencode subagent lifecycle in the translator.
- Compatibility enforcement in editor and worker; deletion guards over backend_config.
- Effective-config dry-run against chosen endpoints, with credential masking.
- Ambient credential discovery (Future Work).

## Sequence of work

The branch stays held back until provider work and harness breakout mature and are tested. The MR is cohesive: the provider/endpoint split and the pricing tie-in land together, because the value proposition ("CC harness plus ZAI provider, priced correctly") requires both. Pricing is not a follow-up.

Scope boundary, decided (2026-07-05): **one MR ships everything** — schema, single- and multi-endpoint behavior, opencode multi-provider rendering, per-agent pickers, per-model cost attribution. The MR bases on the UI ground-up rebuild branch (React SPA + JSON API) and presumes that branch merges first; all UI surfaces are built in the new frontend, not the legacy HTMX templates.

Build order:

1. Provider + ProviderEndpoint tables; reshape and renumber the migration.
2. Endpoint resolution, protocol intersection + harness_lock gate, in editor and worker.
3. Model slots (`slots_for`), unpinned state, per-harness `backend_config` shape, CC as reference.
4. Renderer dispatch contract; CC anthropic/anthropic_compat renderer with credential-source branch and legacy path.
5. Pricing: drop the filter, vendor resolution, lookup-time normalization, bundled z.ai rows, run pricing snapshots + stored cost.
6. Credential source resolution (api_key, oauth_file, subscription_file, env_var) in the worker.
7. opencode: per-endpoint provider blocks, per-agent endpoint config, subagent translator, per-model cost attribution.
8. UI (React SPA): providers page, harness settings, profile editor model section with per-agent provider pickers, effective-config preview with masking.

blubblub integration rides on the same contract when it lands. Discovery is out of scope.

## Future work

### Ambient credential discovery

The credential source discriminator is the foundation. Discovery scans standard disk locations and environment variables and offers to register providers without manual entry. Candidate sources:

- `~/.codex/auth.json` for Codex OAuth.
- `~/.config/opencode/auth.json` for opencode-stored credentials.
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` in the environment.
- A locally running `ollama` daemon.

A found credential is matched against the known-vendor catalog, offered as a draft Provider row, and only persisted on explicit user confirmation. nightdesk never auto-registers credentials without consent. The scan is opt-in and runs from the providers page.

### Harness-specific configuration pages

Each supported harness earns a tailored configuration page rather than a generic form. CC's page knows alias semantics and renders the full-pin guidance. opencode's page renders named agents with per-agent provider pickers. blubblub's page renders whatever its integration defines. The generic layer is the capability descriptor and the renderer contract; the form is owned by the harness.

### Subscription and OAuth run-time refresh

`oauth_file` and `subscription_file` credentials expire. The run-time resolver detects a stale token and either refreshes it (Codex OAuth carries a refresh token nightdesk can exercise) or fails the run with a clear "re-authenticate" message pointing at the endpoint row.

### Pricing source expansion

The vendor-to-source map grows over time. OpenRouter and LiteLLM cover most resellers today. Vendors with no machine-readable price page carry a curated set in the bundled table, refreshed on release via `scripts/update_prices.py`.

## Decisions log

Resolved with the user, 2026-07-05:

1. **Codex OAuth from third-party harnesses is an approved pattern.** opencode supports ChatGPT Plus/Pro login today; blubblub does too. Modeled as protocol kind `openai_codex`, no harness lock.
2. **Claude subscription is claude_sdk-only**, per Anthropic's ToS. Modeled as `harness_lock=claude_sdk` on the subscription endpoint, not as a pseudo-protocol.
3. **Credentials live on endpoints.** Provider is vendor identity + pricing anchor.
4. **Profiles can span providers** on multi-endpoint harnesses: one harness, primary endpoint + per-agent endpoints.
5. **Run cost is stamped at run time.** Price drift never rewrites historical run costs.
6. nightdesk mirrors **opencode's provider options** as the compatibility target; blubblub tracks the same set.

## Reference data

GLM pricing from z.ai's pricing page, used for the bundled table seed and the `vendor = zai` resolution target.

| model | input / M | cached input / M | output / M | cache write |
|---|---|---|---|---|
| glm-5.2 | 1.40 | 0.26 | 4.40 | limited-time free (seed 0) |
| glm-5.1 | 1.40 | 0.26 | 4.40 | limited-time free (seed 0) |
| glm-5 | 1.00 | 0.20 | 3.20 | limited-time free (seed 0) |
| glm-5-code | 1.20 | 0.30 | 5.00 | limited-time free (seed 0) |

These match the reseller `glm-5.2` rows in LiteLLM, confirming reseller data is a valid cross-check when the first-party vendor tag is absent.

## Sources

- z.ai pricing: https://docs.z.ai/guides/overview/pricing
- opencode providers (auth options, chat-completions vs responses split): https://opencode.ai/docs/providers/
- opencode agents: https://opencode.ai/docs/agents/
- blubblub provider model: `~/fun/blubblub/main/src/config/providers.rs` (`ProviderKind::{OpenaiCompat, OpenaiCodex, Anthropic}`), `src/agents/spawn.rs` (`resolve_child_provider_model`)
- LiteLLM community price file: https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
