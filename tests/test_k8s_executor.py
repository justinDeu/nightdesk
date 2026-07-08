"""K8sExecutor lifecycle + failure matrix against FakeK8sClient (no cluster)."""
import asyncio
import json
import subprocess
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nightdesk.backends import get_backend
from nightdesk.db.models import Base, ConfigRow, Run, Ticket
from nightdesk.domain.permissions import PermissionSpec
from nightdesk.domain.run_result import result_sidecar_path, write_result_sidecar
from nightdesk.executors.base import ProvisionContext, RunContext
from nightdesk.executors.k8s import podspec
from nightdesk.executors.k8s.client import FakeK8sClient, PodStatus
from nightdesk.executors.k8s.executor import K8sExecutor
from nightdesk.worker.workspace import WorkspaceError, WorkspaceSpec


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def git_source(tmp_path):
    """A source repo whose 'origin' points at a bare remote (clonable by a pod)."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    (src / "README.md").write_text("hi\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "init")
    _git(src, "remote", "add", "origin", str(remote))
    _git(src, "branch", "-M", "main")
    _git(src, "push", "-u", "origin", "main")
    return src


@pytest.fixture
def cfg_engine(tmp_path):
    eng = create_engine("sqlite+pysqlite:///:memory:",
                        connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(ConfigRow(
            id=1, worktree_root=str(tmp_path / "wt"),
            transcript_root=str(tmp_path / "tr"),
            k8s_runner_image="ghcr.io/nd/runner:1",
            k8s_namespace="nd",
            max_run_duration_seconds=3600,
        ))
        s.commit()
    return eng


def _session_factory(eng):
    def factory():
        return Session(eng)
    return factory


def _run_ctx(tmp_path, eng, git_source, *, run_id="run1", ticket_id="tkt1"):
    transcript = tmp_path / "tr" / "conv.log"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    spec = WorkspaceSpec(role="primary", label="primary", kind="git_worktree",
                         access="read_write", source_path=str(git_source),
                         base_ref="main")
    return RunContext(
        run_id=run_id, ticket_id=ticket_id, ticket_title="t",
        base_prompt="do it", run_intent="first_run",
        spec=PermissionSpec(backend="claude_sdk"),
        backend=get_backend("claude_sdk"), primary=None, endpoints={},
        model_assignments={}, workspace_dir=tmp_path, worktree_root=tmp_path,
        workspace_specs=[spec], transcript_path=str(transcript),
        base_env={"NIGHTDESK_API_URL": "http://nd.example:8765",
                  "NIGHTDESK_RUN_TOKEN": "ndr_abc"},
        git_dirs=[], conversation_session_id=None, prior_turn=None,
        next_run_context=None, cancel_event=asyncio.Event(),
        on_session_id=None, session_factory=_session_factory(eng),
    )


def _executor(fake):
    return K8sExecutor(client_factory=lambda cfg: fake,
                       poll_interval=0.01, ready_timeout=0.05)


async def test_success_reads_result_sidecar(tmp_path, cfg_engine, git_source):
    fake = FakeK8sClient(default_script=[
        PodStatus(phase="Pending"),
        PodStatus(phase="Running", ready=True),
        PodStatus(phase="Succeeded", exit_code=0, reason="Completed"),
    ])
    ctx = _run_ctx(tmp_path, cfg_engine, git_source)
    # Simulate the pod's /result upload landing before terminal.
    write_result_sidecar(
        result_sidecar_path(tmp_path / "tr", ctx.run_id),
        {"exit_status": "success", "session_id": "s1",
         "usage": {"model": "claude-x", "input_tokens": 3, "output_tokens": 1,
                   "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.02},
         "workspace": {"base_sha": "aaa", "head_sha": "bbb", "run_start_sha": "aaa"}},
    )
    outcome = await _executor(fake).execute(ctx)
    assert outcome.result.exit_status == "success"
    assert outcome.result.session_id == "s1"
    assert outcome.result.usage.input_tokens == 3
    assert outcome.diff_uploaded is True
    assert outcome.workspaces[0].head_sha == "bbb"
    # Secret created before pod, and both torn down.
    order = [c[0] for c in fake.calls]
    assert order.index("create_secret") < order.index("create_pod")
    assert ("delete_pod", "nd", podspec.pod_name(ctx.run_id)) in fake.calls
    assert ("delete_secret", "nd", podspec.secret_name(ctx.run_id)) in fake.calls


async def test_agent_reported_failure(tmp_path, cfg_engine, git_source):
    fake = FakeK8sClient(default_script=[PodStatus(phase="Succeeded", exit_code=0)])
    ctx = _run_ctx(tmp_path, cfg_engine, git_source)
    write_result_sidecar(
        result_sidecar_path(tmp_path / "tr", ctx.run_id),
        {"exit_status": "failed", "error_summary": "agent said no"},
    )
    outcome = await _executor(fake).execute(ctx)
    assert outcome.result.exit_status == "failed"
    assert outcome.result.failure_kind == "run_failed"


@pytest.mark.parametrize("reason,kind", [
    ("OOMKilled", "oom"),
    ("DeadlineExceeded", "timeout"),
])
async def test_terminal_reasons(tmp_path, cfg_engine, git_source, reason, kind):
    fake = FakeK8sClient(default_script=[
        PodStatus(phase="Running", ready=True),
        PodStatus(phase="Failed", exit_code=137, reason=reason),
    ])
    ctx = _run_ctx(tmp_path, cfg_engine, git_source)
    outcome = await _executor(fake).execute(ctx)
    assert outcome.result.exit_status == "failed"
    assert outcome.result.failure_kind == kind


async def test_image_pull_never_ready(tmp_path, cfg_engine, git_source):
    fake = FakeK8sClient(default_script=[
        PodStatus(phase="Pending", reason="ImagePullBackOff"),
    ])
    ctx = _run_ctx(tmp_path, cfg_engine, git_source)
    outcome = await _executor(fake).execute(ctx)
    assert outcome.result.exit_status == "failed"
    assert outcome.result.failure_kind == "provision_error"
    # Pod deleted on the give-up path too.
    assert ("delete_pod", "nd", podspec.pod_name(ctx.run_id)) in fake.calls


async def test_pod_lost(tmp_path, cfg_engine, git_source):
    fake = FakeK8sClient(default_script=[PodStatus(phase="Running", ready=True)])
    # After it goes Running, delete it out from under the watch -> read None.
    ctx = _run_ctx(tmp_path, cfg_engine, git_source)
    ex = _executor(fake)

    orig = fake.read_pod_status
    calls = {"n": 0}

    def flaky(ns, name):
        calls["n"] += 1
        if calls["n"] >= 2:
            return None
        return orig(ns, name)

    fake.read_pod_status = flaky
    outcome = await ex.execute(ctx)
    assert outcome.result.failure_kind == "pod_lost"


async def test_cancel_deletes_pod(tmp_path, cfg_engine, git_source):
    fake = FakeK8sClient(default_script=[PodStatus(phase="Running", ready=True)])
    ctx = _run_ctx(tmp_path, cfg_engine, git_source)
    ctx.cancel_event.set()
    outcome = await _executor(fake).execute(ctx)
    assert outcome.result.exit_status == "cancelled"
    assert ("delete_pod", "nd", podspec.pod_name(ctx.run_id)) in fake.calls


async def test_provision_rejects_directory_workspace(tmp_path, cfg_engine):
    ctx = ProvisionContext(
        run_id="r", ticket_id="t", worktree_root=tmp_path,
        specs=[WorkspaceSpec(role="primary", label="p", kind="directory",
                             access="read_write", source_path=str(tmp_path))],
        run_intent="first_run", reuse_existing_worktrees=False,
        fresh_worktree_paths=False, transcript_path=str(tmp_path / "t.log"),
        api_url="http://nd.example:8765", session_factory=_session_factory(cfg_engine),
    )
    with pytest.raises(WorkspaceError):
        await _executor(FakeK8sClient()).provision(ctx)


async def test_provision_rejects_missing_remote(tmp_path, cfg_engine):
    # A git dir with no origin remote.
    src = tmp_path / "noremote"
    src.mkdir()
    _git(src, "init")
    ctx = ProvisionContext(
        run_id="r", ticket_id="t", worktree_root=tmp_path,
        specs=[WorkspaceSpec(role="primary", label="p", kind="git_worktree",
                             access="read_write", source_path=str(src))],
        run_intent="first_run", reuse_existing_worktrees=False,
        fresh_worktree_paths=False, transcript_path=str(tmp_path / "t.log"),
        api_url="http://nd.example:8765", session_factory=_session_factory(cfg_engine),
    )
    with pytest.raises(WorkspaceError):
        await _executor(FakeK8sClient()).provision(ctx)


async def test_provision_ok_and_no_host_bundle(tmp_path, cfg_engine, git_source):
    ctx = ProvisionContext(
        run_id="r", ticket_id="t", worktree_root=tmp_path,
        specs=[WorkspaceSpec(role="primary", label="p", kind="git_worktree",
                             access="read_write", source_path=str(git_source),
                             base_ref="main")],
        run_intent="first_run", reuse_existing_worktrees=False,
        fresh_worktree_paths=False, transcript_path=str(tmp_path / "t.log"),
        api_url="http://nd.example:8765", session_factory=_session_factory(cfg_engine),
    )
    out = await _executor(FakeK8sClient()).provision(ctx)
    assert out.bundle is None
    assert out.git_dirs == []


async def test_provision_unconfigured_fails_fast(tmp_path, git_source):
    # Config row with no runner image -> K8sConfigError at provision.
    from nightdesk.domain.k8s_config import K8sConfigError
    eng = create_engine("sqlite+pysqlite:///:memory:",
                        connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(ConfigRow(id=1, worktree_root=str(tmp_path), transcript_root=str(tmp_path)))
        s.commit()
    ctx = ProvisionContext(
        run_id="r", ticket_id="t", worktree_root=tmp_path,
        specs=[WorkspaceSpec(role="primary", label="p", kind="git_worktree",
                             access="read_write", source_path=str(git_source),
                             base_ref="main")],
        run_intent="first_run", reuse_existing_worktrees=False,
        fresh_worktree_paths=False, transcript_path=str(tmp_path / "t.log"),
        api_url="http://nd.example:8765", session_factory=_session_factory(eng),
    )
    with pytest.raises(K8sConfigError):
        await _executor(FakeK8sClient()).provision(ctx)


async def test_provision_unreachable_api_fails_fast(tmp_path, cfg_engine, git_source):
    from nightdesk.domain.k8s_config import K8sConfigError
    ctx = ProvisionContext(
        run_id="r", ticket_id="t", worktree_root=tmp_path,
        specs=[WorkspaceSpec(role="primary", label="p", kind="git_worktree",
                             access="read_write", source_path=str(git_source),
                             base_ref="main")],
        run_intent="first_run", reuse_existing_worktrees=False,
        fresh_worktree_paths=False, transcript_path=str(tmp_path / "t.log"),
        api_url="http://127.0.0.1:8765", session_factory=_session_factory(cfg_engine),
    )
    with pytest.raises(K8sConfigError):
        await _executor(FakeK8sClient()).provision(ctx)


def test_reconcile_gcs_finished_and_crashes_unfinished(tmp_path, cfg_engine):
    fake = FakeK8sClient()
    ns = "nd"
    # Finished run's pod -> GC. Unfinished run's terminal pod -> worker_crash.
    with Session(cfg_engine) as s:
        t = Ticket(id="tk", title="t", status="running")
        s.add(t)
        finished = Run(id="rf", ticket_id="tk", started_at=datetime.now(timezone.utc),
                       finished_at=datetime.now(timezone.utc), worktree_path="",
                       transcript_path="", host="h", exit_status="success")
        unfinished = Run(id="ru", ticket_id="tk", started_at=datetime.now(timezone.utc),
                         worktree_path="", transcript_path="", host="h")
        s.add_all([finished, unfinished])
        s.commit()
    fake.create_pod(ns, podspec.build_pod(
        __k8s_cfg(tmp_path), run_id="rf", ticket_id="tk", deadline_seconds=60))
    fake.create_pod(ns, podspec.build_pod(
        __k8s_cfg(tmp_path), run_id="ru", ticket_id="tk", deadline_seconds=60))
    fake.script_pod("nd-run-rf", [PodStatus(phase="Succeeded", exit_code=0)])
    fake.script_pod("nd-run-ru", [PodStatus(phase="Failed", exit_code=1, reason="Error")])

    with Session(cfg_engine) as s:
        _executor(fake).reconcile_orphans(s, host="h")
        ru = s.get(Run, "ru")
        assert ru.finished_at is not None
        assert ru.exit_status == "worker_crash"
        assert ru.failure_kind == "pod_lost"
    assert ("delete_pod", ns, "nd-run-rf") in fake.calls
    assert ("delete_pod", ns, "nd-run-ru") in fake.calls


def __k8s_cfg(tmp_path):
    from nightdesk.domain.k8s_config import K8sConfig
    return K8sConfig(api_url="http://nd.example:8765", runner_image="img:1",
                     namespace="nd")
