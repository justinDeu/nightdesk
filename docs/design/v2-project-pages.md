# Project Pages: per-project management interface (v2)

**Status**: Accepted (v1)
**Date**: 2026-07-12
**Ticket**: Project pages — overarching per-project management interface

## 1. Problem

The left sidebar has no per-project surface. Today the only way to work on one
project is the Tickets board with a `project:` filter. That answers *"which
tickets belong to project X?"* but not the actual management question:

> How is this project doing right now, and what should I do next?

A filtered board is blind to everything that isn't a board column: the
project's inbox items (a separate page), its recent runs and how they went
(separate pages), and where its money and models go (the Analytics page). To
manage one project today you hop between four surfaces and hold the context in
your head. The bar for this feature, set in the ticket: **the user should
prefer this page over the tickets-page-filter workflow, or it has failed.**

## 2. Prior art (the dead server-rendered UI)

`git log ed93b4a..d70556f --oneline` is an earlier project-pages implementation
for the old HTMX UI. The code targets a removed UI and cannot be reused, but
the *concepts* that survived user feedback are the spine of this design:

- **Project rail** — every project listed with a color dot, running/review
  badges (only when non-zero), relative last-activity, and archived projects
  collapsed at the bottom. One grouped query, not one per row.
- **Pulse / Overview** — status-count pills (zero counts muted, non-zero
  review the strongest), running-now, spend tiles that distinguish "no runs"
  from "runs that priced to $0", and a **models & profiles** card (per
  model/profile run count, share, cost) answering *"am I using the expensive
  profile where the cheap one would do?"*
- **Management queue** — the decisive upgrade. The original "recent tickets"
  link list was replaced (commit f15e686) by a queue **grouped by lifecycle
  status in triage order** (Review → Running → Queued → Inbox → Draft; empty
  groups omitted), where each row carries **real inline actions**
  (Archive / Requeue / Run again / Cancel / Run now / Promote / Decline)
  restricted to whatever `_VALID_TRANSITIONS` allows from that status. Direct
  user quote captured in that commit: *"there was still no way to actually
  manage a project from these pages."* Acting never navigated away.
- **Activity ledger** — a dense one-line ledger (time, title,
  duration/tokens/cost/model, outcome pill, transcript link) grouped under
  Today / Yesterday / date, with a failures-only toggle.
- **Defaults strip** — a one-line summary under the project path (mode, base
  ref, workspace/toolset counts) ending in an Edit link, so configuration has
  exactly one home (the settings modal), not a second copy on the page.

What we steal: the management queue as the center, the pulse + models/profiles,
the activity ledger, the defaults strip, and the "act inline, never navigate"
rule. What we drop: HTMX pane plumbing, per-run cost in the ledger (the
available data does not carry it — see §7).

## 3. Information architecture

Two routes, both addressable and deep-linkable.

### 3.1 `/projects` — Projects index (pick a project)

The sidebar entry target. A full-width grid of project cards. Each card carries
just enough signal to choose one: color dot + name, source path, a compact
pulse row (Running / Review / Inbox counts, 30-day cost, relative last
activity), and archived projects are rendered muted and collapsed under a
details group at the bottom — the old rail, promoted to a real page. A
"New project" action links to Settings → Projects.

This replaces the old UI's sidebar project *rail* with a discoverable,
deep-linkable page. Rationale in §5.

### 3.2 `/projects/$id` — Project page (manage everything)

A **dashboard**, not a board clone. Full-bleed (`width="full"`), regions
arranged in a grid so a 1440px viewport answers the management question
without scrolling. Top to bottom:

1. **Header band.** Project color dot + name (h1), source path (mono), the
   defaults strip (workspace mode, base ref, toolset/tool counts — each a
   styled Tooltip, never a native `title=`), and actions: **Open in board**
   (deep-link `/tickets?f=project:<slug>`), **Edit** (`/settings/projects`),
   **New ticket** (composer prefilled with this project). A back-link to
   `/projects`.

2. **Pulse tiles** (one row, full width). Lead with state, not labels:
   Running now (lamp-accented when > 0), In review, Queued, Draft, Inbox,
   Spend 30d, Runs 30d. Each tile is a deep-link: board/status-relevant tiles
   open the board filtered to this project; the Inbox tile opens the inbox.

3. **Main grid** (two columns on wide screens):

   - **Left (primary, wider): the management queue.** The project's
     actionable tickets grouped by lifecycle status in triage order
     (Review → Running → Queued → Draft), empty groups omitted. Each row:
     title (link), priority chip, latest-run signal, and **inline actions**
     drawn from the shared `ticketStatusMoves` (only what is legal from that
     status). A header link **Open all in board →** hands off to the full
     board for the work the board is better at (reorder, bulk, saved views).

   - **Right (stacked side panels):**
     - **Inbox for this project.** The project's inbox items with
       promote-to-draft / promote+queue / decline inline, plus the
       completeness blocker count. Header link **Open inbox →**.
     - **Recent activity ledger.** The project's recent runs as dense
       one-line rows (relative time, title, outcome pill, duration, tokens),
       grouped under Today / Yesterday / date, with a **failures-only**
       toggle. Each row links to its run transcript. This is the
       "recent activity / completed work" region.
     - **Models & profiles.** A compact breakdown (model, then profile:
       run count, share %, cost) from the 30-day spend. The
       "expensive-profile" question, in place.

