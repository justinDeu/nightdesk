# tests/test_sandbox.py
import os

import pytest

from nightdesk.domain.permissions import PermissionSpec
from nightdesk.worker.sandbox import assert_no_excluded_paths, build_bwrap_argv


def _spec(**kwargs) -> PermissionSpec:
    """Build a PermissionSpec with a resolved claude binary so build_bwrap_argv
    doesn't raise. Tests that don't care about which binary just inherit the
    host's installed one."""
    kwargs.setdefault("claude_binary_path", _find_claude_for_tests())
    return PermissionSpec(**kwargs)


def _find_claude_for_tests() -> str:
    # Tests don't actually exec the sandbox, but build_bwrap_argv resolves the
    # binary path so the bind-mount source exists. /bin/true is always present.
    return "/bin/true"


def test_off_network_still_shares_net_so_inference_works():
    # ``off`` is user intent — runtime never unshares the net namespace,
    # otherwise the sandboxed CC could not reach api.anthropic.com or
    # whatever ANTHROPIC_BASE_URL points at, and every run would fail.
    spec = _spec(fs_read=[], fs_write=[], network_mode="off")
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["echo", "hi"], env={})
    assert argv[0] == "bwrap"
    assert "--unshare-net" not in argv
    assert argv[-2:] == ["echo", "hi"]


def test_on_network_does_not_unshare():
    spec = _spec(fs_read=[], fs_write=[], network_mode="on")
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})
    assert "--unshare-net" not in argv


def test_off_network_still_mounts_dns_and_ca_so_inference_works():
    # DNS + CA bundle are required for TLS to api.anthropic.com /
    # ANTHROPIC_BASE_URL. They must be mounted in ``off`` mode too.
    spec = _spec(fs_read=[], fs_write=[], network_mode="off")
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})
    triples = list(zip(argv, argv[1:] + [""], argv[2:] + ["", ""]))
    if os.path.exists("/etc/resolv.conf"):
        assert ("--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf") in triples
    # At least one CA bundle bind must be present on any normal Linux host.
    def _is_ca_bind(kind: str, dst: str) -> bool:
        if kind != "--ro-bind":
            return False
        d = dst.lower()
        return "ca-" in d or "cert.pem" in d or "ca-bundle" in d or "ca-certificates" in d
    assert any(_is_ca_bind(kind, dst) for kind, _src, dst in triples)


def test_writable_paths_use_bind_try(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    spec = _spec(fs_read=[], fs_write=[str(workspace)], network_mode="off")
    argv = build_bwrap_argv(spec, working_dir=str(workspace), cmd=["true"], env={})
    pairs = list(zip(argv, argv[1:] + [""]))
    # --bind-try is used (the -try variant tolerates a missing source).
    assert ("--bind-try", str(workspace)) in pairs


def test_readable_paths_use_ro_bind_try(tmp_path):
    readonly = tmp_path / "ro"
    readonly.mkdir()
    spec = _spec(fs_read=[str(readonly)], fs_write=[], network_mode="off")
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})
    pairs = list(zip(argv, argv[1:] + [""]))
    assert ("--ro-bind-try", str(readonly)) in pairs


def test_chdir_is_set():
    spec = _spec(fs_read=[], fs_write=[], network_mode="off")
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["pwd"], env={})
    pairs = list(zip(argv, argv[1:] + [""]))
    assert ("--chdir", "/tmp") in pairs


def test_clearenv_and_setenv_emitted():
    spec = _spec(fs_read=[], fs_write=[], network_mode="off")
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={"FOO": "bar"})
    assert "--clearenv" in argv
    triples = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2)]
    assert ("--setenv", "FOO", "bar") in triples


def test_excluded_paths_are_rejected():
    home = os.path.expanduser("~")
    bad = os.path.join(home, ".config", "nightdesk")
    spec = _spec(fs_read=[], fs_write=[bad], network_mode="off")
    with pytest.raises(ValueError, match="protected directory"):
        build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})


def test_assert_no_excluded_paths_allows_unrelated(tmp_path):
    # Sanity: random paths under tmp are fine.
    assert_no_excluded_paths([str(tmp_path / "anything")])


