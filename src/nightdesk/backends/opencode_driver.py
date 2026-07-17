"""HTTP driver for the opencode backend.

Runs the sandboxed ``opencode serve`` subprocess (its argv is the bwrap
invocation in ``req.bwrap_argv``) and drives it over localhost:

1. spawn the server, wait for ``/global/health``
2. create a session scoped to the workspace dir
3. POST the prompt to ``/session/{id}/prompt_async``
4. stream ``/event`` (SSE), translating to canonical transcript events
5. finish on ``session.idle`` for our session; abort + terminate on cancel

Kept apart from ``opencode.py`` so the backend module stays import-light and
the driver (which needs httpx) is only pulled in when a run actually happens.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import httpx

from nightdesk.backends import opencode_config as ocfg
from nightdesk.backends.opencode_translate import new_state, translate_event, usage_by_model
from nightdesk.domain.cost import RunUsage, compute_cost
from nightdesk.transcript import next_seq, now_iso, write_event
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult


log = logging.getLogger(__name__)

_READY_TIMEOUT = 30.0
_READY_POLL = 0.25


def _base_url(req: ExecutionRequest) -> str:
    return f"http://127.0.0.1:{req.http_port}"


def _auth(req: ExecutionRequest) -> Optional[tuple[str, str]]:
    pw = req.env.get("OPENCODE_SERVER_PASSWORD")
    return ("opencode", pw) if pw else None


async def _wait_ready(client: httpx.AsyncClient, proc: asyncio.subprocess.Process) -> bool:
    deadline = asyncio.get_event_loop().time() + _READY_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        if proc.returncode is not None:
            return False
        try:
            r = await client.get("/global/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except (httpx.HTTPError, OSError):
            pass
        await asyncio.sleep(_READY_POLL)
    return False


def _usage_from_state(state: dict) -> Optional[RunUsage]:
    u = state.get("usage")
    if not u:
        return None
    tokens = u.get("tokens") or {}
    cache = tokens.get("cache") or {}
    model = u.get("model")
    input_t = int(tokens.get("input") or 0)
    output_t = int(tokens.get("output") or 0)
    cache_read = int(cache.get("read") or 0)
    cache_write = int(cache.get("write") or 0)
    # opencode reports a dollar cost directly; fall back to nightdesk pricing.
    cost = u.get("cost")
    if cost is None:
        cost = compute_cost(
            model=model, input_tokens=input_t, output_tokens=output_t,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
        )
    return RunUsage(
        model=model,
        input_tokens=input_t,
        output_tokens=output_t,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost,
    )


async def drive_opencode(req: ExecutionRequest) -> ExecutionResult:
    req.transcript_path.parent.mkdir(parents=True, exist_ok=True)
    seq = [0]
    with req.transcript_path.open("ab") as f:
        write_event(f, {
            "type": "meta", "ts": now_iso(), "seq": next_seq(seq),
            "ticket_id": req.ticket_id,
        })

    proc = await asyncio.create_subprocess_exec(
        *req.bwrap_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def _drain_logs() -> None:
        assert proc.stdout is not None
        async for _line in proc.stdout:
            pass  # server logs; swallow so the pipe never blocks

    log_task = asyncio.create_task(_drain_logs())
    workdir = str(req.working_dir)
    state = new_state()
    base = _base_url(req)
    auth = _auth(req)

    def _emit(events: list[dict]) -> None:
        if not events:
            return
        with req.transcript_path.open("ab") as fh:
            for e in events:
                e.setdefault("ts", now_iso())
                e.setdefault("seq", next_seq(seq))
                write_event(fh, e)

    async with httpx.AsyncClient(base_url=base, auth=auth) as client:
        ready = await _wait_ready(client, proc)
        if not ready:
            await _terminate(proc, log_task)
            msg = "opencode server failed to start"
            _emit([{"type": "worker_error", "kind": "startup", "summary": msg}])
            return ExecutionResult(exit_status="failed", error_summary=msg, pid=proc.pid)

        try:
            session_id = await _create_session(client, workdir)
            # Subscribe to the event stream BEFORE posting the prompt: opencode
            # can start emitting events the instant the prompt is accepted, and
            # posting first would race the subscribe, silently dropping any
            # events emitted in between.
            async with client.stream("GET", "/event", timeout=None) as resp:
                await _post_prompt(client, session_id, workdir, req)
                exit_status, error = await _consume_events(
                    client, resp, session_id, req, state, _emit,
                )
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            log.exception("opencode driver error for ticket %s", req.ticket_id)
            exit_status, error, session_id = "failed", f"opencode driver error: {exc}", None
            _emit([{"type": "worker_error", "kind": "driver", "summary": str(exc)}])

    await _terminate(proc, log_task)

    if exit_status == "cancelled":
        _emit([{"type": "cancelled", "message": "Run cancelled by user."}])

    final = state.get("final_text") or None
    session_ref = None
    if session_id:
        session_ref = {"session_id": session_id, "data_dir": req.env.get("XDG_DATA_HOME")}
    by_model = usage_by_model(state)
    return ExecutionResult(
        exit_status=exit_status,
        error_summary=error,
        pid=proc.pid,
        final_summary=final,
        usage=_usage_from_state(state),
        session_ref=session_ref,
        usage_by_model=by_model or None,
    )


async def _create_session(client: httpx.AsyncClient, workdir: str) -> str:
    r = await client.post("/session", params={"directory": workdir},
                          json={"title": "nightdesk run"}, timeout=30.0)
    r.raise_for_status()
    return r.json()["id"]


async def _post_text(
    client: httpx.AsyncClient, session_id: str, workdir: str,
    req: ExecutionRequest, text: str,
) -> None:
    """POST one user turn (the initial prompt or a mid-run follow-up) to the
    live session. Posting a follow-up before ``session.idle`` keeps the same
    session/run alive, so the agent picks it up at its next step boundary."""
    body = ocfg.build_prompt_body(
        text,
        system=getattr(req.permission_spec, "system_prompt", None),
        model=req.launch_meta.get("model"),
    )
    r = await client.post(
        f"/session/{session_id}/prompt_async",
        params={"directory": workdir}, json=body, timeout=30.0,
    )
    r.raise_for_status()


async def _post_prompt(
    client: httpx.AsyncClient, session_id: str, workdir: str, req: ExecutionRequest,
) -> None:
    await _post_text(client, session_id, workdir, req, req.prompt)


async def _deliver_steer(
    client: httpx.AsyncClient, session_id: str, req: ExecutionRequest,
    item: dict, emit,
) -> None:
    """Inject one queued follow-up into the live session: POST it, mark the DB
    row delivered (via the worker callback), and write the ``steer_delivered``
    breadcrumb through THIS driver's ``emit`` so the transcript seq stays
    single-owner (never via out-of-band ``append_event``)."""
    mid = item.get("id")
    body = item.get("body") or ""
    await _post_text(client, session_id, str(req.working_dir), req, body)
    if req.on_steer_delivered is not None:
        try:
            req.on_steer_delivered(mid, {"body": body, "delivery": "inject"})
        except Exception:
            log.exception("on_steer_delivered failed for steer message %s", mid)
    emit([{
        "type": "steer_delivered", "message_id": mid, "text": body,
        "delivery": "inject",
    }])


async def _consume_events(
    client: httpx.AsyncClient,
    resp: httpx.Response,
    session_id: str,
    req: ExecutionRequest,
    state: dict,
    emit,
) -> tuple[str, Optional[str]]:
    """Stream /event until our session goes idle. Returns (exit_status, error).

    Cancellation is raced against each line fetch with ``asyncio.wait`` rather
    than checked only when a new line arrives — a run that goes silent on the
    wire (server stalled, no more events) must still honor cancel promptly
    instead of hanging until the next SSE line (or forever).
    """
    cancel_task = asyncio.create_task(req.cancel_event.wait())
    line_iter = resp.aiter_lines()
    saw_idle = False
    # Mid-run steering. ``steer_task`` awaits the next queued follow-up (the host
    # watcher feeds the queue); it is raced against the event stream so a
    # follow-up is injected promptly, not only when a new SSE line happens to
    # arrive. ``delivered_since_idle`` keeps us from finishing on the idle that
    # closes a turn during which we injected a follow-up: opencode emits
    # session.idle per completed prompt, so after any injection we consume until
    # a CLEAN idle (queue empty, nothing injected since the previous idle) —
    # otherwise the just-injected follow-up would be torn down mid-work.
    steer_enabled = req.steer_queue is not None
    steer_task = (
        asyncio.create_task(req.steer_queue.get()) if steer_enabled else None
    )
    delivered_since_idle = False
    try:
        while True:
            next_task = asyncio.create_task(line_iter.__anext__())
            wait_set = {next_task, cancel_task}
            if steer_task is not None:
                wait_set.add(steer_task)
            done, _pending = await asyncio.wait(
                wait_set, return_when=asyncio.FIRST_COMPLETED,
            )

            # A queued follow-up won the race: inject it (unless the user is
            # cancelling), re-arm the queue wait, and process any line that also
            # arrived this tick before looping back.
            if steer_task is not None and steer_task in done:
                item = steer_task.result()
                steer_task = asyncio.create_task(req.steer_queue.get())
                if not cancel_task.done():
                    await _deliver_steer(client, session_id, req, item, emit)
                    delivered_since_idle = True
                if next_task not in done:
                    next_task.cancel()
                    try:
                        await next_task
                    except (asyncio.CancelledError, StopAsyncIteration, Exception):
                        pass
                    continue

            if next_task not in done:
                # Cancelled before the next line arrived (no steer item either).
                next_task.cancel()
                try:
                    await next_task
                except (asyncio.CancelledError, StopAsyncIteration, Exception):
                    pass
                await _abort(client, session_id)
                return "cancelled", None
            try:
                line = next_task.result()
            except StopAsyncIteration:
                break  # Server closed the stream.
            if cancel_task.done():
                await _abort(client, session_id)
                return "cancelled", None
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            props = evt.get("properties") or {}
            if props.get("sessionID") not in (None, session_id):
                continue
            emit(translate_event(evt, state))
            if evt.get("type") == "session.idle" and props.get("sessionID") == session_id:
                # Close the idle window on a just-arrived follow-up: drain any
                # already-queued messages now (get_nowait), and keep looping if
                # we injected anything during this turn. Only a clean idle
                # (queue empty, nothing injected since the last idle) finishes
                # the run. Residue the watcher hasn't queued yet lands on the
                # run-completion drain — one turn boundary lost, never a message.
                if steer_enabled:
                    while True:
                        try:
                            item = req.steer_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if not cancel_task.done():
                            await _deliver_steer(client, session_id, req, item, emit)
                            delivered_since_idle = True
                if delivered_since_idle:
                    delivered_since_idle = False
                    continue
                saw_idle = True
                break
    finally:
        for _tk in (cancel_task, steer_task):
            if _tk is not None and not _tk.done():
                _tk.cancel()
    if state.get("error"):
        return "failed", state["error"]
    if not saw_idle:
        # The stream ended (server closed it, crashed, or was killed) without
        # ever reporting our session idle or an explicit error — that is NOT
        # success; a truncated stream must not be reported as a clean finish.
        return "failed", "event stream ended unexpectedly"
    return "success", None


async def _abort(client: httpx.AsyncClient, session_id: str) -> None:
    try:
        await client.post(f"/session/{session_id}/abort", timeout=10.0)
    except (httpx.HTTPError, OSError):
        pass


async def _terminate(proc: asyncio.subprocess.Process, log_task: asyncio.Task) -> None:
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
    if not log_task.done():
        log_task.cancel()
        try:
            await log_task
        except (asyncio.CancelledError, Exception):
            pass
