---
name: nightdesk-api
description: Use when calling the nightdesk HTTP API from scripts, curl, tests, or ad-hoc tooling. Covers bearer-token auth, base URL discovery, the two parallel API surfaces (JSON `/api/v1/*` vs HTMX `/board/*` `/tickets/*` `/archive/*`), `openapi.json` discovery, and PATCH semantics.
---

# nightdesk API access

The nightdesk server exposes two parallel surfaces on the same host. Pick the right one or you will get 405s, HTML fragments, or auth errors.

| Surface | Auth | Content type | Use for |
|---|---|---|---|
| `/api/v1/*` | `Authorization: Bearer <token>` | JSON in, JSON out | Scripts, tests, tooling, anything that isn't a browser |
| `/board/*`, `/tickets/{tid}/*`, `/archive/*`, `/header/*`, `/fs/*` | Browser session cookie | HTML fragments, `204` + `HX-Redirect: /` | HTMX UI only — do not call from scripts |

If you find yourself reaching for an HTMX endpoint from curl, stop and look for the `/api/v1` equivalent. The HTMX routes are not stable contracts.

## Auth

Bearer token, host, and port live in `~/.config/nightdesk/config.toml`:

```toml
bearer_token = "..."
bind_host = "127.0.0.1"
bind_port = 8765
```

Read it fresh every time (a stale value will return `{"detail":"missing bearer"}` or `{"detail":"bad bearer"}`):

```bash
TOKEN=$(awk -F\" '/^bearer_token/ {print $2}' ~/.config/nightdesk/config.toml)
HOST=$(awk -F\" '/^bind_host/    {print $2}' ~/.config/nightdesk/config.toml)
PORT=$(awk -F'= *' '/^bind_port/ {print $2}' ~/.config/nightdesk/config.toml)
BASE="http://${HOST:-127.0.0.1}:${PORT:-8765}"
AUTH=(-H "Authorization: Bearer $TOKEN")

curl -s "${AUTH[@]}" "$BASE/api/v1/tickets" | jq .
```

## Endpoint discovery

`GET /openapi.json` is unauthenticated and is the source of truth for paths, methods, request bodies, and responses. Use it before guessing:

```bash
# All paths
curl -s "$BASE/openapi.json" | jq -r '.paths | keys[]'

# Methods on a specific path
curl -s "$BASE/openapi.json" | jq '.paths["/api/v1/tickets/{tid}"] | keys'

# Request/response schema for an operation
curl -s "$BASE/openapi.json" | jq '.paths["/api/v1/tickets"].post'
```

For Python-side schema details (field types, defaults, validators), read `src/nightdesk/api/schemas.py` directly — it is shorter than the generated OpenAPI and easier to scan.

## Response conventions

- `/api/v1/*` mutations return the affected resource as JSON (or `204` for cancel/transition).
- **Update endpoints are PATCH with sparse semantics**: only fields included in the body are touched; omitted fields are preserved. Sending `{"title": "x"}` will NOT wipe the prompt.
- Errors: FastAPI `{"detail": "..."}` with appropriate status code (`401` missing bearer, `404` not found, `409` invalid transition, `422` validation).
- HTMX routes return `204 No Content` with an `HX-Redirect: /` header — useless to a script.

## Listing / paging tickets

`GET /api/v1/tickets` returns one page of a board-stable ordered list. It does **not** silently clamp:

| Param | Default | Notes |
|---|---|---|
| `status` | all | `inbox`/`draft`/`queued`/`running`/`review`/`archived` |
| `profile_id` | all | |
| `project_id` | all | `null` selects tickets with no project |
| `limit` | `200` | honored up to a hard max of `1000`; **above the max is a `422`, not a clamp** |
| `offset` | `0` | page past the limit / the hard max |
| `sort` | `board` | `board` = position-stable board order (unchanged default); `recent` = `updated_at` desc (newest first). Any other value is a `422`. The Archive page uses `sort=recent` so page 1 is the most recently archived. |

Because the body is a bare JSON array, paging metadata is in response **headers** — always check these, never assume a full result set:

- `X-Total-Count` — total tickets matching the filters (ignores `limit`/`offset`).
- `X-Has-More` — `true` when more rows exist beyond this page; `false` when the page is the whole result set.
- `X-Limit`, `X-Offset` — echo of the effective paging params.

Fetch every ticket (e.g. to retro-tag `project_id`) by looping until `X-Has-More` is `false`:

