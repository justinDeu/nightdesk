# Access Tokens and Permissions for Agents

Status: design (2026-07-08). Studied read-only on `integration-session-suite`.
Companion groundwork: `docs/design/session-suite/resident-agents-v3.md` (per-agent env),
`src/nightdesk/domain/run_tokens.py` (prior art), memory item "agent action provenance".

## 1. Problem

nightdesk is agent-driven software, but its auth model is binary. The admin bearer in
`~/.config/nightdesk/config.toml` grants everything, and the `nightdesk-api` skill tells
every agent to read it:

```bash
TOKEN=$(awk -F\" '/^bearer_token/ {print $2}' ~/.config/nightdesk/config.toml)
```

Any local Claude Code agent that can read a file is an admin. It can rewrite provider
credentials, edit profiles to grant future runs scopes, schedule arbitrary cron prompts,
mint a browser handshake, and delete tickets. The bearer is also structurally load-bearing:
it is the session-cookie signing key (`api/auth.py:60`) and the Fernet key for profile and
provider secrets (`domain/profile_secrets.py:_derive_key`), so it cannot simply be hidden.

Requirements from the owner:

1. Agents get separate, scoped-down tokens.
2. Permissions are asymmetric per agent; some actions are human-only or per-agent-granted.
3. An agent must never be able to read any other token.
4. No plaintext long-lived secrets in files agents are told to read.

## 2. What exists today (findings)

### 2.1 Principals and dependencies (`api/auth.py`)

- `AdminPrincipal` — anyone presenting the config bearer or a signed cookie. Omnipotent.
- `RunPrincipal` — resolved from an `ndr_` run token (sha256-hash lookup in `run_tokens`).
- Deps: `require_bearer` (auth routes only), `require_token_cookie_or_bearer` (every other
  router), `require_principal` / `require_admin` / `require_scopes` (only `runs.py` uses
  `require_scopes`), `enforce_self_ticket`.
- The admin compare is `provided == token_value` — not `secrets.compare_digest`. Minor,
  fix in passing.

### 2.2 Run tokens (`domain/run_tokens.py`) — the prior art

Everything we want, in miniature: `ndr_` + 32 random bytes, only sha256 hash persisted,
expiry = run duration + grace, revocation column, `scopes` list, `scope_data`
self-constraint (`ticket_id`, `create_profile_allowlist`). `SELF_SCOPES` auto-granted;
`GRANTABLE_SCOPES = ("ticket.create",)` is declared and settable on profiles
(`Profile.run_token_scopes`, validated in `routes/profiles.py:46`) but **no route consumes
`ticket.create` yet** — tickets POST only accepts cookie/bearer, so a run token 401s there.
The only scope-enforced surface is the k8s write-back trio in `routes/runs.py`
(`/runs/{rid}/transcript|diff|result`, `require_scopes` + `enforce_self_ticket`).

### 2.3 Enforcement coverage

All 20 routers except `runs.py` and `auth.py` gate every route (reads and writes alike)
with `require_token_cookie_or_bearer` only. Route inventory that a permission model must
cover: tickets (44 routes: CRUD, bulk ops, reorder, transitions, run-now/cancel/requeue,
continue/resume/retry/restart/clone, steer queue, dependencies, next-run-context,
additional-dirs, delete), runs read (+diff/log), transcripts (ticket + run SSE), review
comments (incl. `request-changes`), projects, labels, saved views, search, inbox
(promote/decline), profiles (CRUD/copy/export/import), providers + endpoints
(rotate-credential, pull-models), config (+schedule windows, worker status), cron jobs
(incl. fire-now-and-run), analytics, sessions→agents (messages, end, env, pending
answers per resident-agents v3), fs/suggest, backends, effective-config, auth
(mint-handshake).

### 2.4 Plaintext-at-rest inventory

| Location | What | Readable by local agent? |
|---|---|---|
| `~/.config/nightdesk/config.toml` (0600) | admin bearer | yes — same UID |
| `~/.claude/skills/nightdesk-api/SKILL.md` | *instructions* to read it | yes — and it teaches the read |
| Handshake URL (`/auth/handshake?token=`) | one-shot, 60s, admin-mint-only | transient; fine |
| `NIGHTDESK_RUN_TOKEN` in run env (`run_one.py:644`) | scoped, hours-lived | by the run itself only (by design) |
| Run transcripts | token IF the agent echoes `env` | yes, and transcripts are served over API |
| DB (`run_tokens`, profile/provider secrets) | hashes / Fernet ciphertext | no cleartext |

