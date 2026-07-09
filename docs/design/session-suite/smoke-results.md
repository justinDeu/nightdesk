# Resident-agents SDK smoke results

Live empirical verification of the load-bearing `ClaudeSDKClient` behaviours the
v3 design rests on (resident-agents-v3.md §17, §19.1). Run against the authed
`claude` CLI 2.1.203 + `claude_agent_sdk` in the worktree venv, model `haiku`,
scratch cwd with a custom `/smoketest` command. Reproduce with:

    uv run python scripts/smoke_resident_sdk.py

## Results (all PASS)

| Check | Result | Observed |
|---|---|---|
| a) stream + complete | PASS | two turns streamed to a `ResultMessage`; assistant text `ping` then `pong` |
| a) session reuse | PASS | second `query()` on the same connected client kept the **same** `session_id` (`--resume` semantics hold in-process; no fork) |
| b) `can_use_tool` parks the turn | PASS | callback awaited a future for a held **5.0s**; `receive_response` did not advance during the park (total drain 26s incl. model time). Resolving the future resumed the turn and the deny took effect |
| c) `interrupt()` while parked | PASS | `client.interrupt()` **unblocks** a callback parked on a future — it raises `CancelledError` into the parked callback (`released_by=cancelled`) and `receive_response` returns |
| d) `ExitPlanMode` routes via `can_use_tool` | PASS | in `permission_mode="plan"` the callback fires with `tool_name="ExitPlanMode"`, input keys `['plan', 'planFilePath']` |
| e) custom slash command | PASS | `/smoketest` appears in `get_server_info()["commands"]` (21 total) and `client.query("/smoketest WORLD")` expands, replying `SMOKE_CMD_OK WORLD` |

## Load-bearing findings and design impact

1. **The needs-input spine is sound.** `can_use_tool` suspends the turn
   coroutine cleanly while awaiting a future, and resolving the future resumes
   streaming (b). This is exactly the runner→host→answer bridge in §3.2.

2. **`interrupt()` unblocks a parked callback (c).** The empirical mechanism is
   cancellation: `interrupt()` cancels the receive task, which raises
   `CancelledError` *inside* the awaiting `can_use_tool`. Consequence for the
   runner: the parked-future `await` must tolerate `CancelledError`. The design's
   §19.1 synthetic-deny fallback ("resolve the parked future with a deny
   directly in the runner") is therefore **not strictly required** for
   correctness — interrupt alone ends the turn. We still implement synthetic-deny
   as the *primary* interrupt-while-parked path (resolve the future with a
   `PermissionResultDeny(interrupt=True)` before/without calling `interrupt()`),
   because it gives a controlled turn end with a real deny decision and an
   auditable `pending → cancelled` row, rather than a bare cancellation we would
   have to swallow. `interrupt()` remains the escape hatch if the future path is
   somehow unavailable. No design change; the fallback is promoted to primary.

3. **Pre-approval bypasses the callback (test-methodology finding, not a
   deviation).** A tool listed in `allowed_tools` in `permission_mode="default"`
   is auto-approved and never reaches `can_use_tool`. The reliable, always-routed
   triggers are `ExitPlanMode` (plan mode) and `AskUserQuestion` — the exact
   tools the design uses for `plan_approval` / `ask_question` pending kinds. The
   runner must therefore NOT pre-allow the tools it wants gated; the interactive
   path leaves `AskUserQuestion`/`ExitPlanMode` out of the disallowed set (§10)
   and relies on plan mode / the tools themselves to route through the callback.

4. **`get_server_info()` carries `commands`** (key `commands`, 21 entries incl.
   custom commands) — the composer autocomplete seed. `session_id` rides on every
   message, so the host can capture the resume handle from any turn (a).

No deviations from the v3 design were required. The one adjustment is promoting
the synthetic-deny interrupt path from "fallback" to "primary", justified above.
