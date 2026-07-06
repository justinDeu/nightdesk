---
name: nightdesk-ticket-ops
description: Use when performing ticket lifecycle operations against the nightdesk API — create, fetch, update, transition (draft/queued/running/review/archived), run-now, cancel, requeue, archive/unarchive, runs, transcript streaming, search. Assumes auth and base URL are already set up per `nightdesk-api`.
---

# nightdesk ticket operations

Concrete recipes for the JSON API. Assumes `$BASE` and `${AUTH[@]}` are set (see `nightdesk-api`).

## Status lifecycle

```
draft  ──► queued ──► running ──► review ──► archived
  │         ▲           │          │           │
  │         │           │          ▼           │
  └── direct drop ──────┘        queued ◄──────┘
      (running flips run_now=true)
```

Valid transitions (`_VALID_TRANSITIONS`, `src/nightdesk/domain/tickets.py:28`):

| from → to | allowed |
|---|---|
| `inbox` | `draft`, `queued`, `archived` |
| `draft` | `queued`, `running`, `inbox`, `archived` |
| `queued` | `draft`, `running`, `archived` |
| `running` | `review` |
| `review` | `queued`, `archived` |
| `archived` | `queued` |

Anything else returns `409 invalid transition`. Dropping into `running` from `draft` or `queued` sets `run_now=true` so the scheduler picks it on the next tick.

**API surface caveats — the state machine and the JSON API don't fully line up:**
- `POST /api/v1/tickets/{tid}/transition` only accepts targets `draft|queued|running|review|archived`. **`inbox` is NOT a valid `/transition` target** even though `draft → inbox` is state-machine-legal. There is a dedicated endpoint for that one hop instead — `POST /api/v1/tickets/{tid}/send-to-inbox`, valid ONLY from `draft` (`409` from any other status; see "Send to inbox" below). Tickets otherwise move *into* `inbox` by being captured there directly at creation (`status: "inbox"`).
- `archived` is reachable from `review` (`/archive` or `transition status=archived`), from `draft`/`queued` directly (`/archive` or `transition status=archived` — both now archivable, not just `review`), or from `inbox` (decline).

### Send to inbox

```bash
# draft -> inbox only; 409 from queued/running/review/archived.
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/send-to-inbox" | jq '{status}'
```

The single JSON path back into the inbox for an existing ticket — mirrors the old UI's "send to inbox" action for a draft created too soon. It does not chain through other statuses first: a `queued`/`review`/`archived` ticket must be walked back to `draft` via its own path (`transition status=draft` where legal) before this will accept it. The ticket then shows up in `GET /api/v1/inbox` with `blockers` recomputed from its current fields (see "Inbox (triage)" below) — a well-specified ticket sent back has none, since send-to-inbox doesn't strip its profile/workspace.

## Before creating a ticket

Gather these values before issuing the POST. Do not guess or hard-code them.

### 1. Profile (`profile_id`)

Fetch available profiles and ask the user which one to use:

```bash
curl -s "${AUTH[@]}" "$BASE/api/v1/profiles" | jq '.[] | {id, name}'
```

- If only one profile exists, use it without asking.
- If multiple profiles exist, present the list and ask which to use.

### 2. Workspace mode (`workspace_mode`)

| mode | when to use |
|---|---|
| `git_worktree` | **Default for any ticket involving code changes.** Gives the agent an isolated git worktree branched from `source_path`. |
| `directory` | Tickets that only read files, run queries, or operate on non-git directories. |

If unsure, ask the user. When using `git_worktree`, suggest a `worktree_name` derived from the ticket title (e.g. a ticket titled "Fix login bug" → `fix/login-bug`). Show the derived name to the user before creating the ticket.

### 3. Source path (`source_path`)

`source_path` is the repo root / directory the agent operates in — it **replaced the old `cwd` field** (which no longer exists). Confirm it with the user or infer it from the current working directory. Never assume a path. The server turns top-level `source_path` + `workspace_mode` into the ticket's single **primary** workspace.

### 4. Quick checklist

Before sending the POST, confirm:

- [ ] `profile_id` resolved (asked user if multiple profiles)
- [ ] `workspace_mode` chosen (`git_worktree` for code changes, `directory` otherwise)
- [ ] `source_path` confirmed with user or inferred from `$PWD` (or `status="inbox"` to skip the workspace requirement)
- [ ] `worktree_name` derived from title and shown to user (when using `git_worktree`)

