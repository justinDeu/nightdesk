"""Tests for the path autocomplete endpoint."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nightdesk.api.routes.fs import _suggest_dirs


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """Build a small directory tree for completion tests."""
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "nested").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "betatron").mkdir()
    (tmp_path / "afile.txt").write_text("x")
    (tmp_path / ".hidden").mkdir()
    return tmp_path


def test_lists_children_when_prefix_ends_with_slash(sample_tree: Path) -> None:
    out = _suggest_dirs(str(sample_tree) + "/")
    names = sorted(p.rsplit("/", 2)[-2] for p in out)
    assert names == ["alpha", "beta", "betatron"]
    # All entries are absolute and end with '/'
    assert all(p.startswith("/") and p.endswith("/") for p in out)


def test_filters_by_basename_prefix(sample_tree: Path) -> None:
    out = _suggest_dirs(str(sample_tree) + "/be")
    assert sorted(out) == [
        str(sample_tree) + "/beta/",
        str(sample_tree) + "/betatron/",
    ]


def test_hidden_dirs_excluded_unless_dot_typed(sample_tree: Path) -> None:
    out = _suggest_dirs(str(sample_tree) + "/")
    assert not any("/.hidden/" in p for p in out)
    out_dot = _suggest_dirs(str(sample_tree) + "/.")
    assert out_dot == [str(sample_tree) + "/.hidden/"]


def test_files_are_skipped(sample_tree: Path) -> None:
    out = _suggest_dirs(str(sample_tree) + "/a")
    assert all("afile" not in p for p in out)
    assert out == [str(sample_tree) + "/alpha/"]


def test_non_absolute_prefix_returns_empty() -> None:
    assert _suggest_dirs("relative/path") == []


def test_nonexistent_directory_returns_empty(tmp_path: Path) -> None:
    assert _suggest_dirs(str(tmp_path / "does-not-exist") + "/") == []


def test_limit_is_honored(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"d{i}").mkdir()
    out = _suggest_dirs(str(tmp_path) + "/", limit=3)
    assert len(out) == 3


def test_tilde_prefix_renders_with_tilde(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the user types '~', the suggestions should also start with '~'."""
    monkeypatch.setenv("HOME", str(sample_tree))
    out = _suggest_dirs("~/")
    # All paths should be relative-to-home (start with '~/'), not absolute.
    assert out, "expected at least one suggestion"
    for p in out:
        assert p.startswith("~/"), f"expected ~/ prefix, got {p}"
    assert "~/alpha/" in out
    assert "~/beta/" in out


def test_tilde_with_partial_basename(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(sample_tree))
    out = _suggest_dirs("~/be")
    assert sorted(out) == ["~/beta/", "~/betatron/"]


def test_tilde_bare_completes_home(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typing just '~' should suggest the home directory itself as '~/'."""
    monkeypatch.setenv("HOME", str(sample_tree))
    # Parent of HOME must exist and HOME's basename must be unique enough
    # in its parent for this assertion to be tight, so we use a real parent
    # lookup via the suggestion itself.
    out = _suggest_dirs("~")
    # The home directory itself should appear, rendered as '~/'.
    assert "~/" in out


def test_nested_tilde_drill_down(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(sample_tree))
    out = _suggest_dirs("~/alpha/")
    assert out == ["~/alpha/nested/"]


def test_absolute_path_unchanged_when_no_tilde(sample_tree: Path) -> None:
    """Without a tilde input, the response stays absolute even if the
    listed children happen to live under $HOME."""
    out = _suggest_dirs(str(sample_tree) + "/")
    assert all(p.startswith("/") for p in out)


@pytest.mark.anyio
async def test_html_partial_round_trip(client, sample_tree, monkeypatch):
    """The HTMX-driven HTML endpoint returns suggestion markup."""
    monkeypatch.setenv("HOME", str(sample_tree))
    r = await client.get("/fs/suggest", params={"q": "~/", "target": "cwd-input"})
    assert r.status_code == 200
    body = r.text
    assert "data-suggest-value=\"~/alpha/\"" in body
    assert "data-suggest-value=\"~/beta/\"" in body
    # Make sure the suggestion is a clickable list item, not a button.
    assert "onmousedown=" in body
    assert "<button" not in body
