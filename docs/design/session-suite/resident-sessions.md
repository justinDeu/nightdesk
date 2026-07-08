# Resident Interactive Sessions — v2 Design

Supersedes interactive-sessions.md (v1, sessions-as-tickets). Product decision: tickets and
interactive sessions are two separate flows. Tickets = queue background work, the desk works
over time. Sessions = live work-hours chat. Sessions leave the ticket lifecycle completely.

## The load-bearing insight

The v1 slowness (~8-20s per turn) is structural: a chat turn is `continue_ticket → run_now →
scheduler tick → spawn nightdesk-run-ticket → bwrap → cold Claude Code CLI → --resume replay →
one-shot query() → review`. Every turn pays worker-tick + subprocess + bwrap + CC-boot +
full-history replay. The process is born and dies per turn; no tuning fixes that.

The fix is a **resident process that outlives the turn**, so warm turns cost only
message-delivery + model TTFT. Two hard constraints shape everything:

- **SDK:** `ClaudeSDKClient` keeps a persistent anyio task group alive from `connect()` to
  `disconnect()` and cannot be used across async runtime contexts. One session = one
  long-lived coroutine owning the client for its entire life.
- **Sandbox:** `build_bwrap_argv` refuses to mount `~/.local/share/nightdesk`
  (`_exclusion_paths` in `worker/sandbox.py`), so the agent process cannot touch the DB.
  v1 already solves this: sandboxed `_sdk_runner` speaks NDJSON over stdin/stdout and
  host-side watchers bridge DB↔pipe. v2 keeps exactly that split, just persistent.

The resident session is **two processes**:

```
outer host  nightdesk-session <id>   (NOT sandboxed, detached like run-ticket)
  owns: Session DB row, SessionTurn inbox poll, transcript file, lifecycle,
        usage/resume persistence. Bridges DB <-> inner over pipe/HTTP.
    |
    +-- inner  (bwrap-wrapped, --die-with-parent)
          claude:   python -m nightdesk.worker._session_runner  (persistent ClaudeSDKClient)
                    NDJSON control lines in on stdin, transcript events out on stdout
          opencode: opencode serve  (kept ALIVE across turns; host drives localhost over httpx)
```

`_session_runner` (the reworked heart — `_sdk_runner._run_query` today does one
`async for evt in query(...)` and stdin closes at `claude_executor.py:95`):
1. read init spec from stdin (same shape `claude_executor._build_runner_spec` builds).
2. `client = ClaudeSDKClient(options); await client.connect()` — stdin stays open, CC boots ONCE.
3. loop over NDJSON control lines:
   - `{"type":"user_turn","turn_id","text"}` → `await client.query(text)`;
     `async for msg in client.receive_response(): emit(translate(msg))`; emit
     `{"type":"turn_complete","turn_id","session_id","usage":<cumulative>,"cost_usd":<cumulative>}`.
   - `{"type":"interrupt"}` → `await client.interrupt()` (drain the error_during_execution
     ResultMessage before the next turn, per the SDK caveat).
   - `{"type":"shutdown"}` → `await client.disconnect()`, exit 0.
4. all inside one `asyncio.run` coroutine (anyio-context constraint).

Event translation reuses `_sdk_runner._event_to_dict` + `claude_translator.translate` verbatim;
`_AsyncEmitter` backpressure, `_HEADLESS_DISALLOWED`, CLIJSONDecodeError recovery carry over.
opencode is smaller: `drive_opencode` already runs host-side and finishes on `session.idle`;
v2 just doesn't `_terminate` on idle — it loops back to await the next inbox turn on the same
session/server (`_post_text` already exists).

Contrast: other tools drive a resident Claude TUI with tmux `send-keys` PTY scraping. Rejected —
the SDK streaming client gives a structured, typed, interruptible channel with no screen-scraping
fragility. Same "resident agent, fast turns" outcome, real protocol.

## Goals / Non-Goals

**Goals**
- Send-to-first-token at model latency on a warm session. No worker tick, no bwrap, no CC boot,
  no replay per turn.
- Sessions fully decoupled from tickets: no kind='session', no ticket states, no scheduler,
  no run_now, no Run/Conversation rows.
- Both backends resident (ClaudeSDKClient / opencode serve).
- Robust lifecycle: idle reap, crash detection, restart survival, explicit end, orphan cleanup.
- v1 resume-per-turn survives as the cold-start FALLBACK (crash/reap/restart → next message
  cold-starts with --resume).
- Live queueing of follow-ups while a turn streams; explicit interrupt.

