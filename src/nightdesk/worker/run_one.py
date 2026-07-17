"""Single-ticket runner — the unit of execution invoked as a subprocess.

This module is the standalone equivalent of what ``WorkerLoop._run_ticket``
used to do inline. The daemon spawns ``nightdesk-run-ticket <id>`` per
pick so a crashing run can never take down the dispatcher: if the
subprocess segfaults, the daemon notices via the orphaned-Run sweep and
the user sees a 'review' ticket with a system comment.

Anything that needs to behave like the daemon (profile -> spec merge,
workspace handling, run-now bookkeeping, transcript schema, status transitions,
cancel signaling) lives here so the CLI and the daemon never drift.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import traceback
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import ConfigRow, Run, Ticket
from nightdesk.domain.conversations import (
    create_conversation, get_conversation, latest_turn, sync_conversation_from_turn,
)
from nightdesk.domain import backend_capabilities as bc
from nightdesk.domain import pricing
from nightdesk.domain.cost import compute_cost_from_snapshot, compute_cost_with_softened_fallback
from nightdesk.domain.model_assignment import compute_model_assignments
from nightdesk.domain.permissions import PermissionSpec, merge_permissions
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.providers import (
    ResolvedEndpoint,
    endpoint_compatible,
    resolve_endpoints_for_profile,
)
from nightdesk.domain.run_tokens import issue_run_token, revoke_for_run
from nightdesk.domain.notifications import (
    build_run_completion_payload,
    fire_webhook,
)
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.events import run_actor
from nightdesk.domain.tickets import transition_status
from nightdesk.domain.toolchains import resolve_tool_paths
from nightdesk.transcript import append_event, append_worker_error
from nightdesk.backends import Assignment, get_backend
from nightdesk.executors import ProvisionContext, RunContext, get_executor
from nightdesk.worker.executor import ExecutionResult, Executor
from nightdesk.worker.headless_prompt import HEADLESS_POLICY_VERSION
from nightdesk.worker.sandbox import SANDBOX_HOME
from nightdesk.worker.workspace import (
    Workspace, WorkspaceBundle, WorkspaceSpec, cleanup_workspace,
)


log = logging.getLogger(__name__)


def _maybe_fire_webhook(
    session: Session,
    *,
    ticket_id: str,
    run_id: str,
    exit_status: str,
    error_summary: Optional[str],
    base_url: str,
) -> None:
    """Fire a run-completion webhook if notify_webhook_url is configured."""
    cfg = session.get(ConfigRow, 1)
    url = getattr(cfg, "notify_webhook_url", None)
    if not url or not url.strip():
        return
    from nightdesk.db.models import Run as RunModel, Ticket as TicketModel
    run = session.get(RunModel, run_id)
    ticket = session.get(TicketModel, ticket_id)
    if run is None or ticket is None:
        return
    payload = build_run_completion_payload(
        ticket_id=ticket_id,
        title=ticket.title,
        run_id=run_id,
        exit_status=exit_status,
        error_summary=error_summary,
        cost_usd=run.cost_usd,
        started_at=run.started_at,
        finished_at=run.finished_at,
        base_url=base_url,
    )
    fire_webhook(url, payload)


def _resolve_extension_vendor(
    model: str,
    *,
    model_assignments: dict[str, Assignment],
    endpoints: dict[str, ResolvedEndpoint],
    primary: Optional[ResolvedEndpoint],
) -> str:
    """Vendor to stamp a snapshot extension for ``model`` under: the endpoint
    it was assigned to, else the primary endpoint's, else a best-effort guess
    from the model id itself.

    A profile with literally no endpoint at all (legacy, pre-providers/
    endpoints) gives us no vendor signal beyond the model id. Guessing
    ``"anthropic"`` unconditionally used to be fine when every such profile
    was an ambient Claude run, but a legacy profile can just as well point at
    a non-Anthropic compat endpoint via raw ``profile.env``/
    ``claude_credentials`` (e.g. z.ai/GLM) with no endpoint row to say so. A
    wrong ``"anthropic"`` guess is worse than no guess: ``resolve_vendor_price``'s
    ``"anthropic"`` branch only checks Anthropic-tagged live rows and the
    Anthropic bundled table, so it can never resolve a GLM id and the
    snapshot entry freezes at null rates forever (pricing_snapshot is stamped
    once and never re-derived). Only guess ``"anthropic"`` for an id that
    actually looks like a Claude model (``"claude-*"``); anything else is
    left as ``"unknown"`` so ``resolve_vendor_price``'s vendor-agnostic
    fallback -- which already searches the full bundled table, GLM rows
    included -- gets a chance instead of being shut out by a wrong guess.
    """
    vendor = next(
        (endpoints[a.endpoint_id].vendor for a in model_assignments.values()
         if a.model == model and a.endpoint_id in endpoints),
        None,
    )
    if vendor is None and primary is not None:
        vendor = primary.vendor
    if vendor:
        return vendor
    return "anthropic" if model.startswith("claude-") else "unknown"


def _extend_and_price_from_snapshot(
    run: Run,
    *,
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    model_assignments: dict[str, Assignment],
    endpoints: dict[str, ResolvedEndpoint],
    primary: Optional[ResolvedEndpoint],
    live_all: dict,
    live_source: str,
    usage_by_model: Optional[dict[str, dict[str, int]]] = None,
) -> None:
    """At run finish: extend ``run.pricing_snapshot`` for every actually-used
    model the launch-time stamp didn't cover, then reprice ``run.cost_usd``
    from the (possibly extended) snapshot.

    ``usage_by_model`` (opencode profiles that touched more than one model)
    takes priority over the single ``model``/token-count arguments when
    present and non-empty. Only in that genuine multi-model case is pricing
    softened via
    :func:`nightdesk.domain.cost.compute_cost_with_softened_fallback`: a
    model missing usable rates is priced at the run's primary model's rates
    when available, and logged. The single-model fallback (Claude Code, and
    opencode runs that reported no per-model breakdown) keeps the original
    strict contract -- an unpriceable model never silently borrows another
    model's rates, it just leaves the harness-reported cost in place.
    ``run.cost_usd`` is otherwise only left untouched when nothing at all is
    priceable. Mutates ``run`` in place; never raises (the caller wraps this
    so a pricing failure can't fail the run).
    """
    multi = bool(usage_by_model)
    usage_map = usage_by_model if multi else (
        {
            model: {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
            }
        } if model else None
    )
    if not usage_map:
        return

    snapshot = dict(run.pricing_snapshot or {})
    for used_model in usage_map:
        if used_model in snapshot:
            continue
        vendor = _resolve_extension_vendor(
            used_model, model_assignments=model_assignments,
            endpoints=endpoints, primary=primary,
        )
        extension = pricing.build_pricing_snapshot(
            {used_model: vendor}, live_all=live_all or None, live_source=live_source,
        )
        snapshot.update(extension)
    run.pricing_snapshot = snapshot

    if not multi:
        snapshot_cost = compute_cost_from_snapshot(usage_map, snapshot)
        if snapshot_cost is not None:
            run.cost_usd = snapshot_cost
        return

    primary_assignment = model_assignments.get("primary")
    primary_model = primary_assignment.model if primary_assignment is not None else model
    snapshot_cost, softened = compute_cost_with_softened_fallback(
        usage_map, snapshot, primary_model=primary_model,
    )
    if snapshot_cost is not None:
        run.cost_usd = snapshot_cost
        if softened:
            log.info(
                "run %s: priced models %s at primary model %r rates "
                "(no usable rate of their own)", run.id, softened, primary_model,
            )


@dataclass
class RunOneConfig:
    worktree_root: Path
    transcript_root: Path
    secrets: dict[str, str]
    host: str
    bearer_token: str = ""
    api_url: str = "http://127.0.0.1:8765"
    # Optional override (test seam); defaults to dispatching via the resolved
    # backend's ``execute()`` (see ``nightdesk.backends.get_backend``).
    executor: Optional[Executor] = None
    # Vendor-tagged pricing chain inputs for run pricing snapshots (see
    # ``domain/pricing.py``, ``resolve_live_all``). Left unset (None) in tests
    # and any deployment that hasn't opted in: the snapshot then resolves
    # purely from the bundled table, no network hit. Production wires these
    # from ``NightdeskConfig.data_dir`` / ``.pricing_url``.
    data_dir: Optional[Path] = None
    pricing_url: Optional[str] = None


def _profile_to_spec(
    ticket: Ticket,
    *,
    secret_box: Optional[ProfileSecretBox] = None,
    default_claude_binary: Optional[str] = None,
) -> PermissionSpec:
    p = ticket.profile
    claude_credentials: Optional[dict] = None
    custom_env: dict = {}
    if secret_box is not None:
        if getattr(p, "claude_credentials", None):
            try:
                claude_credentials = secret_box.decrypt(p.claude_credentials)
            except ValueError as exc:
                log.warning("profile %s claude_credentials unreadable: %s", p.id, exc)
        if (
            isinstance(claude_credentials, dict)
            and claude_credentials.get("source") == "inherit"
            and not claude_credentials.get("value")
        ):
            claude_credentials["value"] = os.path.expanduser("~/.claude/.credentials.json")
        if getattr(p, "env", None):
            try:
                decoded = secret_box.decrypt(p.env) or {}
                if isinstance(decoded, dict):
                    custom_env = {str(k): str(v) for k, v in decoded.items()}
            except ValueError as exc:
                log.warning("profile %s env unreadable: %s", p.id, exc)
    base = PermissionSpec(
        fs_read=list(p.fs_read), fs_write=list(p.fs_write),
        allowed_tools=list(p.allowed_tools), denied_tools=list(p.denied_tools),
        network_mode=p.network_mode, network_allowlist=list(p.network_allowlist),
        secret_keys=list(p.secret_keys), default_model=p.default_model,
        backend=getattr(p, "backend", None) or "claude_sdk",
        backend_config=dict(getattr(p, "backend_config", None) or {}),
        claude_credentials=claude_credentials,
        custom_env=custom_env,
        claude_binary_path=getattr(p, "claude_binary_path", None) or default_claude_binary,
        permission_mode=getattr(p, "permission_mode", None),
        system_prompt=getattr(p, "system_prompt", None),
    )
    spec = merge_permissions(base, ticket.permission_overrides)
    return spec

def _workspace_specs_for_ticket(ticket: Ticket) -> list[WorkspaceSpec]:
    if not getattr(ticket, "workspaces", None):
        raise RuntimeError("ticket has no primary workspace")
    specs = [
        WorkspaceSpec(
            role=w.role,
            label=w.label,
            kind=w.kind,
            access=w.access,
            source_path=w.source_path,
            worktree_name=w.worktree_name,
            worktree_path=w.worktree_path,
            branch=w.branch,
            base_ref=w.base_ref,
            retention=w.retention,
        )
        for w in ticket.workspaces
    ]
    for idx, entry in enumerate(ticket.additional_dirs or []):
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        mode = entry.get("mode", "rw")
        if not isinstance(path, str) or not path:
            continue
        specs.append(WorkspaceSpec(
            role="linked",
            label=f"additional-{idx + 1}",
            kind="directory",
            access="read_write" if mode == "rw" else "read_only",
            source_path=path,
        ))
    return specs


def _dedup_append(target: list[str], path: str) -> None:
    if path not in target:
        target.append(path)


def _git_metadata_dirs(bundle: WorkspaceBundle) -> list[str]:
    """Host paths to the git metadata each git_worktree needs mounted.

    For a git worktree the working dir's ``.git`` is a file pointing at
    ``git_common_dir/worktrees/<name>`` outside the working dir; the bare/
    ``.bare`` layout resolves ``git_common_dir`` to the ``.bare`` dir. Binding
    ``git_common_dir`` read-write covers both (the per-worktree dir lives under
    it), so git operations work in the sandbox without a hand-added workspace.
    """
    dirs: list[str] = []
    for w in bundle.workspaces:
        if w.kind == "git_worktree" and w.git_common_dir:
            _dedup_append(dirs, str(w.git_common_dir))
    return dirs


def _apply_workspace_permissions(spec: PermissionSpec,
                                 bundle: WorkspaceBundle) -> PermissionSpec:
    for ws in bundle.fs_write:
        _dedup_append(spec.fs_write, str(ws.path))
    for ws in bundle.fs_read:
        _dedup_append(spec.fs_read, str(ws.path))
    return spec


def _capture_head_sha(path: str) -> Optional[str]:
    """Return the current HEAD commit SHA of the git tree at ``path``.

    Best-effort: returns None for non-git paths or any git failure.
    """
    try:
        r = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def _has_working_tree_changes(path: str) -> bool:
    """True if the git tree at ``path`` has any staged or unstaged changes."""
    try:
        r = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _commit_workspace_changes(
    ws: Workspace, *, ticket_id: str, run_id: str, run_intent: str,
) -> Optional[str]:
    """Best-effort: stage + commit a git_worktree workspace's working-tree
    changes onto its branch. Returns the new commit SHA, or None if there was
    nothing to commit, the workspace isn't a committable git worktree, or git
    failed.

    This is the commit_on_finish path that makes ``base_ref`` stacking work:
    without it a run leaves its work uncommitted, the branch ref never advances
    past the base commit, and any dependent ticket that provisions from this
    branch (via ``base_ref``) gets an empty prerequisite. Never raises — a
    commit hiccup must not fail an otherwise-successful run.
    """
    if ws.kind != "git_worktree" or not ws.branch or not ws.path:
        return None
    path = str(ws.path)
    if not _has_working_tree_changes(path):
        log.info("commit_on_finish: nothing to commit for ticket %s (run %s)",
                 ticket_id, run_id)
        return None
    try:
        add = subprocess.run(
            ["git", "-C", path, "add", "-A"],
            capture_output=True, text=True, timeout=120,
        )
        if add.returncode != 0:
            log.warning("commit_on_finish: git add failed for ticket %s: %s",
                        ticket_id, (add.stderr or "").strip())
            return None
        message = (f"nightdesk: auto-commit for ticket {ticket_id[:8]} "
                   f"(run {run_id[:8]}, intent={run_intent})")
        commit = subprocess.run(
            ["git", "-c", "user.email=nightdesk@worker", "-c", "user.name=nightdesk",
             "-C", path, "commit", "-m", message],
            capture_output=True, text=True, timeout=120,
        )
        if commit.returncode != 0:
            out = (commit.stdout or "") + (commit.stderr or "")
            log.warning("commit_on_finish: git commit failed for ticket %s: %s",
                        ticket_id, out.strip())
            return None
        sha = _capture_head_sha(path)
        if sha:
            log.info("commit_on_finish: committed %s on branch %s for ticket %s (run %s)",
                     sha[:8], ws.branch, ticket_id, run_id)
        return sha
    except (OSError, subprocess.SubprocessError):
        log.exception("commit_on_finish: git error for ticket %s", ticket_id)
        return None


def _record_workspace_resolution(ticket: Ticket, bundle: WorkspaceBundle) -> None:
    if not getattr(ticket, "workspaces", None):
        return
    for row, ws in zip(ticket.workspaces, bundle.workspaces):
        row.kind = ws.kind
        row.access = ws.access
        row.source_path = str(ws.source_path) if ws.source_path else None
        row.resolved_path = str(ws.path)
        row.repo_root = str(ws.repo_path) if ws.repo_path else None
        row.git_common_dir = str(ws.git_common_dir) if ws.git_common_dir else None
        row.relative_path = str(ws.relative_path) if ws.relative_path else None
        row.worktree_path = str(ws.worktree_path) if ws.worktree_path else None
        row.branch = ws.branch
        row.base_ref = ws.base_ref
        row.base_sha = ws.base_sha
        # Snapshot the worktree's HEAD now, before the agent does any work, so
        # the run diff can report start-commit..end-state for THIS run only.
        row.run_start_sha = _capture_head_sha(str(ws.path))
        row.retention = ws.retention
        row.state = "active"



def _record_reported_workspaces(
    ticket: Ticket, conversation_id: str, run: Run, reported: list,
) -> None:
    """Persist a remote executor's reported workspace resolution.

    The host never provisions a k8s run's tree, so ``_record_workspace_resolution``
    (which reads a host bundle) can't run. Instead the pod reports what it
    resolved — branch, base/head SHAs — via ``ExecutionOutcome.workspaces``; we
    write those onto the ticket's workspace rows (primary first) so the Changes
    view and base_ref stacking see real refs. The run diff itself comes from the
    pod-uploaded sidecar (``diff_sidecar_path``), so ``resolved_path`` stays
    unset here. Best-effort: never fails the run.
    """
    rows = getattr(ticket, "workspaces", None) or []
    for row, rw in zip(rows, reported):
        if rw.kind is not None:
            row.kind = rw.kind
        if rw.access is not None:
            row.access = rw.access
        if rw.source_path is not None:
            row.source_path = rw.source_path
        row.conversation_id = row.conversation_id or conversation_id
        row.run_id = row.run_id or run.id
        row.branch = rw.branch or row.branch
        row.base_ref = rw.base_ref or row.base_ref
        row.base_sha = rw.base_sha or row.base_sha
        row.head_sha = rw.head_sha or row.head_sha
        row.run_start_sha = rw.run_start_sha or row.run_start_sha
        if rw.retention is not None:
            row.retention = rw.retention
        row.state = rw.state or row.state


def _capture_fs_snapshots(
    ticket: Ticket, bundle: WorkspaceBundle, run_id: str, transcript_root: Path,
) -> None:
    """Snapshot non-git workspace trees at run start for the filesystem diff.

    Git workspaces capture ``run_start_sha`` instead (see
    ``_record_workspace_resolution``); they don't need a snapshot. For every
    ``directory`` (non-git) workspace, persist a JSON sidecar of the current
    tree so review can diff added/modified/deleted files against it. Best
    effort: a snapshot failure must never abort the run.
    """
    from nightdesk.domain.fs_snapshot import (
        snapshot_sidecar_path, snapshot_tree, write_snapshot,
    )
    rows = getattr(ticket, "workspaces", None) or []
    for row, ws in zip(rows, bundle.workspaces):
        if ws.kind == "git_worktree":
            continue
        try:
            snap = snapshot_tree(str(ws.path))
            write_snapshot(
                snapshot_sidecar_path(transcript_root, run_id, row.id), snap,
            )
        except Exception:
            log.exception(
                "failed to capture filesystem snapshot for workspace %s (run %s)",
                row.id, run_id,
            )


def _cleanup_recorded_worktrees(ticket: Ticket, *, delete_branches: bool = False) -> None:
    for row in getattr(ticket, "workspaces", []) or []:
        if row.kind != "git_worktree" or not row.worktree_path or not row.repo_root:
            continue
        cleanup_workspace(Workspace(
            path=Path(row.resolved_path or row.worktree_path),
            kind="git_worktree",
            repo_path=Path(row.repo_root),
            worktree_path=Path(row.worktree_path),
        ))
        if delete_branches and row.branch:
            subprocess.run(
                ["git", "-C", row.repo_root, "branch", "-D", row.branch],
                check=False,
                capture_output=True,
            )


_BASE_ENV_KEYS = ("HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/bin:/bin"


def _resolve_default_claude_binary(session: Session) -> Optional[str]:
    cfg_row = session.get(ConfigRow, 1)
    path = getattr(cfg_row, "claude_binary_path", None)
    if path:
        return path
    import shutil as _shutil
    return _shutil.which("claude")


def _build_env(
    spec: PermissionSpec,
    secrets: dict[str, str],
    *,
    run_token: Optional[str] = None,
    run_id: Optional[str] = None,
    ticket_id: Optional[str] = None,
    api_url: Optional[str] = None,
) -> dict[str, str]:
    # HOME is forced to the sandbox tmpfs so CC writes its state there, not
    # into a stale read-only mount. The claude binary is passed to the SDK by
    # explicit `cli_path` (see claude_executor._build_runner_spec), so PATH
    # does not need to find it.
    tool_path = os.pathsep.join(getattr(spec, "tool_paths", None) or [])
    path = f"{tool_path}{os.pathsep}{_DEFAULT_PATH}" if tool_path else _DEFAULT_PATH
    env: dict[str, str] = {
        "PATH": path,
        "HOME": SANDBOX_HOME,
        "USER": "sandboxed",
        "LOGNAME": "sandboxed",
        "SHELL": "/bin/sh",
        "CLAUDE_CONFIG_DIR": f"{SANDBOX_HOME}/.claude",
        "XDG_CONFIG_HOME": f"{SANDBOX_HOME}/.config",
        "XDG_DATA_HOME": f"{SANDBOX_HOME}/.local/share",
        "XDG_CACHE_HOME": f"{SANDBOX_HOME}/.cache",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": os.environ.get("TERM", "dumb"),
    }

    # Profile credentials -> CC auth env vars. `inherit` is handled at the
    # bwrap mount layer; api_key / auth_token feed env here. `base_url` is
    # optional and rides along with whichever source is selected.
    creds = getattr(spec, "claude_credentials", None) or {}
    source = creds.get("source")
    if source == "api_key":
        value = creds.get("value")
        if value:
            env["ANTHROPIC_API_KEY"] = str(value)
    elif source == "auth_token":
        value = creds.get("value")
        if value:
            env["ANTHROPIC_AUTH_TOKEN"] = str(value)
    base_url = creds.get("base_url")
    if base_url:
        env["ANTHROPIC_BASE_URL"] = str(base_url)

    # Profile.env: arbitrary env vars set on the profile. Applied after the
    # auth section so explicit values can still override CC env defaults.
    for key, value in (getattr(spec, "custom_env", None) or {}).items():
        env[str(key)] = str(value)

    # Scoped run token and ND callback metadata.
    if run_token:
        env["NIGHTDESK_RUN_TOKEN"] = run_token
    if run_id:
        env["NIGHTDESK_RUN_ID"] = run_id
    if ticket_id:
        env["NIGHTDESK_TICKET_ID"] = ticket_id
    if api_url:
        env["NIGHTDESK_API_URL"] = api_url

    # Forward the host ssh-agent so sandboxed `git push`/`fetch` over SSH
    # (e.g. a self-hosted GitLab) can authenticate. The private key never
    # enters the sandbox — only the agent socket does (the sandbox layer
    # bind-mounts the socket and a read-only known_hosts, and ssh inside the
    # sandbox talks to the agent). SSH_AGENT_PID is deliberately NOT forwarded:
    # the sandbox has its own PID namespace, so a host PID is meaningless inside
    # and ssh only needs the socket.
    ssh_sock = os.environ.get("SSH_AUTH_SOCK")
    if ssh_sock:
        env["SSH_AUTH_SOCK"] = ssh_sock
        # Fail closed. The read-only known_hosts mounted into the sandbox acts
        # as an ALLOWLIST of the hosts the agent may reach over SSH:
        # StrictHostKeyChecking=yes refuses any host not already pinned (and any
        # changed key), so a runaway or prompt-injected agent cannot silently
        # push to / pull from an attacker-controlled host. BatchMode=yes removes
        # the interactive prompt surface, so failures are fast and clean instead
        # of hanging a worker slot. Respect a caller-supplied GIT_SSH_COMMAND.
        env.setdefault(
            "GIT_SSH_COMMAND",
            "ssh -o StrictHostKeyChecking=yes -o BatchMode=yes",
        )
    return env


def _make_session_id_persister(
    session_factory: Callable[[], Session], run_id: str, conversation_id: str,
) -> Callable[[str], None]:
    """Build the eager on_session_id callback for the executor.

    Persists the authoritative ``conversation.session_id`` (and the per-turn
    ``run.session_id``) the instant the SDK emits a session id — OUT OF BAND
    relative to run completion — on a fresh session. Best-effort: a failure
    here must never abort the run; it only means session_id lands at finish.
    """
    from nightdesk.domain.conversations import set_conversation_session

    def _persist(sid: str) -> None:
        s = session_factory()
        try:
            r = s.get(Run, run_id)
            if r is not None and not r.session_id:
                r.session_id = sid
            set_conversation_session(s, conversation_id, sid)
            s.commit()
        except Exception:
            log.exception("eager session_id persist failed for run %s", run_id)
            try:
                s.rollback()
            except Exception:
                pass
        finally:
            s.close()

    return _persist


def _make_steer_delivered_callback(
    session_factory: Callable[[], Session], run_id: str,
) -> Callable[[str, dict], None]:
    """Build the on_steer_delivered callback for an inject-capable executor.

    Marks a delivered SteerMessage's DB row (``delivering -> delivered``) the
    moment the backend confirms it POSTed the follow-up into the live run —
    OUT OF BAND on its own session. It does NOT write the transcript event; the
    executor owns that (seq single-ownership). Best-effort: a failure here only
    means the row stays ``delivering`` and gets folded into next_run_context by
    the run-completion drain instead of showing as delivered."""
    from nightdesk.domain.steering import mark_delivered

    def _mark(message_id: str, _info: dict) -> None:
        s = session_factory()
        try:
            mark_delivered(s, message_id, run_id=run_id)
        except Exception:
            log.exception("steer mark_delivered failed for message %s", message_id)
            try:
                s.rollback()
            except Exception:
                pass
        finally:
            s.close()

    return _mark


def _steer_drain_and_autocontinue(
    session: Session, *, ticket_id: str, conversation_id: str, exit_status: str,
) -> bool:
    """Run-completion drain for mid-run steering.

    Folds any still-queued (pending/delivering) follow-ups into the ticket's
    ``next_run_context`` and, when the run finished cleanly on a resumable
    conversation, auto-issues a ``continue`` so those follow-ups actually drive
    the next turn — via the tested ``continue_ticket`` path, with the drained
    text surfaced as the continue's ``continue_message``.

    Returns True when it staged a continue (the caller must then SKIP the normal
    review transition / webhook / handoff / cleanup, because the ticket is now
    queued for its next turn rather than done). Returns False when there was
    nothing queued, the user cancelled, or the conversation is not resumable —
    in the non-resumable case the drained text still lands as the visible
    "Guidance staged" chip via ``next_run_context``, and the caller falls
    through to review as usual. Nothing is ever silently dropped."""
    from nightdesk.domain.steering import list_steer_messages, drain_pending_to_context
    from nightdesk.domain.conversations import get_conversation
    from nightdesk.domain.tickets import continue_ticket

    if list_steer_messages(session, conversation_id) == []:
        return False
    drain_pending_to_context(session, ticket_id, conversation_id)
    # Cancel wins: the user explicitly stopped, so never auto-continue. The
    # drained text stays on next_run_context for the user to run when ready.
    if exit_status == "cancelled":
        return False
    t = session.get(Ticket, ticket_id)
    if t is None or t.status != "running":
        # A concurrent transition (e.g. a cancel that raced completion) already
        # moved the ticket; don't auto-continue on top of that.
        return False
    conv = get_conversation(session, conversation_id)
    if conv is None or not conv.session_id:
        # Non-resumable (its first turn never recorded a session): fall through
        # to review with next_run_context populated — one click runs it.
        return False
    transition_status(session, ticket_id, "review", actor=run_actor(t.current_run_id))
    continue_ticket(session, ticket_id, next_run_context=None,
                    conversation_id=conversation_id)
    log.info("ticket %s: drained queued steer messages into an auto-continue turn",
             ticket_id)
    return True


def _handoff_to_dependents(session: Session, ticket_id: str, run: Run) -> None:
    """After a successful run, push a summary into each dependent ticket's
    next_run_context so the downstream stage sees what happened upstream."""
    from nightdesk.db.models import TicketDependency
    from nightdesk.domain.tickets import set_next_run_context, get_ticket

    dep_rows = session.scalars(
        select(TicketDependency).where(
            TicketDependency.depends_on_id == ticket_id,
        )
    )
    upstream = get_ticket(session, ticket_id)
    summary_parts = [
        f"[Upstream ticket: {upstream.title}]",
        f"Status: success",
        f"Run ID: {run.id[:8]}",
    ]
    if run.model_used:
        summary_parts.append(f"Model: {run.model_used}")
    if run.cost_usd is not None:
        summary_parts.append(f"Cost: ${run.cost_usd:.4f}")
    if run.error_summary:
        summary_parts.append(f"Summary: {run.error_summary}")
    summary = "\n".join(summary_parts)

    for dep in dep_rows:
        try:
            downstream = get_ticket(session, dep.ticket_id)
            existing = downstream.next_run_context or ""
            context = existing + "\n\n" + summary if existing else summary
            set_next_run_context(session, downstream.id, context.strip())
            log.info("handed off context from ticket %s to dependent %s",
                     ticket_id, downstream.id)
        except Exception:
            log.exception("failed to hand off context to dependent %s",
                          dep.ticket_id)


async def run_one(
    session_factory: Callable[[], Session],
    cfg: RunOneConfig,
    ticket_id: str,
) -> ExecutionResult:
    """Execute exactly one ticket end-to-end. Same flow the daemon used to
    run in-process. Suitable for invocation as a subprocess.
    """
    session = session_factory()
    cancel_event = asyncio.Event()
    ws: Optional[Workspace] = None
    bundle: Optional[WorkspaceBundle] = None
    issued_token = None
    run_log_handler: Optional[logging.Handler] = None
    secret_box = ProfileSecretBox(cfg.bearer_token) if cfg.bearer_token else None
    default_claude_binary = _resolve_default_claude_binary(session)
    run: Optional[Run] = None

    # Install a SIGTERM handler so the daemon (or the user via kill) can
    # gracefully stop the run. The handler sets cancel_event, which the
    # watcher and the executor's wait both observe.
    def _on_sigterm(*_):
        cancel_event.set()
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        # Happens in non-main threads; not relevant when invoked from CLI.
        pass

    try:
        try:
            # Track setup phase so the outer except can identify which step failed.
            _setup_phase = "ticket_lookup"
            ticket = session.get(Ticket, ticket_id)
            if ticket is None:
                log.error("ticket %s not found", ticket_id)
                return ExecutionResult(exit_status="failed",
                                       error_summary="ticket not found")
            overrides = dict(ticket.permission_overrides or {})
            run_intent = str(overrides.get("nightdesk_run_intent") or "first_run")
            parent_run_id = overrides.get("nightdesk_parent_run_id")
            restart_workspace_policy = overrides.get("nightdesk_restart_workspace_policy")
            staged_conversation_id = overrides.get("nightdesk_conversation_id")
            # nightdesk_new_conversation is explicit documentation of a fresh
            # conversation; the default (no staging) also starts a new one.
            next_run_context = ticket.next_run_context
            workspace_specs = _workspace_specs_for_ticket(ticket)
            cfg.transcript_root.mkdir(parents=True, exist_ok=True)

            # Capture whether a prior turn/worktree exists BEFORE we create the
            # new run row (early turn creation below sets current_run_id).
            had_prior_turn = ticket.current_run_id is not None

            # --- Resolve / create the Conversation (BEFORE workspace prep) ----
            # A turn always belongs to a conversation. Continue targets an
            # existing conversation (re-activating it); everything else starts a
            # fresh conversation (new session by definition).
            _setup_phase = "conversation_resolve"
            if staged_conversation_id:
                conversation = get_conversation(session, staged_conversation_id)
                if conversation.ticket_id != ticket.id:
                    raise RuntimeError("staged conversation does not belong to ticket")
                ticket.current_conversation_id = conversation.id
            else:
                profile = ticket.profile
                backend = (getattr(profile, "backend", None) or "claude_sdk") if profile else "claude_sdk"
                conversation = create_conversation(
                    session,
                    ticket_id=ticket.id,
                    profile_id=ticket.profile_id,
                    backend=backend,
                    transcript_path="",  # placeholder; set once we have the id
                )
                conversation.transcript_path = str(
                    cfg.transcript_root / f"{conversation.id}.log"
                )
                ticket.current_conversation_id = conversation.id
                session.flush()
            transcript_path = conversation.transcript_path
            # The prior turn of THIS conversation — the resume seeding source
            # and the last_run_summary reference. (parent_run is kept only as a
            # seeding fallback for legacy data.)
            prior_turn = latest_turn(session, conversation.id)
            parent_run = session.get(Run, parent_run_id) if isinstance(parent_run_id, str) else None
            if prior_turn is None and parent_run is not None:
                prior_turn = parent_run

            # A ticket in review/archived/queued may already have a resolved
            # worktree from a prior run. If the intent is first_run and any
            # workspace is a git_worktree, reuse it instead of failing with
            # "worktree_path already exists".
            has_existing_worktree = (
                run_intent == "first_run"
                and any(s.kind == "git_worktree" for s in workspace_specs)
                and had_prior_turn
            )
            if run_intent == "restart":
                for spec in workspace_specs:
                    if spec.kind == "git_worktree":
                        spec.branch = None
                if restart_workspace_policy == "recreate_in_place":
                    _cleanup_recorded_worktrees(ticket, delete_branches=True)

            # --- Create the turn (Run) row BEFORE workspace prep ------------
            # Closing the 30s debounce race: the row existing means orphan
            # recovery's "running + no unfinished Run" pass never fires while
            # the subprocess is still in workspace prep (which can take longer
            # than the grace window). worktree_path is resolved after prep.
            _setup_phase = "turn_create"
            started_as_run_now = bool(ticket.run_now)
            run_id = str(uuid.uuid4())
            run = start_run(
                session,
                id=run_id,
                ticket_id=ticket.id,
                worktree_path="",  # placeholder; set after workspace prep
                transcript_path=transcript_path,
                pid=os.getpid(),
                host=cfg.host,
                started_as_run_now=started_as_run_now,
                intent=run_intent,
                parent_run_id=parent_run.id if parent_run is not None else None,
                conversation_id=conversation.id,
                headless_policy_version=HEADLESS_POLICY_VERSION,
                restart_workspace_policy=restart_workspace_policy,
                prompt=ticket.prompt,
            )
            log.info("turn %s started (conversation %s) for ticket %s: intent=%s",
                     run.id, conversation.id, ticket.id, run_intent)

            # Select the execution target once (no ``if k8s`` anywhere below).
            # 'local' is the default and only target until a profile opts into
            # 'k8s'; the registry raises a clear error for an unconfigured k8s.
            execution_target = (
                getattr(ticket.profile, "execution_target", None) or "local"
            )
            executor = get_executor(execution_target)

            log.debug("provisioning workspace for ticket %s (target=%s)",
                      ticket.id, execution_target)
            _setup_phase = "workspace_prep"
            prov = await executor.provision(ProvisionContext(
                run_id=run.id,
                ticket_id=ticket.id,
                worktree_root=cfg.worktree_root,
                specs=workspace_specs,
                run_intent=run_intent,
                # resume/retry/continue all reuse the existing worktree (continue
                # resumes the prior SDK conversation on the same worktree).
                reuse_existing_worktrees=run_intent in {"resume", "retry", "continue"} or has_existing_worktree,
                fresh_worktree_paths=run_intent == "restart" and restart_workspace_policy == "fresh_path",
                transcript_path=run.transcript_path,
                api_url=cfg.api_url,
                session_factory=session_factory,
            ))
            # ``bundle`` is present for on-host executors (LocalExecutor) and
            # None for remote ones (K8sExecutor provisions in-pod). Every
            # host-side workspace record below is guarded on it so the local
            # path is byte-identical and the k8s path skips host prep entirely
            # (the pod reports its resolved workspace back via ExecutionOutcome).
            bundle = prov.bundle
            ws = bundle.primary if bundle is not None else None
            if bundle is not None:
                run.worktree_path = str(ws.worktree_path or ws.path)
                _record_workspace_resolution(ticket, bundle)
            # Bind this conversation's workspaces to it (1:N conversation
            # ownership) so continuing an OLD conversation restores the tree it
            # ran against, not one a newer conversation mutated.
            for row in (ticket.workspaces or []):
                if row.conversation_id is None:
                    row.conversation_id = conversation.id
            # Surface provision-time workspace warnings (e.g. an empty base_ref
            # stack where a prerequisite's work was never committed) on the
            # transcript so the operator can see them.
            for _pws in (bundle.workspaces if bundle is not None else []):
                for _pw in (_pws.warnings or []):
                    try:
                        append_event(run.transcript_path, {
                            "type": "system",
                            "subtype": "provision_warning",
                            "data": {"workspace": str(_pws.path), "message": _pw},
                        })
                    except Exception:
                        log.exception("could not record provision warning for run %s",
                                      run.id)
            _setup_phase = "profile_spec"
            spec = _profile_to_spec(
                ticket,
                secret_box=secret_box,
                default_claude_binary=default_claude_binary,
            )
            schedule_cfg = session.get(ConfigRow, 1)
            if bundle is not None:
                # Host workspace: fold the bundle's fs mounts into the spec and
                # resolve toolchain binary paths against the resolved tree. For
                # a remote (k8s) run these are pod-side concerns — the pod is the
                # sandbox and resolves its own toolchains — so both are skipped.
                spec = _apply_workspace_permissions(spec, bundle)
                tool_paths = resolve_tool_paths(
                    ticket=ticket,
                    config=schedule_cfg,
                    base_path=str(ws.path),
                )
                spec = replace(spec, tool_paths=tool_paths)
                run.sandbox_tool_paths = tool_paths
            session.commit()

            # ``run_now`` is set only by an explicit user queue-bypass
            # (request_run_now/set_run_now/drag-to-running). The queued->running
            # transition no longer mutates it, so this flag accurately reflects
            # whether the user ran-now; normal scheduler picks read False here.
            if ticket.run_now:
                ticket.run_now = False
            for _k in ("nightdesk_run_intent", "nightdesk_parent_run_id",
                       "nightdesk_restart_workspace_policy",
                       "nightdesk_conversation_id", "nightdesk_new_conversation"):
                overrides.pop(_k, None)
            ticket.permission_overrides = overrides or None
            ticket.next_run_context = None
            ticket.next_run_context_updated_at = None

            # Snapshot non-git workspace trees now, before the agent touches
            # anything, so the per-run Changes view can diff filesystem state
            # for directory workspaces. Keyed to this run + workspace. Only
            # host-provisioned runs have a bundle to snapshot; a k8s run uploads
            # its own diff sidecar from the pod at finish.
            if bundle is not None:
                _capture_fs_snapshots(ticket, bundle, run.id, cfg.transcript_root)

            # Resolve scheduling knobs from the config table (live values).
            max_duration = getattr(schedule_cfg, "max_run_duration_seconds", 7200) or 7200
            token_grace = getattr(schedule_cfg, "run_token_grace_seconds", 300) or 300
            extra_scopes = list(ticket.profile.run_token_scopes or [])
            issued_token = issue_run_token(
                session,
                run_id=run.id,
                ticket_id=ticket.id,
                extra_scopes=extra_scopes,
                max_run_duration_seconds=max_duration,
                grace_seconds=token_grace,
            )

            # Attach a per-run log handler so the user can download the
            # full worker log for this run from the UI. Detached in the
            # outer finally.
            from nightdesk.logging_setup import per_run_log_handler
            run_log_handler = per_run_log_handler(run.id)
            logging.getLogger().addHandler(run_log_handler)
            log.info("run %s starting for ticket %s (intent=%s)", run.id, ticket.id, run_intent)

            # Resolve the backend and let it compose the sandboxed launch. The
            # worker stays backend-agnostic: no `if backend == ...` here.
            _setup_phase = "endpoint_resolution"
            backend = get_backend(spec.backend)
            profile = ticket.profile
            endpoints = resolve_endpoints_for_profile(session, profile, secret_box)
            primary_endpoint_id = getattr(profile, "endpoint_id", None)
            primary = endpoints.get(primary_endpoint_id) if primary_endpoint_id else None

            # Compatibility gate: every resolved endpoint (primary and any
            # per-agent endpoint) must speak a protocol this backend declares
            # and respect its harness_lock. A Claude subscription endpoint,
            # for instance, can never ride along as a subagent endpoint in an
            # opencode profile. Fail the run cleanly rather than launch.
            for ep_id, ep in endpoints.items():
                if not endpoint_compatible(backend.descriptor, ep):
                    raise RuntimeError(
                        f"profile {profile.id} is incompatible with backend "
                        f"{backend.code!r}: endpoint {ep_id} speaks protocol "
                        f"{ep.protocol_kind!r} (harness_lock={ep.harness_lock!r})"
                    )

            _setup_phase = "model_assignment"
            default_model = profile.default_model or (
                primary.default_model if primary is not None else None
            )
            model_assignments = compute_model_assignments(
                backend.descriptor,
                spec.backend_config,
                primary=primary,
                default_model=default_model,
            )

            # Pricing snapshot: stamp the prices in effect right now so later
            # price changes never rewrite this run's historical cost (see
            # docs/design/providers-and-endpoints.md, "Pricing integration").
            # ``pricing_live_all``/``pricing_live_source`` are resolved once
            # here and reused at finish time to extend the snapshot for any
            # model that turns out to have actually run outside it. Snapshot
            # building must NEVER fail a launch.
            _setup_phase = "pricing_snapshot"
            pricing_live_all: dict = {}
            pricing_live_source = "bundled"
            try:
                pricing_live_all, pricing_live_source, _pricing_live_as_of = (
                    pricing.resolve_live_all(
                        cfg.data_dir, url=cfg.pricing_url, now=datetime.now(timezone.utc),
                    )
                )
            except Exception:
                log.exception(
                    "pricing live-data resolution failed for run %s; "
                    "snapshot will resolve from the bundled table only", run.id,
                )
                pricing_live_all, pricing_live_source = {}, "bundled"
            try:
                models_to_vendor = {
                    a.model: endpoints[a.endpoint_id].vendor
                    for a in model_assignments.values()
                    if a.endpoint_id in endpoints
                }
                if models_to_vendor:
                    run.pricing_snapshot = pricing.build_pricing_snapshot(
                        models_to_vendor,
                        live_all=pricing_live_all or None,
                        live_source=pricing_live_source,
                    )
                    session.commit()
            except Exception:
                log.exception("failed to stamp pricing snapshot for run %s", run.id)
                try:
                    session.rollback()
                except Exception:
                    pass

            _setup_phase = "execute"
            # Hand the fully-resolved run to its execution target (selected once
            # above via get_executor(execution_target)). run_one keeps ALL
            # DB/state orchestration around this call; the executor only owns
            # launch composition + running the agent to completion.
            on_session_id = _make_session_id_persister(
                session_factory, run.id, conversation.id,
            )
            # Mid-run steering: only an inject-capable backend gets a live
            # queue + host watcher (STEER_INJECT). Queue-only backends leave
            # these None and deliver queued follow-ups via the run-completion
            # drain + auto-continue below.
            steer_queue: Optional[asyncio.Queue] = None
            on_steer_delivered = None
            if backend.provides(bc.Capability.STEER_INJECT):
                steer_queue = asyncio.Queue()
                on_steer_delivered = _make_steer_delivered_callback(
                    session_factory, run.id,
                )
            ctx = RunContext(
                run_id=run.id,
                ticket_id=ticket.id,
                ticket_title=ticket.title,
                base_prompt=ticket.prompt,
                run_intent=run_intent,
                spec=spec,
                backend=backend,
                primary=primary,
                endpoints=endpoints,
                model_assignments=model_assignments,
                workspace_dir=prov.workspace_dir,
                worktree_root=cfg.worktree_root,
                workspace_specs=workspace_specs,
                transcript_path=run.transcript_path,
                base_env=_build_env(
                    spec,
                    cfg.secrets,
                    run_token=issued_token.cleartext,
                    run_id=run.id,
                    ticket_id=ticket.id,
                    api_url=cfg.api_url,
                ),
                git_dirs=prov.git_dirs,
                conversation_session_id=conversation.session_id,
                prior_turn=prior_turn,
                next_run_context=next_run_context,
                cancel_event=cancel_event,
                on_session_id=on_session_id,
                session_factory=session_factory,
                conversation_id=conversation.id,
                steer_queue=steer_queue,
                on_steer_delivered=on_steer_delivered,
                override_executor=cfg.executor,
            )
            outcome = await executor.execute(ctx)
            result = outcome.result
            launch_ctx = outcome.launch_ctx
            # A remote executor (k8s) provisions in-pod and reports the resolved
            # workspace back (branch + base/head SHAs) instead of run_one
            # recording it host-side. Persist those onto the ticket's workspace
            # rows so the Changes view and stacking (base_ref) see real refs.
            if outcome.workspaces:
                _record_reported_workspaces(ticket, conversation.id, run,
                                            outcome.workspaces)

            finish_run(session, run.id, exit_status=result.exit_status,
                       error_summary=result.error_summary,
                       session_id=getattr(result, "session_id", None))

            # Record what actually ran and the backend-shaped resume handle.
            fresh_backend_run = session.get(Run, run.id)
            if fresh_backend_run is not None:
                fresh_backend_run.backend = spec.backend
                fresh_backend_run.endpoint_id = getattr(ticket.profile, "endpoint_id", None)
                fresh_backend_run.session_ref = getattr(result, "session_ref", None)
                # Remote executors (k8s) tag why a run failed per the failure
                # matrix; the local path leaves this None (unchanged behavior).
                if getattr(result, "failure_kind", None):
                    fresh_backend_run.failure_kind = result.failure_kind
                session.commit()

            # Backend post-run hook (e.g. claude publishes its session so
            # `claude --resume <id>` works). Best-effort; never fails the run.
            try:
                backend.after_run(launch_ctx, result)
            except Exception:
                log.exception("after_run hook failed for ticket %s", ticket.id)

            # Persist token usage + cost. The CC SDK may not emit a result
            # event on cancellations or hard crashes, in which case usage
            # stays None.
            usage = getattr(result, "usage", None)
            if usage is not None:
                fresh_run = session.get(Run, run.id)
                if fresh_run is not None:
                    fresh_run.model_used = usage.model
                    fresh_run.input_tokens = usage.input_tokens
                    fresh_run.output_tokens = usage.output_tokens
                    fresh_run.cache_read_tokens = usage.cache_read_tokens
                    fresh_run.cache_write_tokens = usage.cache_write_tokens
                    # Harness-reported cost is the default (and the only
                    # figure available for backends/models the pricing chain
                    # can't resolve). Overridden below when the stamped
                    # snapshot can price the actually-used model — the
                    # harness's own estimate assumes Claude prices and is
                    # wrong behind a compat endpoint.
                    fresh_run.cost_usd = usage.cost_usd
                    try:
                        _extend_and_price_from_snapshot(
                            fresh_run,
                            model=usage.model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cache_read_tokens=usage.cache_read_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                            model_assignments=model_assignments,
                            endpoints=endpoints,
                            primary=primary,
                            live_all=pricing_live_all,
                            live_source=pricing_live_source,
                            usage_by_model=getattr(result, "usage_by_model", None),
                        )
                    except Exception:
                        log.exception(
                            "pricing snapshot extension/cost computation "
                            "failed for run %s", run.id,
                        )
                    session.commit()
            # Sync the conversation's CUMULATIVE totals to this (latest) turn's
            # reported totals. Per the cost rule the conversation equals the
            # LAST turn, not a sum (the SDK reports cumulative-since-process-
            # start tokens, which would double-count cache-read if summed).
            if run.conversation_id is not None:
                sync_conversation_from_turn(session, session.get(Run, run.id))

            # Cache this run's model/tool latency (derived from the now-complete
            # transcript) on run_latency so the analytics dashboard aggregates
            # rows instead of rescanning files. Best-effort: never fails the run.
            try:
                from nightdesk.domain.latency import populate_run_latency
                populate_run_latency(session, session.get(Run, run.id))
            except Exception:
                log.exception("could not populate run_latency for run %s", run.id)

            session.expire_all()

            # Mid-run steering drain: fold any follow-ups the user queued during
            # this run into next_run_context and (on a clean finish + resumable
            # conversation) auto-continue so they actually get delivered. When it
            # stages a continue the ticket is re-queued for its next turn, so we
            # SKIP the review transition / webhook / handoff / commit / cleanup
            # below — the next turn's completion owns those.
            steer_autocontinued = False
            if run.conversation_id is not None:
                try:
                    steer_autocontinued = _steer_drain_and_autocontinue(
                        session,
                        ticket_id=ticket.id,
                        conversation_id=run.conversation_id,
                        exit_status=result.exit_status,
                    )
                except Exception:
                    log.exception("steer drain/auto-continue failed for ticket %s",
                                  ticket.id)
            if steer_autocontinued:
                return result

            cur = session.get(Ticket, ticket.id)
            if cur is not None and cur.status == "running":
                try:
                    transition_status(
                        session, ticket.id, "review", actor=run_actor(run.id),
                    )
                    log.info("run %s completed for ticket %s: exit=%s, transitioned to review",
                             run.id, ticket.id, result.exit_status)
                except Exception:
                    log.exception("could not transition to review for %s",
                                  ticket.id)
                try:
                    _maybe_fire_webhook(
                        session,
                        ticket_id=ticket.id,
                        run_id=run.id,
                        exit_status=result.exit_status,
                        error_summary=result.error_summary,
                        base_url=cfg.api_url,
                    )
                except Exception:
                    log.exception("webhook fire failed for ticket %s", ticket.id)
            else:
                log.info("run %s completed for ticket %s: exit=%s (status already %s)",
                         run.id, ticket.id, result.exit_status,
                         cur.status if cur else "unknown")

            # Context handoff: when a ticket finishes successfully, push a
            # summary into each dependent ticket's next_run_context so the
            # downstream stage sees what happened upstream.
            if result.exit_status == "success":
                try:
                    _handoff_to_dependents(session, ticket.id, run)
                except Exception:
                    log.exception("context handoff failed for ticket %s",
                                  ticket.id)

            # commit_on_finish: when the ticket opts in and the run succeeded,
            # commit the working-tree changes onto each read_write git_worktree
            # branch so dependent (stacked) tickets that provision from this
            # branch via base_ref actually receive this ticket's work. This MUST
            # run before the cleanup_on_success loop below, which may delete the
            # worktree (and its uncommitted changes) out from under us.
            if result.exit_status == "success" and bundle is not None:
                cof_ticket = session.get(Ticket, ticket.id)
                if cof_ticket is not None and cof_ticket.commit_on_finish:
                    for owned_ws in bundle.workspaces:
                        if (owned_ws.kind != "git_worktree"
                                or owned_ws.access != "read_write"):
                            continue
                        try:
                            new_sha = _commit_workspace_changes(
                                owned_ws, ticket_id=ticket.id, run_id=run.id,
                                run_intent=run_intent,
                            )
                            if new_sha:
                                append_event(run.transcript_path, {
                                    "type": "system",
                                    "subtype": "commit_on_finish",
                                    "data": {
                                        "branch": owned_ws.branch,
                                        "commit": new_sha,
                                        "workspace": str(owned_ws.path),
                                    },
                                })
                        except Exception:
                            log.exception(
                                "commit_on_finish failed for ticket %s", ticket.id)

            if result.exit_status == "success" and bundle is not None:
                for owned_ws in bundle.workspaces:
                    if owned_ws.retention == "cleanup_on_success":
                        cleanup_workspace(owned_ws)
            return result
        finally:
            # Revoke the run token in every exit path so a misbehaving run
            # can't keep poking the API after it ends.
            if issued_token is not None:
                try:
                    revoke_for_run(session, run.id)
                except Exception:
                    log.exception("failed to revoke run token for run %s", run.id)
            # Detach the per-run log handler so the next run gets a fresh file.
            if run_log_handler is not None:
                try:
                    logging.getLogger().removeHandler(run_log_handler)
                    run_log_handler.close()
                except Exception:
                    pass
    except Exception as exc:
            log.exception("setup failed for ticket %s during %s phase",
                          ticket_id, _setup_phase)
            tb_text = traceback.format_exc()
            try:
                session.rollback()
            except Exception:
                pass

            setup_run_id = None
            if run is not None:
                setup_run_id = run.id
            else:
                cur_ticket = session.get(Ticket, ticket_id)
                if cur_ticket is not None:
                    setup_run_id = cur_ticket.current_run_id
            if setup_run_id is not None:
                try:
                    setup_run = session.get(Run, setup_run_id)
                    if setup_run is not None and setup_run.finished_at is None:
                        finish_run(
                            session,
                            setup_run_id,
                            exit_status="failed",
                            error_summary=f"setup error: {exc}",
                        )
                except Exception:
                    log.exception("could not finish setup-failed run %s", setup_run_id)
            try:
                cur = session.get(Ticket, ticket_id)
                if cur is not None and cur.status == "running":
                    transition_status(
                        session, ticket_id, "review",
                        actor=run_actor(cur.current_run_id),
                    )
            except Exception:
                log.exception("could not transition setup-failed ticket %s "
                              "to review", ticket_id)
            try:
                cur_ticket = session.get(Ticket, ticket_id)
                if (cur_ticket is not None
                        and cur_ticket.current_run_id is not None):
                    _maybe_fire_webhook(
                        session,
                        ticket_id=ticket_id,
                        run_id=cur_ticket.current_run_id,
                        exit_status="failed",
                        error_summary=f"setup error: {exc}",
                        base_url=cfg.api_url,
                    )
            except Exception:
                log.exception("webhook fire failed for setup-failed ticket %s", ticket_id)
            # Best-effort: if a run row was created before the failure,
            # write the worker_error onto its transcript so the user can
            # see it in the detail view. When setup fails before
            # start_run, there is no transcript file yet — the failure is
            # only visible via the logger output, which is acceptable
            # because no run row exists for the user to drill into.
            try:
                cur_ticket = session.get(Ticket, ticket_id)
                if cur_ticket is not None and cur_ticket.current_run_id is not None:
                    setup_run = session.get(Run, cur_ticket.current_run_id)
                    if setup_run is not None and setup_run.transcript_path:
                        append_worker_error(
                            setup_run.transcript_path,
                            kind="setup_error",
                            summary=f"worker setup failed during {_setup_phase} phase: {exc!r}",
                            traceback_text=tb_text,
                        )
            except Exception:
                log.exception("could not record setup failure on transcript "
                              "for %s", ticket_id)
            return ExecutionResult(
                exit_status="failed",
                error_summary=f"setup error: {exc}",
            )
    finally:
        session.close()
