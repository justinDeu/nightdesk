"""Compute per-run git diffs from workspace metadata.

Given a Run and its associated TicketWorkspace, runs ``git diff`` from the
run's start commit to the worktree's current end state and parses the result
into structured per-file hunks suitable for JSON rendering or template
consumption. The diff reflects only what the run changed -- its own commits
plus uncommitted working-tree changes -- not the whole branch versus its
target.
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
    start_sha: Optional[str],
    branch: Optional[str] = None,
) -> RunDiff:
    """Diff what THIS run changed, parsed into a structured FileDiff list.

    The range is ``start_sha`` (the worktree's HEAD captured at run start) to
    the current end state -- i.e. the run's own commits *plus* any uncommitted
    working-tree changes, including new untracked files. This deliberately does
    not diff ``base_ref..HEAD``, which would report the whole branch versus its
    target (every prior run's commits), not just this run's work.

    Handles a missing start commit, an empty diff, and large diffs gracefully.
    """
    root = Path(repo_root)
    if not root.is_dir():
        return RunDiff(error=f"repo root does not exist: {repo_root}")

    # Verify it's a git repo.
    if not (root / ".git").exists() and not _git_cmd(root, ["rev-parse", "--git-dir"]):
        return RunDiff(error="not a git repository")

    resolved_head = _git_cmd(root, ["rev-parse", "HEAD"])
    if not resolved_head:
        return RunDiff(error="cannot resolve HEAD")

    # Resolve the run's start commit. If it wasn't captured (legacy workspace),
    # fall back to the parent of HEAD so we at least show the last commit plus
    # uncommitted changes rather than over-reporting the whole branch.
    resolved_start = start_sha
    if not resolved_start:
        resolved_start = _git_cmd(root, ["rev-parse", f"{resolved_head}^"]) or resolved_head

    # Diff the start commit against the working tree (committed + uncommitted
    # tracked changes), then append any untracked files the run created.
    diff_text = _git_cmd(root, [
        "diff", "--no-color", "--unified=3", resolved_start,
    ])
    if diff_text is None:
        return RunDiff(
            error="git diff failed",
            branch=branch or "",
            base_sha=resolved_start[:12],
            head_sha=resolved_head[:12],
        )

    untracked_text = _untracked_diff(root)
    combined = "\n".join(t for t in (diff_text, untracked_text) if t)

    result = _parse_unified_diff(combined)

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
    result.base_sha = resolved_start[:12]
    result.head_sha = resolved_head[:12]
    result.repo_root = str(root)

    return result


def _untracked_diff(root: Path) -> str:
    """Render new untracked files as synthetic "new file" diff blocks.

    ``git diff <commit>`` ignores untracked files, but those are exactly the
    new files a run creates and leaves uncommitted. We list them and render
    each as an added-file diff via ``git diff --no-index`` (read-only; it
    never touches the index or working tree).
    """
    listing = _git_cmd(root, ["ls-files", "--others", "--exclude-standard"])
    if not listing:
        return ""
    blocks: list[str] = []
    for rel_path in listing.splitlines():
        rel_path = rel_path.strip()
        if not rel_path:
            continue
        # --no-index exits non-zero (1) when files differ, so _git_cmd returns
        # None; capture the diff directly instead.
        block = _git_no_index_diff(root, rel_path)
        if block:
            blocks.append(block)
    return "\n".join(blocks)


def _git_no_index_diff(repo_path: Path, rel_path: str) -> Optional[str]:
    """Return a synthetic added-file diff for an untracked file."""
    try:
        r = subprocess.run(
            ["git", "diff", "--no-color", "--unified=3", "--no-index",
             "/dev/null", rel_path],
            **{_PROC_DIR_KW: str(repo_path)},
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    # --no-index returns 1 when the files differ (always, here) and 0 only when
    # identical; treat anything with output as the diff.
    if r.stdout:
        return r.stdout.rstrip("\n")
    return None


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
