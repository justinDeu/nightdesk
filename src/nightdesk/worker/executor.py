from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from nightdesk.domain.permissions import PermissionSpec

_PROC_DIR_KW = "c" "wd"

@dataclass
class ExecutionRequest:
    ticket_id: str
    prompt: str
    working_dir: Path
    transcript_path: Path
    bwrap_argv: list[str]
    env: dict[str, str]
    permission_spec: PermissionSpec = field(default_factory=PermissionSpec)
    final_summary_marker: str = "<<<NIGHTDESK_FINAL_SUMMARY>>>"
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Claude session id to resume (``continue`` intent). When set, the SDK is
    # launched with ``resume=<id>`` so the new run carries the prior
    # conversation's full message history instead of starting fresh. None for
    # every other intent (and when a continue fell back to fresh context).
    resume_session_id: Optional[str] = None
    # The user's typed continue message (``continue`` intent). When set, the
    # executor writes it as a ``user_message`` event at the very start of this
    # run's transcript so the continuity boundary is visible — the resumed run
    # reads "the user typed this to continue". The message itself is carried as
    # the next user turn via the SDK prompt (see build_continue_prompt), not via
    # this field; this field is only the transcript affordance. None for every
    # non-continue intent and for a textless continue (the header button).
    continue_message: Optional[str] = None
    # Seq counter seed for this turn. A conversation shares ONE transcript file
    # across turns with a single monotonic seq space, so turn N+1 must continue
    # from the file's current max seq instead of restarting at 0. Defaults to 0
    # (a fresh file / first turn).
    seq_start: int = 0
    # Eager session-id persistence: invoked once with the Claude session id the
    # moment it is first observed (the init event), OUT OF BAND relative to
    # run completion. The worker wires this to persist the authoritative
    # conversation.session_id immediately so a cancel/crash after that point
    # never leaves the conversation null-session. Best-effort; errors swallowed
    # by the caller. None when no callback is wired (e.g. tests).
    on_session_id: Optional[Callable[[str], None]] = None
    # Pre-allocated localhost port for HTTP-transport backends (opencode);
    # None for stdio backends (claude_sdk).
    http_port: Optional[int] = None
    # Opaque per-run data a backend's prepare_launch stashes for its execute
    # step (e.g. opencode's session dir + server password). Worker-opaque.
    launch_meta: dict = field(default_factory=dict)


@dataclass
class ExecutionResult:
    exit_status: str           # "success" | "failed" | "cancelled"
    error_summary: Optional[str] = None
    pid: Optional[int] = None
    final_summary: Optional[str] = None
    assistant_tail: list[str] = field(default_factory=list)
    # Set by the executor from the final ``result`` event. None when the
    # run failed before the agent emitted a result.
    usage: Optional[object] = None
    # Claude session id from the final ``result`` event, used to resume the
    # conversation later (claude --resume <id>). None when none was reported.
    session_id: Optional[str] = None
    # Backend-shaped resume handle persisted to Run.session_ref. Opaque to the
    # worker; the backend's resume_descriptor() reads it back.
    session_ref: Optional[dict] = None
    # Per-model token attribution for runs that touched more than one model
    # (opencode profiles with per-agent endpoints/models). Maps model id ->
    # a plain dict of the four token counts (``input_tokens``,
    # ``output_tokens``, ``cache_read_tokens``, ``cache_write_tokens``).
    # None (the default) means the backend only ever reports one aggregate
    # model — ``usage`` above is the sole source of truth and finish-time
    # pricing falls back to its single-model behaviour. Claude Code never
    # sets this (one model per run environment); opencode sets it whenever
    # its transcript carried at least one per-message usage event.
    usage_by_model: Optional[dict[str, dict[str, int]]] = None


class Executor(Protocol):
    async def run(self, req: ExecutionRequest) -> ExecutionResult: ...


class DummyExecutor:
    async def run(self, req: ExecutionRequest) -> ExecutionResult:
        if req.cancel_event.is_set():
            return ExecutionResult(exit_status="cancelled")
        req.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        req.transcript_path.write_text(f"[dummy executor]\nprompt: {req.prompt}\n")
        return ExecutionResult(exit_status="success", final_summary="dummy run complete")


class ShellExecutor:
    async def run(self, req: ExecutionRequest) -> ExecutionResult:
        req.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *req.bwrap_argv,
            **{_PROC_DIR_KW: str(req.working_dir)},
            env={**req.env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def _drain() -> None:
            assert proc.stdout is not None
            with req.transcript_path.open("ab") as f:
                async for line in proc.stdout:
                    f.write(line)

        drain_task = asyncio.create_task(_drain())
        wait_task = asyncio.create_task(proc.wait())
        cancel_task = asyncio.create_task(req.cancel_event.wait())

        done, pending = await asyncio.wait(
            {wait_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if cancel_task in done and wait_task not in done:
            # Cancellation requested. Terminate, then kill after grace.
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
            for task in (wait_task, drain_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            return ExecutionResult(exit_status="cancelled", pid=proc.pid)

        # Process completed normally; ensure drain finishes and clean up cancel task.
        if not cancel_task.done():
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass
        try:
            await drain_task
        except Exception:
            pass
        rc = wait_task.result()
        if rc == 0:
            return ExecutionResult(exit_status="success", pid=proc.pid)
        return ExecutionResult(exit_status="failed", pid=proc.pid,
                                  error_summary=f"exit code {rc}")