The admin bearer never rides a run's env (good). The two real problems are the config
file plus the skill that points agents at it, and the absence of anything between
"run-scoped for hours" and "admin forever".

### 2.5 Provenance groundwork

`DiffComment.author_kind` (`'admin'|'agent'`) + `author_run_id` (`db/models.py:520`) is
the only acting-principal record. There is no events/audit table; ticket transitions
record nothing about who performed them.

## 3. Token model

### 3.1 `ndk_` durable tokens

New table `api_tokens` (naming below intentionally matches `run_tokens` where the
concepts coincide):

```python
class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: str                       # uuid — stable handle for UI/audit (hash rotates on re-mint)
    token_hash: str               # sha256(cleartext), unique index — the lookup key
    prefix_hint: str              # first 12 chars ("ndk_3fA9…") for the list UI
    name: str                     # unique among non-revoked ("pm-agent", "reviewer-bot")
    kind: str                     # 'agent' (durable) | 'run' (ephemeral; §7)
    scopes: list[str]             # expanded scope list (JSON)
    bundle: Optional[str]         # preset it was minted from, display-only
    scope_data: dict              # restrictions: profile_allowlist, project_allowlist,
                                  #   ticket_id (run kind)
    run_id / ticket_id: Optional  # run kind only
    created_at, expires_at (nullable for agent kind), revoked_at
    last_used_at: Optional[datetime]   # coarse: update at most once/minute
    created_by: str               # 'admin' — future-proofing, only admin can mint
```

- Cleartext = `"ndk_" + secrets.token_urlsafe(32)`, shown exactly once at mint, never
  persisted or logged. Lookup by sha256 hash is inherently constant-time-safe for
  256-bit random tokens (no need for bcrypt/argon2 — those defend low-entropy passwords;
  see §11). Fix the admin compare to `secrets.compare_digest` while in the file.
- Revocation is immediate (row flag, checked at resolution like `ndr_` today).
  Expiry optional for agent tokens; default none, UI nudges toward 90d.

### 3.2 The admin bearer stays special (for now)

Do **not** convert the config bearer into an `api_tokens` row in phase 1. It is the
cookie-signing key and the Fernet key; making it a hashed row means the server can no
longer derive those keys from it. Decision: the bearer remains the root credential —
boots the server, signs cookies, encrypts secrets, and is the only credential that can
mint/revoke tokens or mint browser handshakes. Everything else in this doc exists so
that **no agent ever needs it again**. Decoupling the crypto keys from the bearer (a
separate server-side `secret_key` in config, letting the bearer rotate cheaply or become
a row) is Phase 3 (§10) — worth doing, not blocking.

Hard rule that falls out: **an `ndk_` token can never obtain a session cookie.**
`/auth/mint-handshake` and `/auth/login` accept the root bearer only. Cookie = admin;
that stays a human-browser artifact.

## 4. Permission model

### 4.1 Scopes: `resource.action`, with named bundles on top

Recommendation: fine-grained `resource.action` scopes as the enforcement primitive,
plus server-defined **bundles** (named scope sets) as the minting UX. Roles-only is too
coarse for requirement (b) (per-agent asymmetry); scopes-only makes the mint dialog a
40-checkbox wall. Bundles expand to a scope snapshot at mint time (stored in
`scopes`; `bundle` kept for display). Snapshot, not live reference: a token's power must
not change because someone edited a preset — that is a silent grant to every holder.
The tokens page offers "re-sync to bundle" per token when a preset changes.

### 4.2 Scope taxonomy

Grouped by router surface. `read` covers GET/SSE; write actions split where the risk
differs.

