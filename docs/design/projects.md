# RFC: Optional Project Context

**Status**: Draft
**Date**: 2026-05-30
**Author**: nightdesk investigator

## Problem

Users often run Nightdesk against the same repositories. Today each ticket carries its own `cwd`, `workspace_mode`, `worktree_name`, linked workspaces, and profile. That keeps tickets independent, but it creates repeated setup and makes old work harder to find.

The sharp problems are:

1. **Repeated workspace setup.** The same repo path, worktree mode, base ref, worktree naming pattern, and linked workspaces get entered again and again.
2. **Wrong-directory risk.** A ticket can be created against the wrong `cwd`. Since Nightdesk can run write-capable agents, this is more than a convenience issue.
3. **Weak filtering.** The board, archive, and search do not have a repo-level filter. Users infer context from titles or paths.
4. **Poor archive archaeology.** Users need to answer questions like "what did Nightdesk already try in this repo?" without scanning unrelated tickets.
5. **Context loss in child tickets.** A run that creates follow-up tickets should usually keep the same work context.

## 1. What Is a Project in Nightdesk?

A project is an optional saved work context.

A project is:

- A named label for a repo or work area.
- A source `cwd` default.
- A bundle of workspace defaults used when creating tickets.
- A filter value for board, archive, and search.

A project is not:

- Required for every ticket.
- A ticket container with its own page.
- A permissions boundary.
- A profile owner.
- A lifecycle policy.
- A repo-checked config file.

This keeps the concept small. Projects help users create and find tickets. They do not change what a ticket is.

### v1 scope

v1 includes:

- Project CRUD in Settings.
- Nullable `tickets.project_id`.
- Project assignment on ticket create and edit.
- Board filter.
- Archive filter.
- Search filter.
- Ticket card and archive badges.
- Project defaults for workspace fields.
- Backfill by `cwd` with dry-run.
- Parent-to-child project inheritance for child tickets created through Nightdesk APIs.

v1 does not include:

- Project home pages.
- Project ticket list pages.
- Project dashboards.
- Project analytics.
- Project-level cron jobs.
- Project-level scheduling.
- Project-enforced profiles.
- Hard delete in the UI.
- Repo-discoverable `.nightdesk/project.toml`.

## 2. Relationship to Profiles

Projects and profiles are separate concepts.

- A profile defines sandbox behavior: filesystem access, tools, network intent, model, credentials, and permission mode.
- A project defines work context: repo path, workspace mode, base ref, worktree naming, and linked workspace defaults.

v1 should not store a profile default on the project.

The reason is safety and clarity. A project-level profile default can make users think the project owns or enforces sandbox policy. It does not. Profiles are security-sensitive, and they should stay explicit on each ticket.

If repeated profile selection becomes a clear pain later, add a nullable `suggested_profile_id`. It should be labeled as a suggestion, applied only at ticket creation, and never mutate existing tickets.

## 3. Data Model

### 3.1 New table: `projects`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | `String` | PK | UUID |
| `name` | `String` | unique, not null | Human-readable name |
| `slug` | `String` | unique, not null | URL-safe identifier, derived from name unless provided |
| `cwd` | `String` | not null | Source repo or work area path |
| `default_workspace_mode` | `String` | nullable | `"in_place"`, `"directory"`, or `"git_worktree"` |
| `default_worktree_name_template` | `String` | nullable | Example: `"{slug}"` or `"feat/{slug}"` |
| `default_base_ref` | `String` | nullable | Default base ref for worktrees |
| `default_linked_workspaces` | `JSON` | nullable | Array of linked workspace specs |
| `color` | `String` | nullable | Hex color for badges |
| `position` | `Integer` | default 0 | Sort order in dropdowns |
| `archived_at` | `DateTime` | nullable | Soft archive timestamp |
| `created_at` | `DateTime` | tz=True | |
| `updated_at` | `DateTime` | tz=True | |

`cwd` should be normalized before storage. The exact normalization should match existing ticket path behavior where possible. Backfill must compare normalized paths, not raw strings.

### 3.2 Tickets association

Add to `tickets`:

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `project_id` | `String` | FK -> `projects.id`, nullable, indexed | Optional project assignment |

`project_id` is nullable. Tickets without a project remain valid and visible.

The default board, archive, and search results include all tickets. A project filter narrows results. A separate "No project" filter shows tickets where `project_id IS NULL`.

### 3.3 Workspace semantics

