"""Persistent inner runner for resident interactive agents.

Launched by ``worker.session_host`` as a child process. Unlike ``_sdk_runner``
(one-shot ``query()``), this keeps a ``ClaudeSDKClient`` connected across many
turns and speaks a bidirectional NDJSON control protocol over stdin/stdout.

Divergences from ``_sdk_runner`` (see resident-agents-v3.md §10):
- Persistent ``ClaudeSDKClient`` (CC boots ONCE); warm turns cost only delivery
  + model latency.
- ``AskUserQuestion`` / ``ExitPlanMode`` are NOT disallowed here — they route
  through the ``can_use_tool`` callback into the needs-input control loop. The
  headless guard still applies to ticket runs; interactive agents are the
  sanctioned, human-gated exception.

Control protocol
----------------
IN (stdin, one JSON object per line):
- ``{"type":"init", ...spec}``      first line; builds + connects the client.
- ``{"type":"user_turn","turn_id","text"}``
- ``{"type":"answer","request_id","decision","answer"?,"updated_input"?}``
- ``{"type":"interrupt"}``
- ``{"type":"shutdown"}``

OUT (stdout, one JSON object per line):
- transcript events: SDK-shaped dicts (same shape ``_sdk_runner`` emits); the
  host translates + writes them to the transcript file.
- control events (host reads, NOT written to the transcript):
  ``{"type":"server_info","commands":[...],"skills":[...]}``
  ``{"type":"pending_input","request_id","kind","tool","payload","options"}``
  ``{"type":"pending_resolved","request_id"}``
  ``{"type":"turn_complete","turn_id","session_id","usage","cost_usd"}``

The module stays importable without ``claude_agent_sdk`` (the SDK import lives
inside ``_run``) so it can be unit-tested with a stub.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Any, Optional

from nightdesk.worker._sdk_runner import (
    _AsyncEmitter,
    _emit,
    _event_to_dict,
    _usage_to_dict,
    _MAX_STDOUT_BUFFER_BYTES,
    _setup_logging,
)


log = logging.getLogger(__name__)


# Control-event type tags the host discriminates on (never written to the
# transcript file; the renderer ignores them, the page reads them).
CONTROL_EVENT_TYPES = frozenset(
    {"server_info", "pending_input", "pending_resolved", "turn_complete"}
)


def _classify_tool(name: str, tool_input: dict, suggestions: list) -> tuple[str, dict, list]:
    """Map a ``can_use_tool`` invocation to (kind, payload, options).

    - ``AskUserQuestion`` -> ``ask_question`` (questions + option chips).
    - ``ExitPlanMode``    -> ``plan_approval`` (the plan text; approve/keep).
    - anything else       -> ``permission`` (tool input + CLI suggestions).
    """
    if name == "AskUserQuestion":
        questions = tool_input.get("questions") or tool_input.get("question") or tool_input
        return "ask_question", {"questions": questions}, []
    if name == "ExitPlanMode":
        return "plan_approval", {"plan": tool_input.get("plan", "")}, []
    return "permission", {"input": tool_input, "suggestions": suggestions or []}, []


async def _stdin_lines():
    """Yield decoded lines from stdin as they arrive, without blocking the loop.

    Uses an asyncio StreamReader over fd 0 so control lines (answer/interrupt)
    are read WHILE a turn streams and a ``can_use_tool`` callback is parked.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                yield line
    finally:
        transport.close()


