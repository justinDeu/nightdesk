# RFC: First-Class Project Support

**Status**: Draft
**Date**: 2026-05-29
**Author**: nightdesk investigator

## Problem

Users work repeatedly against the same set of repositories (nightdesk, blubblub, etc.). Today each ticket is an isolated unit pinned to a `cwd` and optionally a `workspace_mode`/`worktree_name`. Three friction points result:

1. **Repeated setup.** Every ticket re-picks `cwd`, `profile_id`, `workspace_mode`, worktree naming, and linked workspaces, even though the answers are nearly identical for tickets on the same codebase.
2. **No project-level grouping.** The board, archive, and search expose no project filter. Users scan titles or guess at `cwd` prefixes.
3. **No home for project-level config.** Per-project defaults (preferred profile, default workspace mode, linked workspaces, branch naming, work-hours overrides) live in the user's head or get duplicated across tickets.

## 1. What Is a Project in Nightdesk?

**Recommendation: a project is a named bundle of defaults with an optional grouping scope.**

Specifically, a project is:

- A **named label pinned to a `cwd`** (repo root).
- A **bundle of ticket defaults**: `cwd`, `profile_id`, `workspace_mode`, default linked workspaces, worktree name template.
- A **filter scope** for board, archive, search, and analytics.

Explicitly **not** in v1:

- A lifecycle container with retention policies or "members."
- A permissions boundary (projects do not gate access).
- A discoverable entity checked into the repo (no `.nightdesk/project.toml`).

This framing is the lightest thing that solves all three friction points. It avoids scope creep into team management, RBAC, or repo-level config discovery.

### Alternatives considered

| Framing | Why not v1 |
|---|---|
| Lightweight label only (just a name + `cwd`) | Doesn't solve pain point 1 (repeated setup). You'd still re-enter all defaults per ticket. |
| Full lifecycle container (settings, members, retention) | Scope explosion. No user has asked for this yet. The grouping/filtering value is achievable without it. |
| Repo-discoverable (`.nightdesk/project.toml`) | Adds a deployment coupling. Projects should be a nightdesk-side concern at this stage. Can be layered on later. |

## 2. Data Model

### 2.1 New table: `projects`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | `String` | PK | UUID |
| `name` | `String` | unique, not null | Human-readable ("nightdesk", "blubblub") |
| `slug` | `String` | unique, not null | URL-safe identifier ("nightdesk", "blubblub"). Auto-derived from name on create, editable. |
| `cwd` | `String` | not null | Default working directory (repo root) |
| `default_profile_id` | `String` | FK -> `profiles.id`, nullable | Profile to pre-select on ticket create |
| `default_workspace_mode` | `String` | nullable | `"in_place"`, `"directory"`, `"git_worktree"` |
| `default_worktree_name_template` | `String` | nullable | e.g. `"{slug}"` or `"feat/{slug}"`. `{slug}` replaced with URL-safe ticket title slug. |
| `default_base_ref` | `String` | nullable | Default base ref for worktrees |
| `default_linked_workspaces` | `JSON` | nullable | Array of linked workspace specs to auto-attach: `[{source_path, kind, access, label}]` |
| `color` | `String` | nullable | Hex color for UI badges (e.g. `"#3b82f6"`) |
| `icon` | `String` | nullable | Emoji or icon identifier for visual distinction |
| `position` | `Integer` | default 0 | Sort order in project lists |
| `archived_at` | `DateTime` | nullable | Soft delete timestamp |
| `created_at` | `DateTime` | tz=True | |
| `updated_at` | `DateTime` | tz=True | |

### 2.2 Tickets association

Add to `tickets` table:

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | `String` | FK -> `projects.id`, nullable, indexed | Nullable so existing tickets keep working |

**Why nullable?** Existing tickets must continue to function. New tickets created without a project context (via API directly, or before any projects exist) have `project_id = NULL`. The board shows all tickets by default; project filtering is opt-in.

**Backfill strategy:** Provide a one-time migration helper (a CLI command or a "backfill" button in settings) that matches tickets to projects by `cwd` prefix. For example, if project "nightdesk" has `cwd = "/home/user/repos/nightdesk"`, assign all tickets with that `cwd` prefix to that project. The migration is optional and can be run multiple times (idempotent). Existing tickets with no matching project remain null.

### 2.3 Relationship to profiles

Projects and profiles are **orthogonal**.

- A profile defines **sandbox configuration** (filesystem access, tools, network, model, credentials, permission mode).
- A project defines **working context and defaults** (which repo, which workspace mode, which profile to prefer).
- A project's `default_profile_id` is a convenience default. When creating a ticket within a project, the profile dropdown pre-selects this value but the user can override it. The ticket stores `profile_id` directly; the project default is only applied at creation time.