| Scope | Covers |
|---|---|
| `tickets.read` | tickets GET/list, dependencies GET, effective-config, inbox GET, search, saved views read, backends list |
| `tickets.create` | POST /tickets (subject to `profile_allowlist` restriction, §4.4) |
| `tickets.update` | PATCH ticket fields, labels, priority, project, steer queue, next-run-context, dependencies write, reorder, additional-dirs |
| `tickets.transition` | /transition, promote/decline, send-to-inbox, bulk status |
| `tickets.run` | run-now, cancel, requeue, continue, resume, retry, restart, new-conversation, cron fire-now-and-run |
| `tickets.archive` | archive/unarchive (single + bulk) |
| `tickets.delete` | DELETE /tickets/{tid} |
| `runs.read` | runs list/show/diff/log, run + ticket transcripts |
| `comments.read` / `comments.write` | review comments; `request-changes` counts as write |
| `projects.read` / `projects.write` | projects CRUD |
| `labels.write` | label entity CRUD (reading rides `tickets.read`) |
| `profiles.read` | profiles GET/export (with `claude_credentials`/env secrets masked, as the UI already gets) |
| `profiles.write` | profile CRUD/copy/import — **human-only** (§4.3) |
| `providers.read` | provider/endpoint metadata, catalog (credentials never returned to anyone) |
| `providers.write` | provider CRUD, rotate-credential, endpoint edits — **human-only** |
| `config.read` / `config.write` | config + schedule windows + worker status; write **human-only** |
| `cron.read` / `cron.write` | cron CRUD/enable/disable; write **human-only by default** (§4.3) |
| `agents.read` | resident agents list/detail/transcript |
| `agents.message` | POST messages / answer pending / interrupt on resident agents — **human-only by default** |
| `agents.admin` | create/end/env/restart-runtime — **human-only** |
| `analytics.read` | analytics endpoints |
| `fs.read` | fs/suggest (host path enumeration — deliberately not in any default bundle) |
| `tokens.admin` | mint/revoke/list tokens — **never mintable** (root bearer only) |

Self-scoped variants keep the existing semantics: `runs.write.self` (transcript/diff/
result write-back), `tickets.read.self`, `tickets.update.next_run_context.self` — valid
only with `scope_data.ticket_id`, enforced by `enforce_self_ticket`. Existing
`SELF_SCOPES` strings are aliased to the new names at resolution so in-flight run tokens
survive the deploy.

### 4.3 Human-only actions and why (escalation closure)

A server-side constant `HUMAN_ONLY_SCOPES`; the mint endpoint rejects them, so no token
can ever hold one — the admin bearer/cookie is the only principal that passes. Each entry
closes a concrete escalation path:

- `profiles.write` — a profile carries `run_token_scopes`, env (secrets), `system_prompt`,
  `disallowed_tools`, model/provider binding. Editing a profile escalates every future run
  on it: grant-myself-scopes, exfiltrate via env, defang the headless guard. Closed.
- `cron.write` — a cron job is "run this prompt on schedule with this profile": arbitrary
  persistent code execution. Human-only *by default*; if a scheduling agent ever needs it,
  it is an explicit per-token grant plus a `profile_allowlist` restriction, never in a
  bundle. (`cron.read` and `fire-now-and-run` under `tickets.run` remain grantable — firing
  an existing human-authored job is much weaker than authoring one.)
- `config.write` — schedule windows, worker knobs, future global session settings. Closed.
- `providers.write` — credential replacement/exfil paths, endpoint URL redirection (point
  the endpoint at an attacker proxy and harvest the real key). Closed.
- `agents.message` / `agents.admin` — a resident agent runs in trusted posture on the real
  `~/.claude`. Messaging one is prompt injection into a full-power agent; editing its env
  or restarting its runtime is worse. Human-only by default; `agents.message` is the one
  designed for deliberate per-agent grants (agent-to-agent handoff is a real future want —
  grant it token-by-token with eyes open).
- `tickets.delete` — destructive, no agent workflow needs it (archive exists). Closed.
- `tokens.admin` — a token that mints tokens is admin. Never a scope; the mint/revoke
  routes use `require_bearer` (root only), same tier as `mint-handshake`.

### 4.4 Restrictions (`scope_data`)

Generalize the run-token pattern to durable tokens:

- `profile_allowlist: [ids]` — constrains `tickets.create` and any granted `cron.write`.
  Direct generalization of `create_profile_allowlist`.
