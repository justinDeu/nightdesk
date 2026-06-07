# Management UX Guide

This document covers the ticket management surfaces in nightdesk: labels,
inbox, list view, grouping, keyboard navigation, and execution preview.

For install and setup, see the main [README](../README.md).

---

## Inbox

The inbox is a triage surface for under-specified work. Items appear here when
they lack a workspace source path or profile — the minimum fields needed to
run. The inbox view renders blockers ("required before promoting") so the user
knows exactly what to fill in.

### Keyboard triage

| Key | Action |
| --- | --- |
| `J` / `K` | Move cursor down / up |
| `A` | Accept (promote to draft or queued) |
| `D` | Dismiss / archive |
| `E` | Open edit modal |
| `L` | Open label picker |
| `P` | Open priority picker |

### Promoting

Clicking **Promote** on an inbox item opens a full edit modal with the required
fields highlighted. Once complete, the user can either:

- **Promote to draft** — places the ticket on the board for later scheduling.
- **Accept & queue** — immediately hands it to the scheduler.

### Screenshots needed

- `inbox-triage.png` — inbox page with mixed complete/incomplete items.
- `inbox-promote-modal.png` — promote modal with required-field highlights.

---

## Labels

Labels are simple named+colored tags. They are independent of ticket status and
the run lifecycle. Labels appear as chips on ticket cards, in the sidebar, and
on the detail page. They are queryable (`label:bug`) and groupable on the board.

### Management

- Create/edit/delete labels on the **Settings → Labels** page.
- Inline editing in the list view: click the label anchor to open a lazy-loaded
  picker popover.
- Bulk label operations: select multiple tickets, then add labels (union
  semantics — existing labels are preserved).

### Seed data

The demo seeder creates 8 labels: `backend`, `ui/ux`, `bug`, `research`,
`infra`, `docs`, `cleanup`, `feature`. Tickets are pre-labelled for grouping
demos.

### Screenshots needed

- `labels-settings.png` — label management page.
- `label-chips-board.png` — board cards with label chips.
- `label-inline-picker.png` — inline label picker in list view.
- `label-grouped-board.png` — board grouped by label.

---

## Board Grouping

The board supports grouping tickets into columns by:

- **Status** (default) — the standard Kanban layout.
- **Label** — one column per label; multi-label tickets appear in each column.
- **Project** — columns for each project, plus "No project".
- **Priority** — columns for each priority level.

Grouping is selected via a dropdown in the board toolbar. The grouped board is
read-only (no drag-and-drop between grouped columns). The query bar and
project filter still work within the grouped view.

### Screenshots needed

- `board-group-by-project.png` — board grouped by project.
- `board-group-by-priority.png` — board grouped by priority.

---

## List View

The list view provides a flat, sortable table of all tickets. It shares the
same filtered set as the board (query and project filter parity) but renders
rows instead of columns. Features:

- **Inline editing** — click any property chip (status, priority, project,
  profile, labels) to change it without opening the full edit modal.
- **Grouping** — by status, project, priority, profile, or none.
- **Ordering** — manual, priority, updated, created, title.
- **Cursor** — rows carry `data-ticket-id` for keyboard navigation.
- **Bulk selection** — checkboxes for multi-ticket operations.

### Screenshots needed

- `list-view-default.png` — default list view with status grouping.
- `list-inline-edit.png` — inline property picker in a list row.

---

## Display Settings

Display settings (group + order) are URL query-param backed. There is no
server-side persistence for display state — the URL IS the display config:

```
/list?group=priority&order=title
/board?group=label
```

This means:

- Sharing a URL shares the exact view configuration.
- Bookmarks serve as "saved views" without a dedicated model.
- The command palette includes a dormant `ndSavedViews` hook for future
  named-view persistence.

---

## Keyboard Cursor Navigation

The board and inbox support keyboard-driven cursor navigation:

| Key | Action |
| --- | --- |
| `H` | Move cursor left (previous column) |
| `J` | Move cursor down (next ticket in column) |
| `K` | Move cursor up (previous ticket in column) |
| `L` | Move cursor right (next column) |

The cursor is indicated by the `nd-cursor-active` CSS class. It survives
board polling (HTMX OOB swaps restore the cursor position).

### Property shortcuts (with focused ticket)

When a ticket has the cursor, additional shortcuts act on it:

- `Shift+A` — Archive
- `Shift+R` — Run now
- `Shift+Q` — Requeue
- `Shift+P` — Open priority picker
- `Shift+L` — Open label picker
- `Shift+S` — Open status picker

### Screenshots needed

- `keyboard-cursor-board.png` — board with cursor highlighted on a card.

---

## Execution Context Preview

The execution context preview shows how project defaults, profile settings, and
per-ticket overrides merge into the actual run configuration. It appears on:

- **Ticket create/edit modal** — live-updating as fields change.
- **Board sidebar** — read-only summary for the selected ticket.
- **Ticket detail page** — read-only summary below the run history.
- **Inbox promote modal** — shows what the promoted ticket will run with.

### Provenance chips

Each field in the preview carries a provenance badge:

| Badge | Meaning |
| --- | --- |
| **Global** | From the nightdesk config (e.g. default model). |
| **Project** | From the project's defaults (e.g. toolchains, workspace mode). |
| **Profile** | From the assigned profile (e.g. allowed tools, network mode). |
| **Ticket** | Per-ticket override (e.g. toolchain enable/disable). |
| **Derived** | Computed from other fields (e.g. git-push denied when profile is read-only). |

### Screenshots needed

- `execution-preview-modal.png` — preview in the create/edit modal.
- `execution-preview-sidebar.png` — preview in the board sidebar.
- `execution-preview-detail.png` — preview on the ticket detail page.
- `provenance-chips.png` — close-up of provenance badges.

---

## Demo Data

Run `nightdesk-seed-demo` to populate a demo database with realistic data:

- 14+ tickets across all statuses (inbox, draft, queued, running, review, archived).
- 8 labels with distinct colors, pre-assigned to tickets.
- 3 projects (Web App, Platform, Demo project) with varied defaults.
- 3 inbox items (2 incomplete, 1 complete).
- Finished runs with synthetic NDJSON transcripts.
- Worker heartbeat for the running ticket.
- Effective-config showcase tickets with toolchain overrides.

```bash
nightdesk-seed-demo --reset   # wipe and re-seed
```
