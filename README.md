# nightdesk

Local-first work queue for LLM agents. Schedule a Claude Code run against a directory, walk away, review the result later. All state lives on your machine: SQLite for the queue, files on disk for transcripts. The agent runs inside a bubblewrap sandbox so a misbehaving prompt cannot reach beyond the workspace you gave it.

**Linux only for v1.** macOS and Windows are not supported (bubblewrap is Linux-only).

## What it does

- Each ticket has a prompt, a workspace, and a profile.
- A worker picks queued tickets and dispatches them to `claude` in a sandbox.
- The agent runs headless to completion; you review the result on the board.
- Profiles control what the agent can do (allowed tools, network, filesystem, credentials).
- Per-run scoped tokens let the agent call back into nightdesk (spawn child tickets) without exposing your admin bearer token.

## A look at Nightdesk

![Nightdesk board showing lifecycle columns and selected ticket side-peek](docs/screenshots/board-kanban.png)

Track work from draft to review, inspect running jobs, and open a ticket without losing board context.

![Desk overview showing tickets that need you, live runs, and what changed while you were away](docs/screenshots/desk.png)

The Desk answers three questions at a glance: what needs you, what's running, and what changed while you were away.

![Ticket detail page showing prompt, workspace access, dependencies, and transcript events](docs/screenshots/ticket-transcript.png)

Review prompts, filesystem scope, dependency chains, run history, and transcript events in one place.

![Archive filters and completed ticket history](docs/screenshots/archive.png)

Filter completed work by outcome, project, profile, date, or text search.

![Token and usage analytics dashboard](docs/screenshots/analytics.png)

See token mix, cache behavior, model usage, run outcomes, durations, and cost trends.

![Profile editor for sandbox permissions, tools, network, and credentials](docs/screenshots/profiles.png)

Define profiles for sandbox permissions, credentials, allowed tools, models, and environment.

![Scheduling settings for worker windows and concurrency](docs/screenshots/settings.png)

Control work windows, worker cadence, and dispatch limits from the UI.

<img src="docs/screenshots/mobile-board.png" alt="Board on a phone: full-width snap-scrolling columns behind a drawer navigation" width="300" />

The whole app works on a phone: drawer navigation, snap-scrolling board columns, and touch-safe status moves.

## Install

Requires Linux + bubblewrap + Claude Code 2.1.80 or newer.

```bash
# 1. Install bubblewrap if you don't have it.
pacman -S bubblewrap       # Arch
apt install bubblewrap     # Debian / Ubuntu
dnf install bubblewrap     # Fedora / RHEL

# 2. Install Claude Code if you don't have it.
# See https://docs.claude.com/en/docs/claude-code/quickstart

# 3. Install nightdesk as a uv tool.
uv tool install git+https://github.com/<your-fork>/nightdesk

# 4. One-shot setup: generates a bearer, writes config, installs systemd user
# units, starts the services, opens your browser.
nightdesk-setup
```

`nightdesk-setup` does the following automatically:

- Checks `bwrap` and `claude` are on your PATH and validates the CC version.
- Creates `~/.config/nightdesk/config.toml` with a random bearer token (chmod 600).
- Creates `~/.local/share/nightdesk/{transcripts,work,logs}`.
- Runs Alembic migrations on the SQLite DB.
- Installs and enables the `nightdesk-api.service` and `nightdesk-worker.service` user units.
- Opens a pre-authenticated browser session via a one-shot handshake token.

To re-open the UI later without copy-pasting the bearer:

```bash
nightdesk-login
```

> **Note:** the `uv tool install` above packages only the Python backend —
> it does not include the built web UI. Until packaging bundles the
> frontend, use a source checkout instead (see Development below) if you
> want the full board UI rather than the JSON API alone.

## Installing skills into other coding agents

