# Kubernetes Cloud-Sandbox Executor — Design

## Patterns & Conventions Found

- **Backend ABC package** (`src/nightdesk/backends/`): `Backend(ABC)` with `prepare_launch(ctx) -> LaunchPlan` and `async execute(req) -> ExecutionResult`, self-registering via `backends/registry.py`, dispatched by `get_backend(code)`. `run_one.py` has zero `if backend == …`. This is the template the executor layer must mirror (`backends/base.py:140`).
- **The real execution seam is NOT `worker/executor.py`.** That `Executor` Protocol (`ShellExecutor`/`DummyExecutor`) is only a test override wired through `RunOneConfig.executor` (`run_one.py:1237`). Production execution goes through `backend.execute(request)` (`run_one.py:1241`), which for Claude calls `ClaudeExecutor().run(req)` — spawning the bwrap subprocess AND translating stdout into canonical transcript events (`claude_executor.py:80-326`).
- **Launch composition is host-neutral, then host-bound in two steps**: `backend.prepare_launch(ctx)` returns an environment-agnostic `LaunchPlan(cmd, env, mounts, cc_sessions_dir, secret_env_keys)` (`claude_code.py:122`), then `build_bwrap_argv(...)` binds it to host paths (`sandbox.py:349`). The bwrap step is the only host-specific composition.
- **Transcript = append-only NDJSON file**, tailed by SSE (`transcript.py:111`). The renderer never cares who wrote the file.
- **Run diff = computed on demand from the host workspace** (`domain/diff.py:compute_workspace_diff`, `runs.py:39`). Non-git workspaces already use an *uploaded-artifact* pattern: a JSON snapshot sidecar keyed by `(transcript_root, run_id, workspace_id)` (`run_one.py:_capture_fs_snapshots`, `domain/fs_snapshot.py`) — the template for a pod-uploaded diff.
- **Per-run scoped token** `ndr_…` injected as `NIGHTDESK_RUN_TOKEN` + `NIGHTDESK_API_URL` (`run_one.py:_build_env:618`). `SELF_SCOPES` already declares `run.append_transcript.self` and `run.mark_done.self` (`run_tokens.py:28`) — **but no API route consumes them yet.** The write-back endpoints are the missing piece.
- **Credentials → sandbox env**: profile creds decrypted host-side (`_profile_to_spec` + `ProfileSecretBox`); endpoint creds render into `LaunchPlan.env` with `secret_env_keys` (`claude_code.py:_render_anthropic:75`). This env dict is what a pod Secret must carry.
- **Worker supervision**: subprocess mode spawns `nightdesk-run-ticket <id>` (`main.py:_spawn_subproc`); orphan recovery keys off `Run.pid` liveness (`heartbeat.py:recover_orphaned_runs`). Cancellation = SIGTERM; `_cancel_watcher` polls (`run_one.py:698`).
- **Migrations**: head `0022_providers_and_endpoints`; additive-only, inspector-guarded.

## Architecture Decision

**Chosen:** new `src/nightdesk/executors/` package with an `Executor` ABC — boundary: **"provision the isolated environment, run the agent to completion, produce the run's transcript + diff artifacts."**

- **`LocalExecutor`** — behavior-preserving extraction of `run_one`'s current phases 2–4 (workspace prep → `prepare_launch` → `build_bwrap_argv` → `backend.execute` → workspace-resolution recording). Zero behavior change.
- **`K8sExecutor`** — per-run Secret + Pod running a slim `nightdesk-runner` entrypoint. The pod clones the repo from a remote at `base_ref`, runs the *same backend* inside the pod, streams transcript chunks + a final structured diff back over the run's `ndr_…` token. Host side only orchestrates pod lifecycle → `ExecutionResult`.

`run_one` stays the target-agnostic orchestrator of DB state (run row, token, pricing snapshot, conversation, transitions). It selects `get_executor(profile.execution_target)` once. No `if k8s` anywhere.

**Rationale:** the executor is the orthogonal axis to backends (*where* vs *which harness*); they compose — `K8sExecutor` runs `get_backend(code)` inside the pod, so opencode/claude ride the same pod path. Streaming back over the run-token API means host SSE + diff endpoints work unchanged; the pod is just another "agent driving the API." DB orchestration stays single-homed on the host (cluster never touches SQLite).

**Rejected:**
- *Executor at the `worker/executor.py` Protocol boundary*: below `backend.execute`, Claude-specific, would re-write per backend and still run host-side prep. Wrong layer.
- *Host rsync/tar the worktree into the pod*: forces host↔pod reach, serializes trees through the API server, still needs remote creds for delivery. Clone-from-remote is simpler (Omnigent model). Documented future option for remoteless repos.
- *bwrap inside the pod*: a pod is already the isolation boundary; use `securityContext` + optional `runtimeClassName` (gVisor/Kata) instead.

