# Post-Review Acknowledgement Flow

Status: design proposal, 2026-07-08. Not implemented.
Studied against the `integration-session-suite` branch.

## 1. Problem

The PM-agent workflow works today: an agent uses nightdesk as its work queue,
creates tickets, runs them, reviews the results. Then it hits a fork with two
bad ends:

- The PM agent is told not to archive. Review fills with agent-approved work
  the human never asked to inspect item by item. Review stops meaning
  "needs a human decision".
- The PM agent archives freely. The board stays clean, but the human loses
  the picture of what happened. Reconstructing it means paging the Archive
  sorted by `updated_at` with no signal for "you never saw this".

The owner's candidate fix is a post-review state that agents move tickets
into and humans drain. The worry: is that just a second overflowing column?

Two adjacent questions ride along:

- When is work "done", and when does its branch merge?
- When GitLab/Jira integration lands (sibling design,
  `gitlab-jira-integrations.md`), when do linked issues/MRs close?

## 2. What the codebase gives us today

Findings from `src/nightdesk/domain/tickets.py`, the API routes, and the
frontend, with file references.

**Lifecycle.** `_VALID_TRANSITIONS` (domain/tickets.py:28): inbox, draft,
queued, running, review, archived. `review -> {queued, archived}`. Archived is
the single terminal-ish state and `unarchive` returns to queued. There is no
`archived_at` column. Archiving bumps `updated_at`, and the Archive page
sorts by `sort=recent` on that (docstring at domain/tickets.py:413).

**Board.** The board renders exactly draft/queued/running/review
(`frontend/src/routes/tickets/displayModel.ts:46`, `Board.tsx:26`). Inbox and
Archive are separate pages. Column set is hard-coded in the display model, the
filter model (`filterModel.ts:269` allows archived in filters), `StatusPill`,
bulk status menu (`components/BulkBar.tsx`), and `_ALL_STATUSES` server-side.
A new status touches all of these plus analytics groupings and saved views.

**Desk.** `routes/desk/DeskPage.tsx` already has the awareness skeleton:
a "Needs you" band (failed runs + review tickets), "Running now", and a
"While you were away" band that diffs finished runs / entered-review /
inbox-arrivals against a client-side last-visit timestamp (`visitAt`,
DeskPage.tsx:184). That last band is ephemeral: it is per-browser, resets on
visit, and cannot answer "what did I never look at" once you have visited.

**Who transitions tickets.** All ticket routes sit behind
`require_token_cookie_or_bearer` (api/routes/tickets.py:123), the admin
bearer. Run tokens (`domain/run_tokens.py`) carry only narrow self-scopes
plus grantable `ticket.create`. So a PM agent today transitions and archives
with the full admin token. Nothing records who acted. The only provenance
groundwork is `DiffComment` `Author.kind: 'admin' | 'agent'`
(domain/diff_comments.py). The sibling design `agent-token-permissions.md`
is expected to introduce scoped agent tokens for exactly this surface.

**Notifications.** One webhook fires per run completion when the worker moves
running -> review (`worker/run_one.py:1328`, `domain/notifications.py`).
There is no digest, no event log, no notification on archive.

**Done vs merged.** `commit_on_finish` commits the run's work on the
worktree branch so `base_ref` stacking works (run_one.py:430). Nothing in
nightdesk merges to a mainline. Dependency satisfaction
(`_is_dependency_satisfied`, domain/tickets.py:1111) means "upstream in
review/archived and its latest turn succeeded". So today a ticket can be
archived-and-done while its branch is unmerged, and nightdesk has no way to
know. Also load-bearing: nightdesk runs non-git work (research, docs,
cleanup), so merge state cannot be a lifecycle gate for every ticket.

**Resident agents.** The in-flight resident-agents v3 design
(docs/design/session-suite/resident-agents-v3.md) makes the PM agent a
long-lived resident with a needs-input state and Desk presence. The PM actor
in this doc is either a ticket run (run token) or a resident agent turn.

## 3. The core insight: review debt vs awareness debt

Review overflow is expensive because each item demands inspection before it
can leave the column. Cost is O(n) human attention, serialized, and the
column blocks reuse of the state's meaning.

Awareness is a different obligation. The human needs to know what happened,
at a resolution they choose. That can be satisfied by a grouped summary read
in one sitting, and dismissed in one action. Cost can be O(groups), not
O(tickets).

So the design question is not "which column holds acknowledged work" but
"how do we make acknowledgement batchable, durable, and off the board".
Any option that puts per-item obligations back on the human recreates the
review problem one column to the right.

## 4. Alternatives

Evaluation criteria: awareness quality, click cost (per item / per batch),
board clutter, PM-agent autonomy (never blocked on a human), attribution and
audit, and where merge state lives.

