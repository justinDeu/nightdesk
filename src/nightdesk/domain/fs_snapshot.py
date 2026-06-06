"""Filesystem snapshots and diffs for non-git (``directory``) workspaces.

Git workspaces get their per-run "Changes" view from :mod:`nightdesk.domain.diff`
(``start_sha..end`` via git). Plain directory workspaces have no git history, so
we instead snapshot the workspace tree at run start -- a map of relative path ->
``(size, mtime, content)`` for files under the workspace -- persist it as a JSON
sidecar keyed to the run + workspace, and at review time diff that snapshot
against the current tree.

The result is rendered through the same :class:`~nightdesk.domain.diff.RunDiff`
structure the git path produces, so ``partials/diff_panel.html`` shows real
added / modified / deleted files (with line-level text diffs) for non-git runs.

Storage choice: a sidecar JSON file under the run's transcript dir, not a new
schema column. The snapshot is bulky (it stores text-file contents so review can
produce real line diffs), run-scoped, and never queried relationally -- a file
keyed by ``{run_id}.{workspace_id}.json`` next to the run's transcript is the
natural home and keeps the DB narrow.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from nightdesk.domain.diff import (
    RunDiff,
    _MAX_FILES,
    _MAX_LINES,
    _parse_unified_diff,
)

# Directory names never worth snapshotting: VCS metadata, dependency trees,
# build outputs, and tool caches. Pruned during the walk so we never descend
# into them (cheap) and never report their churn as run changes.
_EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "vendor",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".nox", ".eggs", "*.egg-info",
    ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", ".svelte-kit", "target",
    ".cache", ".parcel-cache", ".gradle", ".idea", ".vscode",
})

# Per-file content cap. Files larger than this are tracked for
# add/modify/delete (via size + mtime) but their content is not stored, so they
# render as binary-style "changed, no inline diff" entries rather than bloating
# the snapshot or producing a giant line diff.
_MAX_CONTENT_BYTES = 1_000_000

# Hard cap on snapshot size so a pathological tree can't blow up memory/disk.
_MAX_SNAPSHOT_FILES = 20_000


def snapshot_sidecar_path(transcript_root: Path | str, run_id: str, ws_id: str) -> Path:
    """Deterministic location of a workspace's run-start snapshot.

    Keyed by ``run_id`` *and* ``ws_id`` so a ticket with several directory
    workspaces (or several runs reusing one workspace row) never collides.
    Lives under the transcript dir, which both the writer (worker, via
    ``cfg.transcript_root``) and the reader (API route, via
    ``Path(run.transcript_path).parent``) resolve to the same place.
    """
    return Path(transcript_root) / "snapshots" / f"{run_id}.{ws_id}.json"


def _is_text(data: bytes) -> bool:
    """Heuristic: treat NUL-containing or non-UTF-8 bytes as binary."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _file_entry(path: Path) -> Optional[dict]:
    """Build a snapshot entry for one file, or None if it can't be read."""
    try:
        st = path.stat()
    except OSError:
        return None
    size = st.st_size
    mtime = st.st_mtime
    if size > _MAX_CONTENT_BYTES:
        # Too big to store content; track metadata only.
        return {"size": size, "mtime": mtime, "text": False, "hash": None}
    try:
        data = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    text = _is_text(data)
    entry = {"size": size, "mtime": mtime, "text": text, "hash": digest}
    if text:
        entry["content"] = data.decode("utf-8")
    return entry


def snapshot_tree(root: str | Path) -> dict:
    """Snapshot every (non-excluded) file under ``root``.

    Returns ``{"root": str, "truncated": bool, "files": {rel: entry}}`` where
    each ``entry`` carries ``size``, ``mtime``, ``hash``, ``text`` and (for
    text files under the cap) ``content``.
    """
    root_path = Path(root)
    files: dict[str, dict] = {}
    truncated = False
    if root_path.is_dir():
        for dirpath, dirnames, filenames in os.walk(root_path):
            # Prune excluded directories in place so os.walk skips them.
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
            for name in filenames:
                full = Path(dirpath) / name
                if full.is_symlink() or not full.is_file():
                    continue
                rel = os.path.relpath(full, root_path)
                entry = _file_entry(full)
                if entry is None:
                    continue
                files[rel] = entry
                if len(files) >= _MAX_SNAPSHOT_FILES:
                    truncated = True
                    break
            if truncated:
                break
    return {"root": str(root_path), "truncated": truncated, "files": files}


