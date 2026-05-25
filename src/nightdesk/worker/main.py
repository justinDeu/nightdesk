from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import sys
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nightdesk.db.models import DaemonStatus, Run, Ticket
from nightdesk.domain.tickets import transition_status
from nightdesk.worker.executor import Executor
from nightdesk.worker.heartbeat import recover_orphaned_runs, write_heartbeat
from nightdesk.worker.scheduler import pick_eligible


log = logging.getLogger(__name__)


@dataclass
class WorkerSettings:
    max_parallel: int
    window_start: time
    window_end: time
    worktree_root: Path
    transcript_root: Path
    secrets: dict[str, str]
    host: str
    # Test/legacy override: when set, the daemon stops spawning subprocesses
    # and runs ``run_one`` in-process with this Executor injected.
    # Production code should leave this as None so each pick becomes a
    # ``nightdesk-run-ticket`` subprocess that the daemon supervises.
    executor: Optional[Executor] = None
    tick_seconds: float = 5.0


def _find_run_ticket_bin() -> list[str]:
    """Locate the ``nightdesk-run-ticket`` entry point.

    Prefer the console script when it's on PATH so the subprocess uses
    the same install the daemon is running from; fall back to
    ``python -m nightdesk._run_ticket_cli`` for unusual environments
    (zipped installs, tests).
    """
    found = shutil.which("nightdesk-run-ticket")
    if found:
        return [found]
    return [sys.executable, "-m", "nightdesk._run_ticket_cli"]