### a) New lifecycle state: review -> done -> archived

Agents (scope-gated) move review -> done. Humans move done -> archived as
the acknowledgement.

Is done-column overflow structurally different from review overflow?
Partially. Acknowledgement can be batch ("ack all", ack by project), so the
drain cost is lower than review's per-item inspection. But the structural
problems are real:

- It is still a board column that only fills between human visits. After a
  week away it is a wall of cards. The board reads as "behind" even when
  nothing needs you. That is a feeling problem as much as a count problem,
  and it is exactly the complaint being fixed.
- Terminal state splits. Bulk archive, unarchive, dependency satisfaction,
  Archive-page queries, saved views, analytics, `request_run_now` source
  states, and `_ALL_STATUSES` all grow a case. The status enum reaches into
  more than a dozen call sites on both sides of the API.
- Semantics get awkward at the edges. Can a human archive straight from
  review (they must be able to, that is today's flow and it should imply
  acknowledgement)? Then done is optional and the column undercounts. Does
  requeue-from-done exist? Does declining an inbox item pass through done?
  Every answer adds a rule.
- Migration debt for zero information gain: everything the state encodes is
  one boolean plus one actor, which a column on the ticket also encodes.

Merge state in this option: still unanswered. Done cannot mean merged
(non-git tickets), so merge would need a chip anyway.

Verdict: workable, better than nothing, but it spends the most and the
overflow worry is only half-addressed.

### b) Orthogonal `acknowledged` flag + unacknowledged feed

No new status. Agents archive freely. The ticket gains
`acknowledged_at` / `acknowledged_by`. Human-initiated actions auto-ack.
Agent-initiated archives leave it null. A Desk band and a digest view show
archived-but-unacknowledged work grouped by project and day, with one-click
batch ack.

- Awareness: durable and server-side (unlike the `visitAt` band). "Show me
  everything that happened that I never saw" is a single indexed query.
- Click cost: zero per item if you only read the digest. One click per
  group, or one for everything. Opening a ticket peek auto-acks it.
- Board clutter: none. The board keeps meaning "work in motion or needing
  a decision".
- Autonomy: total. The PM agent reviews and archives on its own judgment,
  is never waiting on a human, and the human's awareness debt accrues in a
  feed, not in the agent's path.
- Attribution: requires knowing who archived, which needs the actor log
  regardless of option.
- Merge: orthogonal chip in the feed (see section 6).

Cost: two columns, one band, one view, small API. The cheapest option that
fully solves the stated problem.

### c) Review sub-states / badges

Keep everything in review, badge cards by who moved them there
(agent-reviewed vs needs-human), filter chips on the column.

This mislabels the work. An agent-approved ticket is not "in review", it is
finished pending human awareness. The column still grows without bound, the
count in the header still lies, and "collapse the agent-reviewed chip" is
just hiding the pile. Useful as a garnish (the chip is nearly free once
actor attribution exists) but not as the mechanism.

### d) Digest-first: awareness as a feed, not a board problem

Daily/periodic summary generated from transitions, pushed via webhook and
shown on the Desk. No per-ticket state at all.

Right instinct, wrong completeness. A pure digest cannot answer "did I see
this specific ticket" and gives no way to mark caught-up, so the same items
reappear or silently expire. It also depends on an event log that does not
exist yet. Digest is the presentation layer of option b, not a substitute
for its state.

### e) The PM agent writes the report (agent-native)

When the PM agent finishes a batch, it writes a shift report: a markdown
summary of what ran, what it approved, what it archived and why, with ticket
links. Acknowledgement becomes "read the report", one artifact per shift
instead of n tickets.

This is the most agent-native idea and it composes with (b): the report is
how you consume the feed, the ack flag is how the system knows you did. As a
standalone it has the digest problem (free text, no ground truth, the agent
can omit things). It also needs no schema: it is a convention plus a skill
recipe, and optionally a `kind='report'` ticket later.

## 5. Recommendation: b + attribution foundation, garnished with c and e

One design, three layers. No new lifecycle state.

### Layer 0 (foundation): transition events with actors

A `ticket_events` table: `id, ticket_id, kind, from_status, to_status,
actor_kind ('admin'|'agent'), run_id, session_id, payload, created_at`.
Written inside `transition_with_position` and the convenience helpers
(archive, requeue, promote, decline), plus `run_now` requests.

This is the acting-principal groundwork the owner already wants, generalized
from `DiffComment.Author`. It powers: the "who archived this" answer, the
agent-reviewed chip on review cards, the digest, analytics later, and the
integrations design's closure rules. It also gives Archive a real
archived-at (the event timestamp) instead of leaning on `updated_at`.

