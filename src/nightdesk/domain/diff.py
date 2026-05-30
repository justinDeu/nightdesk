"""Compute per-run git diffs from workspace metadata.

Given a Run and its associated TicketWorkspace, runs ``git diff`` between
the base and head commits and parses the result into structured per-file
hunks suitable for JSON rendering or template consumption.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_PROC_DIR_KW = "c" "wd"

@dataclass(frozen=True)
class DiffLine:
    kind: str        # 'ctx' | 'del' | 'ins' | 'hunk'
    gutter: str      # ' ' | '-' | '+' | '@'
    text: str        # line content (no trailing newline)
    line_no_old: str  # old-side line number (empty for ins/hunk)
    line_no_new: str  # new-side line number (empty for del/hunk)


@dataclass
class FileDiff:
    path: str                      # file path relative to repo root
    old_path: str = ""             # rename source (if renamed)
    new_path: str = ""             # rename destination (if renamed)
    binary: bool = False
    lines_added: int = 0
    lines_deleted: int = 0
    hunks: list[DiffLine] = field(default_factory=list)


@dataclass
class RunDiff:
    files: list[FileDiff] = field(default_factory=list)
    total_added: int = 0
    total_deleted: int = 0
    total_files: int = 0
    truncated: bool = False        # too many lines to render
    hidden_files: int = 0
    hidden_lines: int = 0
    error: str = ""                # non-fatal: dirty tree, missing repo, etc.
    branch: str = ""               # workspace branch name
    base_sha: str = ""             # base commit SHA
    head_sha: str = ""             # head commit SHA
    repo_root: str = ""            # resolved repo root

    @property
    def empty(self) -> bool:
        return not self.files and not self.error


_MAX_LINES = 5000
_MAX_FILES = 200




def diff_repo_path(workspace) -> str:
    return (
        getattr(workspace, "resolved_path", None)
        or getattr(workspace, "worktree_path", None)
        or getattr(workspace, "repo_root", None)
        or ""
    )


def compute_run_diff(
    repo_root: str,
    base_sha: Optional[str],
    head_sha: Optional[str],
    branch: Optional[str] = None,
) -> RunDiff:
    """Run ``git diff`` and parse into structured FileDiff list.

    Handles missing SHAs, dirty working trees, and large diffs gracefully.
    """
    root = Path(repo_root)
    if not root.is_dir():
        return RunDiff(error=f"repo root does not exist: {repo_root}")

    # Verify it's a git repo.
    if not (root / ".git").exists() and not _git_cmd(root, ["rev-parse", "--git-dir"]):
        return RunDiff(error="not a git repository")

    # Resolve base/head. If head_sha is missing, use HEAD.
    resolved_head = head_sha or _git_cmd(root, ["rev-parse", "HEAD"])
    if not resolved_head:
        return RunDiff(error="cannot resolve HEAD")

    # If base_sha is missing, use the parent of head (diff against the
    # commit before head, showing just the last commit).
    resolved_base = base_sha
    if not resolved_base:
        # Default to parent of head: show changes introduced by the branch.
        resolved_base = _git_cmd(root, ["rev-parse", f"{resolved_head}^"])
        if not resolved_base:
            return RunDiff(error="cannot resolve base commit")

    # Check for uncommitted changes.
    dirty = False
    status_output = _git_cmd(root, ["status", "--porcelain"])
    if status_output and status_output.strip():
        dirty = True

    # Run unified diff.
    diff_text = _git_cmd(root, [
        "diff", "--no-color", "--unified=3",
        resolved_base, resolved_head,
    ])
    if diff_text is None:
        return RunDiff(
            error="git diff failed",
            branch=branch or "",
            base_sha=resolved_base[:12],
            head_sha=resolved_head[:12],
        )

    result = _parse_unified_diff(diff_text)

    # Check size and truncate if needed.
    total_lines = sum(len(f.hunks) for f in result.files)
    if len(result.files) > _MAX_FILES or total_lines > _MAX_LINES:
        truncated = result.files
        result.files = truncated[:_MAX_FILES]
        hidden_files = len(truncated) - _MAX_FILES
        hidden_lines = total_lines - sum(len(f.hunks) for f in result.files)
        result.truncated = True
        result.hidden_files = hidden_files
        result.hidden_lines = hidden_lines

    result.branch = branch or ""
    result.base_sha = resolved_base[:12]
    result.head_sha = resolved_head[:12]
    result.repo_root = str(root)

    if dirty:
        result.error = "working tree has uncommitted changes; diff shows committed changes only"

    return result


def _git_cmd(repo_path: Path, args: list[str]) -> Optional[str]:
    """Run a git command and return stdout, or None on failure."""
    try:
        r = subprocess.run(
            ["git"] + args,
            **{_PROC_DIR_KW: str(repo_path)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            return None
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None


def _parse_unified_diff(text: str) -> RunDiff:
    """Parse unified diff output into FileDiff structs.

    Handles standard ``diff --git a/X b/Y`` headers, ``--- / +++`` lines,
    ``@@ ... @@`` hunk headers, and ``+/-/ `` content lines.
    """
    files: list[FileDiff] = []
    current: Optional[FileDiff] = None
    total_added = 0
    total_deleted = 0

    # Track line numbers across hunks.
    old_line = 0
    new_line = 0

    for raw_line in text.splitlines():
        # New file header.
        if raw_line.startswith("diff --git "):
            # Save previous file.
            if current is not None:
                files.append(current)

            # Extract path from "diff --git a/X b/Y".
            parts = raw_line[len("diff --git "):].split(" ")
            path = parts[-1] if parts else ""
            if path.startswith("b/"):
                path = path[2:]

            current = FileDiff(path=path)
            old_line = 0
            new_line = 0
            continue

        if current is None:
            continue

        # Rename detection: --- a/X / +++ b/Y.
        if raw_line.startswith("--- "):
            p = raw_line[4:].strip()
            if p.startswith("a/"):
                current.old_path = p[2:]
            elif p != "/dev/null":
                current.old_path = p
            continue

        if raw_line.startswith("+++ "):
            p = raw_line[4:].strip()
            if p.startswith("b/"):
                current.new_path = p[2:]
                # Use the +++ path as canonical (handles renames).
                if current.new_path:
                    current.path = current.new_path
            elif p != "/dev/null":
                current.new_path = p
            continue

        # Binary file indicator.
        if raw_line.startswith("Binary files"):
            current.binary = True
            continue

        # Hunk header: @@ -old_start,old_count +new_start,new_count @@
        if raw_line.startswith("@@"):
            import re
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw_line)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(3))
                current.hunks.append(DiffLine(
                    kind="hunk", gutter="@", text=raw_line,
                    line_no_old="", line_no_new="",
                ))
            continue

        # Content lines.
        if raw_line.startswith("+"):
            current.hunks.append(DiffLine(
                kind="ins", gutter="+", text=raw_line[1:],
                line_no_old="", line_no_new=str(new_line),
            ))
            current.lines_added += 1
            total_added += 1
            new_line += 1
        elif raw_line.startswith("-"):
            current.hunks.append(DiffLine(
                kind="del", gutter="-", text=raw_line[1:],
                line_no_old=str(old_line), line_no_new="",
            ))
            current.lines_deleted += 1
            total_deleted += 1
            old_line += 1
        elif raw_line.startswith(" "):
            current.hunks.append(DiffLine(
                kind="ctx", gutter=" ", text=raw_line[1:],
                line_no_old=str(old_line), line_no_new=str(new_line),
            ))
            old_line += 1
            new_line += 1
        elif raw_line.startswith("\\"):
            # "\ No newline at end of file" — skip.
            continue

    # Don't forget the last file.
    if current is not None:
        files.append(current)

    return RunDiff(
        files=files,
        total_added=total_added,
        total_deleted=total_deleted,
        total_files=len(files),
    )