- `project_allowlist: [ids]` — constrains ticket/run/comment reads and writes to listed
  projects (this node hosts foreign projects — blubblub/backtest/shop; a nightdesk-focused
  agent should not read their tickets). Enforced in the domain query layer, not per-route.
- run kind only: `ticket_id` self-constraint, unchanged.

### 4.5 Default bundles

| Bundle | Scopes | Cannot |
|---|---|---|
| `observer` | all `*.read` except `fs.read`, `profiles.read` | write anything |
| `reviewer` | observer + `comments.read/write` | transition, run, create |
| `pm-agent` | `tickets.read/create/update/transition/archive`, `runs.read`, `comments.*`, `projects.read`, `labels.write`, `analytics.read` | run-now, delete, profiles, providers, config, cron, agents |
| `operator` | pm-agent + `tickets.run`, `cron.read` | authoring cron, profiles, providers, config |

`pm-agent` deliberately includes `archive` but excludes `tickets.run`: queueing a ticket
already causes execution during schedule windows, which is the product's point — the
line drawn is *immediate, out-of-window* execution (`run-now`) stays with `operator`.

## 5. Enforcement

### 5.1 One dependency to rule the routers

Today's `require_token_cookie_or_bearer(bearer_token)` is closed over only the bearer
string; scope checks need the engine. Replace the router-construction signature
mechanically (app.py already threads `get_session`/`engine` to `runs.py`):

```python
scoped = make_scoped(bearer_token, engine)   # built once in app.py, passed to build_router

# in a router:
read  = Depends(scoped("tickets.read"))
trans = Depends(scoped("tickets.transition"))
```

Resolution order inside `scoped(*needed)`:

1. Signed session cookie valid → `AdminPrincipal` (browser keeps full power, zero UX change).
2. Bearer == root token (compare_digest) → `AdminPrincipal`.
3. `ndk_`/`ndr_` prefix → hash lookup in `api_tokens` (unrevoked, unexpired) →
   `TokenPrincipal(id, name, kind, scopes, scope_data)`; check `needed ⊆ scopes`;
   touch `last_used_at` (rate-limited).
4. Otherwise 401.

`TokenPrincipal` replaces/absorbs `RunPrincipal` (same fields plus name/kind);
`enforce_self_ticket` unchanged. `require_scopes` in `runs.py` becomes a thin call into
the same resolver — one code path for every authenticated request.

### 5.2 Migration path across 20 routers

Default-deny falls out for free: an `ndk_` token presented to a router still on
`require_token_cookie_or_bearer` fails the literal-bearer compare and 401s. So rollout is
incremental and safe — un-migrated routes are *inaccessible* to agent tokens, never
over-accessible. Order by agent value: tickets → runs/transcripts → comments →
projects/labels/search/inbox → analytics → the human-only routers last (they only ever
need `scoped()` for their read halves). Router-level dep carries the read scope;
write routes stack their action scope per-route (the `runs.py` split-router pattern
already demonstrates mixing auth tiers in one file).

### 5.3 403 shape — agents must self-diagnose

```json
{
  "detail": "missing scope",
  "missing_scopes": ["tickets.transition"],
  "token": "pm-agent",
  "hint": "This token lacks 'tickets.transition'. An operator can grant it in Settings → Access tokens."
}
```

401 stays `{"detail": "invalid token"}` (never distinguish unknown/revoked/expired to
the caller). Human-only scopes 403 with `"hint": "human-only action; requires the admin session"`
so an agent stops retrying and asks its human. The `nightdesk-api` skill documents both
shapes.

### 5.4 OpenAPI

Tag each route with its scope (FastAPI dependency metadata → `openapi_extra`). Agents
already discover the surface via `openapi.json`; publishing required scopes there means
an agent can predict a 403 before making the call.

## 6. Secret hygiene

### 6.1 Server side

- Only sha256 hashes at rest; cleartext exists in one HTTP response ever.
- Logging: middleware never logs Authorization; `_mask_token` (cli.py:1266) pattern reused
  anywhere a token could surface.

### 6.2 Client side — env vars, not files

- **Ticket runs**: unchanged, `NIGHTDESK_RUN_TOKEN` injected per run, hours-lived,
  self-scoped, revoked at run end (`revoke_for_run`).
