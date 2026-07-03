from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """Raised when a workspace can't be prepared safely.

    The worker catches this in run_one, transitions the ticket to 'review',
    and (when a run row already exists) appends a ``worker_error`` event to
    the run's transcript so the user can see what's wrong (missing source path,
    invalid source path, unsupported mode, etc.).
    """


_SAFE_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class WorkspaceSpec:
    role: str
    label: str
    kind: str
    access: str = "read_write"
    source_path: Optional[str] = None
    worktree_name: Optional[str] = None
    worktree_path: Optional[str] = None
    branch: Optional[str] = None
    base_ref: Optional[str] = None
    retention: str = "preserve"


@dataclass
class Workspace:
    path: Path
    kind: str  # "directory" | "git_worktree"
    role: str = "primary"
    label: str = "primary"
    access: str = "read_write"
    source_path: Optional[Path] = None
    repo_path: Optional[Path] = None
    git_common_dir: Optional[Path] = None
    relative_path: Optional[Path] = None
    worktree_path: Optional[Path] = None
    branch: Optional[str] = None
    base_ref: Optional[str] = None
    base_sha: Optional[str] = None
    retention: str = "preserve"
    # Provision-time diagnostics (e.g. base_ref stacking warnings). Populated by
    # _create_git_worktree so the runner can surface them on the transcript.
    warnings: list[str] = field(default_factory=list)


@dataclass
class WorkspaceBundle:
    primary: Workspace
    workspaces: list[Workspace]

    @property
    def fs_write(self) -> list[Workspace]:
        return [w for w in self.workspaces if w.access == "read_write"]

    @property
    def fs_read(self) -> list[Workspace]:
        return [w for w in self.workspaces if w.access == "read_only"]