Project `cwd` is the source working context. Ticket `cwd` remains the execution directory.

This distinction matters for worktrees:

- A project may point to `/home/user/repos/nightdesk`.
- A ticket may execute in a generated worktree under `~/.local/share/nightdesk/work/...`.
- The ticket should still belong to the project.

Project assignment must not rely only on ticket execution `cwd` after a worktree is created.

### 3.4 Defaults applied at ticket creation

When a ticket is created with a project:

1. Pre-fill `cwd` from `project.cwd`.
2. Pre-fill `workspace_mode` from `project.default_workspace_mode` if present.
3. Pre-fill `base_ref` from `project.default_base_ref` if present.
4. Resolve `default_worktree_name_template` from the ticket title if present.
5. Pre-fill linked workspaces from `project.default_linked_workspaces` if present.
6. Set `ticket.project_id`.

All fields remain editable before save. API callers can override defaults by sending explicit values.

Defaults are creation-time conveniences. Changing a project later does not rewrite existing tickets.

### 3.5 Child tickets

Child tickets created from a run should inherit the parent ticket's `project_id` by default.

Callers may override this by passing a different `project_id` or explicit null, depending on the API shape used for child ticket creation.

This keeps follow-up work findable under the same board, archive, and search filters.

## 4. Migration and Backfill

### 4.1 Alembic revision `0011_projects`

1. Create the `projects` table.
2. Add nullable `project_id` to `tickets`.
3. Add an index on `tickets.project_id`.
4. Do not assign existing tickets in the migration.

Backfill is user-triggered and reversible by editing tickets.

### 4.2 Backfill rules

Backfill assigns tickets to projects based on normalized paths.

Rules:

1. Never overwrite a non-null `ticket.project_id`.
2. Prefer exact normalized `ticket.cwd == project.cwd` matches.
3. Optionally allow path-boundary descendant matches.
4. If more than one project matches, choose the longest matching project path.
5. Never use naive string prefix matching.
6. Always support dry-run.
7. Dry-run output must show which tickets would be assigned to which project.

Path-boundary descendant matching avoids false matches like `/repos/foo` matching `/repos/foobar`.

## 5. API Surface

### 5.1 Project endpoints

#### `GET /api/v1/projects`

List projects.

Query params:

- `archived` bool, default false. When false, return only active projects.

Response: `list[ProjectOut]`.

#### `POST /api/v1/projects`

Create a project.

Request: `ProjectCreate`.

Fields:

- `name`
- `slug`, optional. Derived from name when omitted.
- `cwd`
- `default_workspace_mode`, optional.
- `default_worktree_name_template`, optional.
- `default_base_ref`, optional.
- `default_linked_workspaces`, optional.
- `color`, optional.
- `position`, optional.

Errors:

- 409 if `name` or `slug` already exists.
- 400 for invalid path, workspace mode, color, or linked workspace spec.

#### `GET /api/v1/projects/{id}`

Get one project by UUID.

#### `PATCH /api/v1/projects/{id}`

Partially update a project.

Changing project defaults affects future ticket creation only.

#### `DELETE /api/v1/projects/{id}`

Archive a project by setting `archived_at`.

Archiving does not clear `tickets.project_id`. Historical ticket badges can still show the archived project name. Archived projects are hidden from create and filter dropdowns by default.

Hard delete is not exposed in v1.

### 5.2 Ticket creation

Use the existing ticket creation endpoint.

#### `POST /api/v1/tickets`

Add optional `project_id` to `TicketCreate`.

Behavior when `project_id` is present:

1. Load the project.
2. Build defaults from the project.
3. Merge explicit request fields over those defaults.
4. Create the ticket with `ticket.project_id = project.id`.

Do not add `POST /api/v1/projects/{id}/tickets` in v1. One creation path avoids drift between project and non-project tickets.

### 5.3 Ticket update

Existing ticket update flows should allow changing or clearing `project_id`.

This is required so users can fix backfill mistakes and assign one-off tickets manually.

### 5.4 Ticket list

#### `GET /api/v1/tickets`

Add query param:

- `project_id`, optional UUID.
- `project_id=null` returns tickets with no project.

The filter composes with existing status and profile filters.

### 5.5 Search

#### `GET /api/v1/search`

Add query param:

- `project_id`, optional UUID.
- `project_id=null` searches tickets with no project.

Search results should include project name and color when assigned.

Projects themselves do not need to be indexed in FTS for v1.

### 5.6 HTMX routes

Use slug-based UI params for shareable links:

