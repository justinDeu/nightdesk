# nightdesk-pm setup — initialize a project

Follow this once per project, when `SKILL.md` finds no `.nightdesk/profile.md` at the managed repo root. The result: a nightdesk Project, a scoped PM token, and a committed `.nightdesk/` scaffold the skill reads every shift.

## 0. Preconditions

- A running nightdesk instance (`GET {base}/healthz`).
- An admin available: project creation and token minting need admin auth (root bearer or the SPA). If this session lacks admin credentials, prepare the exact requests below and hand them to the human to execute.

## 1. Gather facts — ask, never guess

Interview the human for every field. Wrong guesses here poison every future shift.

- Managed repo root (`source_path`) and merge target branch (pushed anywhere, or local-only?).
- API base URL.
- Execution profile(s) tickets should use: list with `GET /api/v1/profiles`, confirm the default and any allowed alternates by id.
- Workspace mode (usually `git_worktree`) and base ref.
- The project's warm-up command (what a cold worktree needs before tests pass), boot/smoke command, and full test-suite command.
- Hot files (where semantic conflicts live) and review-escalation rules (which changes get the strong model).
- Design sources (canon docs, roadmap) and any live-environment hazards (shared desktops, real user data, ports).
- GitLab: does this project have repo links attached (`GET /api/v1/projects/{pid}/repo-links`)? If intake is wanted, note the link ids.
- Parallelism policy: max tickets running at once for THIS project, and the queued buffer. Check the global cap first (`GET /api/v1/config` → `max_parallel`); the per-project number is skill-enforced policy and must not assume the server will hold it.

## 2. Create or find the Project

Find first: `GET /api/v1/projects` and match by slug/source_path. Create only if absent:

```
POST /api/v1/projects
{"name": "...", "slug": "...", "source_path": "/abs/repo/root",
 "default_workspace_mode": "git_worktree", "default_base_ref": "main"}
```

Optional fields: `default_worktree_name_template`, `default_linked_workspaces`, `default_toolchains`, `default_tool_paths`, `color`. Admin-scoped (`projects.write`).

## 3. Mint the PM token (admin action)

Default: the **`operator`** bundle — the PM shift needs `tickets.run` for run-now / cancel / continue / new-conversation during incident recovery. Add `integrations.read` to the scope list when GitLab intake is wanted (bundles expand to a snapshot at mint; extra scopes ride along).

```
POST /api/v1/tokens
{"name": "pm-<slug>", "bundle": "operator"}
```

Bundle catalog: `GET /api/v1/tokens/catalog`. Strict alternative: `pm-agent` (no `tickets.run`) for a PM that drafts, transitions, and archives but where the human presses run — document the gap in the ledger if chosen.

Storage: write the token to `~/.config/nightdesk/tokens/<slug>.token`, mode 0600, **outside the repo**. The profile records only the path (`token_hint`). Never write the token into `.nightdesk/` — gitignore is not a secret boundary.

## 4. Scaffold `.nightdesk/`

At the managed repo root:

1. `mkdir -p .nightdesk/scratch`
2. Copy `templates/gitignore` (in this skill) to `.nightdesk/.gitignore`.
3. Copy `templates/profile.md` to `.nightdesk/profile.md`; fill **every** frontmatter field and every prose section from step 1. Delete the template's example comments once real content replaces them.
4. Copy `templates/ledger.md` to `.nightdesk/ledger.md`.
5. Commit `profile.md` + `.gitignore` in the managed repo — that is the entire tracked footprint. `ledger.md` and `scratch/` stay local.

Multi-repo projects (linked workspaces): `.nightdesk/` lives at the primary `source_path` root.

## 5. Smoke test

With `NIGHTDESK_TOKEN` set from the new token file:

- `GET /api/v1/projects/{project_id}` → 200.
- `GET /api/v1/tickets?project_id=...` → 200 (empty list is fine).
- Confirm `default_profile_id` (and each allowed alternate) exists via `GET /api/v1/profiles/{id}`.
- Write the first **SHIFT HANDOFF** entry in the ledger: setup date, token hint path, chosen bundle, parallelism policy, and anything deferred.

## Migrating an existing project-specific PM skill

If the project already has a hand-rolled PM skill (the pattern: a generic "Part 1" plus a project "Part 2"): move Part 2's content into `.nightdesk/profile.md` (machine facts → frontmatter, prose → the matching sections), move or symlink its ledger to `.nightdesk/ledger.md`, then retire the old skill so it stops competing with `nightdesk-pm` for triggers.
