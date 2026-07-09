# Skill updates for the token-permissions feature (apply at merge-to-main)

Per the project CLAUDE.md, the `nightdesk-api` and `nightdesk-ticket-ops` skills
must change **in the same change that ships the feature**. This worktree cannot
edit `~/.claude`, so the full replacement text lives here. At merge-to-main,
apply these edits to the live skill files:

- `~/.claude/skills/nightdesk-api/SKILL.md`
- `~/.claude/skills/nightdesk-ticket-ops/SKILL.md`

The load-bearing change: **stop teaching agents to read the admin bearer out of
`config.toml`.** Agents now export a scoped `ndk_` token as `NIGHTDESK_TOKEN`.

---

## 1. `nightdesk-api/SKILL.md`

### 1a. Replace the auth paragraph (currently line ~10)

> `/api/v1/*` admin routes accept `Authorization: Bearer <token>` **or** the
> signed `nightdesk_session` browser cookie ... run-scoped tokens ... only work
> via the bearer header and only on the scopes granted to that run.

with:

```markdown
`/api/v1/*` routes authenticate three ways, resolved in this order: the signed
`nightdesk_session` browser cookie (the SPA), the **root** admin bearer, and
scoped API tokens. Scopes are the model here — a token only reaches the routes
its scopes cover:

- **`ndk_` durable agent tokens** — what a script or agent should use. Minted
  in Settings → Access tokens with a bundle (`observer` / `reviewer` /
  `pm-agent` / `operator`) or a hand-picked scope set. Sent as
  `Authorization: Bearer ndk_…`. A token can never obtain a session cookie and
  can never mint another token.
- **`ndr_` run tokens** — injected into a sandboxed run as `NIGHTDESK_RUN_TOKEN`,
  self-scoped to that run's own ticket. Unchanged, plus they now actually reach
  the routes their granted scopes claim (e.g. a profile granting `ticket.create`
  lets the run POST a child ticket).
- **root bearer / cookie** — full admin, bypasses scope checks. The bearer is
  still the crypto root (cookie signing + secret encryption); do not put it on
  an agent. Human-only actions (profile/provider/config/cron writes, resident-
  agent messaging, ticket delete) require it and are refused to every token.
```

### 1b. Replace the "## Auth" section (currently lines ~12-32)

Replace the `config.toml` + `awk` recipe entirely with:

```markdown
## Auth

Export a scoped token in your shell — **do not read the admin bearer from
`config.toml`.** Mint one in the UI (Settings → Access tokens), which hands you
a ready-made `export` line shown exactly once:

    export NIGHTDESK_TOKEN=ndk_...

Then:

```bash
TOKEN="${NIGHTDESK_TOKEN:?export a scoped token — see Settings → Access tokens}"
BASE="${NIGHTDESK_BASE:-http://127.0.0.1:8765}"
AUTH=(-H "Authorization: Bearer $TOKEN")

curl -s "${AUTH[@]}" "$BASE/api/v1/tickets" | jq .
```

Pick the smallest bundle that covers the job: `observer` to read, `pm-agent` to
create/triage tickets, `operator` to also run them now. If a call 403s, the body
tells you exactly which scope is missing (below) — re-mint with that scope
rather than reaching for the admin bearer.
```

### 1c. Add a "403 / 401 shapes" subsection (after Auth, before Endpoint discovery)

```markdown
## Permission errors

A scoped token that lacks a scope gets a **403** whose body names the gap, so an
agent can self-diagnose instead of retrying blindly:

```json
{
  "detail": "missing scope",
  "missing_scopes": ["tickets.transition"],
  "token": "pm-agent",
  "hint": "This token lacks 'tickets.transition'. An operator can grant it in Settings → Access tokens."
}
```

If the missing scope is **human-only** (profile/provider/config/cron writes,
`agents.message`/`agents.admin`, `tickets.delete`), the hint says so — that
action requires the admin session and can never be granted to a token. Stop
retrying and ask your human.

A **401** (`{"detail":"invalid token"}`) means the token is unknown, revoked, or
expired — the server never distinguishes which. Re-mint.

The scope taxonomy and which bundle grants what is documented in
`docs/design/agent-token-permissions.md` §4.
```

---

## 2. `nightdesk-ticket-ops/SKILL.md`

### 2a. Any auth preamble

Wherever it says to read the bearer from `config.toml`, replace with the
`NIGHTDESK_TOKEN` env pattern from §1b above. Add a one-liner: "these recipes
need a token whose bundle covers the action — `pm-agent` for
create/update/transition/archive, `operator` to also run-now."

### 2b. Note per-recipe scope requirements

Annotate the lifecycle recipes with the scope each needs, so a 403 is
predictable:

| Recipe | Scope |
|---|---|
| create ticket | `tickets.create` (plus the token's `profile_allowlist`, if set, must include the ticket's profile) |
| fetch / list / search / transcript | `tickets.read` / `runs.read` |
| update fields, labels, priority, project, steer, deps | `tickets.update` |
| transition / promote / decline / send-to-inbox | `tickets.transition` |
| run-now / cancel / requeue / continue / resume / retry / restart | `tickets.run` |
| archive / unarchive | `tickets.archive` |
| review comments (incl. request-changes) | `comments.write` |

Keep the existing caveat that `inbox` is not a valid `/transition` target; it is
unaffected by this change.