This means a project does **not** subsume profile selection. You can use the same profile across multiple projects, and the same project can have its default profile changed without affecting existing tickets.

### 2.4 Workspace composition with project defaults

When a ticket is created within a project context:

1. The ticket's `cwd` is pre-filled from `project.cwd`.
2. The ticket's `workspace_mode` is pre-filled from `project.default_workspace_mode`.
3. If the project has `default_worktree_name_template`, the worktree name field is pre-derived from the ticket title.
4. If the project has `default_linked_workspaces`, those workspace entries are pre-populated in the linked workspaces section of the form.

All of these are pre-fill, not enforcement. The user can change any of them before saving.

### 2.5 Migration plan

**Alembic revision `0011_projects`:**

1. Create `projects` table with all columns.
2. Add `project_id` column to `tickets` (nullable FK).
3. Create index on `tickets.project_id`.
4. No data migration in the alembic revision itself. Backfill is a separate, user-triggered operation.

**Backfill command** (new CLI endpoint or API endpoint):

- For each project, find tickets where `tickets.cwd = project.cwd` (exact match).
- Set `project_id` on those tickets.
- Report count of tickets assigned.
- Idempotent: re-running does not unset tickets already manually assigned to a different project.

## 3. Features Unlocked

### v1 features

#### 3.1 Project-scoped board view

**UX:** A project filter pill (similar to the existing profile filter pills on the board) appears above the board columns. Clicking it opens a dropdown listing all active projects plus an "All projects" option. Selecting a project filters all four board columns to show only tickets with that `project_id` (and tickets with `project_id = NULL` are hidden). The selection persists in the URL query param and session cookie.

**API:** `GET /board?project_id={id}` (HTMX), `GET /api/v1/tickets?project_id={id}` (JSON). The existing `status` and `profile_id` filters compose with `project_id`.

**Rationale:** This directly solves pain point 2 (finding tickets for a project). It reuses the existing filter-pill pattern and requires no new visual components beyond a dropdown.

#### 3.2 Project filter on archive + search

**UX:** The archive page gains a project dropdown filter alongside the existing outcome, profile, and date filters. Search results (`/header/search`) show the project name as a badge on each hit.

**API:** `GET /archive?project_id={id}`, `GET /api/v1/search?q=...&project_id={id}`.

#### 3.3 Project-defaulted ticket creation

**UX:** When the board is filtered to a project, the "+ New ticket" button creates a ticket pre-filled with the project's defaults (`cwd`, `profile_id`, `workspace_mode`, worktree name template, linked workspaces). The ticket edit modal shows the pre-filled values, all editable. On save, the ticket's `project_id` is set.

A secondary flow: from the project list (settings or a future project home), an "Add ticket" action does the same thing.

**API:** `POST /api/v1/projects/{id}/tickets` creates a ticket with the project's defaults applied server-side. The caller can still override any field. The JSON endpoint returns the created ticket with all defaults resolved.

#### 3.4 Project CRUD

**UX:** A "Projects" section under Settings (or a dedicated `/projects` page) with a list of projects and create/edit/delete actions. The form collects: name, slug, cwd, default profile, default workspace mode, worktree name template, linked workspaces, color.

**API:**
- `GET /api/v1/projects` — list all (active by default, `?archived=true` for all)
- `POST /api/v1/projects` — create
- `GET /api/v1/projects/{id}` — get one
- `PATCH /api/v1/projects/{id}` — update
- `DELETE /api/v1/projects/{id}` — archive (soft delete, sets `archived_at`). Hard delete only if no tickets reference it, otherwise 409.

#### 3.5 Project switcher in the nav

**UX:** A small dropdown in the top nav bar (next to or replacing the search bar area) that shows the current project context. Clicking it lists projects for quick switching. Selecting a project navigates to the board filtered by that project. This is a convenience shortcut, not a new page.

This leverages the existing command palette (`Cmd+K`). Project names are indexed in the palette for quick navigation.

### Deferred to post-v1

| Feature | Why defer |
|---|---|
| **Project dashboard** (per-project analytics, token spend by model, throughput, review time) | Requires new analytics aggregation queries. The analytics page already groups by profile and model. Adding project grouping is a follow-up once `project_id` is populated. |
| **Project-level work hours / scheduler overrides** | Schedule windows are global and complex (day masks, overlap resolution). Per-project overrides multiply this complexity. Not needed until users run multiple projects with different schedules concurrently. |
| **Project home page** (landing page with recent runs, open tickets, commits) | Nice-to-have. The board filtered by project + archive filtered by project covers the core need. A dedicated home page is a UX polish pass. |
| **Per-project profile pinning** (enforce a profile, not just default) | Already achievable: the project defaults to a profile, and the user can change it. Enforcement adds a permission-like concept that should be designed with intention. |
| **Templated worktree names beyond `{slug}`** | The `{slug}` template covers the common case. Advanced templates (date tokens, custom variables) add parsing complexity for marginal gain. |
| **Repo-discoverable projects** (`.nightdesk/project.toml`) | Deployment coupling. The nightdesk database is the source of truth for now. |
| **Project-level cron jobs** (cron jobs auto-associated with a project) | Cron jobs already have `cwd` and `profile_id`. They can be manually associated in a follow-up. |

