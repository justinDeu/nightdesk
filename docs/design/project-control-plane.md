# Project control plane — design (2026-07-12)

Status: **Accepted** for Overview / History / Settings + chrome. **Plan tab: design captured, DEFERRED — ships as "coming soon".**
Mockups: `project-control-plane-mockups.html` (four-tab interactive page, same design tokens as the app; also published as a claude.ai artifact during the design session).
Supersedes the project-page portions of `v2-project-pages.md` and the project items in `projects-profiles-ux-audit.md`.

## Why this exists

The v2 project page (pulse tiles + management queue + right-rail panels) missed. User verdicts across four design rounds:

- Boxed card grids read as "a disorganized smattering"; dense unified ledgers (History's form) read as right. **Design law: no card grids on project pages. Ledgers with typographic section headers and hairline dividers.**
- Stat tiles and links that navigate away make the page a hallway. **Acting never navigates away**; the only intentional exits are into a ticket (to inspect/steer) and "Trends ↗ Analytics".
- Attention-ordered triage is Desk's job. A project page that re-sorts by "what needs me" is a Desk re-skin. The project space instead answers: **what is the state of this project, on every time horizon.**
- Observed workflow this serves: the user constantly filters the board to one project to see what exists / what finished; the ack flow exists because agents self-archive so fast that overnight work became invisible. The project space is the systemic answer (History especially).

## Frame

A project is the **control plane** for its work. The user plans and reviews; agents execute. Time horizons map to tabs:

| Tab | Horizon | Job |
|---|---|---|
| Overview | now | state of everything the project touches |
| Plan | future | roadmap: dependency structure + coverage (**deferred, coming soon**) |
| History | past | what happened, day by day, forever back |
| Settings | config | every project-scoped setting, lifted home |

Rulings that bound the design:

- **Desk is untouched.** It stays the whole-of-nightdesk overview and the cross-project "which project needs me" signal. Project pages are the scoped depth behind it. Overlap is deliberate; do not redesign Desk without its own dedicated session.
- **No duplicated workflows.** Overview shows running tickets as plain rows; inspect/steer happens on the ticket page. (An earlier draft embedded a live tail + steer composer; rejected.)
- **No time claims on Plan.** No "tonight", no ETA, no cost forecasts. An earlier "Runway" (simulated overnight timeline with per-ticket duration/cost estimates) was rejected: one slow/failed run invalidates the night, and false precision erodes trust.
- **Inbox stays inbox.** The "inbox as repo-issues / Ideas" reframe and a flesh-out agent (agent matures a one-liner into a full ticket) are parked for a future agentic-workflows pass.
- **Trends tab: cut.** Per-project charts belong on Analytics behind the project filter. Tab row carries a quiet "Trends ↗ Analytics" link.
- Milestones remain "coming soon" (no entity yet). Prose intent from breakdowns has no home until then; cheapest v1 is stamping intent text into created tickets' descriptions.

## Chrome (all tabs)

- **Project strip** under the top strip: one compact tab per active project (color dot, name, needs-you badge, lamp pulse when running). One-click hop; `]`/`[` cycle projects; the active horizon tab persists across hops (nightdesk/History → `]` → CREAM/History).
- **Sidebar**: the Projects nav entry becomes an expandable group listing projects with attention badges; ordered by attention, then running, then true last activity. Strip = fast hop inside the space; sidebar = ambient awareness from anywhere. If real usage kills one, remove it then.
- **Project header**: dot, name, mono path, quiet one-line stat row (profile · toolsets · 30d spend · runs · fail rate), Edit (→ Settings tab) + New ticket (composer pre-scoped).
- **`/projects` index page: deleted.** The sidebar group and strip own picking. `/projects/$id` routes to the tabs.
- **Attention model** (drives badges + ordering): 1) tickets in review, 2) latest run failed, 3) inbox blocked or stale >48h, 4) unacked events. Running shows a lamp pulse but scores zero (system working ≠ needs you). "Last activity" must derive from latest run/event, not `updated_at` (the current card grid shows "1mo ago" on a project that ran 30 minutes ago).

## Overview tab (state)

One full-width ledger column. No panels, no right rail.

1. **Signal strip** — a single text line, not tiles: `1 running · 4 review · 0 queued · 3 draft · 4 inbox · !412 awaiting your review · 1 agent waiting · repo ok · 30d $37.73`. Color carries meaning (jade running, violet review, amber waiting-on-you). Each stat jump-anchors to its section.
2. **RUNNING** — plain rows: pulsing lamp dot, title, priority, right meta (elapsed · cost). Click → ticket page. Nothing else.
3. **NEEDS YOUR VERDICT** — review rows: priority/unacked chip, title, right meta (run duration · cost · files · +/−). Rows expand in place to an evidence block: per-file diff stat, one-line run summary, retry note, linked MR; verbs Approve (archive) / Requeue (+ optional note). Keys: j/k, e expand, a approve, r requeue.
4. **WAITING ON YOUR REPLY** — unified feed of human-input interrupts regardless of source: agent pending-input questions (question inline + quick-reply composer) and MRs awaiting your review (diff stat + open peek). Future runtime-governance gates (budget soft-asks, permission approvals) join this feed — keep the item shape generic: (source, question/summary, quick action).
5. **INBOX** — one line each, inline Draft/Queue/Decline; blocked/stale item first in amber with a Triage verb.
6. **Footer line** — quiet counts + repo + defaults + Edit. Draft titles intentionally absent (they act on Plan).

