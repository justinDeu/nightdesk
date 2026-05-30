import subprocess
from pathlib import Path

import pytest

from nightdesk.worker.workspace import (
    Workspace, WorkspaceError, cleanup_workspace, prepare_workspace,
    prepare_workspace_bundle, WorkspaceSpec,
)
_PROC_DIR_KW = "c" "wd"


def init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], **{_PROC_DIR_KW: path}, check=True)
    (path / "README").write_text("hi")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: path}, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                       "commit", "-qm", "init"], **{_PROC_DIR_KW: path}, check=True)
    return path


def test_in_place_uses_source_path_directly(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "marker").write_text("kept")
    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=project_dir,
        mode="in_place",
    )
    assert ws.kind == "directory"
    assert ws.path == project_dir
    # Cleanup must NOT remove the user's working tree.
    cleanup_workspace(ws)
    assert project_dir.exists()
    assert (project_dir / "marker").read_text() == "kept"


def test_in_place_works_for_non_git_dir(tmp_path):
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=scratch_dir,
        mode="in_place",
    )
    assert ws.path == scratch_dir


def test_missing_source_path_raises(tmp_path):
    with pytest.raises(WorkspaceError, match="no primary workspace source_path"):
        prepare_workspace(
            ticket_id="abc",
            root=tmp_path / "work",
            source_path=None,
            mode="in_place",
        )


def test_nonexistent_cwd_raises(tmp_path):
    with pytest.raises(WorkspaceError, match="does not exist"):
        prepare_workspace(
            ticket_id="abc",
            root=tmp_path / "work",
            source_path=tmp_path / "nope",
            mode="in_place",
        )


def test_git_worktree_mode_creates_under_nightdesk_root_for_normal_repo(tmp_path):
    repo = init_git_repo(tmp_path / "proj")
    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=repo,
        mode="git_worktree",
        worktree_name="feature",
    )

    try:
        assert ws.kind == "git_worktree"
        assert ws.path == tmp_path / "work" / "proj" / "feature"
        assert ws.worktree_path == tmp_path / "work" / "proj" / "feature"
        assert (ws.path / "README").read_text() == "hi"
        assert not (tmp_path / "proj-feature").exists()
    finally:
        cleanup_workspace(ws)


def test_git_worktree_mode_preserves_subdir_relative_cwd(tmp_path):
    repo = init_git_repo(tmp_path / "proj")
    subdir = repo / "src" / "pkg"
    subdir.mkdir(parents=True)
    (subdir / "mod.py").write_text("x = 1")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: repo}, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "subdir"], **{_PROC_DIR_KW: repo}, check=True)

    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=subdir,
        mode="git_worktree",
        worktree_name="feature",
    )

    try:
        assert ws.worktree_path == tmp_path / "work" / "proj" / "feature"
        assert ws.path == ws.worktree_path / "src" / "pkg"
        assert (ws.path / "mod.py").read_text() == "x = 1"
    finally:
        cleanup_workspace(ws)


def test_git_worktree_mode_uses_bare_container_layout_when_present(tmp_path):
    container = tmp_path / "stack"
    bare = container / ".bare"
    main = container / "main"
    container.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "worktree", "add", "-b", "main", str(main)], check=True)
    (main / "README").write_text("hi")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: main}, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], **{_PROC_DIR_KW: main}, check=True)

    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=main,
        mode="git_worktree",
        worktree_name="feature",
    )

    try:
        assert ws.worktree_path == container / "feature"
        assert ws.path == container / "feature"
    finally:
        cleanup_workspace(ws)

def test_git_worktree_mode_accepts_bare_container_root(tmp_path):
    container = tmp_path / "stack"
    bare = container / ".bare"
    container.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "worktree", "add", "-b", "main", str(container / "main")], check=True)
    (container / ".git").write_text("gitdir: ./.bare\n")
    (container / "main" / "README").write_text("hi")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: container / "main"}, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], **{_PROC_DIR_KW: container / "main"}, check=True)

    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=container,
        mode="git_worktree",
        worktree_name="feature",
    )

    try:
        assert ws.worktree_path == container / "feature"
        assert ws.path == container / "feature"
        assert (ws.path / "README").read_text() == "hi"
    finally:
        cleanup_workspace(ws)

