"""LocalExecutor — today's on-host bwrap execution path, extracted verbatim.

This is a behavior-preserving move of ``run_one``'s launch-composition and
execution body: ``backend.prepare_launch`` -> continue/resume session seeding
-> ``build_bwrap_argv`` -> headless prompt build -> ``backend.execute`` (or the
injected ``RunOneConfig.executor`` test seam) -> post-execute transcript
breadcrumbs. ``run_one`` keeps all DB/state orchestration around this call
(run row, tokens, pricing snapshot, conversation, transitions, webhook,
dependents, workspace resolution, commit_on_finish, cleanup).

Pure motion: no behavior change. See ``executors/base.py`` for the import-arrow
note and ``docs/design/session-suite/k8s-executor.md`` (Phase 1) for the intent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import traceback
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.orm import Session

from nightdesk.backends import LaunchContext
from nightdesk.db.models import Ticket
from nightdesk.executors.base import (
    ExecutionOutcome,
    Executor,
    ProvisionContext,
    ProvisionOutcome,
    RunContext,
)
from nightdesk.executors.registry import register
from nightdesk.transcript import append_event, append_worker_error, next_transcript_seq
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult
from nightdesk.worker.headless_prompt import (
    build_continue_prompt,
    build_headless_prompt,
)
from nightdesk.worker.sandbox import (
    build_bwrap_argv,
    run_cc_sessions_dir,
    seed_cc_session,
)
from nightdesk.worker.workspace import prepare_workspace_bundle

log = logging.getLogger(__name__)


def _alloc_free_port() -> int:
    """Reserve a free localhost TCP port for an HTTP-transport backend.

    Bind to port 0, read the assigned port, close. The window between close
    and the backend's server binding is a benign race on a single-host
    worker; the agent shares the host net namespace so the port is reachable.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


