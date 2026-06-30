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
    seeded = list((tmp_path / "nightdesk-cc-sessions" / new_run_id).rglob(
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
async def test_run_one_continue_falls_back_when_parent_has_no_session_id(
    session, sample_profile, tmp_path,
):
    """A parent that crashed before capturing a session id has nothing to
    resume; continue falls back to fresh-context resume."""
    primary = tmp_path / "primary"
    primary.mkdir()
    ticket = create_ticket(
        session, title="cont", prompt="Fix it", status="queued",
        priority=0, profile_id=sample_profile.id, source_path=str(primary),
    )
    # No session_id recorded on the parent (e.g. crashed before init).
    _stage_parent_run(session, ticket, tmp_path, session_id=None)
    continue_ticket(session, ticket.id, next_run_context="keep going")
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
