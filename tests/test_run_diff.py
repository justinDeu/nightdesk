"""Unit tests for the per-run diff endpoint and domain logic.

Tests the diff computation against a temp git repo with known commits,
and verifies the API endpoint returns the expected JSON structure.
"""
import subprocess
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.db.models import Run, TicketWorkspace
from nightdesk.domain.diff import (
    FileDiff, RunDiff, compute_run_diff, _parse_unified_diff,
)
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket


# ---------------------------------------------------------------------------
# Helpers: create a temp git repo with a known base and head commit.
# ---------------------------------------------------------------------------


def _run_git(cwd, *args):
    r = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """Create a temp git repo with two commits and return (path, base_sha, head_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "test@test.com")
    _run_git(repo, "config", "user.name", "Test")

    # Initial commit (base).
    (repo / "hello.txt").write_text("hello world\n")
    (repo / "unchanged.txt").write_text("same content\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial")
    base_sha = _run_git(repo, "rev-parse", "HEAD")

    # Second commit (head): modify, add, delete files.
    (repo / "hello.txt").write_text("hello universe\nsecond line\n")
    (repo / "new_file.py").write_text("print('hi')\n")
    (repo / "unchanged.txt").unlink()
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", "changes")
    head_sha = _run_git(repo, "rev-parse", "HEAD")

    return repo, base_sha, head_sha


# ---------------------------------------------------------------------------
# Domain-level tests: compute_run_diff
# ---------------------------------------------------------------------------


class TestComputeRunDiff:

    def test_basic_diff(self, git_repo):
        repo, base_sha, head_sha = git_repo
        result = compute_run_diff(str(repo), base_sha, head_sha)

        assert result.error == ""
        assert result.total_files == 3  # hello.txt, new_file.py, unchanged.txt
        assert result.total_added > 0
        assert result.total_deleted > 0
        assert result.base_sha == base_sha[:12]
        assert result.head_sha == head_sha[:12]

        paths = [f.path for f in result.files]
        assert "hello.txt" in paths
        assert "new_file.py" in paths
        assert "unchanged.txt" in paths

    def test_modified_file_counts(self, git_repo):
        repo, base_sha, head_sha = git_repo
        result = compute_run_diff(str(repo), base_sha, head_sha)

        hello = next(f for f in result.files if f.path == "hello.txt")
        # "hello world\n" -> "hello universe\nsecond line\n"
        assert hello.lines_added >= 1
        assert hello.lines_deleted >= 1

    def test_added_file(self, git_repo):
        repo, base_sha, head_sha = git_repo
        result = compute_run_diff(str(repo), base_sha, head_sha)

        new_file = next(f for f in result.files if f.path == "new_file.py")
        assert new_file.lines_added > 0
        assert new_file.lines_deleted == 0

    def test_deleted_file(self, git_repo):
        repo, base_sha, head_sha = git_repo
        result = compute_run_diff(str(repo), base_sha, head_sha)

        deleted = next(f for f in result.files if f.path == "unchanged.txt")
        assert deleted.lines_added == 0
        assert deleted.lines_deleted > 0

    def test_no_changes(self, git_repo):
        repo, base_sha, _ = git_repo
        result = compute_run_diff(str(repo), base_sha, base_sha)
        assert result.total_files == 0
        assert result.total_added == 0
        assert result.total_deleted == 0
        assert result.empty

    def test_nonexistent_repo(self, tmp_path):
        result = compute_run_diff(str(tmp_path / "nonexistent"), None, None)
        assert result.error != ""
        assert result.files == []
        assert result.total_files == 0

    def test_missing_head_uses_current(self, git_repo):
        repo, base_sha, head_sha = git_repo
        # When head_sha is None, should resolve to HEAD.
        result = compute_run_diff(str(repo), base_sha, None)
        assert result.head_sha == head_sha[:12]

    def test_branch_passed_through(self, git_repo):
        repo, base_sha, head_sha = git_repo
        result = compute_run_diff(str(repo), base_sha, head_sha, branch="feat-xyz")
        assert result.branch == "feat-xyz"


class TestParseUnifiedDiff:

    def test_empty_diff(self):
        result = _parse_unified_diff("")
        assert result.files == []
        assert result.total_added == 0

    def test_single_file(self):
        diff = (
            "diff --git a/hello.txt b/hello.txt\n"
            "--- a/hello.txt\n"
            "+++ b/hello.txt\n"
            "@@ -1 +1 @@\n"
            "-hello world\n"
            "+hello universe\n"
        )
        result = _parse_unified_diff(diff)
        assert len(result.files) == 1
        assert result.files[0].path == "hello.txt"
        assert result.files[0].lines_added == 1
        assert result.files[0].lines_deleted == 1
        assert result.total_added == 1
        assert result.total_deleted == 1

    def test_binary_file(self):
        diff = (
            "diff --git a/image.png b/image.png\n"
            "Binary files /dev/null and b/image.png differ\n"
        )
        result = _parse_unified_diff(diff)
        assert len(result.files) == 1
        assert result.files[0].binary is True

    def test_new_file(self):
        diff = (
            "diff --git a/new.py b/new.py\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+print('hello')\n"
            "+print('world')\n"
        )
        result = _parse_unified_diff(diff)
        assert len(result.files) == 1
        assert result.files[0].path == "new.py"
        assert result.files[0].lines_added == 2
        assert result.files[0].lines_deleted == 0

    def test_context_lines(self):
        diff = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " line1\n"
            "-line2\n"
            "+LINE2\n"
            " line3\n"
        )
        result = _parse_unified_diff(diff)
        f = result.files[0]
        assert len(f.hunks) == 5  # hunk header + ctx + del + ins + ctx
        assert f.hunks[0].kind == "hunk"
        assert f.hunks[1].kind == "ctx"
        assert f.hunks[2].kind == "del"
        assert f.hunks[3].kind == "ins"
        assert f.hunks[4].kind == "ctx"

    def test_no_newline_marker_ignored(self):
        diff = (
            "diff --git a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "\\ No newline at end of file\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        result = _parse_unified_diff(diff)
        # "\\ No newline" lines should not create hunks.
        non_hunk = [h for h in result.files[0].hunks if h.kind != "hunk"]
        assert len(non_hunk) == 2  # just -old and +new


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


async def test_diff_endpoint_no_workspace(client, session):
    """Run with no workspace returns an empty diff with an error message."""
    profile = create_profile(
        session, name="dp", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="dt", prompt="", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="review", cwd="/tmp")
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path="/tmp/wt",
        transcript_path="/tmp/tr",
        host="testhost",
    )
    session.add(run)
    session.commit()

    r = await client.get(f"/api/v1/runs/{run.id}/diff")
    assert r.status_code == 200
    data = r.json()
    assert data["files"] == []
    assert data["error"] == "no git workspace found for this run"


async def test_diff_endpoint_with_git_repo(client, session, tmp_path):
    """Full integration: create a git repo, add workspace metadata, verify diff."""
    # Create a git repo with a known commit pair.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True, check=True)
    (repo / "a.txt").write_text("aaa\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("bbb\n")
    (repo / "b.txt").write_text("new\n")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "edit"], cwd=str(repo), capture_output=True, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Create ticket + run + workspace.
    profile = create_profile(
        session, name="dp2", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="dt2", prompt="", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="review", cwd=str(repo))
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path=str(repo),
        transcript_path="/tmp/tr2",
        host="testhost",
    )
    session.add(run)
    session.flush()
    ws = TicketWorkspace(
        ticket_id=t.id,
        run_id=run.id,
        role="primary",
        kind="git_worktree",
        repo_root=str(repo),
        base_sha=base,
        head_sha=head,
        branch="feat-test",
        state="ready",
    )
    session.add(ws)
    session.commit()

    r = await client.get(f"/api/v1/runs/{run.id}/diff")
    assert r.status_code == 200
    data = r.json()
    assert data["total_files"] == 2
    assert data["total_added"] > 0
    assert data["branch"] == "feat-test"
    paths = [f["path"] for f in data["files"]]
    assert "a.txt" in paths
    assert "b.txt" in paths



async def test_diff_endpoint_ticket_workspace_fallback_uses_worktree_path(
    client, session, tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), capture_output=True, check=True)
    (repo / "a.txt").write_text("aaa\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("bbb\n")
    (repo / "b.txt").write_text("new\n")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "edit"], cwd=str(repo), capture_output=True, check=True)

    main_checkout = tmp_path / "main-checkout"
    subprocess.run(["git", "clone", str(repo), str(main_checkout)], capture_output=True, check=True)
    subprocess.run(["git", "checkout", base], cwd=str(main_checkout), capture_output=True, check=True)

    profile = create_profile(
        session, name="dp3", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="dt3", prompt="", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="review", cwd=str(repo))
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path=str(repo),
        transcript_path="/tmp/tr3",
        host="testhost",
    )
    session.add(run)
    session.flush()
    ws = TicketWorkspace(
        ticket_id=t.id,
        run_id=None,
        role="primary",
        kind="git_worktree",
        source_path=str(main_checkout),
        resolved_path=str(repo),
        repo_root=str(main_checkout),
        worktree_path=str(repo),
        base_sha=base,
        branch="feat-test",
        state="ready",
    )
    session.add(ws)
    session.commit()

    r = await client.get(f"/api/v1/runs/{run.id}/diff")
    assert r.status_code == 200
    data = r.json()
    assert data["total_files"] == 2
    assert data["head_sha"] != data["base_sha"]

async def test_diff_endpoint_run_not_found(client):
    r = await client.get("/api/v1/runs/nonexistent/diff")
    assert r.status_code == 404
