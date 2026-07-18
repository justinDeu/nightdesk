"""Per-agent host process: ``nightdesk-session <id>``.

Spawned detached by the worker supervisor (like ``nightdesk-run-ticket``). It
owns the Session row, the inbox poll, the transcript file, per-turn usage
slicing, the resume handle, and the control-channel bridge to the inner
``_session_runner``. It is NEVER sandboxed (it writes the DB); in the trusted
posture the inner is a plain child with the merged env.

Lifecycle (resident-agents-v3.md §5):
  claim row (WHERE host_pid IS NULL) -> decrypt+merge env -> spawn+connect inner
  -> stream turns, bridge needs-input -> idle-reap / restart / end.

Two concurrent tasks:
  - reader: drains inner events (transcript events -> file; control events ->
    PendingInput rows / usage slices / turn-complete signal).
  - main loop: polls the inbox, claims turns, delivers them, self-reaps on the
    live-evaluated idle timeout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from nightdesk.backends import opencode_config as ocfg
from nightdesk.config import NightdeskConfig, load_config
from nightdesk.db.models import ConfigRow, Profile, Session, SessionTurn
from nightdesk.domain import sessions as sess
from nightdesk.domain import session_import as simp
from nightdesk.domain.backend_capabilities import OPENCODE
from nightdesk.domain.model_assignment import compute_model_assignments
from nightdesk.domain.permissions import PermissionSpec
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.providers import resolve_endpoints_for_profile
from nightdesk.transcript import (
    append_event,
    next_transcript_seq,
    now_iso,
    write_event,
)
from nightdesk.worker._session_runner import CONTROL_EVENT_TYPES
from nightdesk.worker.resident_backends import (
    StartSpec,
    resident_backend_for,
    translator_for,
)


log = logging.getLogger(__name__)

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionHost:
    def __init__(
        self,
        *,
        session_factory,
        session_id: str,
        backend=None,
        box: Optional[ProfileSecretBox] = None,
        config: Optional[NightdeskConfig] = None,
        host: str = "localhost",
        poll_interval: float = 0.2,
        watchdog_grace: float = 8.0,
    ) -> None:
        self._sf = session_factory
        self._sid = session_id
        # None -> resolved from the row's frozen ``backend`` at boot (see
        # ``run()``); an explicit override (tests) always wins.
        self._backend = backend
        self._config = config or load_config()
        self._box = box or ProfileSecretBox(self._config.bearer_token)
        self._host = host
        self._poll = poll_interval
        # Grace after a watchdog interrupt() before declaring the inner
        # unresponsive and tearing down (crashed -> idle, turn failed).
        self._watchdog_grace = watchdog_grace

        # Resolved at boot from row.backend: which resident runtime this
        # generation is (never changes across restarts — Session.backend is
        # frozen at create time) and its transcript-translate step.
        self._backend_code = "claude"
        self._translate = translator_for(self._backend_code)

        self._handle: Any = None
        self._transcript_path: Optional[Path] = None
        self._seq = [0]
        # Per-generation cumulative usage baseline for turn slicing.
        self._prev_cumulative: dict[str, int] = {}
        self._prev_cost = 0.0
        self._first_turn_of_resumed_gen = False
        # Signalling between reader and main loop.
        self._turn_complete: dict[str, dict] = {}
        self._turn_done_evt = asyncio.Event()
        self._streaming_turn_id: Optional[str] = None
        self._shutdown = False
        self._shutdown_status = "idle"

    # ------------------------------------------------------------------
    # Boot / claim
    # ------------------------------------------------------------------
    def _build_spec(self, db, row: Session, env: dict, resume: Optional[str]) -> StartSpec:
        if row.backend == "opencode":
            return self._build_opencode_spec(db, row, env, resume)
        return StartSpec(
            working_dir=row.source_path,
            model=row.model,
            resume=resume,
            env=env,
            # Trusted posture loads the owner's real ~/.claude.
            setting_sources=["project", "user"],
        )

    def _build_opencode_spec(self, db, row: Session, env: dict,
                             resume: Optional[str]) -> StartSpec:
        """Render the opencode config/auth the same way a ticket run does
        (``domain.model_assignment.compute_model_assignments`` +
        ``backends.opencode_config``) and inject it via env, mirroring how
        ``backends.opencode.OpencodeBackend.prepare_launch`` wires the
        sandboxed ticket path — trusted resident posture just skips the
        bwrap/mount machinery, there is no sandbox here.
        """
        profile = db.get(Profile, row.profile_id) if row.profile_id else None
        backend_config = dict(getattr(profile, "backend_config", None) or {})
        endpoints = (
            resolve_endpoints_for_profile(db, profile, self._box)
            if profile is not None else {}
        )
        primary = (
            endpoints.get(profile.endpoint_id)
            if profile is not None and profile.endpoint_id else None
        )
        default_model = (
            row.model
            or (profile.default_model if profile is not None else None)
            or (primary.default_model if primary is not None else None)
        )
        assignments = compute_model_assignments(
            OPENCODE, backend_config, primary=primary, default_model=default_model,
        )
        system_prompt = getattr(profile, "system_prompt", None) if profile is not None else None
        perm_spec = PermissionSpec(
            allowed_tools=list(profile.allowed_tools) if profile is not None else [],
            denied_tools=list(profile.denied_tools) if profile is not None else [],
            default_model=default_model,
            backend=OPENCODE.code,
            backend_config=backend_config,
            system_prompt=system_prompt,
        )
        config = ocfg.render_config(perm_spec, endpoints, assignments)
        auth = ocfg.render_auth(endpoints, assignments)

        provider_ids = ocfg.resolve_provider_ids(endpoints, assignments)
        primary_assignment = assignments.get("primary")
        model = (
            ocfg.model_str(primary_assignment, provider_ids)
            if primary_assignment is not None else None
        )

        oc_env = dict(env)
        oc_env.update({
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            # Fail-closed network/footprint posture, mirroring the ticket
            # path's env (see backends/opencode.py) — a resident agent is
            # still unattended between turns.
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_SHARE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",  # don't pick up host CLAUDE.md
        })
        if auth is not None:
            oc_env["OPENCODE_AUTH_CONTENT"] = json.dumps(auth)

        return StartSpec(
            working_dir=row.source_path,
            model=model,
            system_prompt=system_prompt,
            resume=resume,
            env=oc_env,
        )

    async def run(self) -> int:
        pid = os.getpid()
        with self._sf() as db:
            if not sess.claim_host(db, self._sid, host=self._host, pid=pid):
                log.info("session %s already claimed; exiting", self._sid)
                return 0
            row = sess.get_session_row(db, self._sid)
            self._transcript_path = Path(row.transcript_path)
            self._backend_code = row.backend
            self._translate = translator_for(row.backend)
            if self._backend is None:
                self._backend = resident_backend_for(row.backend)
            try:
                env = sess.decrypt_env_for_spawn(row, self._box)
            except ValueError as exc:
                # Secret unreadable (bearer rotated): fail cleanly, stay cold.
                self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
                self._write_transcript({"type": "worker_error",
                                        "kind": "env_secret_unreadable",
                                        "summary": str(exc)})
                sess.release_host(db, self._sid, status="idle")
                return 1
            resume = (row.resume_handle or {}).get("session_id")
            source_path = row.source_path
            spec = self._build_spec(db, row, env, resume)

        self._transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = [next_transcript_seq(self._transcript_path)]
        self._reset_generation(resumed=bool(resume))
        self._write_transcript({"type": "session_booting"})

        # Best-effort: import any turns made in a terminal round-trip past our
        # last-written jsonl entry, then the resident resumes the same id.
        # Claude-only: the CC session jsonl this replays has no opencode
        # counterpart (cc_session_jsonl would just never resolve a path for
        # an opencode session id, but skip the lookup outright for clarity).
        if resume and self._backend_code == "claude":
            try:
                self._delta_import(source_path, resume)
            except Exception:  # noqa: BLE001
                log.exception("delta-import failed for session %s", self._sid)

        self._install_signals()
        self._handle = await self._backend.start(spec, "trusted")
        reader = asyncio.create_task(self._read_events())
        try:
            await self._main_loop()
        finally:
            await self._teardown(reader)
        return 0

    def _install_signals(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def _term(*_):
            # Graceful evict/stop: reap to idle (resume armed).
            self._shutdown = True
            self._shutdown_status = "idle"
            self._turn_done_evt.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _term)
            except (NotImplementedError, ValueError):
                pass

    # ------------------------------------------------------------------
    # Reader: inner events -> transcript + control
    # ------------------------------------------------------------------
    async def _read_events(self) -> None:
        try:
            async for evt in self._handle.events():
                etype = evt.get("type")
                if etype in CONTROL_EVENT_TYPES:
                    self._handle_control(evt)
                else:
                    self._write_transcript_translated(evt)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("event reader failed for session %s", self._sid)

    def _handle_control(self, evt: dict) -> None:
        etype = evt.get("type")
        if etype == "pending_input":
            self._on_pending_input(evt)
        elif etype == "pending_resolved":
            with self._sf() as db:
                sess.resolve_pending(db, self._sid, evt.get("request_id"),
                                     status="answered")
        elif etype == "turn_complete":
            tid = evt.get("turn_id") or ""
            self._turn_complete[tid] = evt
            self._turn_done_evt.set()
        elif etype == "server_info":
            # Persisted as a breadcrumb so the composer (frontend phase) can read
            # slash commands/skills off the transcript SSE. Renderer ignores it.
            self._write_transcript({
                "type": "server_info",
                "commands": evt.get("commands") or [],
                "skills": evt.get("skills") or [],
            })

    def _on_pending_input(self, evt: dict) -> None:
        request_id = evt.get("request_id") or ""
        with self._sf() as db:
            row = sess.create_pending(
                db, self._sid,
                request_id=request_id,
                kind=str(evt.get("kind") or "permission"),
                tool=evt.get("tool"),
                payload=evt.get("payload") or {},
            )
        # A needs_input breadcrumb so the ask shows in transcript context.
        self._write_transcript({
            "type": "needs_input",
            "request_id": request_id,
            "kind": evt.get("kind"),
            "tool": evt.get("tool"),
            "payload": evt.get("payload") or {},
        })

    # ------------------------------------------------------------------
    # Main loop: inbox poll + self-reap
    # ------------------------------------------------------------------
    async def _main_loop(self) -> None:
        while not self._shutdown:
            turn = self._claim_next_turn()
            if turn is not None:
                await self._deliver(turn)
                continue
            if self._should_reap():
                self._shutdown = True
                self._shutdown_status = "idle"
                break
            try:
                await asyncio.wait_for(self._turn_done_evt.wait(), timeout=self._poll)
            except asyncio.TimeoutError:
                pass
            self._turn_done_evt.clear()

    def _claim_next_turn(self) -> Optional[SessionTurn]:
        """Claim the next deliverable turn.

        While a user turn is streaming, only control turns (interrupt/answer/
        restart) are deliverable — they must reach the inner mid-stream. Ended
        status short-circuits the loop.
        """
        with self._sf() as db:
            row = db.get(Session, self._sid)
            if row is None or row.status == "ended":
                self._shutdown = True
                self._shutdown_status = "ended" if row is not None else "idle"
                return None
            q = (
                db.query(SessionTurn)
                .filter(SessionTurn.session_id == self._sid,
                        SessionTurn.status == "queued")
                .order_by(SessionTurn.position.asc())
            )
            if self._streaming_turn_id is not None:
                q = q.filter(SessionTurn.kind != "user")
            turn = q.first()
            if turn is None:
                return None
            claimed = sess.claim_turn(db, turn.id)
            if claimed is not None:
                db.expunge(claimed)
            return claimed

    async def _deliver(self, turn: SessionTurn) -> None:
        kind = turn.kind
        if kind == "user":
            await self._deliver_user_turn(turn)
        elif kind == "answer":
            await self._handle.send({"type": "answer", **_decode(turn.body),
                                     "request_id": turn.ref_request_id})
            self._finish_control_turn(turn)
        elif kind == "interrupt":
            await self._handle.send({"type": "interrupt"})
            self._finish_control_turn(turn)
        elif kind == "restart":
            self._finish_control_turn(turn)
            body = _decode(turn.body)
            await self._restart_inner(force=bool(body.get("force")),
                                      clear_context=bool(body.get("clear_context")))
        elif kind == "wake":
            # No-op boot marker (wake endpoint / wake-on-create): its whole job
            # was making the supervisor spawn this host. The agent now idles
            # until a real turn or the idle timeout.
            self._finish_control_turn(turn)
        elif kind == "reap":
            # Explicit graceful reap: run the normal idle-reap flow now. Teardown
            # (in run()) disconnects the inner, publishes the resume handle, and
            # releases the host to idle.
            self._finish_control_turn(turn)
            self._shutdown = True
            self._shutdown_status = "idle"
            self._turn_done_evt.set()

    def _finish_control_turn(self, turn: SessionTurn) -> None:
        with self._sf() as db:
            row = db.get(SessionTurn, turn.id)
            if row is not None:
                row.status = "done"
                row.finished_at = _now()
                db.commit()

    async def _deliver_user_turn(self, turn: SessionTurn) -> None:
        self._streaming_turn_id = turn.id
        self._turn_complete.pop(turn.id, None)
        with self._sf() as db:
            row = db.get(SessionTurn, turn.id)
            if row is not None:
                row.status = "streaming"
                db.commit()
            srow = db.get(Session, self._sid)
            if srow is not None:
                srow.last_activity_at = _now()
                db.commit()
        # Persist the user's message before delivery: the inner runner never
        # echoes the prompt back, so this is the transcript's only record of
        # it. ``user_message`` matches the ticket-run continue event, which the
        # frontend already renders as a user bubble.
        self._write_transcript({"type": "user_message", "turn_id": turn.id,
                                "text": turn.body})
        await self._handle.send({"type": "user_turn", "turn_id": turn.id, "text": turn.body})

        # Wait for turn_complete (the reader signals it). Control turns delivered
        # meanwhile keep the loop responsive via a nested poll. A max_turn_seconds
        # watchdog (live-read, 0 disables) interrupts a runaway turn; if the
        # inner stays unresponsive past the grace window, we tear down
        # (crashed -> idle, turn failed) so the agent stays recoverable.
        t0 = time.monotonic()
        interrupted_at: Optional[float] = None
        watchdog_failed = False
        while turn.id not in self._turn_complete and not self._shutdown:
            max_s = self._max_turn_seconds()
            if max_s > 0:
                elapsed = time.monotonic() - t0
                if interrupted_at is None and elapsed > max_s:
                    log.warning("turn %s exceeded max_turn_seconds=%s; interrupting",
                                turn.id, max_s)
                    await self._handle.send({"type": "interrupt"})
                    interrupted_at = time.monotonic()
                elif interrupted_at is not None and \
                        (time.monotonic() - interrupted_at) > self._watchdog_grace:
                    log.warning("turn %s unresponsive after watchdog interrupt; "
                                "tearing down", turn.id)
                    watchdog_failed = True
                    self._shutdown = True
                    self._shutdown_status = "idle"  # recoverable; resume armed
                    break
            control = self._claim_next_turn()
            if control is not None and control.kind != "user":
                await self._deliver(control)
                continue
            try:
                await asyncio.wait_for(self._turn_done_evt.wait(), timeout=self._poll)
            except asyncio.TimeoutError:
                pass
            self._turn_done_evt.clear()

        tc = self._turn_complete.pop(turn.id, None)
        self._streaming_turn_id = None
        if tc is not None:
            self._slice_usage(turn.id, tc)
        elif watchdog_failed:
            # Inner never answered the watchdog interrupt: fail the turn and drop
            # a crash breadcrumb; teardown releases the host to idle.
            with self._sf() as db:
                row = db.get(SessionTurn, turn.id)
                if row is not None and row.status in ("streaming", "delivering"):
                    row.status = "failed"
                    row.error = "turn exceeded max_turn_seconds and did not respond to interrupt"
                    row.finished_at = _now()
                    db.commit()
            self._write_transcript({"type": "session_crashed"})
        else:
            # Shutdown before completion: mark interrupted.
            with self._sf() as db:
                row = db.get(SessionTurn, turn.id)
                if row is not None and row.status == "streaming":
                    row.status = "interrupted"
                    row.finished_at = _now()
                    db.commit()

    # ------------------------------------------------------------------
    # Usage slicing (cumulative deltas over the generation baseline)
    # ------------------------------------------------------------------
    def _reset_generation(self, *, resumed: bool) -> None:
        self._prev_cumulative = {f: 0 for f in _TOKEN_FIELDS}
        self._prev_cost = 0.0
        self._first_turn_of_resumed_gen = resumed

    def _slice_usage(self, turn_id: str, tc: dict) -> None:
        usage = tc.get("usage") or {}
        cur = {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(
                usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(
                usage.get("cache_creation_input_tokens") or usage.get("cache_write_tokens") or 0),
        }
        delta = {f: max(0, cur[f] - self._prev_cumulative.get(f, 0)) for f in _TOKEN_FIELDS}
        cur_cost = float(tc.get("cost_usd") or 0.0)
        cost_delta = max(0.0, cur_cost - self._prev_cost)
        includes_resume = self._first_turn_of_resumed_gen
        self._first_turn_of_resumed_gen = False
        self._prev_cumulative = cur
        self._prev_cost = cur_cost

        error = tc.get("error")
        with self._sf() as db:
            row = db.get(SessionTurn, turn_id)
            if row is not None:
                # A turn whose only outcome was a provider/session error (e.g.
                # opencode's ProviderAuthError on a misconfigured endpoint)
                # still ends via session.idle/turn_complete like any other —
                # without this it would read as a successful "done" with 0
                # tokens instead of surfacing the failure.
                row.status = "failed" if error else "done"
                if error:
                    row.error = str(error)
                row.finished_at = _now()
                for f in _TOKEN_FIELDS:
                    setattr(row, f, delta[f])
                row.cost_usd = cost_delta
                row.includes_resume = includes_resume
                db.commit()
            srow = db.get(Session, self._sid)
            if srow is not None:
                for f in _TOKEN_FIELDS:
                    setattr(srow, f, (getattr(srow, f) or 0) + delta[f])
                srow.cost_usd = (srow.cost_usd or 0.0) + cost_delta
                srow.last_activity_at = _now()
                sid = tc.get("session_id")
                if sid:
                    srow.resume_handle = {**(srow.resume_handle or {}), "session_id": str(sid)}
                db.commit()
                # Everything the CLI appended to its session jsonl during this
                # turn was ALSO streamed to us live and already written to the
                # transcript. Advance the delta-import watermark past it so the
                # next wake's _delta_import does not replay this turn's history
                # into the transcript again (only genuine terminal round-trips
                # made while the agent is cold land past the watermark).
                self._advance_import_watermark(db, srow)

    def _advance_import_watermark(self, db, srow: Session) -> None:
        """Mark the CC session jsonl as fully imported up to its current length.

        Called after live-streamed output lands in the transcript (per-turn and
        at teardown). Shared logic in ``domain.session_import`` — the API's
        sync-terminal endpoint uses the same watermark semantics.
        """
        simp.advance_import_watermark(db, srow)

    # ------------------------------------------------------------------
    # Restart (shared by explicit restart; a cold wake is a fresh host)
    # ------------------------------------------------------------------
    async def _restart_inner(self, *, force: bool, clear_context: bool = False) -> None:
        old = self._handle
        # Publish the resume handle, then re-decrypt env and respawn with resume.
        # ``clear_context`` respawns WITHOUT resume (fresh conversation context)
        # and drops the persisted handle — belt-and-braces on top of the
        # enqueue-time wipe in ``request_restart``, covering a turn that
        # completed (and re-published a session id) between enqueue and claim.
        with self._sf() as db:
            row = sess.get_session_row(db, self._sid)
            if clear_context:
                resume = None
                if row.resume_handle is not None:
                    row.resume_handle = None
                    db.commit()
            else:
                resume = (row.resume_handle or {}).get("session_id")
            try:
                env = sess.decrypt_env_for_spawn(row, self._box)
            except ValueError as exc:
                self._write_transcript({"type": "worker_error",
                                        "kind": "env_secret_unreadable",
                                        "summary": str(exc)})
                return
            spec = self._build_spec(db, row, env, resume)
        try:
            await old.close()
        except Exception:  # noqa: BLE001
            log.exception("closing inner before restart failed")
        self._reset_generation(resumed=bool(resume))
        self._handle = await self._backend.start(spec, "trusted")
        self._reader_restarted()
        evt: dict = {"type": "runtime_restarted"}
        if clear_context:
            evt["context_cleared"] = True
        self._write_transcript(evt)

    def _reader_restarted(self) -> None:
        # Rebind the reader to the new handle.
        self._reader = asyncio.create_task(self._read_events())

    # ------------------------------------------------------------------
    # Reap / teardown
    # ------------------------------------------------------------------
    def _max_turn_seconds(self) -> int:
        """Live-read the per-turn watchdog cap (0 disables). Never frozen."""
        with self._sf() as db:
            cfg = db.get(ConfigRow, 1)
            return int(cfg.max_turn_seconds) if cfg and cfg.max_turn_seconds else 0

    def _should_reap(self) -> bool:
        with self._sf() as db:
            row = db.get(Session, self._sid)
            if row is None:
                return True
            cfg = db.get(ConfigRow, 1)
            timeout = sess.effective_timeout(row, cfg)
            if sess.has_open_pending(db, self._sid):
                return False  # human is the blocker; never reap
            if self._streaming_turn_id is not None:
                return False
            if sess._queued_count(db, self._sid) > 0:
                return False
            idle = (_now() - _as_utc(row.last_activity_at)).total_seconds()
            return idle > timeout

    async def _teardown(self, reader: asyncio.Task) -> None:
        # Capture the live handle's inner session id BEFORE closing it. This
        # only matters for the opencode handle (``_OpencodeResidentHandle``
        # tracks ``session_id`` from ``_ensure_session``/construction; the
        # claude ``_SubprocHandle`` has no such attribute, so ``getattr``
        # below returns None and this is a no-op there — its session id only
        # ever arrives via a turn_complete event, same as before). Reading
        # first (rather than after close) means this stays correct even if
        # ``close()`` is ever changed to reset session state; today it only
        # aborts/cancels the live turn and does not touch ``session_id``.
        # ``self._handle`` is always the CURRENT generation's handle —
        # ``_restart_inner`` (including its ``clear_context`` path) replaces
        # it with the freshly spawned handle before returning, so teardown
        # never reads a stale handle's session id. A post-clear_context
        # handle's session_id is None until its own first turn creates a
        # session, so this can't resurrect a wiped conversation either.
        handle_session_id = None
        try:
            if self._handle is not None:
                handle_session_id = getattr(self._handle, "session_id", None)
                await self._handle.close()
        except Exception:  # noqa: BLE001
            log.exception("closing inner failed for session %s", self._sid)
        for task in (reader, getattr(self, "_reader", None)):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        with self._sf() as db:
            row = db.get(Session, self._sid)
            if row is not None:
                # Cover turns that ended without turn_complete (interrupted /
                # crashed): their streamed output is in the transcript, so the
                # next wake must not delta-import it again.
                self._advance_import_watermark(db, row)
            resume = row.resume_handle if row is not None else None
            if handle_session_id:
                # A turn that ended without turn_complete (SIGTERM eviction,
                # watchdog teardown, crash) never reached ``_slice_usage``,
                # the only other place that publishes ``session_id`` into the
                # DB. Without this, a first-generation session whose only
                # turn was interrupted keeps ``resume_handle`` at None, and
                # the next wake cold-starts a brand-new inner conversation
                # instead of resuming the one that actually exists.
                resume = {**(resume or {}), "session_id": str(handle_session_id)}
            sess.release_host(db, self._sid, status=self._shutdown_status,
                              resume_handle=resume)

    # ------------------------------------------------------------------
    # Transcript helpers
    # ------------------------------------------------------------------
    def _write_transcript(self, evt: dict) -> None:
        out = dict(evt)
        out.setdefault("ts", now_iso())
        out["seq"] = self._seq[0]
        self._seq[0] += 1
        try:
            with self._transcript_path.open("ab") as f:
                write_event(f, out)
        except OSError:
            log.exception("transcript write failed for session %s", self._sid)

    def _write_transcript_translated(self, evt: dict) -> None:
        try:
            canonical = self._translate(evt)
        except Exception:  # noqa: BLE001
            canonical = [{"type": "assistant_text", "text": json.dumps(evt, default=str)}]
        for c in canonical:
            self._write_transcript(c)

    def _delta_import(self, source_path: str, session_id: str) -> None:
        """Import turns made in a terminal round-trip past our watermark.

        ``--resume`` keeps one jsonl per session (no fork), so this is a trivial
        delta: read OUR OWN session file past the last line we imported and
        translate the new entries into the canonical transcript. Shared logic
        in ``domain.session_import``; the host writes through its live seq
        counter so imported lines land exactly where inner output would.
        """
        path = simp.cc_session_jsonl(source_path, session_id)
        if path is None or not path.exists():
            return
        with self._sf() as db:
            row = sess.get_session_row(db, self._sid)
            watermark = int((row.resume_handle or {}).get("imported_lines") or 0)
        imported, total = simp.import_delta(path, watermark, self._write_transcript)
        if total <= watermark:
            return
        with self._sf() as db:
            row = sess.get_session_row(db, self._sid)
            row.resume_handle = {**(row.resume_handle or {}),
                                 "session_id": session_id,
                                 "imported_lines": total}
            db.commit()
        log.info("delta-imported %d jsonl entries for session %s", imported, self._sid)


def _decode(body: str) -> dict:
    try:
        obj = json.loads(body or "{}")
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _amain(session_id: str) -> int:
    cfg = load_config()
    from nightdesk.db.session import make_engine, session_factory as _sf
    import socket
    factory = _sf(make_engine(cfg.db_path))
    host = SessionHost(
        session_factory=factory,
        session_id=session_id,
        config=cfg,
        host=socket.gethostname(),
    )
    return await host.run()


def main() -> int:
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: nightdesk-session <session_id>", file=sys.stderr)
        return 2
    return asyncio.run(_amain(sys.argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
