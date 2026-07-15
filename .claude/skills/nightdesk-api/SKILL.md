---
name: nightdesk-api
description: Use when calling the nightdesk HTTP API from scripts, curl, tests, or ad-hoc tooling. Covers bearer-token auth, base URL discovery, the JSON `/api/v1/*` surface (the only API — the legacy HTMX UI was removed in favor of a React SPA served at `/`), `openapi.json` discovery, and PATCH semantics.
---

# nightdesk API access

> **nightdesk skill** · package v0.0.1 · updated 2026-07-12. A user-global copy
> (installed by `nightdesk-install-skills`) can drift from the code; if anything
> below disagrees with `GET /openapi.json`, re-run `nightdesk-install-skills --force`
> (or `--all --force`) to refresh.

`/api/v1/*` is the entire HTTP API — JSON in, JSON out. There is no separate HTMX/HTML API surface anymore: the old server-rendered UI (`/board/*`, `/tickets/{tid}/*` HTML page, `/archive/*`, `/header/*`, `/settings/*`, `/profiles/*` HTML editor, `/diagnostics` page, `/cron/*` page, `/fs/suggest` HTML partial) was removed and replaced by a React SPA that talks exclusively to `/api/v1`. The SPA itself is served at `/` (see "SPA / static serving" below) — everything under `/` that isn't `/api/v1/*`, `/auth/*`, or `/healthz` is the SPA's own client-side routing, not a server route.

`/api/v1/*` routes accept `Authorization: Bearer <token>` **or** the signed `nightdesk_session` browser cookie — the cookie support exists so the browser SPA can call the JSON API directly without also sending a bearer header; scripts and agents use the bearer header with a scoped token.

## Auth

Authenticate with a **scoped access token** (`ndk_...`), minted by the human in Settings → Access tokens (or `POST /api/v1/tokens` with the admin bearer). Resolution order — env var wins, then the conventional token file:

```bash
TOKEN="${NIGHTDESK_TOKEN:-$(cat ~/.config/nightdesk/agent-token 2>/dev/null)}"
BASE="${NIGHTDESK_BASE_URL:-http://127.0.0.1:8765}"
AUTH=(-H "Authorization: Bearer $TOKEN")

curl -s "${AUTH[@]}" "$BASE/api/v1/tickets" | jq .
```

If `$TOKEN` is empty, stop and ask the human to mint one — do **not** go looking for other credentials. In particular, never read `~/.config/nightdesk/config.toml`: the `bearer_token` there is the human's root credential (it also signs sessions and encrypts stored secrets). Agent actions performed with it are attributed to the human — archives self-acknowledge and skip the human's Desk review queue — and it grants far more than any agent needs.

### Scopes, 401 vs 403

Tokens carry a scope snapshot (e.g. the `operator` bundle: ticket read/create/update/transition/archive/run, runs read, comments, projects read, labels, analytics, cron read). Two failure shapes:

- `401 {"detail":"invalid token"}` — missing/revoked/expired token. Ask the human to re-mint; retrying is pointless.
- `403 {"detail":"missing scope","missing_scopes":[...],"token":"<name>","hint":"..."}` — the token lacks that scope. If the hint says **human-only** (acknowledge, delete, profile/provider/config/cron writes, token management), the action is deliberately reserved for the human's admin session: hand it off, never work around it.

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

- `/api/v1/*` mutations return the affected resource as JSON (or `204` for delete/cancel-style actions).
- **Update endpoints are PATCH with sparse semantics**: only fields included in the body are touched; omitted fields are preserved. Sending `{"title": "x"}` will NOT wipe the prompt.
- Errors: FastAPI `{"detail": "..."}` with appropriate status code (`401` invalid/missing token, `403` missing scope (structured body, see Auth), `404` not found, `409` invalid transition, `422` validation).

## SPA / static serving