## 4. API Surface

### 4.1 Project endpoints

#### `GET /api/v1/projects`

List all projects.

**Query params:**
- `archived` (bool, default false) — include archived projects

**Response:** `list[ProjectOut]`

```json
[
  {
    "id": "uuid",
    "name": "nightdesk",
    "slug": "nightdesk",
    "cwd": "/home/user/repos/nightdesk",
    "default_profile_id": "profile-uuid",
    "default_workspace_mode": "git_worktree",
    "default_worktree_name_template": "{slug}",
    "default_base_ref": "main",
    "default_linked_workspaces": [],
    "color": "#3b82f6",
    "icon": null,
    "position": 0,
    "archived_at": null,
    "created_at": "...",
    "updated_at": "..."
  }
]
```

#### `POST /api/v1/projects`

Create a project.

**Request:** `ProjectCreate`

```json
{
  "name": "nightdesk",
  "slug": "nightdesk",
  "cwd": "/home/user/repos/nightdesk",
  "default_profile_id": "profile-uuid",
  "default_workspace_mode": "git_worktree",
  "default_worktree_name_template": "{slug}",
  "default_base_ref": "main",
  "default_linked_workspaces": [],
  "color": "#3b82f6",
  "icon": null,
  "position": 0
}
```

**Response:** `ProjectOut` (201)

**Errors:** 409 if `name` or `slug` already exists.

#### `GET /api/v1/projects/{id}`

Get a single project.

**Response:** `ProjectOut`

**Errors:** 404

#### `PATCH /api/v1/projects/{id}`

Update a project (partial).

**Request:** `ProjectUpdate` (all fields optional)

**Response:** `ProjectOut`

**Errors:** 404, 409 on name/slug collision.

#### `DELETE /api/v1/projects/{id}`

Archive a project (soft delete).

**Response:** 204

**Errors:** 404, 409 if tickets reference this project (must reassign or nullify first).

### 4.2 Ticket creation with project defaults

#### `POST /api/v1/projects/{id}/tickets`

Create a ticket with project defaults applied. Merges the project's defaults into the ticket fields, then creates the ticket. Any field explicitly provided by the caller overrides the project default.

**Request:** `TicketCreate` (same schema as today, but all fields except `title` are optional since the project fills them in)

**Behavior:**
1. Load project by `{id}`.
2. Build ticket defaults from project: `cwd`, `profile_id`, `workspace_mode`, worktree name (resolved from template + title slug), linked workspaces.
3. Merge caller's explicit fields on top (caller wins).
4. Set `ticket.project_id = project.id`.
5. Create ticket normally.

**Response:** `TicketOut` (201)

### 4.3 Modified existing endpoints

#### `GET /api/v1/tickets`

Add query param: `project_id` (string, optional). Filters by `tickets.project_id = :id`. Pass `"null"` to find tickets with no project.

#### `GET /api/v1/search`

Add query param: `project_id` (string, optional). Filters search results by project.

#### HTMX board and archive endpoints

Mirror the same `project_id` query param:
- `GET /board?project_id={id}` — board filtered by project
- `GET /board/columns?project_id={id}` — column partials filtered by project
- `GET /archive?project_id={id}` — archive filtered by project
- `GET /archive/rows?project_id={id}` — archive rows filtered by project

The project filter composes with all existing filters (status, profile, outcome, date, search).

### 4.4 Backfill endpoint

#### `POST /api/v1/projects/backfill`

Assign tickets to projects based on `cwd` matching.

**Request:**

```json
{
  "dry_run": true
}
```

**Response:**

```json
{
  "assigned": {
    "project-uuid-1": 14,
    "project-uuid-2": 7
  },
  "unassigned": 3
}
```

When `dry_run` is false, actually writes the `project_id` values.

## 5. UI / UX Changes

### 5.1 Board: project filter pill

Add a project filter dropdown above the board columns, styled identically to the existing profile filter pill. Position it to the left of the profile filter. The dropdown lists:

- "All projects" (default)
- One entry per active project, showing name and color dot

When a project is selected:
- Only tickets with that `project_id` appear in all four columns.
- The URL updates to `/?project_id={slug}` for shareable links.
- The selection persists in a session cookie so navigation returns to the filtered view.
- A small "x" clears the filter.

No changes to the column structure, card layout, or drag-and-drop behavior.

