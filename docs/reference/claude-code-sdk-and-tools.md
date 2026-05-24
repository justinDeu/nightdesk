# Claude Code SDK and Tool Reference

Reference for how the nightdesk worker drives Claude Code, what tools the agent
is actually offered, and why `TodoWrite` no longer appears. Captured 2026-05-24
against the versions pinned below. Re-verify with the probes in the last section
when the CLI or SDK is upgraded.

## Versions in use

| Component | Version | Location |
| --- | --- | --- |
| Python SDK | `claude-agent-sdk` 0.2.82 | `.venv/.../claude_agent_sdk` |
| CLI binary | Claude Code 2.1.150 | `~/.local/share/claude/versions/2.1.150` (symlinked from `~/.local/bin/claude`) |

The Python SDK does not talk to the API directly. It launches the `claude` CLI
as a subprocess in stream-json mode and parses its stdout:

```
claude --output-format stream-json --verbose [flags...]
```

Source: `claude_agent_sdk/_internal/transport/subprocess_cli.py`.

## How `ClaudeAgentOptions` maps to CLI flags

From `subprocess_cli.py:_build_command`. This is how `_sdk_runner.py` options
reach the model.

| Option | CLI flag | Notes |
| --- | --- | --- |
| `system_prompt=None` | `--system-prompt ""` | Empty string. Suppresses the default Claude Code system prompt. This is what the worker gets today. |
| `system_prompt="..."` | `--system-prompt "..."` | Full replacement. |
| `system_prompt={"type":"preset","preset":"claude_code","append":"..."}` | `--append-system-prompt "..."` | Keeps the default prompt, appends text. |
| `system_prompt={"type":"file","path":...}` | `--system-prompt-file` | |
| `tools=[...]` | `--tools a,b,c` | Base set from the built-in tools. `""` disables all, `"default"` enables all. |
| `allowed_tools=[...]` | `--allowedTools a,b` | Whitelist. SDK auto-appends `Skill` when skills are in play. |
| `disallowed_tools=[...]` | `--disallowedTools a,b` | Denylist. Supports patterns like `Bash(git *)`. |
| `setting_sources=[...]` | `--setting-sources user,project,local` | Which settings layers load. Defaults to `["user","project"]` when skills are active. |
| `model` | `--model` | |
| `permission_mode` | `--permission-mode` | |
| `mcp_servers` | `--mcp-config <json>` | |
| `include_partial_messages` | `--include-partial-messages` | |

Key correction to an earlier assumption: **a system prompt cannot add or remove a
tool.** Tool availability is set by `--tools` / `--allowedTools` /
`--disallowedTools`, the loaded MCP servers, and the CLI version's built-in
registry. The prompt only nudges which available tool the model picks.

## The live tool set (the authoritative source)

The CLI emits a `system` / `init` stream-json message at session start whose
`tools` array is the exact set offered to the model. That is the ground truth,
not the strings in the binary (the binary still contains `TodoWrite` even though
the CLI does not offer it).

### Full set (user + project settings, my interactive shell)

```
Task, AskUserQuestion, Bash, CronCreate, CronDelete, CronList, Edit,
EnterPlanMode, EnterWorktree, ExitPlanMode, ExitWorktree, Glob, Grep, LSP,
Monitor, NotebookEdit, PushNotification, Read, RemoteTrigger, ScheduleWakeup,
SendMessage, ShareOnboardingGuide, Skill, TaskCreate, TaskGet, TaskList,
TaskOutput, TaskStop, TaskUpdate, TeamCreate, TeamDelete, ToolSearch, WebFetch,
WebSearch, Write
```

### Worker set (`setting_sources=["project"]`, `system_prompt=""`, worker denylist)

```
Task, Bash, CronCreate, CronDelete, CronList, Edit, EnterWorktree, ExitWorktree,
Glob, Grep, Monitor, NotebookEdit, PushNotification, Read, RemoteTrigger,
ScheduleWakeup, SendMessage, ShareOnboardingGuide, Skill, TaskCreate, TaskGet,
TaskList, TaskOutput, TaskStop, TaskUpdate, TeamCreate, TeamDelete, ToolSearch,
WebFetch, WebSearch, Write
```

Differences in the worker set: no MCP servers, reduced agent roster
(`claude`, `Explore`, `general-purpose`, `Plan`, `statusline-setup`), and the
denylist removes `AskUserQuestion`, `EnterPlanMode`, `ExitPlanMode`
(`_sdk_runner.py:_HEADLESS_DISALLOWED`). **`TodoWrite` is absent from both sets.**

### Rough tool categories

- File and search: `Read`, `Edit`, `Write`, `Glob`, `Grep`, `NotebookEdit`, `LSP`
- Shell: `Bash`
- Web: `WebFetch`, `WebSearch`
- Tasks / todos: `TaskCreate`, `TaskUpdate`, `TaskList`, `TaskGet`, `TaskOutput`, `TaskStop`
- Sub-agents and teams: `Task` (sub-agent dispatch; recorded as `Agent` in some transcripts), `SendMessage`, `TeamCreate`, `TeamDelete`, `Monitor`
- Scheduling: `CronCreate`, `CronDelete`, `CronList`, `ScheduleWakeup`, `RemoteTrigger`, `PushNotification`
- Worktrees: `EnterWorktree`, `ExitWorktree`
- Plan mode: `EnterPlanMode`, `ExitPlanMode`
- Skills and discovery: `Skill`, `ToolSearch`
- Misc: `AskUserQuestion`, `ShareOnboardingGuide`

