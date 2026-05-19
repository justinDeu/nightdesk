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

## Common gotchas

- `GET /board/tickets/{id}` → `405 Method Not Allowed`. The board surface has no per-ticket GET; use `GET /api/v1/tickets/{tid}`.
- `POST /api/v1/...` without `Authorization` → `{"detail":"missing bearer"}`. Always set the header.
- A `PATCH` body that omits a field does not clear it. To clear, send an explicit `null` (where the `*Update` schema allows `Optional[...]`).
- Tickets in `status="running"` cannot be deleted — `409` from `DELETE /api/v1/tickets/{tid}`. Cancel or wait first.
- `bind_host = "127.0.0.1"` by default — `localhost` works, but a stale `0.0.0.0` assumption from elsewhere will hang.

## Sister skill

For concrete ticket recipes (create, transition, run-now, comments, archive, transcript stream), use `nightdesk-ticket-ops`.
