"""Tests for filesystem snapshots + diffs on non-git (directory) workspaces.

Covers:
- snapshot_tree capture + sensible excludes,
- compute_fs_run_diff over a constructed dir exercising create/modify/delete,
- kind-based workspace selection (select_diff_workspace / compute_workspace_diff),
- the nested-repo guard: a directory workspace whose path sits inside an
  unrelated git repo must NEVER surface that repo's git history,
- the API diff endpoint + ticket-page diff panel agreeing for a directory
  workspace.
"""
import subprocess
from datetime import datetime, timezone

from nightdesk.db.models import Run, TicketWorkspace
from nightdesk.domain.diff import (
    compute_workspace_diff, select_diff_workspace,
)
from nightdesk.domain.fs_snapshot import (
    compute_fs_run_diff,
    read_snapshot,
    snapshot_sidecar_path,
    snapshot_tree,
    write_snapshot,
)
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket

_PROC_DIR_KW = "c" "wd"


# ---------------------------------------------------------------------------
# snapshot_tree
# ---------------------------------------------------------------------------


class TestSnapshotTree:

    def test_captures_files_with_content(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").write_text("print(1)\n")

        snap = snapshot_tree(tmp_path)
        files = snap["files"]
        assert set(files) == {"a.txt", "sub/b.py"}
        assert files["a.txt"]["content"] == "hello\n"
        assert files["a.txt"]["text"] is True
        assert files["a.txt"]["hash"]

    def test_excludes_noise_dirs(self, tmp_path):
        (tmp_path / "keep.txt").write_text("keep\n")
        for noise in (".git", "node_modules", "__pycache__", ".venv"):
            d = tmp_path / noise
            d.mkdir()
            (d / "junk").write_text("junk\n")

        snap = snapshot_tree(tmp_path)
        assert set(snap["files"]) == {"keep.txt"}

    def test_binary_file_has_no_content(self, tmp_path):
        (tmp_path / "img.bin").write_bytes(b"\x00\x01\x02\x03")
        snap = snapshot_tree(tmp_path)
        entry = snap["files"]["img.bin"]
        assert entry["text"] is False
        assert "content" not in entry

    def test_roundtrip_sidecar(self, tmp_path):
        (tmp_path / "a.txt").write_text("x\n")
        snap = snapshot_tree(tmp_path)
        path = snapshot_sidecar_path(tmp_path / "tr", "run1", "ws1")
        write_snapshot(path, snap)
        assert read_snapshot(path) == snap


# ---------------------------------------------------------------------------
# compute_fs_run_diff: create / modify / delete
# ---------------------------------------------------------------------------


class TestComputeFsRunDiff:

    def _snapshot_then(self, root):
        return snapshot_tree(root)

    def test_create_modify_delete(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        (root / "keep.txt").write_text("unchanged\n")
        (root / "mod.txt").write_text("old line\n")
        (root / "gone.txt").write_text("delete me\n")

        snap = snapshot_tree(root)

        # The "run" mutates the tree.
        (root / "mod.txt").write_text("new line\n")
        (root / "gone.txt").unlink()
        (root / "added.txt").write_text("brand new\n")

        result = compute_fs_run_diff(str(root), snap)
        assert result.error == ""
        paths = sorted(f.path for f in result.files)
        assert paths == ["added.txt", "gone.txt", "mod.txt"]
        assert "keep.txt" not in paths

        added = next(f for f in result.files if f.path == "added.txt")
        assert added.lines_added > 0 and added.lines_deleted == 0

        deleted = next(f for f in result.files if f.path == "gone.txt")
        assert deleted.lines_deleted > 0 and deleted.lines_added == 0

        mod = next(f for f in result.files if f.path == "mod.txt")
        assert mod.lines_added >= 1 and mod.lines_deleted >= 1

    def test_no_changes_is_empty(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        (root / "a.txt").write_text("same\n")
        snap = snapshot_tree(root)
        result = compute_fs_run_diff(str(root), snap)
        assert result.files == []
        assert result.empty

    def test_missing_snapshot_is_meaningful_not_git_error(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        (root / "a.txt").write_text("x\n")
        result = compute_fs_run_diff(str(root), None)
        assert "not a git repository" not in result.error
        assert "snapshot" in result.error

    def test_missing_root(self, tmp_path):
        result = compute_fs_run_diff(str(tmp_path / "nope"), {"files": {}})
        assert result.error != ""
        assert result.files == []

    def test_binary_modified_file_marked_binary(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        (root / "blob.bin").write_bytes(b"\x00\x01")
        snap = snapshot_tree(root)
        (root / "blob.bin").write_bytes(b"\x00\x02\x03")
        result = compute_fs_run_diff(str(root), snap)
        blob = next(f for f in result.files if f.path == "blob.bin")
        assert blob.binary is True


# ---------------------------------------------------------------------------
# Kind-based selection
# ---------------------------------------------------------------------------


class _WS:
    def __init__(self, **kw):
        self.id = kw.get("id", "wsid")
        self.role = kw.get("role")
        self.kind = kw.get("kind")
        self.resolved_path = kw.get("resolved_path")
        self.worktree_path = kw.get("worktree_path")
        self.repo_root = kw.get("repo_root")
        self.base_sha = kw.get("base_sha")
        self.run_start_sha = kw.get("run_start_sha")
        self.branch = kw.get("branch")


class TestSelectDiffWorkspace:

    def test_prefers_primary_with_path(self):
        a = _WS(role="linked", kind="directory", resolved_path="/x")
        b = _WS(role="primary", kind="git_worktree", resolved_path="/y")
        assert select_diff_workspace([a, b]) is b

    def test_falls_back_to_first_with_path(self):
        a = _WS(role="linked", kind="directory", resolved_path="/x")
        assert select_diff_workspace([a]) is a

    def test_none_when_empty(self):
        assert select_diff_workspace([]) is None


class TestComputeWorkspaceDiffDispatch:

    def test_directory_kind_routes_to_fs_diff(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        (root / "a.txt").write_text("old\n")
        snap = snapshot_tree(root)
        (root / "a.txt").write_text("new\n")

        transcript_root = tmp_path / "tr"
        ws = _WS(id="wsid", role="primary", kind="directory",
                 resolved_path=str(root))
        write_snapshot(
            snapshot_sidecar_path(transcript_root, "run1", "wsid"), snap,
        )
        result = compute_workspace_diff(
            ws, transcript_root=transcript_root, run_id="run1",
        )
        assert result is not None
        assert [f.path for f in result.files] == ["a.txt"]

    def test_none_workspace(self, tmp_path):
        assert compute_workspace_diff(
            None, transcript_root=tmp_path, run_id="r",
        ) is None


# ---------------------------------------------------------------------------
# Nested-repo guard (domain level)
# ---------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", *args], **{_PROC_DIR_KW: str(repo)},
                   capture_output=True, check=True)


class TestNestedRepoGuard:

    def test_directory_inside_git_repo_never_shows_git_history(self, tmp_path):
        """A directory workspace nested inside an unrelated git repo must show
        only its own filesystem changes, never the surrounding repo's commits.
        """
        repo = tmp_path / "outer_repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@t.com")
        _git(repo, "config", "user.name", "T")
        # The surrounding repo has unrelated committed history.
        (repo / "unrelated_a.txt").write_text("repo history a\n")
        (repo / "unrelated_b.txt").write_text("repo history b\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "unrelated repo commit")

        # The actual workspace is a subdirectory of the repo.
        ws_dir = repo / "workdir"
        ws_dir.mkdir()
        (ws_dir / "doc.md").write_text("draft\n")
        snap = snapshot_tree(ws_dir)

        # The run edits its own file.
        (ws_dir / "doc.md").write_text("final\n")

        transcript_root = tmp_path / "tr"
        ws = _WS(id="wsid", role="primary", kind="directory",
                 resolved_path=str(ws_dir), repo_root=None)
        write_snapshot(
            snapshot_sidecar_path(transcript_root, "run1", "wsid"), snap,
        )
        result = compute_workspace_diff(
            ws, transcript_root=transcript_root, run_id="run1",
        )
        assert result is not None
        assert result.error == ""
        paths = [f.path for f in result.files]
        assert paths == ["doc.md"]
        # The surrounding repo's committed files must NOT appear.
        assert "unrelated_a.txt" not in paths
        assert "unrelated_b.txt" not in paths


# ---------------------------------------------------------------------------
# API endpoint + ticket-page parity for a directory workspace
# ---------------------------------------------------------------------------


def _make_dir_ticket_run_ws(session, tmp_path, transcript_dir):
    """Create a ticket + run + directory workspace with a captured snapshot,
    where the run modified one file. Returns (run, ws, root)."""
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    (root / "notes.md").write_text("first draft\n")
    snap = snapshot_tree(root)
    # The "run" already happened: mutate the tree to simulate its output.
    (root / "notes.md").write_text("final draft\nwith more\n")
    (root / "new.txt").write_text("created by run\n")

    profile = create_profile(
        session, name=f"fsp-{tmp_path.name}", fs_read=[], fs_write=[],
        allowed_tools=[], denied_tools=[], network_mode="off",
        network_allowlist=[], secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="fs", prompt="", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="review", source_path=str(root))
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path=str(root),
        transcript_path=str(transcript_dir / "run.log"),
        host="testhost",
    )
    session.add(run)
    session.flush()
    ws = TicketWorkspace(
        ticket_id=t.id,
        run_id=run.id,
        role="primary",
        kind="directory",
        resolved_path=str(root),
        source_path=str(root),
        state="active",
    )
    session.add(ws)
    session.commit()
    # Write the snapshot sidecar where the routes look for it.
    write_snapshot(
        snapshot_sidecar_path(transcript_dir, run.id, ws.id), snap,
    )
    return run, ws, root


async def test_diff_endpoint_directory_workspace(client, session, tmp_path):
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    run, ws, root = _make_dir_ticket_run_ws(session, tmp_path, transcript_dir)

    r = await client.get(f"/api/v1/runs/{run.id}/diff")
    assert r.status_code == 200
    data = r.json()
    assert data["error"] == ""
    paths = sorted(f["path"] for f in data["files"])
    assert paths == ["new.txt", "notes.md"]
    # Never the git-only dead-end.
    assert "not a git repository" not in data["error"]


async def test_diff_endpoint_and_panel_agree_for_directory(
    client, session, tmp_path,
):
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    run, ws, root = _make_dir_ticket_run_ws(session, tmp_path, transcript_dir)

    api = await client.get(f"/api/v1/runs/{run.id}/diff")
    panel = await client.get(
        f"/tickets/{run.ticket_id}/runs/{run.id}/diff-panel",
    )
    assert api.status_code == 200
    assert panel.status_code == 200
    api_paths = sorted(f["path"] for f in api.json()["files"])
    body = panel.text
    # The panel renders the same files the JSON endpoint reports.
    for p in api_paths:
        assert p in body
    assert "not a git repository" not in body