## Create a ticket

Required: `title`, `profile_id`. New tickets default to `status="draft"`.

**`profile_id` is optional only for `status="inbox"`** — a captured-but-under-specified inbox item can be created with no profile at all (`profile_id: null` in the response). Any other status still requires it: `422 "profile_id is required"` if omitted. This is the same completeness boundary `ticket_completeness` enforces at promotion time (see "Promote/decline" below) — an inbox item missing a profile just can't be promoted yet, not that it can't exist.

**A workspace is required (exactly one primary). There is no `cwd` field.** Provide the primary workspace one of two ways:
- **Simple:** set top-level `source_path` (absolute; normalized server-side) plus optional `workspace_mode` / `worktree_name`. The server synthesizes the single primary workspace from these.
- **Explicit:** pass a `workspaces` list containing exactly one `role:"primary"` entry (see "Workspaces" below).

Provide neither and the create returns `422 "workspaces must include exactly one primary workspace"` — **unless** `status="inbox"`, the one exception (captured triage items may have no workspace yet).

`workspace_mode` sets the primary workspace's kind (see "Before creating a ticket" for selection guidance):

| mode | behavior |
|---|---|
| `directory` | agent runs in `source_path` as a plain directory |
| `in_place` | runs directly in `source_path` (legacy alias for directory) |
| `git_worktree` | agent gets an isolated git worktree branched from `source_path` |
| `worktree` | reserved; not yet fully implemented |

When using `git_worktree`, pair with `worktree_name` (e.g. `fix/my-feature`) — the server generates one if omitted, but an explicit name is clearer. If this ticket is a **prerequisite** that other (stacked) tickets will `base_ref` onto, also set `"commit_on_finish": true` so a successful run commits its work onto the branch — otherwise dependents provision an empty tree. See "Dependent / stacked tickets".

**Single-line / inline prompt** (fine for short prompts):

```bash
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X POST "$BASE/api/v1/tickets" \
  -d '{
    "title": "...",
    "prompt": "...",
    "profile_id": "<uuid>",
    "source_path": "/home/thor/fun/nightdesk",
    "workspace_mode": "directory"
  }' | jq '{id, title, status}'
```

**Multi-line prompt with markdown / backticks / quotes** (the right pattern — quoting will bite you otherwise):

```bash
# 1. Write the body to a file via heredoc. Use 'JSON' (quoted) so the shell
#    doesn't expand $vars or backticks inside the prompt.
cat > /tmp/ticket.json <<'JSON'
{
  "title": "...",
  "prompt": "Multi-line prompt with `code`, \"quotes\", and\nnewlines as \\n escapes."
}
JSON

# 2. Inject profile_id, source_path, and any other fields with Python.
#    Do NOT use `jq --arg` or `jq '. + {...}'` for this merge — jq chokes on
#    prompts containing backticks, em-dashes, or other non-ASCII characters
#    and returns "Invalid numeric literal" without a useful error. Python is
#    reliable for any prompt content.
python3 -c "
import json
with open('/tmp/ticket.json') as f:
    t = json.load(f)
t['profile_id'] = '<uuid>'
t['source_path'] = '/home/thor/fun/nightdesk'
t['workspace_mode'] = 'git_worktree'
t['worktree_name'] = 'fix/my-feature'
with open('/tmp/ticket.full.json', 'w') as f:
    json.dump(t, f)
"

# 3. POST the merged file.
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X POST "$BASE/api/v1/tickets" \
  --data @/tmp/ticket.full.json | jq '{id, title, status}'
```

JSON requires real `\n` escapes inside string values — literal newlines in the source are fine because `<<'JSON'` preserves them, but each newline inside a `"..."` value must be written as `\n`. Multi-paragraph prompts read more naturally by writing each paragraph as a separate line in the heredoc.

## Workspaces (multi-workspace tickets)

Tickets support a `workspaces` list — the primary workspace plus any additional directories or worktrees exposed to the agent. Pass it instead of (or alongside) top-level `source_path`; it must contain exactly one `role:"primary"` entry. Each entry is a `TicketWorkspaceIn` object:

