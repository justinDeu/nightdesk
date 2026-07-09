# Skill updates required by the ack-flow branch

The feat/ack-flow branch changed the ticket API surface. Per the repo CLAUDE.md
("Keep the API-client skills in sync with the code"), the two hand-written
skills need these edits when this branch lands. They live outside the repo
(`~/.claude/skills/...`), so this file records the diff to apply.

## `nightdesk-api` (`~/.claude/skills/nightdesk-api/SKILL.md`)

New admin-only endpoints under `/api/v1/tickets`:

- `POST /api/v1/tickets/{tid}/ack` — acknowledge one ticket's outcome (mark it
  seen). 200 with the ticket; 409 if the ticket is not in `review`/`archived`
  (nothing to acknowledge); 404 if unknown.
- `POST /api/v1/tickets/ack` — bulk acknowledge. Body is one of:
  - `{"ticket_ids": ["...", "..."]}`
  - `{"project_scope": true, "project_id": "<id-or-null>", "before": "<iso8601>"}`
    (`project_id: null` = the no-project group; `before` makes "ack all"
    race-safe against work archived mid-read). Returns
    `{"acknowledged": [...ids], "count": n}`.
- `GET /api/v1/tickets/ack/digest?project_id=<optional>` — unacknowledged
  review/archived work grouped by project then day. Returns
  `{total, generated_at, groups: [{project_id, day, count, succeeded, failed,
  cost_usd, tickets: [{ticket_id, title, status, project_id, entered_at,
  actor_kind, outcome, cost_usd, run_id}]}]}`. Pass `generated_at` back as the
  bulk-ack `before` for race safety.
- `GET /api/v1/tickets/ack/count` — `{"count": n}` for the Desk band header.

New query param on `GET /api/v1/tickets`:

- `acknowledged=true|false` — filter by acknowledgement state (composes with the
  existing filter builder).

New fields on the ticket object (`TicketOut`, returned everywhere a ticket is):

- `acknowledged_at` (nullable ISO8601), `acknowledged_by` (nullable, currently
  always `"admin"`).
- `archived_at` (nullable) — the real archived timestamp from the event log,
  populated on list/detail responses (not `updated_at`).
- `agent_reviewed` (bool) — a run/agent, not a human, moved the ticket into
  review. Only meaningful while `status == "review"`.
- `description` (nullable) — human-facing what/why, distinct from `prompt`. See
  the description section below.

### `description`: human summary split from `prompt`

- `TicketCreate` and `TicketUpdate` gain `description` (nullable string). On
  `TicketUpdate`, an explicit `null` clears it (same as `project_id`).
- Focused route `PATCH /api/v1/tickets/{tid}/description` with body
  `{"description": "..."}` (empty/absent clears), matching the other sparse
  metadata routes (`/priority`, `/status`, `/project`, `/profile`).
- `prompt` is unchanged and is still exactly what runs. `description` is NEVER
  injected into the agent's context — it is metadata for humans scanning the
  board, review, and the ack digest. No mirroring between the two fields.

Note: the ack endpoints are deliberately admin-only. When scoped agent tokens
land (sibling `agent-token-permissions` design), the ack scope is intentionally
absent from the grantable list — an agent must never acknowledge its own work.

## `nightdesk-ticket-ops` (`~/.claude/skills/nightdesk-ticket-ops/SKILL.md`)

Add an "Acknowledge reviewed/archived work" recipe covering the single and bulk
ack calls above, and note that:

- Acknowledgement is NOT a lifecycle transition — it never moves the ticket and
  writes no `ticket_event`. It is orthogonal to status.
- Human-initiated transitions (archive/requeue/continue/etc.) auto-acknowledge;
  agent/run transitions never do; re-entering an active state (requeue,
  unarchive) clears the ack.
- The PM-agent shift-report convention (design section 5 "e garnish") is NOT yet
  implemented as a first-class feature — it remains a convention. No schema.

Add a `description` convention to the "Create a ticket" recipe: when an agent
files a ticket via the API, it SHOULD set a concise human-readable `description`
(the what/why) alongside the `prompt` (the agent instructions). The
`description` is what a human reads on the board, in review, and in the ack
digest; the `prompt` is what runs. Do not duplicate the prompt into the
description — write the description for a person skimming outcomes.