**Non-Goals (this cut)**
- git-worktree/stacking, dependencies, cron, webhooks for sessions (directory workspaces only).
- Multi-user/collaborative sessions; concurrent turns in one session (queue + interrupt instead).
- Promote-via-kind-flip (gone; "create ticket from session" = future copy feature).
- Ticket STEER_INJECT (partly falls out; scoped as follow-up, not built here).

## Design decisions

### 1. Process topology — worker-supervised, detached per-session host
A dedicated per-session subprocess `nightdesk-session <id>`, spawned detached
(`start_new_session=True`) and supervised by the existing worker daemon. NOT a new standalone
daemon, NOT the API.
- Sandbox machinery lives in worker code; the host reuses it, spawned like `_run_ticket_cli`.
- `main.py:_spawn_subproc` subprocesses already survive worker restarts (detached; orphan
  recovery re-adopts via pid liveness). Sessions inherit that: worker restart does NOT kill
  live sessions.
- API is the wrong owner: SQLite single-writer, deploy-frequent restarts, possible multi-worker
  uvicorn. API stays a pure DB writer.

Spawn trigger: worker `tick_once` gains a session-supervisor pass parallel to `pick_eligible`
(session with a queued SessionTurn and no live host_pid → spawn, tracked like `self._subprocs`).
Cold-start tick collapsed by an optional **wake nudge**: API sends SIGUSR1 to
`WorkerHeartbeat.pid` after enqueuing on a cold session, breaking the worker's
`wait_for(shutdown_event.wait(), timeout=tick)` sleep. Nudge = latency optimization, not a
correctness dependency.

### 2. Transport API→host — DB SessionTurn inbox + host poll (~0.2s)
The SessionTurn row IS the inbox. The host polls its own session's queued turns, claims one
(queued→delivering), hands it to the resident client. No sockets, no per-host HTTP. Matches the
established watcher idiom (`_cancel_watcher`/`_steer_watcher`); 200ms worst-case delivery is
dwarfed by model TTFT. Interrupt rides the same inbox as a control row. Sockets = noted future
optimization only.

### 3. Data model — lean session-owned tables
v2 reuses none of the run pipeline, so v1's ticket-reuse argument is gone; the only reusable
piece is the transcript tail/format, a pure function on a file path. Polymorphic
Conversation/Run ownership rejected (touches scheduler, orphan recovery, run tokens, analytics
for zero benefit).

```python
class Session(Base):
    id, title, profile_id (nullable FK), project_id (nullable)
    backend: str                  # frozen snapshot (runtime lock, like Conversation.backend)
    model: Optional[str]          # frozen at start
    status: str                   # 'active' | 'idle' | 'ended' | 'crashed'
    workspace_kind: str           # 'directory'
    workspace_access: str         # 'read_write'
    source_path: str              # live tree, or scratch dir
    host: Optional[str]; host_pid: Optional[int]   # liveness = _pid_alive(host_pid)
    last_activity_at: datetime    # drives idle reap
    resume_handle: Optional[dict] # {"session_id":...} claude | {"session_id","data_dir"} opencode
    transcript_path: str          # own NDJSON file in transcript_root
    pricing_snapshot: Optional[dict]   # frozen at start
    cost_usd, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
    created_at, updated_at, ended_at

class SessionTurn(Base):          # inbox item AND turn record
    id, session_id (FK cascade), position
    kind: str                     # 'user' | 'interrupt'
    body: Text
    status: str                   # queued -> delivering -> streaming -> done
                                  # queued->cancelled ; streaming->interrupted/failed
    model_used, input/output/cache tokens, cost_usd   # per-turn slice
    error: Optional[Text]
    created_at, started_at, finished_at
```

- resume_handle mirrors `Run.session_ref` so cold-start reuses identical seed/resume mechanics.
- **Per-turn cost from cumulative:** SDK usage/cost are cumulative per client life. Host tracks a
  per-generation baseline; per-turn delta = cumulative − prev within a generation. Post-cold
  resume replay tokens land honestly on the first warm turn. Session total = Σ deltas.
- Bindings (backend, model, profile, source_path, pricing) frozen at session start.
- status = coarse persisted state; liveness DERIVED from `_pid_alive(host_pid)`.
  UI: Live / Idle(warm) / Cold / Ended.

### 4. Sandbox — sound, frozen at session start
bwrap wraps the process tree (`--unshare-pid`, `--die-with-parent`); the sandbox is a property
of the process, not the turn. Profile policy applies once at session start via a session-shaped
`_profile_to_spec` + `build_bwrap_argv` with the directory workspace mounted rw. Scratch dir:
`worktree_root.parent / "nightdesk-sessions" / <session_id>`. Per-session cc-session store at
`worktree_root.parent / "nightdesk-cc-sessions" / <session_id>` persists across turns (no
re-seed warm); on reap the session jsonl is published for the next cold-start seed.
`--die-with-parent` ties inner to the outer host — the host IS the session and survives worker
restarts via detachment.