def test_cc_sessions_dir_binds_session_store(tmp_path):
    """A per-run session store is bound over CLAUDE_CONFIG_DIR/projects so the
    conversation survives sandbox teardown, and the dir is created."""
    store = tmp_path / "nightdesk-cc-sessions" / "run-1"
    spec = _spec(fs_read=[], fs_write=[], network_mode="off")
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={},
                            cc_sessions_dir=str(store))
    triples = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2)]
    assert ("--bind", str(store), "/sandbox-home/.claude/projects") in triples
    assert store.is_dir()  # created so the bind source exists


def test_cc_sessions_dir_under_protected_dir_is_rejected():
    """A store path inside the protected nightdesk data dir must be refused —
    the agent must never see ~/.local/share/nightdesk."""
    bad = os.path.join(os.path.expanduser("~"), ".local", "share",
                       "nightdesk", "cc-sessions", "run-1")
    spec = _spec(fs_read=[], fs_write=[], network_mode="off")
    with pytest.raises(ValueError, match="protected directory"):
        build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={},
                         cc_sessions_dir=bad)


def test_publish_cc_session_mirrors_into_host_claude(tmp_path, monkeypatch):
    """publish_cc_session copies the run's session jsonl from its per-run store
    into ~/.claude/projects, mirroring the relative path so `claude --resume`
    finds it."""
    from nightdesk.worker.sandbox import publish_cc_session

    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))

    store = tmp_path / "store"
    enc = "-home-thor-fun-nightdesk"
    sess = "sess-abc-123"
    src = store / enc / f"{sess}.jsonl"
    src.parent.mkdir(parents=True)
    src.write_text('{"type":"summary"}\n')

    dst = publish_cc_session(str(store), sess)
    expected = fake_home / ".claude" / "projects" / enc / f"{sess}.jsonl"
    assert dst == str(expected)
    assert expected.read_text() == '{"type":"summary"}\n'


def test_publish_cc_session_missing_is_noop(tmp_path, monkeypatch):
    from nightdesk.worker.sandbox import publish_cc_session

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert publish_cc_session(str(tmp_path / "empty-store"), "nope") is None


def test_default_worktree_root_survives_the_sandbox_guard():
    # Regression: git_worktree-mode tickets resolve under cfg.worktree_root and
    # that path gets bind-mounted into the sandbox. If the default worktree_root
    # lived under the protected data dir (~/.local/share/nightdesk), every
    # worktree run would die with "inside protected directory". Keep them apart.
    from nightdesk.config import NightdeskConfig

    wt = str(NightdeskConfig().worktree_root)
    assert_no_excluded_paths([wt, os.path.join(wt, "proj", "feature")])


def test_blanket_etc_and_usr_are_not_passed_alone():
    # /usr is bind-mounted ro (necessary for libc + claude binary).
    # /etc is NOT bind-mounted blanket — only specific files (resolv.conf,
    # hosts, CA bundle, passwd, group). The defining check: there is no
    # `--ro-bind /etc /etc` argument pair.
    spec = _spec(fs_read=[], fs_write=[], network_mode="on")
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})
    pairs = list(zip(argv, argv[1:] + [""]))
    assert ("--ro-bind", "/usr") in pairs  # /usr blanket is still mounted
    assert ("--ro-bind", "/etc") not in pairs  # /etc blanket is NOT mounted


def test_credentials_inherit_mounts_credentials_file(tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text("{}")
    spec = _spec(
        fs_read=[], fs_write=[], network_mode="off",
        claude_credentials={"source": "inherit", "value": str(creds)},
    )
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})
    pairs = list(zip(argv, argv[1:] + [""], argv[2:] + ["", ""]))
    assert any(
        kind == "--ro-bind" and src == str(creds)
        and dst == "/sandbox-home/.claude/.credentials.json"
        for kind, src, dst in pairs
    )


def test_credentials_inherit_missing_file_raises(tmp_path):
    spec = _spec(
        fs_read=[], fs_write=[], network_mode="off",
        claude_credentials={"source": "inherit", "value": str(tmp_path / "nope.json")},
    )
    with pytest.raises(ValueError, match="credentials file is missing"):
        build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})