The built frontend (`frontend/dist`, a Vite/React app) is mounted at `/` when present: `GET /` and any unmatched path serve `index.html` (client-side routing owns everything that isn't `/api/v1`, `/auth`, or `/healthz`), `GET /assets/{path}` serves hashed build assets with an immutable cache header. `/app/*` 308-redirects to the equivalent `/*` path (a transition shim from when the SPA was mounted at `/app`; safe to use but prefer `/` in new links). None of this is relevant to a script driving the JSON API — it only matters if you're checking whether the UI itself is being served.

## Listing / paging tickets

`GET /api/v1/tickets` returns one page of a board-stable ordered list. It does **not** silently clamp:

| Param | Default | Notes |
|---|---|---|
| `status` | all | `inbox`/`draft`/`queued`/`running`/`review`/`archived` |
| `profile_id` | all | |
| `project_id` | all | `null` selects tickets with no project |
| `priority` | all | exact band on the `0`-`4` scale; out-of-range is a `422` |
| `label` | all | label **name** (case-insensitive) or label id; matches tickets carrying that label |
| `outcome` | all | latest-run terminal state: `succeeded` (last run `exit_status == success`) or `failed` (any other finished status). Any other value is a `422`. |
| `q` | all | free-text substring (case-insensitive) over ticket `title` + `prompt` |
| `acknowledged` | all | `true` = only tickets with `acknowledged_at` set (post-review ack); `false` = only unacknowledged |
| `limit` | `200` | honored up to a hard max of `1000`; **above the max is a `422`, not a clamp** |
| `offset` | `0` | page past the limit / the hard max |
| `sort` | `board` | `board` = position-stable board order (unchanged default); `recent` = `updated_at`, `created` = `created_at`, `priority` = the 0-4 band, `cost` = latest run `cost_usd` (runless tickets sort as NULL). Any other value is a `422`. The Archive page uses `sort=recent` so page 1 is the most recently archived. |
| `order` | `desc` | direction for every `sort` except `board` (which ignores it); `asc` or `desc`. |

All filter params are optional and **AND-combined**, applied server-side, so `X-Total-Count` and paging stay honest — a filtered list never undercounts by filtering only already-loaded rows.

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

- `POST /api/v1/...` without `Authorization` → `401 {"detail":"invalid token"}`. Always set the header.
- A `PATCH` body that omits a field does not clear it. To clear, send an explicit `null` (where the `*Update` schema allows `Optional[...]`).
- Tickets in `status="running"` cannot be deleted — `409` from `DELETE /api/v1/tickets/{tid}`. Cancel or wait first.
- `bind_host = "127.0.0.1"` by default — `localhost` works, but a stale `0.0.0.0` assumption from elsewhere will hang.
- Continuing/resuming a conversation has JSON endpoints now (see **Conversations** below); `resume`, `retry`, `restart`, and `clone` also have JSON parity — see `nightdesk-ticket-ops`.

## Endpoint families added for the SPA rebuild

All under `/api/v1`, same cookie-or-bearer auth as everything else in this table:

- **Inbox**: `GET /inbox` (items + completeness `blockers`), `GET /inbox/count`, `POST /tickets/{tid}/promote` (`{"target": "draft"|"queued"}`, 422 with blockers when incomplete), `POST /tickets/{tid}/decline`.
- **Saved views**: `POST /views` (`{"name", "surface", "params"}`), `PATCH /views/{id}` (rename only), `DELETE /views/{id}`, `POST /views/reorder` (`{"view_ids": [...]}`) — alongside the existing `GET /views`. `surface` is `"tickets"` for the live SPA surface; its `params` are `{"f": "<filter string>", "view": "board"|"list"}` (both optional, empty values stripped). The legacy `"board"`/`"list"` surfaces (params `q`/`group`/`order`/`props`) stay valid for older stored views. Unknown surface or unknown param key → 422; duplicate name → 409.
- **Analytics**: `GET /analytics/summary`, `/analytics/spend?range=today|7d|30d`, `/analytics/tokens?range=`, `/analytics/latency?range=`, `/analytics/prices?range=` — same numbers the `/analytics` HTML dashboard renders, sliced by range. All five accept an optional `?project_id=` that scopes the whole response to one project (404 if unknown). `summary` and `spend` include a `by_project` rollup (`{project_id, project_name, project_slug, total_tokens, cost, run_count, success_rate, ...}` per project); `spend`'s and `tokens`' `daily_series` entries carry the full token breakdown (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `total_tokens`, `cost`, `run_count`, `by_model`) so one series covers spend-over-time, tokens-over-time, and runs-over-time. `prices` returns the current effective per-model price set behind the cost numbers: `{source, as_of, prices: [{model, vendor, input, output, cache_write, cache_read, source, as_of, repriced_since}]}` (USD per 1M tokens; `source` per row is `live`/`cache`/`bundled`/`none`; vendor-aware so non-Anthropic models like GLM resolve too).
- **Bulk**: `PATCH /tickets/bulk/labels` (replaces, not merges, each ticket's label set), `POST /tickets/bulk/archive`, `POST /tickets/bulk/unarchive`.
- **Profiles**: `POST /profiles/{pid}/copy`, `GET /profiles/{pid}/export` (secrets redacted), `POST /profiles/import` (JSON body `{"payload": {...}}`, not multipart), `POST /profiles/import-from-cc` (`{"settings": {...}, "name"?}`).
- **Helpers**: `POST /preview/worktree-name`, `POST /preview/cron` (`{"schedule", "timezone", "count"}` → next N fire times), `POST /notifications/test` (`{"url"}`), `GET /projects/{id}/activity` (unified project activity feed — see below), `GET /diagnostics` (CC install check, no log tails).
- **Project activity feed** (`GET /projects/{id}/activity`): one merged, reverse-chronological, cursor-paginated stream (History tab) — envelope `{"items": [...], "rollups": [...], "next_cursor": "...", "has_more": bool}`, NOT a bare list (the old runs-only array shape was replaced). `items[].kind` is `run` | `lifecycle` | `repo` | `cron`. **Filters + search are server-side** (never client-side over a loaded window): `?kind=all|runs|failures|shipped|repo|lifecycle`, `?q=<title text>`, `?cursor=<next_cursor>`, `?limit=` (1–200, default 50), `?include_rollups=true` (weekly numbers-only aggregates, returned only on the first page when no cursor). 404 if the project is unknown.
- **Labels & projects** (resolve filter/create values): `GET /labels`, `POST /labels`, `GET/PATCH/DELETE /labels/{label_id}`, `PUT /labels/tickets/{ticket_id}` (replace a ticket's label set); `GET /projects`, `POST /projects`, `GET/PATCH/DELETE /projects/{project_id}`. The `label` list filter and bulk/labels take a label id (or name); `project_id` comes from here.
- **API tokens** (durable `ndk_` agent tokens — script-relevant): `GET /tokens` (metadata + `prefix_hint` only, never the secret), `POST /tokens` to mint (body `{"name", "bundle"?: "observer"|"reviewer"|"pm-agent"|"operator", "scopes"?: [...], "profile_allowlist"?, "project_allowlist"?, "expires_in_days"?}` — explicit `scopes` wins over `bundle`; the response is the ONLY time the cleartext token is returned), `GET /tokens/catalog` (scope + bundle vocabulary), `POST /tokens/{id}/revoke`, `DELETE /tokens/{id}`. Human-only scopes (`profiles.write`, `providers.write`, `cron.write`, `config.write`, `integrations.write`, `agents.message`, `agents.admin`, `tickets.delete`, `tokens.admin`) can never be minted — `422`. Minting gates on the admin session, not a token.
- **Integrations (GitLab v1)**: `GET/POST /connections` (a connection = a forge credential + endpoint URL), `GET/POST /repo-links`, `POST /repo-links/{rid}/import-ticket` (import an issue/MR as a draft ticket), `GET/POST /tickets/{tid}/external-links`. Browse (read-only, live-proxied, TTL-cached): `GET /repo-links/{rid}/issues[?state=|&search=|&page_token=]`, `GET /repo-links/{rid}/issues/{iid}`, and the same for `/merge-requests[/{iid}]`. MR **list** items carry a derived `awaiting_your_review` boolean (the connection user is a requested reviewer on an open MR — resolved via `GET /user` against the connection's token, cached per connection; no persistence). `integrations.write` (connection/repo-link CRUD) is human-only.
- **Ack (post-review)**: `POST /tickets/{tid}/ack`, `POST /tickets/ack` (bulk), `GET /tickets/ack/count`, `GET /tickets/ack/digest`. Records that a human reviewed a run's outcome; pairs with the `acknowledged` list filter.
- **Review diff comments**: comments are line-anchored on a **run's diff**, never the ticket — `GET/POST /runs/{rid}/comments`, `PATCH /diff-comments/{cid}`, `POST /diff-comments/{cid}/resolve|unresolve`, `DELETE /diff-comments/{cid}`. **There is no `/tickets/{tid}/comments` endpoint** — ticket-level comments were removed and are not coming back. See `nightdesk-ticket-ops` for the recipe.
- **Run diff surfaces**: `GET /runs/{rid}/diff` (full structured per-file unified diff with hunks — the Changes tab payload) and `GET /runs/{rid}/diffstat` (light per-file `{path, additions, deletions, binary}` + `total_files`/`total_added`/`total_deleted`, **no hunk bodies** — the Overview verdict-row tally). Both resolve the same source (pod-uploaded sidecar for k8s runs, else the selected workspace's computed diff) so the stat always agrees with the full diff.
- **Agents (resident interactive)**: `GET /agents` lists sessions and accepts an optional `?project_id=` that scopes to one project's sessions (sessions carry `project_id`); the rest of the surface (`POST /agents`, `/agents/{aid}/messages|interrupt|wake|end|pending/{request_id}|...`) is admin-only and human-driven.

See `nightdesk-ticket-ops` for the per-ticket conversation/run-action recipes (resume/retry/restart/clone/next-run-context/additional-dirs/steer) and lifecycle recipes (`POST /tickets/{tid}/send-to-inbox` — the only JSON path back into the inbox; `draft` only, `409` otherwise).

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

- **Dependency edges** — `GET`/`POST /api/v1/tickets/{tid}/dependencies` and `DELETE /api/v1/tickets/{tid}/dependencies/{dep_on_id}` (gate execution order so a dependent ticket waits for its prerequisite).
- **Stacked / dependent tickets** — set a workspace's `base_ref` (a field on `TicketWorkspaceIn`, see `src/nightdesk/api/schemas.py`) to a prerequisite's branch so the dependent's git_worktree is cut from that branch instead of HEAD, building directly on the prerequisite's commits.