class _Runner:
    def __init__(self, emit) -> None:
        self._emit = emit
        self._pending: dict[str, asyncio.Future] = {}
        self._client: Any = None
        self._turn_task: Optional[asyncio.Task] = None
        # Cumulative usage over the client's whole life; per-turn slicing is the
        # host's job (it keeps the generation baseline).
        self._session_id: Optional[str] = None

    # -- can_use_tool: the needs-input spine ------------------------------
    async def can_use_tool(self, name: str, tool_input: dict, ctx: Any):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        request_id = str(uuid.uuid4())
        suggestions = list(getattr(ctx, "suggestions", None) or [])
        kind, payload, options = _classify_tool(name, tool_input or {}, suggestions)
        title = getattr(ctx, "title", None)
        if title:
            payload["title"] = title
        await self._emit({
            "type": "pending_input",
            "request_id": request_id,
            "kind": kind,
            "tool": name,
            "payload": payload,
            "options": options,
        })
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = fut
        try:
            decision = await fut
        except asyncio.CancelledError:
            # interrupt() cancels the parked callback (verified live); end the
            # turn with a synthetic deny rather than propagating the cancel.
            self._pending.pop(request_id, None)
            await self._emit({"type": "pending_resolved", "request_id": request_id})
            return PermissionResultDeny(message="interrupted", interrupt=True)
        self._pending.pop(request_id, None)
        await self._emit({"type": "pending_resolved", "request_id": request_id})

        verb = str(decision.get("decision") or "deny")
        updated = decision.get("updated_input")
        if verb in ("allow", "approve"):
            if name == "AskUserQuestion":
                # Selections ride back as updatedInput; fall back to the raw
                # answer if the client did not shape one.
                ui = updated or decision.get("answer") or tool_input
                return PermissionResultAllow(updated_input=ui)
            return PermissionResultAllow(updated_input=updated)
        return PermissionResultDeny(
            message=str(decision.get("answer") or ""),
            interrupt=bool(decision.get("_interrupt")),
        )

    # -- one turn ---------------------------------------------------------
    async def _run_turn(self, turn_id: str, text: str) -> None:
        cumulative_usage: Optional[dict] = None
        cost_usd: Optional[float] = None
        try:
            await self._client.query(text)
            async for msg in self._client.receive_response():
                sid = getattr(msg, "session_id", None)
                if sid:
                    self._session_id = str(sid)
                name = type(msg).__name__
                if name == "ResultMessage":
                    u = _usage_to_dict(getattr(msg, "usage", None))
                    if u is not None:
                        cumulative_usage = u
                    tc = getattr(msg, "total_cost_usd", None)
                    if tc is not None:
                        cost_usd = float(tc)
                d = _event_to_dict(msg)
                if d is not None:
                    await self._emit(d)
        except Exception as exc:  # noqa: BLE001 - surface turn failure, stay alive
            log.exception("turn %s failed", turn_id)
            await self._emit({
                "type": "result", "subtype": "error",
                "result": f"turn crashed: {exc}",
            })
        await self._emit({
            "type": "turn_complete",
            "turn_id": turn_id,
            "session_id": self._session_id,
            "usage": cumulative_usage,
            "cost_usd": cost_usd,
        })

    def _answer(self, msg: dict) -> None:
        fut = self._pending.get(msg.get("request_id"))
        if fut is not None and not fut.done():
            fut.set_result(msg)

    async def _interrupt(self) -> None:
        # Primary path (verified in smoke): resolve any parked future with a
        # synthetic deny so the turn ends with a real decision + auditable row.
        parked = [f for f in self._pending.values() if not f.done()]
        if parked:
            for fut in parked:
                fut.set_result({"decision": "deny", "_interrupt": True})
            return
        # No parked callback: interrupt the streaming turn directly.
        try:
            await self._client.interrupt()
        except Exception:  # noqa: BLE001
            log.exception("client.interrupt() failed")

    async def run(self) -> int:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        # All stdin reads go through ONE async reader so the sync-then-async
        # buffering hazard never arises. The first line is the init spec.
        lines = _stdin_lines()
        spec: dict = {}
        try:
            first = await lines.__anext__()
            obj = json.loads(first)
            if isinstance(obj, dict):
                obj.pop("type", None)
                spec = obj
        except (StopAsyncIteration, json.JSONDecodeError):
            spec = {}

        opts_kwargs = _build_options_kwargs(spec)
        opts_kwargs["can_use_tool"] = self.can_use_tool
        try:
            options = ClaudeAgentOptions(**opts_kwargs)
        except Exception as exc:  # noqa: BLE001
            await self._emit({"type": "result", "subtype": "error",
                              "result": f"invalid options: {exc}"})
            return 1

        self._client = ClaudeSDKClient(options)
        await self._client.connect()
        try:
            await self._emit_server_info()
            async for line in lines:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype == "user_turn":
                    # Host serializes turns (delivers one, waits for
                    # turn_complete), so at most one is ever in flight.
                    self._turn_task = asyncio.create_task(
                        self._run_turn(msg.get("turn_id", ""), msg.get("text", ""))
                    )
                elif mtype == "answer":
                    self._answer(msg)
                elif mtype == "interrupt":
                    await self._interrupt()
                elif mtype == "shutdown":
                    break
            if self._turn_task is not None and not self._turn_task.done():
                self._turn_task.cancel()
                try:
                    await self._turn_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        finally:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return 0

    async def _emit_server_info(self) -> None:
        info = None
        try:
            info = await self._client.get_server_info()
        except Exception:  # noqa: BLE001
            log.exception("get_server_info failed")
        commands = []
        skills = []
        if isinstance(info, dict):
            commands = info.get("commands") or info.get("slash_commands") or []
            skills = info.get("skills") or []
        await self._emit({"type": "server_info", "commands": commands, "skills": skills})


def _build_options_kwargs(spec: dict) -> dict[str, Any]:
    """Translate the init spec into ClaudeAgentOptions kwargs.

    Trusted posture: ``setting_sources=["project","user"]`` loads the owner's
    real ``~/.claude`` (skills / hooks / CLAUDE.md) — the sanctioned interactive
    behaviour. AskUserQuestion / ExitPlanMode are intentionally NOT disallowed.
    """
    kwargs: dict[str, Any] = {}
    if spec.get("working_dir"):
        kwargs["cwd"] = spec["working_dir"]
    if spec.get("allowed_tools"):
        kwargs["allowed_tools"] = list(spec["allowed_tools"])
    if spec.get("disallowed_tools"):
        kwargs["disallowed_tools"] = list(spec["disallowed_tools"])
    if spec.get("model"):
        kwargs["model"] = spec["model"]
    if spec.get("permission_mode"):
        kwargs["permission_mode"] = spec["permission_mode"]
    if spec.get("cli_path"):
        kwargs["cli_path"] = spec["cli_path"]
    if spec.get("system_prompt"):
        kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": spec["system_prompt"],
        }
    if spec.get("resume"):
        kwargs["resume"] = spec["resume"]
    kwargs["setting_sources"] = spec.get("setting_sources") or ["project", "user"]
    kwargs["max_buffer_size"] = spec.get("max_buffer_size") or _MAX_STDOUT_BUFFER_BYTES
    return kwargs


async def _run() -> int:
    emitter: Optional[_AsyncEmitter] = None
    try:
        emitter = await _AsyncEmitter.connect(1)
    except Exception:  # noqa: BLE001 - defensive fallback
        log.exception("failed to set up async stdout emitter; using sync write")
        emitter = None

    async def emit(evt: Any) -> None:
        if emitter is not None:
            await emitter.emit(evt)
        else:
            _emit(evt)

    try:
        from claude_agent_sdk import ClaudeSDKClient  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        await emit({"type": "result", "subtype": "error",
                    "result": f"failed to import claude_agent_sdk: {exc}"})
        return 1

    runner = _Runner(emit)
    try:
        return await runner.run()
    finally:
        if emitter is not None:
            await emitter.aclose()


def main() -> int:
    _setup_logging()
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