def write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot), encoding="utf-8")


def read_snapshot(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _entry_changed(old: dict, new: dict) -> bool:
    """Did a file present in both snapshots change?

    Prefer content hashes when both sides have one; fall back to size + mtime
    for oversized (content-less) files.
    """
    old_hash = old.get("hash")
    new_hash = new.get("hash")
    if old_hash is not None and new_hash is not None:
        return old_hash != new_hash
    return old.get("size") != new.get("size") or old.get("mtime") != new.get("mtime")


def _split_lines(text: str) -> list[str]:
    # keepends=True so difflib emits proper unified output; we strip the diff
    # markers later via the shared parser.
    return text.splitlines(keepends=True)


def _file_block(rel: str, old: Optional[dict], new: Optional[dict]) -> str:
    """Render one changed file as a unified-diff block for the shared parser.

    ``old``/``new`` are snapshot entries (or None for add/delete). Text files
    with stored content on both relevant sides get a real line diff; anything
    else gets a synthetic ``Binary files`` marker so it shows as "changed,
    no inline diff".
    """
    header = f"diff --git a/{rel} b/{rel}"

    old_text = old.get("content") if old else None
    new_text = new.get("content") if new else None

    # We can produce a real text diff only when every side that exists has
    # stored text content. (A pure add has no old side; a pure delete has no
    # new side.) Otherwise fall back to a binary-style marker.
    can_text = True
    if old is not None and old_text is None:
        can_text = False
    if new is not None and new_text is None:
        can_text = False

    if not can_text:
        return f"{header}\nBinary files a/{rel} and b/{rel} differ"

    from_lines = _split_lines(old_text) if old_text is not None else []
    to_lines = _split_lines(new_text) if new_text is not None else []
    from_file = "/dev/null" if old is None else f"a/{rel}"
    to_file = "/dev/null" if new is None else f"b/{rel}"
    body = "".join(difflib.unified_diff(
        from_lines, to_lines, fromfile=from_file, tofile=to_file, n=3,
    ))
    # An empty body would mean "no change"; callers only pass changed files,
    # but guard anyway so we never emit a header with no hunks.
    if not body:
        return ""
    return f"{header}\n{body.rstrip(chr(10))}"


def compute_fs_run_diff(
    root: str,
    snapshot: Optional[dict],
    branch: Optional[str] = None,
) -> RunDiff:
    """Diff a run-start filesystem snapshot against the current tree.

    Mirrors :func:`nightdesk.domain.diff.compute_run_diff`: returns a
    :class:`RunDiff` with per-file added/modified/deleted hunks, the same size
    truncation behavior, and a non-fatal ``error`` for the degenerate cases
    (missing root, no snapshot captured).
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return RunDiff(error=f"workspace path does not exist: {root}")
    if not snapshot or "files" not in snapshot:
        return RunDiff(
            error="no filesystem snapshot was captured for this run",
            branch=branch or "",
            repo_root=str(root_path),
        )

    old_files: dict[str, dict] = snapshot.get("files") or {}
    current = snapshot_tree(root_path)
    new_files: dict[str, dict] = current.get("files") or {}

    added = sorted(set(new_files) - set(old_files))
    deleted = sorted(set(old_files) - set(new_files))
    common = sorted(set(old_files) & set(new_files))
    modified = [rel for rel in common if _entry_changed(old_files[rel], new_files[rel])]

    blocks: list[str] = []
    for rel in sorted(added):
        blocks.append(_file_block(rel, None, new_files[rel]))
    for rel in sorted(modified):
        blocks.append(_file_block(rel, old_files[rel], new_files[rel]))
    for rel in sorted(deleted):
        blocks.append(_file_block(rel, old_files[rel], None))

    combined = "\n".join(b for b in blocks if b)
    result = _parse_unified_diff(combined)

    # Same truncation policy as the git path.
    total_lines = sum(len(f.hunks) for f in result.files)
    if len(result.files) > _MAX_FILES or total_lines > _MAX_LINES:
        all_files = result.files
        result.files = all_files[:_MAX_FILES]
        result.truncated = True
        result.hidden_files = len(all_files) - _MAX_FILES
        result.hidden_lines = total_lines - sum(len(f.hunks) for f in result.files)

    result.branch = branch or ""
    result.repo_root = str(root_path)
    if snapshot.get("truncated") or current.get("truncated"):
        # Snapshot itself hit the file cap; flag it so the view isn't read as
        # exhaustive.
        result.truncated = True
    return result
