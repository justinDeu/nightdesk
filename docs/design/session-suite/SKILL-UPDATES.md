# Skill updates — session-suite round 2 (apply at merge-to-main)

Consolidates the four feature branches' skill deltas into one apply-guide. Per
the repo CLAUDE.md, `~/.claude/skills/nightdesk-api/SKILL.md` and
`~/.claude/skills/nightdesk-ticket-ops/SKILL.md` must be updated in the same
change that ships an API surface. This worktree cannot write `~/.claude`, so the
edits are captured here and applied at merge-to-main.

The per-branch files stay in place for their full replacement text:

- `docs/design/SKILL-UPDATES-token-perms.md` — auth model rewrite (the big one).
- `docs/design/SKILL-UPDATES-ack-flow.md` — ack endpoints + `description`.
- `docs/design/SKILL-UPDATES-gitlab.md` — the `/api/v1` integrations family.
- resident-agents' needs are captured inline below (no per-branch file was cut).

Read those for the verbatim text. This file records the **order to apply them**
and the **cross-feature reconciliations** the isolated per-branch files could
not see. Where they disagree, this file wins.

## Apply order

1. token-perms auth rewrite first — it changes how EVERY recipe authenticates
   (stop reading the admin bearer from `config.toml`; export a scoped
   `NIGHTDESK_TOKEN`; document the 401/403 shapes). All later recipes assume it.
2. ack-flow — ack endpoints, `description`, the `acknowledged` filter and the new
   `TicketOut` fields (`acknowledged_at/by`, `archived_at`, `agent_reviewed`,
   `description`).
3. gitlab — the additive `/api/v1` integrations family.
4. resident agents — the `/api/v1/agents` family (admin-only, below).

## Cross-feature reconciliations (these override the per-branch files)

### Scope taxonomy is unified

The single scope vocabulary lives in `src/nightdesk/domain/scopes.py`. On top of
the token-perms taxonomy, this round adds:

- `agents.read`, `agents.message`, `agents.admin` — the resident-agents surface.
  `agents.message` and `agents.admin` are **human-only** (unmintable).
- `integrations.read` (grantable), `integrations.link` (grantable; run tokens
  carry the `.self` variant), `integrations.write` (**human-only** — connection
  and repo-link CRUD hold a forge credential + endpoint URL, same posture as
  `providers.write`).

Legacy run-token scope strings are aliased to this vocabulary at resolution
(`expand_run_scopes`), so a profile granting `integrations.read` /
`integrations.link.self` keeps working.

### The `/api/v1/agents` family is admin-only

Resident interactive agents replace the old `/api/v1/sessions` surface. Document
the family (create, list, `GET {id}`, delete, `messages`, `interrupt`, `end`,
`wake`, `pending`, `pending/{request_id}` answer, `env`, `restart-runtime`,
turns, `transcript` SSE) but state plainly: **the whole `/api/v1/agents` surface
requires the admin session or root bearer.** Driving a resident agent is a human
act — an `ndk_`/`ndr_` token gets a `403` naming a human-only scope, never
access. This is the integration ruling (seam A); it is stricter than the
token-perms taxonomy alone (which lists a grantable `agents.read`) because a
resident agent runs in trusted posture on the real `~/.claude`.

### Acknowledgement stays admin-only, no scope exists

The ack endpoints (`POST /tickets/{tid}/ack`, `POST /tickets/ack`,
`GET /tickets/ack/digest`, `GET /tickets/ack/count`) require the admin
session/bearer. There is deliberately **no ack scope** — an agent must never be
able to mark its own work seen. A token 401s on these routes. Keep this note.

### Token-driven transitions are attributed and never auto-ack

With scoped tokens now able to reach transition/archive/requeue/run-now, a
transition performed by an `ndk_` agent token records `actor_kind='token'` (an
`ndr_` run token records `'run'`) in `ticket_events` and does **not** set
`acknowledged_at`. Only a human (admin) transition auto-acks. Worth a sentence in
`nightdesk-ticket-ops` so an agent understands its archives still show up in the
owner's digest.

### GitLab import now writes `description` (seam C)

Correct the gitlab per-branch file's import note: `POST
/repo-links/{id}/import-ticket` sets BOTH fields. The `prompt` stays
self-sufficient (it quotes the issue body as reference data and the agent runs on
it alone); the human-facing `description` carries a readable issue summary
(reference + body as prose), which the board, review, side-peek, and ack digest
prefer over the prompt. `description` is never injected into the agent's context.

## Out of scope (do NOT document as available)

Jira, MR creation, inbound webhooks (gitlab v2/v3); opencode resident agents;
`agents.message` agent-to-agent grants; the run-token/`ndk_` table unification
(Phase 2). Sandboxed resident-agent posture is reserved, not shipped.