| field | type | description |
|---|---|---|
| `kind` | `directory`, `git_worktree`, `in_place`, `worktree` | required |
| `role` | `primary`, `linked` | default `linked` |
| `label` | string | human label, default `""` |
| `access` | `read_write`, `read_only` | default `read_write` |
| `source_path` | absolute path | directory or repo root for the workspace |
| `worktree_name` | string | optional branch/worktree name for git_worktree kind |
| `worktree_path` | absolute path | explicit worktree path (server resolves if omitted) |
| `branch` | string | branch to check out |
| `base_ref` | string | ref the new git_worktree branch is cut from (`git worktree add -b <branch> <target> <base_ref>`); defaults to HEAD. See "Dependent / stacked tickets" below |
| `retention` | `preserve`, `cleanup_on_success`, `cleanup_after_review` | default `preserve` |

```bash
# Primary worktree + an extra linked read-only directory. The list MUST contain
# exactly one role:"primary" entry — top-level source_path is NOT consulted to
# fill in a primary when a workspaces list is present.
cat > /tmp/ticket.json <<'JSON'
{
  "title": "...",
  "prompt": "...",
  "workspaces": [
    {
      "kind": "git_worktree",
      "role": "primary",
      "label": "primary",
      "access": "read_write",
      "source_path": "/home/thor/fun/nightdesk",
      "worktree_name": "fix/my-feature"
    },
    {
      "kind": "directory",
      "role": "linked",
      "label": "shared-config",
      "access": "read_only",
      "source_path": "/home/thor/config"
    }
  ]
}
JSON
# Inject profile_id with Python (NOT jq — see the create recipe above).
python3 -c "
import json
t = json.load(open('/tmp/ticket.json'))
t['profile_id'] = '<uuid>'
json.dump(t, open('/tmp/ticket.full.json', 'w'))
"
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X POST "$BASE/api/v1/tickets" --data @/tmp/ticket.full.json | jq '{id, title, status}'
```

`TicketOut.workspaces` is a list of resolved `TicketWorkspaceOut` objects with additional server-populated fields: `resolved_path`, `repo_root`, `git_common_dir`, `relative_path`, `base_sha`, `head_sha`, `state` (`pending` / `ready` / `error`), `position`.

Top-level `worktree_name` and `worktree_path` fields on the ticket are convenience aliases for the primary workspace — prefer setting them via `workspaces` for clarity.

## Dependencies

List, add, and remove dependency edges. A dependency edge gates **execution order only**: the scheduler won't run the dependent ticket until the prerequisite reaches a done state.

```bash
# List a ticket's dependencies
curl -s "${AUTH[@]}" "$BASE/api/v1/tickets/$TID/dependencies" | jq .

# Add an edge: $TID depends on $PREREQ_TID (won't run until $PREREQ_TID finishes)
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X POST "$BASE/api/v1/tickets/$TID/dependencies" \
  -d '{"depends_on_id": "'"$PREREQ_TID"'"}' | jq .   # 201; 422 on cycle

# Remove an edge
curl -s "${AUTH[@]}" -X DELETE "$BASE/api/v1/tickets/$TID/dependencies/$PREREQ_TID"  # 204
```

The POST body is a `DependencyCreate` (`{"depends_on_id": "<prerequisite_tid>"}`). A cycle returns `422`; an unknown ticket returns `404`.

## Dependent / stacked tickets (`base_ref`)

When ticket **B** depends on ticket **A** *and* B's work must **build on** A's code changes (not merely run after it), do **both** of the following:

1. **Add a dependency edge** so B won't start until A finishes (see "Dependencies" above):
   `POST /api/v1/tickets/<B>/dependencies` with `{"depends_on_id": "<A>"}`.
2. **Set B's primary workspace `base_ref` to A's branch name** so B's git_worktree is cut from A's committed work instead of from `main`/HEAD.

### Why all three are needed (the stacking trap)

- The **dependency edge** only gates *execution order*. On its own, B's worktree still branches off HEAD and would **not** contain A's changes.
- **`base_ref`** is what actually makes the worktree build on A. nightdesk provisions a git_worktree with `git worktree add -b <branch> <target> [base_ref]` (see `_create_git_worktree` in `src/nightdesk/worker/workspace.py`): when `base_ref` is set, the new branch is cut from that ref instead of HEAD. Pointing it at A's branch means B's agent opens **on top of A's *committed* work**, so it can't produce work that semantically conflicts with A — eliminating painful end-of-run rebases.
- **The prerequisite must actually have committed work on its branch.** This is the part that is easy to get wrong. nightdesk runs leave the agent's changes **uncommitted in the worktree** by default, so the prerequisite's branch ref stays parked at the commit it was cut from. If B provisions from that branch name, `git worktree add ... <base_ref>` produces a tree with **none of A's actual changes** — B's agent is then told "A's work is already in your tree" and has to improvise a standalone implementation. To make stacking real you must opt in (next section).
- They **compose**: the dep edge guarantees A has already *run* before B provisions, but **only `commit_on_finish` (or a manual commit) guarantees A's branch has actually advanced** by the time B provisions.