def _run_git(source_dir: Path, *args: str) -> str:
    cmd_args = ["git", "-C", str(source_dir), *args]
    log.debug("git %s (source_dir=%s)", " ".join(args), source_dir)
    try:
        result = subprocess.run(
            cmd_args,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        log.error("git %s failed for %s%s", " ".join(args), source_dir, detail)
        raise WorkspaceError(f"git {' '.join(args)} failed for {source_dir}{detail}") from exc
    return result.stdout.strip()

def _git_ref_exists(source_dir: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(source_dir), "show-ref", "--verify", "--quiet", ref],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0

def _resolve_git_path(source_dir: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = source_dir / path
    return path.resolve()


def _base_ref_stack_warning(source: GitSource, base_ref: Optional[str]) -> Optional[str]:
    """Warn when ``base_ref`` carries no commits beyond the source HEAD.

    This is the provision-time guard for ``base_ref`` stacking: a dependent
    ticket that provisions from a base_ref which hasn't advanced past HEAD
    silently receives an EMPTY prerequisite (the exact failure mode where a
    prerequisite's run left its work uncommitted). Returns a human-readable
    warning when the stack is empty, or None when base_ref does advance the
    tree or the check can't be determined.
    """
    if not base_ref or not source.base_sha:
        return None
    try:
        out = _run_git(
            source.command_dir, "rev-list", "--count",
            f"{source.base_sha}..{base_ref}",
        )
    except Exception:
        # Unresolvable ref / git error: don't block provisioning on the check.
        return None
    count = (out or "").strip()
    if count.isdigit() and int(count) == 0:
        return (
            f"base_ref {base_ref!r} has no commits beyond the repository HEAD "
            f"({source.base_sha[:8]}); this worktree was provisioned with none "
            f"of that ref's intended work. If {base_ref!r} points at another "
            f"ticket's branch, that ticket's run likely left its changes "
            f"uncommitted — enable commit_on_finish on the prerequisite "
            f"(or commit its branch manually) before the dependent provisions."
        )
    return None


def _safe_worktree_name(name: Optional[str], *, ticket_id: str) -> str:
    raw = (name or f"ticket-{ticket_id[:8]}").strip()
    if not _SAFE_WORKTREE_NAME.fullmatch(raw):
        raise WorkspaceError(
            "worktree_name must start with a letter or number and contain "
            "only letters, numbers, '.', '_' or '-'."
        )
    return raw


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False



@dataclass
class GitSource:
    repo_root: Path
    git_common_dir: Path
    command_dir: Path
    relative_path: Path
    base_sha: str
    bare_container: bool = False


def _bare_container_source(source_dir: Path) -> Optional[GitSource]:
    bare = source_dir / ".bare"
    git_file = source_dir / ".git"
    if not bare.is_dir() or not git_file.is_file():
        return None
    try:
        if "gitdir:" not in git_file.read_text():
            return None
    except OSError:
        return None
    try:
        is_bare = _run_git(bare, "rev-parse", "--is-bare-repository")
        if is_bare != "true":
            return None
        base_sha = _run_git(bare, "rev-parse", "HEAD")
    except WorkspaceError:
        return None
    return GitSource(
        repo_root=source_dir,
        git_common_dir=bare.resolve(),
        command_dir=bare.resolve(),
        relative_path=Path("."),
        base_sha=base_sha,
        bare_container=True,
    )

def _git_source(source_dir: Path) -> GitSource:
    bare_source = _bare_container_source(source_dir)
    if bare_source is not None:
        return bare_source
    repo_root = Path(_run_git(source_dir, "rev-parse", "--show-toplevel")).resolve()
    git_common = _resolve_git_path(source_dir, _run_git(source_dir, "rev-parse", "--git-common-dir"))
    base_sha = _run_git(source_dir, "rev-parse", "HEAD")
    try:
        relative = source_dir.resolve().relative_to(repo_root)
    except ValueError as exc:
        raise WorkspaceError(f"workspace source path is not inside git repo root: {source_dir}") from exc
    return GitSource(
        repo_root=repo_root,
        git_common_dir=git_common,
        command_dir=repo_root,
        relative_path=relative,
        base_sha=base_sha,
        bare_container=False,
    )


def _default_worktree_path(*, root: Path, repo_root: Path, git_common_dir: Path,
                           name: str) -> Path:
    if git_common_dir.name == ".bare" and git_common_dir.parent == repo_root:
        return repo_root / name
    if git_common_dir != repo_root / ".git" and git_common_dir.parent == repo_root.parent:
        return git_common_dir.parent / name
    return root / repo_root.name / name



def _fresh_worktree_target(base: Path, attempt: int) -> Path:
    if attempt <= 1:
        return base
    return base.parent / f"{base.name}-{attempt}"


def _fresh_worktree_name(base: str, attempt: int) -> str:
    if attempt <= 1:
        return base
    return f"{base}-{attempt}"


def _allocate_fresh_worktree_slot(*, root: Path, source: GitSource, base_name: str,
                                  explicit_path: Optional[str], branch: Optional[str]) -> tuple[str, Path, str]:
    for attempt in range(1, 10_000):
        candidate_name = _fresh_worktree_name(base_name, attempt)
        if explicit_path:
            candidate_target = _fresh_worktree_target(
                Path(explicit_path).expanduser().resolve(), attempt
            )
        else:
            candidate_target = _default_worktree_path(
                root=root,
                repo_root=source.repo_root,
                git_common_dir=source.git_common_dir,
                name=candidate_name,
            )
        branch_name = branch or f"nightdesk/{candidate_name}"
        if candidate_target.exists():
            continue
        if branch is None and _git_ref_exists(source.command_dir, f"refs/heads/{branch_name}"):
            continue
        return candidate_name, candidate_target, branch_name
    raise WorkspaceError("could not allocate a fresh worktree path")

def _create_git_worktree(*, ticket_id: str, root: Path, source_dir: Path,
                         worktree_name: Optional[str],
                         explicit_path: Optional[str],
                         branch: Optional[str], base_ref: Optional[str],
                         reuse_existing: bool = False,
                         fresh_if_exists: bool = False) -> Workspace:
    source = _git_source(source_dir)
    repo_root = source.repo_root
    git_common_dir = source.git_common_dir
    relative = source.relative_path

    # Provision-time guard for base_ref stacking: if base_ref hasn't advanced
    # past the source HEAD, the dependent gets an empty prerequisite. Surface a
    # warning (don't block) so the operator can enable commit_on_finish on the
    # prerequisite or commit its branch manually.
    provision_warnings: list[str] = []
    if base_ref:
        _bw = _base_ref_stack_warning(source, base_ref)
        if _bw:
            provision_warnings.append(_bw)
            log.warning("base_ref stacking risk for ticket %s: %s", ticket_id, _bw)

    log.info("creating git worktree for ticket %s: source_dir=%s repo=%s",
             ticket_id, source_dir, repo_root)

    # Slash-style names (e.g. "fix/tui-status") are valid git branch names but
    # not valid filesystem directory names. Treat them as the intended branch
    # (unless one was passed explicitly) and derive a safe directory name by
    # replacing '/' with '-'.
    if worktree_name and "/" in worktree_name:
        if branch is None:
            branch = worktree_name
        worktree_name = worktree_name.replace("/", "-")

    name = _safe_worktree_name(worktree_name, ticket_id=ticket_id)
    target = Path(explicit_path).expanduser() if explicit_path else _default_worktree_path(
        root=root,
        repo_root=repo_root,
        git_common_dir=git_common_dir,
        name=name,
    )
    branch_name = branch or f"nightdesk/{name}"
    if not target.is_absolute():
        raise WorkspaceError("worktree_path must be absolute")
    target = target.resolve()
    if _is_relative_to(target, repo_root) and not source.bare_container:
        raise WorkspaceError("worktree_path must not be inside the source repo")
    if target.exists():
        if reuse_existing:
            log.info("reusing existing worktree at %s for ticket %s", target, ticket_id)
            run_path = target / relative
            if not run_path.exists() or not run_path.is_dir():
                raise WorkspaceError(f"resolved worktree path does not exist: {run_path}")
            branch_name = branch or _run_git(target, "rev-parse", "--abbrev-ref", "HEAD")
            return Workspace(
                path=run_path,
                kind="git_worktree",
                source_path=source_dir,
                repo_path=source.command_dir,
                git_common_dir=git_common_dir,
                relative_path=relative,
                worktree_path=target,
                branch=branch_name,
                base_ref=base_ref,
                base_sha=source.base_sha,
                warnings=list(provision_warnings),
            )
        if not fresh_if_exists:
            log.error("worktree_path already exists for ticket %s: %s", ticket_id, target)
            raise WorkspaceError(f"worktree_path already exists: {target}")
        log.info("target %s exists for ticket %s; allocating fresh slot", target, ticket_id)
        name, target, branch_name = _allocate_fresh_worktree_slot(
            root=root,
            source=source,
            base_name=name,
            explicit_path=explicit_path,
            branch=branch,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["worktree", "add", "-b", branch_name, str(target)]
    if base_ref:
        cmd.append(base_ref)
    log.info("git worktree add branch=%s target=%s base=%s for ticket %s",
             branch_name, target, base_ref or "HEAD", ticket_id)
    _run_git(source.command_dir, *cmd)

    run_path = target / relative
    if not run_path.exists() or not run_path.is_dir():
        log.error("resolved worktree path does not exist after checkout: %s (ticket %s)",
                  run_path, ticket_id)
        subprocess.run(
            ["git", "-C", str(source.command_dir), "worktree", "remove", "--force", str(target)],
            check=False,
            capture_output=True,
        )
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise WorkspaceError(f"resolved worktree path does not exist: {run_path}")
    log.info("git worktree created successfully for ticket %s: path=%s branch=%s",
             ticket_id, run_path, branch_name)
    return Workspace(
        path=run_path,
        kind="git_worktree",
        source_path=source_dir,
        repo_path=source.command_dir,
        git_common_dir=git_common_dir,
        relative_path=relative,
        worktree_path=target,
        branch=branch_name,
        base_ref=base_ref,
        base_sha=source.base_sha,
        warnings=list(provision_warnings),
    )


def _normalize_kind(mode: str) -> str:
    if mode == "in_place":
        return "directory"
    if mode == "worktree":
        return "git_worktree"
    return mode


def prepare_workspace(
    *,
    ticket_id: str,
    root: Path,
    source_path: Optional[Path],
    mode: str = "directory",
    worktree_name: Optional[str] = None,
    worktree_path: Optional[str] = None,
    branch: Optional[str] = None,
    base_ref: Optional[str] = None,
    role: str = "primary",
    label: str = "primary",
    access: str = "read_write",
    retention: str = "preserve",
    reuse_existing: bool = False,
    fresh_if_exists: bool = False,
) -> Workspace:
    """Resolve the working directory the agent will run in.

    directory: agent's working directory is ``source_path`` directly.
    git_worktree: create a visible git worktree from ``source_path`` and run in
                  the equivalent relative path inside the worktree.
    scratch: not supported yet.
    """
    kind = _normalize_kind(mode)
    log.debug("prepare_workspace: ticket=%s source_path=%s mode=%s kind=%s",
              ticket_id, source_path, mode, kind)
    if access not in {"read_write", "read_only"}:
        raise WorkspaceError(f"unknown workspace access: {access!r}")

    if kind == "scratch":
        raise WorkspaceError("workspace kind 'scratch' is not supported yet")

    if source_path is None:
        raise WorkspaceError(
            "ticket has no primary workspace source_path. Set the primary "
            "workspace source path to the directory the agent should run in."
        )
    source_dir = Path(str(source_path).strip()).expanduser().resolve()
    if not source_dir.exists():
        raise WorkspaceError(f"workspace source path does not exist: {source_dir}")
    if not source_dir.is_dir():
        raise WorkspaceError(f"workspace source path is not a directory: {source_dir}")

    if kind == "directory":
        return Workspace(path=source_dir, kind="directory", role=role, label=label,
                         access=access, source_path=source_dir, retention=retention)

    if kind == "git_worktree":
        ws = _create_git_worktree(
            ticket_id=ticket_id,
            root=root,
            source_dir=source_dir,
            worktree_name=worktree_name,
            explicit_path=worktree_path,
            branch=branch,
            base_ref=base_ref,
            reuse_existing=reuse_existing,
            fresh_if_exists=fresh_if_exists,
        )
        ws.role = role
        ws.label = label
        ws.access = access
        ws.retention = retention
        return ws

    raise WorkspaceError(f"unknown workspace_mode: {mode!r}")


def prepare_workspace_bundle(*, ticket_id: str, root: Path,
                             specs: list[WorkspaceSpec],
                             reuse_existing_worktrees: bool = False,
                             fresh_worktree_paths: bool = False) -> WorkspaceBundle:
    log.info("preparing workspace bundle for ticket %s: %d workspace(s), "
             "reuse_existing=%s, fresh=%s",
             ticket_id, len(specs), reuse_existing_worktrees, fresh_worktree_paths)
    if not specs:
        raise WorkspaceError("at least one workspace is required")
    primary_count = sum(1 for spec in specs if spec.role == "primary")
    if primary_count != 1:
        raise WorkspaceError("one primary workspace is required")

    prepared: list[Workspace] = []
    try:
        for idx, spec in enumerate(specs):
            source = Path(spec.source_path).expanduser() if spec.source_path else None
            ws = prepare_workspace(
                ticket_id=ticket_id,
                root=root,
                source_path=source,
                mode=spec.kind,
                worktree_name=spec.worktree_name,
                worktree_path=spec.worktree_path,
                branch=spec.branch,
                base_ref=spec.base_ref,
                role=spec.role,
                label=spec.label or f"workspace-{idx + 1}",
                access=spec.access,
                retention=spec.retention,
                reuse_existing=reuse_existing_worktrees,
                fresh_if_exists=fresh_worktree_paths,
            )
            prepared.append(ws)
    except Exception:
        for ws in reversed(prepared):
            cleanup_workspace(ws)
        raise

    primary = next(w for w in prepared if w.role == "primary")
    return WorkspaceBundle(primary=primary, workspaces=prepared)


def cleanup_workspace(ws: Workspace) -> None:
    """No-op for user directories. Remove only Nightdesk-created workspaces."""
    if ws.kind == "git_worktree" and ws.repo_path is not None and ws.worktree_path is not None:
        log.info("cleaning up git worktree: %s (repo=%s)", ws.worktree_path, ws.repo_path)
        subprocess.run(
            ["git", "-C", str(ws.repo_path), "worktree", "remove", "--force", str(ws.worktree_path)],
            check=False,
            capture_output=True,
        )
        if ws.worktree_path.exists():
            shutil.rmtree(ws.worktree_path, ignore_errors=True)
    elif ws.kind == "scratch" and ws.path.exists():
        shutil.rmtree(ws.path, ignore_errors=True)