Attribution source: admin bearer/cookie => `admin`. Run token =>
`agent` + `run_id`. Resident-agent turns => `agent` + `session_id`. When the
sibling agent-token-permissions design lands scoped agent tokens, its
principal slots into the same two columns.

### Layer 1 (the mechanism): acknowledged flag + auto-ack

Ticket columns: `acknowledged_at (nullable)`, `acknowledged_by (nullable,
'admin')`. Semantics: "a human has seen this ticket's outcome".

Rules:

- Any human-initiated transition, archive, requeue, continue, or
  new-conversation sets `acknowledged_at` implicitly. Humans never do a
  separate ack for work they touched.
- Opening the ticket peek/detail as a human acks it (read receipt).
- Agent-initiated transitions never set it.
- Unarchive or requeue clears it (the ticket re-enters motion, its next
  outcome is unseen again).
- Tickets created and archived by a human are born irrelevant to the feed.

API:

- `POST /api/v1/tickets/{tid}/ack`
- `POST /api/v1/tickets/ack` with either `ticket_ids: [...]` or a filter
  object `{project_id?, before?}` for "ack everything in project X up to
  now". `before` (a timestamp captured when the digest rendered) makes
  "ack all" race-safe against work archived mid-read.
- `GET /api/v1/tickets?acknowledged=false` composes with the existing
  filter builder (`_ticket_filters`), one more column filter.

Ack endpoints are human-only: admin principal required, and the ack scope is
deliberately absent from the grantable-scope list in the
agent-token-permissions design. An agent acknowledging its own work is the
one thing this feature must make impossible.

### Layer 2 (the surfaces)

**Desk band: "To acknowledge".** Sits under "Needs you". Server-backed
count of archived-and-unacked tickets. Collapsed rendering: grouped rows,
one per project-day, e.g. "nightdesk, yesterday: 7 tickets, 6 succeeded,
1 failed-then-fixed, ~$4.20" with expand. Group row actions: expand, ack
group. Band header action: "Ack all (n)". This band replaces nothing:
"While you were away" stays as the ephemeral since-last-visit diff, the new
band is the durable obligation.

**Digest view.** `/desk/ack` (or a filter preset on the Tickets list):
unacked work grouped by day then project. Each row: title, outcome pill,
who archived (agent chip with run link), cost, merge chip (section 6),
diffstat where applicable. This is where per-item drill-down lives for the
minority of items worth it.

**Keyboard flow** (matching existing list bindings): `j/k` move, `x` select,
`e` ack selected (mnemonic: same key Gmail uses for archive-as-done),
`shift+e` ack group, `o`/enter opens peek (which acks). Target: clearing a
40-ticket week is one read plus fewer than five keystrokes.

**Review-column chip (the c garnish).** Derived from ticket_events: cards
moved to review by the worker whose ticket the PM agent has since touched
get an "agent-reviewed" chip, and the review column gets a filter chip to
hide them while the PM is mid-pass. Cheap, optional, no schema.

**Webhook digest (the d garnish).** A scheduled daily summary POST built
from ticket_events plus the unacked set, alongside the existing per-run
webhook. Config: `notify_digest_cron`. Deliverable text mirrors the Desk
band's group rows and links to `/desk/ack`.

### The PM agent's side (the e garnish)

The PM agent's API surface for this flow, using the scoped agent token from
the sibling design:

```
POST /api/v1/tickets/{tid}/transition   {"status": "archived"}   scope: ticket.transition.archive
POST /api/v1/tickets/{tid}/requeue      (send back for another pass)  scope: ticket.transition.requeue
POST /api/v1/tickets                    (file follow-up work)     scope: ticket.create
```

Suggested scope names for the sibling design: `ticket.transition.archive`,
`ticket.transition.requeue`, `ticket.read`, `ticket.create`. No ack scope
exists at all.

Convention (skill recipe, `nightdesk-ticket-ops` addition, no schema): after
a batch pass the PM agent files a low-priority "Shift report" ticket, or
posts the summary into its own resident-agent transcript, linking the
tickets it archived. The digest view remains ground truth. The report is
narrative. If reports prove central, promote to `kind='report'` later.

### Why the overflow worry is answered

- Nothing new on the board. The pile lives in a feed built to be read as
  groups and dismissed in batches.
- Dismissal cost is O(groups): a week of PM output is a paragraph of group
  rows and one "ack all".
- The count is honest. "To acknowledge: 41" means 41 unseen outcomes, not
  41 tasks. Review's count goes back to meaning real decisions.
- If the human ignores it for a month, only a number grows. The board, the
  agent, and the pipeline are unaffected. Optional pressure valve
  (default off): auto-ack after N days, config `ack_auto_expire_days`,
  logged as an event so nothing silently disappears.

## 6. Done vs merged

Recommendation: merge state is an orthogonal fact, never a lifecycle gate.

