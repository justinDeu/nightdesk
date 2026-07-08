# Interactive Sessions — Design Doc

Studied read-only against `base/session-suite`.

## Patterns & Conventions Found

- **A "turn" is a `Run`; a "thread" is a `Conversation`.** `Conversation` owns the authoritative resume handle (`Conversation.session_id`) and one transcript file (`transcript_path`); `Run` rows are turns ordered by `position` within it (`db/models.py:276-398`). Continue = resume the conversation's `session_id` on the same worktree. This is already a chat loop.
- **The whole run pipeline keys off `Ticket`.** `worker/run_one.py` starts with `ticket = session.get(Ticket, ticket_id)`; `RunToken.ticket_id` and `TicketWorkspace.ticket_id` are `NOT NULL`; the scheduler picks `Ticket` rows; transcript SSE resolves by ticket id.
- **Immediate dispatch already exists.** `request_run_now` → `queued` + `run_now=True`; `scheduler.pick_eligible` picks `status='queued' AND run_now=True` **unconditionally**, ignoring window and capacity (`scheduler.py:100-111`). Continue rides this path (`tickets.py:826`).
- **The chat turn loop already exists.** `continue_ticket` (`tickets.py:791-826`): stage `intent=continue` + conversation id → `request_run_now` → worker seeds `resume=<session_id>` and appends the typed message via `build_continue_prompt` (`run_one.py:1183-1193`). Terminal state after any run is `running → review` (`run_one.py:1369-1371`), and `review` is a valid `continue` source. **A back-and-forth chat is `draft → running → review → running → review …` with zero new machinery.**
- **Terminal transition is centralized** at `run_one.py:1368-1371`; webhook + dependents handoff hang off it (`:1377-1401`).
- **Ticket list surfaces funnel through two chokepoints:** `domain/tickets.py::_ticket_filters` (feeds `list_tickets`/`count_tickets` → board columns, Tickets list, Archive, Desk bands via `useTickets({status})` in `DeskPage.tsx:112-113`) and `domain/query.py::search_tickets` (global search + saved views + board search). Inbox is its own `list_inbox`. Analytics joins `Run → Ticket` at five sites (`analytics.py:192,332,390,633,723,778`).
- **Frontend:** TanStack Router, routes registered in `frontend/src/router.tsx` under the pathless `app` layout; nav is data-driven from `components/navEntries.ts` (shared by `SideNav` + `NavDrawer`). Streaming: `TranscriptScroller` + `useLiveTranscript(url)` (`TicketDetailPage.tsx:55,144`) and `LiveTranscript` (`RunTheater.tsx:135`) consume `/api/v1/tickets/{id}/transcript` and `/api/v1/runs/{rid}/transcript`.
- **Migration head is `0022_providers_and_endpoints`**; additive-column migrations follow the `0020` shape.

## Goals / Non-Goals

**Goals**
- Ad-hoc chat-first agent session against a profile and optional workspace, no ticket authoring.
- Turn-by-turn chat with live streaming transcript; each message dispatches immediately (bypasses the window).
- Works on `claude_sdk` and `opencode` via the existing conversation-resume path.
- Promote a session to a real ticket, keeping conversation + run history.
- Sessions stay out of board, inbox, analytics-by-default, search, saved views.

**Non-Goals (v1)**
- Sub-second dispatch (turns start within the worker tick, ~5s; called out below).
- Multiple concurrent turns in one session (message while running → 409; composer disabled).
- git-worktree/stacking, dependencies, cron, webhooks for sessions.
- Multi-user presence / collaborative sessions.

## Architecture Decision

**Chosen: a session IS a `Ticket` with `kind='session'`.** One column `tickets.kind` (`'ticket'` default, `'session'`), migration `0026_session_kind`. Sessions reuse the entire run pipeline, conversation model, transcript streaming, run tokens, workspaces, run-now dispatch, and continue/resume semantics **unchanged**. A `/api/v1/sessions/*` façade + a Sessions UI page are the front door; the only backend cost is excluding `kind='session'` from ticket surfaces.

Lifecycle maps exactly onto existing statuses:

| Chat action | Mechanism (existing) | Status path |
|---|---|---|
| First message | set `prompt`, `request_run_now` (intent `first_run`) | `draft → queued → running → review` |
| Reply | `continue_ticket` (resume + append) | `review → queued → running → review` |
| Resting between turns | — | `review` (idle, continueable) |
| Thinking | — | `running` (transcript streaming) |

**Rejected: separate `Session` entity (+ nullable `Conversation.ticket_id`).** Would force `RunToken.ticket_id`, `TicketWorkspace.ticket_id`, `Run.ticket_id` nullable/polymorphic and make `run_one`/scheduler/transcript-SSE branch on owner type everywhere — a second copy of the 750-line `run_one` or an owner abstraction threaded through every ticket assumption. Maximum blast radius for a cosmetic distinction. The "conversations gain owner polymorphism" variant is worse (splits transcript/resume plumbing too).

