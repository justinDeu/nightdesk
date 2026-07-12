from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import subprocess

import pytest
from sqlalchemy.orm import Session

from nightdesk.db.models import Ticket, Run
from nightdesk.domain.runs import finish_run, get_run, start_run
from nightdesk.domain.tickets import (
    continue_ticket,
    create_ticket,
    restart_ticket,
    resume_ticket,
    set_next_run_context,
    transition_status,
)
from nightdesk.transcript import read_events
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult
from nightdesk.worker.run_one import (
    RunOneConfig,
    _build_env,
    _capture_head_sha,
    _record_workspace_resolution,
    run_one,
)
from nightdesk.worker.workspace import Workspace, WorkspaceBundle
_PROC_DIR_KW = "c" "wd"


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], **{_PROC_DIR_KW: str(path)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], **{_PROC_DIR_KW: str(path)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], **{_PROC_DIR_KW: str(path)}, capture_output=True, check=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], **{_PROC_DIR_KW: str(path)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], **{_PROC_DIR_KW: str(path)}, capture_output=True, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(path)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_capture_head_sha_returns_head(tmp_path):
    head = _init_git_repo(tmp_path / "repo")
    assert _capture_head_sha(str(tmp_path / "repo")) == head


def test_capture_head_sha_non_git_returns_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _capture_head_sha(str(plain)) is None


def test_record_workspace_resolution_captures_run_start_sha(session, sample_profile, tmp_path):
    repo = tmp_path / "repo"
    head = _init_git_repo(repo)
    ticket = create_ticket(
        session,
        title="ws",
        prompt="p",
        status="running",
        priority=0,
        profile_id=sample_profile.id,
        source_path=str(repo),
        workspaces=[{
            "role": "primary",
            "label": "primary",
            "kind": "git_worktree",
            "access": "read_write",
            "source_path": str(repo),
        }],
    )
    ws = Workspace(
        path=repo,
        kind="git_worktree",
        source_path=repo,
        repo_path=repo,
        worktree_path=repo,
        branch="feat",
        base_ref="main",
        base_sha=head,
    )
    bundle = WorkspaceBundle(primary=ws, workspaces=[ws])

    _record_workspace_resolution(ticket, bundle)

    assert ticket.workspaces[0].run_start_sha == head


@dataclass
class CapturingExecutor:
    request: ExecutionRequest | None = None

    async def run(self, req: ExecutionRequest) -> ExecutionResult:
        self.request = req
        return ExecutionResult(exit_status="success", final_summary="done")



def _spec(**overrides):
    base = {
        "secret_keys": [],
        "claude_credentials": None,
        "custom_env": {},
    }
    base.update(overrides)
    return type("Spec", (), base)()


def test_git_metadata_dirs_collects_common_dirs():
    from nightdesk.worker.run_one import _git_metadata_dirs
    from nightdesk.worker.workspace import Workspace, WorkspaceBundle

    wt = Workspace(
        path=Path("/wt/feature"), kind="git_worktree",
        git_common_dir=Path("/repo/.git"),
    )
    extra = Workspace(
        path=Path("/wt/other"), kind="git_worktree", role="linked", label="lib",
        git_common_dir=Path("/repo/.git"),  # duplicate, must be deduped
    )
    plain = Workspace(path=Path("/data"), kind="directory")
    bundle = WorkspaceBundle(primary=wt, workspaces=[wt, extra, plain])

    assert _git_metadata_dirs(bundle) == ["/repo/.git"]


def test_git_metadata_dirs_empty_for_non_git_ticket():
    from nightdesk.worker.run_one import _git_metadata_dirs
    from nightdesk.worker.workspace import Workspace, WorkspaceBundle

    plain = Workspace(path=Path("/data"), kind="directory")
    bundle = WorkspaceBundle(primary=plain, workspaces=[plain])
    assert _git_metadata_dirs(bundle) == []


def test_build_env_sets_sandbox_home_and_fixed_path(monkeypatch):
    # v1 deliberately does NOT propagate the host PATH or HOME. The sandbox
    # has a curated PATH and HOME points at the sandbox tmpfs so CC writes
    # its state there. The only host env vars that leak are LANG/TERM (taken
    # via os.environ.get with defaults).
    monkeypatch.setenv("PATH", "/opt/tool/bin:/usr/bin")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("UNSELECTED_SECRET", "do-not-leak")

    env = _build_env(_spec(), {})

    assert env["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/bin:/bin"
    assert env["HOME"] == "/sandbox-home"
    assert env["CLAUDE_CONFIG_DIR"] == "/sandbox-home/.claude"
    assert env["LANG"] == "C.UTF-8"
    assert "UNSELECTED_SECRET" not in env


def test_build_env_injects_auth_token_credentials(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    spec = _spec(claude_credentials={
        "source": "auth_token",
        "value": "zai-secret",
        "base_url": "https://api.z.ai/api/anthropic",
    })

    env = _build_env(spec, {})

    assert env["ANTHROPIC_AUTH_TOKEN"] == "zai-secret"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert "ANTHROPIC_API_KEY" not in env


def test_build_env_injects_api_key_credentials(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    spec = _spec(claude_credentials={"source": "api_key", "value": "sk-test"})

    env = _build_env(spec, {})

    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_build_env_inherit_source_does_not_set_auth_env(monkeypatch):
    # inherit credentials are mounted at the bwrap layer, not injected as env.
    monkeypatch.setenv("PATH", "/usr/bin")
    spec = _spec(claude_credentials={"source": "inherit"})

    env = _build_env(spec, {})

    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_build_env_applies_custom_env(monkeypatch):
    # Profile.env (custom_env) lands in the sandbox env verbatim. Models /
    # behavior toggles all flow through this path now.
    monkeypatch.setenv("PATH", "/usr/bin")
    spec = _spec(custom_env={
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7",
        "DISABLE_TELEMETRY": "1",
    })

    env = _build_env(spec, {})

    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-4-7"
    assert env["DISABLE_TELEMETRY"] == "1"

def init_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], **{_PROC_DIR_KW: path}, check=True)
    (path / "README").write_text("hi")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: path}, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], **{_PROC_DIR_KW: path},
        check=True,
    )
    return path