- **Resident agents**: the v3 env panel (`Session.env`, secret values Fernet-encrypted,
  write-only in the UI, decrypt-and-merge at spawn) is the distribution channel. Each
  resident agent gets its own `ndk_` token injected as `NIGHTDESK_TOKEN` (secret:true).
  Apply-and-restart (`POST /agents/{id}/restart-runtime`) was designed exactly for
  "hand the agent a fresh token mid-conversation".
- **The user's local Claude Code** (the original offender): skills change to
  `TOKEN="${NIGHTDESK_TOKEN:?export a scoped token, see Settings → Access tokens}"` and
  **stop mentioning config.toml**. The user exports a scoped `ndk_` token in their shell
  env. Per project CLAUDE.md, both `nightdesk-api` and `nightdesk-ticket-ops` skills must
  be updated in the same change that ships the feature.

Honesty about the last hop: an exported var in `~/.zshrc` is still agent-readable, and a
keyring is not realistic here — `secret-tool`/KWallet need a D-Bus session and an unlock
prompt; this server is headless and the consumer is a non-interactive agent, so an OS
keychain either blocks or auto-unlocks into equivalence with a file. systemd user
credentials (`systemd-creds`) are the least-bad upgrade but the daemons are currently
orphaned setsid processes, not units. The design point is therefore: **what an agent can
find is a scoped token whose blast radius the human chose** — not the admin bearer. The
remaining exposure of the admin bearer to same-UID trusted agents is an accepted risk in
Phase 1 and has a real fix in deployment hardening (§10 Phase 3: run the API under its
own UID so `config.toml` 0600 actually excludes agents).

### 6.3 Token isolation (requirement c)