### Make the prerequisite commit its work (`commit_on_finish`)

`base_ref` points at a *commit*, not a working tree, so it only carries A's changes if those changes are **on the branch**. Three ways to guarantee that:

1. **Recommended — set `commit_on_finish: true` on the prerequisite (ticket A).** On a successful run, nightdesk auto-commits A's working-tree changes onto its `git_worktree` branch (best-effort, never fails the run; recorded as a `commit_on_finish` transcript event). Then A's branch ref advances and B's `base_ref` actually receives the work. Set it on A at creation alongside the usual fields:

   ```jsonc
   // ticket A (the prerequisite) — opt in so its work lands on its branch
   {
     "title": "A — prerequisite",
     "workspaces": [{
       "kind": "git_worktree", "role": "primary", "access": "read_write",
       "source_path": "/home/thor/fun/nightdesk",
       "worktree_name": "feat/a-prerequisite"
     }],
     "commit_on_finish": true
   }
   ```

   PATCH it onto an existing A the same way: `{"commit_on_finish": true}`.

2. **Manual commit.** After A's run finishes (and before B provisions), commit A's worktree yourself: `git -C <A-worktree> add -A && git -C <A-worktree> commit -m "..."`. This is what you must do if A does not opt into `commit_on_finish`.

3. **Safety net — provision-time warning.** If you forget and B provisions from a `base_ref` that hasn't advanced past HEAD, nightdesk records a `provision_warning` transcript event (and logs it) at B's provision time: *"base_ref … has no commits beyond the repository HEAD … enable `commit_on_finish` on the prerequisite (or commit its branch manually)."* It does not block provisioning — watch for it so you can fix A and re-provision B rather than letting B improvise.

### Set `base_ref` at creation

Put A's branch name in B's primary workspace entry alongside the usual `kind`/`source_path`/`worktree_name`/`role`/`label`/`access`:

```bash
# B builds on A's branch. Requires that A's work is actually COMMITTED on
# $PREREQ_BRANCH — set `commit_on_finish: true` on ticket A (or commit A's
# worktree manually) before B provisions, or B gets an empty prerequisite.
# $PREREQ_BRANCH is the worktree_name/branch you gave ticket A; $A_TID is A's id.
cat > /tmp/ticket.json <<'JSON'
{
  "title": "B — builds on A",
  "prompt": "Extend the work from ticket A. Your worktree is already branched off A's branch.",
  "workspaces": [
    {
      "kind": "git_worktree",
      "role": "primary",
      "access": "read_write",
      "label": "",
      "source_path": "/home/thor/fun/nightdesk",
      "worktree_name": "feat/b-on-top-of-a",
      "base_ref": "__PREREQ_BRANCH__"
    }
  ]
}
JSON

# Inject profile_id (and substitute the prereq branch) with Python — never jq,
# which chokes on backticks/non-ASCII in prompts.
PREREQ_BRANCH="feat/a-prerequisite"
python3 -c "
import json, os
with open('/tmp/ticket.json') as f:
    t = json.load(f)
t['profile_id'] = '<uuid>'
t['workspaces'][0]['base_ref'] = os.environ['PREREQ_BRANCH']
with open('/tmp/ticket.full.json', 'w') as f:
    json.dump(t, f)
"
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X POST "$BASE/api/v1/tickets" --data @/tmp/ticket.full.json | jq '{id, title, status}'

# Then add the dependency edge so B waits for A:
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X POST "$BASE/api/v1/tickets/$B_TID/dependencies" \
  -d '{"depends_on_id": "'"$A_TID"'"}' | jq .
```

### Patch `base_ref` onto an existing draft/pending ticket

`base_ref` can also be set later via PATCH, **as long as B is still `draft`/`pending` (before its worktree provisions)**. PATCH `workspaces` with the full primary entry — a sparse PATCH replaces the workspaces list, so include the existing kind/source_path/worktree_name/role/label/access fields, not just `base_ref`:

