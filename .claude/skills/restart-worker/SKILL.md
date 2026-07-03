---
name: restart-worker
description: Internal dev runbook for restarting the nightdesk worker without stranding running tickets. Use BEFORE stopping or restarting nightdesk-worker. NOT shipped to users.
internal: true
---

# Restarting the nightdesk worker (dev runbook)

This is an **internal** dev skill. It opts out of shipping via the `internal: true` frontmatter flag, so `nightdesk-install-skills` never copies it to users. User-facing skills live alongside it in `.claude/skills/` without that flag.

## The rule

A running ticket is real, paid-for work in flight. Killing the worker mid-run aborts the active agent subprocess and strands the ticket. **Do not restart the worker while tickets are running unless it is absolutely necessary** (e.g. the worker is wedged, or a code change requires it). A docs-only change, a skill update, or editing most source files does NOT require a worker restart — only the API server needs restarting for code changes the API serves. When in doubt, restart only the API and leave the worker alone. For the stop/start mechanics and the API-before-worker migration-race rule, see the `restart-instance` skill.

## Before restarting — check and confirm

1. List what is running and tell the user, by title:
   ```bash
   curl -s "${AUTH[@]}" "$BASE/api/v1/tickets?status=running" \
     | jq -r '.[] | "\(.id)  \(.title)  run=\(.current_run_id)"'
   ```
   Also check `GET /api/v1/worker/status` (`total_running`, `run_now_running`).
2. If nothing is running, a worker restart is safe — proceed (the recovery steps below do not apply).
3. If tickets ARE running, **do not restart without explicit per-instance confirmation from the user.** Present the list and ask: restart now, or wait for the runs to finish? Honor a "wait" answer — poll until the runs complete or the user changes their mind.

## Record the interrupted tickets

Before stopping the worker, capture each running ticket's id, title, and `current_run_id` (and note its active conversation so you can resume it). You will need this list to restore them after the restart. Keep it in front of you for the whole procedure and report against it.

## Stop the worker (and its run subprocesses)

Stop the worker. The orphan-recovery sweep only reclaims a run once its subprocess pid is provably dead, so verify the agent children are gone too:

```bash
pgrep -af "nightdesk-worker"
pgrep -af "claude"   # or whatever runtime the profiles spawn — the run subprocesses
```

If run subprocesses linger (orphaned to init when the worker died), kill them, then wait. Only once the pids are dead will the next sweep reclaim those tickets.

## Wait for orphan recovery

On restart the worker runs `recover_orphaned_runs` every tick (`src/nightdesk/worker/heartbeat.py`). It does two things, neither of which resumes the work:

- **Session-backed interrupted runs** → the turn is marked `worker_crash`, the ticket moves `running → review`. The conversation and SDK session id persist, so the run IS resumable.
- **Tickets marked `running` with no Run row** (never really started) → after a 30s grace, reset to `queued`. These have no in-flight work to resume.

Poll until every recorded ticket has left `running`:

```bash
curl -s "${AUTH[@]}" "$BASE/api/v1/tickets?status=running" | jq 'length'   # expect 0 for your tickets
```

If a recorded ticket stays `running`, its run subprocess is probably still alive — recheck pids and wait. Do not proceed to the next step while a recorded ticket is still `running`.

## Resume the interrupted tickets with priority

For each recorded ticket that is now in `review`, **continue** it — this resumes the SDK conversation from where it died and stages a **run-now**, which bypasses the queue AND the capacity cap, so the interrupted tickets run first and nothing else takes their place:

```bash
curl -s "${AUTH[@]}" -X POST "$BASE/api/v1/tickets/$TID/continue" \
  -H 'Content-Type: application/json' \
  -d '{"message": "The nightdesk worker was restarted under you. Resume the work you were doing and pick up exactly where you left off."}' | jq '{id, status, current_run_id}'
```

Rules and fallbacks:
- `continue` requires a resumable active conversation (a session id) and `review`/`archived` status. If it returns `409` ("... start a new conversation ..."), the run had no resumable session — fall back to `POST /requeue` (so it re-runs) and tell the user this one could not resume mid-flight.
- The runless tickets that recovery reset to `queued` do not need a continue — they just re-run from the queue. If you want them prioritized too, `POST /run-now` them.
- Because `continue` sets run-now, these tickets jump ahead of everything currently queued. That is the intent. Just be aware run-now bypasses `max_parallel`, so if many tickets were interrupted they may all start at once.

## Verify and report

Confirm the recorded tickets are the ones running again and that no previously-queued ticket jumped ahead:

```bash
curl -s "${AUTH[@]}" "$BASE/api/v1/tickets?status=running" | jq -r '.[] | "\(.id)  \(.title)"'
curl -s "${AUTH[@]}" "$BASE/api/v1/worker/status" | jq '{total_running, run_now_running}'
```

Then tell the user, against the original list: which tickets were running before, that the worker was restarted, that each was continued from where it left off (or could not be and was requeued), and that they have priority over the queue.

## Inform the user throughout

This is not optional. At minimum tell the user: (1) which tickets were running and at risk, (2) that you asked/will ask before restarting, (3) that the worker was restarted, (4) that the interrupted tickets were resumed with priority and are running again (or why one could not be), and (5) confirm no queued ticket took their place.