- `/board?project={slug}`
- `/board?project=none`
- `/board/columns?project={slug}`
- `/archive?project={slug}`
- `/archive?project=none`
- `/archive/rows?project={slug}`
- `/header/search?q=...&project={slug}`
- `/header/search?q=...&project=none`

The server resolves slugs to project IDs. Unknown slugs should produce a clear filter error state rather than silently showing all tickets. Archived project slugs should still work for historical links and show that the project is archived.

## 6. UI / UX Changes

### 6.1 Board filter

Add a project dropdown to the board filters.

Options:

- All projects.
- No project.
- One entry per active project, sorted by `position`, then name.

Selecting a project filters all board columns. Selecting "No project" shows only tickets with no project.

The URL stores the filter as `project={slug}` or `project=none`.

No project page is added.

### 6.2 Archive filter

Add the same project dropdown to archive filters.

The filter composes with outcome, profile, date, and text filters.

### 6.3 Search filter

Header search accepts the current project filter when present.

Search results show the project badge for assigned tickets.

When no project filter is active, search covers all tickets.

### 6.4 Ticket cards and archive rows

Show a small project badge when a ticket has a project.

The badge should be subtle:

- project color dot
- project name

Unprojected tickets do not need a badge unless the current filter is "No project".

Archived projects can still render as badges on historical tickets.

### 6.5 Ticket create and edit

Ticket create form:

- If a project filter is active, pre-select that project.
- Apply project defaults to workspace fields.
- Keep all pre-filled fields editable.
- Make the project selector visible so the user can clear or change it.

Ticket edit form:

- Show the assigned project.
- Allow changing the project.
- Allow clearing the project.
- Do not rewrite workspace fields when changing project on an existing ticket unless the user explicitly asks to apply defaults.

This avoids surprising edits to tickets that already have concrete execution settings.

### 6.6 Settings: Projects

Add Projects under Settings.

Settings should support:

- List projects with name, cwd, active or archived state, and ticket count.
- Create project.
- Edit project.
- Archive project.
- Run backfill dry-run.
- Apply backfill after reviewing dry-run output.

Do not add a dedicated project page in v1.

### 6.7 Filter persistence

The project filter should persist in the URL.

Session-cookie persistence is optional. If used, it must not override an explicit URL param.

Do not persist project filters across unrelated pages in a way that surprises users. The URL should be the source of truth for shared links.

### 6.8 Hidden dependency handling

Filtered boards can hide tickets from other projects.

If a visible ticket is blocked by a dependency outside the current project filter, the card should indicate that at least one dependency is hidden by filters.

This prevents users from seeing a blocked ticket without knowing why it cannot run.

## 7. Open Questions

### Q1: Should tickets require a project?

No.

Projects are optional. Tickets without a project stay valid and visible in unfiltered views.

### Q2: Should projects store a profile default?

No for v1.

A future `suggested_profile_id` may be added if repeated profile selection is a proven pain. It must be a weak creation-time suggestion, not enforcement.

### Q3: Should changing a project update existing tickets?

No.

Project defaults apply only at creation time. Existing tickets store concrete values.

### Q4: Should archived projects remain attached to tickets?

Yes.

Archiving hides the project from default selectors but preserves historical context.

### Q5: Should the UI use project slugs or UUIDs?

Use slugs in HTMX URLs and UUIDs in JSON APIs.

Do not call a slug `project_id`. UI params should use `project`. JSON params should use `project_id`.

### Q6: Should projects appear in FTS search results?

No for v1.

Project names are short and already available through filters. Indexing projects adds index maintenance without solving the main use case.

## 8. Implementation Sequence

1. Alembic migration: create `projects`, add nullable `tickets.project_id`, add index.
2. Domain layer: project CRUD, slug validation, path normalization, default application, backfill planning.
3. Ticket service: accept `project_id`, apply defaults on create, allow assignment changes on update.
4. Child ticket creation: inherit parent `project_id` by default.
5. JSON API: project CRUD, ticket filters, search filters, backfill dry-run and apply.
6. HTMX routes: board, archive, and search project filters.
7. Settings UI: project management and backfill review.
8. Ticket UI: create/edit project selector and default prefill.
9. Badges: board cards, archive rows, search results.
10. Dependency visibility: show when blockers are hidden by the active project filter.

The first useful cut is migration, domain, ticket creation/update, Settings CRUD, and filters on board/archive/search. It should still keep projects optional and avoid project pages.