def test_credentials_api_key_does_not_mount_file(tmp_path):
    # api_key auth is handled at the env layer, not the mount layer.
    spec = _spec(
        fs_read=[], fs_write=[], network_mode="off",
        claude_credentials={"source": "api_key", "value": "sk-test"},
    )
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})
    # No /sandbox-home/.claude/.credentials.json mount target should appear.
    assert not any("/.credentials.json" in a for a in argv)

def test_excluded_path_ancestor_is_rejected():
    home = os.path.expanduser("~")

    with pytest.raises(ValueError, match="protected directory"):
        assert_no_excluded_paths([home])


def test_tool_paths_are_added_to_path_and_mounted_read_only(tmp_path):
    from nightdesk.worker.run_one import _build_env

    tool_dir = tmp_path / "tools" / "bin"
    tool_dir.mkdir(parents=True)
    spec = _spec(tool_paths=[str(tool_dir)])

    env = _build_env(spec, {})
    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env=env)
    triples = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2)]

    assert env["PATH"].split(os.pathsep)[0] == str(tool_dir)
    assert ("--ro-bind-try", str(tool_dir), str(tool_dir)) in triples


def test_tool_path_symlink_runtime_root_is_mounted(tmp_path):
    user_bin = tmp_path / "home" / ".local" / "bin"
    venv_root = tmp_path / "home" / ".local" / "share" / "pipx" / "venvs" / "ruff"
    target_bin = venv_root / "bin"
    user_bin.mkdir(parents=True)
    target_bin.mkdir(parents=True)
    target = target_bin / "ruff"
    target.write_text("#!/bin/sh\n")
    (user_bin / "ruff").symlink_to(target)
    spec = _spec(tool_paths=[str(user_bin)])

    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})
    triples = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2)]

    assert ("--ro-bind-try", str(user_bin), str(user_bin)) in triples
    assert ("--ro-bind-try", str(venv_root), str(venv_root)) in triples

def test_tool_path_symlink_with_target_inside_tool_dir_does_not_mount_parent(tmp_path):
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()
    target = tool_dir / "tmux-sessionizer"
    target.write_text("#!/bin/sh\n")
    (tool_dir / "tm").symlink_to(target)
    spec = _spec(tool_paths=[str(tool_dir)])

    argv = build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={})
    triples = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2)]

    assert ("--ro-bind-try", str(tool_dir), str(tool_dir)) in triples
    assert ("--ro-bind-try", str(tmp_path), str(tmp_path)) not in triples


def test_git_dir_is_mounted_read_write(tmp_path):
    """A git_worktree's git_common_dir must be bound read-write at the same
    path so the worktree's `.git` pointer resolves and refs can be updated —
    without the user hand-adding a `.git` workspace."""
    git_common = tmp_path / "repo" / ".git"
    git_common.mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    spec = _spec(fs_read=[], fs_write=[str(worktree)], network_mode="off")
    argv = build_bwrap_argv(
        spec, working_dir=str(worktree), cmd=["true"], env={},
        git_dirs=[str(git_common)],
    )
    triples = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2)]
    # Read-write (--bind-try, not --ro-bind-try) at the same absolute path.
    assert ("--bind-try", str(git_common), str(git_common)) in triples
    assert ("--ro-bind-try", str(git_common), str(git_common)) not in triples


def test_bare_git_dir_is_mounted(tmp_path):
    """The `.bare` dir of a bare-container layout (git_common_dir == .bare) is
    bound the same way."""
    bare = tmp_path / "repo" / ".bare"
    bare.mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    spec = _spec(fs_read=[], fs_write=[str(worktree)], network_mode="off")
    argv = build_bwrap_argv(
        spec, working_dir=str(worktree), cmd=["true"], env={},
        git_dirs=[str(bare)],
    )
    triples = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2)]
    assert ("--bind-try", str(bare), str(bare)) in triples


