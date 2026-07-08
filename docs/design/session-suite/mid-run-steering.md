# Mid-run steering — design

Read-only study of `base/session-suite`.

## TL;DR of the key architectural findings

- A "turn" in nightdesk == one `Run` == one `Backend.execute()` call. There is **no between-turn pause inside a live run today**; `next_run_context` is consumed only when a *new* Run is staged (`continue`/`resume`/`retry` intents in `domain/tickets.py`), which happens after the current run finishes and the ticket sits in `review`.
- The executor runs on the **host** (the `nightdesk-run-ticket` subprocess = `run_one`), not inside the sandbox. `run_one` already owns a `session_factory` and already runs a host-side DB poll loop: `_cancel_watcher` (`run_one.py:698`) polls the ticket every 0.5s and flips `cancel_event`. **This is the delivery mechanism we reuse** — a sibling `_steer_watcher` polling `SteerMessage` rows and pushing them onto an `asyncio.Queue` threaded through `ExecutionRequest`, exactly parallel to how cancel is delivered via `asyncio.Event`.
- **opencode** (`backends/opencode_driver.py`) drives a persistent localhost HTTP session and finishes on `session.idle`. Posting another `/session/{id}/prompt_async` before idle keeps the same session/run alive → **true same-run injection** at step-boundary granularity.
- **claude_sdk** (`worker/claude_executor.py` + `worker/_sdk_runner.py`) uses the SDK's one-shot `query()` and **closes stdin right after writing the runner spec** (`claude_executor.py:95`). There is no live input channel into the running turn. Mid-turn injection is *possible* only by reworking `_sdk_runner` to `ClaudeSDKClient` streaming-input mode + a stdin control channel — a substantial, separate change. **v1 ships claude_sdk as queue-only** (delivered by auto-continuing into a new Run).

This yields a two-capability model: **STEER_QUEUE** (mandatory floor, every backend) and **STEER_INJECT** (capability-gated, opencode in v1).

## Goals / Non-goals

**Goals**
- While a run is live, the user types follow-up messages held in a **visible, editable, reorderable, deletable queue** on the ticket detail page.
- Messages are **auto-delivered at the next safe point**. Where the backend supports it (opencode), they're injected into the *same* live run; where it doesn't (claude_sdk), they're guaranteed to drive the *next* turn automatically without a manual "Continue" click.
- Delivery is **visible in the transcript** (a `steer_delivered` event) and the queue reflects `pending → delivering → delivered`.
- Generalize `next_run_context` without breaking it: the existing "Guidance for next run" chip and `/continue` flow keep working unchanged.

**Non-goals**
- True token-level interrupt of an in-flight generation (opencode injects at the next step boundary, not mid-token; claude injects at run end). A future `interrupt` delivery mode is sketched but not built.
- Reworking `_sdk_runner` to streaming-input mode (deferred follow-up: STEER_INJECT for claude).
- Multi-user concurrent editing / OT on the queue. Last-write-wins with optimistic refresh.

## Capability model

Add to `domain/backend_capabilities.py` `Capability` enum:

```python
STEER_QUEUE = "steer_queue"    # accepts a queued follow-up delivered at the next turn boundary
STEER_INJECT = "steer_inject"  # delivers a queued follow-up into the SAME live run without a new Run
```

- `STEER_QUEUE` is granted to **every** backend.
- `STEER_INJECT` is granted to **OPENCODE only** in v1. `CLAUDE_SDK` currently declares `frozenset(Capability)` (every capability); **change that to an explicit set that excludes `STEER_INJECT`** so the capability actually gates. This is the one place the "claude has every capability" shortcut must break — call it out in review.

Degradation contract:
- `STEER_INJECT` present → queued messages flush into the current run; UI label: *"Delivered to the running agent."*
- `STEER_INJECT` absent, `STEER_QUEUE` present → messages held; when the run finishes they **auto-drive a `continue` turn**. UI label: *"Will be sent as the next turn when this run finishes."*

The backend string is frozen on the `Conversation` (`models.py:307`), so capability is resolved from `conversation.backend`, not the (mutable) profile.

## Data model