- Nightdesk runs non-git work, so archived cannot require merged.
- Keep `commit_on_finish` as the "work is committed on its branch" primitive
  it already is.
- Trusted PM flows: the agent merges before archiving, as part of its review
  judgment, and that is just work the agent does in a workspace. Record it
  as a ticket_event (`kind='merged'`, payload: target ref, sha) when the
  agent reports it, or derive it from the MR integration when that exists.
- Human review flows: merge at review, as today, before archiving.
- Surfaces: a merge chip on ticket cards, the digest rows, and the Archive
  list: merged / unmerged / n-a. The digest view groups "archived but
  unmerged branches" as its own callout, because that is the one
  acknowledgement case that carries a real follow-up action.
- The dependency check stays as-is (review/archived + success). Stacked
  dependents already consume the branch via `base_ref`, merge is not their
  gate either.

## 7. Interface to the GitLab/Jira integration (sibling design)

Contract offered, not co-designed:

- ticket_events is the trigger bus. The integration subscribes to
  `archived` and `merged` events and reads `acknowledged_at`.
- Suggested closure rule: a linked issue is offered for closure (or
  auto-closed, per-integration policy) when the ticket is acknowledged AND
  the linked MR is merged. Either signal alone is insufficient: unmerged
  means the work has not landed, unacked means the human has not seen it.
  For non-MR tickets the rule degrades to acknowledged alone.
- MR merge state flows the other way: the integration writes a `merged`
  ticket_event (or a `merge_state` property), which is what the chip in
  section 6 renders. Nightdesk core never polls git remotes itself.
- The digest view exposes the pending closures as a third group:
  "ready to close upstream: 3 issues".

## 8. Phasing

| Phase | Contents | Size |
|---|---|---|
| 1 | `ticket_events` table + writes with actor attribution in `transition_with_position` and helpers; principal plumbed from auth deps | S-M |
| 1 | `acknowledged_at/by` columns, auto-ack on human actions and peek-open, ack endpoints (single + bulk with `before`), `acknowledged` filter in `_ticket_filters` | S |
| 2 | Desk "To acknowledge" band with grouped rows + ack-all; digest view with keyboard flow | M |
| 3 | Review-column agent-reviewed chip; merge chip fed by events; webhook daily digest | S each |
| 3 | PM shift-report recipe in `nightdesk-ticket-ops` skill | S, docs only |
| ext | Closure rules, MR merge-state ingestion | owned by integrations design |

Phase 1 is useful alone (provenance + queryable unseen-work) even before any
UI ships. One additive alembic revision covers both tables per the existing
migration policy.

## 9. Open questions

- Auto-expire default: off, or 14 days? Off preserves the awareness
  guarantee, on caps the number. Leaning off, with the config available.
- Does peek-open-acks feel right, or should ack require the explicit action?
  Read receipts can feel presumptuous. Cheap to flip either way.
- Should "To acknowledge" include tickets a human archived from draft/queued
  (never ran)? Proposed: no, human action implies awareness.
- Multi-project noise: the board host runs foreign projects too. The band
  groups by project already, but a per-project mute may be wanted.
- Should the shift report get first-class `kind='report'` in v1? Proposed:
  no, convention first, promote on evidence.
- Bulk-ack race: `before` timestamp handles archive-during-read, but a
  requeue-during-read clears ack correctly by rule. Verify the digest
  refetch story in implementation.

## 10. Addendum: ticket `description` (implemented with this branch)

Motivation from the owner: reviewing a ticket by reading its `prompt` is bad —
the prompt is agent instructions, not a human summary. So authoring splits into
two fields:

- `prompt` — unchanged. Exactly what the agent runs. The worker's prompt builder
  is untouched; `description` is NEVER injected into the agent's context.
- `description` — nullable, human-facing what/why. Metadata only. No backfill, no
  auto-mirroring between the two fields.

Semantics and surfaces:

- Read surfaces prefer `description` over `title`/`prompt` for the summary line
  (fallback to `title` when empty): the ack digest rows, the Desk "To
  acknowledge" / "Needs you" rows, the review-column card. This is the synergy
  that motivated the addition — it makes acknowledgement readable at a glance.
- Ticket detail and the side-peek show `description` first-class; `prompt` is
  demoted to a collapsible "Agent prompt" section.
- The authoring composer (quick capture + full editor) has both fields, each
  labeled for who it is for.
- API: `TicketCreate` / `TicketUpdate` / `TicketOut` carry `description`; a
  focused `PATCH /api/v1/tickets/{tid}/description` matches the other sparse
  metadata routes (empty/absent body clears it). It rides migration 0028.
- Interface to the sibling GitLab/Jira import: imported issue bodies retarget
  into `description` (not `prompt`) at final merge. The column and API are left
  ready; this branch does not implement the import.
