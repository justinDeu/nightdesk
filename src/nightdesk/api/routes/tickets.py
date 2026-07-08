from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.domain.projects import ProjectNotFound
from nightdesk.api.schemas import (
    AdditionalDirAdd,
    BulkArchiveRequest, BulkLabelsUpdate,
    BulkPriorityUpdate, BulkProfileUpdate, BulkProjectUpdate, BulkStatusUpdate,
    BulkUpdateResult,
    DependencyCreate, DependencyOut,
    TicketClone, TicketContinue, TicketCreate, TicketNewConversation,
    TicketNextRunContext, TicketOut,
    TicketPriorityUpdate, TicketProfileUpdate,
    TicketProjectUpdate, TicketReorder, TicketRestart, TicketResumeOrRetry,
    TicketStatusUpdate, TicketTransition,
    TicketUpdate,
)
from nightdesk.domain.tickets import (
    add_dependency, archive, bulk_archive, bulk_unarchive,
    bulk_update_priority, bulk_update_profile,
    bulk_update_project, bulk_update_status, clone_ticket, continue_ticket,
    count_tickets,
    create_ticket,
    delete_ticket, get_ticket, list_tickets,
    merge_next_run_context_into_prompt, new_conversation_ticket,
    remove_dependency, requeue,
    reorder_in_column, request_run_now, restart_ticket, resume_ticket,
    retry_ticket, send_to_inbox, set_next_run_context, set_run_now, transition_status,
    transition_with_position, unarchive, update_ticket,
    update_ticket_priority, update_ticket_profile, update_ticket_project,
    ConversationNotResumable, TicketNotFound, InvalidTransition,
    CyclicDependency, DependencyNotFound,
)
from nightdesk.domain.profiles import ProfileNotFound
from nightdesk.domain.labels import LabelNotFound, set_ticket_labels


# Hard ceiling on the page size for GET /api/v1/tickets. The default ``limit``
# is 200, but callers may request a larger page (e.g. to retro-tag every ticket
# in one pass). Anything above this is REJECTED with 422 — never silently
# clamped — so a caller cannot unknowingly receive a truncated slice. Page past
# the ceiling with ``offset``; the ``X-Total-Count`` / ``X-Has-More`` headers
# let a caller detect incomplete results regardless of the limit it asked for.
MAX_LIST_LIMIT = 1000


def _coerce_dirs(payload_dirs):
    """Convert AdditionalDir pydantic models to plain dicts for JSON storage."""
    if payload_dirs is None:
        return None
    return [
        d.model_dump() if hasattr(d, "model_dump") else dict(d) for d in payload_dirs
    ]


def _coerce_workspaces(payload_workspaces):
    if payload_workspaces is None:
        return None
    return [
        w.model_dump() if hasattr(w, "model_dump") else dict(w)
        for w in payload_workspaces
    ]