`ToolSearch` exists because 2.1.x defers many tools: not every tool is loaded
upfront, and the model uses `ToolSearch` to fetch a deferred tool's schema before
calling it.

## TodoWrite vs Task tools

The single most consequential fact for nightdesk transcript work.

- **Default switch: Claude Code v2.1.142 / TypeScript Agent SDK 0.3.142.** From
  that version on, sessions use `TaskCreate` / `TaskUpdate` / `TaskGet` /
  `TaskList` instead of `TodoWrite`. Our CLI (2.1.150) is past that line, so
  `TodoWrite` is never offered.
- **Revert lever:** set env `CLAUDE_CODE_ENABLE_TASKS=0` on the CLI process to
  re-enable `TodoWrite`. It is an environment variable, not a prompt or a tool
  flag.
- Source: https://code.claude.com/docs/en/agent-sdk/todo-tracking

### Shape difference

| With `TodoWrite` (legacy) | With Task tools (current) |
| --- | --- |
| One call rewrites the full `todos` array | `TaskCreate` adds one item; `TaskUpdate` patches one item by `taskId` |
| Item: `{content, status, activeForm}` | `TaskCreate` input: `{subject, description, activeForm?, metadata?}` |
| | `TaskUpdate` input: `{taskId, status?, subject?, ..., addBlocks?, addBlockedBy?, owner?}` |
| Current state = last call's array | Current state = fold all create + update events, or read a `TaskList` result |

`status` is `pending` | `in_progress` | `completed`; `status: "deleted"` removes
an item.

**The assigned task ID is not in the `TaskCreate` input.** The docs describe it
as `{ task: { id, subject } }` in the result. Observed behavior in CLI 2.1.150 is
a flat string instead: `"Task #1 created successfully: <subject>"`,
`is_error=False`. The id is the sequential `#N`, and it matches the `taskId` the
model later passes to `TaskUpdate` (`"1"`, `"2"`, ...). So reconstruction can key
off creation order, with `TaskUpdate.taskId` as the authoritative join key.
Confirmed against production transcript `0b36a957`.

Capture caveat in our pipeline: `claude_translator._translate_user` stringifies
`tool_result` content into a flat `output` string (`claude_translator.py:143`).
So the task id is present but as text, not structured JSON. Confirm `Task*`
results are captured at all before relying on them (an observed `ToolSearch`
result was stored as `null`).

### Implication for a todo panel: support both sources

nightdesk does not pin the CLI version, and `CLAUDE_CODE_ENABLE_TASKS=0` (or an
older/future build) can flip which tool fires. A todo panel should therefore be
source-agnostic, not hardcoded to either tool:

- Normalize both into one shape: `{id, label, status, activeForm}`.
- `TodoWrite` adapter: current list = the last `TodoWrite` call's `todos` array.
- `Task*` adapter: fold `TaskCreate` (append; id from the `tool_result`) and
  `TaskUpdate` (patch by `taskId`) in `seq` order; a `TaskList` result is a
  snapshot shortcut.
- The two are mutually exclusive per run, so the panel picks whichever adapter
  has events. It never merges the two.

## Sub-agent attribution and transcripts

Two independent mechanisms exist for tying a sub-agent's work to the sub-agent.

1. **`parent_tool_use_id` on the message stream.** `AssistantMessage` and
   `UserMessage` carry `parent_tool_use_id` (`types.py:1030`, `1020`). It is
   `None` for top-level messages and set to the spawning `Task` tool_use id for
   sub-agent ("sidechain") messages (`types.py:1544`). Join key:
   `subagent.tool_use_id == nested_event.parent_tool_use_id`. Our runner
   currently discards this field in `_block_to_dict` and the `AssistantMessage`
   branch.

2. **On-disk sub-agent transcripts.** The SDK session store writes per-agent
   transcripts (`session_store.py:153`):
   - Main: `<projects_dir>/<project_key>/<session_id>.jsonl`
   - Sub-agent: `<project_key>/<session_id>/subagents/agent-<id>.jsonl`

   Not needed if we capture `parent_tool_use_id`, but available as a fallback.

Empirically, a sub-agent's own `tool_use` / `tool_result` events DO appear in the
main stream, interleaved with `subagent` lifecycle pings (started / progress /
notification). They are not summarized away; only the attribution tag is missing
once we drop `parent_tool_use_id`.

## How to reproduce / re-verify

Print the live tool set for the worker's exact configuration:

```
claude -p "reply ok" --output-format stream-json --verbose \
  --system-prompt "" \
  --setting-sources project \
  --disallowedTools AskUserQuestion,EnterPlanMode,ExitPlanMode \
  | head -4
```

The first `{"type":"system","subtype":"init",...}` line lists `tools`,
`mcp_servers`, `agents`, `slash_commands`, and `permissionMode`.

Check whether `TodoWrite` is offered:

```
... | python3 -c "import sys,json; [print('TodoWrite' in (json.loads(l).get('tools') or [])) for l in sys.stdin if '\"init\"' in l]"
```

Re-enable legacy `TodoWrite` for comparison:

```
CLAUDE_CODE_ENABLE_TASKS=0 claude -p "..." --output-format stream-json --verbose
```
