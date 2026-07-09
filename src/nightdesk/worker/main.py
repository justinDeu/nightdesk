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
from nightdesk.domain.cron_jobs import materialize_due_cron_jobs
from nightdesk.domain.events import run_actor
from nightdesk.domain.tickets import transition_status
from nightdesk.worker.executor import Executor
from nightdesk.worker.heartbeat import recover_orphaned_runs, write_heartbeat


def _reconcile_executor_orphans(session, *, host: str) -> None:
    """Run each remote executor's orphan reconciliation (adopt/GC pods).

    Best-effort and self-guarding: an unconfigured k8s executor returns
    immediately, and any error is logged rather than allowed to wedge the tick.
    """
    from nightdesk.executors import available_executors, get_executor
    for code in available_executors():
        if code == "local":
            continue
        try:
            get_executor(code).reconcile_orphans(session, host=host)
        except Exception:
            log.exception("executor %s orphan reconcile failed (continuing)", code)
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
    # Admin bearer, used only to derive the ProfileSecretBox that decrypts
    # integration (GitLab) connection credentials for the linked-item refresh
    # pass. Empty disables that pass (no key to decrypt with).
    bearer_token: str = ""


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
        # Detached per-agent session-host subprocesses, keyed by session id.
        self._session_procs: dict[str, asyncio.subprocess.Process] = {}
        # Rate-limit the "cc check not ok" log to once per minute.
        self._cc_warn_at: float = 0.0
        # Monotonic deadline for the next linked-item (external_links) refresh.
        self._next_link_refresh_at: float = 0.0

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
    # Resident interactive agents: per-agent host supervision.
    # ------------------------------------------------------------------
    def _find_session_bin(self) -> list[str]:
        found = shutil.which("nightdesk-session")
        if found:
            return [found]
        return [sys.executable, "-m", "nightdesk.worker.session_host"]

    async def _spawn_session_host(self, session_id: str) -> None:
        argv = self._find_session_bin() + [session_id]
        log.info("spawning session host: %s", " ".join(argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            log.exception("failed to spawn session host for %s", session_id)
            return
        self._session_procs[session_id] = proc
        try:
            await proc.wait()
        finally:
            self._session_procs.pop(session_id, None)

    def _session_supervisor_pass(self) -> None:
        """One supervision pass over resident agents: sweep crashes, spawn hosts
        for cold agents with queued work (under the live cap), evict LRU over cap."""
        from nightdesk.worker import session_reaper as reaper

        session = self._session_factory()
        to_spawn: list[str] = []
        to_evict = []
        try:
            try:
                reaper.orphan_sweep(session, host=self.settings.host)
            except Exception:
                log.exception("agent orphan sweep failed (continuing)")

            from nightdesk.db.models import ConfigRow
            cfg = session.get(ConfigRow, 1)
            cap = int(cfg.max_live_sessions) if cfg and cfg.max_live_sessions else 4
            live = reaper.live_sessions(session)
            free = max(0, cap - len(live))
            needing = reaper.sessions_needing_host(session)
            # Don't double-spawn one we already have a tracked proc for.
            needing = [r for r in needing if r.id not in self._session_procs]
            for row in needing[:free]:
                to_spawn.append(row.id)
            to_evict = reaper.over_cap_evictions(session)
        finally:
            session.close()

        for sid in to_spawn:
            asyncio.create_task(self._spawn_session_host(sid))
        for row in to_evict:
            reaper.evict(row)

    # ------------------------------------------------------------------
    # Linked-item (external_links) refresh — GitLab integration.
    # ------------------------------------------------------------------
    def _maybe_refresh_links(self, session: Session) -> None:
        """Every ~5 min, refresh cached state on external links of non-archived
        tickets. Best-effort: any failure logs and never wedges the tick."""
        if not self.settings.bearer_token:
            return
        now = _time.monotonic()
        if now < self._next_link_refresh_at:
            return
        from nightdesk.domain import integrations as ints
        from nightdesk.domain.profile_secrets import ProfileSecretBox

        self._next_link_refresh_at = now + ints.LINKED_REFRESH_INTERVAL_SECONDS
        try:
            box = ProfileSecretBox(self.settings.bearer_token)
            summary = ints.refresh_all_links(session, box)
            if summary.checked:
                log.info(
                    "integration refresh: checked=%d updated=%d errors=%d changed=%d",
                    summary.checked, summary.updated, summary.errors, len(summary.changes),
                )
        except Exception:
            log.exception("integration link refresh failed (continuing)")

    # ------------------------------------------------------------------
    # Scheduler tick.
    # ------------------------------------------------------------------
    async def tick_once(self) -> None:
        session = self._session_factory()
        try:
            write_heartbeat(session, host=self.settings.host, pid=os.getpid())

            # Materialize due cron jobs BEFORE any readiness gating. This only
            # inserts queued ticket rows and brings up no backend, so due cron
            # jobs become visible queued tickets even when a backend is down;
            # they then wait until the worker can run them. Keeping it ahead of
            # the gate holds for today's global CC check AND for the per-backend
            # gates that replace it later.
            try:
                materialize_due_cron_jobs(session, datetime.now(timezone.utc))
            except Exception:
                log.exception("cron materialization failed (continuing)")

            # Refresh linked GitLab issues/MRs (external_links) on a slow cadence,
            # independent of the CC readiness gate below — integration freshness
            # doesn't need a working agent backend. Self-throttled and guarded.
            self._maybe_refresh_links(session)

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

            # Remote executors (k8s) own their own orphan reconciliation: the
            # pid-liveness sweep above can't see pods. No-op when k8s isn't
            # configured (reconcile swallows the config error).
            _reconcile_executor_orphans(session, host=self.settings.host)

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
                # Scheduler pick is a system/run action, never a human one — a
                # 'run' actor so this never spuriously acknowledges the ticket.
                transition_status(session, t.id, "running", actor=run_actor(None))
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

        # Resident-agent supervision: spawn/evict/sweep per-agent hosts. Runs
        # every tick, independent of ticket picks (and of the CC gate above,
        # which returns early — but agents run on the trusted posture with their
        # own hosts, so they are supervised here whenever the tick reaches this
        # point).
        try:
            self._session_supervisor_pass()
        except Exception:
            log.exception("agent supervisor pass failed (continuing)")

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
        if not os.environ.get("SSH_AUTH_SOCK"):
            # No ssh-agent in the worker's environment: SSH_AUTH_SOCK forwarding
            # in run_one._build_env will no-op, so any ticket that pushes/fetches
            # over SSH (e.g. a self-hosted GitLab remote) will fail to
            # authenticate. Surface it once at startup so the cause is obvious.
            log.info(
                "SSH_AUTH_SOCK not set in the worker environment: sandboxed git "
                "over SSH will not authenticate. Start the worker from a session "
                "that has an ssh-agent if your tickets use SSH remotes."
            )
        session = self._session_factory()
        try:
            recover_orphaned_runs(session, host=self.settings.host)
            _reconcile_executor_orphans(session, host=self.settings.host)
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
        # SIGUSR1 wake nudge: the API raises it after enqueuing on a cold agent
        # (or ticket run_now) so the loop breaks its tick sleep and spawns a host
        # immediately instead of waiting up to tick_seconds. A latency
        # optimization, never a correctness dependency — the next plain tick
        # would spawn it anyway.
        wake_event = asyncio.Event()

        def _shutdown(*_):
            shutdown_event.set()
            for proc in list(self._subprocs.values()):
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass

        def _wake(*_):
            wake_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except (NotImplementedError, ValueError):
                pass
        try:
            loop.add_signal_handler(signal.SIGUSR1, _wake)
        except (NotImplementedError, ValueError, AttributeError):
            pass

        log.info("nightdesk worker ready; entering tick loop")
        while not shutdown_event.is_set():
            await self.tick_once()
            wake_event.clear()
            waiters = [asyncio.create_task(shutdown_event.wait()),
                       asyncio.create_task(wake_event.wait())]
            try:
                done, pending = await asyncio.wait(
                    waiters, timeout=self.settings.tick_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for w in waiters:
                    if not w.done():
                        w.cancel()

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
