"""Resident backend seam: how the host spawns and talks to the inner agent.

v1 shipped ``ClaudeResidentBackend`` only (a child running
``nightdesk.worker._session_runner`` over a stdin/stdout NDJSON control
protocol). This module now also ships ``OpencodeResidentBackend``. The
``posture`` argument reserves the deferred sandboxed posture — for Claude only
the argv prefix changes; for opencode it would gain the sandbox wrapper the
ticket-side ``backends.opencode.OpencodeBackend`` already uses.

Seam tension (see resident-agents-v3.md §10 and the ticket that added this
backend): the seam was designed around Claude's stdin/stdout NDJSON control
protocol, including a needs-input spine (``pending_input``/``pending_resolved``)
built on ``can_use_tool``/``AskUserQuestion``/``ExitPlanMode`` — none of which
opencode has. opencode's process model is also fundamentally different: a
``opencode serve`` HTTP daemon driven over localhost + Server-Sent Events
(see ``backends/opencode_driver.py``), not a line-oriented child process.
Rather than force opencode's HTTP/SSE shape through a fake stdio handle, this
module implements :class:`ResidentHandle` directly against the HTTP driver
(``_OpencodeResidentHandle``): the ``send``/``events``/``close`` contract is
unchanged, but internally it POSTs turns and streams ``/event`` instead of
writing/reading NDJSON lines. Every permission decision is baked into the
rendered opencode config at start time (headless never-ask, same as a ticket
run), so ``_OpencodeResidentHandle`` never emits ``pending_input`` — an
``answer`` control message delivered to it is a no-op. The other seam
divergence: :class:`ClaudeResidentBackend` yields the inner's RAW SDK-shaped
events for the host to translate (``worker.claude_translator``); opencode's
handle instead translates internally (via ``backends.opencode_translate``,
reused verbatim from the ticket path) and yields already-canonical transcript
events, since usage/cost accounting needs that same stateful translation
pass anyway (see ``_OpencodeResidentHandle._handle_sse_event``). The host
picks the right (no-op vs Claude) translate step per :func:`translator_for`
rather than hard-coding one, so it never has to know which shape a given
backend's handle emits.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import sys
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional, Protocol

import httpx

from nightdesk.backends import opencode_config as ocfg
from nightdesk.backends.opencode_translate import new_state, translate_event


log = logging.getLogger(__name__)

# Token fields accumulated cumulatively across a resident generation, mirroring
# the shape ``SessionHost._slice_usage`` expects from a Claude ResultMessage's
# (session-cumulative) usage. opencode reports usage per-message rather than
# session-cumulative, so ``_OpencodeResidentHandle`` sums every message seen
# so far itself — see its docstring.
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


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


# ---------------------------------------------------------------------------
# opencode: HTTP/SSE resident handle
# ---------------------------------------------------------------------------


def _alloc_free_port() -> int:
    """Reserve a free localhost TCP port for the per-session opencode server.

    Bind to port 0, read the assigned port, close. The window between close
    and ``opencode serve`` binding it is a benign race on a single-host
    worker (mirrors ``executors.local._alloc_free_port``).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_ready(client: httpx.AsyncClient, proc: "asyncio.subprocess.Process",
                      *, timeout: float = 30.0, poll: float = 0.25) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if proc.returncode is not None:
            return False
        try:
            r = await client.get("/global/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except (httpx.HTTPError, OSError):
            pass
        await asyncio.sleep(poll)
    return False


async def _terminate_proc(proc: "asyncio.subprocess.Process") -> None:
    if proc.returncode is not None:
        return
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
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass


def _cumulative_tokens(state: dict) -> dict[str, int]:
    """Sum every message's usage snapshot in ``state`` (see
    ``opencode_translate.new_state``) into one flat cumulative-token dict.

    Unlike Claude's ``ResultMessage.usage`` (already session-cumulative),
    opencode's ``message.updated`` reports per-message totals, so summing the
    latest snapshot per message id is what makes the running total
    cumulative — matching what ``SessionHost._slice_usage``'s delta-over-
    baseline slicing expects from ANY backend's ``turn_complete.usage``.
    """
    totals = {f: 0 for f in _TOKEN_FIELDS}
    for entry in state.get("usage_by_message", {}).values():
        for f in _TOKEN_FIELDS:
            totals[f] += int(entry.get(f) or 0)
    return totals


class _OpencodeResidentHandle:
    """A :class:`ResidentHandle` over a per-session ``opencode serve`` daemon.

    One dedicated server process per resident agent, driven over localhost
    HTTP + the ``/event`` SSE stream (the same protocol
    ``backends/opencode_driver.py`` drives for ticket runs), started once by
    :class:`OpencodeResidentBackend.start` and kept warm across many turns —
    unlike a ticket run's one-shot drive, ``_run_turn``-equivalent work here
    happens repeatedly against the SAME session/process.

    Translation happens HERE (not at the host layer): ``translate_event``
    needs the same stateful ``new_state()`` this handle already keeps for
    cumulative usage/cost accounting (see ``_handle_sse_event``), so running
    it twice (once for usage bookkeeping, once at the host for the
    transcript) would just duplicate work over the same event stream.
    ``events()`` therefore yields already-canonical transcript events plus
    this handle's own synthetic ``turn_complete`` control events; the host's
    translate step for the ``opencode`` backend is the identity function
    (see :func:`translator_for`).
    """

    def __init__(
        self,
        proc: "asyncio.subprocess.Process",
        client: httpx.AsyncClient,
        *,
        workdir: str,
        model: Optional[str],
        system_prompt: Optional[str],
        resume: Optional[str],
    ) -> None:
        self._proc = proc
        self._client = client
        self._workdir = workdir
        self._model = model
        self._system_prompt = system_prompt
        self.pid = proc.pid
        # A resume handle is an opencode session id from a prior generation:
        # reuse it directly (real conversation continuity) instead of
        # creating a fresh session on the first turn.
        self.session_id: Optional[str] = resume
        self._queue: "asyncio.Queue[Optional[dict]]" = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        # Set once the /event stream is actually subscribed (or the reader
        # has given up). ``start`` waits on it so the first turn is never
        # POSTed before the SSE subscription is live — opencode can emit
        # events (up to session.idle) the instant a prompt is accepted, and a
        # prompt posted ahead of the subscribe would silently drop them, the
        # same race backends/opencode_driver.py avoids by subscribing first.
        self._stream_ready = asyncio.Event()
        self._state = new_state()
        self._cost_by_message: dict[str, float] = {}
        self._active_turn_id: Optional[str] = None
        self._closed = False

    def start_reader(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    # -- ResidentHandle protocol -------------------------------------------
    async def send(self, obj: dict) -> None:
        t = obj.get("type")
        if t == "user_turn":
            await self._start_turn(obj.get("turn_id") or "", obj.get("text") or "")
        elif t == "interrupt":
            await self._abort()
        elif t == "answer":
            # opencode has no can_use_tool/AskUserQuestion/ExitPlanMode
            # equivalent (see the module docstring) so this handle never
            # emits pending_input — nothing is ever parked to resolve.
            pass
        elif t == "shutdown":
            self._closed = True

    async def events(self) -> AsyncIterator[dict]:
        while True:
            evt = await self._queue.get()
            if evt is None:
                return
            yield evt

    async def close(self) -> None:
        self._closed = True
        await self._abort()
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass
        await _terminate_proc(self._proc)

    # -- turn lifecycle ------------------------------------------------------
    async def _ensure_session(self) -> str:
        if self.session_id is None:
            r = await self._client.post(
                "/session", params={"directory": self._workdir},
                json={"title": "nightdesk agent"}, timeout=30.0,
            )
            r.raise_for_status()
            self.session_id = r.json()["id"]
        return self.session_id

    async def _start_turn(self, turn_id: str, text: str) -> None:
        self._active_turn_id = turn_id
        try:
            sid = await self._ensure_session()
            body = ocfg.build_prompt_body(
                text, system=self._system_prompt, model=self._model,
            )
            r = await self._client.post(
                f"/session/{sid}/prompt_async",
                params={"directory": self._workdir}, json=body, timeout=30.0,
            )
            r.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            log.exception("opencode resident: failed to post turn %s", turn_id)
            await self._queue.put({
                "type": "worker_error", "kind": "driver", "summary": str(exc),
            })
            self._finish_turn(turn_id)

    async def _abort(self) -> None:
        if self.session_id is None:
            return
        try:
            await self._client.post(f"/session/{self.session_id}/abort", timeout=10.0)
        except (httpx.HTTPError, OSError):
            pass

    def _finish_turn(self, turn_id: str) -> None:
        if self._active_turn_id != turn_id:
            return
        self._active_turn_id = None
        totals = _cumulative_tokens(self._state)
        cost = sum(self._cost_by_message.values())
        self._queue.put_nowait({
            "type": "turn_complete", "turn_id": turn_id,
            "session_id": self.session_id,
            "usage": totals, "cost_usd": cost,
        })

    # -- event stream ---------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            async with self._client.stream("GET", "/event", timeout=None) as resp:
                self._stream_ready.set()
                async for line in resp.aiter_lines():
                    if self._closed:
                        break
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload:
                        continue
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    self._handle_sse_event(evt)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError):
            log.exception("opencode resident event stream ended for session %s",
                          self.session_id)
        finally:
            # Unblock ``start`` even if the subscribe raised before the stream
            # ever opened — the handle then fails on the first turn instead of
            # hanging the boot until the wait times out.
            self._stream_ready.set()
            self._queue.put_nowait(None)

    def _handle_sse_event(self, evt: dict) -> None:
        if not isinstance(evt, dict):
            return
        props = evt.get("properties") or {}
        sid = props.get("sessionID")
        if self.session_id is not None and sid not in (None, self.session_id):
            return
        etype = evt.get("type")
        if etype == "message.updated":
            # translate_event only tracks the LATEST cost snapshot overall
            # (state["usage"]["cost"]); per-message cost isn't exposed there,
            # so this handle tracks it itself, the same way translate_event
            # tracks per-message tokens in state["usage_by_message"].
            info = props.get("info") or {}
            # Key by the same (id or modelID) fallback translate_event uses for
            # tokens (see opencode_translate.new_state) so an id-less message's
            # cost is counted on the same accumulator as its tokens, not dropped.
            mid = info.get("id") or info.get("modelID")
            cost = info.get("cost")
            if mid and cost is not None:
                self._cost_by_message[str(mid)] = float(cost)
        for canonical in translate_event(evt, self._state):
            self._queue.put_nowait(canonical)
        if (etype == "session.idle" and self._active_turn_id is not None
                and sid == self.session_id):
            self._finish_turn(self._active_turn_id)


class OpencodeResidentBackend:
    """Spawns a per-session ``opencode serve`` daemon and drives it over
    localhost HTTP/SSE (see the module docstring's "seam tension" note for
    why this doesn't go through the stdin/stdout NDJSON path).

    Trusted posture: a plain host child, no bwrap — same posture contract as
    :class:`ClaudeResidentBackend`. Config/auth (env-injected
    ``OPENCODE_CONFIG_CONTENT`` / ``OPENCODE_AUTH_CONTENT``) is rendered by
    ``SessionHost`` (mirroring how a ticket run resolves profile
    endpoint + model, see ``domain.model_assignment.compute_model_assignments``)
    and arrives here pre-baked into ``spec.env`` — this backend only knows how
    to boot the process and hand back a driven handle.
    """

    async def start(self, spec: StartSpec, posture: str = "trusted") -> ResidentHandle:
        binary = shutil.which("opencode") or os.path.expanduser("~/.opencode/bin/opencode")
        port = _alloc_free_port()
        password = secrets.token_urlsafe(24)

        child_env = dict(os.environ)
        child_env.update({k: str(v) for k, v in (spec.env or {}).items()})
        child_env["OPENCODE_SERVER_PASSWORD"] = password

        argv = [binary, "serve", "--port", str(port), "--hostname", "127.0.0.1", "--pure"]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=child_env,
        )
        client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", auth=("opencode", password),
        )
        ready = await _wait_ready(client, proc)
        if not ready:
            await client.aclose()
            await _terminate_proc(proc)
            raise RuntimeError("opencode server failed to start")

        handle = _OpencodeResidentHandle(
            proc, client,
            workdir=spec.working_dir, model=spec.model,
            system_prompt=spec.system_prompt, resume=spec.resume,
        )
        handle.start_reader()
        # Don't hand the host a handle whose /event stream isn't subscribed yet:
        # the host may deliver a turn immediately, and a prompt posted ahead of
        # the subscribe would drop the events it triggers. The reader sets this
        # the moment the stream opens (or gives up); the timeout is only a
        # backstop against a wedged connect on an already-health-checked server.
        try:
            await asyncio.wait_for(handle._stream_ready.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            await handle.close()
            raise RuntimeError("opencode event stream failed to subscribe")
        return handle


# ---------------------------------------------------------------------------
# Backend selection: Session.backend (runtime-lock vocabulary, see
# domain.sessions) -> ResidentBackend implementation / transcript translator.
# ---------------------------------------------------------------------------

_RESIDENT_BACKENDS: dict[str, Callable[[], "ResidentBackend"]] = {
    "claude": ClaudeResidentBackend,
    "opencode": OpencodeResidentBackend,
}


def resident_backend_for(session_backend_code: str) -> "ResidentBackend":
    """The :class:`ResidentBackend` implementation for a session's frozen
    ``backend`` value. Unknown/legacy codes fall back to Claude (the only
    value any pre-multi-backend row can carry)."""
    factory = _RESIDENT_BACKENDS.get(session_backend_code, ClaudeResidentBackend)
    return factory()


def _identity_translate(evt: dict) -> list[dict]:
    """opencode's handle already yields canonical transcript events (see
    ``_OpencodeResidentHandle``'s docstring) — nothing left to translate."""
    return [evt]


def translator_for(session_backend_code: str) -> Callable[[dict], list[dict]]:
    """The host's per-event transcript-translate step for a session's
    ``backend``. Claude's handle yields raw SDK-shaped events (translated via
    ``worker.claude_translator``); opencode's handle translates internally."""
    if session_backend_code == "opencode":
        return _identity_translate
    from nightdesk.worker.claude_translator import translate
    return translate
