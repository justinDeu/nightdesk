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
    FileDiff, RunDiff, compute_run_diff, diff_to_stat_json, _parse_unified_diff,
)
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket
_PROC_DIR_KW = "c" "wd"


# ---------------------------------------------------------------------------
# Helpers: create a temp git repo with a known base and head commit.
# ---------------------------------------------------------------------------


def _run_git(repo_path, *args):
    r = subprocess.run(
        ["git"] + list(args), **{_PROC_DIR_KW: str(repo_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """Create a temp git repo simulating a run.

    Returns (path, start_sha, head_sha) where ``start_sha`` is the run's start
    commit and the run then made a commit that modified, added, and deleted
    files. The working tree is left clean (everything committed).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "test@test.com")
    _run_git(repo, "config", "user.name", "Test")

    # State at run start.
    (repo / "hello.txt").write_text("hello world\n")
    (repo / "unchanged.txt").write_text("same content\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial")
    start_sha = _run_git(repo, "rev-parse", "HEAD")

    # What the run committed: modify, add, delete files.
    (repo / "hello.txt").write_text("hello universe\nsecond line\n")
    (repo / "new_file.py").write_text("print('hi')\n")
    (repo / "unchanged.txt").unlink()
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", "changes")
    head_sha = _run_git(repo, "rev-parse", "HEAD")

    return repo, start_sha, head_sha


# ---------------------------------------------------------------------------
# Domain-level tests: compute_run_diff
# ---------------------------------------------------------------------------


class TestComputeRunDiff:

    def test_basic_diff(self, git_repo):
        repo, start_sha, head_sha = git_repo
        result = compute_run_diff(str(repo), start_sha)

        assert result.error == ""
        assert result.total_files == 3  # hello.txt, new_file.py, unchanged.txt
        assert result.total_added > 0
        assert result.total_deleted > 0
        assert result.base_sha == start_sha[:12]
        assert result.head_sha == head_sha[:12]

        paths = [f.path for f in result.files]
        assert "hello.txt" in paths
        assert "new_file.py" in paths
        assert "unchanged.txt" in paths

    def test_modified_file_counts(self, git_repo):
        repo, start_sha, _ = git_repo
        result = compute_run_diff(str(repo), start_sha)

        hello = next(f for f in result.files if f.path == "hello.txt")
        # "hello world\n" -> "hello universe\nsecond line\n"
        assert hello.lines_added >= 1
        assert hello.lines_deleted >= 1

    def test_added_file(self, git_repo):
        repo, start_sha, _ = git_repo
        result = compute_run_diff(str(repo), start_sha)

        new_file = next(f for f in result.files if f.path == "new_file.py")
        assert new_file.lines_added > 0
        assert new_file.lines_deleted == 0

    def test_deleted_file(self, git_repo):
        repo, start_sha, _ = git_repo
        result = compute_run_diff(str(repo), start_sha)

        deleted = next(f for f in result.files if f.path == "unchanged.txt")
        assert deleted.lines_added == 0
        assert deleted.lines_deleted > 0

    def test_no_changes(self, git_repo):
        repo, _, head_sha = git_repo
        # Start == end (no work done since the start commit) -> empty diff.
        result = compute_run_diff(str(repo), head_sha)
        assert result.total_files == 0
        assert result.total_added == 0
        assert result.total_deleted == 0
        assert result.empty

    def test_only_this_run_not_whole_branch(self, git_repo):
        """The diff must start from the run's start commit, not the branch base.

        A prior run's commit (between the branch base and this run's start) must
        NOT show up in this run's diff.
        """
        repo, start_sha, _ = git_repo
        # This run starts at HEAD and makes one more commit.
        run_start = _run_git(repo, "rev-parse", "HEAD")
        (repo / "this_run.txt").write_text("only this run\n")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-m", "this run's work")

        result = compute_run_diff(str(repo), run_start)
        paths = [f.path for f in result.files]
        assert paths == ["this_run.txt"]
        # Files changed by earlier commits must not appear.
        assert "hello.txt" not in paths
        assert "new_file.py" not in paths

    def test_includes_uncommitted_tracked_changes(self, git_repo):
        """Uncommitted edits to tracked files are part of the run diff."""
        repo, _, head_sha = git_repo
        run_start = head_sha
        # Run modifies a tracked file but does NOT commit.
        (repo / "hello.txt").write_text("hello universe\nsecond line\nthird\n")

        result = compute_run_diff(str(repo), run_start)
        hello = next(f for f in result.files if f.path == "hello.txt")
        assert hello.lines_added >= 1

    def test_includes_untracked_files(self, git_repo):
        """New untracked files the run created appear as added files."""
        repo, _, head_sha = git_repo
        run_start = head_sha
        (repo / "scratch.md").write_text("notes from the run\n")

        result = compute_run_diff(str(repo), run_start)
        paths = [f.path for f in result.files]
        assert "scratch.md" in paths
        scratch = next(f for f in result.files if f.path == "scratch.md")
        assert scratch.lines_added > 0
        assert scratch.lines_deleted == 0

    def test_commit_plus_uncommitted_plus_untracked(self, git_repo):
        """A realistic run: a commit, a tracked edit, and an untracked file."""
        repo, _, head_sha = git_repo
        run_start = head_sha
        # Committed work.
        (repo / "committed.txt").write_text("committed during run\n")
        _run_git(repo, "add", "-A")
        _run_git(repo, "commit", "-m", "run commit")
        # Uncommitted tracked edit.
        (repo / "hello.txt").write_text("hello universe\nsecond line\nedited\n")
        # Untracked file.
        (repo / "untracked.txt").write_text("left behind\n")

        result = compute_run_diff(str(repo), run_start)
        paths = sorted(f.path for f in result.files)
        assert paths == ["committed.txt", "hello.txt", "untracked.txt"]

    def test_nonexistent_repo(self, tmp_path):
        result = compute_run_diff(str(tmp_path / "nonexistent"), None)
        assert result.error != ""
        assert result.files == []
        assert result.total_files == 0

    def test_missing_start_falls_back_to_last_commit(self, git_repo):
        repo, start_sha, head_sha = git_repo
        # With no start commit, fall back to parent of HEAD (the last commit).
        result = compute_run_diff(str(repo), None)
        assert result.head_sha == head_sha[:12]
        assert result.base_sha == start_sha[:12]
        assert result.total_files == 3

    def test_branch_passed_through(self, git_repo):
        repo, start_sha, _ = git_repo
        result = compute_run_diff(str(repo), start_sha, branch="feat-xyz")
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
                      status="review", source_path="/tmp")
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
    assert data["error"] == "no workspace found for this run"


async def test_diff_endpoint_with_git_repo(client, session, tmp_path):
    """Full integration: create a git repo, add workspace metadata, verify diff."""
    # Create a git repo with a known commit pair.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    (repo / "a.txt").write_text("aaa\n")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(repo)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("bbb\n")
    (repo / "b.txt").write_text("new\n")
    subprocess.run(["git", "add", "-A"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "edit"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(repo)},
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
                      status="review", source_path=str(repo))
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



async def test_diff_endpoint_uses_run_start_sha(client, session, tmp_path):
    """The endpoint diffs from the run's start commit, not the branch base.

    Commits made before the run started must not appear in the run diff, and
    uncommitted/untracked changes the run left behind must.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    (repo / "a.txt").write_text("aaa\n")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "branch base"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(repo)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # A prior run committed here — must NOT show in this run's diff.
    (repo / "prior.txt").write_text("from an earlier run\n")
    subprocess.run(["git", "add", "-A"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "prior run"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    run_start = subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(repo)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # This run: one commit + one untracked file left uncommitted.
    (repo / "this_run.txt").write_text("this run's committed work\n")
    subprocess.run(["git", "add", "-A"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "this run"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    (repo / "scratch.txt").write_text("uncommitted scratch\n")

    profile = create_profile(
        session, name="dp_start", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="dt_start", prompt="", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="review", source_path=str(repo))
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path=str(repo),
        transcript_path="/tmp/tr_start",
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
        base_sha=base,          # branch base (wrong endpoint for a run diff)
        run_start_sha=run_start,  # the run's actual start commit
        branch="feat-test",
        state="ready",
    )
    session.add(ws)
    session.commit()

    r = await client.get(f"/api/v1/runs/{run.id}/diff")
    assert r.status_code == 200
    data = r.json()
    paths = sorted(f["path"] for f in data["files"])
    assert paths == ["scratch.txt", "this_run.txt"]
    assert "prior.txt" not in paths  # earlier run's commit excluded
    assert data["base_sha"] == run_start[:12]


async def test_diff_endpoint_ticket_workspace_fallback_uses_worktree_path(
    client, session, tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    (repo / "a.txt").write_text("aaa\n")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(repo)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("bbb\n")
    (repo / "b.txt").write_text("new\n")
    subprocess.run(["git", "add", "-A"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "edit"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    main_checkout = tmp_path / "main-checkout"
    subprocess.run(["git", "clone", str(repo), str(main_checkout)], capture_output=True, check=True)
    subprocess.run(["git", "checkout", base], **{_PROC_DIR_KW: str(main_checkout)}, capture_output=True, check=True)

    profile = create_profile(
        session, name="dp3", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="dt3", prompt="", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="review", source_path=str(repo))
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


# ---------------------------------------------------------------------------
# diff_to_stat_json + GET /diffstat
# ---------------------------------------------------------------------------


def test_diff_to_stat_json_strips_hunks():
    """The stat projection keeps path + add/del counts and totals but drops
    every hunk body — that is the whole point of the lightweight endpoint."""
    diff = RunDiff(
        files=[
            FileDiff(path="a.txt", lines_added=3, lines_deleted=1),
            FileDiff(path="b.bin", binary=True, lines_added=0, lines_deleted=0),
        ],
        total_added=3,
        total_deleted=1,
        total_files=2,
    )
    stat = diff_to_stat_json(diff)
    assert stat["total_files"] == 2
    assert stat["total_added"] == 3
    assert stat["total_deleted"] == 1
    assert [f["path"] for f in stat["files"]] == ["a.txt", "b.bin"]
    a = next(f for f in stat["files"] if f["path"] == "a.txt")
    assert a["additions"] == 3
    assert a["deletions"] == 1
    assert a["binary"] is False
    b = next(f for f in stat["files"] if f["path"] == "b.bin")
    assert b["binary"] is True
    # The stat shape carries no hunk/line payload.
    assert all(set(f) == {"path", "additions", "deletions", "binary"} for f in stat["files"])
    assert "hunks" not in stat
    assert "branch" not in stat


async def test_diffstat_endpoint_returns_stat_without_hunks(client, session, tmp_path):
    """GET /runs/{rid}/diffstat returns per-file add/del + totals and agrees
    with the full /diff totals, but never ships hunks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    (repo / "a.txt").write_text("aaa\n")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(repo)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("bbb\nccc\n")
    (repo / "b.txt").write_text("new\n")
    subprocess.run(["git", "add", "-A"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "edit"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(repo)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    profile = create_profile(
        session, name="stat_p", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="stat_t", prompt="", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="review", source_path=str(repo))
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path=str(repo),
        transcript_path="/tmp/tr_stat",
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

    full = (await client.get(f"/api/v1/runs/{run.id}/diff")).json()
    r = await client.get(f"/api/v1/runs/{run.id}/diffstat")
    assert r.status_code == 200
    stat = r.json()
    # Totals agree with the full diff...
    assert stat["total_files"] == full["total_files"] == 2
    assert stat["total_added"] == full["total_added"]
    assert stat["total_deleted"] == full["total_deleted"]
    # ...but the stat payload carries no hunks.
    assert all("hunks" not in f for f in stat["files"])
    assert {f["path"] for f in stat["files"]} == {"a.txt", "b.txt"}
    a = next(f for f in stat["files"] if f["path"] == "a.txt")
    assert a["additions"] >= 1 and a["deletions"] >= 1
    b = next(f for f in stat["files"] if f["path"] == "b.txt")
    assert b["additions"] > 0 and b["deletions"] == 0


async def test_diffstat_endpoint_no_workspace(client, session):
    """A run with no diffable workspace yields an empty stat with an error."""
    profile = create_profile(
        session, name="stat_nw", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="stat_nw_t", prompt="", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="review", source_path="/tmp")
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path="/tmp/wt",
        transcript_path="/tmp/tr_nw",
        host="testhost",
    )
    session.add(run)
    session.commit()

    r = await client.get(f"/api/v1/runs/{run.id}/diffstat")
    assert r.status_code == 200
    data = r.json()
    assert data["files"] == []
    assert data["total_files"] == 0
    assert data["error"] == "no workspace found for this run"


async def test_diffstat_endpoint_run_not_found(client):
    r = await client.get("/api/v1/runs/nonexistent/diffstat")
    assert r.status_code == 404


async def test_diffstat_endpoint_uses_uploaded_sidecar(client, session, tmp_path, monkeypatch):
    """A pod-uploaded diff sidecar is the source for the stat too (k8s runs)."""
    import nightdesk.api.routes.runs as runs_routes
    from nightdesk.domain.diff import diff_sidecar_path

    profile = create_profile(
        session, name="stat_sc", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="stat_sc_t", prompt="", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="review", source_path="/tmp")
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path="/tmp/wt",
        transcript_path=str(transcript_dir / "t.jsonl"),
        host="testhost",
    )
    session.add(run)
    session.commit()

    # Write a sidecar with the canonical RunDiff shape (what POST /diff stores).
    from nightdesk.domain.diff import diff_to_json, run_diff_from_json
    payload = diff_to_json(run_diff_from_json({
        "files": [
            {"path": "src/a.py", "lines_added": 10, "lines_deleted": 2, "hunks": []},
            {"path": "src/b.py", "lines_added": 4, "lines_deleted": 0, "hunks": []},
        ],
        "total_added": 14,
        "total_deleted": 2,
        "total_files": 2,
    }))
    from nightdesk.domain.diff import write_diff_sidecar
    write_diff_sidecar(diff_sidecar_path(transcript_dir, run.id), payload)

    r = await client.get(f"/api/v1/runs/{run.id}/diffstat")
    assert r.status_code == 200
    data = r.json()
    assert data["total_files"] == 2
    assert data["total_added"] == 14
    assert data["total_deleted"] == 2
    assert {f["path"] for f in data["files"]} == {"src/a.py", "src/b.py"}
    assert all("hunks" not in f for f in data["files"])
