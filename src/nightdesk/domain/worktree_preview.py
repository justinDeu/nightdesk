"""Pure helpers for previewing where a git_worktree ticket workspace would
land, and whether a workspace's ``base_ref`` resolves in its source repo.

Split out of the (now-removed) HTMX board routes so the JSON
``/api/v1/preview/worktree-name`` endpoint (``api/routes/helpers.py``) has
somewhere to import them from that carries no FastAPI/Jinja dependency.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


def safe_preview_name(name: Optional[str]) -> str:
    raw = (name or "").strip().strip("/")
    if not raw:
        return "ticket-worktree"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in raw)
    return safe.strip(".-_") or "ticket-worktree"


def git_value(source_dir: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def base_ref_status(source_path: str, base_ref: Optional[str]) -> Optional[str]:
    """Return whether ``base_ref`` resolves to a commit in the repo at ``source_path``.

    - ``None``  -> nothing to check (no base_ref, or source_path not a usable git dir).
    - ``"ok"``  -> the ref resolves to a commit; the worktree branch can start there.
    - ``"missing"`` -> the ref does not resolve. The "branch is gone" case a
      caller should warn about: ``git worktree add ... <base_ref>`` would
      fail at run time, leaving the ticket stuck. Surfacing it at preview
      time is the whole point of this check.
    """
    ref = (base_ref or "").strip()
    if not ref:
        return None
    try:
        source = Path(os.path.expanduser(source_path.strip())).resolve()
    except Exception:
        return None
    # Confirm this is actually a git working area before judging the ref.
    if git_value(source, "rev-parse", "--git-dir") is None:
        return None
    resolved = git_value(source, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return "ok" if resolved else "missing"


def is_bare_container(path: Path) -> bool:
    bare = path / ".bare"
    git_file = path / ".git"
    if not bare.is_dir() or not git_file.is_file():
        return False
    try:
        if "gitdir:" not in git_file.read_text():
            return False
    except OSError:
        return False
    return git_value(bare, "rev-parse", "--is-bare-repository") == "true"


def preview_worktree_path(*, source_path: str, name: Optional[str],
                          custom_path: Optional[str],
                          worktree_root: Path) -> tuple[Path, str]:
    if custom_path and custom_path.strip():
        return Path(os.path.expanduser(custom_path.strip())), "custom path"
    source = Path(os.path.expanduser(source_path.strip())).resolve()
    wt_name = safe_preview_name(name)
    if is_bare_container(source):
        return source / wt_name, "bare-container layout"
    repo_root_raw = git_value(source, "rev-parse", "--show-toplevel")
    common_raw = git_value(source, "rev-parse", "--git-common-dir")
    if repo_root_raw and common_raw:
        repo_root = Path(repo_root_raw).resolve()
        common = Path(common_raw)
        if not common.is_absolute():
            common = (source / common).resolve()
        else:
            common = common.resolve()
        if common != repo_root / ".git" and common.parent == repo_root.parent:
            return common.parent / wt_name, "bare-container layout"
        return worktree_root / repo_root.name / wt_name, "Nightdesk worktree root"
    return worktree_root / source.name / wt_name, "Nightdesk worktree root"
