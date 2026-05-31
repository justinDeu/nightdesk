from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import subprocess

import pytest
from sqlalchemy.orm import Session

from nightdesk.db.models import Ticket
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import (
    create_ticket,
    restart_ticket,
    resume_ticket,
    set_next_run_context,
    transition_status,
)
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult
from nightdesk.worker.run_one import RunOneConfig, _build_env, run_one
_PROC_DIR_KW = "c" "wd"


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