```python
class SteerMessage(Base):
    __tablename__ = "steer_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # Belongs to a conversation (the live unit of work), NOT a specific Run:
    # authored during run N, delivered into run N (inject) or N+1 (queue-only).
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # pending -> delivering -> delivered ; or pending -> cancelled
    state: Mapped[str] = mapped_column(String, default="pending", nullable=False, index=True)
    # "at_turn" (default) or "inject"; inject downgrades to at_turn without STEER_INJECT.
    delivery_mode: Mapped[str] = mapped_column(String, default="at_turn", nullable=False)
    delivered_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    conversation: Mapped["Conversation"] = relationship()
```

State machine: `pending → delivering → delivered`, and `pending → cancelled`. `delivering` is a short-lived claim state; a `delivering` row on a finished/orphaned run is reset to `pending` by orphan recovery.

Why conversation-scoped, not run-scoped: a message typed during run N must survive run N ending and drive run N+1 on a queue-only backend. `Conversation` is the same anchor the transcript file, session_id, and `latest_turn()` already use.

### Migration `0023_steer_messages`

`revision = "0023_steer_messages"`, `down_revision = "0022_providers_and_endpoints"` (confirmed at `0022_providers_and_endpoints.py:49`). Follow the repo's guard-based style (inspector check before `create_table`), indexes on `conversation_id`, `ticket_id`, `state`. Downgrade drops the table. Linearized against sibling feature migrations at master merge.

## Domain layer

New module `src/nightdesk/domain/steering.py`:

```python
def add_steer_message(session, *, conversation_id, ticket_id, body, delivery_mode="at_turn") -> SteerMessage
def list_steer_messages(session, conversation_id, *, states=("pending","delivering")) -> list[SteerMessage]
def edit_steer_message(session, message_id, *, body) -> SteerMessage      # pending only
def reorder_steer_messages(session, conversation_id, ordered_ids) -> list[SteerMessage]  # pending only
def cancel_steer_message(session, message_id) -> SteerMessage             # pending -> cancelled
def claim_next_steer_message(session, conversation_id) -> Optional[SteerMessage]  # atomic claim
def mark_delivered(session, message_id, *, run_id) -> None
def drain_pending_to_context(session, ticket_id, conversation_id) -> Optional[str]
    # fold remaining pending bodies into next_run_context (queue-only fallback), cancel the rows
```

Guards: edit/reorder/cancel refuse anything not `pending`. `claim_next_steer_message` uses ordered SELECT + state-conditional UPDATE to prevent double-claim.

### Relationship to `next_run_context` (do not break it)

- `next_run_context` stays exactly as-is; all `/continue`, `/resume`, `/retry`, `/restart`, `/new-conversation` flows unchanged.
- SteerMessage is the **live-run** queue; `next_run_context` is the **at-rest** staged note. They converge at exactly one point: run finishes with pending steer messages on a queue-only backend → `drain_pending_to_context` appends bodies into `next_run_context`, then the completion path auto-issues a `continue`. Reuses the existing, tested `continue_ticket` machinery.

## Backend interface changes

Add to `ExecutionRequest` (`worker/executor.py`), parallel to `cancel_event`:

```python
steer_queue: Optional[asyncio.Queue] = None      # host watcher pushes {"id","body"}
on_steer_delivered: Optional[Callable[[str, dict], None]] = None
```

**Critical correctness point:** transcript `seq` is owned by the executor's counter (`claude_executor.py:114`, opencode's seq list at `opencode_driver.py:90`). The `steer_delivered` breadcrumb MUST be emitted inside the executor's own `_emit`, never via out-of-band `transcript.append_event` (which rescans the file and would collide with the executor's next write). The `on_steer_delivered` callback only touches the DB row.

No `Backend` ABC signature changes; inject-capable backends read `req.steer_queue`, others ignore it.

## Worker changes (`worker/run_one.py`)