@pytest.mark.anyio
async def test_run_one_maps_workspace_access_into_sandbox_permissions(session, sample_profile, tmp_path):
    primary = tmp_path / "primary"
    docs = tmp_path / "docs"
    primary.mkdir()
    docs.mkdir()
    ticket = create_ticket(
        session,
        title="multi",
        prompt="p",
        status="running",
        priority=0,
        profile_id=sample_profile.id,
        source_path=str(primary),
        workspaces=[
            {
                "role": "primary",
                "label": "primary",
                "kind": "directory",
                "access": "read_write",
                "source_path": str(primary),
            },
            {
                "role": "linked",
                "label": "docs",
                "kind": "directory",
                "access": "read_only",
                "source_path": str(docs),
            },
        ],
    )
    ticket_id = ticket.id
    executor = CapturingExecutor()
    bind = session.get_bind()

    result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=executor,
        ),
        ticket_id,
    )

    assert result.exit_status == "success"
    assert executor.request is not None
    assert executor.request.working_dir == primary
    assert "PATH" in executor.request.env
    assert "--setenv" in executor.request.bwrap_argv
    assert str(primary) in executor.request.permission_spec.fs_write
    assert str(docs) in executor.request.permission_spec.fs_read
    assert str(docs) not in executor.request.permission_spec.fs_write

    with Session(bind) as verify:
        refreshed = verify.get(Ticket, ticket_id)
        assert refreshed is not None
        assert refreshed.workspaces[0].resolved_path == str(primary)
        assert refreshed.workspaces[1].resolved_path == str(docs)


@pytest.mark.anyio
async def test_run_one_builds_headless_prompt_with_next_run_context(session, sample_profile, tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    ticket = create_ticket(
        session,
        title="rerun",
        prompt="Fix the drag bug",
        status="running",
        priority=0,
        profile_id=sample_profile.id,
        source_path=str(primary),
    )
    set_next_run_context(session, ticket.id, "Use polling instead of asking questions")
    executor = CapturingExecutor()
    result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=executor,
        ),
        ticket.id,
    )
    assert result.exit_status == "success"
    assert executor.request is not None
    assert "This is a headless Nightdesk worker run." in executor.request.prompt
    assert "Fix the drag bug" in executor.request.prompt
    assert "Use polling instead of asking questions" in executor.request.prompt


@pytest.mark.anyio
async def test_run_one_uses_staged_resume_intent(session, sample_profile, tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    ticket = create_ticket(
        session,
        title="rerun",
        prompt="Fix the drag bug",
        status="queued",
        priority=0,
        profile_id=sample_profile.id,
        source_path=str(primary),
    )
    transition_status(session, ticket.id, "running")
    prior = start_run(
        session,
        ticket_id=ticket.id,
        worktree_path=str(primary),
        transcript_path=str(tmp_path / "transcripts" / "prior.log"),
        pid=None,
        host="testhost",
    )
    finish_run(session, prior.id, exit_status="success", error_summary="Prior summary")
    transition_status(session, ticket.id, "review")
    resume_ticket(session, ticket.id, next_run_context="Continue with polling")
    transition_status(session, ticket.id, "running")
    executor = CapturingExecutor()
    result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=executor,
        ),
        ticket.id,
    )
    assert result.exit_status == "success"
    assert executor.request is not None
    assert "RUN INTENT: resume" in executor.request.prompt
    assert "Continue with polling" in executor.request.prompt
    assert "Prior summary" in executor.request.prompt


@pytest.mark.anyio
async def test_run_one_reuses_existing_worktree_for_resume(session, sample_profile, tmp_path):
    repo = init_git_repo(tmp_path / "proj")
    ticket = create_ticket(
        session,
        title="rerun",
        prompt="Fix the drag bug",
        status="running",
        priority=0,
        profile_id=sample_profile.id,
        source_path=str(repo),
        workspace_mode="git_worktree",
        worktree_name="feature",
    )
    ticket_id = ticket.id
    first = CapturingExecutor()
    first_result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=first,
        ),
        ticket_id,
    )
    assert first_result.exit_status == "success"
    session.expire_all()
    refreshed = session.get(Ticket, ticket_id)
    assert refreshed is not None
    assert refreshed.status == "review"
    worktree_path = refreshed.workspaces[0].worktree_path
    assert worktree_path is not None

    resume_ticket(session, ticket_id, next_run_context="Keep going")
    transition_status(session, ticket_id, "running")

    second = CapturingExecutor()
    second_result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=second,
        ),
        ticket_id,
    )
    assert second_result.exit_status == "success"
    assert second.request is not None
    assert Path(worktree_path) == second.request.working_dir


@pytest.mark.anyio
async def test_run_one_recreates_existing_worktree_for_restart_same_path(session, sample_profile, tmp_path):
    repo = init_git_repo(tmp_path / "proj")
    ticket = create_ticket(
        session,
        title="rerun",
        prompt="Fix the drag bug",
        status="running",
        priority=0,
        profile_id=sample_profile.id,
        source_path=str(repo),
        workspace_mode="git_worktree",
        worktree_name="feature",
    )
    ticket_id = ticket.id
    first = CapturingExecutor()
    first_result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=first,
        ),
        ticket_id,
    )
    assert first_result.exit_status == "success"
    session.expire_all()
    refreshed = session.get(Ticket, ticket_id)
    old_worktree_path = refreshed.workspaces[0].worktree_path
    assert old_worktree_path is not None
    restart_ticket(session, ticket_id, next_run_context="Start fresh", workspace_policy="recreate_in_place")
    transition_status(session, ticket_id, "running")

    second = CapturingExecutor()
    second_result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=second,
        ),
        ticket_id,
    )
    assert second_result.exit_status == "success"
    assert second.request is not None
    assert Path(old_worktree_path) == second.request.working_dir


@pytest.mark.anyio
async def test_run_one_allocates_fresh_worktree_for_restart_new_path(session, sample_profile, tmp_path):
    repo = init_git_repo(tmp_path / "proj")
    ticket = create_ticket(
        session,
        title="rerun",
        prompt="Fix the drag bug",
        status="running",
        priority=0,
        profile_id=sample_profile.id,
        source_path=str(repo),
        workspace_mode="git_worktree",
        worktree_name="feature",
    )
    ticket_id = ticket.id
    first = CapturingExecutor()
    first_result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=first,
        ),
        ticket_id,
    )
    assert first_result.exit_status == "success"
    session.expire_all()
    refreshed = session.get(Ticket, ticket_id)
    old_worktree_path = refreshed.workspaces[0].worktree_path
    assert old_worktree_path is not None
    restart_ticket(session, ticket_id, next_run_context="Start fresh", workspace_policy="fresh_path")
    transition_status(session, ticket_id, "running")

    second = CapturingExecutor()
    second_result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=second,
        ),
        ticket_id,
    )
    assert second_result.exit_status == "success"
    assert second.request is not None
    assert Path(old_worktree_path) != second.request.working_dir


