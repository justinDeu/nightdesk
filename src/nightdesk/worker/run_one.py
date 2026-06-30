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
import sys
import traceback
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightdesk.db.models import ConfigRow, Run, Ticket
from nightdesk.domain.conversations import (
    create_conversation, get_conversation, latest_turn, sync_conversation_from_turn,
)
from nightdesk.domain.permissions import PermissionSpec, merge_permissions
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.run_tokens import issue_run_token, revoke_for_run
from nightdesk.domain.notifications import (
    build_run_completion_payload,
    fire_webhook,
)
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import transition_status
from nightdesk.domain.toolchains import resolve_tool_paths
from nightdesk.transcript import append_event, append_worker_error, next_transcript_seq
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult, Executor
from nightdesk.worker.headless_prompt import (
    HEADLESS_POLICY_VERSION,
    build_continue_prompt,
    build_headless_prompt,
)
from nightdesk.worker.sandbox import (
    SANDBOX_HOME,
    build_bwrap_argv,
    publish_cc_session,
    run_cc_sessions_dir,
    seed_cc_session,
)
from nightdesk.worker.workspace import (
    Workspace, WorkspaceBundle, WorkspaceSpec, cleanup_workspace,
    prepare_workspace_bundle,
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


@dataclass
class RunOneConfig:
    worktree_root: Path
    transcript_root: Path
    secrets: dict[str, str]
    host: str
    bearer_token: str = ""
    api_url: str = "http://127.0.0.1:8765"
    # Optional override; defaults to dispatching via worker.backends.get_executor.
    executor: Optional[Executor] = None


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


def _resolve_executor(backend: str, override: Optional[Executor]) -> Executor:
    if override is not None:
        return override
    from nightdesk.worker.backends import get_executor

    return get_executor(backend)


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


async def _cancel_watcher(
    session_factory: Callable[[], Session],
    ticket_id: str,
    cancel_event: asyncio.Event,
) -> None:
    """Poll the ticket; set the cancel event if status leaves 'running'."""
    try:
        while not cancel_event.is_set():
            session = session_factory()
            try:
                t = session.get(Ticket, ticket_id)
                if t is not None and t.status != "running":
                    cancel_event.set()
                    return
            finally:
                session.close()
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return
    except asyncio.CancelledError:
        return


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
    watcher: Optional[asyncio.Task] = None
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

            log.debug("preparing workspace bundle for ticket %s", ticket.id)
            _setup_phase = "workspace_prep"
            bundle = prepare_workspace_bundle(
                ticket_id=ticket.id,
                root=cfg.worktree_root,
                specs=workspace_specs,
                # resume/retry/continue all reuse the existing worktree (continue
                # resumes the prior SDK conversation on the same worktree).
                reuse_existing_worktrees=run_intent in {"resume", "retry", "continue"} or has_existing_worktree,
                fresh_worktree_paths=run_intent == "restart" and restart_workspace_policy == "fresh_path",
            )
            ws = bundle.primary
            run.worktree_path = str(ws.worktree_path or ws.path)
            _record_workspace_resolution(ticket, bundle)
            # Bind this conversation's workspaces to it (1:N conversation
            # ownership) so continuing an OLD conversation restores the tree it
            # ran against, not one a newer conversation mutated.
            for row in (ticket.workspaces or []):
                if row.conversation_id is None:
                    row.conversation_id = conversation.id
            _setup_phase = "profile_spec"
            spec = _apply_workspace_permissions(
                _profile_to_spec(
                    ticket,
                    secret_box=secret_box,
                    default_claude_binary=default_claude_binary,
                ),
                bundle,
            )
            schedule_cfg = session.get(ConfigRow, 1)
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
            # for directory workspaces. Keyed to this run + workspace.
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

            env = _build_env(
                spec,
                cfg.secrets,
                run_token=issued_token.cleartext,
                run_id=run.id,
                ticket_id=ticket.id,
                api_url=cfg.api_url,
            )
            # Use the running interpreter so the sandbox finds the installed
            # nightdesk package (--clearenv wipes PYTHONPATH; relying on
            # bare ``python`` would pick up the system Python without the
            # editable install).
            # Anchor the session store as a sibling of worktree_root — outside
            # the protected ~/.local/share/nightdesk data dir, so it can be
            # bind-mounted into the sandbox (same reason worktrees live there).
            cc_sessions_root = cfg.worktree_root.parent / "nightdesk-cc-sessions"
            cc_sessions_dir = run_cc_sessions_dir(str(cc_sessions_root), run.id)

            # ``continue`` intent: resume the CONVERSATION's Claude Code
            # conversation by replaying its authoritative session id into the
            # SDK. Each run gets an isolated per-run session store bound over
            # the sandbox's CLAUDE_CONFIG_DIR/projects, so the prior turn's
            # session file must be seeded into THIS run's store for the SDK's
            # ``resume=<id>`` to resolve inside the sandbox. If the conversation
            # has no session id or the file is gone, fall back to a fresh-context
            # resume (same worktree, no conversation history) and record it.
            resume_session_id: Optional[str] = None
            fell_back_to_fresh_context = False
            if run_intent == "continue":
                conv_sid = conversation.session_id
                seed_source = prior_turn.id if prior_turn is not None else None
                if conv_sid and seed_source is not None:
                    seeded = seed_cc_session(
                        cc_sessions_dir,
                        conv_sid,
                        [
                            run_cc_sessions_dir(str(cc_sessions_root), seed_source),
                            os.path.expanduser("~/.claude/projects"),
                        ],
                    )
                    if seeded is not None:
                        resume_session_id = conv_sid
                        log.info(
                            "continue turn %s: resuming conversation session %s (seeded %s)",
                            run.id, conv_sid, seeded,
                        )
                    else:
                        fell_back_to_fresh_context = True
                        log.warning(
                            "continue turn %s: conversation session %s not found "
                            "in any store; falling back to fresh-context resume",
                            run.id, conv_sid,
                        )
                elif conv_sid:
                    # Session id present but no prior turn to seed from; the SDK
                    # may still resolve it from the published host store.
                    resume_session_id = conv_sid
                else:
                    fell_back_to_fresh_context = True
                    log.warning(
                        "continue turn %s: conversation has no session_id; "
                        "falling back to fresh-context resume", run.id,
                    )

            # Seed this turn's seq counter from the shared conversation
            # transcript so the file keeps ONE monotonic seq space across turns.
            seq_start = next_transcript_seq(transcript_path)

            log.debug("building bwrap argv for ticket %s run %s", ticket.id, run.id)
            _setup_phase = "bwrap_build"
            argv = build_bwrap_argv(
                spec,
                working_dir=str(ws.path),
                cmd=[sys.executable, "-m", "nightdesk.worker._sdk_runner"],
                env=env,
                cc_sessions_dir=cc_sessions_dir,
                git_dirs=_git_metadata_dirs(bundle),
            )
            log.debug("building headless prompt for ticket %s run %s", ticket.id, run.id)
            _setup_phase = "prompt_build"
            # ``continue`` intent: when the user typed a message AND we are
            # genuinely resuming the prior SDK conversation, send that message
            # as the next user turn on top of the resumed history — not buried
            # as "NEXT RUN CONTEXT" inside the reconstructed headless blob. The
            # base ticket prompt plus every prior turn are already in the
            # resumed session, so build_continue_prompt emits the user's text as
            # the body of the SDK prompt, which the SDK appends verbatim as the
            # next user message. Textless continues (the header button) and
            # every fresh-context fallback keep the existing reconstructed prompt.
            continue_message = (next_run_context or "").strip() if run_intent == "continue" else ""
            genuine_continue = bool(
                run_intent == "continue" and resume_session_id and continue_message
            )
            if genuine_continue:
                prompt = build_continue_prompt(
                    ticket_id=ticket.id,
                    ticket_title=ticket.title,
                    user_message=continue_message,
                    workspace_path=str(ws.path),
                )
            else:
                # When a ``continue`` fell back to fresh context, the agent is NOT
                # resuming the prior conversation, so don't hand it the
                # "resuming the prior conversation" instruction — use the honest
                # resume (fresh-context) wording instead. The Run.intent DB column
                # still records "continue" for traceability.
                prompt_intent = "resume" if fell_back_to_fresh_context else run_intent
                prompt = build_headless_prompt(
                    ticket_id=ticket.id,
                    ticket_title=ticket.title,
                    base_prompt=ticket.prompt,
                    run_intent=prompt_intent,
                    workspace_path=str(ws.path),
                    next_run_context=next_run_context,
                    last_run_summary=(prior_turn.error_summary or prior_turn.exit_status) if prior_turn is not None else None,
                )
            request = ExecutionRequest(
                ticket_id=ticket.id, prompt=prompt,
                working_dir=ws.path, transcript_path=Path(run.transcript_path),
                bwrap_argv=argv, env=env, permission_spec=spec,
                cancel_event=cancel_event,
                resume_session_id=resume_session_id,
                # Surface the typed continue message at the transcript boundary
                # for any continue that carried one (genuine resume or fallback),
                # so the resumed run visibly opens with what the user typed.
                continue_message=continue_message or None,
                seq_start=seq_start,
                # Eagerly persist the conversation.session_id the moment the SDK
                # emits it (init event), so a cancel/crash afterward never leaves
                # the conversation null-session.
                on_session_id=_make_session_id_persister(
                    session_factory, run.id, conversation.id,
                ),
            )

            watcher = asyncio.create_task(
                _cancel_watcher(session_factory, ticket.id, cancel_event)
            )
            executor_error_kind: Optional[str] = None
            executor_error_tb: Optional[str] = None
            try:
                executor = _resolve_executor(spec.backend, cfg.executor)
                result: ExecutionResult = await executor.run(request)
            except Exception as exc:
                log.exception("executor crashed for ticket %s", ticket.id)
                executor_error_kind = "executor_error"
                executor_error_tb = traceback.format_exc()
                result = ExecutionResult(
                    exit_status="failed",
                    error_summary=f"executor error: {exc}",
                )
            finally:
                if watcher is not None and not watcher.done():
                    watcher.cancel()
                    try:
                        await watcher
                    except asyncio.CancelledError:
                        pass

            # Worker-level error surfacing: any failed run gets a final
            # ``worker_error`` event tacked onto its transcript so the user
            # sees the failure cause in the same view they read the rest of
            # the run. Cancellations stay quiet because the user already
            # knows they cancelled.
            if result.exit_status not in ("success", "cancelled") and result.error_summary:
                kind = executor_error_kind or "run_failed"
                append_worker_error(
                    run.transcript_path,
                    kind=kind,
                    summary=result.error_summary,
                    traceback_text=executor_error_tb,
                )

            # Record a continue→fresh-context fallback on the transcript so the
            # reason is discoverable from the run's own artifact (a ``system``
            # event is persisted but not rendered as an error card). Paired with
            # the WARNING log line above; best-effort, never fails the run.
            if fell_back_to_fresh_context:
                try:
                    append_event(run.transcript_path, {
                        "type": "system",
                        "subtype": "continue_session_unavailable",
                        "data": {
                            "message": (
                                "Continue was requested but the prior run had no "
                                "resumable Claude session; ran as a fresh-context "
                                "resume on the same worktree instead."
                            ),
                        },
                    })
                except Exception:
                    log.exception("could not record continue-fallback breadcrumb for run %s", run.id)

            finish_run(session, run.id, exit_status=result.exit_status,
                       error_summary=result.error_summary,
                       session_id=getattr(result, "session_id", None))

            # Publish the run's Claude session from its isolated per-run store
            # into the host ~/.claude/projects so `claude --resume <id>` works.
            # Best-effort; never fails the run.
            _sid = getattr(result, "session_id", None)
            if _sid:
                publish_cc_session(cc_sessions_dir, _sid)

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
                    fresh_run.cost_usd = usage.cost_usd
                    session.commit()
            # Sync the conversation's CUMULATIVE totals to this (latest) turn's
            # reported totals. Per the cost rule the conversation equals the
            # LAST turn, not a sum (the SDK reports cumulative-since-process-
            # start tokens, which would double-count cache-read if summed).
            if run.conversation_id is not None:
                sync_conversation_from_turn(session, session.get(Run, run.id))

            session.expire_all()
            cur = session.get(Ticket, ticket.id)
            if cur is not None and cur.status == "running":
                try:
                    transition_status(session, ticket.id, "review")
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
                    transition_status(session, ticket_id, "review")
            except Exception:
                log.exception("could not transition setup-failed ticket %s "
                              "to review", ticket_id)
            try:
                cur_ticket = session.get(Ticket, ticket_id)
                if cur_ticket is not None and cur_ticket.current_run_id is not None:
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
