"""Single-ticket runner — the unit of execution invoked as a subprocess.

This module is the standalone equivalent of what ``WorkerLoop._run_ticket``
used to do inline. The daemon spawns ``nightdesk-run-ticket <id>`` per
pick so a crashing run can never take down the dispatcher: if the
subprocess segfaults, the daemon notices via the orphaned-Run sweep and
the user sees a 'review' ticket with a system comment.

Anything that needs to behave like the daemon (profile -> spec merge,
ticket.cwd / additional_dirs handling, run-now bookkeeping, transcript
schema, status transitions, cancel signaling) lives here so the CLI and
the daemon never drift.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session

from nightdesk.db.models import ConfigRow, Run, Ticket
from nightdesk.domain.permissions import PermissionSpec, merge_permissions
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.run_tokens import issue_run_token, revoke_for_run
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import transition_status
from nightdesk.transcript import append_worker_error
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult, Executor
from nightdesk.worker.headless_prompt import (
    HEADLESS_POLICY_VERSION,
    build_headless_prompt,
)
from nightdesk.worker.sandbox import SANDBOX_HOME, build_bwrap_argv
from nightdesk.worker.workspace import (
    Workspace, WorkspaceBundle, WorkspaceSpec, cleanup_workspace,
    prepare_workspace_bundle,
)


log = logging.getLogger(__name__)


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
    include_legacy_fs: bool = True,
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
    if not include_legacy_fs:
        return spec
    extras_rw: list[str] = []
    for entry in (ticket.additional_dirs or []):
        if not isinstance(entry, dict):
            continue
        mode = entry.get("mode", "rw")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        if mode == "rw":
            extras_rw.append(path)
        else:
            log.warning("ticket %s additional_dirs entry mode=%r ignored "
                        "(only 'rw' is honored)", ticket.id, mode)
    if extras_rw:
        existing = set(spec.fs_write)
        for p_ in extras_rw:
            if p_ not in existing:
                spec.fs_write.append(p_)
                existing.add(p_)
    if ticket.cwd and ticket.cwd not in spec.fs_write:
        spec.fs_write.append(ticket.cwd)
    return spec

def _workspace_specs_for_ticket(ticket: Ticket) -> list[WorkspaceSpec]:
    if getattr(ticket, "workspaces", None):
        return [
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

    specs = [
        WorkspaceSpec(
            role="primary",
            label="primary",
            kind=getattr(ticket, "workspace_mode", None) or "directory",
            access="read_write",
            source_path=ticket.cwd,
        )
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


def _apply_workspace_permissions(spec: PermissionSpec,
                                 bundle: WorkspaceBundle) -> PermissionSpec:
    for ws in bundle.fs_write:
        _dedup_append(spec.fs_write, str(ws.path))
    for ws in bundle.fs_read:
        _dedup_append(spec.fs_read, str(ws.path))
    return spec


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
        row.retention = ws.retention
        row.state = "active"



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
    env: dict[str, str] = {
        "PATH": _DEFAULT_PATH,
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
    return env


def _resolve_executor(backend: str, override: Optional[Executor]) -> Executor:
    if override is not None:
        return override
    from nightdesk.worker.backends import get_executor

    return get_executor(backend)


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
            ticket = session.get(Ticket, ticket_id)
            if ticket is None:
                log.error("ticket %s not found", ticket_id)
                return ExecutionResult(exit_status="failed",
                                       error_summary="ticket not found")
            overrides = dict(ticket.permission_overrides or {})
            run_intent = str(overrides.get("nightdesk_run_intent") or "first_run")
            parent_run_id = overrides.get("nightdesk_parent_run_id")
            restart_workspace_policy = overrides.get("nightdesk_restart_workspace_policy")
            parent_run = session.get(Run, parent_run_id) if isinstance(parent_run_id, str) else None
            next_run_context = ticket.next_run_context
            workspace_specs = _workspace_specs_for_ticket(ticket)
            # A ticket in review/archived/queued may already have a resolved
            # worktree from a prior run.  If the intent is first_run (the
            # default, set by bare run-now / scheduler pick) and any workspace
            # is a git_worktree, reuse it instead of failing with
            # "worktree_path already exists".
            has_existing_worktree = (
                run_intent == "first_run"
                and any(s.kind == "git_worktree" for s in workspace_specs)
                and ticket.current_run_id is not None
            )
            if run_intent == "restart":
                for spec in workspace_specs:
                    if spec.kind == "git_worktree":
                        spec.branch = None
                if restart_workspace_policy == "recreate_in_place":
                    _cleanup_recorded_worktrees(ticket, delete_branches=True)
            bundle = prepare_workspace_bundle(
                ticket_id=ticket.id,
                root=cfg.worktree_root,
                specs=workspace_specs,
                reuse_existing_worktrees=run_intent in {"resume", "retry"} or has_existing_worktree,
                fresh_worktree_paths=run_intent == "restart" and restart_workspace_policy == "fresh_path",
            )
            ws = bundle.primary
            _record_workspace_resolution(ticket, bundle)
            spec = _apply_workspace_permissions(
                _profile_to_spec(
                    ticket,
                    include_legacy_fs=False,
                    secret_box=secret_box,
                    default_claude_binary=default_claude_binary,
                ),
                bundle,
            )
            cfg.transcript_root.mkdir(parents=True, exist_ok=True)

            started_as_run_now = bool(ticket.run_now)
            if ticket.run_now:
                ticket.run_now = False
            overrides.pop("nightdesk_run_intent", None)
            overrides.pop("nightdesk_parent_run_id", None)
            overrides.pop("nightdesk_restart_workspace_policy", None)
            ticket.permission_overrides = overrides or None
            ticket.next_run_context = None
            ticket.next_run_context_updated_at = None

            run_id = str(uuid.uuid4())
            run = start_run(
                session,
                id=run_id,
                ticket_id=ticket.id,
                worktree_path=str(ws.worktree_path or ws.path),
                transcript_path=str(cfg.transcript_root / f"{run_id}.log"),
                pid=os.getpid(),
                host=cfg.host,
                started_as_run_now=started_as_run_now,
                intent=run_intent,
                parent_run_id=parent_run.id if parent_run is not None else None,
                headless_policy_version=HEADLESS_POLICY_VERSION,
                restart_workspace_policy=restart_workspace_policy,
            )

            # Resolve scheduling knobs from the config table (live values).
            schedule_cfg = session.get(ConfigRow, 1)
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
            argv = build_bwrap_argv(
                spec,
                cwd=str(ws.path),
                cmd=[sys.executable, "-m", "nightdesk.worker._sdk_runner"],
                env=env,
            )
            prompt = build_headless_prompt(
                ticket_id=ticket.id,
                ticket_title=ticket.title,
                base_prompt=ticket.prompt,
                run_intent=run_intent,
                workspace_path=str(ws.path),
                next_run_context=next_run_context,
                last_run_summary=(parent_run.error_summary or parent_run.exit_status) if parent_run is not None else None,
            )
            request = ExecutionRequest(
                ticket_id=ticket.id, prompt=prompt,
                cwd=ws.path, transcript_path=Path(run.transcript_path),
                bwrap_argv=argv, env=env, permission_spec=spec,
                cancel_event=cancel_event,
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

            finish_run(session, run.id, exit_status=result.exit_status,
                       error_summary=result.error_summary)

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

            session.expire_all()
            cur = session.get(Ticket, ticket.id)
            if cur is not None and cur.status == "running":
                try:
                    transition_status(session, ticket.id, "review")
                except Exception:
                    log.exception("could not transition to review for %s",
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
            log.exception("setup failed for ticket %s before executor.run",
                          ticket_id)
            tb_text = traceback.format_exc()
            try:
                session.rollback()
            except Exception:
                pass
            try:
                cur = session.get(Ticket, ticket_id)
                if cur is not None and cur.status == "running":
                    transition_status(session, ticket_id, "review")
            except Exception:
                log.exception("could not transition setup-failed ticket %s "
                              "to review", ticket_id)
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
                            summary=f"worker setup failed: {exc!r}",
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
