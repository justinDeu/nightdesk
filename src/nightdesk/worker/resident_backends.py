"""Resident backend seam: how the host spawns and talks to the inner agent.

v1 ships ``ClaudeResidentBackend`` only (a child running
``nightdesk.worker._session_runner``). The ``posture`` argument reserves the
deferred sandboxed posture — only the argv prefix changes; the stdin/stdout
NDJSON control protocol is identical, so the host never branches on it.

``opencode`` is deferred entirely (assessed in resident-agents-v3.md §10): it has
no ``can_use_tool`` / ``AskUserQuestion`` / ``ExitPlanMode``, so the needs-input
spine has no counterpart. The frozen ``Session.backend`` keeps it clean to add.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Protocol


log = logging.getLogger(__name__)


@dataclass
class StartSpec:
    """Everything the inner needs to boot. Serialized as the ``init`` line."""

    working_dir: str
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    system_prompt: Optional[str] = None
    resume: Optional[str] = None
    allowed_tools: list = field(default_factory=list)
    disallowed_tools: list = field(default_factory=list)
    cli_path: Optional[str] = None
    setting_sources: Optional[list] = None
    # Env merged over the host environment before exec (trusted posture).
    env: dict = field(default_factory=dict)

    def init_line(self) -> dict:
        d: dict[str, Any] = {"type": "init", "working_dir": self.working_dir}
        for key in ("model", "permission_mode", "system_prompt", "resume",
                    "allowed_tools", "disallowed_tools", "cli_path",
                    "setting_sources"):
            v = getattr(self, key)
            if v:
                d[key] = v
        return d


class ResidentHandle(Protocol):
    """A running inner process the host drives over NDJSON."""

    pid: Optional[int]

    async def send(self, obj: dict) -> None: ...
    def events(self) -> AsyncIterator[dict]: ...
    async def close(self) -> None: ...


class ResidentBackend(Protocol):
    async def start(self, spec: StartSpec, posture: str) -> ResidentHandle: ...


class _SubprocHandle:
    """Handle over a real inner subprocess (stdin lines in, stdout events out)."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.pid = proc.pid

    async def send(self, obj: dict) -> None:
        if self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def events(self) -> AsyncIterator[dict]:
        assert self._proc.stdout is not None
        buf = bytearray()
        while True:
            chunk = await self._proc.stdout.read(65536)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                raw = bytes(buf[:nl])
                del buf[: nl + 1]
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    evt = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(evt, dict):
                    yield evt

    async def close(self) -> None:
        try:
            await self.send({"type": "shutdown"})
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass


class ClaudeResidentBackend:
    """Spawns ``python -m nightdesk.worker._session_runner`` as the inner.

    Trusted posture: a plain child of the host with the merged env, no bwrap.
    The host writes the DB and is never sandboxed; the inner reads the owner's
    real ``~/.claude`` by design.
    """

    async def start(self, spec: StartSpec, posture: str = "trusted") -> ResidentHandle:
        import os

        argv = [sys.executable, "-m", "nightdesk.worker._session_runner"]
        # Trusted posture: merge the agent's env over the host environment.
        child_env = dict(os.environ)
        child_env.update({k: str(v) for k, v in (spec.env or {}).items()})
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=child_env,
        )
        handle = _SubprocHandle(proc)
        await handle.send(spec.init_line())
        return handle