1. Build `steer_queue` alongside `cancel_event` (near `run_one.py:769`); pass into `ExecutionRequest` (built at `run_one.py:1210`). Wire only when `backend.provides(Capability.STEER_INJECT)`.
2. **`_steer_watcher`** — host coroutine, sibling to `_cancel_watcher` (`run_one.py:1231`): polls pending SteerMessages ~0.5s, claims (`pending→delivering`), pushes onto `steer_queue`. Spawned only for inject-capable runs.
3. **On-delivered callback** — closure over `session_factory`; calls `mark_delivered`; hands the executor `{"type":"steer_delivered","message_id":...,"text":body}` to stamp seq/ts and write.
4. **Run-completion drain (fallback)** — after `finish_run` (~`run_one.py:1292`), before the `review` transition (`run_one.py:1369`): if pending messages remain, `drain_pending_to_context`, then if the conversation has a resumable session, stage `continue` (`_stage_next_run(intent="continue", ...)` + `request_run_now`) instead of transitioning to review, so the worker immediately picks the ticket up with the drained context as `continue_message`. Non-resumable → fall through to `review` with `next_run_context` populated (visible in the "Guidance staged" chip).
5. **Orphan/crash recovery** — extend `heartbeat.recover_orphaned_runs` (`main.py:199` sweep): `delivering` rows with null `delivered_run_id` on finished/orphaned runs reset to `pending`.

### Per-backend delivery specifics

- **opencode** (`opencode_driver.py`, `_consume_events` at :198): race `steer_queue.get()` into the existing `asyncio.wait` set. On steer item: POST body via new `_post_followup` to `/session/{id}/prompt_async`, then `on_steer_delivered` + emit event. **Do not finish on the first `session.idle`** if a steer message is in flight or queued: on idle, `get_nowait()` → if present, post and keep looping; else break.
- **claude_sdk**: `steer_queue=None` for v1; delivery entirely via run-end drain + auto-continue. Document in `_sdk_runner.py` docstring that STEER_INJECT requires `ClaudeSDKClient` streaming input (follow-up).

## API surface (`api/routes/tickets.py` + `api/schemas.py`)

Sub-resource on the ticket's **active conversation** (optional `conversation_id` accepted):

| Method | Path | Body | Response | Notes |
|---|---|---|---|---|
| POST | `/api/v1/tickets/{tid}/steer` | `{body, delivery_mode?}` | `SteerMessageOut` 201 | 409 if not `running` |
| GET | `/api/v1/tickets/{tid}/steer` | — | `{messages, capability:{inject}}` | pending+delivering, ordered |
| PATCH | `/api/v1/tickets/{tid}/steer/{mid}` | `{body}` | `SteerMessageOut` | 409 if not `pending` |
| POST | `/api/v1/tickets/{tid}/steer/reorder` | `{ordered_ids}` | `{messages}` | 409 if any not `pending` |
| DELETE | `/api/v1/tickets/{tid}/steer/{mid}` | — | 204 | `pending → cancelled` |

`SteerMessageOut`: id, body, position, state, delivery_mode, delivered_run_id, created_at, delivered_at. Update `nightdesk-api` + `nightdesk-ticket-ops` skills in the same change (schema-drift rule) with a "steer a live run" recipe.

### SSE / transcript eventing

1. Transcript event `steer_delivered` flows over existing SSE (`routes/transcript.py`, `_format_sse` at :26 emits any typed event). Frontend: add to `KNOWN_EVENT_TYPES` (`lib/transcript.ts:28`), render a "you sent: …" divider in `TranscriptView.tsx`. Shape: `{"type":"steer_delivered","message_id":str,"text":str,"delivery":"inject"|"at_turn"}`.
2. Queue-state updates: no bespoke SSE channel. When a `steer_delivered` arrives on the transcript SSE, invalidate the steer query (plus refetch on submit). One SSE connection, minimal surface.

## Frontend (`frontend/src`)

- **New `detail/SteerQueue.tsx`** — visible strip in/above the composer: draggable chips (body line-clamped, state badge, inline edit, delete). Delivered chips fade out (breadcrumb appears in transcript). Use `[data-tooltip]` primitive + real hover affordances; reuse the existing dnd approach.
- **`ActivityComposer.tsx`** — add a third mode that only appears while `running`: "Steer this run" → POST `/steer`. Helper text per capability: inject → "Sends to the running agent at its next step."; queue-only → "Queued — sent as the next turn when this run finishes." Existing `continue`/`guidance` modes unchanged.
- **`api/tickets.ts` / `api/types.ts`** — steer client methods + types.
- Chip state machine: `pending` (editable/deletable/draggable) → `delivering` (locked, spinner) → `delivered`/`cancelled` (removed). Optimistic add; reconcile from GET.

