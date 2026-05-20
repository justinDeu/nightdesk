from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from nightdesk.domain.permissions import PermissionSpec


@dataclass
class ExecutionRequest:
    ticket_id: str
    prompt: str
    cwd: Path
    transcript_path: Path
    bwrap_argv: list[str]
    env: dict[str, str]
    permission_spec: PermissionSpec = field(default_factory=PermissionSpec)
    final_summary_marker: str = "<<<NIGHTDESK_FINAL_SUMMARY>>>"
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class ExecutionResult:
    exit_status: str           # "success" | "failed" | "cancelled"
    error_summary: Optional[str] = None
    pid: Optional[int] = None
    final_summary: Optional[str] = None
    assistant_tail: list[str] = field(default_factory=list)
    # Set by claude executor from the final ``result`` event. None when the
    # run failed before the SDK emitted a result.
    usage: Optional[object] = None
    # Claude session id from the final ``result`` event, used to resume the
    # conversation later (claude --resume <id>). None when none was reported.
    session_id: Optional[str] = None


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
            cwd=str(req.cwd),
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