## MVP Cutlines

**In v1:** `execution_target='k8s'` per profile ('local' default untouched); **git_worktree workspaces with a reachable git remote only** (pod clones origin at base_ref, checks out run branch); `claude_sdk` in-pod (opencode architecturally compatible, not in v1 test surface); live transcript write-back; structured diff upload at finish; lifecycle → `exit_status`/`failure_kind`; cancel = pod delete; `activeDeadlineSeconds` = max_run_duration; orphan pod reconciliation; one Secret per run, deleted with the pod.

**NOT in v1 (explicit):**
- `directory`/`in_place` workspaces on k8s — rejected at preflight with clear `WorkspaceError`.
- Additional/linked workspaces — single primary repo only.
- Pushing the result branch / merging — v1 delivers review artifacts (transcript + diff). Branch push gated behind `commit_on_finish` AND configured push creds; absent creds → pod commits locally, only the uploaded diff survives.
- `base_ref` stacking across local-only branches — fresh clone sees only pushed refs; surfaced as a provision warning.
- `continue`/resume of a prior pod session — v1 pods are single-shot; continue falls back to fresh-context resume (re-clone) with the existing `fell_back_to_fresh_context` breadcrumb.
- In-cluster nightdesk deployment, autoscaling, multi-cluster, GPUs beyond `nodeSelector` + resources.
- ssh-agent forwarding — k8s git auth = HTTPS token or deploy key from a referenced cluster Secret.

## Component Design

### `Executor` ABC — `src/nightdesk/executors/base.py`
Contract for "run this resolved run in an isolated environment and produce its artifacts." Imports: `domain.permissions`, `backends` (types), `worker.executor` (`ExecutionResult`). Must not import `run_one` (arrow: `worker → executors → backends → domain`).

```python
@dataclass
class RunContext:
    run_id: str; ticket_id: str; conversation_id: str
    backend_code: str
    spec: PermissionSpec                  # merged profile+overrides (creds/env decrypted)
    endpoints: dict[str, ResolvedEndpoint]
    primary_endpoint_id: Optional[str]
    model_assignments: dict[str, Assignment]
    workspace_specs: list[WorkspaceSpec]
    prompt: str; run_intent: str
    resume_session_id: Optional[str]
    transcript_path: Path; seq_start: int
    api_url: str; run_token: str
    cancel_event: asyncio.Event
    on_session_id: Optional[Callable[[str], None]]

@dataclass
class ExecutionOutcome:
    result: ExecutionResult
    workspaces: list[ResolvedWorkspace]   # what to write into TicketWorkspace rows
    diff_uploaded: bool = False           # k8s stored a diff sidecar; skip host diff

class Executor(ABC):
    code: ClassVar[str]                   # "local" | "k8s"
    @abstractmethod
    async def execute(self, ctx: RunContext) -> ExecutionOutcome: ...
    def reconcile_orphans(self, session, *, host: str) -> None: ...  # no-op for local
```

### `LocalExecutor` — `src/nightdesk/executors/local.py`
Today's path extracted verbatim: `prepare_workspace_bundle`, `_apply_workspace_permissions`, `resolve_tool_paths`, `backend.prepare_launch`, `_build_env`, `build_bwrap_argv`, `backend.execute`, `backend.after_run`, workspace-resolution capture, fs-snapshot capture, commit_on_finish, cleanup. `diff_uploaded=False`. **Must preserve the `RunOneConfig.executor` test seam** (`DummyExecutor` injection keeps working). This extraction (run_one lines ~892–1439) is the one meaningful refactor; pure motion, gated by the existing suite.

### `K8sExecutor` — `src/nightdesk/executors/k8s/executor.py`
Host-side pod lifecycle: preflight (reject non-git/remoteless), build `RunSpec` JSON, create per-run `Secret` (env + token) and `Pod` (labels `nightdesk/run-id`, `nightdesk/ticket-id`), wait Ready, watch to terminal or cancel/deadline, map to `ExecutionResult`, tear down, `reconcile_orphans` adopts/GCs labeled pods. Transcript file populated by the API append endpoint (pod POSTs); `K8sExecutor` never writes it.

### `K8sClient` — `src/nightdesk/executors/k8s/client.py`
The only module importing `kubernetes`. `create_secret/create_pod/read_pod_status/delete_pod/delete_secret/list_pods/read_pod_log`. In-cluster config if `k8s_in_cluster`, else kubeconfig path. `FakeK8sClient` in tests = in-memory pod state machine — **no cluster in CI.**

