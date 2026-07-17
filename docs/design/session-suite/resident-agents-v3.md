# Resident Interactive Agents — v3 Design

Supersedes `resident-sessions.md` (v2). Keeps v2's process topology, transport, sandbox model, lifecycle machinery, and cost slicing. Adds the user rulings from `omnigent-session-ui-notes.md` (2026-07-09 amendments): the "Agents" UI surface, first-class needs-input notifications, a five-state visual model, per-agent env with runtime restart, a real composer, full Run-Theater parity on the agent screen, and a claude-only backend seam.

Read `resident-sessions.md` §"The load-bearing insight" and §"Design decisions 1–5" first. This doc amends rather than restates that machinery.

## 0. What changed from v2

| Area | v2 | v3 |
|---|---|---|
| UI surface | "Sessions" page | "Agents" nav item, Agents page, agent screen |
| Blocked-on-human | not modeled (headless bans `AskUserQuestion`/`ExitPlanMode`) | first-class `needs-input` state + `PendingInput` entity + badge + Desk section + inline dialogs |
| Visual state | Live / Idle / Cold / Ended | alive / needs-input / warm / cold / ended |
| Idle timeout | ~20m fixed | global config default 300s, live-evaluated, per-agent override |
| Env | none | `Session.env` JSON, secret values encrypted, apply-and-restart |
| Composer | plain `<Textarea>` | tiptap editor: slash autocomplete, @-mentions, chips, live highlight |
| Agent screen | transcript + queue strip | RunTheater parity (TasksPanel, SubagentsPanel, thinking, tool cards) + interrupt + env panel + terminal handoff |
| Backends | claude + opencode resident | claude only at v3 ship; opencode resident backend later shipped on the kept seam (§10 addendum) |
| Resume cost | open question | folds into first post-cold turn with a "resume" tooltip label (decided) |

The single biggest structural addition is the **needs-input control loop**. v2's runner used the one-shot `query()` and baked `AskUserQuestion`/`ExitPlanMode` into `_HEADLESS_DISALLOWED` (`_sdk_runner.py:382`). v3's resident runner uses `ClaudeSDKClient` with a `can_use_tool` callback, so those tools are no longer banned in the interactive path. They become the mechanism by which the agent asks the human something.

## 1. Naming and navigation

**Decision: keep the `Session` entity name in code, label it "Agents" in the UI.**

The nav item, page title, and empty states read "Agents" and "agent". The backend table stays `sessions` / `session_turns`, the domain module stays `domain/sessions.py`, the CLI stays `nightdesk-session <id>`.

Rationale:
- "Agent" is already overloaded in ways that collide badly with a DB table. The Claude SDK yields subagent `Task` lifecycle events; the transcript renderer has a `subagent` event type and a `SubagentsPanel`; profiles reference agent-shaped config. A top-level `Agent` table sits one word from "sub-agent" everywhere in `lib/transcript.ts` and `_sdk_runner.py`.
- "Session" is the SDK's own noun (`get_server_info().session_id`, `--resume <session_id>`, one jsonl per session). The resume handle, cc-session store, and delta-import all key on a session id.
- The UI label is pure presentation and costs nothing.

Concretely: `navEntries.ts` gets `{ to: "/agents", label: "Agents", icon: Bot }`. Routes are `/agents` and `/agents/$id`. Files live under `frontend/src/routes/agents/`. The rename from `routes/sessions/` is part of the v1-teardown.

## 2. Goals / Non-Goals