def test_no_git_dirs_adds_no_git_mounts(tmp_path):
    """A non-git ticket (no git_dirs) gets no extra git binds."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    spec = _spec(fs_read=[], fs_write=[str(workspace)], network_mode="off")
    base = build_bwrap_argv(spec, working_dir=str(workspace), cmd=["true"], env={})
    with_empty = build_bwrap_argv(spec, working_dir=str(workspace), cmd=["true"],
                                  env={}, git_dirs=[])
    assert base == with_empty


def test_git_dir_overlapping_fs_mount_is_not_double_mounted(tmp_path):
    """If a git dir is already covered by the working dir / an fs mount, it is
    not re-bound (bwrap rejects mounting the same destination twice)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    spec = _spec(fs_read=[], fs_write=[str(worktree)], network_mode="off")
    argv = build_bwrap_argv(
        spec, working_dir=str(worktree), cmd=["true"], env={},
        git_dirs=[str(worktree)],
    )
    # Exactly one bind of the worktree path (from fs_write), none added for git.
    binds = [i for i in range(len(argv) - 2)
             if argv[i] == "--bind-try" and argv[i + 1] == str(worktree)]
    assert len(binds) == 1


def test_git_dir_under_protected_dir_is_rejected():
    """A git dir overlapping the protected nightdesk data dir is refused."""
    bad = os.path.join(os.path.expanduser("~"), ".local", "share",
                       "nightdesk", "repo", ".git")
    spec = _spec(fs_read=[], fs_write=[], network_mode="off")
    with pytest.raises(ValueError, match="protected directory"):
        build_bwrap_argv(spec, working_dir="/tmp", cmd=["true"], env={},
                         git_dirs=[bad])


def test_tool_runtime_root_only_skips_exact_system_bins():
    from pathlib import Path

    from nightdesk.worker.sandbox import _tool_runtime_root

    # Real system bin path: should not return parent.parent ("/").
    assert _tool_runtime_root(Path("/usr/bin/cat")) == Path("/usr/bin")
    assert _tool_runtime_root(Path("/usr/local/bin/foo")) == Path("/usr/local/bin")

    # Decoy path whose string starts with "/usr/bin" but is a separate dir.
    # The old prefix-string check would falsely return the bin subdir here.
    assert _tool_runtime_root(Path("/usr/binary/pkg/bin/tool")) == Path("/usr/binary/pkg")


def test_exclusion_paths_includes_custom_data_dir(monkeypatch, tmp_path):
    """When NIGHTDESK_DATA_DIR is set, _exclusion_paths includes the custom path."""
    from nightdesk.worker.sandbox import _exclusion_paths

    monkeypatch.setenv("NIGHTDESK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NIGHTDESK_DB_PATH", raising=False)
    monkeypatch.delenv("NIGHTDESK_TRANSCRIPT_ROOT", raising=False)
    monkeypatch.delenv("NIGHTDESK_LOG_DIR", raising=False)
    monkeypatch.delenv("NIGHTDESK_CONFIG", raising=False)

    paths = _exclusion_paths()

    assert str(tmp_path) in paths
    # Static defaults must always be present.
    import os
    home = os.path.expanduser("~")
    assert os.path.join(home, ".local", "share", "nightdesk") in paths
    assert os.path.join(home, ".config", "nightdesk") in paths


def test_exclusion_paths_includes_relocated_secrets_parent(monkeypatch, tmp_path):
    """When NIGHTDESK_SECRETS points elsewhere, its parent dir is excluded too.

    In the default case the static ``~/.config/nightdesk`` entry already covers
    ``secrets.env``. If NIGHTDESK_SECRETS relocates the file, the parent is no
    longer covered by the static list and must be appended dynamically.
    load_config tolerates a missing secrets file on disk (it checks
    ``secrets_path.exists()``), so pointing at a not-yet-created path is fine.
    """
    from nightdesk.worker.sandbox import _exclusion_paths

    alt = tmp_path / "alt" / "secrets.env"
    monkeypatch.setenv("NIGHTDESK_SECRETS", str(alt))
    monkeypatch.delenv("NIGHTDESK_CONFIG", raising=False)
    monkeypatch.delenv("NIGHTDESK_DATA_DIR", raising=False)
    monkeypatch.delenv("NIGHTDESK_DB_PATH", raising=False)
    monkeypatch.delenv("NIGHTDESK_TRANSCRIPT_ROOT", raising=False)
    monkeypatch.delenv("NIGHTDESK_LOG_DIR", raising=False)

    paths = _exclusion_paths()

    assert str(tmp_path / "alt") in paths
    # Static defaults must always be present.
    home = os.path.expanduser("~")
    assert os.path.join(home, ".local", "share", "nightdesk") in paths
    assert os.path.join(home, ".config", "nightdesk") in paths
