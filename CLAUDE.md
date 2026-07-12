# nightdesk

## Keep the API-client skills in sync with the code

Two personal skills document this project's HTTP API for scripts, tests, and ad-hoc tooling:

- `nightdesk-api` — `~/.claude/skills/nightdesk-api/SKILL.md` — auth, base URL, the JSON `/api/v1/*` vs HTMX surfaces.
- `nightdesk-ticket-ops` — `~/.claude/skills/nightdesk-ticket-ops/SKILL.md` — ticket lifecycle recipes (create, transition, run-now, archive, runs, transcript).
- `nightdesk-pm` — `.claude/skills/nightdesk-pm/SKILL.md` (bundled in-repo) — the PM-shift operating system. Hard-codes: token bundle names from `src/nightdesk/domain/scopes.py` (`pm-agent` / `operator`), the steer endpoints (`/api/v1/tickets/{tid}/steer*`), the ack digest route (`/api/v1/tickets/ack/digest`), external-links, and the admin-only status of acknowledgement and issue import. Changing any of those means updating this skill (and its `setup.md`) in the same change.

These are hand-written and drift when the API changes. When you touch any of the following, update the matching skill **in the same change**:

- `src/nightdesk/api/schemas.py` — request/response fields, especially `TicketCreate` / `TicketUpdate`. The skills hard-code field names and example payloads. (Example drift already hit: the old `cwd` create field was replaced by `source_path` + `workspaces`, and a primary workspace is now required.)
- `src/nightdesk/api/routes/*.py` — endpoint paths, methods, status codes.
- `src/nightdesk/domain/tickets.py` `_VALID_TRANSITIONS` plus the `archive` / `decline_ticket` / promote guards — drives the lifecycle table and the transition caveats in `nightdesk-ticket-ops`.
- The `TicketTransition` allowed-target enum — the skill notes that `inbox` is NOT a valid `/transition` target even though `draft → inbox` is state-machine-legal.

Quick reconciliation after a schema change:

```bash
curl -s "$BASE/openapi.json" | jq '.components.schemas.TicketCreate'
```

Compare the fields and `required` list against the skill's "Create a ticket" recipe and fix any mismatch.

## Skills: shipped vs internal

All skills live together in `.claude/skills/` (so Claude Code discovers them all). `nightdesk-install-skills` ships every skill there **except** ones whose `SKILL.md` frontmatter sets `internal: true`. That flag is how dev runbooks (operating the live instance, restarting services) opt out of being copied into users' harnesses. Default = ships, which is what you want for new user-facing skills; only mark a skill `internal: true` when it is a nightdesk-dev runbook. Shipped skills carry a `nightdesk-` prefix (they install into users' global skill dirs and need the brand namespace); internal skills don't ship, so they use short unprefixed names. Relevant internal skills: `restart-worker` (don't restart the worker while tickets run; confirm, record, resume with priority) and `restart-instance` (API-before-worker to avoid the migration race).

## Frontend design standards are mandatory

Before building or reworking ANY page/screen/component under `frontend/`, read
`.claude/skills/ui-design-standards/SKILL.md` and follow it. Non-negotiables it
encodes: no centered-column work surfaces (`max-w-* mx-auto` page shells are
banned outside login/focused modals), full-viewport layouts per the archetype
table, side-peek over navigation, styled tooltips only, Linear-grade density.
PRs that reintroduce a banned pattern get bounced at review.
