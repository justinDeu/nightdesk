# Multi-backend architecture

nightdesk runs one agent process per ticket inside a bwrap sandbox. This
document defines how agent harnesses ("backends") and inference endpoints
("providers") are modelled so that adding a backend means adding one module
plus declarative UI metadata — not editing the worker.

## Concepts

- **Backend** — the agent harness executed inside the per-ticket sandbox.
  Examples: `claude_sdk` (Claude Code via SDK runner), `opencode`, `dummy`
  (tests). One Python module per backend under `src/nightdesk/backends/`.
- **Provider** — an inference endpoint plus credential: Anthropic, an
  Anthropic-compatible proxy (z.ai), OpenAI, OpenRouter, Ollama. A DB row,
  configured once, consumed by any backend that declares support for its
  kind. Replaces the buried `claude_credentials` concept (which remains as a
  legacy fallback).
- **Capability** — a feature a backend declares. The UI and run pipeline
  degrade per-capability instead of assuming Claude parity.
- **Canonical transcript** — the NDJSON event schema in
  `nightdesk.transcript`. Unchanged. Every backend translates its native
  events into it.

## Required floor

A backend is admissible only if it can:

1. stream canonical transcript events while running,
2. end with exactly one terminal `result` event (success/error + summary),
3. run headless — never blocks on interactive input,
4. die on SIGTERM.

Everything else is a declared capability:

| Capability | Consumers | Degradation when absent |
|---|---|---|
| `cost_usage` | run stats, analytics | counts/cost show as missing |
| `session_resume` | resume command in run stats | affordance hidden |
| `tool_policy` | profile tool allow/deny | **warning** in editor + exec context: policy is advisory, sandbox still contains |
| `permission_modes` | profile permission_mode field | field hidden |
| `model_select` | profile model fields | fields hidden, backend default used |
| `system_prompt` | profile system prompt | field hidden |
| `follow_up_context` | resume/retry prompt scaffold | scaffold still sent; no session continuity |
| `subagents` | transcript subagent rendering | never emitted |
| `thinking` | transcript thinking blocks | never emitted |
| `rate_limit_signal` | rate-limit badge | never emitted |

## Package layout

```
src/nightdesk/backends/
    __init__.py      # re-exports registry helpers
    base.py          # contracts: Backend, BackendDescriptor, Capability,
                     # LaunchContext, LaunchPlan, Mount, transports,
                     # ResolvedProvider
    registry.py      # register()/get_backend()/all_backends()
    claude_code.py   # code "claude_sdk" (string kept for data compat)
    opencode.py      # code "opencode"
    dummy.py         # code "dummy" (tests)
```

Layering: `api` and `worker` both import `backends`; `backends` imports
`domain` only — never `worker`. `domain/backend_capabilities.py` keeps its
public names (`FieldGroup`, `backend_choices`, `capability_or_default`,
`consumes`) but is backed by the registry, so editor code and tests keep
working.

## Contracts

```python
class Capability(StrEnum):
    COST_USAGE, SESSION_RESUME, TOOL_POLICY, PERMISSION_MODES,
    MODEL_SELECT, SYSTEM_PROMPT, FOLLOW_UP_CONTEXT, SUBAGENTS,
    THINKING, RATE_LIMIT_SIGNAL

@dataclass(frozen=True)
class BackendDescriptor:
    code: str                       # stored on Profile.backend / Run.backend
    label: str
    summary: str
    capabilities: frozenset[Capability]
    group_keys: tuple[str, ...]     # editor field groups (existing FieldGroup keys)
    provider_kinds: frozenset[str]  # accepted Provider.kind values
    requires_provider: bool         # opencode: True; claude: False (legacy fallback)
    enabled: bool = True

@dataclass(frozen=True)
class Mount:
    host: str
    sandbox: str
    mode: Literal["ro", "rw"]

@dataclass(frozen=True)
class StdioTransport:
    pass

@dataclass(frozen=True)
class HttpTransport:
    port: int                # pre-allocated free localhost port (shared netns)
    ready_path: str = "/"
    ready_timeout: float = 20.0

@dataclass
class LaunchPlan:
    cmd: list[str]                          # argv inside the sandbox
    env: dict[str, str]                     # merged into the run env
    transport: StdioTransport | HttpTransport
    mounts: list[Mount]                     # extra binds (binaries, scratch dirs)
    needs_claude_binary: bool = False       # claude backend only

@dataclass
class LaunchContext:
    spec: PermissionSpec
    provider: ResolvedProvider | None       # decrypted at run time
    run_id: str
    workspace_dir: Path
    scratch_root: Path                      # per-run backend scratch, bind-mountable
    http_port: int | None                   # set iff backend.wants_http

class Backend(ABC):
    descriptor: ClassVar[BackendDescriptor]
    wants_http: ClassVar[bool] = False

    def prepare_launch(self, ctx: LaunchContext) -> LaunchPlan: ...
    async def execute(self, req: ExecutionRequest) -> ExecutionResult: ...
    def resume_descriptor(self, run) -> ResumeDescriptor | None: ...
    def after_run(self, ctx: LaunchContext, result: ExecutionResult) -> None:
        """Optional post-run hook (claude publishes the CC session here)."""
    def validate_profile(self, fields: dict) -> list[str]:
        """Optional editor-side validation messages."""
```

`run_one.py` becomes backend-agnostic:

