---
name: nightdesk-monitor-tickets
description: Use when watching a batch of nightdesk tickets through to completion — e.g. to merge/review each one as its run finishes, or to babysit a serialized (dependency-linked) set. Covers the poll/emit recipe for the Monitor tool: JSON-API auth, derived run status, tz-fixed timestamps, elapsed/duration, and the transition/failure/stuck/heartbeat/terminal emit contract. Ships a copy-pasteable reference monitor script.
---

# nightdesk ticket monitoring

> **nightdesk skill** · package v0.0.1 · updated 2026-07-12. A user-global copy
> (installed by `nightdesk-install-skills`) can drift from the code; if anything
> below disagrees with `GET /openapi.json`, re-run `nightdesk-install-skills --force`
> (or `--all --force`) to refresh.

Watch a set of tickets to completion and emit one event per line on stdout — the contract the Monitor tool consumes. This is the "is it done yet / did it blow up" watch you stand up after queueing a batch, so you can merge or review each ticket as its run lands.

Auth and base-URL depth live in `nightdesk-api`; this skill assumes you know the JSON `/api/v1/*` surface and only covers what's specific to *monitoring*. The whole skill is: **the gotchas below + the reference script + the Monitor invocation.**

## The gotchas (each one bit us in a real batch)

| # | Gotcha | What to do |
|---|---|---|
| 1 | **Wrong surface.** HTMX `/board/*`, `/tickets/{tid}/*` return `204 + HX-Redirect: /` — useless to a script. | Use JSON `/api/v1/*` with `Authorization: Bearer <token>` only. |
| 1 | **Stale token.** A cached/revoked token returns `401 {"detail":"invalid token"}`. | Resolve the token **fresh** every run: `NIGHTDESK_TOKEN` env, else `~/.config/nightdesk/agent-token`. Never the admin `bearer_token` in `config.toml` (see `nightdesk-api` Auth). Host/port may still come from `config.toml`. |
| 2 | **A run has NO `status` field.** `run["status"]` is silently `null`. | Derive: *running* ⟺ `finished_at is null`. Outcome = `exit_status`. |
| 2 | **`exit_status` is a STRING, not a number.** Values: `"success"` \| `"failed"` \| `"cancelled"` \| `"worker_crash"`. | Test success with `== "success"`. A `!= 0` check flags *every* run as failed. |
| 2 | **Error detail is split across fields.** | Read `error_summary` (human text) and `failure_kind` (category). |
| 2 | **Ticket status IS a real field** (`draft\|queued\|running\|review\|archived`); run status is not. Don't conflate them. | Track ticket `status` for transitions/terminal; derive run state separately. |
| 3 | **Timestamps are tz-NAIVE UTC.** They look like `2026-06-30T20:59:37.602849` (no offset). `datetime.now(timezone.utc) - naive` raises `TypeError`. | Attach `tzinfo=timezone.utc` after `fromisoformat`, or work entirely in naive UTC. (SQLite drops the tzinfo the column declares.) |
| 4 | **The user wants elapsed visible.** It's the proof the watch is alive. | Running: `now - started_at`. Finished: `finished_at - started_at` (duration). Print both. |
| 5 | **bash + `jq` chokes** on prompts/strings with backticks/em-dashes; shell quoting bites. | Write the poll loop in **Python** (clean JSON + real date math). |
| 5 | **One event = one stdout line**, `print(..., flush=True)`. | Buffering looks like a hung watch. Always flush. |
| 6 | **Silence ≠ success.** A monitor that greps only for the success token stays mute through a crashloop — the #1 way these have failed. | Emit on **all** of: transitions, run-finish, failure, stuck, heartbeat, and a terminal line. |
| 6 | **`exit_status="cancelled"` is a normal cancel/requeue, NOT a failure.** | Failure = `exit_status not in (None, "success", "cancelled")` **or** `failure_kind` set. |
| 7 | **Event spam auto-stops the watch.** | Don't emit every poll. Emit on transitions/failures + a heartbeat every ~6–8 polls + the terminal line. Rapid transitions collapse within ~200ms anyway. |
| 8 | **The Monitor tool is hard to debug once running.** | **Smoke-test first:** run the script `--once` and eyeball the snapshot (statuses + elapsed, no exception) before wiring it to the watch. |
| 9 | **A watch must end.** | Self-exit when all tracked tickets are terminal (`review`/`archived` = run finished, parked for you). Hard-cap with a TIMEOUT exit so it can't run forever. |
| 10 | **Deps endpoint shape.** `POST\|PUT /.../dependencies/{dep_on_id}` → `405`. | Add a dep with `POST /api/v1/tickets/{tid}/dependencies` + body `{"depends_on_id": "<blocker_id>"}`. Remove with `DELETE /api/v1/tickets/{tid}/dependencies/{dep_on_id}`. The scheduler **does** respect linked deps. |
| 10 | **Link deps at-or-before queue time.** Queue-first, link-later lets the scheduler claim dependents before the link exists → they run prematurely against missing prerequisites. | If you linked late, cancel + requeue the dependents. |

