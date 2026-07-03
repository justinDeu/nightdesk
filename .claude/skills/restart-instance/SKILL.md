---
name: restart-instance
description: Internal dev runbook for restarting the nightdesk API and/or worker processes on the live box. Use BEFORE stopping nightdesk-api/nightdesk-worker. NOT shipped to users.
internal: true
---

# Restarting the nightdesk instance (dev runbook)

Internal dev skill. Opts out of shipping via the `internal: true` frontmatter flag, so `nightdesk-install-skills` never copies it to users.

## How the instance runs on this box

The live instance is NOT under systemd here (the README mentions `nightdesk-api.service` units, but no unit files are installed on this host). `nightdesk-api` and `nightdesk-worker` are plain processes with cwd `/home/thor/fun/nightdesk`, no args, orphaned to systemd --user after their tmux pane died. Logs go to `~/.local/share/nightdesk/{api,worker}.log`.

```bash
pgrep -af "nightdesk-(api|worker)"
```

## If you only need a code change served

Most changes (including all source edits the API serves) only require restarting the **API**, not the worker. The worker can keep running tickets across an API restart. So: do you actually need to touch the worker? If not, restart only the API. If tickets ARE running and you must restart the worker, stop — follow the `restart-worker` skill first (confirm with the user, record running tickets, resume them with priority).

## Stop

```bash
kill -TERM <api-pid> <worker-pid>
```

Poll until both are gone. (If restarting the worker with tickets running, you already followed the worker-restart skill.)

## Start the API FIRST, then the worker (migration race)

Both processes call `_run_migrations` → `alembic upgrade head` on startup. Launching them at the same time races on the SQLite DB: both see the old revision, both run the new migration, one wins and stamps it, the other crashes on `duplicate column name`. **Always start the API alone, wait for it to be healthy, then start the worker.**

```bash
cd /home/thor/fun/nightdesk
VENV=.venv/bin; LOGD=~/.local/share/nightdesk

# 1. API only
setsid bash -c "exec '$VENV/nightdesk-api' >> '$LOGD/api.log' 2>&1" < /dev/null &

# 2. Wait for health (migration completes here)
until [ "$(curl -s -o /dev/null -w '%{http_code}' http://0.0.0.0:8765/openapi.json)" = 200 ]; do sleep 1; done

# 3. Now the worker
setsid bash -c "exec '$VENV/nightdesk-worker' >> '$LOGD/worker.log' 2>&1" < /dev/null &
```

If a previous crash left the DB stamped at the new revision with the column already present, starting the API alone is the fix — `upgrade head` becomes a no-op. Recover, then start the worker.

## Verify

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://0.0.0.0:8765/openapi.json   # 200
curl -s "${AUTH[@]}" "$BASE/api/v1/worker/status" | jq '{stale,total_running,pid}'
```

`stale: false` and the pid matches what you just launched. Then resume any interrupted tickets per the `restart-worker` skill.