4. **TicketPeek side panel.** Clicking a queue row opens the app-wide peek
   (inspection without navigation); Enter / cmd-click / the Open button does
   full navigation to the ticket detail. Honors the global
   "navigation where a peek belongs" rule.

## 4. Navigation

- **Sidebar:** one new entry, **Projects** (`FolderKanban` icon), to `/projects`,
  positioned after Tickets (project-scoped work is the sibling of the global
  board). The entry is a flat icon like the others — no per-project children
  in the rail for v1.
- **Decision: a flat entry → index page, not a hover flyout of projects.** The
  current rail is a flat icon list with no children; a flyout would introduce a
  new nav pattern and its own keyboard/aria story. A discoverable, deep-linkable
  index page is simpler and is itself a useful surface (the old rail, promoted).
  A future enhancement can add a hover/recent-projects flyout off the rail.

## 5. Inline actions vs deep-linking

| Stays on the page (inline) | Navigates (deep-link) |
|---|---|
| Every lifecycle action on a queue row (run now, requeue, archive, cancel, send to inbox) | **Open in board** — full board filtered to this project |
| Inbox promote-to-draft / promote+queue / decline | Individual ticket full detail (Enter / cmd-click / Open) |
| Ticket inspection via the peek | **Open inbox** — the full Inbox triage page |
| Priority on a queue row (peek) | **Edit** project — Settings → Projects |
| New ticket (composer, prefilled) | Pulse tiles → board / inbox filtered |

The board keeps the jobs it is structurally better at (drag-reorder, multi-select
bulk ops, saved views, integration lens). The project page keeps the jobs the
filtered board is structurally blind to (cross-surface overview + one-place
action). They link to each other; neither tries to replace the other.

## 6. Data sources — existing, project-scoped APIs (no backend changes)

Every region is fed by endpoints that already exist and already scope by
project. **No new backend endpoint or migration is required.**

| Region | Source |
|---|---|
| Queue + status counts | `GET /api/v1/tickets?project_id=<id>` (grouped client-side; inbox & archived handled explicitly) |
| Inbox band | `GET /api/v1/inbox?project_id=<id>` (carries completeness blockers) |
| Activity ledger | `GET /api/v1/projects/<id>/activity` — the ~30 most recent runs against the project's tickets, in `routes/helpers.py` (pre-existing) |
| Spend tiles + models & profiles | `GET /api/v1/analytics/spend?range=30d&project_id=<id>` — already project-scoped; returns `totals`, `by_model`, `by_profile` |
| Project context | `GET /api/v1/projects` + `/projects/<id>` |

The `projectActivity` API client and the project-scoped `analyticsApi.spend`
were clearly added in anticipation of exactly this page; this design consumes
them rather than adding parallel endpoints.

## 7. Reuse decision: queue, not an embedded Board/List

The ticket asks to reuse the existing board/list components "where sane." After
reviewing `Board.tsx` / `List.tsx`, they are **not sane to embed here**: they
are tightly coupled to the TicketsPage interaction shell (multi-select sets,
j/k+h/l cursor ring, column-travel memory, bulk bar, saved views). Dropping
that shell onto the project page would duplicate a large state machine for a
surface whose job is overview + quick action, not board manipulation — exactly
the "filtered board" experience we are trying to beat.

Instead we **reuse the building blocks**, not the shell: the shared
`useTicketActions` / `ticketStatusMoves` (so transition legality has one source
of truth), `TicketPeek`, and the primitives (`StatusPill`, `PriorityChip`,
`Button`, `Tooltip`, `EmptyState`, tokens). The full Board/List experience
remains one click away via **Open in board**. This is the line "where sane"
draws.

## 8. Known data gaps (accepted in v1)

- **No per-run cost or model in the activity ledger.** The
  `/projects/<id>/activity` row carries `outcome`, `duration_seconds`,
  `tokens`, `started_at` — but no `cost_usd` or `model_used`. The ledger shows
  duration + tokens + outcome and links the transcript; the *money* story lives
  in the pulse (30-day total) and the models & profiles card, both fed by the
  spend endpoint. Adding per-run cost/model would be a backend change deferred
  from v1 (the ticket scopes to existing data unless a gap *forces* an endpoint;
  the aggregate spend covers the money question without one).
- **Success rate is not project-scoped** in the analytics summary. We derive a
  recent success/failure ratio from the activity ledger's outcomes (recent
  window only) rather than imply a precise project lifetime rate.

## 9. What we are NOT building in v1

- Per-project cron jobs or project-level scheduling.
- A full project analytics chart suite (the Analytics page, deep-linked, owns
  charts; the project page shows a spend summary + models/profiles only).
- Project permissions / membership (projects are not a security boundary —
  per `docs/design/projects.md`).
- Repo-discoverable `.nightdesk/project.toml`.
- A sidebar hover/recent-projects flyout (flat entry → index page instead).
- Drag-reorder and bulk operations on the project page (the board's job).
- Project-level saved views.
- Per-run cost/model in the activity ledger (§8).
- Cost-per-run or Gantt/roadmap views.

## 10. Why this beats the filtered-board workflow

The filtered board shows the project's tickets as columns. The project page
shows the project's **state** as a pulse, its **actionable work** as a queue you
can act on without leaving, its **inbox folded in** (otherwise a separate page),
its **recent activity with outcomes**, and **where the money and models go** —
all for one project, on one screen, addressable by URL. The board is one click
away for the manipulation it is better at. The page earns its place by making
the cross-surface overview the default instead of a four-tab assembly the user
has to perform manually.