```bash
cat > /tmp/patch.json <<'JSON'
{
  "workspaces": [
    {
      "kind": "git_worktree",
      "role": "primary",
      "access": "read_write",
      "label": "",
      "source_path": "/home/thor/fun/nightdesk",
      "worktree_name": "feat/b-on-top-of-a",
      "base_ref": "feat/a-prerequisite"
    }
  ]
}
JSON
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X PATCH "$BASE/api/v1/tickets/$B_TID" --data @/tmp/patch.json | jq '{id, status}'
```

### Caveats

- The `base_ref` branch **must exist, and must carry A's committed work**, in the repo when B's worktree provisions. The dependency edge only guarantees A has *run* — it does **not** guarantee A's branch has advanced. Use `commit_on_finish: true` on A (or commit A's worktree manually) so the branch B cuts from actually contains A's changes. Never point `base_ref` at a branch nothing creates — provisioning will fail.
- **Don't delete a prerequisite branch** until every dependent that bases on it has provisioned/landed — it's both the worktree base *and* the rebase reference point.
- **Chains stack transitively** (A ← B ← C): each link's `base_ref` is the immediately-preceding branch (B's `base_ref` = A's branch, C's `base_ref` = B's branch), and each link gets its own dependency edge.
- **Merge-time reconciliation.** Stacking happens at *work* time, not necessarily *merge* time. If the host project squash-merges, B's branch carries A's individual commits while `main` has them squashed. To land only B's own delta, rebase with:

  ```bash
  git rebase --onto main <A-branch-tip> <B-branch>
  ```

  This drops the redundant prerequisite commits and replays only B's. Land each branch's own MR onto `main` independently, **in dependency order** (A, then B, then C).

## Fetch / list

```bash
curl -s "${AUTH[@]}" "$BASE/api/v1/tickets/$TID" | jq .
curl -s "${AUTH[@]}" "$BASE/api/v1/tickets?status=queued" | jq '.[].title'
```

List filters: `status`, `profile_id`, `limit` (default 200).

## Update (PATCH, sparse)

```bash
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X PATCH "$BASE/api/v1/tickets/$TID" \
  -d '{"title": "new title", "prompt": "new prompt"}' | jq .
```

Only fields in the body are touched. Multi-line prompts: write the JSON to a temp file and pass `--data @file.json` so quoting stays sane.

## Run now

```bash
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/run-now"
```

Sets the internal scheduler-bypass flag. The next scheduler tick (≤5s) picks the ticket and transitions it to `running`. Works from `draft`, `queued`, `review`, `archived`.

## Transition / cancel / requeue / archive

```bash
# Generic transition (also supports `position`)
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X POST "$BASE/api/v1/tickets/$TID/transition" \
  -d '{"status": "queued"}'

curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/cancel"     # running → review
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/requeue"    # review|archived → queued
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/archive"    # review → archived
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/unarchive"  # archived → queued
```

## Delete

```bash
curl -s "${AUTH[@]}" -X DELETE "$BASE/api/v1/tickets/$TID"
```

`409` if the ticket is `running`. Cancel it first.

## Runs

```bash
curl -s "${AUTH[@]}" "$BASE/api/v1/runs?ticket_id=$TID" | jq .
curl -s "${AUTH[@]}" "$BASE/api/v1/runs/$RID"          | jq .
```

A run's `started_as_run_now` flag is run-level history; it survives even after the ticket's transient `run_now` flag clears.

## Conversations and continuing a run

Runs are grouped into **conversations**. A ticket has many conversations (1:N) with exactly one active at a time; each run is one turn within its conversation. The conversation is the continuous thread of work and holds the Claude/OpenCode session id used to resume.

- **Continue** extends the active conversation: same runtime, same workspace, full message history resumed via the SDK session. The typed text becomes the next user turn. JSON route: `POST /api/v1/tickets/{tid}/continue` with body `{"message": "..."}`.
- **New conversation** starts a fresh session (no history). Use it to switch runtime (e.g. Claude Code to OpenCode) or start over. JSON route: `POST /api/v1/tickets/{tid}/new-conversation` with body `{"message"?, "profile_id"?, "workspace"?: "keep"|"fresh"}`.
- Conversations are **runtime-bound**. You cannot continue a conversation across an incompatible runtime; switching runtime requires a new conversation.
- `resume`, `retry`, `restart`, and `clone` all have JSON routes (see below) — the SPA's ticket detail page calls the same routes.

