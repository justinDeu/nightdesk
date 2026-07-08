"""K8sExecutor — run a resolved turn in a per-run Kubernetes pod.

Host-side pod lifecycle only: preflight (reject non-git / remoteless), build the
RunSpec, create the per-run Secret + Pod, watch to a terminal phase (or cancel /
deadline), map the outcome to an ``ExecutionResult`` (reading the pod-uploaded
result sidecar), and tear the Secret + Pod down on every exit path. The pod does
the real work and streams transcript/diff/result back over the run token, so
this class never writes the transcript and never touches SQLite for run state.

No cluster is required to test this: it talks to a ``K8sClientProtocol``, and the
suite drives it with ``FakeK8sClient``. See
docs/design/session-suite/k8s-executor.md (Lifecycle & Failure Matrix).
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Callable, Optional

from nightdesk.backends import LaunchContext
from nightdesk.db.models import ConfigRow, Run
from nightdesk.domain.cost import RunUsage
from nightdesk.domain.k8s_config import K8sConfig, K8sConfigError
from nightdesk.domain.run_result import read_result_sidecar, result_sidecar_path
from nightdesk.executors.base import (
    ExecutionOutcome,
    Executor,
    ProvisionContext,
    ProvisionOutcome,
    ResolvedWorkspace,
    RunContext,
)
from nightdesk.executors.k8s import podspec
from nightdesk.executors.k8s.client import (
    FakeK8sClient,
    K8sClient,
    K8sClientProtocol,
    PodStatus,
    REASON_DEADLINE,
    REASON_IMAGE_PULL,
    REASON_OOM,
)
from nightdesk.runner.runspec import RunSpec
from nightdesk.worker.executor import ExecutionResult
from nightdesk.worker.workspace import WorkspaceError, WorkspaceSpec

log = logging.getLogger(__name__)

# Nominal in-pod working dir; the pod clones the repo here. Never a host path.
POD_WORKSPACE = Path("/workspace")


def _default_client_factory(cfg: K8sConfig) -> K8sClientProtocol:
    return K8sClient(in_cluster=cfg.in_cluster, kubeconfig_path=cfg.kubeconfig_path)


def _primary_git_spec(specs: list[WorkspaceSpec]) -> WorkspaceSpec:
    """The single primary git_worktree spec, or a WorkspaceError.

    v1 k8s runs only support a single primary git repo with a reachable remote
    (the pod clones origin). directory/in_place workspaces and additional linked
    workspaces are rejected at preflight per the MVP cutlines.
    """
    primary = next((s for s in specs if s.role == "primary"), None)
    if primary is None and specs:
        primary = specs[0]
    if primary is None:
        raise WorkspaceError("k8s run has no primary workspace")
    if primary.kind != "git_worktree":
        raise WorkspaceError(
            f"k8s execution target only supports git_worktree workspaces; "
            f"got {primary.kind!r}. Use the local target for directory workspaces."
        )
    return primary


def _resolve_remote(source_path: Optional[str]) -> str:
    """Resolve the clonable remote URL of a workspace's source repo.

    The pod has no host filesystem access, so it clones ``origin``; the host
    reads its URL here. A source repo with no ``origin`` remote can't be run on
    k8s (the pod would have nothing to clone) and is rejected at preflight.
    """
    if not source_path:
        raise WorkspaceError("k8s workspace has no source_path to resolve a remote from")
    try:
        r = subprocess.run(
            ["git", "-C", source_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError(f"could not read git remote for {source_path}: {exc}") from exc
    url = (r.stdout or "").strip()
    if r.returncode != 0 or not url:
        raise WorkspaceError(
            f"k8s runs require a reachable git remote; {source_path} has no "
            f"'origin' remote for the pod to clone"
        )
    return url


class K8sExecutor(Executor):
    code = "k8s"

    def __init__(
        self,
        *,
        client_factory: Callable[[K8sConfig], K8sClientProtocol] = _default_client_factory,
        poll_interval: float = 2.0,
        ready_timeout: float = 300.0,
    ):
        self._client_factory = client_factory
        self.poll_interval = poll_interval
        self.ready_timeout = ready_timeout

    # -- config ---------------------------------------------------------------

    def _config_from_session(self, session, api_url: str) -> K8sConfig:
        """Read + validate k8s config from a session the CALLER owns.

        Must not close ``session`` — ``reconcile_orphans`` hands us the worker
        tick's shared session and keeps using it after this returns; closing it
        would detach the tick's in-flight objects.
        """
        row = session.get(ConfigRow, 1)
        cfg = K8sConfig.from_config_row(row, api_url=api_url)
        cfg.validate()
        return cfg

    def _load_config(self, session_factory, api_url: str) -> K8sConfig:
        """Read + validate k8s config from a fresh, executor-owned session."""
        if session_factory is None:
            raise K8sConfigError("k8s executor needs a session factory to read config")
        session = session_factory()
        try:
            return self._config_from_session(session, api_url)
        finally:
            session.close()

    # -- provision ------------------------------------------------------------

    async def provision(self, ctx: ProvisionContext) -> ProvisionOutcome:
        # Fail fast (before pricing) if k8s is not runnably configured or the
        # workspace isn't a clonable git remote.
        self._load_config(ctx.session_factory, ctx.api_url)
        primary = _primary_git_spec(ctx.specs)
        _resolve_remote(primary.source_path)
        return ProvisionOutcome(
            workspace_dir=POD_WORKSPACE,
            bundle=None,
            git_dirs=[],
        )

    # -- execute --------------------------------------------------------------

    async def execute(self, ctx: RunContext) -> ExecutionOutcome:
        cfg = self._load_config(ctx.session_factory, ctx.base_env.get("NIGHTDESK_API_URL", ""))
        client = self._client_factory(cfg)

        primary = _primary_git_spec(ctx.workspace_specs)
        remote_url = _resolve_remote(primary.source_path)
        base_ref = primary.base_ref or "HEAD"
        branch = primary.branch or f"nightdesk/{ctx.ticket_id[:8]}/{ctx.run_id[:8]}"

        deadline = self._max_run_duration(ctx.session_factory)

        runspec = RunSpec(
            run_id=ctx.run_id,
            ticket_id=ctx.ticket_id,
            ticket_title=ctx.ticket_title,
            backend_code=ctx.backend.code,
            base_prompt=ctx.base_prompt,
            run_intent=ctx.run_intent,
            api_url=ctx.base_env.get("NIGHTDESK_API_URL", ""),
            run_token=ctx.base_env.get("NIGHTDESK_RUN_TOKEN", ""),
            remote_url=remote_url,
            base_ref=base_ref,
            branch=branch,
            spec=ctx.spec,
            endpoints=ctx.endpoints,
            primary_endpoint_id=(ctx.primary.id if ctx.primary is not None else None),
            model_assignments=ctx.model_assignments,
            base_env=_callback_env(ctx.base_env),
        )

        secret = podspec.build_secret(ctx.run_id, ctx.ticket_id, runspec.to_json())
        pod = podspec.build_pod(
            cfg, run_id=ctx.run_id, ticket_id=ctx.ticket_id,
            deadline_seconds=deadline,
        )
        ns = cfg.namespace

        result: ExecutionResult
        try:
            # Secret before the pod so the pod always mounts a populated Secret.
            client.create_secret(ns, secret["name"], secret["string_data"], secret["labels"])
            client.create_pod(ns, pod)
            status = await self._watch(client, ns, podspec.pod_name(ctx.run_id), ctx.cancel_event)
            result = self._map_result(ctx, status)
        except WorkspaceError:
            raise
        except Exception as exc:  # noqa: BLE001 - map any host-side k8s error
            log.exception("k8s executor error for run %s", ctx.run_id)
            result = ExecutionResult(
                exit_status="failed",
                error_summary=f"k8s executor error: {exc}",
                failure_kind="provision_error",
            )
        finally:
            # Tear down on EVERY exit path (Secret carries live creds + token).
            try:
                client.delete_pod(ns, podspec.pod_name(ctx.run_id))
            except Exception:
                log.exception("failed to delete pod for run %s", ctx.run_id)
            try:
                client.delete_secret(ns, podspec.secret_name(ctx.run_id))
            except Exception:
                log.exception("failed to delete secret for run %s", ctx.run_id)

        # The pod uploaded its diff sidecar (GET /diff serves it); tell run_one
        # to skip host diff and record the reported branch/base/head refs.
        reported = ResolvedWorkspace(
            kind="git_worktree",
            access=primary.access,
            source_path=primary.source_path,
            branch=branch,
            base_ref=base_ref,
            base_sha=self._reported(ctx, "base_sha"),
            head_sha=self._reported(ctx, "head_sha"),
            run_start_sha=self._reported(ctx, "run_start_sha"),
            retention=primary.retention,
            state="active",
        )
        # A minimal LaunchContext so run_one can still call backend.after_run
        # uniformly (no-op for k8s; the pod already published any session).
        launch_ctx = LaunchContext(
            spec=ctx.spec, endpoint=ctx.primary, run_id=ctx.run_id,
            ticket_id=ctx.ticket_id, workspace_dir=POD_WORKSPACE,
            scratch_root=POD_WORKSPACE, endpoints=ctx.endpoints,
            model_assignments=ctx.model_assignments,
        )
        return ExecutionOutcome(
            result=result,
            launch_ctx=launch_ctx,
            workspaces=[reported],
            diff_uploaded=True,
        )

    # -- watch + mapping ------------------------------------------------------

    async def _watch(self, client, ns, name, cancel_event) -> PodStatus:
        waited = 0.0
        became_ready = False
        while True:
            if cancel_event.is_set():
                client.delete_pod(ns, name)
                return PodStatus(phase="Failed", reason="Cancelled")
            status = client.read_pod_status(ns, name)
            if status is None:
                return PodStatus(phase="Failed", reason="PodLost",
                                 message="pod disappeared before finishing")
            if status.ready or status.phase == "Running":
                became_ready = True
            if status.terminal:
                return status
            # Never became Ready within the readiness budget -> scheduling /
            # image-pull failure (provision_error).
            if not became_ready and waited >= self.ready_timeout:
                client.delete_pod(ns, name)
                return PodStatus(phase="Failed", reason=status.reason or REASON_IMAGE_PULL,
                                 message="pod never became ready")
            await asyncio.sleep(self.poll_interval)
            waited += self.poll_interval

    def _map_result(self, ctx: RunContext, status: PodStatus) -> ExecutionResult:
        sidecar = read_result_sidecar(
            result_sidecar_path(Path(ctx.transcript_path).parent, ctx.run_id)
        )
        # Explicit host-side failures (cancel/deadline/oom/image-pull) win over a
        # possibly-stale sidecar; otherwise the pod's reported result is truth.
        reason = status.reason or ""
        if reason == "Cancelled":
            return ExecutionResult(exit_status="cancelled")
        if reason == REASON_DEADLINE or (
            status.phase == "Failed" and reason == REASON_DEADLINE
        ):
            return ExecutionResult(exit_status="failed",
                                   error_summary="run exceeded its deadline",
                                   failure_kind="timeout")
        if reason == REASON_OOM:
            return ExecutionResult(exit_status="failed",
                                   error_summary="pod was OOMKilled",
                                   failure_kind="oom")
        if reason in ("PodLost", "NodeLost"):
            return ExecutionResult(exit_status="failed",
                                   error_summary="pod was lost before reporting a result",
                                   failure_kind="pod_lost")
        if reason == REASON_IMAGE_PULL or status.message == "pod never became ready":
            return ExecutionResult(exit_status="failed",
                                   error_summary="pod never became ready (image pull / scheduling)",
                                   failure_kind="provision_error")

        if sidecar is not None:
            return _result_from_sidecar(sidecar)

        # Terminal without a mark_done -> the pod died mid-run.
        if status.phase == "Failed":
            # A non-zero pre-agent exit is a clone/preflight failure in-pod.
            if status.exit_code not in (None, 0):
                return ExecutionResult(exit_status="failed",
                                       error_summary="pod exited before reporting a result",
                                       failure_kind="workspace_error")
            return ExecutionResult(exit_status="worker_crash",
                                   error_summary="pod finished without reporting a result",
                                   failure_kind="pod_lost")
        # Succeeded but no sidecar: treat as success with no usage recorded.
        return ExecutionResult(exit_status="success")

    def _reported(self, ctx: RunContext, key: str) -> Optional[str]:
        sidecar = read_result_sidecar(
            result_sidecar_path(Path(ctx.transcript_path).parent, ctx.run_id)
        )
        if not sidecar:
            return None
        ws = sidecar.get("workspace") or {}
        return ws.get(key)

    def _max_run_duration(self, session_factory) -> int:
        if session_factory is None:
            return 7200
        session = session_factory()
        try:
            row = session.get(ConfigRow, 1)
            return int(getattr(row, "max_run_duration_seconds", 7200) or 7200)
        finally:
            session.close()

    # -- reconciliation -------------------------------------------------------

    def reconcile_orphans(self, session, *, host: str) -> None:
        """Adopt/GC labelled runner pods a crashed worker left behind.

        For each managed pod: if its Run is finished, GC the pod+secret; if the
        Run is unfinished and the pod is terminal without a result, mark it a
        worker crash; a still-running pod is left in place (adopted — its watch
        re-attaches when the run resumes). Pid-liveness recovery is bypassed for
        k8s runs; this reconciler is the authority.
        """
        try:
            cfg = self._config_from_session(session, "http://reconcile.invalid")
        except K8sConfigError:
            return
        client = self._client_factory(cfg)
        ns = cfg.namespace
        try:
            pods = client.list_pods(ns, podspec.label_selector())
        except Exception:
            log.exception("k8s reconcile: could not list pods")
            return
        from nightdesk.domain.runs import finish_run
        for pod in pods:
            run_id = pod["labels"].get(podspec.LABEL_RUN_ID)
            if not run_id:
                continue
            run = session.get(Run, run_id)
            status: PodStatus = pod["status"]
            if run is None or run.finished_at is not None:
                # Finished (or unknown) run -> GC the pod + secret.
                client.delete_pod(ns, pod["name"])
                client.delete_secret(ns, podspec.secret_name(run_id))
                continue
            if status.terminal:
                # Unfinished run, terminal pod, no mark_done -> worker crash.
                try:
                    finish_run(session, run_id, exit_status="worker_crash",
                               error_summary="pod terminated without reporting a result")
                    r = session.get(Run, run_id)
                    if r is not None:
                        r.failure_kind = "pod_lost"
                        session.commit()
                except Exception:
                    log.exception("k8s reconcile: could not finish orphaned run %s", run_id)
                client.delete_pod(ns, pod["name"])
                client.delete_secret(ns, podspec.secret_name(run_id))
            # else: still running -> adopt (leave in place).


def _callback_env(base_env: dict) -> dict:
    """Forward only the NIGHTDESK_* callback metadata to the pod.

    Provider creds ride in the RunSpec's spec/endpoints (rendered in-pod by
    prepare_launch); the host-side sandbox env (HOME/PATH/CLAUDE_CONFIG_DIR) is
    meaningless in the pod, which builds its own.
    """
    return {k: v for k, v in base_env.items() if k.startswith("NIGHTDESK_")}


def _result_from_sidecar(sidecar: dict) -> ExecutionResult:
    usage = None
    u = sidecar.get("usage")
    if isinstance(u, dict):
        usage = RunUsage(
            model=u.get("model"),
            input_tokens=int(u.get("input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            cache_read_tokens=int(u.get("cache_read_tokens") or 0),
            cache_write_tokens=int(u.get("cache_write_tokens") or 0),
            cost_usd=u.get("cost_usd"),
        )
    status = sidecar.get("exit_status") or "failed"
    return ExecutionResult(
        exit_status=status,
        error_summary=sidecar.get("error_summary"),
        session_id=sidecar.get("session_id"),
        session_ref=sidecar.get("session_ref"),
        usage=usage,
        usage_by_model=sidecar.get("usage_by_model"),
        failure_kind=("run_failed" if status == "failed" else None),
    )
