from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from pathlib import Path

from nightdesk.api.auth import (
    Principal,
    enforce_self_ticket,
    require_scopes,
)
from nightdesk.domain import scopes as sc
from nightdesk.api.schemas import RunOut
from nightdesk.db.models import TicketWorkspace
from nightdesk.domain.diff import (
    RunDiff, compute_workspace_diff, diff_sidecar_path, diff_to_json,
    diff_to_stat_json, read_diff_sidecar, run_diff_from_json,
    select_diff_workspace, write_diff_sidecar,
)
from nightdesk.domain.run_result import result_sidecar_path, write_result_sidecar
from nightdesk.domain.runs import get_run, list_runs, RunNotFound
from nightdesk.logging_setup import run_log_path
from nightdesk.transcript import KNOWN_TYPES, append_events


def build_router(get_session, bearer_token: str, engine=None, scoped=None) -> APIRouter:
    # Bare parent (no global auth dep). The read routes carry the runs.read
    # scope gate; the run-token write-back routes carry their own per-route
    # self-scope gate. A read token would fail the write-back's self-scope check
    # and vice versa, so the two auth models must not share one router-level dep.
    router = APIRouter(tags=["runs"])
    read = APIRouter(
        prefix="/api/v1/runs",
        dependencies=[Depends(scoped(sc.RUNS_READ))],
    )

    @read.get("", response_model=list[RunOut])
    async def lst(ticket_id: str | None = Query(default=None),
                   session: Session = Depends(get_session)):
        return list_runs(session, ticket_id=ticket_id)

    @read.get("/{rid}", response_model=RunOut)
    async def show(rid: str, session: Session = Depends(get_session)):
        try:
            return get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")

    def _resolve_run_diff(session: Session, rid: str):
        """Resolve the canonical RunDiff for a run, shared by /diff and /diffstat.

        Prefers the pod-uploaded sidecar (k8s runs have no host worktree); else
        selects the run's diffable workspace by kind and computes the diff
        (git worktree ``start_sha..end`` or a directory snapshot diff). Returns
        ``(RunDiff | None, sidecar_hit)`` — ``None`` means no diffable workspace
        (caller renders its empty state). Raises HTTPException(404) on unknown rid.
        """
        try:
            run = get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")

        sidecar = read_diff_sidecar(
            diff_sidecar_path(Path(run.transcript_path).parent, rid)
        )
        if sidecar is not None:
            return run_diff_from_json(sidecar), True

        ws = select_diff_workspace(_run_workspaces(session, run))
        result = compute_workspace_diff(
            ws,
            transcript_root=Path(run.transcript_path).parent,
            run_id=rid,
        )
        return result, False

    @read.get("/{rid}/diff")
    async def run_diff(rid: str, session: Session = Depends(get_session)):
        """Return a structured unified diff for the run's workspace changes.

        A remote (k8s) run has no host worktree to diff, so it uploads a
        structured diff sidecar from the pod; when that sidecar is present it is
        served verbatim (it is already the canonical RunDiff JSON shape).
        Otherwise the run's diffable workspace is selected by kind and
        dispatched: git workspaces diff ``start_sha..end`` via git; non-git
        (directory) workspaces diff the run-start filesystem snapshot against the
        current tree.
        """
        result, _sidecar_hit = _resolve_run_diff(session, rid)
        if result is None:
            return JSONResponse({
                "files": [],
                "total_added": 0,
                "total_deleted": 0,
                "total_files": 0,
                "truncated": False,
                "hidden_files": 0,
                "hidden_lines": 0,
                "error": "no workspace found for this run",
                "branch": "",
                "base_sha": "",
                "head_sha": "",
                "repo_root": "",
            })
        return JSONResponse(diff_to_json(result))

    @read.get("/{rid}/diffstat")
    async def run_diffstat(rid: str, session: Session = Depends(get_session)):
        """Light per-file diff stat (path, additions, deletions) + totals.

        Same source resolution as ``GET /{rid}/diff`` (pod-uploaded sidecar,
        else the selected workspace's computed diff), projected to a stat-only
        shape with no hunk bodies. The Overview verdict rows read this for a
        files count and +/− tally per review run without shipping the full
        unified diff. Numbers always agree with the Changes tab.
        """
        result, _sidecar_hit = _resolve_run_diff(session, rid)
        if result is None:
            return JSONResponse({
                "files": [],
                "total_files": 0,
                "total_added": 0,
                "total_deleted": 0,
                "truncated": False,
                "hidden_files": 0,
                "error": "no workspace found for this run",
            })
        return JSONResponse(diff_to_stat_json(result))

    @read.get("/{rid}/log")
    async def download_log(rid: str, session: Session = Depends(get_session)):
        """Download the per-run worker log file as plain text."""
        try:
            get_run(session, rid)
        except RunNotFound:
            raise HTTPException(404, "not found")
        path = run_log_path(rid)
        if not path.exists():
            return PlainTextResponse("(no log for this run)", status_code=404)
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
            filename=f"nightdesk-run-{rid}.log",
        )

    # --- Run-token write-back surface ------------------------------------
    # These accept the run's own scoped ndr_ token (or an admin bearer). They
    # are how an off-host agent (a k8s pod) streams its transcript and uploads
    # its diff/result back over HTTP: the pod holds only its run token, so every
    # route is require_scopes(...self) + enforce_self_ticket. See
    # docs/design/session-suite/k8s-executor.md ("API Additions").
    wb = APIRouter(prefix="/api/v1/runs")
    if engine is not None:
        _append = require_scopes(bearer_token, engine, ["run.append_transcript.self"])
        _mark_done = require_scopes(bearer_token, engine, ["run.mark_done.self"])

        def _self_run(session: Session, rid: str, principal: Principal):
            try:
                run = get_run(session, rid)
            except RunNotFound:
                raise HTTPException(404, "not found")
            enforce_self_ticket(principal, run.ticket_id)
            return run

        @wb.post("/{rid}/transcript")
        async def post_transcript(
            rid: str,
            request: Request,
            session: Session = Depends(get_session),
            principal: Principal = Depends(_append),
        ):
            """Append a batch of canonical NDJSON transcript events.

            Body is newline-delimited JSON, one canonical event per line. Each
            line must be a JSON object with a recognized ``type``; the host
            reassigns ``seq`` to keep the transcript's monotonic space. Makes the
            host SSE tail light up live for pod runs.
            """
            run = _self_run(session, rid, principal)
            raw = (await request.body()).decode("utf-8", errors="replace")
            events: list[dict] = []
            for lineno, line in enumerate(raw.splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    import json as _json
                    evt = _json.loads(line)
                except ValueError:
                    raise HTTPException(400, f"line {lineno}: not valid JSON")
                if not isinstance(evt, dict):
                    raise HTTPException(400, f"line {lineno}: event must be an object")
                etype = evt.get("type")
                if not isinstance(etype, str) or etype not in KNOWN_TYPES:
                    raise HTTPException(400, f"line {lineno}: unknown event type {etype!r}")
                events.append(evt)
            written = append_events(run.transcript_path, events)
            return {"appended": written}

        @wb.post("/{rid}/diff")
        async def post_diff(
            rid: str,
            request: Request,
            session: Session = Depends(get_session),
            principal: Principal = Depends(_mark_done),
        ):
            """Upload the run's structured diff (RunDiff JSON) as a sidecar.

            Stored next to the transcript; ``GET /diff`` prefers it. Round-tripped
            through ``run_diff_from_json``/``diff_to_json`` so a malformed payload
            is normalized rather than served raw.
            """
            run = _self_run(session, rid, principal)
            try:
                import json as _json
                payload = _json.loads((await request.body()).decode("utf-8"))
            except ValueError:
                raise HTTPException(400, "body must be a JSON RunDiff object")
            if not isinstance(payload, dict):
                raise HTTPException(400, "body must be a JSON RunDiff object")
            normalized = diff_to_json(run_diff_from_json(payload))
            write_diff_sidecar(
                diff_sidecar_path(Path(run.transcript_path).parent, rid),
                normalized,
            )
            return {"stored": True}

        @wb.post("/{rid}/result")
        async def post_result(
            rid: str,
            request: Request,
            session: Session = Depends(get_session),
            principal: Principal = Depends(_mark_done),
        ):
            """Persist the run's reported outcome (exit/usage/session) as a sidecar.

            The host stays the authority: it reads this back into an
            ExecutionResult and runs the same finish_run + pricing path a local
            run takes. This route never finalizes the run row itself.
            """
            run = _self_run(session, rid, principal)
            try:
                import json as _json
                payload = _json.loads((await request.body()).decode("utf-8"))
            except ValueError:
                raise HTTPException(400, "body must be a JSON object")
            if not isinstance(payload, dict):
                raise HTTPException(400, "body must be a JSON object")
            status = payload.get("exit_status")
            if status not in ("success", "failed", "cancelled"):
                raise HTTPException(400, f"invalid exit_status {status!r}")
            write_result_sidecar(
                result_sidecar_path(Path(run.transcript_path).parent, rid),
                payload,
            )
            return {"stored": True}

    router.include_router(read)
    router.include_router(wb)
    return router


def _run_workspaces(session: Session, run) -> list:
    """Candidate workspaces for a run's diff, conversation-scoped first.

    A Conversation owns its workspaces (1:N), so the diff for any turn in it is
    computed against the tree THAT conversation ran against. Preference order:
    the run's conversation's workspaces; then run-scoped rows (legacy); then the
    ticket's workspaces (older legacy rows). The caller picks the diffable one
    via ``select_diff_workspace``. Mirrors the HTMX ticket-page path.
    """
    conversation_id = getattr(run, "conversation_id", None)
    if conversation_id:
        conv_scoped = list(session.execute(
            select(TicketWorkspace)
            .where(TicketWorkspace.conversation_id == conversation_id)
            .order_by(TicketWorkspace.position)
        ).scalars())
        if conv_scoped:
            return conv_scoped
    run_scoped = list(session.execute(
        select(TicketWorkspace)
        .where(TicketWorkspace.run_id == run.id)
        .order_by(TicketWorkspace.position)
    ).scalars())
    if run_scoped:
        return run_scoped
    return list(session.execute(
        select(TicketWorkspace)
        .where(TicketWorkspace.ticket_id == run.ticket_id)
        .order_by(TicketWorkspace.position)
    ).scalars())