### resume / retry / restart / clone

All four require the ticket to be in `review` or `archived` (`409` otherwise) and, like `continue`/`new-conversation`, stage a queued run-now and return the ticket as `TicketOut`.

```bash
# resume / retry: fresh-context agent on the SAME worktree (a new conversation
# internally). Body: {"message"?: "..."}.
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/resume" \
  -H 'Content-Type: application/json' -d '{"message":"pick up where you left off"}' | jq '{status}'
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/retry" -d '{}' \
  -H 'Content-Type: application/json' | jq '{status}'

# restart: fresh worktree, fresh context. workspace_policy is required:
# "recreate_in_place" (wipe + recreate the same path) or "fresh_path" (new path).
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/restart" \
  -H 'Content-Type: application/json' \
  -d '{"workspace_policy":"fresh_path","message":"start clean"}' | jq '{status}'

# clone: create a new draft ticket copying prompt/profile/workspaces.
# carry_context=true folds the source ticket's staged next_run_context into
# the clone's prompt. Returns 201 with the NEW ticket (different id).
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/clone" \
  -H 'Content-Type: application/json' \
  -d '{"title":"retry with a tweak","carry_context":true}' | jq '{id, title}'
```

### Steering: next-run-context

Stage (or clear) a note that either rides along on the next `continue`/`resume`/etc., or gets folded permanently into the prompt:

```bash
# Stage a steering note (empty body clears it).
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/next-run-context" \
  -H 'Content-Type: application/json' -d '{"body":"also handle the edge case with empty input"}' | jq '.next_run_context'

# Fold the staged note into the prompt permanently and clear it. 422 if nothing is staged.
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/merge-next-run-context" | jq '{prompt, next_run_context}'
```

### Additional directories

```bash
# Add (idempotent by path). mode is "rw" (default) or "ro". path must be absolute (422 otherwise).
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/additional-dirs" \
  -H 'Content-Type: application/json' -d '{"path":"/opt/shared-lib","mode":"ro"}' | jq '.additional_dirs'

# Remove (path as a query param, not a body).
curl -s "${AUTH[@]}" -X DELETE "$BASE/api/v1/tickets/$TID/additional-dirs?path=/opt/shared-lib" | jq '.additional_dirs'
```

```bash
# Continue the active conversation (message = next user turn on the resumed session).
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/continue" \
  -H 'Content-Type: application/json' -d '{"message":"now also fix the tests"}' | jq .

# Start a fresh conversation — e.g. to switch runtime. All fields optional.
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/new-conversation" \
  -H 'Content-Type: application/json' \
  -d '{"message":"retry","profile_id":"<id>","workspace":"fresh"}' | jq .
```

Both endpoints stage a queued run-now the worker picks up and return the affected ticket as `TicketOut` JSON. **`continue` requires a resumable active conversation** (one whose turn recorded a session id): if there is no active conversation, or its session id is null, `continue` returns `409 {"detail": "... start a new conversation ..."}` — call `new-conversation` instead (this is also the only way to switch runtime, since sessions are not portable across runtimes). Other errors: ticket not found → `404`; from a non-`review`/`archived` status → `409`; empty `continue` message → `422`; unknown `profile_id` → `404`.

## Inbox (triage)

`inbox` sits outside the runnable board; items there may be missing a profile/workspace and are captured via `POST /api/v1/tickets` with `"status":"inbox"` (workspace/profile optional there only).

```bash
curl -s "${AUTH[@]}" "$BASE/api/v1/inbox" | jq '.[] | {id: .ticket.id, blockers}'   # blockers: [] means promotable
curl -s "${AUTH[@]}" "$BASE/api/v1/inbox/count" | jq .count

# Promote onto the board. 422 with the blocker list if still incomplete.
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/promote" \
  -H 'Content-Type: application/json' -d '{"target":"queued"}' | jq '{status}'

# Decline (inbox -> archived). Always allowed, no completeness gate.
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/decline"
```

**`inbox` is still not a valid `/transition` target** — see the lifecycle caveats above; promotion is the only supported `inbox -> {draft,queued}` path.

## Bulk actions

