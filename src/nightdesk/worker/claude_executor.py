# src/nightdesk/worker/claude_executor.py
"""ClaudeExecutor: spawns the Claude Agent SDK in a sandboxed subprocess.

The executor does not import `claude_agent_sdk` itself. Instead it runs the
bwrap argv produced by `build_bwrap_argv` whose inner command is
`python -m nightdesk.worker._sdk_runner`. The runner imports the SDK inside the
sandbox, executes the query, and emits one JSON event per line on stdout.

This module is importable without the SDK installed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from nightdesk.transcript import next_seq, now_iso, write_event
from nightdesk.worker.claude_translator import translate
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult
from nightdesk.worker.sandbox import SANDBOX_CLAUDE_BIN


log = logging.getLogger(__name__)


async def _spawn_sdk_subprocess(argv: list[str]) -> asyncio.subprocess.Process:
    """Indirection point so tests can patch the spawn step."""
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


class ClaudeExecutor:
    def __init__(self, *, model: Optional[str] = None,
                  allowed_tools: Optional[list[str]] = None):
        # These act as fallbacks if the request's permission_spec doesn't carry
        # values. In production the WorkerLoop always supplies a merged spec.
        self.model = model
        self.allowed_tools = allowed_tools or []

    def _build_runner_spec(self, req: ExecutionRequest) -> dict[str, Any]:
        spec = req.permission_spec
        allowed = list(spec.allowed_tools) if spec.allowed_tools else list(self.allowed_tools)
        disallowed = list(spec.denied_tools)
        model = spec.default_model or self.model
        permission_mode = getattr(spec, "permission_mode", None)  # None -> SDK default
        return {
            "prompt": req.prompt,
            "cwd": str(req.cwd),
            "allowed_tools": allowed,
            "disallowed_tools": disallowed,
            "model": model,
            "permission_mode": permission_mode,
            # Bypass PATH lookup — the binary is bind-mounted at a known path
            # inside the sandbox (see worker.sandbox.build_bwrap_argv).
            "cli_path": SANDBOX_CLAUDE_BIN,
        }

    async def run(self, req: ExecutionRequest) -> ExecutionResult:
        req.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        argv = list(req.bwrap_argv) if req.bwrap_argv else [
            "python", "-m", "nightdesk.worker._sdk_runner",
        ]
        runner_spec = self._build_runner_spec(req)

        proc = await _spawn_sdk_subprocess(argv)
        assert proc.stdin is not None and proc.stdout is not None

        try:
            proc.stdin.write(json.dumps(runner_spec).encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

        final: Optional[str] = None
        exit_status = "success"
        error: Optional[str] = None
        last_result_event: Optional[dict[str, Any]] = None
        assistant_tail: list[str] = []

        seq_counter: list[int] = [0]

        async def _drain() -> None:
            nonlocal final, exit_status, error, last_result_event, assistant_tail
            assert proc.stdout is not None
            buf = bytearray()
            with req.transcript_path.open("ab") as f:
                # Always emit a 'meta' header so the renderer can identify the
                # transcript as canonical even before any agent output.
                write_event(f, {
                    "type": "meta", "ts": now_iso(),
                    "seq": next_seq(seq_counter),
                    "ticket_id": req.ticket_id,
                })
                # Chunked read so a single oversized JSON event can't trip
                # asyncio's StreamReader line limit (default 64 KiB).
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        raw = bytes(buf[:nl])
                        del buf[: nl + 1]
                        await _handle_line(f, raw)
                if buf:
                    await _handle_line(f, bytes(buf))
                    buf.clear()

        async def _handle_line(f, line: bytes) -> None:
            nonlocal final, exit_status, error, last_result_event, assistant_tail
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                return
            try:
                evt = json.loads(text)
            except json.JSONDecodeError:
                # Treat as opaque assistant text.
                write_event(f, {
                    "type": "assistant_text", "ts": now_iso(),
                    "seq": next_seq(seq_counter),
                    "text": text,
                })
                return
            if not isinstance(evt, dict):
                write_event(f, {
                    "type": "assistant_text", "ts": now_iso(),
                    "seq": next_seq(seq_counter),
                    "text": text,
                })
                return
            # Capture run-completion summary before translation drops it.
            if evt.get("type") == "result":
                last_result_event = evt
                if evt.get("subtype") == "success":
                    final = evt.get("result")
                else:
                    exit_status = "failed"
                    error = evt.get("result") or "agent reported failure"
            try:
                canonical = translate(evt)
            except Exception as exc:
                log.exception("translator failed for ticket %s", req.ticket_id)
                canonical = [{
                    "type": "assistant_text",
                    "text": f"<translator error: {exc}> {text}",
                }]
            if not canonical:
                return
            for c in canonical:
                if c.get("type") == "assistant_text":
                    assistant_text = str(c.get("text") or "").strip()
                    if assistant_text:
                        assistant_tail.append(assistant_text)
                        assistant_tail[:] = assistant_tail[-8:]
                c.setdefault("ts", now_iso())
                c.setdefault("seq", next_seq(seq_counter))
                write_event(f, c)

        drain_task = asyncio.create_task(_drain())
        wait_task = asyncio.create_task(proc.wait())
        cancel_task = asyncio.create_task(req.cancel_event.wait())

        done, _ = await asyncio.wait(
            {wait_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if cancel_task in done and wait_task not in done:
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
            for t in (wait_task, drain_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            return ExecutionResult(exit_status="cancelled", pid=proc.pid)

        # Normal completion path.
        if not cancel_task.done():
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass
        try:
            await drain_task
        except Exception as exc:
            log.exception("transcript drain failed for ticket %s", req.ticket_id)
            exit_status = "failed"
            error = error or f"drain error: {exc}"

        rc = wait_task.result()
        if rc != 0 and exit_status == "success":
            exit_status = "failed"
            error = error or f"sdk runner exited {rc}"

        usage = None
        if last_result_event is not None:
            try:
                from nightdesk.domain.cost import extract_usage
                usage = extract_usage(last_result_event, model_hint=req.permission_spec.default_model)
            except Exception:
                log.exception("usage extraction failed for ticket %s", req.ticket_id)

        return ExecutionResult(
            exit_status=exit_status,
            pid=proc.pid,
            error_summary=error,
            final_summary=final,
            assistant_tail=assistant_tail,
            usage=usage,
        )