### `nightdesk-runner` (in-pod entrypoint) — `src/nightdesk/runner/`
Pod PID 1. Reads a `RunSpec` (mounted file/env):
1. Clone `RunSpec.remote_url` at `base_ref`, `git checkout -b <branch>`; record `run_start_sha`.
2. Reconstruct `LaunchContext`, call `get_backend(code).prepare_launch(ctx)` — same code as host.
3. Run the agent (no bwrap; the pod is the sandbox). Reuse `ClaudeExecutor`'s translate path with a `TranscriptSink` swap (file → HTTP).
4. `POST /api/v1/runs/{rid}/transcript` (batched NDJSON) live; session id via result POST.
5. At finish: compute structured diff in-pod (`run_start_sha..HEAD`) → `POST /api/v1/runs/{rid}/diff` + final result/usage → mark_done.
6. Exit code reflects outcome → pod phase.

**Why not reuse `nightdesk-run-ticket`:** it is host-coupled (opens SQLite, issues tokens, subprocess supervisor). The pod needs a token-scoped, DB-less runner; it reuses the pure pieces (`backends`, `claude_executor` translation, `domain/diff`, transcript schema, `_sdk_runner`). Only the sink (file→HTTP) and source (worktree→clone) differ.

### Runner image
`python:3.12-slim` + git + nodejs + pinned `claude` binary + nightdesk wheel. Entrypoint `nightdesk-runner`. No SQLite/FastAPI. `securityContext`: non-root, read-only root FS where possible, no privilege escalation, drop all caps; optional `runtimeClassName` from config.

## Data Flow (K8s)

1. Scheduler picks ticket → `run_one` does all DB setup exactly as today.
2. `get_executor("k8s").execute(ctx)`.
3. Preflight → `RunSpec` → `Secret` (env incl. token/API URL/provider creds) + `Pod` (labeled, deadline, resources/nodeSelector).
4. Pod: clone → `prepare_launch` → agent → transcript POSTs; host SSE serves them live unchanged.
5. Host `_cancel_watcher` sets `cancel_event` → `K8sExecutor` deletes pod.
6. Pod finish: diff POST (sidecar) + result/usage (mark_done).
7. `K8sExecutor` maps pod terminal phase → `ExecutionOutcome(result, workspaces=[reported branch/base/head sha], diff_uploaded=True)`.
8. `run_one` finish: records `TicketWorkspace` (no resolved host path; SHAs from pod report), prices from usage, → `review`. Cleanup deletes Secret+Pod.
9. `GET /runs/{rid}/diff`: sidecar present → serve it; else compute from host workspace. Frontend unchanged.

## API Additions (run-token write-back surface)

All `require_scopes(...)` + `enforce_self_ticket`, consuming already-declared self-scopes:

- **`POST /api/v1/runs/{rid}/transcript`** — scope `run.append_transcript.self`. Batched NDJSON canonical events, appended via `transcript.append_event`; validates `type`/`seq` per line. Makes host SSE light up for pod runs.
- **`POST /api/v1/runs/{rid}/diff`** — scope `run.mark_done.self`. `_diff_to_json` RunDiff shape → diff sidecar (`diff_sidecar_path`, mirroring `fs_snapshot`). `GET …/diff` prefers the sidecar.
- **`POST /api/v1/runs/{rid}/result`** — scope `run.mark_done.self`. Persists `exit_status`, `error_summary`, `session_id`/`session_ref`, usage (tokens, `model_used`, `usage_by_model`) so host pricing runs identically. Host still owns the authoritative `finish_run` transition.

Route ownership: extend `api/routes/runs.py` with a run-token-guarded sub-router (existing routes stay admin cookie/bearer).

## Config + Profile Changes

**Migration `0025_execution_target`** (`down_revision="0022_providers_and_endpoints"`, re-parented at master merge). Additive, inspector-guarded:
- `profiles.execution_target TEXT NOT NULL DEFAULT 'local'`
- `config`: `k8s_kubeconfig_path`, `k8s_in_cluster BOOL DEFAULT 0`, `k8s_namespace DEFAULT 'nightdesk'`, `k8s_runner_image`, `k8s_cpu_request/limit`, `k8s_mem_request/limit`, `k8s_node_selector JSON '{}'`, `k8s_runtime_class`, `k8s_git_credentials_secret` (name of pre-existing cluster Secret for clone/push auth).

Schemas: `execution_target: Literal["local","k8s"] = "local"` on Profile create/update/out; `k8s_*` on ConfigOut/Update. Settings UI: "Cloud sandbox (Kubernetes)" section (two-pane convention); profile editor: execution-target selector next to backend selector. `execution_target` is a **profile field, never a ticket-prompt concept**.

