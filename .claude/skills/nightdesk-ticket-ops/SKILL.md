---
name: nightdesk-ticket-ops
description: Use when performing ticket lifecycle operations against the nightdesk API — create, fetch, update, transition (draft/queued/running/review/archived), run-now, cancel, requeue, archive/unarchive, comments, runs, transcript streaming, search. Assumes auth and base URL are already set up per `nightdesk-api`.
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
| `draft` | `queued`, `running`, `inbox` |
| `queued` | `draft`, `running` |
| `running` | `review` |
| `review` | `queued`, `archived` |
| `archived` | `queued` |

Anything else returns `409 invalid transition`. Dropping into `running` from `draft` or `queued` sets `run_now=true` so the scheduler picks it on the next tick.

**API surface caveats — the state machine and the JSON API don't fully line up:**
- `POST /api/v1/tickets/{tid}/transition` only accepts targets `draft|queued|running|review|archived`. **`inbox` is NOT a valid `/transition` target** even though `draft → inbox` is state-machine-legal. Tickets move *into* `inbox` only via the HTMX inbox routes (`/inbox/...`); the JSON API has no `→ inbox` path.
- `archived` is reachable only from `review` (`/archive` or `transition status=archived`) or from `inbox` (decline). **A `draft` or `queued` ticket cannot be archived via the API or board** — only deleted, or hand-walked `draft → inbox → archived` through the domain layer. (Known bug.)

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

When using `git_worktree`, pair with `worktree_name` (e.g. `fix/my-feature`) — the server generates one if omitted, but an explicit name is clearer.

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
| `base_ref` | string | base ref for diffing |
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

## Comments

```bash
curl -s "${AUTH[@]}" "$BASE/api/v1/tickets/$TID/comments" | jq .
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X POST "$BASE/api/v1/tickets/$TID/comments" \
  -d '{"body": "note text"}' | jq .
```

## Runs

```bash
curl -s "${AUTH[@]}" "$BASE/api/v1/runs?ticket_id=$TID" | jq .
curl -s "${AUTH[@]}" "$BASE/api/v1/runs/$RID"          | jq .
```

A run's `started_as_run_now` flag is run-level history; it survives even after the ticket's transient `run_now` flag clears.

## Conversations and continuing a run

Runs are grouped into **conversations**. A ticket has many conversations (1:N) with exactly one active at a time; each run is one turn within its conversation. The conversation is the continuous thread of work and holds the Claude/OpenCode session id used to resume.

- **Continue** extends the active conversation: same runtime, same workspace, full message history resumed via the SDK session. The typed text becomes the next user turn. Route: browser-only HTMX form `POST /tickets/{tid}/continue` with form field `next_run_context`.
- **New conversation** starts a fresh session (no history). Use it to switch runtime (e.g. Claude Code to OpenCode) or start over. Route: browser-only HTMX `POST /tickets/{tid}/new-conversation`, choosing runtime and workspace.
- Conversations are **runtime-bound**. You cannot continue a conversation across an incompatible runtime; switching runtime requires a new conversation.
- Legacy re-run verbs still exist as HTMX routes: `/tickets/{tid}/resume`, `/retry`, `/restart`. `resume`/`retry` start fresh-context on the same worktree; `restart` uses a fresh worktree.

**There is no JSON `/api/v1/*` continue or new-conversation endpoint.** Scripts cannot continue a conversation through the JSON API; it is browser/HTMX only. The JSON `Run` and `Ticket` schemas do not expose conversation fields. To drive a follow-up programmatically you stage it the same way the UI does (set `next_run_context` and transition to `queued`), but that starts a fresh-context run, not a session resume.

## Transcript (SSE)

```bash
curl -N "${AUTH[@]}" "$BASE/api/v1/tickets/$TID/transcript"
```

Server-Sent Events stream of the most recent run's canonical transcript. Use `-N` to disable curl buffering. The stream resolves the current run via `ticket.current_run_id` then falls back to the latest run for that ticket.

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

- Calling `POST /board/tickets/{tid}` from a script. That's the HTMX update endpoint; it returns `204 + HX-Redirect`. Use `PATCH /api/v1/tickets/{tid}` instead.
- Sending a sparse PATCH and expecting omitted fields to clear. They don't — see `nightdesk-api`.
- Trying to transition `draft → review`. Not allowed; go through `running` or use `archive`/`requeue` paths.
- Passing `cwd` on create. That field was removed — the server ignores it and you get `422 "workspaces must include exactly one primary workspace"`. Use top-level `source_path` (or a `workspaces` list with one primary).
- Trying to archive a `draft`/`queued` ticket. `/archive` and `transition status=archived` only work from `review` (and `inbox` declines). There is no API path to archive a never-run draft — see the lifecycle caveats above.
- Forgetting `-N` on the SSE curl — output looks frozen.
- Passing both `--data @file.json` AND a second `-d` (or `-d @-` from a heredoc) on the same `curl`. Multiple `-d` flags **concatenate with `&`**, which produces malformed JSON and silently posts garbage; the server returns a 4xx and `jq '{id,title,status}'` shows `{id: null, title: null, status: null}`. Use a single `--data @file.json` and inject any extra fields with Python BEFORE the POST.
- Using an unquoted heredoc tag (`<<JSON` instead of `<<'JSON'`) when the prompt contains backticks or `$`. The shell will expand them and either run commands or empty out variables silently. Always quote the tag.
- Using `jq --arg` or `jq '. + {...}'` to inject fields into a prompt-containing JSON file. Prompts with backticks, em-dashes, or non-ASCII characters cause jq to emit `Invalid numeric literal` and produce no output. Use Python to merge fields into the JSON file instead — it handles any string content reliably.