```python
backend = get_backend(spec.backend)
provider = resolve_provider(session, profile.provider_id, secret_box)
ctx = LaunchContext(..., http_port=alloc_port() if backend.wants_http else None)
plan = backend.prepare_launch(ctx)
argv = build_bwrap_argv(spec, working_dir, cmd=plan.cmd, env={**env, **plan.env},
                        mounts=plan.mounts, require_claude=plan.needs_claude_binary,
                        git_dirs=_git_metadata_dirs(bundle))
result = await backend.execute(req)
backend.after_run(ctx, result)
```

No `if spec.backend == ...` anywhere in the worker.

## Providers

Table `providers`:

| column | type | notes |
|---|---|---|
| id | str pk | uuid |
| name | str unique | display name |
| kind | str | `anthropic`, `anthropic_compat`, `claude_subscription`, `openai`, `openai_compat`, `openrouter`, `ollama` |
| base_url | str nullable | compat/proxy/ollama endpoints |
| credential | text nullable | encrypted via the profile secret box; never returned in plaintext |
| default_model | str nullable | `provider/model` format where applicable |
| models | JSON list | optional curated model list for pickers |
| config | JSON dict | kind-specific extras |

- `profiles.provider_id` (nullable FK). Resolution order at run time:
  provider row if set, else legacy `claude_credentials` (claude backend
  only).
- `runs.backend`, `runs.provider_id`, `runs.session_ref` (JSON) record what
  actually ran. `session_ref` is the backend-shaped resume handle;
  `Run.session_id` stays for claude compatibility.
- Scheduler is intentionally untouched (schema headroom only). Per-provider
  caps/pauses are a later, cheap add on top of `runs.provider_id`.
- JSON API: `/api/v1/providers` CRUD, bearer-authed, `value_set` flag instead
  of plaintext echo (same pattern as profile credentials).
- Migration `0019` is additive (real DBs exist downstream of `0018`).

## Backend notes

### claude_sdk

Behavior-preserving refactor of today's flow. `prepare_launch` owns: CC
sessions dir mount, claude binary requirement, credential/provider env
(`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`).
`execute` is the existing `ClaudeExecutor` (stdio JSON lines through
`_sdk_runner`). `after_run` publishes the CC session for `claude --resume`.
Declares every capability. Provider kinds: `anthropic`, `anthropic_compat`,
`claude_subscription`.

### opencode

Per-ticket `opencode serve` daemon inside the sandbox, driven over
`http://127.0.0.1:{port}`. The port is allocated on the host (network
namespace is shared). `prepare_launch` writes a generated `opencode.json`
into the per-run scratch dir: permissions rendered never-ask (deny what the
profile denies, allow the rest), model from profile/provider, telemetry/
autoupdate off, provider block rendered from the Provider row. `execute`
polls readiness, creates a session, posts the prompt, consumes the SSE event
stream, translates to canonical events, aborts on cancel, extracts token
usage/cost from the assistant message info, returns `session_ref`
(directory + session id) for later interactive resume. Declares:
`cost_usage`, `session_resume`, `tool_policy`, `model_select`,
`system_prompt`. Provider kinds: `anthropic`, `anthropic_compat`, `openai`,
`openai_compat`, `openrouter`, `ollama`.

Exact endpoints/flags live in `backends/opencode.py` next to the code; the
protocol was verified against opencode 1.16.2.

### Removed: omp_rpc

The `omp_rpc` placeholder (remote-RPC model) and the experimental local-OMP
branch are both superseded. Unknown backend codes degrade: the editor still
renders shared groups and the effective-config preview flags the unknown
backend; runs against it fail loudly at dispatch.

## Implementation status

Landed and tested (full suite green, 1598 passed):

- `nightdesk/backends/` package: `base.py` contracts, `registry.py`,
  `claude_code.py`, `opencode.py` (+ `opencode_config.py`,
  `opencode_translate.py`, `opencode_driver.py`), `dummy.py`.
- Worker is backend-agnostic: `run_one.py` has no `if backend == ...`. The
  claude launch composition (cc-sessions mount, binary, credential env) moved
  into `ClaudeBackend.prepare_launch` / `after_run`.
- `build_bwrap_argv` gained `mounts` + `require_claude`; the legacy
  `cc_sessions_dir` param was kept (claude passes it through `LaunchPlan`) so
  the sandbox tests are unchanged.
- Provider entity end to end: model, migration `0019`, `domain/providers.py`,
  `/api/v1/providers` CRUD, `profiles.provider_id` / `backend_config` accepted
  by the profile API.
- `domain/backend_capabilities.py`: `omp_rpc` replaced by `opencode`; added a
  runtime `Capability` enum, `provider_kinds`, `requires_provider`.

Deferred (intentionally, to land on top of the in-flight `ui/theme-revamp`
branch which is rewriting the editor): the profile-editor provider picker and
capability-gated field hiding, and a providers settings pane. The old
`omp_rpc` form-processing in `api/routes/profiles.py` + `profile_pane.html` is
now dead (never reachable — the backend is gone from the registry) and is
removed as part of that editor pass rather than piecemeal here. opencode
profiles are fully creatable via the JSON API today.

The opencode HTTP driver is written against the verified 1.16.2 protocol and
unit-tested at the config/translation layer; a live end-to-end run was not
exercised in this pass (no inference credits spent).

## Adding a backend (the contract this design optimizes)

1. Write `src/nightdesk/backends/<name>.py`: a descriptor + the five
   methods. Register it in `registry.py`.
2. If it needs new editor fields, add a `FieldGroup` and reference its key
   in `group_keys`; backend-specific form fields render from the group.
3. Done. The worker, scheduler, transcript pipeline, review surface, and
   analytics need no changes.