```bash
all=(); offset=0
while :; do
  r=$(curl -s -D - "${AUTH[@]}" "$BASE/api/v1/tickets?limit=1000&offset=$offset")
  body=$(printf '%s' "$r" | tail -n +$(printf '%s' "$r" | grep -n $'\r$' | tail -1 | cut -d: -f1) | tail -n +1)
  # ...append body rows to `all`...
  echo "$r" | tr -d '\r' | grep -i 'X-Has-More: false' >/dev/null && break
  offset=$((offset + 1000))
done
```

In Python, just read `resp.headers["x-total-count"]` / `resp.headers["x-has-more"]`. The historical bug was that `?limit=500` was silently ignored and 200 rows came back with no signal of truncation — the headers exist precisely so that cannot recur.

## Common gotchas

- `GET /board/tickets/{id}` → `405 Method Not Allowed`. The board surface has no per-ticket GET; use `GET /api/v1/tickets/{tid}`.
- `POST /api/v1/...` without `Authorization` → `{"detail":"missing bearer"}`. Always set the header.
- A `PATCH` body that omits a field does not clear it. To clear, send an explicit `null` (where the `*Update` schema allows `Optional[...]`).
- Tickets in `status="running"` cannot be deleted — `409` from `DELETE /api/v1/tickets/{tid}`. Cancel or wait first.
- `bind_host = "127.0.0.1"` by default — `localhost` works, but a stale `0.0.0.0` assumption from elsewhere will hang.
- Continuing/resuming a conversation has JSON endpoints now (see **Conversations** below); only the legacy `/resume`, `/retry`, `/restart` verbs remain HTMX-only.

## Conversations

Runs are grouped into conversations (a ticket has many, one active; each run is a turn). The conversation is the resumable thread and holds the runtime session id. Both verbs are first-class JSON endpoints — scripts and agents can resume a session or start a fresh one without a browser:

```bash
# Continue the ACTIVE conversation: `message` becomes the next user turn on the
# resumed SDK session (same runtime, full history). Stages a run-now.
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/continue" \
  -H 'Content-Type: application/json' \
  -d '{"message":"now also add tests"}' | jq '{status, run_now}'

# Start a FRESH conversation (new session, no history). Switches runtime when
# `profile_id` is given (sessions are not portable across runtimes, so an empty
# runtime switch still requires new-conversation). `workspace` is keep|fresh.
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/new-conversation" \
  -H 'Content-Type: application/json' \
  -d '{"message":"retry from scratch","profile_id":"<id>","workspace":"fresh"}' | jq .
```

- Both `POST` the affected ticket back as `TicketOut` JSON and stage a queued run-now the worker picks up.
- `continue` request body: `{"message": "..."}` (non-empty; empty → `422`).
- `new-conversation` request body: `{"message"?: "...", "profile_id"?: "...", "workspace"?: "keep"|"fresh"}` — all optional.
- `continue` is only valid when the active conversation has a resumable session id. If there is no active conversation, or its first turn never recorded a session id (not resumable), `continue` returns **`409 {"detail": "... start a new conversation ..."}`** — the detail names the problem and points at `new-conversation`. Use `new-conversation` for that case (and to switch runtime).
- Status errors mirror other ticket mutations: ticket not found → `404`; continue/new-conversation from a non-`review`/`archived` status → `409`; unknown `profile_id` → `404`.

See `src/nightdesk/domain/conversations.py` for the model and `nightdesk-ticket-ops` for recipes.

## Installing these skills

These nightdesk skills are plain markdown, so any agent that loads
`<dir>/skills/<name>/SKILL.md` folders can use them. `nightdesk-install-skills`
detects installed coding agents and installs into each one's default skills
directory (Claude Code, opencode, and pi all share the same `SKILL.md` layout):

```bash
nightdesk-install-skills --list-harnesses   # show supported + detected agents
nightdesk-install-skills                    # Claude Code only -> straight install
nightdesk-install-skills --all              # every detected agent (non-interactive; alias --yes)
nightdesk-install-skills --harness opencode # one specific agent (non-interactive)
nightdesk-install-skills --target ./myproj  # project-local ./myproj/.claude/skills
nightdesk-install-skills --force            # reinstall even if up to date
```

Each target keeps its own drift marker (`.nightdesk-skills-version`), so updates
are tracked per agent.

## Sister skill

For concrete ticket recipes (create, transition, run-now, archive, transcript stream), use `nightdesk-ticket-ops`. That skill also covers:

- **Dependency edges** — `GET`/`POST`/`DELETE /api/v1/tickets/{tid}/dependencies` (gate execution order so a dependent ticket waits for its prerequisite).
- **Stacked / dependent tickets** — set a workspace's `base_ref` (a field on `TicketWorkspaceIn`, see `src/nightdesk/api/schemas.py`) to a prerequisite's branch so the dependent's git_worktree is cut from that branch instead of HEAD, building directly on the prerequisite's commits.