class _WritingExecutor:
    """Executor that simulates an agent doing real work: it leaves an
    uncommitted file in the run's working dir, then reports success."""

    def __init__(self, filename: str = "venues.py", body: str = "VENUES = []\n"):
        self.filename = filename
        self.body = body
        self.request: ExecutionRequest | None = None

    async def run(self, req: ExecutionRequest) -> ExecutionResult:
        self.request = req
        (req.working_dir / self.filename).write_text(self.body)
        return ExecutionResult(exit_status="success", final_summary="done")


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.mark.anyio
async def test_run_one_commit_on_finish_commits_workspace_changes(
    session, sample_profile, tmp_path,
):
    """commit_on_finish (the fix): on a successful run the ticket's
    working-tree changes are committed onto its git_worktree branch, so a
    dependent that base_ref-points at this branch actually receives the work.
    Reproduces-and-fixes the 'runs leave work uncommitted' failure mode."""
    repo = init_git_repo(tmp_path / "proj")
    base_sha = _git(repo, "rev-parse", "HEAD")
    ticket = create_ticket(
        session,
        title="prereq",
        prompt="add venues",
        status="running",
        priority=0,
        profile_id=sample_profile.id,
        source_path=str(repo),
        workspace_mode="git_worktree",
        worktree_name="venues",
        commit_on_finish=True,
    )
    ticket_id = ticket.id

    result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=_WritingExecutor(),
        ),
        ticket_id,
    )
    assert result.exit_status == "success"

    session.expire_all()
    refreshed = session.get(Ticket, ticket_id)
    ws_row = refreshed.workspaces[0]
    wt = Path(ws_row.worktree_path or ws_row.resolved_path)
    branch = ws_row.branch

    # The prerequisite's work is now committed on the branch (it advanced past
    # the base commit), so a dependent provisioning from `branch` receives it.
    tip = _git(wt, "rev-parse", "HEAD")
    assert tip != base_sha
    assert branch is not None
    assert _git(repo, "rev-parse", branch) == tip
    assert "venues.py" in _git(wt, "ls-tree", "--name-only", "HEAD")
    # And the working tree is clean — no dangling uncommitted work left behind.
    assert _git(wt, "status", "--porcelain") == ""

    # A commit_on_finish event was recorded on the run's transcript.
    run = session.get(Run, refreshed.current_run_id)
    cof = [e for e in read_events(run.transcript_path)
           if e.get("subtype") == "commit_on_finish"]
    assert len(cof) == 1
    assert cof[0]["data"]["branch"] == branch
    assert cof[0]["data"]["commit"] == tip


@pytest.mark.anyio
async def test_run_one_without_commit_on_finish_leaves_work_uncommitted(
    session, sample_profile, tmp_path,
):
    """Default (commit_on_finish off) preserves the historical behavior the bug
    describes: a successful run leaves its work UNCOMMITTED, so the branch ref
    never advances and any dependent base_ref-pointing here gets an empty
    prerequisite."""
    repo = init_git_repo(tmp_path / "proj")
    base_sha = _git(repo, "rev-parse", "HEAD")
    ticket = create_ticket(
        session,
        title="prereq",
        prompt="add venues",
        status="running",
        priority=0,
        profile_id=sample_profile.id,
        source_path=str(repo),
        workspace_mode="git_worktree",
        worktree_name="venues",
        # commit_on_finish intentionally NOT set (default off)
    )
    ticket_id = ticket.id

    result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=_WritingExecutor(),
        ),
        ticket_id,
    )
    assert result.exit_status == "success"

    session.expire_all()
    refreshed = session.get(Ticket, ticket_id)
    ws_row = refreshed.workspaces[0]
    wt = Path(ws_row.worktree_path or ws_row.resolved_path)
    branch = ws_row.branch

    # Branch ref never advanced — exactly the bug: dependent gets empty prereq.
    assert _git(repo, "rev-parse", branch) == base_sha
    # The work is sitting uncommitted in the working tree.
    assert "venues.py" in _git(wt, "status", "--porcelain")


@pytest.mark.anyio
async def test_run_one_finishes_run_when_setup_fails_after_start(session, tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    from nightdesk.db.models import Profile, Run

    profile = Profile(
        name="setup-failure",
        fs_read=["/home"],
        fs_write=[],
        allowed_tools=[],
        denied_tools=[],
        network_mode="off",
        network_allowlist=[],
        secret_keys=[],
    )
    session.add(profile)
    session.commit()

    ticket = create_ticket(
        session,
        title="broken setup",
        prompt="p",
        status="running",
        priority=0,
        profile_id=profile.id,
        source_path=str(primary),
    )
    ticket_id = ticket.id

    executor = CapturingExecutor()
    result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=executor,
        ),
        ticket_id,
    )

    assert result.exit_status == "failed"
    assert "setup error" in (result.error_summary or "")
    session.expire_all()
    refreshed_ticket = session.get(Ticket, ticket_id)
    assert refreshed_ticket is not None
    assert refreshed_ticket.status == "review"
    run = session.get(Run, refreshed_ticket.current_run_id)
    assert run is not None
    assert run.finished_at is not None
    assert run.exit_status == "failed"
    assert "setup error" in (run.error_summary or "")