```bash
# Replace (not merge) the label set on every listed ticket.
curl -s "${AUTH[@]}" -H 'Content-Type: application/json' \
  -X PATCH "$BASE/api/v1/tickets/bulk/labels" \
  -d '{"ticket_ids":["'"$T1"'","'"$T2"'"],"label_ids":["'"$LABEL_ID"'"]}' | jq '.skipped'

curl -s "${AUTH[@]}" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/v1/tickets/bulk/archive" -d '{"ticket_ids":["'"$T1"'"]}' | jq .
curl -s "${AUTH[@]}" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/v1/tickets/bulk/unarchive" -d '{"ticket_ids":["'"$T1"'"]}' | jq .
```

Each bulk call returns `{"updated": [...TicketOut], "skipped": [{"ticket_id", "reason"}]}` — a partial failure never fails the whole batch.

## Transcript (SSE)

```bash
curl -N "${AUTH[@]}" "$BASE/api/v1/tickets/$TID/transcript"
```

Server-Sent Events stream of the most recent run's canonical transcript. Use `-N` to disable curl buffering. The stream resolves the current run via `ticket.current_run_id` then falls back to the latest run for that ticket.

```bash
curl -N "${AUTH[@]}" "$BASE/api/v1/runs/$RID/transcript"
```

Same SSE protocol, but for one **specific** run by id — use it to view an older run's transcript instead of just the ticket's latest. `404` if the run doesn't exist or its transcript file is missing. A finished run (`finished_at` set) replays its transcript once and immediately sends `event: end`, since nothing further will ever be appended; a run that's still in flight (`finished_at` null) tails exactly like the ticket endpoint, polling until it finishes. Both endpoints share the same event framing (`id: <seq>` per canonical event, `Last-Event-ID` resume support, `?since_seq=` to skip already-rendered events).

## Profiles, config, worker, search, fs

```bash
curl -s "${AUTH[@]}" "$BASE/api/v1/profiles"            | jq '.[].name'
curl -s "${AUTH[@]}" "$BASE/api/v1/config"              | jq .
curl -s "${AUTH[@]}" "$BASE/api/v1/worker/status"       | jq .
curl -s "${AUTH[@]}" "$BASE/api/v1/search?q=foo"        | jq .
curl -s "${AUTH[@]}" "$BASE/api/v1/fs/suggest?path=~/f" | jq .
```

`PATCH /api/v1/config` accepts `window_start`, `window_end` (`HH:MM`), and `max_parallel`. Data-dir paths are bootstrap-only and cannot be changed at runtime.

## Common mistakes

- **Stacking without `commit_on_finish` on the prerequisite.** A dependency edge + `base_ref` is not enough: runs leave work *uncommitted* by default, so the prerequisite's branch never advances and the dependent provisions an empty tree (then improvises a duplicate implementation). Set `commit_on_finish: true` on the prerequisite, or commit its worktree manually, before the dependent provisions. If you forget, watch for the `provision_warning` transcript event at the dependent's provision time. See "Dependent / stacked tickets".
- Sending a sparse PATCH and expecting omitted fields to clear. They don't — see `nightdesk-api`.
- Trying to transition `draft → review`. Not allowed; go through `running` or use `archive`/`requeue` paths.
- Passing `cwd` on create. That field was removed — the server ignores it and you get `422 "workspaces must include exactly one primary workspace"`. Use top-level `source_path` (or a `workspaces` list with one primary).
- Forgetting `-N` on the SSE curl — output looks frozen.
- Passing both `--data @file.json` AND a second `-d` (or `-d @-` from a heredoc) on the same `curl`. Multiple `-d` flags **concatenate with `&`**, which produces malformed JSON and silently posts garbage; the server returns a 4xx and `jq '{id,title,status}'` shows `{id: null, title: null, status: null}`. Use a single `--data @file.json` and inject any extra fields with Python BEFORE the POST.
- Using an unquoted heredoc tag (`<<JSON` instead of `<<'JSON'`) when the prompt contains backticks or `$`. The shell will expand them and either run commands or empty out variables silently. Always quote the tag.
- Using `jq --arg` or `jq '. + {...}'` to inject fields into a prompt-containing JSON file. Prompts with backticks, em-dashes, or non-ASCII characters cause jq to emit `Invalid numeric literal` and produce no output. Use Python to merge fields into the JSON file instead — it handles any string content reliably.