- **API**: `GET /api/v1/tokens` returns metadata + `prefix_hint` only. There is no
  endpoint that returns a stored token value, ever — not for admin, not for anyone
  (values aren't stored; the schema makes the leak impossible rather than forbidden).
- **Cross-token**: `tokens.admin` is unmintable, so no token can list even the metadata
  of other tokens.
- **Filesystem/DB**: hashes only. Resident-agent env secrets are Fernet-encrypted; the
  API returns `{key, secret:true, set:true}` never values.
- **Transcripts** (audited): nothing injects tokens into transcripts, but an agent that
  runs `env` in Bash echoes its own `NIGHTDESK_RUN_TOKEN`/`NIGHTDESK_TOKEN` into a
  transcript that is stored and served (`/runs/{rid}/transcript`) to anyone with
  `runs.read`. Today that leaks only a self-scoped short-lived `ndr_`; once durable
  tokens ride resident-agent envs it leaks real capability. Mitigation (Phase 2): redact
  `\bnd[kr]_[A-Za-z0-9_-]{16,}\b` in `transcript.append_events` and the SSE tail path —
  cheap, prefix-keyed, no false-positive surface worth worrying about. Note the limit:
  redaction cleans *our* store; the token also sat in the model's context and could be
  written anywhere the agent can write. Scoping, expiry, and revocation are the real
  controls; redaction is hygiene.

## 7. Unifying `ndr_` and `ndk_`

One vocabulary, one resolver, one table.

- `api_tokens.kind = 'run'` rows replace `run_tokens`: `issue_run_token` writes there
  with `kind='run'`, `run_id`/`ticket_id`/`expires_at` set, scopes = renamed SELF_SCOPES
  (+ profile grants), `scope_data.ticket_id` self-constraint intact. Prefix stays `ndr_`
  — the prefix signals lifecycle to humans reading logs.
- `resolve_run_token` folds into `resolve_token` (dispatch on prefix is unnecessary —
  hash lookup finds the row; the prefix is cosmetic).
- Migration: additive revision creating `api_tokens`; switch issuance; leave `run_tokens`
  rows to expire (hours), keep the old resolver reading the old table for one release,
  drop the table in the next revision. No data migration needed — run tokens are
  ephemeral by construction.
- `Profile.run_token_scopes` validation widens from `("ticket.create",)` to the grantable
  subset of the new taxonomy (still excluding HUMAN_ONLY and anything non-self without a
  restriction). This also finally makes `ticket.create` *work* — the tickets POST route
  gains `scoped("tickets.create")`, which run tokens holding the grant now pass.

## 8. UX

### 8.1 Settings → Access tokens

- List: name, bundle chip, scope count (expandable), `prefix_hint`, created, expires,
  last used ("3m ago" / "never"), revoke button (confirm dialog names the token).
- Mint dialog: name; bundle preset radio (expands into a grouped scope checklist the user
  can then tweak — presets are starting points, not cages); optional expiry; optional
  profile/project allowlists; human-only scopes rendered as a visible-but-disabled group
  labeled "admin session only" (teaches the model instead of hiding it).
- One-time reveal: full token + copy button + "you will not see this again"; a ready-made
  `export NIGHTDESK_TOKEN=…` snippet.
- Revoked tokens stay listed (struck through) for 30 days — audit continuity for
  `last_used_at` questions.

### 8.2 Per-agent assignment (resident agents)

In the v3 env panel, a "Nightdesk access" row: pick bundle → one click mints a token named
after the agent, stores it as secret env `NIGHTDESK_TOKEN`, offers apply-and-restart.
Convention: **one token per agent, named for it** — revocation then maps 1:1 to "cut that
agent off", and `last_used_at` tells you which resident is actually calling home. Ending
an agent prompts to revoke its token.

### 8.3 Provenance (ties into agent-action-provenance groundwork)

Extend the `DiffComment.author_kind` pattern rather than inventing an audit framework:

- `author_kind` values become `'admin' | 'run' | 'token'`; add nullable `author_token_id`
  beside `author_run_id`.
- Ticket transitions get the same stamp: `Ticket.last_transition_actor_kind` +
  `last_transition_token_id` (or, better, on the Conversation/status-change write —
  wherever the transition lands today). This is the cheap "record the acting principal"
  the owner asked to feel out, without committing to an events table.
- The full `ticket_events` audit trail remains open (§12) — the stamps above are designed
  to backfill into it if built.

## 9. Threat model

Assume a fully compromised (prompt-injected or misbehaving) agent holding each bundle.
"Host fs access" means the agent also has filesystem tools on this box as the same UID —
true for local CC and trusted resident agents, false for k8s pods and remote callers.

| Holder | Worst case via API | Closed | Accepted |
|---|---|---|---|
| `observer` | reads all tickets/runs/transcripts/analytics (transcripts can contain anything past runs saw) | any write | read exposure of transcript corpus |
| `reviewer` | + spam/false review comments, `request-changes` noise | transitions, runs | comment vandalism (revocable, attributed) |
| `pm-agent` | + create/transition/archive floods; create tickets with hostile prompts **within its profile allowlist** that execute in-window | run-now, delete, profile choice (allowlist), cron, config, providers | a queued hostile ticket runs later with the allowed profile's power — the allowlist choice IS the blast-radius knob |
| `operator` | + immediate execution (run-now), firing existing cron jobs | authoring cron/profiles, providers, config | immediate execution of anything it may create |
| any + host fs | read `~/.config/nightdesk/config.toml` → root bearer → everything | — (Phase 1) | **the** accepted risk: same-UID fs makes API scoping moot for on-box agents; fixed only by split-UID deployment or sandboxed posture (§10) |

Escalation paths explicitly closed: token→cookie (handshake/login root-only);
token→token (`tokens.admin` unmintable); profile edit (human-only); cron authoring
(human-only default); provider/endpoint edit incl. credential-harvest-via-endpoint-URL
(human-only); resident-agent puppeteering (human-only default); scope self-expansion
(scopes immutable post-mint — change = revoke + re-mint, so every escalation is a human
act in the UI).

Framing for the doc's readers: scoping limits blast radius, it does not grant
omniscience-proofing. An agent with read access to the user's home can read whatever the
user left there. The wins are real anyway: off-box callers (k8s pods, future remote
agents) are fully contained; on-box agents no longer *need* the root bearer, so skills,
examples, and habits stop teaching the worst path; and revocation/attribution exist.

## 10. Phasing and sizing

**Phase 1 — tokens work (M, ~3-4 focused runs)**
`api_tokens` model + additive migration; `domain/api_tokens.py` (mint/resolve/revoke,
generalizing `run_tokens.py`); `auth.py` rework (`TokenPrincipal`, `make_scoped`,
compare_digest, 403 shape); thread `engine` through `app.py` router construction;
migrate tickets/runs/transcript/comments/search/inbox/projects/labels/analytics routers
to `scoped()`; `routes/tokens.py` (root-bearer-only mint/list/revoke); Settings → Access
tokens page; HUMAN_ONLY constant + bundles; skills rewrite (`NIGHTDESK_TOKEN` env,
403-shape docs, delete the awk recipe) — same-change per CLAUDE.md. Tests: resolver,
scope deny/allow per router tier, human-only mint rejection, cookie unaffected, 403 body.

**Phase 2 — distribution + provenance (M)**
Run-token unification (`kind='run'`, widen `run_token_scopes` validation, wire
`tickets.create` for run tokens, drop `run_tokens` next revision); resident-agent env
panel token assignment + revoke-on-end; `project_allowlist`/`profile_allowlist`
enforcement in the query layer; transcript token redaction; `author_kind='token'` +
transition actor stamps; OpenAPI scope annotations.

**Phase 3 — root hardening (L, optional, independent)**
Separate `secret_key` in config for cookie signing + Fernet (bearer rotation stops
invalidating cookies/secrets; bearer can then become a revocable row itself);
split-UID deployment option (API/worker under a service user, `config.toml` unreadable
to agent UID) — pairs with the systemd migration the restart-instance runbook already
wants; sandboxed resident-agent posture (reserved in v3) as the trusted-posture fix.

## 11. Rejected alternatives

- **JWT / PASETO stateless tokens** — revocation requires a denylist, which is a table,
  which is what we already have; stateless buys nothing on a single-node SQLite app and
  loses `last_used_at`. The `ndr_` hash-lookup pattern is proven here.
- **OAuth2/OIDC** — single-human system; there is no third party to delegate to. The
  scope *concept* is borrowed; the protocol machinery is dead weight.
- **Roles-only (no scopes)** — cannot express per-agent asymmetry or restrictions
  (profile/project allowlists); every novel agent shape would demand a new role in code.
- **bcrypt/argon2 for token hashes** — KDFs defend low-entropy secrets; these are 256-bit
  random strings, where sha256 preimage resistance is the bound. Slow hashing would just
  tax every API request.
- **OS keychain for client tokens** — headless box, non-interactive consumers; a keyring
  that auto-unlocks for the agent's UID is a file with extra steps (§6.2).
- **mTLS client certs** — strong, but hostile to the curl-from-skill ergonomics the whole
  system is built on, and cert issuance/revocation UX would dwarf the tokens page.
- **Per-route ACL table (dynamic permissions)** — scope-to-route mapping in code, not
  data; auditable in a diff, no admin UI for editing the permission *system* (which would
  itself be a human-only-scope headache).

## 12. Open questions

1. **`agents.message` grant shape** — when agent-to-agent messaging arrives, is a global
   scope enough, or does it need a target allowlist (`scope_data.agent_allowlist`) so
   agent A may message B but not C? Leaning allowlist; defer until the first real want.
2. **Transition actor storage** — stamp columns on the ticket/conversation (cheap, latest
   only) vs a `ticket_events` table (full history, enables activity feeds the UI wants
   anyway). Owner said "feel it out"; Phase 2 ships stamps, events table decided later.
3. **`fire-now-and-run` placement** — bundled under `tickets.run` here; arguably it
   deserves `cron.fire` if firing human-authored jobs should be grantable separately from
   ticket execution.
4. **Bearer-as-row endgame** — after Phase 3's key split, does the config bearer become a
   `kind='root'` row (revocable, last-used-visible) or stay a config literal? Row is
   cleaner; needs a bootstrap story for "all tokens revoked, recover access".
5. **Rate limiting / anomaly flags per token** — `last_used_at` is the hook; is a
   per-token request counter + "unusual burst" surfacing worth Phase 2, or noise?
6. **Foreign projects on this node** — should `project_allowlist` be *required* (not
   optional) for non-observer bundles on multi-project nodes? Default-open is simpler;
   default-scoped is safer. Currently optional.
