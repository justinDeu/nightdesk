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

Valid transitions (`src/nightdesk/domain/tickets.py:13`):

| from → to | allowed |
|---|---|
| `draft` | `queued`, `running` |
| `queued` | `draft`, `running` |
| `running` | `review` |
| `review` | `queued`, `archived` |
| `archived` | `queued` |

Anything else returns `409 invalid transition`. Dropping into `running` from `draft` or `queued` sets `run_now=true` so the scheduler picks it on the next tick.

## Create a ticket

Required: `title`, `profile_id`, `cwd`. `cwd` is validated as a non-empty string and normalized to an absolute path. New tickets default to `status="draft"`. If only one profile exists, fetch it once with `curl "${AUTH[@]}" "$BASE/api/v1/profiles" | jq -r '.[0].id'`.

`workspace_mode` controls how the agent's working directory is set up:

| mode | behavior |
|---|---|
| `directory` | agent runs in `cwd` as a plain directory (default) |
| `in_place` | agent runs directly in `cwd` (legacy alias for directory) |
| `git_worktree` | agent gets an isolated git worktree branched from `cwd` |
| `worktree` | reserved; not yet fully implemented |

For git worktree isolation use `"workspace_mode": "git_worktree"`. Pair with `worktree_name` (optional branch name) or let the server generate one.

**Single-line / inline prompt** (fine for short prompts):

```bash
curl -s "${AUTH[@]}" -H "Content-Type: application/json" \
  -X POST "$BASE/api/v1/tickets" \
  -d '{
    "title": "...",
    "prompt": "...",
    "profile_id": "<uuid>",
    "cwd": "/home/thor/fun/nightdesk",
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

# 2. Inject profile_id, cwd, and any other fields with Python.
#    Do NOT use `jq --arg` or `jq '. + {...}'` for this merge — jq chokes on
#    prompts containing backticks, em-dashes, or other non-ASCII characters
#    and returns "Invalid numeric literal" without a useful error. Python is
#    reliable for any prompt content.
python3 -c "
import json
with open('/tmp/ticket.json') as f:
    t = json.load(f)
t['profile_id'] = '<uuid>'
t['cwd'] = '/home/thor/fun/nightdesk'
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

Tickets support an optional `workspaces` list for exposing additional directories or worktrees to the agent alongside the primary `cwd`. Each entry is a `TicketWorkspaceIn` object:

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
# Create ticket with an extra linked read-only directory
cat > /tmp/ticket.json <<'JSON'
{
  "title": "...",
  "prompt": "...",
  "workspaces": [
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
jq '. + {profile_id: "<uuid>", cwd: "/home/thor/fun/nightdesk"}' \
  /tmp/ticket.json > /tmp/ticket.full.json
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
- Forgetting `-N` on the SSE curl — output looks frozen.
- Passing both `--data @file.json` AND a second `-d` (or `-d @-` from a heredoc) on the same `curl`. Multiple `-d` flags **concatenate with `&`**, which produces malformed JSON and silently posts garbage; the server returns a 4xx and `jq '{id,title,status}'` shows `{id: null, title: null, status: null}`. Use a single `--data @file.json` and inject any extra fields with Python BEFORE the POST.
- Using an unquoted heredoc tag (`<<JSON` instead of `<<'JSON'`) when the prompt contains backticks or `$`. The shell will expand them and either run commands or empty out variables silently. Always quote the tag.
- Using `jq --arg` or `jq '. + {...}'` to inject fields into a prompt-containing JSON file. Prompts with backticks, em-dashes, or non-ASCII characters cause jq to emit `Invalid numeric literal` and produce no output. Use Python to merge fields into the JSON file instead — it handles any string content reliably.