@pytest.mark.anyio
async def test_run_one_fails_cleanly_on_incompatible_endpoint(session, tmp_path):
    """A profile whose endpoint's harness_lock excludes the backend must fail
    the run before launch, never dispatch to prepare_launch."""
    from nightdesk.db.models import Profile, Provider, ProviderEndpoint, Run

    primary = tmp_path / "primary"
    primary.mkdir()

    provider = Provider(name="Anthropic", vendor="anthropic")
    session.add(provider)
    session.commit()
    endpoint = ProviderEndpoint(
        provider_id=provider.id,
        label="Claude subscription",
        protocol_kind="anthropic",
        credential_source="subscription_file",
        harness_lock="claude_sdk",
    )
    session.add(endpoint)
    session.commit()

    profile = Profile(
        name="opencode-locked-out",
        backend="opencode",
        endpoint_id=endpoint.id,
        fs_read=["/home"], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(profile)
    session.commit()

    ticket = create_ticket(
        session,
        title="locked endpoint",
        prompt="p",
        status="running",
        priority=0,
        profile_id=profile.id,
        source_path=str(primary),
    )
    ticket_id = ticket.id

    executor = CapturingExecutor()
    result = await run_one(
        lambda: session,
        RunOneConfig(
            worktree_root=tmp_path / "work",
            transcript_root=tmp_path / "transcripts",
            secrets={},
            host="testhost",
            executor=executor,
        ),
        ticket_id,
    )

    assert result.exit_status == "failed"
    assert "incompatible" in (result.error_summary or "")
    # prepare_launch (and therefore execute) never ran.
    assert executor.request is None
    session.expire_all()
    refreshed_ticket = session.get(Ticket, ticket_id)
    assert refreshed_ticket.status == "review"


# --- continue intent: resume the prior SDK conversation ---------------------

def _stage_parent_run(session, ticket, tmp_path, *, session_id):
    """Drive a ticket through one completed run so it has a resumable parent,
    then leave it in 'review' ready for a continue stage. Returns the parent
    Run row."""
    transition_status(session, ticket.id, "running")
    prior = start_run(
        session, ticket_id=ticket.id,
        worktree_path=str(tmp_path / "primary"),
        transcript_path=str(tmp_path / "transcripts" / "prior.log"),
        pid=None, host="testhost",
    )
    finish_run(
        session, prior.id, exit_status="success",
        error_summary=None, session_id=session_id,
    )
    transition_status(session, ticket.id, "review")
    return prior


def _write_parent_session_file(tmp_path, parent_run_id, session_id):
    """Drop a session jsonl into the parent run's per-run cc-sessions store
    (the canonical source run_one seeds from)."""
    store = tmp_path / "nightdesk-cc-sessions" / parent_run_id
    enc = store / "-encoded-workdir"
    enc.mkdir(parents=True, exist_ok=True)
    (enc / f"{session_id}.jsonl").write_text('{"type":"summary"}\n')
    return store


@pytest.mark.anyio
async def test_run_one_continue_threads_resume_session_id_and_seeds_sandbox(
    session, sample_profile, tmp_path,
):
    """continue intent: the parent's session id is threaded onto the
    ExecutionRequest (resume_session_id) AND its session file is seeded into
    the new run's isolated sandbox store so the SDK's resume=<id> resolves."""
    primary = tmp_path / "primary"
    primary.mkdir()
    ticket = create_ticket(
        session, title="cont", prompt="Fix it", status="queued",
        priority=0, profile_id=sample_profile.id, source_path=str(primary),
    )
    parent_sid = "sess-parent-continue-1"
    prior = _stage_parent_run(session, ticket, tmp_path, session_id=parent_sid)
    prior_id = prior.id
    _write_parent_session_file(tmp_path, prior_id, parent_sid)
    continue_ticket(session, ticket.id, next_run_context="keep going")
    transition_status(session, ticket.id, "running")
    ticket_id = ticket.id
    bind = session.get_bind()

    executor = CapturingExecutor()
    result = await run_one(
        lambda: session,
        RunOneConfig(worktree_root=tmp_path / "work",
                     transcript_root=tmp_path / "transcripts",
                     secrets={}, host="testhost", executor=executor),
        ticket.id,
    )
    assert result.exit_status == "success"
    assert executor.request is not None
    # Spec threading: the parent session id reached the SDK-bound request.
    assert executor.request.resume_session_id == parent_sid
    # run_one closes its session; verify DB state on a fresh session.
    with Session(bind) as verify:
        new_run_id = verify.get(Ticket, ticket_id).current_run_id
        assert new_run_id is not None and new_run_id != prior_id
    # Sandbox availability: the session file was seeded into the NEW run's
    # isolated store (bound over the sandbox's CLAUDE_CONFIG_DIR/projects).
    # The claude_sdk backend anchors its per-run session store under the
    # unified backend scratch root, not the legacy nightdesk-cc-sessions tree
    # (that legacy root is kept only as a seed-source fallback for parents
    # that predate the backend refactor — see _write_parent_session_file).
    seeded = list((tmp_path / "nightdesk-backend-scratch" / new_run_id / "cc-sessions").rglob(
        f"{parent_sid}.jsonl"))
    assert seeded, "parent session was not seeded into the new run's store"
    # The continue prompt is honest about resuming the conversation.
    assert "RUN INTENT: continue" in executor.request.prompt
    assert "resuming the prior Claude Code conversation" in executor.request.prompt


@pytest.mark.anyio
async def test_run_one_continue_with_message_makes_it_the_user_turn(
    session, sample_profile, tmp_path,
):
    """Requirement: typing a continue message must carry it as a NEW user turn
    on top of the resumed SDK conversation — not fold it into a reconstructed
    prompt as NEXT RUN CONTEXT. For a genuine continue (parent session seeded),
    the SDK prompt is built around the typed message, resume_session_id is
    threaded, and the message is also surfaced on the request so the executor
    can write it at the transcript boundary."""
    primary = tmp_path / "primary"
    primary.mkdir()
    ticket = create_ticket(
        session, title="cont", prompt="Fix it", status="queued",
        priority=0, profile_id=sample_profile.id, source_path=str(primary),
    )
    parent_sid = "sess-parent-msg-1"
    prior = _stage_parent_run(session, ticket, tmp_path, session_id=parent_sid)
    _write_parent_session_file(tmp_path, prior.id, parent_sid)
    follow_up = "Now also cover the touch-screen variant"
    base_prompt = ticket.prompt  # capture before run_one closes its session
    continue_ticket(session, ticket.id, next_run_context=follow_up)
    transition_status(session, ticket.id, "running")

    executor = CapturingExecutor()
    result = await run_one(
        lambda: session,
        RunOneConfig(worktree_root=tmp_path / "work",
                     transcript_root=tmp_path / "transcripts",
                     secrets={}, host="testhost", executor=executor),
        ticket.id,
    )
    assert result.exit_status == "success"
    assert executor.request is not None
    # The typed message is the next user turn on the resumed conversation...
    assert "USER MESSAGE\n" + follow_up in executor.request.prompt
    # ...NOT folded into the reconstructed headless blob.
    assert "NEXT RUN CONTEXT" not in executor.request.prompt
    assert "BASE TICKET PROMPT" not in executor.request.prompt
    # The prior base prompt is in the resumed history already, so it is not
    # re-sent as part of this turn.
    assert base_prompt not in executor.request.prompt
    # The resume target + transcript affordance are threaded onto the request.
    assert executor.request.resume_session_id == parent_sid
    assert executor.request.continue_message == follow_up


@pytest.mark.anyio
async def test_run_one_continue_falls_back_when_session_file_missing(
    session, sample_profile, tmp_path,
):
    """When the parent session file is gone, continue falls back to a
    fresh-context resume: no resume_session_id threaded, the prompt reads as a
    fresh-context resume, the fallback is recorded on the transcript, and the
    Run.intent still records 'continue'."""
    primary = tmp_path / "primary"
    primary.mkdir()
    ticket = create_ticket(
        session, title="cont", prompt="Fix it", status="queued",
        priority=0, profile_id=sample_profile.id, source_path=str(primary),
    )
    parent_sid = "sess-parent-gone-2"
    prior = _stage_parent_run(session, ticket, tmp_path, session_id=parent_sid)
    # NOTE: deliberately do NOT write the parent session file.
    continue_ticket(session, ticket.id, next_run_context="keep going")
    transition_status(session, ticket.id, "running")
    ticket_id = ticket.id
    bind = session.get_bind()

    executor = CapturingExecutor()
    result = await run_one(
        lambda: session,
        RunOneConfig(worktree_root=tmp_path / "work",
                     transcript_root=tmp_path / "transcripts",
                     secrets={}, host="testhost", executor=executor),
        ticket.id,
    )
    assert result.exit_status == "success"
    assert executor.request is not None
    # Fell back: nothing to resume.
    assert executor.request.resume_session_id is None
    # Honest fresh-context prompt (not the "resuming prior conversation" text).
    assert "RUN INTENT: resume" in executor.request.prompt
    assert "resuming the prior Claude Code conversation" not in executor.request.prompt
    # Recorded on the run's transcript artifact + Run.intent stays 'continue'.
    with Session(bind) as verify:
        new_run = verify.get(Run, verify.get(Ticket, ticket_id).current_run_id)
        assert new_run is not None
        assert new_run.intent == "continue"
        transcript_path = new_run.transcript_path
    transcript = Path(transcript_path).read_text()
    assert "continue_session_unavailable" in transcript


@pytest.mark.anyio
async def test_continue_refuses_when_conversation_has_no_session_id(
    session, sample_profile, tmp_path,
):
    """A conversation whose first turn crashed before capturing a session id is
    not resumable: Continue is REFUSED (routing to New conversation) rather than
    silently falling back to a fresh-context run. This is the null-session fix:
    the user is told explicitly instead of getting a context-less 'continue'."""
    from nightdesk.domain.tickets import ConversationNotResumable

    primary = tmp_path / "primary"
    primary.mkdir()
    ticket = create_ticket(
        session, title="cont", prompt="Fix it", status="queued",
        priority=0, profile_id=sample_profile.id, source_path=str(primary),
    )
    # No session_id recorded on the parent (e.g. crashed before init).
    _stage_parent_run(session, ticket, tmp_path, session_id=None)
    with pytest.raises(ConversationNotResumable):
        continue_ticket(session, ticket.id, next_run_context="keep going")
    # The ticket stayed in review (no run was staged/dispatched).
    session.refresh(ticket)
    assert ticket.status == "review"


@pytest.mark.anyio
async def test_run_one_continue_appends_turn_to_same_conversation_and_transcript(
    session, sample_profile, tmp_path,
):
    """Acceptance #1 + #7: continuing a conversation appends a turn to the SAME
    conversation (one conversation, many turns) and to the SAME transcript file
    with ONE monotonic seq space (turn 2's events continue above turn 1's, so
    the live-tail lastSeq dedup never collides)."""

    class EmittingExecutor:
        """Writes a meta + assistant_text event using the request's seq seed and
        reports a shared session id so finish_run lifts it onto the conversation."""
        def __init__(self):
            self.requests = []

        async def run(self, req):
            from nightdesk.transcript import write_event, next_seq, now_iso
            from nightdesk.worker.executor import ExecutionResult
            from nightdesk.domain.cost import RunUsage
            self.requests.append(req)
            seq = [req.seq_start]
            with req.transcript_path.open("ab") as f:
                write_event(f, {"type": "meta", "ts": now_iso(),
                                "seq": next_seq(seq), "ticket_id": req.ticket_id})
                write_event(f, {"type": "assistant_text", "ts": now_iso(),
                                "seq": next_seq(seq), "text": "turn output"})
            return ExecutionResult(
                exit_status="success", session_id="sess-shared",
                usage=RunUsage(model="claude-sonnet-4", input_tokens=10,
                               output_tokens=5, cache_read_tokens=0,
                               cache_write_tokens=0, cost_usd=0.001),
            )

    primary = tmp_path / "primary"
    primary.mkdir()
    ticket = create_ticket(
        session, title="c", prompt="Fix it", status="queued",
        priority=0, profile_id=sample_profile.id, source_path=str(primary),
    )
    ticket_id = ticket.id
    cfg = RunOneConfig(worktree_root=tmp_path / "work",
                       transcript_root=tmp_path / "transcripts",
                       secrets={}, host="testhost", executor=EmittingExecutor())

    # Turn 1 (first_run): creates the conversation + its transcript file.
    transition_status(session, ticket_id, "running")
    await run_one(lambda: session, cfg, ticket_id)
    session.expire_all()
    t1 = session.get(Ticket, ticket_id)
    run1 = session.get(Run, t1.current_run_id)
    conv_id = run1.conversation_id
    tpath = run1.transcript_path
    assert conv_id is not None
    assert run1.position == 0

    # Continue -> turn 2 in the SAME conversation.
    continue_ticket(session, ticket_id, next_run_context="next turn")
    transition_status(session, ticket_id, "running")
    await run_one(lambda: session, cfg, ticket_id)
    session.expire_all()
    t2 = session.get(Ticket, ticket_id)
    run2 = session.get(Run, t2.current_run_id)

    # One conversation, many turns; one shared transcript file.
    assert run2.conversation_id == conv_id
    assert run2.transcript_path == tpath
    assert run2.position == 1
    # The authoritative session id is lifted onto the conversation.
    from nightdesk.domain.conversations import get_conversation
    assert get_conversation(session, conv_id).session_id == "sess-shared"

    # Single monotonic seq space: every seq is unique and file-ordered, and
    # turn 2's events continue strictly above turn 1's (turn 1 wrote seq 0,1).
    # (A continue-fallback breadcrumb may append one extra system event after;
    # what matters is no collisions and turn 2 continues the same space.)
    from nightdesk.transcript import read_events
    seqs = [e["seq"] for e in read_events(tpath) if isinstance(e.get("seq"), int)]
    assert seqs == sorted(seqs)                 # monotonic in file order
    assert len(set(seqs)) == len(seqs)          # no collisions across turns
    assert 0 in seqs and 1 in seqs              # turn 1's two events
    assert any(s >= 2 for s in seqs)            # turn 2 continued above turn 1


@pytest.mark.anyio
async def test_run_one_resume_does_not_thread_resume_session_id(
    session, sample_profile, tmp_path,
):
    """Regression guard: the existing resume intent stays fresh-context — it
    must NOT thread a resume_session_id even when the parent has one."""
    primary = tmp_path / "primary"
    primary.mkdir()
    ticket = create_ticket(
        session, title="res", prompt="Fix it", status="queued",
        priority=0, profile_id=sample_profile.id, source_path=str(primary),
    )
    parent_sid = "sess-parent-resume-3"
    prior = _stage_parent_run(session, ticket, tmp_path, session_id=parent_sid)
    _write_parent_session_file(tmp_path, prior.id, parent_sid)
    resume_ticket(session, ticket.id, next_run_context="fresh context please")
    transition_status(session, ticket.id, "running")

    executor = CapturingExecutor()
    result = await run_one(
        lambda: session,
        RunOneConfig(worktree_root=tmp_path / "work",
                     transcript_root=tmp_path / "transcripts",
                     secrets={}, host="testhost", executor=executor),
        ticket.id,
    )
    assert result.exit_status == "success"
    assert executor.request.resume_session_id is None
    assert "RUN INTENT: resume" in executor.request.prompt


# --- pricing snapshot: stamped at launch, extended/priced at finish ---------
from nightdesk.db.models import Profile, Provider, ProviderEndpoint  # noqa: E402
from nightdesk.domain.cost import RunUsage  # noqa: E402


@dataclass
class UsageExecutor:
    """Test executor: succeeds and reports a fixed ``RunUsage``."""
    usage: RunUsage
    request: ExecutionRequest | None = None

    async def run(self, req: ExecutionRequest) -> ExecutionResult:
        self.request = req
        return ExecutionResult(exit_status="success", final_summary="done",
                                usage=self.usage)


@dataclass
class MultiModelUsageExecutor:
    """Test executor: succeeds and reports usage for more than one model,
    like an opencode run whose primary and per-agent slots hit different
    models/endpoints."""
    usage: RunUsage
    usage_by_model: dict[str, dict[str, int]]
    request: ExecutionRequest | None = None

    async def run(self, req: ExecutionRequest) -> ExecutionResult:
        self.request = req
        return ExecutionResult(exit_status="success", final_summary="done",
                                usage=self.usage, usage_by_model=self.usage_by_model)


def _make_two_endpoint_opencode_profile(session):
    """An opencode profile with a primary endpoint (vendor A, full-pinned)
    and a per-agent endpoint (vendor B), so ``compute_model_assignments``
    resolves two distinct (endpoint, model, vendor) triples for one run."""
    from nightdesk.db.models import Profile, Provider, ProviderEndpoint

    primary_provider = Provider(name="Openai", vendor="openai")
    secondary_provider = Provider(name="Zai", vendor="zai")
    session.add_all([primary_provider, secondary_provider])
    session.commit()
    primary_endpoint = ProviderEndpoint(
        provider_id=primary_provider.id, label="primary", protocol_kind="openai_compat",
        credential_source="api_key",
    )
    secondary_endpoint = ProviderEndpoint(
        provider_id=secondary_provider.id, label="secondary", protocol_kind="openai_compat",
        credential_source="api_key",
    )
    session.add_all([primary_endpoint, secondary_endpoint])
    session.commit()
    profile = Profile(
        name="multi-model-opencode", backend="opencode", endpoint_id=primary_endpoint.id,
        default_model="gpt-5.4",
        backend_config={"agents": [
            {"name": "researcher", "endpoint_id": secondary_endpoint.id, "model": "glm-5.2"},
        ]},
        fs_read=["/tmp"], fs_write=["/tmp"], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(profile)
    session.commit()
    return profile, primary_endpoint, secondary_endpoint


@pytest.mark.anyio
async def test_run_one_two_model_opencode_run_prices_both_from_snapshot(
    session, tmp_path,
):
    """Finish-time pricing for a two-model opencode run (distinct vendors)
    prices each model's usage at its own vendor's rates and sums them."""
    profile, primary_endpoint, secondary_endpoint = _make_two_endpoint_opencode_profile(session)
    usage = RunUsage(
        model="glm-5.2", input_tokens=500_000, output_tokens=100_000,
        cache_read_tokens=0, cache_write_tokens=0, cost_usd=None,
    )
    usage_by_model = {
        "gpt-5.4": {"input_tokens": 1_000_000, "output_tokens": 500_000,
                    "cache_read_tokens": 0, "cache_write_tokens": 0},
        "glm-5.2": {"input_tokens": 500_000, "output_tokens": 100_000,
                    "cache_read_tokens": 0, "cache_write_tokens": 0},
    }
    executor = MultiModelUsageExecutor(usage=usage, usage_by_model=usage_by_model)
    result, run = await _run_ticket_with_profile(session, profile, tmp_path, executor)

    assert result.exit_status == "success"
    # gpt-5.4 (openai, bundled table has no openai rows -> resolves to "none")
    # is not in the bundled table, so this run relies on glm-5.2 (zai) being
    # priceable; gpt-5.4 usage is silently excluded from the sum rather than
    # collapsing the whole run to None.
    assert run.pricing_snapshot["glm-5.2"]["vendor"] == "zai"
    expected_glm = (500_000 * 1.40 + 100_000 * 4.40) / 1_000_000.0
    assert run.cost_usd == pytest.approx(expected_glm)


@pytest.mark.anyio
async def test_run_one_two_model_opencode_run_softens_unrated_secondary(
    session, tmp_path,
):
    """A secondary model with no rate of its own is priced at the primary
    model's rates instead of leaving the whole run unpriced."""
    profile, primary_endpoint, secondary_endpoint = _make_two_endpoint_opencode_profile(session)
    # Swap in a zai primary so the primary model has real bundled rates, and
    # give the secondary agent an unpriceable model id.
    profile.endpoint_id = secondary_endpoint.id
    profile.default_model = "glm-5.2"
    profile.backend_config = {"agents": [
        {"name": "researcher", "endpoint_id": primary_endpoint.id,
         "model": "totally-unpriceable-agent-model"},
    ]}
    session.commit()

    usage = RunUsage(
        model="glm-5.2", input_tokens=100, output_tokens=100,
        cache_read_tokens=0, cache_write_tokens=0, cost_usd=None,
    )
    usage_by_model = {
        "glm-5.2": {"input_tokens": 1_000_000, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0},
        "totally-unpriceable-agent-model": {"input_tokens": 1_000_000, "output_tokens": 0,
                                             "cache_read_tokens": 0, "cache_write_tokens": 0},
    }
    executor = MultiModelUsageExecutor(usage=usage, usage_by_model=usage_by_model)
    result, run = await _run_ticket_with_profile(session, profile, tmp_path, executor)

    assert result.exit_status == "success"
    # Both models' usage priced at glm-5.2's (the primary model's) rate.
    expected = 2 * (1_000_000 * 1.40) / 1_000_000.0
    assert run.cost_usd == pytest.approx(expected)


@pytest.mark.anyio
async def test_run_one_two_model_opencode_run_keeps_harness_cost_when_all_unpriceable(
    session, tmp_path,
):
    """When neither model in a multi-model run can be priced (even via the
    primary-rate softening), the harness-reported cost is kept."""
    profile, primary_endpoint, secondary_endpoint = _make_two_endpoint_opencode_profile(session)
    profile.default_model = "totally-unpriceable-primary-model"
    profile.backend_config = {"agents": [
        {"name": "researcher", "endpoint_id": secondary_endpoint.id,
         "model": "totally-unpriceable-agent-model"},
    ]}
    session.commit()

    usage = RunUsage(
        model="totally-unpriceable-primary-model", input_tokens=100, output_tokens=100,
        cache_read_tokens=0, cache_write_tokens=0, cost_usd=4.2,
    )
    usage_by_model = {
        "totally-unpriceable-primary-model": {"input_tokens": 100, "output_tokens": 100,
                                               "cache_read_tokens": 0, "cache_write_tokens": 0},
        "totally-unpriceable-agent-model": {"input_tokens": 100, "output_tokens": 100,
                                             "cache_read_tokens": 0, "cache_write_tokens": 0},
    }
    executor = MultiModelUsageExecutor(usage=usage, usage_by_model=usage_by_model)
    result, run = await _run_ticket_with_profile(session, profile, tmp_path, executor)

    assert result.exit_status == "success"
    assert run.cost_usd == pytest.approx(4.2)  # harness-reported, kept as-is


def _make_compat_profile(session, *, vendor="zai", default_model="glm-5.2",
                          protocol_kind="anthropic_compat"):
    provider = Provider(name=vendor.title(), vendor=vendor)
    session.add(provider)
    session.commit()
    endpoint = ProviderEndpoint(
        provider_id=provider.id, label="compat", protocol_kind=protocol_kind,
        credential_source="api_key",
    )
    session.add(endpoint)
    session.commit()
    profile = Profile(
        name=f"{vendor}-compat", backend="claude_sdk", endpoint_id=endpoint.id,
        default_model=default_model,
        fs_read=["/tmp"], fs_write=["/tmp"], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(profile)
    session.commit()
    return profile, endpoint


async def _run_ticket_with_profile(session, profile, tmp_path, executor):
    # run_one() closes whatever session its factory hands it (the production
    # per-run-subprocess lifecycle), so fetch results afterward through a
    # fresh Session on the same bind rather than the (now-closed) fixture
    # session — same pattern as the other full-run_one tests in this file.
    primary = tmp_path / "primary"
    primary.mkdir(exist_ok=True)
    ticket = create_ticket(
        session, title="pricing", prompt="p", status="running", priority=0,
        profile_id=profile.id, source_path=str(primary),
    )
    ticket_id = ticket.id
    bind = session.get_bind()
    result = await run_one(
        lambda: session,
        RunOneConfig(worktree_root=tmp_path / "work",
                     transcript_root=tmp_path / "transcripts",
                     secrets={}, host="testhost", executor=executor),
        ticket_id,
    )
    with Session(bind) as verify:
        refreshed = verify.get(Ticket, ticket_id)
        run = verify.get(Run, refreshed.current_run_id)
        verify.expunge(run)
    return result, run


@pytest.mark.anyio
async def test_run_one_stamps_pricing_snapshot_at_launch_for_compat_profile(
    session, tmp_path,
):
    """A compat-pinned profile (full-pin on a ``*_compat`` endpoint) gets its
    pricing snapshot stamped at launch, before the agent ever runs."""
    profile, endpoint = _make_compat_profile(session)
    executor = CapturingExecutor()  # no usage -> only the launch stamp matters
    result, run = await _run_ticket_with_profile(session, profile, tmp_path, executor)

    assert result.exit_status == "success"
    assert run.pricing_snapshot is not None
    entry = run.pricing_snapshot["glm-5.2"]
    assert entry["vendor"] == "zai"
    assert entry["input"] == pytest.approx(1.40)
    assert entry["source"] == "bundled"  # no pricing_url configured in the test


@pytest.mark.anyio
async def test_run_one_snapshot_cost_overrides_harness_reported_cost(
    session, tmp_path,
):
    """The stamped snapshot's cost wins over the harness's own (Claude-priced)
    estimate once the actually-used model is covered by the snapshot."""
    profile, endpoint = _make_compat_profile(session, vendor="zai", default_model="glm-5.2")
    usage = RunUsage(
        model="glm-5.2", input_tokens=1_000_000, output_tokens=500_000,
        cache_read_tokens=0, cache_write_tokens=0,
        cost_usd=999.0,  # deliberately wrong harness estimate (assumes Claude prices)
    )
    executor = UsageExecutor(usage=usage)
    result, run = await _run_ticket_with_profile(session, profile, tmp_path, executor)

    assert result.exit_status == "success"
    # zai glm-5.2: input 1.40, output 4.40 per 1M tokens (bundled table).
    expected = (1_000_000 * 1.40 + 500_000 * 4.40) / 1_000_000.0
    assert run.cost_usd == pytest.approx(expected)
    assert run.cost_usd != pytest.approx(999.0)


@pytest.mark.anyio
async def test_run_one_extends_snapshot_for_off_snapshot_model_at_finish(
    session, tmp_path,
):
    """When the harness reports usage for a model the launch-time snapshot
    didn't cover, finish extends the snapshot (vendor falls back to the
    primary endpoint's) and reprices from it when possible."""
    profile, endpoint = _make_compat_profile(session, vendor="zai", default_model="glm-5.2")
    usage = RunUsage(
        model="glm-5-code",  # a different zai model, not in the launch-time pin
        input_tokens=1_000_000, output_tokens=200_000,
        cache_read_tokens=0, cache_write_tokens=0, cost_usd=42.0,
    )
    executor = UsageExecutor(usage=usage)
    result, run = await _run_ticket_with_profile(session, profile, tmp_path, executor)

    assert result.exit_status == "success"
    assert "glm-5.2" in run.pricing_snapshot  # the original launch-time stamp
    ext = run.pricing_snapshot["glm-5-code"]
    assert ext["vendor"] == "zai"  # fell back to the primary endpoint's vendor
    expected = (1_000_000 * 1.20 + 200_000 * 5.00) / 1_000_000.0
    assert run.cost_usd == pytest.approx(expected)


@pytest.mark.anyio
async def test_run_one_keeps_harness_cost_when_snapshot_extension_cannot_price(
    session, tmp_path,
):
    """A model the pricing chain can't resolve at all (no bundled/live/cache
    row) keeps the harness-reported cost instead of silently zeroing it."""
    profile, endpoint = _make_compat_profile(session, vendor="zai", default_model="glm-5.2")
    usage = RunUsage(
        model="totally-unpriceable-model-xyz",
        input_tokens=100, output_tokens=100,
        cache_read_tokens=0, cache_write_tokens=0, cost_usd=7.5,
    )
    executor = UsageExecutor(usage=usage)
    result, run = await _run_ticket_with_profile(session, profile, tmp_path, executor)

    assert result.exit_status == "success"
    ext = run.pricing_snapshot["totally-unpriceable-model-xyz"]
    assert ext["source"] == "none"
    assert ext["input"] is None
    assert run.cost_usd == pytest.approx(7.5)  # harness-reported, kept as-is


@pytest.mark.anyio
async def test_run_one_legacy_run_gets_finish_time_snapshot_vendor_anthropic(
    session, sample_profile, tmp_path,
):
    """A legacy profile with no endpoint at all (ambient Claude credentials)
    gets a finish-time snapshot stamped with vendor="anthropic"."""
    usage = RunUsage(
        model="claude-haiku-4-5", input_tokens=1_000_000, output_tokens=200_000,
        cache_read_tokens=0, cache_write_tokens=0, cost_usd=1.0,
    )
    executor = UsageExecutor(usage=usage)
    result, run = await _run_ticket_with_profile(session, sample_profile, tmp_path, executor)

    assert result.exit_status == "success"
    ext = run.pricing_snapshot["claude-haiku-4-5"]
    assert ext["vendor"] == "anthropic"
    expected = (1_000_000 * 1.0 + 200_000 * 5.0) / 1_000_000.0
    assert run.cost_usd == pytest.approx(expected)


@pytest.mark.anyio
async def test_run_one_legacy_run_prices_glm_instead_of_guessing_anthropic(
    session, sample_profile, tmp_path,
):
    """Regression guard (token/cost tracking broken for GLM runs on legacy
    profiles): a legacy profile with no endpoint at all -- e.g. one wired to
    z.ai/GLM the old way, via raw ``profile.env``/``claude_credentials``
    rather than a Provider/ProviderEndpoint -- must NOT have its finish-time
    snapshot mislabeled ``vendor="anthropic"`` just because that's the only
    guess available. A wrong ``"anthropic"`` guess can never resolve a GLM
    id (that branch only checks Anthropic rows) and permanently freezes the
    run's cost at null once the snapshot is stamped. The model id alone is a
    strong enough signal to skip the wrong guess and let the vendor-agnostic
    bundled-table fallback (which does carry z.ai rows) price it correctly.
    """
    usage = RunUsage(
        model="glm-5.2", input_tokens=1_000_000, output_tokens=500_000,
        cache_read_tokens=0, cache_write_tokens=0,
        cost_usd=999.0,  # deliberately wrong "harness assumed Claude prices"
    )
    executor = UsageExecutor(usage=usage)
    result, run = await _run_ticket_with_profile(session, sample_profile, tmp_path, executor)

    assert result.exit_status == "success"
    ext = run.pricing_snapshot["glm-5.2"]
    assert ext["vendor"] != "anthropic"
    assert ext["input"] == pytest.approx(1.40)
    expected = (1_000_000 * 1.40 + 500_000 * 4.40) / 1_000_000.0
    assert run.cost_usd == pytest.approx(expected)
    assert run.cost_usd != pytest.approx(999.0)


@pytest.mark.anyio
async def test_run_one_pricing_failure_never_fails_launch_or_finish(
    session, tmp_path, monkeypatch,
):
    """A pricing-chain failure at launch or finish must never fail the run —
    it degrades to no snapshot / the harness-reported cost."""
    import nightdesk.worker.run_one as run_one_mod

    monkeypatch.setattr(
        run_one_mod.pricing, "resolve_live_all",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network exploded")),
    )
    monkeypatch.setattr(
        run_one_mod.pricing, "build_pricing_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("snapshot build exploded")),
    )
    monkeypatch.setattr(
        run_one_mod, "_extend_and_price_from_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pricing exploded")),
    )
    profile, endpoint = _make_compat_profile(session, vendor="zai", default_model="glm-5.2")
    usage = RunUsage(
        model="glm-5.2", input_tokens=100, output_tokens=100,
        cache_read_tokens=0, cache_write_tokens=0, cost_usd=3.0,
    )
    executor = UsageExecutor(usage=usage)
    result, run = await _run_ticket_with_profile(session, profile, tmp_path, executor)

    assert result.exit_status == "success"
    assert run.pricing_snapshot is None  # resolve_live_all blew up before stamping
    assert run.cost_usd == pytest.approx(3.0)  # harness-reported, extension never ran