def _ticket_to_out(t) -> TicketOut:
    """Build a TicketOut including dependency edges."""
    from nightdesk.api.schemas import TicketWorkspaceOut
    deps = []
    for dep in (t.dependencies or []):
        upstream = dep.depends_on
        deps.append(DependencyOut(
            id=dep.id,
            ticket_id=t.id,
            depends_on_id=dep.depends_on_id,
            depends_on_title=upstream.title if upstream else "(deleted)",
            depends_on_status=upstream.status if upstream else "unknown",
            created_at=dep.created_at,
        ))
    workspaces = []
    for ws in (t.workspaces or []):
        workspaces.append(TicketWorkspaceOut.model_validate(ws))
    labels = [
        {"id": lbl.id, "name": lbl.name, "color": lbl.color}
        for lbl in (t.labels or [])
    ]
    data = {
        "id": t.id,
        "title": t.title,
        "prompt": t.prompt,
        "status": t.status,
        "kind": t.kind,
        "priority": t.priority,
        "position": t.position,
        "project_id": t.project_id,
        "profile_id": t.profile_id,
        "permission_overrides": t.permission_overrides,
        "toolchain_overrides": t.toolchain_overrides,
        "additional_dirs": t.additional_dirs or [],
        "workspaces": workspaces,
        "labels": labels,
        "run_now": t.run_now,
        "commit_on_finish": t.commit_on_finish,
        "scheduled_after": t.scheduled_after,
        "current_run_id": t.current_run_id,
        "next_run_context": t.next_run_context,
        "next_run_context_updated_at": t.next_run_context_updated_at,
        "dependencies": deps,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }
    return TicketOut(**data)


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/tickets",
        tags=["tickets"],
        dependencies=[Depends(require_token_cookie_or_bearer(bearer_token))],
    )

    @router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
    async def create(payload: TicketCreate, session: Session = Depends(get_session)):
        data = payload.model_dump()
        # If caller didn't specify status, let domain default it (to 'draft').
        if data.get("status") is None:
            data.pop("status", None)
        if "workspace_mode" not in payload.model_fields_set:
            data.pop("workspace_mode", None)
        data["additional_dirs"] = _coerce_dirs(payload.additional_dirs) or []
        data["workspaces"] = _coerce_workspaces(payload.workspaces)
        try:
            t = create_ticket(session, **data)
            return _ticket_to_out(t)
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        except (InvalidTransition, ValueError) as e:
            raise HTTPException(422, str(e))

    @router.get("", response_model=list[TicketOut])
    async def lst(
        response: Response,
        status: str | None = Query(default=None),
        profile_id: str | None = Query(default=None),
        project_id: str | None = Query(default=None),
        priority: int | None = Query(default=None, ge=0, le=4),
        label: str | None = Query(default=None),
        outcome: str | None = Query(default=None, pattern="^(succeeded|failed)$"),
        q: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=MAX_LIST_LIMIT),
        offset: int = Query(default=0, ge=0),
        sort: str = Query(default="board", pattern="^(board|recent|created|priority|cost)$"),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        session: Session = Depends(get_session),
    ):
        """List tickets, one page at a time.

        ``limit`` is honored up to ``MAX_LIST_LIMIT``; requesting more is a 422
        (not a silent clamp). ``offset`` pages past the limit. The
        ``X-Total-Count`` and ``X-Has-More`` headers expose whether the page is
        the whole result set, so callers can never mistake a truncated slice for
        completeness — the original bug was exactly that a larger ``limit`` was
        ignored and 200 rows were returned with no signal of truncation.

        Filter dimensions (all optional, AND-combined, applied server-side so
        the count and paging stay honest): ``status``, ``profile_id``,
        ``project_id`` (``null`` matches no-project), ``priority`` (0-4),
        ``label`` (name or id), ``outcome`` (``succeeded``/``failed`` on the
        latest run), and ``q`` (free-text substring over title + prompt).

        ``sort`` defaults to ``board`` (position-stable, unchanged for existing
        callers). Other keys — ``recent`` (updated_at), ``created``,
        ``priority``, ``cost`` (latest run) — pair with ``order`` (``desc``
        default / ``asc``). ``sort=recent`` with the default ``order`` is the
        newest-first order the Archive page uses. Any other value is a 422.
        """
        tickets = list_tickets(
            session, status=status, profile_id=profile_id,
            project_id=project_id, limit=limit, offset=offset, sort=sort,
            order=order, priority=priority, label=label, outcome=outcome, q=q,
        )
        total = count_tickets(
            session, status=status, profile_id=profile_id, project_id=project_id,
            priority=priority, label=label, outcome=outcome, q=q,
        )
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Has-More"] = "true" if (offset + len(tickets)) < total else "false"
        response.headers["X-Limit"] = str(limit)
        response.headers["X-Offset"] = str(offset)
        return [_ticket_to_out(t) for t in tickets]

    # --- Bulk metadata update endpoints -----------------------------------------
    # Registered BEFORE /{tid} routes so FastAPI does not match "bulk" as a tid.

    @router.patch("/bulk/priority", response_model=BulkUpdateResult)
    async def bulk_priority(
        payload: BulkPriorityUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            updated, skipped = bulk_update_priority(
                session, payload.ticket_ids, payload.priority,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.patch("/bulk/status", response_model=BulkUpdateResult)
    async def bulk_status(
        payload: BulkStatusUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            updated, skipped = bulk_update_status(
                session, payload.ticket_ids, payload.status,
            )
        except InvalidTransition as e:
            raise HTTPException(422, str(e))
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.patch("/bulk/project", response_model=BulkUpdateResult)
    async def bulk_project(
        payload: BulkProjectUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            updated, skipped = bulk_update_project(
                session, payload.ticket_ids, payload.project_id,
            )
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.patch("/bulk/profile", response_model=BulkUpdateResult)
    async def bulk_profile(
        payload: BulkProfileUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            updated, skipped = bulk_update_profile(
                session, payload.ticket_ids, payload.profile_id,
            )
        except ProfileNotFound:
            raise HTTPException(404, "profile not found")
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.post("/reorder", response_model=list[TicketOut])
    async def reorder(payload: TicketReorder, session: Session = Depends(get_session)):
        try:
            tickets = reorder_in_column(session, payload.status, payload.ticket_ids)
            return [_ticket_to_out(t) for t in tickets]
        except InvalidTransition as e:
            raise HTTPException(422, str(e))

    @router.patch("/bulk/labels", response_model=BulkUpdateResult)
    async def bulk_labels(
        payload: BulkLabelsUpdate,
        session: Session = Depends(get_session),
    ):
        """Replace the label set on every listed ticket (not additive — mirrors
        the per-ticket PUT ``/api/v1/labels/tickets/{id}`` semantics)."""
        updated = []
        skipped = []
        for tid in payload.ticket_ids:
            try:
                t = set_ticket_labels(session, tid, payload.label_ids)
                updated.append(t)
            except TicketNotFound:
                skipped.append({"ticket_id": tid, "reason": "not found"})
            except LabelNotFound as e:
                skipped.append({"ticket_id": tid, "reason": str(e)})
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.post("/bulk/archive", response_model=BulkUpdateResult)
    async def bulk_archive_route(
        payload: BulkArchiveRequest,
        session: Session = Depends(get_session),
    ):
        updated, skipped = bulk_archive(session, payload.ticket_ids)
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    @router.post("/bulk/unarchive", response_model=BulkUpdateResult)
    async def bulk_unarchive_route(
        payload: BulkArchiveRequest,
        session: Session = Depends(get_session),
    ):
        updated, skipped = bulk_unarchive(session, payload.ticket_ids)
        return BulkUpdateResult(
            updated=[_ticket_to_out(t) for t in updated],
            skipped=skipped,
        )

    # --- Per-ticket routes (path parameter {tid}) --------------------------------

    @router.get("/{tid}", response_model=TicketOut)
    async def show(tid: str, session: Session = Depends(get_session)):
        try:
            t = get_ticket(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")

    @router.patch("/{tid}", response_model=TicketOut)
    async def update(tid: str, payload: TicketUpdate, session: Session = Depends(get_session)):
        data = payload.model_dump()
        fields = {k: v for k, v in data.items() if v is not None}
        if "project_id" in payload.model_fields_set:
            fields["project_id"] = data["project_id"]
        if "additional_dirs" in fields:
            fields["additional_dirs"] = _coerce_dirs(payload.additional_dirs) or []
        if "workspaces" in fields:
            fields["workspaces"] = _coerce_workspaces(payload.workspaces)
        try:
            t = update_ticket(session, tid, **fields)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProjectNotFound:
            raise HTTPException(404, "project not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.post("/{tid}/run-now", response_model=TicketOut)
    async def run_now(tid: str, session: Session = Depends(get_session)):
        try:
            t = request_run_now(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/cancel-run-now", response_model=TicketOut)
    async def cancel_run_now(tid: str, session: Session = Depends(get_session)):
        # Symmetric inverse of run-now: clear the run_now flag without changing
        # status (a queued+run-now ticket stays queued, it just stops asking the
        # scheduler to bypass the queue). Reuses the same domain helper the HTMX
        # surface and the drag-to-Queued path use, so there is one write path.
        try:
            t = set_run_now(session, tid, False)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")

    @router.post("/{tid}/cancel", response_model=TicketOut)
    async def cancel(tid: str, session: Session = Depends(get_session)):
        try:
            t = transition_status(session, tid, "review")
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/requeue", response_model=TicketOut)
    async def requeue_route(tid: str, session: Session = Depends(get_session)):
        try:
            t = requeue(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/transition", response_model=TicketOut)
    async def transition(
        tid: str, payload: TicketTransition,
        session: Session = Depends(get_session),
    ):
        try:
            t = transition_with_position(
                session, tid, payload.status, position=payload.position
            )
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/archive", response_model=TicketOut)
    async def archive_route(tid: str, session: Session = Depends(get_session)):
        try:
            t = archive(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/unarchive", response_model=TicketOut)
    async def unarchive_route(tid: str, session: Session = Depends(get_session)):
        try:
            t = unarchive(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/send-to-inbox", response_model=TicketOut)
    async def send_to_inbox_route(tid: str, session: Session = Depends(get_session)):
        """Send a draft ticket back to the inbox. Valid ONLY from ``draft``;
        every other status is a 409 (see ``domain.tickets.send_to_inbox``).

        The generic ``POST /{tid}/transition`` endpoint deliberately refuses
        ``status: "inbox"`` as a target (``TicketTransition`` excludes it —
        promotion is a one-way crossing everywhere else in the API), so this
        is the only JSON path back into the inbox for an existing ticket.
        """
        try:
            t = send_to_inbox(session, tid)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.post("/{tid}/continue", response_model=TicketOut)
    async def continue_route(
        tid: str, payload: TicketContinue,
        session: Session = Depends(get_session),
    ):
        """Continue the ACTIVE conversation: the ``message`` becomes the next
        user turn on the resumed SDK session (same runtime, full history).

        Mirrors the HTMX ``POST /tickets/{tid}/continue`` form end to end — it
        calls the same ``continue_ticket`` domain function, which stages the
        continue intent + the conversation id and queues a run-now so the worker
        seeds ``resume=`` from the conversation's session id. No resume logic is
        duplicated here. A conversation with no resumable session id is refused
        (409) and the caller is pointed at ``new-conversation``.
        """
        try:
            t = continue_ticket(session, tid, next_run_context=payload.message)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ConversationNotResumable as e:
            raise HTTPException(409, str(e))
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return _ticket_to_out(t)

    @router.post("/{tid}/new-conversation", response_model=TicketOut)
    async def new_conversation_route(
        tid: str, payload: TicketNewConversation,
        session: Session = Depends(get_session),
    ):
        """Start a FRESH conversation (new session, no resumed history).

        Symmetric JSON counterpart to the HTMX ``POST /tickets/{tid}/new-
        conversation`` form. Picks the runtime (``profile_id``; default the
        ticket's current profile) and workspace policy (``keep`` current files
        or ``fresh`` worktree). This is the path for switching runtime (e.g.
        Claude Code to OpenCode), since sessions are not portable across
        runtimes — an empty runtime switch still requires new-conversation.
        """
        try:
            t = new_conversation_ticket(
                session, tid,
                next_run_context=payload.message,
                profile_id=payload.profile_id,
                workspace_policy="fresh" if payload.workspace == "fresh" else None,
            )
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        except ProfileNotFound:
            raise HTTPException(404, "profile not found")
        return _ticket_to_out(t)

    @router.post("/{tid}/resume", response_model=TicketOut)
    async def resume_route(
        tid: str, payload: TicketResumeOrRetry,
        session: Session = Depends(get_session),
    ):
        """Fresh-context agent on the same worktree (a new conversation
        internally). JSON parity with the HTMX ``/tickets/{tid}/resume`` form."""
        try:
            t = resume_ticket(session, tid, next_run_context=payload.message)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return _ticket_to_out(t)

    @router.post("/{tid}/retry", response_model=TicketOut)
    async def retry_route(
        tid: str, payload: TicketResumeOrRetry,
        session: Session = Depends(get_session),
    ):
        """Re-attempt on the same worktree, fresh context. JSON parity with the
        HTMX ``/tickets/{tid}/retry`` form."""
        try:
            t = retry_ticket(session, tid, next_run_context=payload.message)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return _ticket_to_out(t)

    @router.post("/{tid}/restart", response_model=TicketOut)
    async def restart_route(
        tid: str, payload: TicketRestart,
        session: Session = Depends(get_session),
    ):
        """Fresh worktree, fresh context. JSON parity with the HTMX
        ``/tickets/{tid}/restart`` form."""
        try:
            t = restart_ticket(
                session, tid,
                next_run_context=payload.message,
                workspace_policy=payload.workspace_policy,
            )
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ValueError as e:
            raise HTTPException(422, str(e))
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        return _ticket_to_out(t)

    @router.post("/{tid}/clone", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
    async def clone_route(
        tid: str, payload: TicketClone,
        session: Session = Depends(get_session),
    ):
        try:
            cloned = clone_ticket(
                session, tid,
                title=(payload.title or "").strip() or None,
                carry_context=payload.carry_context,
            )
        except TicketNotFound:
            raise HTTPException(404, "not found")
        return _ticket_to_out(cloned)

    @router.post("/{tid}/next-run-context", response_model=TicketOut)
    async def next_run_context_route(
        tid: str, payload: TicketNextRunContext,
        session: Session = Depends(get_session),
    ):
        """Stage (or clear, with an empty body) a steering note for the next run."""
        try:
            t = set_next_run_context(session, tid, payload.body)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        return _ticket_to_out(t)

    @router.post("/{tid}/merge-next-run-context", response_model=TicketOut)
    async def merge_next_run_context_route(
        tid: str, session: Session = Depends(get_session),
    ):
        """Fold the staged next-run-context into the prompt and clear it."""
        try:
            t = merge_next_run_context_into_prompt(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return _ticket_to_out(t)

    @router.post("/{tid}/additional-dirs", response_model=TicketOut)
    async def add_additional_dir_route(
        tid: str, payload: AdditionalDirAdd,
        session: Session = Depends(get_session),
    ):
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        dirs = list(t.additional_dirs or [])
        if not any(d.get("path") == payload.path for d in dirs):
            dirs.append({"path": payload.path, "mode": payload.mode})
            t = update_ticket(session, tid, additional_dirs=dirs)
        return _ticket_to_out(t)

    @router.delete("/{tid}/additional-dirs", response_model=TicketOut)
    async def remove_additional_dir_route(
        tid: str, path: str = Query(...),
        session: Session = Depends(get_session),
    ):
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        dirs = [d for d in (t.additional_dirs or []) if d.get("path") != path]
        t = update_ticket(session, tid, additional_dirs=dirs)
        return _ticket_to_out(t)

    @router.delete("/{tid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete(tid: str, session: Session = Depends(get_session)):
        try:
            delete_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    # --- Focused metadata update endpoints --------------------------------------
    # Lightweight property-edit routes for the property picker, list inline
    # edits, keyboard actions, and bulk operations.  Each touches exactly one
    # field so callers don't need to construct a full TicketUpdate just to
    # change the priority.  The existing full PATCH endpoint is preserved.

    @router.patch("/{tid}/priority", response_model=TicketOut)
    async def update_priority(
        tid: str, payload: TicketPriorityUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            t = update_ticket_priority(session, tid, payload.priority)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.patch("/{tid}/status", response_model=TicketOut)
    async def update_status(
        tid: str, payload: TicketStatusUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            t = transition_status(session, tid, payload.status)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except InvalidTransition as e:
            raise HTTPException(409, str(e))

    @router.patch("/{tid}/project", response_model=TicketOut)
    async def update_project(
        tid: str, payload: TicketProjectUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            t = update_ticket_project(session, tid, payload.project_id)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProjectNotFound:
            raise HTTPException(404, "project not found")

    @router.patch("/{tid}/profile", response_model=TicketOut)
    async def update_profile(
        tid: str, payload: TicketProfileUpdate,
        session: Session = Depends(get_session),
    ):
        try:
            t = update_ticket_profile(session, tid, payload.profile_id)
            return _ticket_to_out(t)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        except ProfileNotFound:
            raise HTTPException(404, "profile not found")

    # --- Dependency endpoints ---------------------------------------------------

    @router.get("/{tid}/dependencies", response_model=list[DependencyOut])
    async def list_deps(tid: str, session: Session = Depends(get_session)):
        try:
            t = get_ticket(session, tid)
        except TicketNotFound:
            raise HTTPException(404, "not found")
        out = []
        for dep in (t.dependencies or []):
            upstream = dep.depends_on
            out.append(DependencyOut(
                id=dep.id,
                ticket_id=t.id,
                depends_on_id=dep.depends_on_id,
                depends_on_title=upstream.title if upstream else "(deleted)",
                depends_on_status=upstream.status if upstream else "unknown",
                created_at=dep.created_at,
            ))
        return out

    @router.post("/{tid}/dependencies", response_model=DependencyOut,
                  status_code=status.HTTP_201_CREATED)
    async def add_dep(
        tid: str, payload: DependencyCreate,
        session: Session = Depends(get_session),
    ):
        try:
            t = add_dependency(session, tid, payload.depends_on_id)
        except TicketNotFound:
            raise HTTPException(404, "ticket not found")
        except CyclicDependency as e:
            raise HTTPException(422, str(e))
        # Find the newly added dependency.
        for dep in t.dependencies:
            if dep.depends_on_id == payload.depends_on_id:
                upstream = dep.depends_on
                return DependencyOut(
                    id=dep.id,
                    ticket_id=t.id,
                    depends_on_id=dep.depends_on_id,
                    depends_on_title=upstream.title if upstream else "(deleted)",
                    depends_on_status=upstream.status if upstream else "unknown",
                    created_at=dep.created_at,
                )
        raise HTTPException(500, "dependency was not created")

    @router.delete("/{tid}/dependencies/{dep_on_id}",
                    status_code=status.HTTP_204_NO_CONTENT)
    async def remove_dep(tid: str, dep_on_id: str,
                         session: Session = Depends(get_session)):
        try:
            remove_dependency(session, tid, dep_on_id)
        except DependencyNotFound:
            raise HTTPException(404, "dependency not found")

    return router
