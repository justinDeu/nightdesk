# Documentation Screenshots

The screenshots in `docs/screenshots/` come from the demo database seeded by `nightdesk-seed-demo`.

The fixture includes tickets in every lifecycle state, project defaults, linked workspaces, dependency chains, synthetic transcripts, online worker heartbeat data, 30 days of run outcomes, token counts, model mix, and costs.

## Refresh Flow

Use an isolated `HOME` and a non-default port when another Nightdesk instance is already running.

```bash
mkdir -p .tmp/demo-home/.config/nightdesk .tmp/demo-home/.local/share/nightdesk-demo
```

Create `.tmp/demo-home/.config/nightdesk/config.toml`:

```toml
bearer_token = "nightdesk-demo-token"
bind_host = "127.0.0.1"
bind_port = 8876
db_path = "/path/to/nightdesk/.tmp/demo-home/.local/share/nightdesk-demo/nightdesk.db"
transcript_root = "/path/to/nightdesk/.tmp/demo-home/.local/share/nightdesk-demo/transcripts"
worktree_root = "/path/to/nightdesk/.tmp/demo-home/.local/share/nightdesk-demo/worktrees"
```

Replace `/path/to/nightdesk` with the absolute path to the repository.

Seed and run the demo server:

```bash
uv run nightdesk-seed-demo --reset \
  --db-path "$(pwd)/.tmp/demo-home/.local/share/nightdesk-demo/nightdesk.db" \
  --transcript-root "$(pwd)/.tmp/demo-home/.local/share/nightdesk-demo/transcripts" \
  --source-path "/demo/nightdesk"

HOME="$(pwd)/.tmp/demo-home" uv run nightdesk-api
```

Capture these pages with Playwright at `1600x1000`. Before each screenshot, inject temporary CSS that hides scrollbars so every image has the same clean frame:

```css
*, *::before, *::after { scrollbar-width: none !important; }
*::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }
html, body { overflow: hidden !important; }
```

| File | Page |
| --- | --- |
| `desk.png` | `/` (the Desk overview) |
| `board-kanban.png` | `/tickets?view=board`, then click `Implement CSV export for reports` so the side-peek is open |
| `ticket-transcript.png` | `/tickets/<id>` — fetch the id of `Implement CSV export for reports` from `/api/v1/tickets` first; seeded ids change on every `--reset` |
| `archive.png` | `/archive` |
| `analytics.png` | `/analytics` |
| `profiles.png` | `/settings/profiles` |
| `settings.png` | `/settings/scheduling` |
| `mobile-board.png` | `/tickets?view=board` at a 390x844 viewport (phone frame for the README's mobile section) |

Authenticate through the `/login` form with `nightdesk-demo-token` before capture.

Two gotchas:

- The seeded worker heartbeat goes stale a few minutes after seeding, flipping the
  header pill to a red "Worker stale". Either capture immediately after seeding or
  push the heartbeat forward first:

  ```bash
  sqlite3 <demo-db> "UPDATE worker_heartbeat SET last_seen_at = datetime('now','+30 minutes')"
  ```

- `nightdesk-seed-demo --reset` deletes and recreates the DB file. A server that
  was already running keeps its connection to the old, deleted file — restart the
  demo API after every reseed or you'll capture stale data (and fetch stale
  ticket ids).