Selection: `executors/registry.py` (`register/get_executor`), seeded with `LocalExecutor` always, `K8sExecutor` when k8s config present.

## Lifecycle & Failure Matrix

| Situation | Pod signal | `Run.exit_status` | `failure_kind` |
|---|---|---|---|
| Agent success | `Succeeded` + mark_done(success) | `success` | — |
| Agent reported failure | mark_done(failed) | `failed` | `run_failed` |
| Cancel | host `delete_pod` | `cancelled` | — |
| Deadline hit | `Failed`/`DeadlineExceeded` | `failed` | `timeout` |
| Image pull / scheduling failure | never Ready within `pod_ready_timeout` | `failed` | `provision_error` |
| OOMKilled | container terminated `OOMKilled` | `failed` | `oom` |
| Node death, no mark_done | phase lost/Failed w/o result | `worker_crash` | `pod_lost` |
| Clone/preflight failure in pod | non-zero exit pre-agent | `failed` | `workspace_error` |
| Worker restart mid-run | pod still Running → adopted | unchanged | — |

**Orphan reconciliation:** on worker start + each tick, list pods by `nightdesk/run-id`; Run finished → delete pod+secret; Run unfinished + pod terminal w/o mark_done → `worker_crash`/`pod_lost`; both running → adopt (re-attach watch). Pid-liveness path bypassed for k8s runs; the reconciler is the authority.

## Test Plan (no cluster in CI)

- `FakeK8sClient` scripted state machine (Pending→Running→Succeeded/Failed, injectable ImagePullBackOff/DeadlineExceeded/OOMKilled/node-loss). Unit-test every failure-matrix row + cancel (assert delete_pod).
- Podspec golden tests: labels, deadline, resources, nodeSelector, runtimeClass, envFrom, securityContext.
- Secret lifecycle: created before pod, deleted in every exit path.
- Reconciler: fake pods + Run rows in the four adopt/GC/crash states.
- New API endpoints: run-token auth (accept `ndr_…`, reject cross-ticket via `enforce_self_ticket`), transcript append writes NDJSON, diff sidecar + GET preference, result persists usage.
- `nightdesk-runner`: stubbed `claude_agent_sdk` + local git fixture as "remote"; clone→run→POST transcript/diff against a test API.
- **`LocalExecutor` extraction: the entire existing worker/run_one suite must stay green unchanged** (regression gate); `DummyExecutor` injection still works.
- Registry: `get_executor('local')` always; `get_executor('k8s')` clear error when unconfigured.

## File-by-File Implementation Checklist

**Phase 1 — Executor seam (no k8s yet; suite must stay green):**
- [ ] `src/nightdesk/executors/__init__.py`, `base.py`, `registry.py`, `local.py`
- [ ] `src/nightdesk/worker/run_one.py` — replace inline execution body with `get_executor(...).execute(ctx)`; keep all DB/pricing/token/transition logic

**Phase 2 — API write-back (usable by any agent, not just k8s):**
- [ ] `api/routes/runs.py` — POST transcript/diff/result with `require_scopes`
- [ ] `domain/diff.py` — `diff_sidecar_path` + helpers; GET prefers sidecar
- [ ] Endpoint tests

**Phase 3 — In-pod runner:**
- [ ] `src/nightdesk/runner/{__main__,main,clone,api_sink}.py`; `nightdesk-runner` console script
- [ ] `docker/runner/Dockerfile`

**Phase 4 — K8s executor + config:**
- [ ] `executors/k8s/{client,podspec,executor}.py`; `domain/k8s_config.py`
- [ ] `alembic/versions/0025_execution_target.py`
- [ ] `db/models.py` — `Profile.execution_target`, `ConfigRow.k8s_*`
- [ ] `api/schemas.py` — profile + config fields
- [ ] `worker/main.py` — `executor.reconcile_orphans` alongside `recover_orphaned_runs`
- [ ] `pyproject.toml` — `kubernetes>=29` as optional `k8s` extra
- [ ] Settings UI + profile editor fields

## Critical Details

- **Secret tradeoff:** creds + run token live in a namespaced Secret. Mitigate: one Secret per run, `ownerReference` to the Pod, explicit delete on every exit path, token already expires at `max_run_duration + grace`, dedicated locked-down namespace. Never bake creds into the image or a ConfigMap.
- **API reachability:** default `bind_host=127.0.0.1` is unreachable from a cluster — k8s mode requires a cluster-routable API address (or tunnel/Service). Validated at executor init; fail fast.
- **State ownership:** SQLite host-only; pods stateless/DB-less, HTTP-only via scoped token. Migration race story unchanged.
- **Behavior preservation:** Phase-1 extraction is the highest-risk change; pure motion, full suite is the gate. K8s code lands only after it's green.