nightdesk ships a few bundled skills (`nightdesk-api`, `nightdesk-ticket-ops`,
`nightdesk-monitor-tickets`) that document its HTTP API. They are plain
markdown, so they work inside any coding agent that loads
`<dir>/skills/<name>/SKILL.md` folders — Claude Code, [opencode](https://opencode.ai/docs/skills/),
and [pi](https://pi.dev/docs/latest/skills) all share that layout.

`nightdesk-install-skills` is harness-aware: it detects which agents are
installed and installs into each one's default skills directory, with a
per-target drift marker so updates are tracked independently.

```bash
nightdesk-install-skills --list-harnesses    # show supported + detected agents
nightdesk-install-skills                     # Claude Code only -> straight install (default)
nightdesk-install-skills --all               # every detected agent, non-interactive (alias: --yes)
nightdesk-install-skills --harness opencode  # one specific agent, non-interactive
nightdesk-install-skills --harness pi
nightdesk-install-skills --target ./myproj   # project-local ./myproj/.claude/skills
nightdesk-install-skills --force             # reinstall even if up to date
```

Detected defaults: Claude Code → `$CLAUDE_CONFIG_DIR/skills` or `~/.claude/skills`;
opencode → `$OPENCODE_CONFIG_DIR/skills` or `$XDG_CONFIG_HOME/opencode/skills`
(default `~/.config/opencode/skills`); pi → `$PI_CODING_AGENT_DIR/skills` or
`~/.pi/agent/skills`. When more than one agent is present and no flag is passed,
you are prompted `y/n` for each.

## Daily use

The board lives at `http://127.0.0.1:8765`. The default port and bind host are
configurable in `~/.config/nightdesk/config.toml`.

- Three default profiles are seeded by `nightdesk-setup`: **Read only**,
  **Edit workspace**, **Full workspace**. Edit them or add new ones on
  `/profiles`.
- Each profile picks one of three Claude Code authentication sources:
  - **Inherit my user credentials** — bind-mounts `~/.claude/.credentials.json`
    read-only into the sandbox. Closest to running `claude` yourself.
  - **`ANTHROPIC_API_KEY`** — provide an API key the profile stores encrypted.
  - **`ANTHROPIC_AUTH_TOKEN`** — for proxied inference (Z.AI etc.).
- Adjust scheduling on `/settings`: active hours window, max parallel runs,
  polling interval, claude binary path, minimum CC version. Changes take
  effect on the worker's next tick.
- Check `/diagnostics` if something is wrong. It shows nightdesk version,
  Python version, the resolved Claude binary path + version, and tails of the
  API and worker logs. Copy the whole page into a bug report.

## Where things live

| Thing | Location |
| --- | --- |
| Config | `~/.config/nightdesk/config.toml` |
| Optional secrets | `~/.config/nightdesk/secrets.env` |
| SQLite DB | `~/.local/share/nightdesk/nightdesk.db` |
| Transcripts | `~/.local/share/nightdesk/transcripts/` |
| Worktrees | `~/.local/share/nightdesk-worktrees/` |
| Daemon logs | `~/.local/share/nightdesk/logs/{api,worker}.log` (rotating, 10 MB × 5) |
| Per-run logs | `~/.local/share/nightdesk/logs/runs/<run-id>.log` |
| Systemd units | `~/.config/systemd/user/nightdesk-{api,worker}.service` |

`journalctl --user -u nightdesk-api` and `... -u nightdesk-worker` also work.

## Configuration

Every path and bind setting resolves through three layers, lowest priority
first: built-in defaults, then `config.toml` keys, then `NIGHTDESK_*`
environment variables, then CLI flags (the flags just export the matching env
var so spawned subprocesses inherit it). Setting `data_dir` derives `db_path`,
`transcript_root`, and `log_dir` beneath it unless you set those explicitly.

If you explicitly set a derived key like `db_path` in `config.toml`, setting
`data_dir` (or `NIGHTDESK_DATA_DIR`) will not override it — only keys you left
unset are derived from `data_dir`.

| config.toml key | Env var | CLI flag | Default |
| --- | --- | --- | --- |
| — | `NIGHTDESK_CONFIG` | `--config` | `~/.config/nightdesk/config.toml` |
| — | `NIGHTDESK_SECRETS` | `--secrets` | `~/.config/nightdesk/secrets.env` |
| `data_dir` | `NIGHTDESK_DATA_DIR` | `--data-dir` | `~/.local/share/nightdesk` |
| `db_path` | `NIGHTDESK_DB_PATH` | `--db-path` | `<data_dir>/nightdesk.db` |
| `transcript_root` | `NIGHTDESK_TRANSCRIPT_ROOT` | `--transcript-root` | `<data_dir>/transcripts` |
| `log_dir` | `NIGHTDESK_LOG_DIR` | `--log-dir` | `<data_dir>/logs` |
| `worktree_root` | `NIGHTDESK_WORKTREE_ROOT` | `--worktree-root` | `~/.local/share/nightdesk-worktrees` |
| `bind_host` | `NIGHTDESK_BIND_HOST` | `--bind-host` | `127.0.0.1` |
| `bind_port` | `NIGHTDESK_BIND_PORT` | `--bind-port` | `8765` |
| `bearer_token` | `NIGHTDESK_BEARER_TOKEN` | — | (random at setup) |
| `worktree_base_ref` | `NIGHTDESK_WORKTREE_BASE_REF` | — | (unset) |

`nightdesk-config list` prints the resolved values; `nightdesk-config set <key>
<value>` writes a key back to the active config file.

To run a throwaway second instance with its own database, transcripts, and
config — no `HOME` override needed:

```bash
NIGHTDESK_CONFIG=/tmp/nd-test/config.toml NIGHTDESK_DATA_DIR=/tmp/nd-test \
  nightdesk-init
NIGHTDESK_CONFIG=/tmp/nd-test/config.toml NIGHTDESK_DATA_DIR=/tmp/nd-test \
  nightdesk-api --bind-port 9999
```

The configured `data_dir`, `db_path`, `transcript_root`, and `log_dir` are also
added to the sandbox exclusion list, so a relocated database is still never
bind-mounted into a run.

## Sandbox model

Each run executes inside a fresh bubblewrap sandbox with:

- A separate PID, IPC, and UTS namespace (no namespace sharing with the host).
- A fresh `$HOME` tmpfs (`/sandbox-home`).
- Read-only `/usr` (needed for libc, the dynamic linker, and the `claude` binary).
- Curated `/etc`: only `resolv.conf`, `hosts`, the CA bundle, and `passwd`/`group`.
- The Python runtime, the nightdesk package, and the resolved `claude` binary
  bind-mounted explicitly.
- Whatever filesystem paths the profile and ticket allow, read or read-write
  as specified.
- Network: the host network namespace is always inherited so the sandboxed
  CC can reach `api.anthropic.com` (or `ANTHROPIC_BASE_URL`) for inference.
  The `network_mode` profile setting is user intent; `off` is enforced via
  `denied_tools` (Bash, WebFetch, etc.) rather than at the namespace layer.
  Per-host outbound allowlists are a known v1 limitation. The long-lived
  bearer token is never available inside the sandbox; per-run scoped tokens
  are the only way for the agent to call back into the API.
- `~/.config/nightdesk/` and `~/.local/share/nightdesk/` are explicitly NOT
  mounted, so the agent cannot read the bearer token off disk.

## What's not in v1

- Pass-through of user Claude Code skills, agents, hooks, or MCP servers.
- Backends other than Claude Code.
- macOS / Windows.
- Real network isolation (per-host outbound allowlists, blocked LAN access).
- Per-run CPU/RAM/wall-clock caps.
- Disk-usage automation.
- Multi-user / multi-tenant deployment.

## Development

If you want to hack on nightdesk rather than just use it, this gets you a
full checkout with the web UI on a fresh machine:

```bash
git clone <repo>
cd nightdesk

# 1. Python environment (uv, or fall back to venv+pip if you don't have it).
uv sync                            # creates .venv from uv.lock
# .venv (or the equivalent uv run) has nightdesk-* on PATH from here on.

# 2. Build the SPA. Requires Node 20+.
./scripts/build-frontend.sh        # npm ci + vite build -> frontend/dist
# frontend/dist is served at the app root `/` by nightdesk-api once it exists;
# skip this step and you get the JSON API only, with nothing useful at `/`.

# 3. Create the DB + default config.
nightdesk-init

# 4. Run it. Either the combined dev command, or the two processes separately:
nightdesk-dev                      # API + worker together, hot reload
# --- or ---
nightdesk-api                      # in one terminal
nightdesk-worker                   # in another

pytest                             # runs the test suite
```

If you don't use uv: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`,
then prefix the commands above with `.venv/bin/`.

`nightdesk-dev` serves whatever is currently in `frontend/dist` — it does not
build or watch the frontend itself. For frontend-only iteration with instant
hot reload (no rebuild-and-refresh loop), run a throwaway API and the Vite
dev server instead; see `frontend/README.md`.

`nightdesk-migrate` exposes Alembic subcommands (`up`, `down`, `current`,
`history`, `heads`, `stamp`).

To refresh the documentation screenshots, seed the demo database and capture the UI with Playwright. See `docs/screenshots.md`.

## Troubleshooting

- **Browser opens to the login page instead of the board.** Your session
  cookie expired. Run `nightdesk-login` or paste the bearer from
  `~/.config/nightdesk/config.toml`.
- **`nightdesk-api.service` keeps restarting.** Check `journalctl --user -u
  nightdesk-api`. A migration failure exits with code 70 and systemd is
  configured to stop restarting on that code; fix the issue and
  `systemctl --user start nightdesk-api.service`.
- **Worker isn't picking up tickets.** Open `/diagnostics`. If
  `cc_check_status` is anything but `ok`, the Claude binary is missing,
  too old, or unreachable. Fix it in `/settings` or by reinstalling CC.
- **Sandbox refuses to start a run.** Common causes: profile points at a path
  that doesn't exist, or the workspace is inside `~/.config/nightdesk/` or
  `~/.local/share/nightdesk/` (denied by design). The per-run log file under
  `~/.local/share/nightdesk/logs/runs/` has the full bwrap argv.