**Goals** (v2's carried forward, plus)
- Warm send-to-first-token at model latency (v2).
- Agents fully decoupled from tickets (v2).
- **Needs-input is first-class**: a blocked agent is visible in the sidebar badge, the Desk, and the Agents list without opening it; the dialog renders inline on the agent screen; the answer rides the control channel back into the inner process's `can_use_tool` callback; the pending row survives API and worker restarts.
- **Five live states** surfaced everywhere: alive, needs-input, warm, cold, ended.
- **Per-agent env** editable in the UI, applied by a graceful runtime restart that resumes the same session id.
- **A genuinely good composer**: slash-command autocomplete, @-file mentions, command/skill chips, live highlighting.
- **Agent screen parity** with the ticket Run Theater (tasks, sub-agents, thinking, tool cards) plus interrupt, queue strip, env panel, terminal handoff, wake.
- Trusted posture on the real `~/.claude`; sandboxed posture is a design axis, deferred.

**Non-Goals (this cut)**
- Sandboxed (bwrap) interactive agents. v1 trusted agents run on the owner's real `~/.claude` with no bwrap. The `ResidentBackend` spawn seam takes a posture argument so the sandbox can be added later without reshaping the pipe.
- opencode resident backend (seam only at v3 ship — later implemented, see §10 addendum).
- In-browser terminal / xterm / tmux anywhere. The escape hatch is "open in terminal" printing `claude --resume <id>`.
- Multi-user, collaborative agents, concurrent turns in one agent (queue + interrupt).
- Promote-to-ticket (v2 dropped it; `promote_session` goes away with the v1 teardown).

## 3. Architecture

### 3.1 Process topology (v2, one nuance)

```
outer host  nightdesk-session <id>   (detached like run-ticket; NOT sandboxed in trusted posture)
  owns: Session row, SessionTurn inbox poll, PendingInput writes, transcript file,
        lifecycle, usage/resume persistence, env-merged spawn, control-channel bridge.
    |
    +-- inner  python -m nightdesk.worker._session_runner   (persistent ClaudeSDKClient)
          control lines (NDJSON) in on stdin, transcript+control events out on stdout
```

Nuance vs v2: in the trusted posture the inner is **not** wrapped in bwrap. It is a plain child with `setting_sources=["project","user"]` (loads the real `~/.claude`). `ResidentBackend.start()` takes a `posture` so a future sandboxed posture reuses the same stdin/stdout control protocol; only the argv prefix changes. The host itself is never sandboxed (it writes the DB).

Worker supervision, detachment, orphan re-adoption, and the SIGUSR1 wake nudge are v2 verbatim. SIGUSR1 ships.

### 3.2 The needs-input control loop (the new spine)

The inner runs `ClaudeSDKClient` with a `can_use_tool` callback. When the agent calls `AskUserQuestion`, `ExitPlanMode`, or a permission-gated tool, the SDK invokes the callback and **blocks the turn coroutine** until it returns. The callback cannot reach the DB, so it turns the ask into a control event on stdout and awaits a matching control line on stdin.

Sequence:
1. Human sends a message on a warm agent. Turn streams normally.
2. Agent calls `ExitPlanMode` mid-turn. SDK calls `can_use_tool(tool_name, input, {suggestions})`.
3. Callback allocates `request_id` (uuid), emits `{"type":"pending_input","request_id","kind":"plan_approval","tool":"ExitPlanMode","payload":{...},"options":[...]}` on stdout, then awaits a future parked in `dict[request_id -> future]`. The turn is suspended inside the callback; `receive_response` does not advance.
4. The **host** reads that control line (control events are discriminated by `type` in the `pending_input`/`pending_resolved`/`turn_complete`/`server_info` set and NOT written to the transcript file). Host inserts a `PendingInput` row, bumps `last_activity_at`, writes a `needs_input` breadcrumb to the transcript so the ask shows in context.
5. API surfaces the row: `GET /api/v1/agents/{id}` includes `pending_input`; `GET /api/v1/agents/pending` lists all pending across agents (sidebar badge + Desk). DB reads — survive API restarts.
6. Human answers → `POST /api/v1/agents/{id}/pending/{request_id}` with `{decision, answer?, updated_input?}`.
7. API validates row still `pending`, enqueues a **control SessionTurn** (`kind='answer'`, `body=json(decision)`, `ref_request_id`) — the same inbox as user/interrupt turns, so a cold host still gets it (arms a wake + SIGUSR1).
8. Host claims the answer turn, writes `{"type":"answer","request_id","decision":...}` to inner stdin.
9. Inner resolves the parked future; `can_use_tool` returns; the turn resumes streaming. Host marks the row `answered`, emits `pending_resolved`.
10. Turn completes → usage slice → `SessionTurn.done`.

Why route answers through the SessionTurn inbox: one durable transport, works when the host is cold, keeps the API a pure DB writer. 200ms poll latency is invisible next to human answer time.

Only one `PendingInput` can be pending per agent in practice (the turn is blocked inside the callback). Enforce a partial unique index `(session_id) WHERE status='pending'` so a buggy double-emit cannot create two open asks.

## 4. Data model

`domain/sessions.py` is rewritten from the v1 façade into a real owned-table domain. Three tables.

```python
class Session(Base):                      # UI label: "Agent"
    id, title
    profile_id (nullable FK)              # frozen at start
    project_id (nullable)
    backend: str                          # 'claude' (runtime lock; opencode later)
    model: Optional[str]                  # frozen at start
    status: str                           # 'active'|'idle'|'ended'|'crashed'  (coarse, persisted)
    workspace_kind: str                   # 'directory'
    workspace_access: str                 # 'read_write'
    source_path: str                      # live tree, or per-agent scratch dir
    posture: str                          # 'trusted' (v1). 'sandboxed' reserved.
    host: Optional[str]; host_pid: Optional[int]      # liveness = _pid_alive(host_pid)
    last_activity_at: datetime            # drives idle reap
    idle_timeout_s: Optional[int]         # per-agent override; NULL -> inherit global config
    resume_handle: Optional[dict]         # {"session_id": ...}  (claude)
    env: Optional[dict]                   # {"KEY": {"value": <plain|cipher>, "secret": bool}}
    transcript_path: str
    pricing_snapshot: Optional[dict]      # frozen at start
    cost_usd, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
    created_at, updated_at, ended_at

class SessionTurn(Base):                  # inbox item AND turn record
    id, session_id (FK cascade), position
    kind: str                             # 'user' | 'interrupt' | 'answer'
    body: Text                            # user text; or json(decision) for answer/interrupt
    ref_request_id: Optional[str]         # answer turns -> the PendingInput they resolve
    status: str                           # queued -> delivering -> streaming -> done
                                          # queued->cancelled ; streaming->interrupted/failed
    model_used, input/output/cache tokens, cost_usd     # per-turn slice
    error: Optional[Text]
    created_at, started_at, finished_at

class PendingInput(Base):                 # one open human-input request
    id, session_id (FK cascade)
    request_id: str                       # inner-allocated; unique within session
    kind: str                             # 'permission' | 'ask_question' | 'plan_approval'
    tool: Optional[str]                   # e.g. 'ExitPlanMode', 'Bash'
    payload: dict                         # question+options, plan text, or tool input + suggestions
    status: str                           # 'pending' | 'answered' | 'cancelled'
    answer: Optional[dict]                # what the human chose (audit)
    created_at, answered_at
    # partial unique index: (session_id) WHERE status='pending'
```

**Env storage.** `Session.env` maps `KEY -> {value, secret}`. Secret values encrypted at rest with the existing `ProfileSecretBox` (`domain/profile_secrets.py`). API never returns a secret value (returns `{key, secret:true, set:true}`); the editor is write-only for secrets, mirroring `KeyValueEditor`'s contract. At spawn, host decrypts and **merges env over the user's own environment** (trusted posture) before exec.

**PendingInput persistence rationale:** a real table so it survives API restarts and shows in `GET /agents/pending`. On orphan re-adoption, `pending` rows for a live host stay pending; if the host died, the crash sweep marks them `cancelled` and fails the in-flight turn.

**Migration (rework 0026 in place).** Rename `0026_session_kind.py` → `0026_sessions`, keep `down_revision='0025_execution_target'`, drop the `kind` column work; `create_table` sessions/session_turns/pending_inputs with the partial unique index. Revert v1's kind plumbing: `Ticket.kind` and its filters in `domain/tickets.py` (`create_ticket`, `_ticket_filters`, `list_inbox`), `domain/query.py`, `domain/analytics.py`, `run_one.py` session skips. Single additive revision; nothing downstream shipped.

## 5. State machine

Persisted `Session.status` stays coarse; the five UI states are **derived**.

```
              spawn (worker tick / wake nudge)
                   |
               [STARTING]  claim row (UPDATE ... WHERE host_pid IS NULL),
                   |       decrypt+merge env, seed resume_handle, spawn inner, connect()
                   |       status=active, host_pid=pid; 'session_booting'
                   v
        +----> [LIVE:IDLE] <-------------------------+   warm; poll inbox ~0.2s
        |          | claim queued user turn           |
        |          v                                  | receive_response completes:
        |    [LIVE:STREAMING] ----------------------->+   slice usage, SessionTurn.done
        |          |  can_use_tool fires
        |          v
        |    [LIVE:NEEDS_INPUT]  PendingInput row written; turn coroutine parked
        |          |  answer turn delivered -> future resolved
        |          +--> back to LIVE:STREAMING
        |          | (interrupt while parked: synthetic deny, turn ends)
        |
        | idle_timeout with no activity (effective timeout; default 300s)
        v
     [REAPING]  shutdown inner, disconnect, publish resume_handle,
        |       persist cumulative usage; status=idle, host_pid=None
        v
     [COLD/IDLE]  next message (or answer, or wake) re-spawns STARTING with resume

Explicit end: any live state -> [ENDING] -> teardown -> status=ended (terminal)
Crash: host_pid dies unclean -> orphan sweep -> in-flight turn failed,
       pending inputs cancelled, 'session_crashed', status=idle (resume armed) else ended
```

**Derived UI state** (`domain/sessions.py::describe_liveness`, mirrored in `api/agents.ts`):

| UI state | Derivation |
|---|---|
| `alive` | `_pid_alive(host_pid)` and a turn is `streaming` and no open `PendingInput` |
| `needs-input` | an open `PendingInput` exists (checked first — the human must act) |
| `warm` | pid alive, no streaming turn, no pending input |
| `cold` | `status='idle'` and pid not alive |
| `ended` | `status='ended'` |
| `crashed` | `status='crashed'` — cold variant with a crash breadcrumb |

## 6. Idle timeout and reap

- **Global config, default 300s.** `ConfigRow.session_idle_timeout_s`, editable in Settings, no restart, applies to **every** inheriting agent at once.
- **Per-agent override** `Session.idle_timeout_s` (nullable). Effective = override if set, else global (not min). NULL = inherit.
- **Live evaluation (load-bearing):** effective timeout is read **at each reap check** from the current ConfigRow + Session row, never frozen at spawn. Changing the global takes effect on the next reap pass for every inheriting agent, including already-warm ones. Overridden agents unaffected until the override clears to NULL.
- **`max_live_agents`** (ConfigRow `max_live_sessions`, default 4) with LRU eviction stays. At cap a wake stays queued; UI shows "waiting for a free slot".
- **Needs-input gates the reaper:** an agent with an open `PendingInput` is NOT reaped and NOT LRU-evicted — the turn is genuinely in flight, parked in `can_use_tool`; the human is the blocker and the badge keeps it visible. Reaper predicate: `now - last_activity_at > effective AND no streaming turn AND no open PendingInput`.
- **No turn-level answer timeout.** The turn parks until answered or interrupted. Escape valves: interrupt (synthetic deny, turn ends) or end. "Auto-deny after N minutes" is a future knob.

## 7. Env vars and runtime restart

- **Editor:** per-agent env panel built on `routes/settings/parts/KeyValueEditor.tsx` + per-row secret toggle (masked, write-only).
- **Storage:** §4. `PUT /api/v1/agents/{id}/env` replaces the map; domain encrypts secret values, preserves untouched ciphers (`value:null, secret:true` keeps stored cipher). Env changes alone do NOT restart — they apply on next spawn.
- **Apply-and-restart** (`POST /api/v1/agents/{id}/restart-runtime`): use case = hand the agent a one-time token mid-conversation. API marks restart requested (control turn or `restart_epoch` bump) + wakes host. Host at the next turn boundary (409 if streaming and `!force`; with force, interrupt first) runs the restart handshake: `disconnect()`, publish resume_handle, re-decrypt + re-merge env, respawn inner with `resume=<session_id>`, `connect()`. Same row, same session id (resume doesn't fork), `runtime_restarted` breadcrumb. Replay cost folds into the first post-restart turn (§16). One `_restart_inner()` shared by reaper-wake and explicit restart.

## 8. Composer

### 8.1 Editor library — **tiptap** (decided)
- tiptap (ProseMirror): `@tiptap/extension-mention` + `@tiptap/suggestion` give @-mentions and a second `/` trigger cheaply; chips = mention node views styled to tokens; live highlight = a ProseMirror Decoration plugin; serialize to the plain string the SDK wants. Category leader for structured chat composers; Omnigent's proven choice.
- CodeMirror 6 rejected (inline atomic nodes against the grain); Lexical rejected (thinner ecosystem for this shape).
- Deps: `@tiptap/react`, trimmed `@tiptap/starter-kit`, `@tiptap/extension-mention`, `@tiptap/suggestion`. Code-split to the agents route.

### 8.2 Behavior (`frontend/src/routes/agents/AgentComposer.tsx`)
- **Slash autocomplete:** seed from `get_server_info()` (delivered via `server_info` SSE event) + refresh from `commands_changed`. Picking inserts raw `/name` (custom commands expand headlessly).
- **@-file mentions:** `@` trigger querying `fs/suggest` with new `include_files=true` (fs.py is directory-only today — small change, §12).
- **Chips:** command and skill picks render as styled atoms; skills from `get_server_info().skills`.
- **Highlighting:** decoration plugin colors `/commands`, `@mentions`, `` `code` `` spans live with theme token classes.
- **Keys:** Cmd/Ctrl+Enter sends; Esc closes popover else blurs; Enter in popover accepts.
- **Always enabled while streaming**: sends enqueue turns; queue strip (`AgentQueue.tsx`, adapted from `SteerQueue.tsx`) shows them with edit/reorder/cancel.

## 9. Agent screen parity

`frontend/src/routes/agents/AgentScreen.tsx` subscribes to the agent transcript SSE once at page level (`useLiveTranscript('/api/v1/agents/{id}/transcript')`) and shares events with the panels, as the ticket detail page does. Reused verbatim: `TranscriptScroller`, `TasksPanel` (via `buildTodoList`), `SubagentsPanel` (via `buildSubagentList`, jump-to `nightdesk:focus-subagent` unchanged), `useLiveTranscript` (+ new `KNOWN_EVENT_TYPES`: `pending_input`, `pending_resolved`, `turn_complete`, `server_info`, `needs_input`, `runtime_restarted`, `session_booting`, `session_crashed`).

Screen-only additions: `AgentStatePill` (five-state), interrupt button (alive; as "Cancel request" when needs-input), `PendingInputCard` (permission → allow/deny; ask_question → option chips with previews + Other free-text; plan_approval → plan text + Approve / Keep planning), `AgentEnvPanel` (+ Apply and restart), `AgentQueue`, open-in-terminal handoff (enabled warm/cold, never mid-stream: reap if warm, show `claude --resume <session_id>`; on next wake host delta-imports our own jsonl past the last entry we wrote), wake affordance when cold.

## 10. Backends seam (claude only)

> **Addendum (multi-backend resident sessions ticket):** the "defer entirely"
> call below was v1/v3's assessment. `OpencodeResidentBackend` has since
> shipped in `worker/resident_backends.py` — see that module's docstring for
> how the needs-input gap was actually resolved (short answer: it isn't
> needed, because opencode's config is rendered headless-never-ask at start
> time the same way a ticket run's is, so `_OpencodeResidentHandle` never has
> anything to ask; `answer` control messages are a no-op for it). The
> assessment that opencode's HTTP/SSE process model doesn't fit the
> stdin/stdout `ResidentHandle` shape verbatim was correct — the fix was
> implementing `ResidentHandle` against the HTTP driver directly rather than
> faking a stdio pipe, not reshaping the protocol itself. `Session.backend`
> now derives from the profile's capability code (`claude_sdk` -> `"claude"`,
> `opencode` -> `"opencode"`) instead of being hard-coded; see
> `domain/sessions.py`'s `_RESIDENT_BACKEND_FOR_CAPABILITY`.

`worker/resident_backends.py`:

```python
class ResidentBackend(Protocol):
    async def start(self, spec: StartSpec, posture: str) -> ResidentHandle: ...
    # ResidentHandle: send_turn(text) -> AsyncIterator[event]; interrupt();
    #                 answer(request_id, decision); close(); resume_handle
```

**opencode: originally deferred entirely (assessed honestly).** No `can_use_tool`, no `AskUserQuestion`, no `ExitPlanMode` — the needs-input spine has no counterpart; completion is `session.idle` over HTTP, not a typed ResultMessage. A thin plain-chat slice would ship an agent that silently cannot do the defining v3 feature and needs a parallel host loop. Not "extremely trivial". Seam + frozen `Session.backend` keep it clean to add later. (See the addendum above — this has since shipped.)

`_session_runner.py` lifts from `_sdk_runner.py` with the critical divergences: `ClaudeSDKClient` (persistent) not `query()` (one-shot), and `AskUserQuestion`/`ExitPlanMode` are NOT in the disallowed set — they route through `can_use_tool` → pending/answer. The headless guard still applies to ticket runs; interactive agents are the sanctioned exception, gated by a human.

## 11. API surface (admin bearer/cookie)

All under `/api/v1/agents` (`routes/agents.py` rewrite of `routes/sessions.py`; `routes/agent_transcript.py`).

| Method / path | Body | Behavior |
|---|---|---|
| `POST /api/v1/agents` | `{title?, profile_id, source_path?, env?, idle_timeout_s?}` | create (idle, cold); scratch dir if no path. 201 |
| `GET /api/v1/agents` | — | list + derived liveness + open-pending flag |
| `GET /api/v1/agents/{id}` | — | detail + turns + `pending_input` + env keys (secrets masked) |
| `DELETE /api/v1/agents/{id}` | — | 204; 409 while live |
| `POST /api/v1/agents/{id}/messages` | `{message}` | enqueue user turn; cold → wake+SIGUSR1. 202 |
| `POST /api/v1/agents/{id}/interrupt` | — | interrupt (needs-input → synthetic deny). 202; 409 if nothing in flight |
| `POST /api/v1/agents/{id}/end` | — | teardown; ended. 200 |
| `POST /api/v1/agents/{id}/wake` | — | wake without message. 202 |
| `GET /api/v1/agents/pending` | — | all open PendingInput across agents (badge + Desk) |
| `POST /api/v1/agents/{id}/pending/{request_id}` | `{decision, answer?, updated_input?}` | answer; enqueues answer turn. 202; 409 if answered |
| `PUT /api/v1/agents/{id}/env` | `{env}` | replace map; encrypt secrets; no restart. 200 |
| `POST /api/v1/agents/{id}/restart-runtime` | `{force?}` | graceful inner restart, same session id. 202; 409 if streaming and !force |
| `GET/POST /api/v1/agents/{id}/turns/...` | reorder/edit/cancel | queue-strip ops |
| `GET /api/v1/agents/{id}/transcript` | — | SSE; `_format_sse` + tail keyed on transcript_path; tails until not live and nothing queued/streaming/pending |

## 12. SSE events

Reuses `_format_sse` + Last-Event-ID watermark verbatim; tail predicate = agent liveness. Transcript events (persisted + rendered): existing set + `needs_input`, `runtime_restarted`, `session_booting`, `session_crashed`. Control events (streamed, NOT persisted; renderer ignores, page reads): `pending_input`, `pending_resolved`, `turn_complete`, `server_info` (carries slash_commands/skills). Durable truth for pending state is the `PendingInput` table via `GET /agents/{id}` — SSE is an optimization; reconnecting clients re-read rows. If interleaving proves messy, split control to `/agents/{id}/events` — table-as-truth makes that mechanical.

**fs endpoint:** `fs/suggest` is directory-only (`fs.py:71` `entry.is_dir()`); add `include_files: bool = False` param. Small change, blocks @-mentions otherwise.

## 13. Frontend components (real files)

New under `frontend/src/routes/agents/`: `AgentsPage.tsx` (list/create/delete, five-state rows, per-row pending badge), `AgentScreen.tsx`, `AgentComposer.tsx` (tiptap), `AgentQueue.tsx`, `PendingInputCard.tsx`, `AgentEnvPanel.tsx`, `AgentStatePill.tsx` (or extend `ui/StatusPill.tsx`).

Reused unchanged: `TranscriptScroller`, `TasksPanel`, `SubagentsPanel`, `lib/transcript.ts` (+event types), `ui/*`, `KeyValueEditor`.

Shell edits: `navEntries.ts` ("Sessions"→"Agents", `/agents`, icon Bot) + optional `badge?: number` on `NavEntry`; `SideNav.tsx`/`NavDrawer.tsx` render the badge from a shared `usePendingAgents()` hook (polls `GET /agents/pending`); `DeskPage.tsx` gains an "Agents waiting on you" band above "Running now" from the same hook; `router.tsx` routes; `api/agents.ts` + `types.ts` (rewrite of `api/sessions.ts`): `useAgents/useAgent/usePostMessage/useInterrupt/useAnswerPending/usePendingAgents/usePutEnv/useRestartRuntime/useWake/useEndAgent`; Settings global session block (`session_idle_timeout_s`, `max_live_sessions`, `max_queued_turns`, `max_turn_seconds`); delete `routes/sessions/*`, `api/sessions.ts`.

## 14. Config knobs

Mutable (ConfigRow, Settings, no restart): `session_idle_timeout_s` (300, global, live-evaluated), `max_live_sessions` (4), `max_queued_turns`, `max_turn_seconds`. Static (`config.py`): scratch root `worktree_root.parent / "nightdesk-sessions"`; cc-session store root for the future sandboxed posture; trusted posture reads the real `~/.claude/projects/<slug>` for resume + delta-import. Per-agent: `Session.idle_timeout_s` nullable override.

## 15. Failure matrix (additions to v2)

| Failure | Detection | Response |
|---|---|---|
| Human never answers | — | turn parked; agent held warm (not reaped); badge/Desk keep visible; interrupt or end to escape |
| Interrupt while needs-input | interrupt control with open PendingInput | synthetic deny; turn unblocks and ends; row → cancelled |
| Restart while streaming | host at turn boundary | 409 if !force; force → interrupt then restart |
| Env secret unreadable (bearer rotated) | SecretBox decrypt raises | spawn fails cleanly "re-enter env secrets"; agent stays cold |
| Host crash with open pending | orphan sweep | turn failed, pending rows cancelled, crashed→idle (resume armed) |
| Double pending emit | partial unique index | second insert rejected; host logs, keeps first |
| Answer races reap | answer is durable turn; reap gated on no-open-pending | reap can't fire while pending; answer arms wake if cold |
| Global timeout lowered mid-life | reaper re-reads config each pass | inheriting warm agents reaped next pass; overrides unaffected |
| Terminal handoff two-writer | host reaped before handoff; delta-import on wake | single writer; import own jsonl delta then resume |
| commands_changed missed | `server_info` re-sent on reconnect | composer list self-heals |

Plus v2's full matrix.

## 16. Cost

v2 per-turn slicing + session pricing snapshot stay. **Decided:** resume/restart replay cost folds into the first post-cold/post-restart turn's slice with a small "includes resume" tooltip label. Not a separate line, not silent. `Session total = Σ turn slices` stays exact.

## 17. Test plan (no real Claude)

- **FakeResidentBackend** (in-proc) incl. scripted `can_use_tool` that emits `pending_input` and blocks until `answer`.
- **Fake `_session_runner` stub** (subprocess NDJSON) incl. the pending/answer round trip.
- **Host loop:** turns→transcript+done; slicing across turns; reap→idle+resume; needs-input round trip (row written, turn parked, answer delivered, resumed, row answered); reap-gating on open pending; interrupt-while-parked → deny → turn ends; crash → turn failed + pending cancelled; restart-runtime → same session id, env re-merged, replay cost on first turn.
- **Idle-timeout inheritance:** global change reaps already-warm inheriting agent next pass; override survives global change; clearing override resumes inheritance.
- **Domain/API:** CRUD; pending aggregation; answer 409; env PUT masks/preserves/encrypts; restart 409; SSE typed events.
- **Migration:** 0026_sessions up/down; three tables + partial index; `Ticket.kind` gone; no kind filters remain.
- **Frontend:** five-state pills; badge; Desk band; PendingInputCard variants; composer autocomplete (server_info + commands_changed); @-mentions with files; queue strip; env panel; terminal handoff; Settings timeout edit.
- **Live smoke (not stubbable, run FIRST):** `interrupt()` unblocks `receive_response`; `ExitPlanMode` routes through `can_use_tool` and the decision resumes the turn; custom `/command` expands in the resident client.

## 18. File-by-file checklist (sizing)

**Backend new:** `alembic/versions/0026_sessions.py` rework (S); `db/models.py` Session/SessionTurn/PendingInput + drop Ticket.kind (M); `domain/sessions.py` rewrite incl. describe_liveness, env encrypt/merge, pending answer, restart (L ~400); `worker/_session_runner.py` (L ~340); `worker/session_host.py` (L ~480); `worker/resident_backends.py` (M ~280); `worker/session_reaper.py` (M ~220); `worker/main.py` supervisor + SIGUSR1 (M); `nightdesk-session` CLI (S); `api/routes/agents.py` (M ~260); `api/routes/agent_transcript.py` (S ~110); `api/routes/fs.py` include_files (XS); `api/schemas.py` (S); `config.py` + ConfigRow (S); app mount (S).

**Backend reverts (v1 kind teardown):** `domain/tickets.py`, `domain/query.py`, `domain/analytics.py`, `worker/run_one.py` (S each).

**Frontend:** `api/agents.ts` + types (M); `AgentsPage` (S), `AgentScreen` (M), `AgentComposer` (L — tiptap), `AgentQueue` (S), `PendingInputCard` (M), `AgentEnvPanel` (S), `AgentStatePill` (XS); `lib/transcript.ts` (XS); navEntries + SideNav/NavDrawer badge (S); DeskPage band (S); Settings block (S); router (XS); package.json tiptap (XS); delete `routes/sessions/*`, `api/sessions.ts` (XS).

**Docs/skills:** update `nightdesk-api` (agents endpoints), note in `nightdesk-ticket-ops`, supersede `resident-sessions.md`/`interactive-sessions.md` (S). Tests: L overall.

## 19. Risks and open questions

1. **`can_use_tool` blocking semantics** are the load-bearing assumption: the callback must suspend the turn without deadlocking `receive_response`, and `interrupt()` must unblock a callback-parked turn. Live smoke tests run FIRST. If interrupt does not unblock a parked callback, the synthetic-deny escape returns from the callback directly (the runner controls it) — de-riskable, but verify.
2. **tiptap bundle weight:** trim starter-kit; code-split to the agents route.
3. **Env secret rotation:** shares the bearer-derived Fernet key; rotating the bearer invalidates them like profile secrets. Documented tradeoff.
4. **Trusted posture on real `~/.claude`:** the user's real skills/hooks/CLAUDE.md run in the agent. Surface in create-dialog copy. Sandboxed posture is the deferred mitigation.
5. **needs-input agents pin live slots** (excluded from LRU): bounded by badge visibility + explicit end; add "auto-deny after N minutes" if it bites.
6. **Control events on the transcript SSE:** table-as-truth makes splitting to a second SSE mechanical if interleaving is messy.
7. **opencode** stays a real gap (no needs-input equivalent); frozen `Session.backend` keeps future mixing clean.