## History tab (record)

Approved as mocked in round 2:

- One reverse-chronological ledger, day-grouped (sticky headers), interleaving run outcomes, lifecycle transitions (entered review, archived, acked), repo events (MR opened/merged, issue closed), cron fires. One line per event.
- Week-boundary rollup rows, **numbers only** (runs · shipped · $ · success%) — prose digests explicitly rejected.
- Bad-day clusters get an ember left-border treatment.
- Right-edge mini time rail (month/week markers, ember marks for bad days) for scrubbing.
- Filter chips (All / Runs / Failures / Shipped / Repo / Lifecycle) + search. **Filters must be server-side** on the activity endpoint — client-side filtering over the loaded window makes the Failures chip lie about anything beyond it.
- "Load earlier" pagination.

## Settings tab (config)

Everything project-scoped, lifted out of global Settings. Global Settings→Projects becomes a thin list that deep-links here.

- Sticky in-page **section rail** (~180px) with scroll-spy + click-to-jump; amber dot on sections with unsaved changes. **Collapsible sections** (chevrons).
- Sections: Identity (name, color, path, archived) · Execution defaults (profile, toolsets, workspace mode, base ref, commit-on-finish; provenance hint → effective config) · Repo links (attach/detach, connection health) · Labels & defaults (default labels on create; label management stays instance-wide) · Automation (this project's crons; edit/disable/new) · Danger (archive, delete-if-empty).
- Dirty-state rows + sticky Save/Revert bar.
- Entirely a frontend lift: every field exists on the Project entity / current APIs (repo links 0029, crons real).

## Plan tab (roadmap) — DEFERRED, ships as "coming soon"

Design captured for when it's picked up; do not build yet. The tab renders a coming-soon stub.

- **Coverage backlog**: queued + draft + ready inbox as one list. Each ticket carries exactly one path badge answering "will this ever run and why": `queued` (scheduler decides when) / `after ↑` (dependency-ordered) / `cron · <name>` / `blocked: <reason> [fix ▾]` (inline fix, e.g. assign profile) / `no path ⚠` (the attention state this page drives to zero) / `on hold` (deliberate). Rollup sentence above the list.
- **Workstream layout**: each connected component of the TicketDependency DAG renders as its own lane; nodes are compact chips laid left→right by topological depth; SVG edges show branches AND merges (a merge is just multiple edges into one ticket). Arbitrary depth, any number of parallel streams. Unlinked tickets pool in a SINGLE TICKETS ledger below. Lanes/depth are derived at render — no schema change. Fixed buckets (Ready/Next/Later) were tried and rejected as too restrictive.
- **Wiring**: drag-to-wire (drag a node's gutter dot onto another row → writes a blocked-by edge) + a `b` in-list picker. Derived lanes re-stage on explicit action, not on poll, or planning feels unstable.
- **Inline batch capture**: composer row at top — Enter creates, keep typing; Tab priority, @ profile.
- **Breakdown workbench**: prose intent → proposed ticket decomposition (editable rows, keep/drop, pre-wired deps) → batch-create. Batch create + dependency wiring compose existing endpoints and work with manual entry today; **agentic generation of the proposal ships in the agentic-workflows pass**.
- **Schedule assurance**: the project's crons + one sentence about the window/slots/queue depth. Nothing more.

## Backend needs (consolidated)

| Need | Status | Unlocks |
|---|---|---|
| Unified project activity feed (runs + lifecycle + repo events + cron fires; cursor-paginated; server-side filters) | NEW | History; Overview recency; true last-activity |
| Attention rollup per project (review/failed/blocked/unacked counts + last activity) | NEW | Sidebar/strip badges, ordering |
| Per-run light diff stat (files, +/−, per-file) without full diff payload | NEW | Overview verdict evidence; History rows |
| "Awaiting your review" MR flag (derived from GitLab reviewer state) | NEW | Overview waiting-on-you feed |
| Coverage/path classifier + dependency-depth staging | NEW, client-side over existing DAG | Plan (deferred) |
| Batch ticket create + dependency wiring in one flow | NEW (thin composition) | Plan breakdown (deferred) |
| Agentic proposal generation, flesh-out agent, milestone entity | LATER (agentic-workflows pass) | Plan / Ideas |
| Ticket CRUD/transitions, TicketDependency DAG, crons, schedule windows, steering, GitLab reads, pending-input answers, session `project_id` | REAL today | everything else |

## Build order (tickets)

1. Chrome: project strip, sidebar group, tabs (Plan = coming soon), `]`/`[` hop, delete index, attention rollup endpoint.
2. Overview tab (+ diff-stat endpoint, MR awaiting-review flag, project-filtered agents list).
3. History tab (+ unified activity feed endpoint).
4. Settings tab lift (+ global Settings→Projects becomes deep links).

Chain after v2 merge readiness; all work happens on the v2 line.