class LocalExecutor(Executor):
    """Run the agent in an on-host bwrap sandbox — the default execution
    target. Behavior-identical to what ``run_one`` used to do inline."""

    code = "local"

    async def provision(self, ctx: ProvisionContext) -> ProvisionOutcome:
        """Build the on-host worktree bundle — today's ``run_one`` workspace
        prep, moved verbatim behind the two-phase seam. run_one still records
        the host workspace resolution (see ``_record_workspace_resolution`` and
        the fs-snapshot / tool-path steps) off the returned bundle, so the local
        flow is byte-identical: only the ``prepare_workspace_bundle`` *call*
        moved here."""
        bundle = prepare_workspace_bundle(
            ticket_id=ctx.ticket_id,
            root=ctx.worktree_root,
            specs=ctx.specs,
            reuse_existing_worktrees=ctx.reuse_existing_worktrees,
            fresh_worktree_paths=ctx.fresh_worktree_paths,
        )
        primary = bundle.primary
        git_dirs: list[str] = []
        for w in bundle.workspaces:
            if w.kind == "git_worktree" and w.git_common_dir:
                d = str(w.git_common_dir)
                if d not in git_dirs:
                    git_dirs.append(d)
        return ProvisionOutcome(
            workspace_dir=primary.path,
            bundle=bundle,
            git_dirs=git_dirs,
        )

    async def execute(self, ctx: RunContext) -> ExecutionOutcome:
        # Per-run scratch, anchored as a sibling of worktree_root — outside
        # the protected ~/.local/share/nightdesk data dir so it can be
        # bind-mounted into the sandbox (same reason worktrees live there).
        scratch_root = ctx.worktree_root.parent / "nightdesk-backend-scratch" / ctx.run_id
        launch_ctx = LaunchContext(
            spec=ctx.spec,
            endpoint=ctx.primary,
            run_id=ctx.run_id,
            ticket_id=ctx.ticket_id,
            workspace_dir=ctx.workspace_dir,
            scratch_root=scratch_root,
            http_port=_alloc_free_port() if ctx.backend.wants_http else None,
            endpoints=ctx.endpoints,
            model_assignments=ctx.model_assignments,
        )
        plan = ctx.backend.prepare_launch(launch_ctx)

        # Backend launch env (provider creds, agent-specific knobs) layers
        # over the base run env (built host-side by run_one).
        env = {**ctx.base_env, **plan.env}
        # Per-run Claude session store, only set for backends that model
        # one (claude_sdk); the continue/resume block below is a no-op for
        # backends that leave this None. Legacy root kept as a seed-source
        # fallback for continuations whose parent turn predates the
        # unified per-run scratch layout.
        cc_sessions_dir = plan.cc_sessions_dir
        cc_sessions_root = ctx.worktree_root.parent / "nightdesk-cc-sessions"

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
        if ctx.run_intent == "continue" and cc_sessions_dir is None:
            # This backend doesn't model a resumable Claude-style session
            # store (e.g. opencode's resume path is backend-native, not
            # this seeded-jsonl mechanism); always run as a fresh-context
            # resume on the same worktree.
            fell_back_to_fresh_context = True
            log.warning(
                "continue turn %s: backend %s has no session store; "
                "falling back to fresh-context resume", ctx.run_id, ctx.spec.backend,
            )
        elif ctx.run_intent == "continue":
            conv_sid = ctx.conversation_session_id
            seed_source = ctx.prior_turn.id if ctx.prior_turn is not None else None
            if conv_sid and seed_source is not None:
                seeded = seed_cc_session(
                    cc_sessions_dir,
                    conv_sid,
                    [
                        run_cc_sessions_dir(str(cc_sessions_root), seed_source),
                        # A parent turn that ran after the backend refactor
                        # stores its session under the unified per-run
                        # scratch layout instead of the legacy root above.
                        str(ctx.worktree_root.parent / "nightdesk-backend-scratch"
                            / seed_source / "cc-sessions"),
                        os.path.expanduser("~/.claude/projects"),
                    ],
                )
                if seeded is not None:
                    resume_session_id = conv_sid
                    log.info(
                        "continue turn %s: resuming conversation session %s (seeded %s)",
                        ctx.run_id, conv_sid, seeded,
                    )
                else:
                    fell_back_to_fresh_context = True
                    log.warning(
                        "continue turn %s: conversation session %s not found "
                        "in any store; falling back to fresh-context resume",
                        ctx.run_id, conv_sid,
                    )
            elif conv_sid:
                # Session id present but no prior turn to seed from; the SDK
                # may still resolve it from the published host store.
                resume_session_id = conv_sid
            else:
                fell_back_to_fresh_context = True
                log.warning(
                    "continue turn %s: conversation has no session_id; "
                    "falling back to fresh-context resume", ctx.run_id,
                )

        # Seed this turn's seq counter from the shared conversation
        # transcript so the file keeps ONE monotonic seq space across turns.
        seq_start = next_transcript_seq(ctx.transcript_path)

        log.debug("building bwrap argv for ticket %s run %s (backend=%s)",
                  ctx.ticket_id, ctx.run_id, ctx.spec.backend)
        argv = build_bwrap_argv(
            ctx.spec,
            working_dir=str(ctx.workspace_dir),
            cmd=plan.cmd,
            env=env,
            cc_sessions_dir=plan.cc_sessions_dir,
            git_dirs=ctx.git_dirs,
            mounts=plan.mounts,
            require_claude=plan.needs_claude_binary,
        )
        log.debug("building headless prompt for ticket %s run %s", ctx.ticket_id, ctx.run_id)
        # ``continue`` intent: when the user typed a message AND we are
        # genuinely resuming the prior SDK conversation, send that message
        # as the next user turn on top of the resumed history — not buried
        # as "NEXT RUN CONTEXT" inside the reconstructed headless blob. The
        # base ticket prompt plus every prior turn are already in the
        # resumed session, so build_continue_prompt emits the user's text as
        # the body of the SDK prompt, which the SDK appends verbatim as the
        # next user message. Textless continues (the header button) and
        # every fresh-context fallback keep the existing reconstructed prompt.
        continue_message = (ctx.next_run_context or "").strip() if ctx.run_intent == "continue" else ""
        genuine_continue = bool(
            ctx.run_intent == "continue" and resume_session_id and continue_message
        )
        if genuine_continue:
            prompt = build_continue_prompt(
                ticket_id=ctx.ticket_id,
                ticket_title=ctx.ticket_title,
                user_message=continue_message,
                workspace_path=str(ctx.workspace_dir),
            )
        else:
            # When a ``continue`` fell back to fresh context, the agent is NOT
            # resuming the prior conversation, so don't hand it the
            # "resuming the prior conversation" instruction — use the honest
            # resume (fresh-context) wording instead. The Run.intent DB column
            # still records "continue" for traceability.
            prompt_intent = "resume" if fell_back_to_fresh_context else ctx.run_intent
            prompt = build_headless_prompt(
                ticket_id=ctx.ticket_id,
                ticket_title=ctx.ticket_title,
                base_prompt=ctx.base_prompt,
                run_intent=prompt_intent,
                workspace_path=str(ctx.workspace_dir),
                next_run_context=ctx.next_run_context,
                last_run_summary=(ctx.prior_turn.error_summary or ctx.prior_turn.exit_status) if ctx.prior_turn is not None else None,
            )
        request = ExecutionRequest(
            ticket_id=ctx.ticket_id, prompt=prompt,
            working_dir=ctx.workspace_dir, transcript_path=Path(ctx.transcript_path),
            bwrap_argv=argv, env=env, permission_spec=ctx.spec,
            cancel_event=ctx.cancel_event,
            resume_session_id=resume_session_id,
            # Surface the typed continue message at the transcript boundary
            # for any continue that carried one (genuine resume or fallback),
            # so the resumed run visibly opens with what the user typed.
            continue_message=continue_message or None,
            seq_start=seq_start,
            # Eagerly persist the conversation.session_id the moment the SDK
            # emits it (init event), so a cancel/crash afterward never leaves
            # the conversation null-session.
            on_session_id=ctx.on_session_id,
            http_port=launch_ctx.http_port,
            launch_meta=launch_ctx.backend_state,
        )

        watcher = asyncio.create_task(
            _cancel_watcher(ctx.session_factory, ctx.ticket_id, ctx.cancel_event)
        )
        executor_error_kind: Optional[str] = None
        executor_error_tb: Optional[str] = None
        try:
            if ctx.override_executor is not None:
                # Test/override seam: run the supplied executor directly.
                result: ExecutionResult = await ctx.override_executor.run(request)
            else:
                result = await ctx.backend.execute(request)
        except Exception as exc:
            log.exception("executor crashed for ticket %s", ctx.ticket_id)
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
                ctx.transcript_path,
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
                append_event(ctx.transcript_path, {
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
                log.exception("could not record continue-fallback breadcrumb for run %s", ctx.run_id)

        return ExecutionOutcome(
            result=result,
            launch_ctx=launch_ctx,
            workspaces=[],
            diff_uploaded=False,
        )


register(LocalExecutor.code, LocalExecutor())