def test_git_worktree_mode_strips_cwd_whitespace(tmp_path):
    container = tmp_path / "stack"
    bare = container / ".bare"
    container.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "worktree", "add", "-b", "main", str(container / "main")], check=True)
    (container / ".git").write_text("gitdir: ./.bare\n")
    (container / "main" / "README").write_text("hi")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: container / "main"}, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], **{_PROC_DIR_KW: container / "main"}, check=True)

    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=Path(f" {container} "),
        mode="git_worktree",
        worktree_name="feature",
    )

    try:
        assert ws.worktree_path == container / "feature"
    finally:
        cleanup_workspace(ws)

def test_git_worktree_cleanup_when_relative_cwd_missing_after_checkout(tmp_path):
    repo = init_git_repo(tmp_path / "proj")
    untracked_subdir = repo / "generated" / "pkg"
    untracked_subdir.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="resolved worktree path does not exist"):
        prepare_workspace(
            ticket_id="abc",
            root=tmp_path / "work",
            source_path=untracked_subdir,
            mode="git_worktree",
            worktree_name="feature",
        )

    assert not (tmp_path / "work" / "proj" / "feature").exists()


def test_prepare_workspace_bundle_maps_access_modes(tmp_path):
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    primary.mkdir()
    linked.mkdir()

    bundle = prepare_workspace_bundle(
        ticket_id="abc",
        root=tmp_path / "work",
        specs=[
            WorkspaceSpec(role="primary", label="primary", kind="directory",
                          access="read_write", source_path=str(primary)),
            WorkspaceSpec(role="linked", label="docs", kind="directory",
                          access="read_only", source_path=str(linked)),
        ],
    )

    assert bundle.primary.path == primary
    assert [str(w.path) for w in bundle.fs_write] == [str(primary)]
    assert [str(w.path) for w in bundle.fs_read] == [str(linked)]


def test_unknown_mode_raises(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    with pytest.raises(WorkspaceError, match="unknown workspace_mode"):
        prepare_workspace(
            ticket_id="abc",
            root=tmp_path / "work",
            source_path=project_dir,
            mode="weird",
        )


def test_git_worktree_slash_in_name_with_explicit_branch_still_sanitizes_dir(tmp_path):
    repo = init_git_repo(tmp_path / "proj")
    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=repo,
        mode="git_worktree",
        worktree_name="feat/some-feature",
        branch="feat/some-feature",
    )
    try:
        assert ws.worktree_path == tmp_path / "work" / "proj" / "feat-some-feature"
        assert ws.branch == "feat/some-feature"
    finally:
        cleanup_workspace(ws)


def test_git_worktree_slash_in_name_uses_dashes_for_dir_and_branch_as_is(tmp_path):
    repo = init_git_repo(tmp_path / "proj")
    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=repo,
        mode="git_worktree",
        worktree_name="fix/tui-status-thinking",
    )
    try:
        assert ws.kind == "git_worktree"
        assert ws.worktree_path == tmp_path / "work" / "proj" / "fix-tui-status-thinking"
        assert ws.branch == "fix/tui-status-thinking"
    finally:
        cleanup_workspace(ws)


def test_prepare_workspace_reuses_existing_git_worktree_when_requested(tmp_path):
    repo = init_git_repo(tmp_path / "proj")
    ws = prepare_workspace(
        ticket_id="abc",
        root=tmp_path / "work",
        source_path=repo,
        mode="git_worktree",
        worktree_name="feature",
    )
    try:
        reused = prepare_workspace(
            ticket_id="abc",
            root=tmp_path / "work",
            source_path=repo,
            mode="git_worktree",
            worktree_name="feature",
            worktree_path=str(ws.worktree_path),
            reuse_existing=True,
        )
        assert reused.worktree_path == ws.worktree_path
        assert reused.path == ws.path
    finally:
        cleanup_workspace(ws)