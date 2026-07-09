#!/usr/bin/env python
"""Manual LIVE smoke test for the resident-agents SDK assumptions.

NOT a pytest test (it spends real tokens against the authed `claude` CLI). It
verifies the load-bearing ``ClaudeSDKClient`` behaviours the resident-agents v3
design rests on (design risk #1). Run it by hand from the worktree:

    cd <worktree>
    uv run python scripts/smoke_resident_sdk.py

Uses the cheapest available model (haiku) and a handful of tiny turns. Point the
working directory at a scratch dir that owns a custom slash command under
``.claude/commands/`` (this script creates one if missing).

Checks (see resident-agents-v3.md §17 / §19.1):
  a) connect -> query -> receive_response streams and completes; a second query
     on the same client reuses the session (same session_id).
  b) a can_use_tool callback that parks on an asyncio future suspends the turn;
     resolving it with an allow after N seconds resumes streaming.
  c) client.interrupt() while parked in the callback — does the turn unblock?
  d) ExitPlanMode routes through can_use_tool when permission_mode="plan".
  e) a custom slash command expands via client.query("/name args").

Each check prints PASS/FAIL and the observed detail; the exit code is non-zero
if any check fails, but the observations (not the pass/fail) are the point.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
)

MODEL = os.environ.get("CLAUDE_MODEL", "haiku")
SCRATCH = Path(
    os.environ.get(
        "SMOKE_CWD",
        "/tmp/claude-1000/-home-thor-fun-nightdesk/"
        "12168fa5-fc3d-478b-b752-db87b993dcd7/scratchpad/smoke-agents",
    )
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _ensure_scratch() -> None:
    (SCRATCH / ".claude" / "commands").mkdir(parents=True, exist_ok=True)
    cmd = SCRATCH / ".claude" / "commands" / "smoketest.md"
    if not cmd.exists():
        cmd.write_text(
            "---\ndescription: Echo a greeting\n---\n\n"
            'Please respond with exactly this text and nothing else: '
            '"SMOKE_CMD_OK $ARGUMENTS"\n'
        )


def _session_id_of(msg: Any) -> str | None:
    sid = getattr(msg, "session_id", None)
    return str(sid) if sid else None


def _text_of(msg: Any) -> str:
    out = []
    for b in getattr(msg, "content", None) or []:
        if isinstance(b, TextBlock):
            out.append(b.text)
    return " ".join(out)


async def _drain(client: ClaudeSDKClient) -> tuple[str, str | None]:
    """Consume one response; return (joined assistant text, session_id)."""
    text_parts: list[str] = []
    session_id: str | None = None
    async for msg in client.receive_response():
        sid = _session_id_of(msg)
        if sid:
            session_id = sid
        if isinstance(msg, AssistantMessage):
            text_parts.append(_text_of(msg))
    return " ".join(t for t in text_parts if t), session_id


results: dict[str, str] = {}


def _record(key: str, ok: bool, detail: str) -> None:
    results[key] = f"{'PASS' if ok else 'FAIL'}: {detail}"
    _log(f"[{key}] {results[key]}")


async def check_ab_session_reuse() -> None:
    """(a) stream + complete; second query reuses the session id."""
    opts = ClaudeAgentOptions(
        model=MODEL,
        cwd=str(SCRATCH),
        setting_sources=["project"],
        allowed_tools=[],
        disallowed_tools=["Bash", "Read", "Write", "Edit"],
    )
    async with ClaudeSDKClient(opts) as client:
        await client.query("Reply with the single word: ping")
        text1, sid1 = await _drain(client)
        await client.query("Reply with the single word: pong")
        text2, sid2 = await _drain(client)
        ok = bool(sid1) and sid1 == sid2
        _record(
            "a_stream_complete",
            bool(text1) and bool(text2),
            f"turn1={text1!r} turn2={text2!r}",
        )
        _record(
            "a_session_reuse",
            ok,
            f"sid1={sid1} sid2={sid2} same={sid1 == sid2}",
        )


async def check_b_park_and_resume() -> None:
    """(b) can_use_tool parks the turn on a future; deny after N s resumes.

    Parks on ExitPlanMode in plan mode (the reliable trigger — a plain
    permission-gated tool in ``allowed_tools`` is pre-approved and never reaches
    the callback). This mirrors the real needs-input spine: the design parks the
    turn coroutine inside ``can_use_tool`` and resolves it from the control
    channel. We resolve with a deny ("keep planning") so nothing executes.
    """
    parked_at: dict[str, float] = {}
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

    async def can_use_tool(name: str, tool_input: dict, ctx: Any):
        if name != "ExitPlanMode":
            return PermissionResultAllow()
        # Only park (and measure) the FIRST ExitPlanMode. After we deny, the
        # model may call it again; those return immediately so the measurement
        # of the first park is not overwritten.
        if "t0" in parked_at:
            return PermissionResultDeny(message="keep planning (smoke, repeat)")
        parked_at["t0"] = time.monotonic()
        _log(f"    can_use_tool fired: {name}; parking on future")
        decision = await fut  # PARK
        parked_at["t1"] = time.monotonic()
        return decision

    opts = ClaudeAgentOptions(
        model=MODEL,
        cwd=str(SCRATCH),
        setting_sources=["project"],
        permission_mode="plan",
        can_use_tool=can_use_tool,
    )
    async with ClaudeSDKClient(opts) as client:
        await client.query(
            "Make a trivial one-line plan to add a code comment, then call "
            "ExitPlanMode to present it."
        )

        async def resolver() -> None:
            # Wait until the callback has actually parked, then release.
            for _ in range(200):
                if "t0" in parked_at:
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(5)
            if not fut.done():
                _log("    resolver: releasing deny after 5s of parking")
                fut.set_result(PermissionResultDeny(message="keep planning (smoke)"))

        resolver_task = asyncio.create_task(resolver())
        t_start = time.monotonic()
        text, _ = await _drain(client)
        elapsed = time.monotonic() - t_start
        await resolver_task
        parked = "t1" in parked_at
        park_dur = parked_at.get("t1", 0) - parked_at.get("t0", 0)
        suspended = parked and park_dur >= 4.0
        _record(
            "b_park_suspends",
            parked and suspended,
            f"parked={parked} park_dur={park_dur:.1f}s "
            f"drain_elapsed={elapsed:.1f}s reply={text[:60]!r}",
        )


async def check_c_interrupt_while_parked() -> None:
    """(c) interrupt() while parked in the callback — does the turn unblock?"""
    state: dict[str, Any] = {"released_by": None}
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

    async def can_use_tool(name: str, tool_input: dict, ctx: Any):
        if name != "ExitPlanMode":
            return PermissionResultAllow()
        state["parked"] = True
        _log(f"    can_use_tool fired (interrupt test): {name}; parking")
        try:
            decision = await fut
            state["released_by"] = "future"
            return decision
        except asyncio.CancelledError:
            state["released_by"] = "cancelled"
            raise

    opts = ClaudeAgentOptions(
        model=MODEL,
        cwd=str(SCRATCH),
        setting_sources=["project"],
        permission_mode="plan",
        can_use_tool=can_use_tool,
    )
    async with ClaudeSDKClient(opts) as client:
        await client.query(
            "Make a trivial one-line plan to add a code comment, then call "
            "ExitPlanMode to present it."
        )

        async def interrupter() -> None:
            for _ in range(200):
                if state.get("parked"):
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(2)
            _log("    interrupter: calling client.interrupt() while parked")
            try:
                await client.interrupt()
                state["interrupt_ok"] = True
            except Exception as exc:  # noqa: BLE001
                state["interrupt_ok"] = False
                state["interrupt_err"] = repr(exc)

        interrupt_task = asyncio.create_task(interrupter())
        unblocked = False
        detail = ""
        try:
            # Bound the wait: if interrupt does NOT unblock a parked callback,
            # this would hang forever, so cap it.
            text, _ = await asyncio.wait_for(_drain(client), timeout=20)
            unblocked = True
            detail = f"drain returned reply={text[:60]!r}"
        except asyncio.TimeoutError:
            unblocked = False
            detail = "drain still blocked 20s after interrupt (parked future NOT released)"
        finally:
            if not fut.done():
                fut.set_result(PermissionResultDeny(message="smoke cleanup", interrupt=True))
            await interrupt_task
        _record(
            "c_interrupt_unblocks",
            unblocked,
            f"{detail} released_by={state.get('released_by')} "
            f"interrupt_ok={state.get('interrupt_ok')}",
        )


async def check_d_exitplanmode() -> None:
    """(d) ExitPlanMode routes through can_use_tool in permission_mode=plan."""
    seen: dict[str, Any] = {}
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

    async def can_use_tool(name: str, tool_input: dict, ctx: Any):
        seen.setdefault("names", []).append(name)
        _log(f"    can_use_tool fired (plan test): {name} input_keys={list(tool_input)}")
        if name == "ExitPlanMode":
            seen["plan_input"] = tool_input
            # Deny (keep planning) so we don't run anything.
            if not fut.done():
                fut.set_result(True)
            return PermissionResultDeny(message="keep planning (smoke)")
        return PermissionResultAllow()

    opts = ClaudeAgentOptions(
        model=MODEL,
        cwd=str(SCRATCH),
        setting_sources=["project"],
        permission_mode="plan",
        can_use_tool=can_use_tool,
    )
    async with ClaudeSDKClient(opts) as client:
        await client.query(
            "Make a one-line plan to add a comment to a file, then call "
            "ExitPlanMode to present it. Keep it trivial."
        )
        try:
            text, _ = await asyncio.wait_for(_drain(client), timeout=60)
        except asyncio.TimeoutError:
            text = "<timeout>"
        got = "ExitPlanMode" in (seen.get("names") or [])
        _record(
            "d_exitplanmode_routes",
            got,
            f"tools_seen={seen.get('names')} plan_input_keys="
            f"{list(seen.get('plan_input', {}))}",
        )


async def check_e_custom_command() -> None:
    """(e) a custom slash command expands via client.query('/name args')."""
    opts = ClaudeAgentOptions(
        model=MODEL,
        cwd=str(SCRATCH),
        setting_sources=["project"],
        disallowed_tools=["Bash", "Read", "Write", "Edit"],
    )
    async with ClaudeSDKClient(opts) as client:
        info = await client.get_server_info()
        cmds = []
        if info:
            cmds = info.get("commands") or info.get("slash_commands") or []
        names = [c.get("name") if isinstance(c, dict) else c for c in cmds]
        has_cmd = any("smoketest" in str(n) for n in names)
        _log(f"    server_info commands count={len(names)} has_smoketest={has_cmd}")
        await client.query("/smoketest WORLD")
        text, _ = await _drain(client)
        ok = "SMOKE_CMD_OK" in text and "WORLD" in text
        _record(
            "e_custom_command",
            ok,
            f"discovered={has_cmd} reply={text[:80]!r}",
        )


async def main() -> int:
    _ensure_scratch()
    _log(f"model={MODEL} cwd={SCRATCH}")
    checks = [
        ("a/b session reuse + stream", check_ab_session_reuse),
        ("b park + resume", check_b_park_and_resume),
        ("c interrupt while parked", check_c_interrupt_while_parked),
        ("d ExitPlanMode routes", check_d_exitplanmode),
        ("e custom command", check_e_custom_command),
    ]
    for label, fn in checks:
        _log(f"\n=== {label} ===")
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            _record(label, False, f"raised {exc!r}")

    _log("\n=== SUMMARY ===")
    for k, v in results.items():
        _log(f"  {k}: {v}")
    return 0 if all(v.startswith("PASS") for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