class WorkerLoop:
    def __init__(self, session_factory: Callable[[], Session], settings: WorkerSettings):
        self._session_factory = session_factory
        self.settings = settings
        # In-process mode (tests): asyncio.Task per running ticket.
        # Subprocess mode (production): subprocess.Popen handle per ticket id.
        self._inproc: dict[str, asyncio.Task] = {}
        self._subprocs: dict[str, asyncio.subprocess.Process] = {}
        # Rate-limit the "cc check not ok" log to once per minute.
        self._cc_warn_at: float = 0.0

    # ------------------------------------------------------------------
    # In-process path (only used when settings.executor is provided —
    # primarily tests where we want a Dummy/Shell executor in the same
    # process so we can assert against transcript writes and DB state).
    # ------------------------------------------------------------------
    async def _run_inproc(self, ticket_id: str) -> None:
        from nightdesk.worker.run_one import RunOneConfig, run_one

        cfg = RunOneConfig(
            worktree_root=self.settings.worktree_root,
            transcript_root=self.settings.transcript_root,
            secrets=self.settings.secrets,
            host=self.settings.host,
            executor=self.settings.executor,
        )
        try:
            await run_one(self._session_factory, cfg, ticket_id)
        finally:
            self._inproc.pop(ticket_id, None)

    # ------------------------------------------------------------------
    # Subprocess path (production).
    # ------------------------------------------------------------------
    async def _spawn_subproc(self, ticket_id: str) -> None:
        argv = _find_run_ticket_bin() + [ticket_id]
        log.info("spawning run-ticket subprocess: %s", " ".join(argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            log.exception("failed to spawn run-ticket for %s", ticket_id)
            return
        self._subprocs[ticket_id] = proc
        try:
            # Drain stderr so the pipe buffer doesn't block the child. Lines
            # are logged at WARNING level since the subprocess only writes to
            # stderr on unexpected errors (tracebacks, unhandled exceptions).
            async def _drain_stderr(p: asyncio.subprocess.Process, tid: str) -> None:
                if p.stderr is None:
                    return
                while True:
                    line = await p.stderr.readline()
                    if not line:
                        break
                    log.warning("run-ticket %s stderr: %s",
                                tid, line.decode("utf-8", errors="replace").rstrip())

            drain_task = asyncio.create_task(_drain_stderr(proc, ticket_id))
            rc = await proc.wait()
            # Give the stderr drain a moment to flush remaining lines.
            try:
                await asyncio.wait_for(drain_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            if rc != 0:
                log.warning("run-ticket subprocess for %s exited rc=%s",
                            ticket_id, rc)
        finally:
            self._subprocs.pop(ticket_id, None)

    # ------------------------------------------------------------------
    # Cancellation supervision: if a ticket gets transitioned away from
    # running while the subprocess is still alive (cancel button, user
    # drag), send SIGTERM. The subprocess installs a handler that flips
    # its cancel_event and shuts the executor down cleanly.
    # ------------------------------------------------------------------
    async def _supervise_cancellations(self) -> None:
        session = self._session_factory()
        try:
            for tid, proc in list(self._subprocs.items()):
                if proc.returncode is not None:
                    continue
                t = session.get(Ticket, tid)
                if t is not None and t.status != "running":
                    log.info("ticket %s left running while subprocess alive; SIGTERM pid=%s",
                             tid, proc.pid)
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Scheduler tick.
    # ------------------------------------------------------------------
    async def tick_once(self) -> None:
        session = self._session_factory()
        try:
            write_heartbeat(session, host=self.settings.host, pid=os.getpid())

            # Gate: refuse new picks when the CC binary check has not passed.
            ds = session.get(DaemonStatus, 1)
            if ds is not None and ds.cc_check_status != "ok":
                now = _time.monotonic()
                if now >= self._cc_warn_at:
                    log.warning(
                        "CC check status is %r (%s); skipping tick until resolved.",
                        ds.cc_check_status,
                        ds.cc_check_message or "no message",
                    )
                    self._cc_warn_at = _time.monotonic() + 60.0
                return

            # Cheap orphan sweep every tick so wedged tickets self-heal
            # without a daemon restart. Pass 1 (crashed Runs on this host)
            # is a no-op when nothing crashed; pass 2 (tickets in 'running'
            # with no Run row) catches anything that ended up in 'running'
            # without going through the worker (legacy code paths, manual
            # DB pokes, race between API and worker pick).
            try:
                recover_orphaned_runs(session, host=self.settings.host)
            except Exception:
                log.exception("orphan sweep failed (continuing)")

            # Capacity is driven by actual unfinished Run rows so the count
            # survives restarts and isn't polluted by tickets stuck in
            # 'running' without a Run row (which orphan recovery resets to
            # 'queued' on startup). The window/capacity decision now comes from
            # ScheduleWindow rows, resolved inside pick_eligible.
            total_running = session.scalar(
                select(func.count()).select_from(Run).where(Run.finished_at.is_(None))
            ) or 0
            picks = pick_eligible(
                session,
                now=datetime.now(timezone.utc),
                total_running=total_running,
            )
            pick_ids: list[str] = []
            for t in picks:
                transition_status(session, t.id, "running")
                pick_ids.append(t.id)
        finally:
            session.close()

        for ticket_id in pick_ids:
            if self.settings.executor is not None:
                task = asyncio.create_task(self._run_inproc(ticket_id))
                self._inproc[ticket_id] = task
            else:
                # Fire-and-forget; the subprocess writes Run rows and
                # transitions the ticket itself. We keep the awaitable
                # only so we can SIGTERM on cancellation and reap on exit.
                asyncio.create_task(self._spawn_subproc(ticket_id))

        # Cancellation supervision runs every tick regardless of picks.
        if self._subprocs:
            await self._supervise_cancellations()

    async def run_forever(self) -> None:
        s = self.settings
        log.info(
            "nightdesk worker starting: host=%s pid=%d max_parallel=%d "
            "window=%s-%s tick=%.1fs mode=%s worktree_root=%s "
            "transcript_root=%s",
            s.host, os.getpid(), s.max_parallel,
            s.window_start.strftime("%H:%M"), s.window_end.strftime("%H:%M"),
            s.tick_seconds,
            "inproc" if s.executor is not None else "subprocess",
            s.worktree_root, s.transcript_root,
        )
        session = self._session_factory()
        try:
            recover_orphaned_runs(session, host=self.settings.host)
        finally:
            session.close()
        # Signals do two things: SIGTERM each child subprocess so users get a
        # clean shutdown via `systemctl stop nightdesk-worker`, AND set an
        # asyncio.Event so the main tick loop wakes from its sleep and exits.
        # Without the event, `add_signal_handler(SIGINT)` swallows Ctrl-C
        # (overrides Python's default KeyboardInterrupt) and the loop spins
        # forever; the only escape was kill -9.
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        def _shutdown(*_):
            shutdown_event.set()
            for proc in list(self._subprocs.values()):
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except (NotImplementedError, ValueError):
                pass

        log.info("nightdesk worker ready; entering tick loop")
        while not shutdown_event.is_set():
            await self.tick_once()
            try:
                await asyncio.wait_for(shutdown_event.wait(),
                                         timeout=self.settings.tick_seconds)
            except asyncio.TimeoutError:
                pass

        # Wait briefly for children we just SIGTERMed so the daemon doesn't
        # return while subprocesses are still mid-shutdown. Bounded so a
        # wedged child can't pin the daemon.
        if self._subprocs:
            log.info("shutdown: waiting for %d child(ren) to exit",
                     len(self._subprocs))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(p.wait() for p in list(self._subprocs.values())),
                                    return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                log.warning("shutdown: %d child(ren) still alive after 10s; "
                            "they will be reaped by the OS",
                            sum(1 for p in self._subprocs.values()
                                 if p.returncode is None))


def default_host() -> str:
    return socket.gethostname()