### Terminal status

A run finishing — **success or failure** — transitions the ticket `running → review` (failures park in `review` too, so you see them). So for a "monitor to completion" watch, a ticket is **terminal** when `status in ("review", "archived")`. That `review` flip is your merge/review signal.

## Reference monitor script

Copy this to `monitor_tickets.py` (or anywhere outside the repo). It takes ticket IDs on argv, or a `--status` filter via the API; resolves the token fresh each run (`NIGHTDESK_TOKEN` env → `~/.config/nightdesk/agent-token`; base URL from `NIGHTDESK_BASE_URL` or config.toml host/port); and bakes in every lesson above — flush-per-line, transition/failure/stuck/heartbeat/terminal emit, elapsed math, tz-fixed timestamps, self-exit on all-terminal, TIMEOUT cap. `--once` is the built-in smoke test.

```python
#!/usr/bin/env python3
"""nightdesk ticket progress monitor.

Watches a set of nightdesk tickets to completion (e.g. so you can merge/review
them as they finish) and prints one event per line on stdout — the emit contract
the Monitor tool consumes.

Bakes in every hard-won lesson from monitoring real batches:

  * The token is resolved FRESH every run: NIGHTDESK_TOKEN env, else the
    ~/.config/nightdesk/agent-token file (a scoped ndk_ token — never the admin
    bearer). Stale/revoked -> 401 {"detail":"invalid token"}. Only the JSON
    /api/v1/* surface is used.
  * A run has NO `status` field. It is *running* iff finished_at is null; the
    outcome is the STRING exit_status ("success"|"failed"|"cancelled"|
    "worker_crash"); error detail is error_summary / failure_kind.
  * API timestamps are tz-NAIVE UTC ("2026-06-30T20:59:37.602849", no offset).
    Subtracting a tz-aware now() raises TypeError, so we attach UTC after parse.
  * Emit on transitions / finish / failure / stuck / heartbeat / terminal —
    silence is NOT success. Bounded volume: never every poll (spam auto-stops
    the watch), a heartbeat only every ~7 polls, rapid transitions collapse.
  * Self-exits when every tracked ticket is terminal (status in review|archived),
    hard-capped by a TIMEOUT so it can't run forever.

Usage:
  python3 monitor_tickets.py <ticket_id> [ticket_id ...]
  python3 monitor_tickets.py --status running            # snapshot+watch all running
  python3 monitor_tickets.py --status running,queued      # multiple groups
  python3 monitor_tickets.py <ids...> --once              # SMOKE TEST: one snapshot, exit
  python3 monitor_tickets.py <ids...> --interval 60 --timeout-mins 360

Env overrides (token file / config.toml host+port otherwise):
  NIGHTDESK_TOKEN          a scoped ndk_ token (preferred)
  NIGHTDESK_BASE_URL       e.g. http://127.0.0.1:8765
  (NIGHTDESK_API_URL / NIGHTDESK_BEARER_TOKEN still honored as legacy names)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- Tunables ----------------------------------------------------------------
DEFAULT_INTERVAL = 45          # seconds between polls (matches the reference watcher)
HEARTBEAT_EVERY = 7            # ~5 min at 45s; proof-of-life line WITH elapsed
STUCK_MINS = 30                # running w/ no transition/finish this long -> STUCK
DEFAULT_TIMEOUT_MINS = 240     # hard cap so the watch can't run forever
TERMINAL_STATUSES = ("review", "archived")  # ticket finished its run, awaiting you
CONFIG_PATH = Path(
    os.environ.get("NIGHTDESK_CONFIG", str(Path.home() / ".config/nightdesk/config.toml"))
)


# --- Config + auth -----------------------------------------------------------
def load_base_and_token() -> tuple[str, str]:
    """Resolve base URL + token FRESH each run (env overrides win).

    Token order: NIGHTDESK_TOKEN env -> NIGHTDESK_BEARER_TOKEN env (legacy
    name) -> ~/.config/nightdesk/agent-token file. Monitoring only needs read
    scopes, so a scoped ndk_ token is the right credential — never the admin
    bearer in config.toml. config.toml is consulted for host/port only.
    """
    token = os.environ.get("NIGHTDESK_TOKEN", "") or os.environ.get(
        "NIGHTDESK_BEARER_TOKEN", ""
    )
    if not token:
        try:
            token = (Path.home() / ".config/nightdesk/agent-token").read_text().strip()
        except OSError:
            pass
    base = os.environ.get("NIGHTDESK_BASE_URL", "") or os.environ.get(
        "NIGHTDESK_API_URL", ""
    )
    if token and base:
        return base.rstrip("/"), token
    host, port = "127.0.0.1", 8765
    try:
        with open(CONFIG_PATH, "rb") as fh:
            raw = tomllib.load(fh)
        cfg = {**raw, **raw.get("nightdesk", {})}  # top-level wins; tolerate nesting
        host = cfg.get("bind_host", host)
        port = int(cfg.get("bind_port", port))
    except FileNotFoundError:
        pass  # rely on env / defaults below
    except Exception as exc:  # malformed toml etc.
        print(f"WARN could not parse {CONFIG_PATH}: {exc}", flush=True)
    if not base:
        base = f"http://{host}:{port}"
    if not token:
        sys.stderr.write(
            "ERROR no token: set NIGHTDESK_TOKEN or write a scoped token to "
            "~/.config/nightdesk/agent-token (ask the human to mint one)\n"
        )
        sys.exit(2)
    return base.rstrip("/"), token


def api_get(base: str, token: str, path: str, timeout: float = 15.0):
    """GET a JSON /api/v1/* path. Returns parsed JSON or raises."""
    req = urllib.request.Request(base + path,
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# --- Timestamps (lesson: tz-NAIVE UTC) --------------------------------------
def parse_ts(s):
    """Parse an API timestamp, forcing UTC. API JSON is tz-NAIVE; a naive dt
    subtracted from a tz-aware now() raises TypeError, so we attach UTC."""
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# --- Run state is DERIVED (lesson: no `status` on a run) ---------------------
def run_outcome(run) -> str:
    """'running' | 'cancelled' | 'success' | 'failed' | 'unknown'."""
    if run is None:
        return "unknown"
    if run.get("finished_at") is None:
        return "running"
    return run.get("exit_status") or "unknown"


def run_is_running(run) -> bool:
    return run is not None and run.get("finished_at") is None


def is_failure(run) -> bool:
    """A real failure. cancel is NOT a failure (normal cancel/requeue)."""
    if run is None:
        return False
    es = run.get("exit_status")
    if es == "cancelled":
        return False
    return es not in (None, "success") or bool(run.get("failure_kind"))


def elapsed_for(run):
    """Running -> now-started_at. Finished -> finished_at-started_at (duration)."""
    started = parse_ts(run.get("started_at")) if run else None
    if started is None:
        return None
    end = parse_ts(run.get("finished_at")) if run.get("finished_at") else now_utc()
    return (end - started).total_seconds()


# --- Emit (lesson: one event = one flushed line) ----------------------------
def emit(line: str) -> None:
    print(line, flush=True)


def short(tid: str) -> str:
    return tid[:8]


def summarize_ticket(tid: str, ticket, run) -> str:
    title = (ticket or {}).get("title", "?")
    status = (ticket or {}).get("status", "?")
    base = f"[{short(tid)}] {status:<8} {title[:40]}"
    if run is None:
        return base
    secs = elapsed_for(run)
    dur = fmt_dur(secs) if secs is not None else "?"
    if run_is_running(run):
        return f"{base}  elapsed {dur}"
    es = run.get("exit_status") or "?"
    return f"{base}  done {es} in {dur}"


# --- Ticket set resolution ---------------------------------------------------
def resolve_ids(base: str, token: str, statuses: list[str]) -> list[str]:
    ids: list[str] = []
    for st in statuses:
        for t in api_get(base, token, f"/api/v1/tickets?status={st}&limit=500"):
            ids.append(t["id"])
    seen, out = set(), []
    for i in ids:                      # de-dup, preserve order
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# --- Snapshot one ticket: returns (ticket, run|None) -------------------------
def snapshot_ticket(base: str, token: str, tid: str):
    ticket = api_get(base, token, f"/api/v1/tickets/{tid}")
    run = None
    rid = ticket.get("current_run_id")
    if rid:
        run = api_get(base, token, f"/api/v1/runs/{rid}")
    return ticket, run


# --- Monitor loop ------------------------------------------------------------
def monitor(base: str, token: str, ids: list[str], args) -> int:
    deadline = now_utc().timestamp() + args.timeout_mins * 60
    # Per-ticket memory for transition / stuck / failure dedup.
    prev_status = {tid: None for tid in ids}
    prev_run_finished = {tid: False for tid in ids}
    prev_failed = {tid: False for tid in ids}
    stuck_fired = {tid: False for tid in ids}
    last_activity = {tid: now_utc().timestamp() for tid in ids}
    poll = 0

    emit(f"START watching {len(ids)} ticket(s): {', '.join(short(i) for i in ids)}")

    while True:
        poll += 1
        all_terminal = True
        try:
            for tid in ids:
                ticket, run = snapshot_ticket(base, token, tid)
                status = ticket.get("status")
                running = run_is_running(run)
                finished = run is not None and run.get("finished_at") is not None

                # 1) status TRANSITION
                if prev_status[tid] is not None and status != prev_status[tid]:
                    emit(f"TRANSITION [{short(tid)}] "
                         f"{prev_status[tid]} -> {status}  "
                         f"{(ticket.get('title') or '')[:40]}")
                    last_activity[tid] = now_utc().timestamp()
                    stuck_fired[tid] = False
                prev_status[tid] = status

                # 2) a run FINISHING (newly finished since last poll)
                if finished and not prev_run_finished[tid]:
                    es = run.get("exit_status") or "unknown"
                    detail = ""
                    if run.get("failure_kind"):
                        detail = f" failure_kind={run['failure_kind']}"
                    if run.get("error_summary"):
                        detail += f" :: {run['error_summary'][:160]}"
                    dur = fmt_dur(elapsed_for(run) or 0)
                    emit(f"RUN_DONE [{short(tid)}] exit_status={es} in {dur}{detail}")
                    last_activity[tid] = now_utc().timestamp()
                    stuck_fired[tid] = False
                prev_run_finished[tid] = finished

                # 3) detected FAILURE (once)
                if is_failure(run) and not prev_failed[tid]:
                    emit(f"FAILURE  [{short(tid)}] exit_status={run.get('exit_status')} "
                         f"failure_kind={run.get('failure_kind')} "
                         f"error={(run.get('error_summary') or '')[:160]}")
                    prev_failed[tid] = True
                elif not is_failure(run):
                    prev_failed[tid] = False

                # 4) STUCK: running, no transition/finish for STUCK_MINS
                if running and status == "running":
                    if now_utc().timestamp() - last_activity[tid] > STUCK_MINS * 60:
                        if not stuck_fired[tid]:
                            emit(f"STUCK    [{short(tid)}] running "
                                 f">{STUCK_MINS}m with no transition "
                                 f"(elapsed {fmt_dur(elapsed_for(run) or 0)})")
                            stuck_fired[tid] = True
                else:
                    stuck_fired[tid] = False

                if status not in TERMINAL_STATUSES:
                    all_terminal = False

            # 5) terminal: all done
            if all_terminal:
                emit(f"ALL_DONE all {len(ids)} ticket(s) terminal "
                     f"({'/'.join(TERMINAL_STATUSES)}) after {poll} poll(s)")
                return 0

            # 6) heartbeat every N polls WITH elapsed (proof of life)
            if poll % HEARTBEAT_EVERY == 0:
                lines = []
                for tid in ids:
                    try:
                        ticket, run = snapshot_ticket(base, token, tid)
                        lines.append(summarize_ticket(tid, ticket, run))
                    except Exception as exc:
                        lines.append(f"[{short(tid)}] snapshot error: {exc}")
                emit("HEARTBEAT poll={} :: ".format(poll) + " | ".join(lines))

        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode(errors="replace")[:160]
            except Exception:
                pass
            emit(f"WARN poll {poll} HTTP {exc.code} {body}")
        except urllib.error.URLError as exc:
            emit(f"WARN poll {poll} network error: {exc.reason}")
        except Exception as exc:  # never let one bad poll kill the watch
            emit(f"WARN poll {poll} error: {type(exc).__name__}: {exc}")

        if now_utc().timestamp() >= deadline:
            emit(f"TIMEOUT reached {args.timeout_mins}m hard cap after {poll} polls")
            return 2

        time.sleep(args.interval)


# --- Smoke test (lesson: validate BEFORE wiring to Monitor) ------------------
def smoke_once(base: str, token: str, ids: list[str]) -> int:
    emit(f"SMOKE snapshot of {len(ids)} ticket(s) from {base}")
    rc = 0
    for tid in ids:
        try:
            ticket, run = snapshot_ticket(base, token, tid)
            emit(summarize_ticket(tid, ticket, run))
            if run is not None:                       # exercise the tz-fixed math
                secs = elapsed_for(run)
                emit(f"   run_id={short(run.get('id', ''))} "
                     f"started={run.get('started_at')} "
                     f"finished={run.get('finished_at')} "
                     f"exit_status={run.get('exit_status')} "
                     f"failure_kind={run.get('failure_kind')} "
                     f"elapsed/dur={fmt_dur(secs or 0)}")
        except Exception as exc:
            rc = 1
            emit(f"ERROR [{short(tid)}] {type(exc).__name__}: {exc}")
    emit("SMOKE OK" if rc == 0 else "SMOKE FAILED")
    return rc


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Monitor nightdesk tickets to completion.")
    p.add_argument("ids", nargs="*", help="ticket IDs to watch")
    p.add_argument("--status", help="comma list of statuses to snapshot+watch "
                                    "(e.g. running,queued)")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                   help=f"poll interval seconds (default {DEFAULT_INTERVAL})")
    p.add_argument("--timeout-mins", type=int, default=DEFAULT_TIMEOUT_MINS,
                   help=f"hard cap in minutes (default {DEFAULT_TIMEOUT_MINS})")
    p.add_argument("--once", action="store_true",
                   help="SMOKE TEST: print one snapshot and exit")
    a = p.parse_args(argv)

    base, token = load_base_and_token()

    if a.status:
        statuses = [s.strip() for s in a.status.split(",") if s.strip()]
        ids = resolve_ids(base, token, statuses)
        if not ids:
            emit(f"No tickets found for --status {a.status}")
            return 0
        emit(f"Resolved {len(ids)} ticket(s) from status={a.status}")
    else:
        ids = a.ids

    if not ids:
        p.error("give ticket IDs or --status STATUS")

    if a.once:
        return smoke_once(base, token, ids)
    return monitor(base, token, ids, a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

## Wiring it to the Monitor tool

**Step 1 — smoke test first** (lesson #8). Run the snapshot once standalone and confirm it prints statuses + elapsed with no exception:

```bash
# by explicit IDs:
python3 monitor_tickets.py <id1> <id2> <id3> <id4> --once
# or resolve the current batch from the API:
python3 monitor_tickets.py --status running,queued --once
```

You should see one `SMOKE` block per ticket with a real `elapsed`/`dur` and `exit_status`, ending in `SMOKE OK`. Fix any error here *before* starting the watch — once the persistent monitor is running it's hard to debug.

**Step 2 — start the persistent watch.** Stand it up as a persistent, non-backgrounded process; the script self-exits (`ALL_DONE`, rc 0) when every tracked ticket hits `review`/`archived`, or `TIMEOUT` (rc 2) at the cap:

```
command: python3 monitor_tickets.py <id1> <id2> <id3> <id4>
```

Or watch the live batch without copying IDs:

```
command: python3 monitor_tickets.py --status running,queued
```

Tune for long/serialized runs: `--interval 60 --timeout-mins 360`. The script's stdout (one event per line, flushed) is what the Monitor tool surfaces; it never spams (transitions/failures + a heartbeat every ~7 polls + the terminal line), so it won't trip the watch's auto-stop.

## Sister skills

- `nightdesk-api` — bearer auth, base-URL discovery, the JSON vs HTMX surface split.
- `nightdesk-ticket-ops` — create/transition/cancel/requeue/archive, runs, transcript. Use it to set up dependency links (`POST .../dependencies`) at-or-before queue time.