## Failure / edge cases — the forced choice, answered

**Run finishes with the queue non-empty → roll into `next_run_context` AND auto-continue.** Leaving them `pending` is a trap on queue-only backends (no next turn until a manual click; messages look sent but aren't). Rolling into `next_run_context` + auto-`continue` (when resumable) makes the user's intent actually happen, via the tested continue path, with drained text visible as `continue_message`. Non-resumable → `review` with the pre-filled chip; one click runs it. Nothing silently dropped.

- **Cancel with pending messages** — cancel wins; drain into `next_run_context`, no auto-continue (user explicitly stopped).
- **Delivering crash** — orphan sweep resets to `pending`.
- **opencode idles before watcher claims a just-added message** — on-idle `get_nowait` closes most of the window; residue hits the run-completion drain. Race costs one turn boundary, never a message.
- **Edit/delete after delivery started** — 409; strip greys `delivering` chips.
- **Seq collision** — prevented by construction (executor-only writes). Most likely implementation bug; re-check in review.
- **Empty/whitespace body** — rejected (mirror `set_next_run_context` strip-guard).
- **Ticket not running** — POST 409; composer hides steer mode.

## Test plan

- Domain unit (`tests/domain/test_steering.py`): CRUD guards, claim atomicity/ordering, drain folds + cancels + populates context.
- Capability: `OPENCODE.provides(STEER_INJECT)`, `not CLAUDE_SDK.provides(STEER_INJECT)`, both `STEER_QUEUE` (guards the deliberate narrowing).
- Worker inject (`tests/worker/test_opencode_steer.py`): fake httpx server; steer item → follow-up POST, no finish on first idle while queued, `on_steer_delivered` fires, `steer_delivered` in transcript with non-colliding seq.
- Worker queue-only (`tests/worker/test_run_one_steer.py`): DummyExecutor inproc; pre-seed pending; assert drain into `next_run_context` + auto-continue staged (or review + chip when non-resumable).
- Orphan recovery; API CRUD + 409s + capability payload; migration upgrade/downgrade + guard idempotency; frontend SteerQueue + composer mode tests.

## Implementation checklist (file-by-file)

Foundation
- [ ] `alembic/versions/0023_steer_messages.py`
- [ ] `src/nightdesk/db/models.py` — `SteerMessage` + relationship
- [ ] `src/nightdesk/domain/backend_capabilities.py` — STEER_QUEUE/STEER_INJECT; narrow CLAUDE_SDK to explicit set

Core logic
- [ ] `src/nightdesk/domain/steering.py`
- [ ] `src/nightdesk/worker/executor.py` — ExecutionRequest fields
- [ ] `src/nightdesk/worker/run_one.py` — queue, watcher, callback, drain + auto-continue
- [ ] `src/nightdesk/backends/opencode_driver.py` — `_post_followup`, queue race, idle drain, emit
- [ ] `src/nightdesk/worker/_sdk_runner.py` — docstring note only
- [ ] `src/nightdesk/worker/heartbeat.py` — stale delivering reset

Integration
- [ ] `src/nightdesk/api/schemas.py` + `routes/tickets.py` — schemas + five endpoints
- [ ] `frontend/src/api/types.ts` + `api/tickets.ts`
- [ ] `frontend/src/routes/tickets/detail/SteerQueue.tsx` (new)
- [ ] `frontend/src/routes/tickets/detail/ActivityComposer.tsx`
- [ ] `frontend/src/lib/transcript.ts` + `components/TranscriptView.tsx`
- [ ] `~/.claude/skills/nightdesk-api/SKILL.md` + `nightdesk-ticket-ops/SKILL.md`
- [ ] Tests as above

Key review risks: (1) narrowing `CLAUDE_SDK.capabilities`; (2) seq-ownership for `steer_delivered`; (3) migration linearization at master merge.
