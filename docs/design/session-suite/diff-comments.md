# Run Diff Commenting — Design

Studied read-only against `base/session-suite`. This is the React/Vite SPA branch (not HTMX): the diff renders in `frontend/src/components/DiffView.tsx`, consumed by `RunTheater.tsx` (Diff tab) and `RunTimeline.tsx` (inline "Changes").

## Patterns & Conventions Found

- **Diff endpoint:** `GET /api/v1/runs/{rid}/diff` — `src/nightdesk/api/routes/runs.py:39`. Returns `{files:[{path, old_path, new_path, binary, lines_added, lines_deleted, hunks:[{kind, gutter, text, line_no_old, line_no_new}]}], total_added, total_deleted, total_files, truncated, branch, base_sha, head_sha, repo_root, error}`. Each hunk row already carries `line_no_old` / `line_no_new` as strings (`""` on the side that doesn't apply) — ideal anchor material.
- **Diff computation:** `src/nightdesk/domain/diff.py`. git_worktree workspaces diff `run_start_sha..HEAD` live; directory workspaces diff an fs snapshot. `run_start_sha` is captured at run start (`TicketWorkspace.run_start_sha`, `db/models.py:462`).
- **next_run_context (the steering surface):** single free-form text field on `Ticket` (`next_run_context`, `next_run_context_updated_at`, `db/models.py:212`). Domain fns `set_next_run_context` / `merge_next_run_context_into_prompt` in `domain/tickets.py:765`. Routes `POST /api/v1/tickets/{tid}/next-run-context` and `/merge-next-run-context` in `routes/tickets.py:557`. On the next run the worker folds it into the prompt (`domain/tickets.py:899` `_stage_next_run` / `carry_context`).
- **UI kit:** `@/ui/{Button,Tooltip,Textarea(Input),Badge,Toast}`, `@/lib/cn`, Tailwind tokens `ink-*` (surfaces), `moon-*` (text), `success`/`failed`/`review`/`lamp` (accents), `rounded-card`/`rounded-control`, `font-mono text-[11px]`. `[data-tooltip]`/`<Tooltip>` primitive is mandated over native `title=`.
- **Activity rail:** there is **no** generic events/activity table. The "rail" is composed: `RunTimeline` (per-run accordion) + `ActivityComposer` (`detail/ActivityComposer.tsx`) which already renders a staged-guidance "PendingChip". That chip is where "N review comments queued for next run" surfaces.
- **Migration head:** `0022_providers_and_endpoints` (revises `0021_run_latency`). `0024_diff_comments` sets `down_revision = "0022_providers_and_endpoints"`, linearized at integration.

## Two corrections to the brief (load-bearing)

1. **Anchors are NOT fully stable.** The *run row* is immutable once finished, but the diff endpoint recomputes live from git (`run_start_sha..HEAD` against the *current* working tree) on every request. Resume/Retry/Continue reuse the same worktree, so a later run mutates the tree and an older run's recomputed diff shifts line numbers. Design consequence: store `anchor_head_sha` (the diff's `head_sha` at comment time) + `anchor_text` (the line's text) on each thread, and mark a thread **"outdated"** when the live diff's `head_sha` differs — exactly how GitHub greys out review comments after a force-push. Do not assume a pinned snapshot.

2. **Run-token HTTP surface is not wired in this branch.** `require_scopes`/`require_principal` and `SELF_SCOPES` (incl. `ticket.update.next_run_context`) exist in `api/auth.py` + `domain/run_tokens.py`, but **every** tickets/runs route uses `require_token_cookie_or_bearer` (admin cookie/bearer only). No route currently accepts an `ndr_` run token. So "a running agent fetches its review feedback over HTTP" needs new wiring. Recommendation: the agent gets feedback the cheap way — via `next_run_context` on its next run (no new auth needed) — and the live read endpoint is Phase 2, gated behind a new `run.read_comments.self` scope.

## Architecture Decision

**Chosen approach:** One new table `diff_comments`, one-level threading via nullable self-FK `parent_id`. A **root** comment (`parent_id IS NULL`) carries the anchor (`file_path`, `side`, `line`, `anchor_head_sha`, `anchor_text`) plus `resolved`/`delivered_at`; **replies** carry only `body` + author and point at their root. Bodies are ordered by `created_at`.

**Rationale:**
- Single table matches the codebase's denormalizing style (runs/workspaces already denorm `ticket_id`/`conversation_id`).
- Resolution and the anchor are thread-level facts; putting them on the root avoids a second `DiffThread` table while still supporting replies (agent says "done", human re-comments).
- One-level nesting mirrors GitHub review threads; single-user product needs nothing deeper.

**Why `parent_id` over "flat-per-anchor grouping":** flat grouping can't distinguish a *reopened new thread* on the same line from an old resolved one (same `file_path/side/line`). `parent_id` makes thread identity explicit and makes resolve/delete semantics trivial.

**Rejected alternatives:**
- *Two tables (DiffThread + DiffComment):* extra table/migration/relationship for a single-user tool; root-carries-anchor gets the same result.
- *Comment stored as a JSON blob on the run:* breaks per-thread resolve/query, no FK integrity.
- *Reusing next_run_context as the store:* single text field, no anchoring; can't render gutters or resolve threads.

## Data Model

New table `diff_comments` (`db/models.py`):

```python
class DiffComment(Base):
    __tablename__ = "diff_comments"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    # denorm for cheap ticket-rail listing + next_run_context targeting,
    # mirrors runs/workspaces carrying ticket_id/conversation_id.
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True)
    # Threading: NULL = root (carries anchor); else points at the root.
    parent_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("diff_comments.id", ondelete="CASCADE"), nullable=True, index=True)
    # Anchor (root only; NULL on replies).
    file_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    side: Mapped[Optional[str]] = mapped_column(String, nullable=True)   # 'old' | 'new'
    line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1-based, on `side`
    anchor_head_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    anchor_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Content.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Acting principal (agent may file via API). 'admin' | 'agent'.
    author_kind: Mapped[str] = mapped_column(String, default="admin", nullable=False)
    author_run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("runs.id"), nullable=True)  # the run token's run, when agent-authored
    # Resolution + delivery (root only).
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
```

- `side` semantics match `DiffLine`: `ins` row → `new`, `del` row → `old`, `ctx` row → default `new`. `line` is the corresponding `line_no_*`.
- `author_kind='agent'` + `author_run_id` records the acting principal (provenance groundwork), no avatar/identity machinery.
- Root-only fields are nullable so replies reuse the same table cleanly.

## Component Design

### Domain: `src/nightdesk/domain/diff_comments.py` (new)
Single-purpose CRUD + the bundling logic. No HTTP, no ORM leakage past return.
```python
def list_run_comments(session, run_id) -> list[DiffComment]        # roots + replies, ordered
def create_comment(session, run_id, *, anchor, body, author) -> DiffComment   # root
def reply_comment(session, parent_id, *, body, author) -> DiffComment
def edit_comment(session, comment_id, body) -> DiffComment
def set_resolved(session, comment_id, resolved: bool, author) -> DiffComment   # root only
def delete_comment(session, comment_id) -> None                    # cascades replies
def unresolved_threads(session, run_id) -> list[DiffComment]       # roots where not resolved
def request_changes(session, run_id) -> Ticket
    # format unresolved roots+replies into a structured block, APPEND to the
    # ticket's next_run_context (reuse set_next_run_context), stamp delivered_at.
```
`anchor` = `{file_path, side, line, anchor_head_sha, anchor_text}`, resolved by the route from the diff the client is looking at. `author` = `{kind, run_id}` derived from the principal.

Bundled block format written into `next_run_context` (plain, agent-readable, diff-anchored — not a chat log):
```
## Review comments to address (3 unresolved)
- src/foo.py:42 (new): "this should be memoized"
    ↳ agent: attempted but reverted — human: still needed under load
- src/bar.ts:in the deleted block near line 88 (old): "why remove the guard?"
```

### Domain: `domain/tickets.py` (modify)
Add a small `append_next_run_context(session, ticket_id, text)` helper next to `set_next_run_context:765` so we append rather than overwrite staged guidance.

### API: `src/nightdesk/api/routes/review_comments.py` (new)
Mounted by the app factory. Two prefixes on one router:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/runs/{rid}/comments` | principal (admin; run-self Phase 2) | list threads for a run's diff |
| POST | `/api/v1/runs/{rid}/comments` | admin (Phase 1) | create root or reply (`parent_id?`) |
| PATCH | `/api/v1/diff-comments/{cid}` | admin | edit body |
| POST | `/api/v1/diff-comments/{cid}/resolve` | admin | resolve root |
| POST | `/api/v1/diff-comments/{cid}/unresolve` | admin | reopen root |
| DELETE | `/api/v1/diff-comments/{cid}` | admin | delete (cascade replies) |
| POST | `/api/v1/runs/{rid}/comments/request-changes` | admin | bundle unresolved → next_run_context |

Create payload: `{parent_id?, file_path, side, line, anchor_head_sha, anchor_text, body}`. Response schema `DiffCommentOut` in `schemas.py` + a computed `outdated: bool` set by comparing `anchor_head_sha` to the live diff `head_sha`.

### Auth wiring
- Phase 1: whole router uses `require_token_cookie_or_bearer(bearer_token)` (admin), matching runs/tickets.
- Phase 2 (agent read/create): add scope `run.read_comments.self` (and optionally `run.create_comment.self`) to `SELF_SCOPES` in `domain/run_tokens.py:28`. Switch the GET (and optionally POST) to `require_scopes(...)` + `enforce_self_ticket(principal, ticket_id)`. Requires passing `engine` into `build_router`. Agent-created comment = `author_kind='agent'`, `author_run_id=principal.run_id`.

### Migration: `alembic/versions/0024_diff_comments.py` (new)
`revision = "0024_diff_comments"`, `down_revision = "0022_providers_and_endpoints"`. `create_table` with the columns above + indexes on `run_id`, `ticket_id`, `parent_id`. Additive; `downgrade()` drops the table. (Integration re-parents `down_revision` when linearizing after 0023.)

### Frontend

**`frontend/src/api/diffComments.ts` (new):** `runsApi`-style module — `listForRun(rid)`, `create(rid, payload)`, `edit(cid, body)`, `resolve/unresolve(cid)`, `remove(cid)`, `requestChanges(rid)`, plus `useRunComments(rid)` query hook keyed `qk.runs.comments(rid)`.

**`frontend/src/components/DiffView.tsx` (modify — the core UI):**
- Optional props `runId?`, `headSha?`, `comments?`. When present, `DiffView` becomes interactive; without them it renders exactly as today.
- **Gutter affordance:** each `DiffRow` gets a hover-revealed `+` button in a new leading gutter cell (Tooltip "Comment on line"). Click opens an inline composer row (`<Textarea>` + Save/Cancel) inserted under the line, styled `rounded-card border-ink-700 bg-ink-900`.
- **Thread rendering:** threads render as full-width rows under their anchor line: author chip (`admin`/`agent` `<Badge>`), body, relative time, reply composer, Resolve/Reopen/Edit/Delete actions (Tooltip-wrapped icon buttons). Resolved threads collapse to "Resolved · N comments" (click to expand). Outdated threads (`anchor_head_sha !== diff.head_sha`) get a `lamp`-toned "outdated" `<Badge>` and render against `anchor_text`.
- **File-header badge:** `FileDiff` header shows a comment-count `<Badge>` (`review` tone) when that file has threads.
- Anchor derivation on create: `side = ins?'new':del?'old':'new'`, `line = Number(side==='new'?row.line_no_new:row.line_no_old)`, `anchor_text = row.text`, `anchor_head_sha = diff.head_sha`.

**`frontend/src/routes/tickets/RunTheater.tsx` (modify):** fetch `useRunComments(rid)`; Diff tab button gets an unresolved-count `<Badge>`; pass `runId`/`headSha`/`comments` into `<DiffView>`; add **"Request changes"** button in the diff pane header (visible when unresolved threads exist) → toast "N comments sent to next run" → invalidate ticket query so the PendingChip updates.

**`frontend/src/routes/tickets/RunTimeline.tsx` (modify):** pass the same props into its inline `<DiffView>` and show the per-run unresolved count next to "Changes".

**`frontend/src/routes/tickets/detail/ActivityComposer.tsx` (modify):** after `request-changes` the bundled block appears in the PendingChip automatically. Add a subtle "from review comments" caption when delivered review threads exist. No new rail component.

**Keyboard access:** Cmd/Ctrl+Enter submits (matches ActivityComposer:150); Esc cancels. The gutter `+` is a real `<button>` (tab-focusable); action icons carry `aria-label`s.

## Data Flow

1. Human opens Run Theater → Diff tab. `GET /runs/{rid}/diff` (live) + `GET /runs/{rid}/comments`.
2. Clicks a line's gutter `+` → inline composer → `POST /runs/{rid}/comments` with anchor derived from the diff row.
3. Thread renders under the line; resolve/edit/reply via `/diff-comments/{cid}/*`.
4. **Request changes** → `POST /runs/{rid}/comments/request-changes` → domain formats unresolved threads → appends to `ticket.next_run_context` → stamps `delivered_at`.
5. Staged block shows in the ActivityComposer PendingChip. On the next run the worker folds `next_run_context` into the prompt (`domain/tickets.py:899`).
6. (Phase 2) Running agent reads/replies via run token with `run.read_comments.self`.

## Build Sequence

**Phase 1 — Backend foundation:** models, migration 0024, domain module, `append_next_run_context`, schemas.
**Phase 2 — API:** router + registration in app factory, `outdated` computation.
**Phase 3 — Frontend:** api client + keys, DiffView interactivity, RunTheater, RunTimeline, ActivityComposer caption.
**Phase 4 — Agent read (optional, gated):** run-token scope + `require_scopes` GET.

## Critical Details

- **Anchor staleness:** never trust `line` alone across runs. Compare `anchor_head_sha` to live `diff.head_sha`; on mismatch mark `outdated` and render `anchor_text`. Threads are never silently mis-placed.
- **Error handling:** unknown `rid`/`cid` → 404. Reply-to-non-root or resolve-of-reply → 422. `request-changes` with zero unresolved → 422 (mirrors `merge_next_run_context` empty-guard at `tickets.py:778`). Deleting a root cascades replies via FK.
- **State:** all state in `diff_comments`; the diff stays stateless/recomputed. `next_run_context` remains the single steering channel (append, don't fork a parallel one). Comments stay strictly diff-anchored — the "no free-form ticket comments" decision stands.
- **Testing:** domain unit tests (create root/reply, resolve/unresolve, delete cascade, request_changes formatting + delivered_at); API tests (CRUD, 404/422 guards, `outdated` flips when a second run advances the worktree head — reuse run-diff git fixtures); frontend (threads at correct anchors, composer open, resolved collapse, badge counts).
- **Security:** Phase 1 admin-only. Phase 2 agent access strictly self-ticket via `enforce_self_ticket`. Bodies rendered as text (React escapes). No new egress.

## File-by-file checklist
- `src/nightdesk/db/models.py` — add `DiffComment` (Modify)
- `alembic/versions/0024_diff_comments.py` — create table (Create)
- `src/nightdesk/domain/diff_comments.py` — CRUD + bundling (Create)
- `src/nightdesk/domain/tickets.py` — `append_next_run_context` (Modify)
- `src/nightdesk/api/schemas.py` — 3 schemas (Modify)
- `src/nightdesk/api/routes/review_comments.py` — router (Create)
- app factory — register router (Modify)
- `frontend/src/api/diffComments.ts` — client + hook (Create)
- `frontend/src/api/keys.ts` — `qk.runs.comments` (Modify)
- `frontend/src/components/DiffView.tsx` — interactive gutter/threads (Modify)
- `frontend/src/routes/tickets/RunTheater.tsx` — badge + request-changes (Modify)
- `frontend/src/routes/tickets/RunTimeline.tsx` — pass props + count (Modify)
- `frontend/src/routes/tickets/detail/ActivityComposer.tsx` — review caption (Modify)
- Phase 4 only: `src/nightdesk/domain/run_tokens.py` — new scope (Modify)

CLAUDE.md compliance: this adds routes + schemas, so `nightdesk-api` and `nightdesk-ticket-ops` skills must be updated in the same change.