### 5. Lifecycle state machine

```
            spawn (worker tick / wake nudge)
                 |
             [STARTING]  acquire Session row (conditional UPDATE ... WHERE host_pid IS NULL),
                 |       seed cc-session if resume_handle, build bwrap, spawn inner, connect()
                 |       status=active, host_pid=pid; transcript 'session_booting'
                 v
      +----> [LIVE:IDLE] <------------------+     warm; poll inbox ~0.2s
      |          | claim queued turn         |
      |          v                           | receive_response completes:
      |    [LIVE:STREAMING] -----------------+   slice usage, SessionTurn.done
      |          | interrupt control
      |          v
      |    (client.interrupt, drain, deliver next queued) --> LIVE:IDLE
      |
      | idle_timeout (default ~20m) with no activity
      v
   [REAPING]  shutdown inner, disconnect, publish resume_handle + cc file,
      |       persist cumulative usage; status=idle, host_pid=None
      v
   [COLD/IDLE]  next message re-spawns STARTING with resume  (THE FALLBACK PATH)

Explicit end: any live state -> [ENDING] -> teardown -> status=ended (terminal)
Crash: host_pid dies unclean -> worker session orphan sweep -> in-flight turns failed,
       'session_crashed' breadcrumb, status=idle (resume armed) else ended
```

- **max_live_sessions** (config, default ~4) bounds CC memory. At cap: LRU-reap least-recently
  -active idle session; if none idle, wake stays queued, UI shows "waiting for a free slot".
- **Orphan sweep** on worker start + each tick (sibling to recover_orphaned_runs): active
  sessions with dead host_pid → crash handling; scratch GC for ended sessions;
  --die-with-parent reaps inners.
- pid-reuse false-positives bounded by a host heartbeat on last_activity_at + cmdline check.