## Data Model

`db/models.py` — add to `Ticket`:
```python
# 'ticket' (board work) | 'session' (ad-hoc chat, hidden from board/inbox/
# analytics/search). Sessions reuse the entire run pipeline; only the ticket
# list surfaces filter them out.
kind: Mapped[str] = mapped_column(String, default="ticket", nullable=False, index=True)
```

**Migration `0026_session_kind`** (`down_revision = "0022_providers_and_endpoints"`; linearized at integration): `add_column` with `server_default="ticket"` (backfills existing rows) + index; downgrade drops both.

**Exclusion points (the precise blast radius):**

| Site | Change |
|---|---|
| `domain/tickets.py::_ticket_filters` (`:345`) | Append `Ticket.kind == "ticket"` unless a `kind` arg overrides. Covers board, Tickets list, Archive, Desk bands. **Single most important filter.** |
| `domain/tickets.py::list_inbox` (`:683`) | kind guard (defensive). |
| `domain/query.py::search_tickets` (`:581`) | kind filter → search, saved views, board search. |
| `domain/query.py::search_runs` (`:601`) | same join filter. |
| `domain/analytics.py` joins (`:192,332,390,633,723,778`) | `Ticket.kind == "ticket"` on each `join(Ticket, …)`. |
| `worker/scheduler.py::pick_eligible` | **No change.** Sessions are `queued+run_now`, picked unconditionally. |

## Component Design

### Domain: `domain/sessions.py` (new, thin façade over `domain/tickets.py`)
```python
def create_session(session, *, title, profile_id, workspace=None, project_id=None) -> Ticket
    # kind='session' ticket in 'draft'. workspace None -> per-session scratch dir
    # as primary directory workspace; else directory (in_place) on source_path.
def post_session_turn(session, session_id, message) -> Ticket
    # no active conversation -> prompt=message + request_run_now (first_run)
    # active conversation    -> continue_ticket(message)
    # SessionBusy (409) if status in ('queued','running')
def promote_session(session, session_id, *, title, prompt=None, target_status='review') -> Ticket
    # kind -> 'ticket'; keep conversations/runs/workspaces; land in review (history visible) or draft
def list_sessions(session, *, limit=100) -> list[Ticket]   # kind='session', updated_at desc
def archive_session(session, session_id) -> Ticket          # review/draft -> archived
```
Scratch path: `worktree_root.parent / "nightdesk-sessions" / <ticket_id>` (sibling of worktrees so `sandbox.py` can bind-mount it), created as `directory` / `read_write` `TicketWorkspace`.

### API: `api/routes/sessions.py` (new router, mounted in `api/app.py`)
| Method / path | Body | Delegates to | Notes |
|---|---|---|---|
| `POST /api/v1/sessions` | `SessionCreate{title?, profile_id, source_path?, access?}` | `create_session` | 201 |
| `GET /api/v1/sessions` | — | `list_sessions` | |
| `GET /api/v1/sessions/{id}` | — | `get_ticket` + `list_conversations` | |
| `POST /api/v1/sessions/{id}/messages` | `{message}` | `post_session_turn` | 409 on busy/non-resumable |
| `POST /api/v1/sessions/{id}/promote` | `{title, prompt?, target_status}` | `promote_session` | returns `TicketOut` |
| `POST /api/v1/sessions/{id}/archive` | — | `archive_session` | |
| `DELETE /api/v1/sessions/{id}` | — | `delete_ticket` | 204; blocked while running |

**Transcript/runs SSE: reuse as-is** — `/api/v1/tickets/{id}/transcript` resolves by id regardless of `kind`. Zero SSE code added.

Schemas: `SessionCreate`, `SessionMessage`, `SessionPromote`, `SessionOut` (TicketOut shape + `conversations`).

### Worker: `run_one.py` — two small guards only
- `run_one.py:1377-1401`: skip `_maybe_fire_webhook` and `_handoff_to_dependents` when `ticket.kind == "session"`.
- Everything else reused unchanged. `review` terminus = session idle state. `_workspace_specs_for_ticket` already handles the directory workspace.

## Data Flow (one chat turn)

1. Composer on `/sessions/$id` → `POST /api/v1/sessions/{id}/messages {message}`.
2. `post_session_turn` → `continue_ticket(message)` → stage `intent=continue`, `request_run_now` → `review → queued`, `run_now=True`.
3. Next worker tick (≤ tick_seconds) → picked unconditionally → `running`.
4. `run_one` resumes `conversation.session_id`, appends message via `build_continue_prompt`, streams into the shared transcript.
5. `useLiveTranscript('/api/v1/tickets/{id}/transcript')` tails the SSE → `TranscriptScroller` renders live.
6. Turn ends → `running → review`. Composer re-enables. Repeat.