### 5.2 Ticket edit modal: project context

When creating a ticket while a project filter is active:
- The modal title shows "New ticket in [Project Name]".
- `cwd` is pre-filled and the field is visually highlighted to show it came from a default.
- `profile_id` dropdown is pre-selected.
- `workspace_mode` is pre-selected.
- If the project has a worktree name template, the worktree name field is auto-populated with the resolved value.
- If the project has default linked workspaces, they appear pre-populated in the linked workspaces section.

All fields remain editable. The user can change any pre-filled value.

When creating a ticket with no project filter active, the modal works exactly as today.

### 5.3 Ticket card: project badge

On each ticket card (board and archive), show a small colored dot or tag with the project name when a project is assigned. This uses the project's `color` field. Keep it subtle — a small pill next to the status chip.

### 5.4 Archive: project filter

Add a project dropdown to the archive filter bar, alongside the existing outcome and profile dropdowns. Same component as the board project filter.

### 5.5 Settings: project management

Add a "Projects" tab to the settings page (alongside Scheduling, Claude, Worktrees, Notifications). The Projects tab shows:

- A list of projects with name, cwd, default profile, and ticket count.
- Create / Edit / Archive actions.
- A "Backfill tickets" button that runs the backfill endpoint.
- The edit form collects all project fields.

Alternatively (if the settings tabs are getting crowded), a dedicated `/projects` page with the same content. The settings approach is simpler for v1.

### 5.6 Nav: project quick-switch

Add the project list to the existing command palette (`Cmd+K`). Typing a project name navigates to the board filtered by that project. No new UI component needed — the command palette already supports navigation targets.

For visual context, a small project indicator (colored dot + name) appears in the nav bar when a board filter is active, acting as a breadcrumb.

### 5.7 No new components needed

All of the above reuses existing patterns:
- Filter pill/dropdown (same as profile filter)
- Settings tab (same as other settings tabs)
- Command palette entries (same as ticket search)
- Ticket card badges (same as status chips)

## 6. Open Questions

Ranked by impact on the design:

### Q1: Should tickets require a project?

**Recommendation: No.** `project_id` is nullable. Tickets created before projects exist, or created via the API without a project context, work exactly as today. This avoids a migration cliff and keeps the feature opt-in.

### Q2: What happens to tickets whose `cwd` doesn't match any project?

**Recommendation: Leave `project_id` null.** No auto-creation of "unassigned" projects. The board shows unprojected tickets when no filter is active. Users can manually assign them or run backfill.

### Q3: Should linked workspaces be project-level, ticket-level, or both?

**Recommendation: Both.** Projects define default linked workspaces (pre-filled on ticket create). Tickets store their own linked workspaces (in `ticket_workspaces` as today). The project defaults are a creation-time convenience, not a runtime constraint. This avoids changing the workspace resolution logic.

### Q4: Should project slugs be auto-generated or user-specified?

**Recommendation: Auto-generated from name (slugified), user-editable.** Same pattern as many slug-based systems. If the user doesn't provide a slug, derive it from the name (lowercase, hyphens for spaces, strip special chars). Allow override at creation time.

### Q5: Should deleting a project nullify `project_id` on its tickets or block deletion?

**Recommendation: Block deletion if tickets reference it (409).** Require the user to reassign or nullify tickets first. This prevents accidental data loss and keeps the relationship explicit. Archiving (soft delete) is always allowed.

### Q6: Should the project filter persist across page navigation?

**Recommendation: Yes, via session cookie.** When a user selects a project on the board, navigating to archive or back to board preserves the selection. Navigating to settings or analytics clears it. The cookie value is the project slug, with a 24-hour TTL.

### Q7: Should projects appear in the FTS5 search index?

**Recommendation: Not in v1.** Project names are short and few. The command palette and filter dropdown are sufficient for discovery. Adding projects to FTS5 would require index rebuild on project create/rename, which adds migration complexity for little gain.

## 7. Implementation Sequencing

Suggested order for the implementation ticket:

1. **Alembic migration** — create `projects` table, add `project_id` to `tickets`.
2. **Domain layer** — `src/nightdesk/domain/projects.py` with CRUD functions, backfill logic.
3. **JSON API** — project CRUD endpoints + modified ticket/search endpoints.
4. **HTMX API** — board/archive filter params, project settings page.
5. **UI: settings** — project management in settings.
6. **UI: board filter** — project filter pill on board.
7. **UI: ticket creation** — pre-fill defaults from project context.
8. **UI: archive + search** — project filter and badges.
9. **UI: command palette** — project navigation entries.
10. **Backfill tool** — CLI command or API endpoint for ticket assignment.

Steps 1-3 form the core and can ship as a single PR. Steps 4-10 are incremental and can be split across PRs.