### 6. Steering / queueing — sessions own their queue; SteerMessage stays tickets-only
Messages during a streaming turn become queued SessionTurns; the host delivers at the turn
boundary (instant on a resident client). Interrupt = explicit escape (POST /interrupt → host
client.interrupt() → drain → deliver next queued immediately). Do NOT generalize SteerMessage
(inject/at_turn/drain/auto-continue semantics don't map); UI may adapt SteerQueue.tsx into a
SessionQueue, backend tables stay separate.

**Ticket STEER_INJECT for claude:** partly falls out. Reusable free: the streaming runner +
stdin control protocol + translate loop. Not free: ticket path is one-Run-per-turn; needs a
ClaudeExecutor variant that runs _session_runner for exactly one turn with stdin open for the
existing _steer_watcher to inject, closing at ResultMessage. Medium follow-up; not this cut.

### 7. Removal / rework of v1 — rework 0026 in place
No prod DB has 0023-0026 (unmerged lineage) → repurpose `0026_session_kind` → `0026_sessions`
(create sessions + session_turns; NO kind column). Keep 0023/0024/0025. Changes:
- rewrite alembic 0026; models: drop Ticket.kind, add Session/SessionTurn
- revert kind plumbing: domain/tickets.py (create_ticket kind, _ticket_filters, list_inbox),
  domain/query.py (search_tickets/search_runs), domain/analytics.py (five joins),
  worker/run_one.py (webhook/handoff session skips)
- rewrite domain/sessions.py, routes/sessions.py, schemas, tests, and the two UI pages

### 8. API surface (admin bearer/cookie)

| Method / path | Body | Behavior |
|---|---|---|
| POST /api/v1/sessions | {title?, profile_id, source_path?} | create (status=idle, cold); scratch dir if no path. 201 |
| GET /api/v1/sessions | — | list + derived liveness |
| GET /api/v1/sessions/{id} | — | detail + turns (queue strip + history) |
| POST /api/v1/sessions/{id}/messages | {message} | enqueue turn; if cold, wake (+ SIGUSR1 nudge); return turn row immediately. 202 |
| POST /api/v1/sessions/{id}/interrupt | — | interrupt control (≤0.2s). 202; 409 if no turn in flight |
| POST /api/v1/sessions/{id}/end | — | graceful teardown; status=ended (terminal). 200 |
| POST /api/v1/sessions/{id}/wake | — | explicit wake without a message. 202 |
| GET /api/v1/sessions/{id}/transcript | — | SSE; reuses _format_sse + tail loop keyed on Session.transcript_path; tails until not live and nothing queued/streaming |
| DELETE /api/v1/sessions/{id} | — | 204; 409 while live (end first) |

useLiveTranscript + TranscriptScroller consume the SSE unchanged.

### 9. UI
- SessionsPage: liveness badges Live / Idle / Cold ("wakes on send") / Ended.
- SessionChatPage: composer STAYS ENABLED while streaming (queues); queued-message strip
  (SessionQueue, adapted from SteerQueue.tsx — editable/cancelable pre-delivery); interrupt
  button while streaming; "Waking session…" affordance when cold; Promote removed; opencode
  fresh-context banner kept.

### 10. Cost / pricing
Per-turn slicing per §3. Pricing snapshot frozen at session start (Session.pricing_snapshot),
extend-at-end for models actually used (reuse the _extend_and_price_from_snapshot pattern).
Endpoint changes mid-session never rewrite history.

## Failure matrix

| Failure | Detection | Response |
|---|---|---|
| Host crash mid-turn | orphan sweep: host_pid dead | in-flight turn failed, session_crashed breadcrumb, status=idle (resume armed) |
| Worker bounce | detached hosts survive | re-adopt by pid on tick; zero interruption |
| API restart | API holds no subprocess | zero impact; SSE reconnects via Last-Event-ID |
| Inner/sandbox killed | pipe EOF / httpx error | turn failed, clean reap→idle; next message cold-starts |
| SDK hang (no ResultMessage) | max_turn_seconds watchdog | interrupt; if unresponsive teardown → crashed→idle |
| Queue overflow | queued count > cap | 429 on enqueue; UI disables send |
| Idle lingering | last_activity_at > idle_timeout | graceful reap → idle |
| Reap races inbound message | host re-checks inbox before disconnect; API re-wakes if lost | worst case one cold-start; turn row durable, never lost |
| pid reuse false-positive | heartbeat + cmdline check | bounded staleness → crash handling |
| Double spawn | conditional UPDATE ... WHERE host_pid IS NULL claim | loser exits; no split brain |

## Test plan (no real Claude)
- FakeResidentBackend (in-proc): start/send_turn→events/interrupt/close/resume_handle with
  canned events + chosen cumulative usage. Drives host-loop + domain tests.
- Fake _session_runner stub (subprocess-level NDJSON protocol) for pipe-bridge integration.
- Host loop tests: turns → transcript + done; usage slicing across two turns; idle reap →
  idle + resume published; interrupt drains + delivers queued; crash → sweep marks failed;
  wake cold-starts with seeded resume.
- Domain/API tests: CRUD, guards, 404/409/429, SSE typed events + end.
- Migration: 0026_sessions up/down; Ticket.kind gone; no kind filters remain.
- Frontend: badges, queue strip, interrupt, always-on composer, cold→waking.

## File-by-file checklist (sizing)
Backend new: alembic 0026_sessions (S); models Session/SessionTurn + drop kind (M);
domain/sessions.py rewrite (L ~350); worker/_session_runner.py (L ~300, lifted from
_sdk_runner); worker/session_host.py (L ~450); worker/resident_backends.py — ResidentBackend
protocol + ResidentClaude + ResidentOpencode (L ~350); nightdesk-session CLI + console script
(S); worker/session_reaper.py or heartbeat extension (M ~200); worker/main.py supervisor pass +
SIGUSR1 (M); routes/sessions.py rewrite (M ~220); routes/session_transcript.py (S ~90);
schemas (S); app mount (S); config knobs idle_timeout/max_live_sessions/max_queued_turns/
max_turn_seconds + roots (S).
Backend reverts: tickets/query/analytics/run_one kind plumbing (S each).
Frontend: api/sessions.ts + types (M); SessionsPage (S); SessionChatPage (M); SessionQueue (S).
Docs/skills: nightdesk-api + ticket-ops note; supersede interactive-sessions.md (S).
Tests: L overall.

## Risks & open questions
1. SDK single-async-context constraint is absolute — one owner coroutine, comment citing the caveat.
2. receive_response blocks until ResultMessage; interrupt is the only mid-turn escape. Needs a
   live smoke test that interrupt() reliably unblocks receive_response (not stubbable).
3. opencode resume across cold-start needs a per-session (not per-run) data dir preserved under
   the session scratch and XDG_DATA_HOME re-pointed on wake; verify --session resumes cleanly.
4. Resume-generation cost slicing: replay tokens spike the first post-cold turn — surface as
   labeled "resume cost" or fold silently? (product call)
5. SIGUSR1 wake nudge: small and worth shipping; plain-tick fallback (≤5s cold) if too clever.
6. max_live_sessions LRU eviction could reap a session the user returns to — generous idle
   timeout + clear Cold state; cap value needs a user ruling.
7. Ticket STEER_INJECT follow-up is cheap-ish now but explicitly out of scope.