## Interactivity latency

Worker sleeps `settings.tick_seconds` between ticks (`main.py:290-295`, default 5.0; production reads `ConfigRow.polling_interval_seconds`). A turn starts within ~5s of send. **Acceptable for v1 — show a "dispatching…" affordance.** Optional later fast-path: API sets an event/sentinel the worker's `wait_for(shutdown_event.wait(), timeout=tick)` races on. Keep polling for v1.

## Capability Notes

- **claude_sdk:** full resume via seeded `cc_sessions_dir` + `resume=<session_id>` (`run_one.py:1104-1153`). Native chat.
- **opencode:** no seeded-jsonl store → `continue` falls back to fresh-context resume on the same workspace (`run_one.py:1104-1113`), recorded on the transcript. Works turn-to-turn; each turn re-reads the workspace rather than replaying chat memory. Surface in UI when the profile backend is opencode.

## Edge Cases

- **Message while running/queued:** `SessionBusy` → 409; composer disabled. (v1 rejects; queueing is a later enhancement — or steering integration.)
- **Workspace dirty (in_place session):** agent works in-place on the live tree — intended. Create dialog shows the resolved path.
- **First message on empty session:** `first_run` path, not `continue`.
- **Non-resumable conversation** (first turn crashed pre-session_id): message route retries as fresh first-run-style turn (new conversation) instead of 409-ing.
- **Promote while running:** blocked (must be review/draft); UI hides Promote mid-turn.
- **Delete/cancel:** `delete_ticket` refuses while running (existing guard); cancel via existing watcher/SIGTERM path.
- **Session on the board:** guarded by the kind filters; regression test asserts a session never shows in any ticket list.

## Test Plan

- Migration: upgrade populated DB, backfill, round-trip.
- **Exclusion (load-bearing):** session + normal ticket in each status; assert `list_tickets`, `count_tickets`, `list_inbox`, `search_tickets`, `search_runs`, and each analytics aggregate return only the normal ticket.
- Turn loop (in-proc worker + Dummy executor): first message → `first_run`, one conversation, `review`; second → `continue` intent, same conversation, resume seeded; `SessionBusy` mid-run.
- Immediate dispatch with a closed window.
- Promote: kind flips, history preserved, visible on board.
- Worker guards: webhook + dependents skipped.
- API happy paths + 404/409; transcript SSE streams for a session id.
- Frontend: list renders; chat streams and re-enables composer; session never appears on board.

## File-by-File Implementation Checklist

**Backend**
- [ ] `src/nightdesk/db/models.py` — `Ticket.kind` (default 'ticket', indexed)
- [ ] `alembic/versions/0026_session_kind.py`
- [ ] `src/nightdesk/domain/tickets.py` — `create_ticket(kind=...)`; `_ticket_filters` kind arg; `list_inbox` guard
- [ ] `src/nightdesk/domain/sessions.py` — new façade + `SessionBusy`
- [ ] `src/nightdesk/domain/query.py` — kind filter in `search_tickets` + `search_runs`
- [ ] `src/nightdesk/domain/analytics.py` — kind filter on the five joins
- [ ] `src/nightdesk/worker/run_one.py` — skip webhook + dependents for sessions
- [ ] `src/nightdesk/api/schemas.py` — session schemas
- [ ] `src/nightdesk/api/routes/sessions.py` — new router
- [ ] `src/nightdesk/api/app.py` — mount router

**Frontend**
- [ ] `frontend/src/api/sessions.ts` — new client + hooks + query keys
- [ ] `frontend/src/api/types.ts` — session types
- [ ] `frontend/src/routes/sessions/SessionsPage.tsx` — list + "New session" (profile picker, optional `PathInput`)
- [ ] `frontend/src/routes/sessions/SessionChatPage.tsx` — `TranscriptScroller` + `useLiveTranscript` above, `Textarea` composer below (Cmd/Ctrl+Enter, disabled while running); Promote + Archive
- [ ] `frontend/src/components/navEntries.ts` — Sessions nav entry
- [ ] `frontend/src/router.tsx` — `/sessions` + `/sessions/$id`

**Docs / skills**
- [ ] `nightdesk-api` + `nightdesk-ticket-ops` skills — document `/api/v1/sessions/*` and `kind` (same-change rule)

**Reused unchanged:** `api/routes/transcript.py`, `domain/conversations.py`, `worker/scheduler.py`, `worker/main.py`, `domain/run_tokens.py`, `worker/sandbox.py`, `worker/workspace.py`, transcript React components.

**Load-bearing insight:** because a chat turn is already `continue_ticket` → `run_now` → `run_one` → `review`, sessions add **one column, one façade module, one router, one nav page, and a handful of `kind` filters** — and inherit resume, streaming, run tokens, pricing, cancellation, and sandboxing for free.
