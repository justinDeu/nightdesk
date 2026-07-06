"""Claude Code backend (``claude_sdk``).

Behaviour-preserving home for what used to live inline in ``run_one.py`` and
``worker.backends``: the Claude Agent SDK runs in a sandboxed subprocess
(``python -m nightdesk.worker._sdk_runner``), the per-run session store is
bound in so ``claude --resume`` works afterwards, and credentials map to the
``ANTHROPIC_*`` env vars.

When a profile has no endpoint attached, ``prepare_launch`` takes the legacy
path unchanged: an empty env, letting the worker's ``_build_env`` supply
``claude_credentials`` / ambient auth. When an endpoint is attached,
``prepare_launch`` dispatches on its protocol — both ``anthropic`` and
``anthropic_compat`` render through :func:`_render_anthropic`, which branches
credential handling on ``credential_source`` rather than protocol (a Claude
subscription is ``anthropic`` + ``credential_source=subscription_file``, not
its own protocol — see ``docs/design/providers-and-endpoints.md``).
"""
from __future__ import annotations

import json
import logging
import shlex
import sys
from typing import Optional

from nightdesk.backends.base import (
    Backend,
    LaunchContext,
    LaunchPlan,
    ResumeDescriptor,
    StdioTransport,
)
from nightdesk.domain import backend_capabilities as bc
from nightdesk.worker.claude_executor import ClaudeExecutor
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult


log = logging.getLogger(__name__)


# Model slot name -> the ANTHROPIC_*/CLAUDE_CODE_* env var it renders as.
_SLOT_ENV = {
    "primary": "ANTHROPIC_MODEL",
    "opus_alias": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet_alias": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku_alias": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "small_fast": "ANTHROPIC_SMALL_FAST_MODEL",
    "subagent": "CLAUDE_CODE_SUBAGENT_MODEL",
}


def _extract_subscription_token(raw: str) -> Optional[str]:
    """Pull the OAuth access token out of a raw Claude credentials JSON blob.

    Tries ``claudeAiOauth.accessToken`` first, then a top-level
    ``accessToken``. Returns None (never raises) on any parse/shape failure —
    the sandbox's inherit-credentials mount still authenticates in that case,
    so silently emitting nothing is the correct degrade, not a hard failure.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        log.warning("subscription credential is not valid JSON: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    token = (data.get("claudeAiOauth") or {}).get("accessToken") if isinstance(
        data.get("claudeAiOauth"), dict
    ) else None
    if not token:
        token = data.get("accessToken")
    return token if isinstance(token, str) and token else None


def _render_anthropic(ctx: LaunchContext) -> LaunchPlan:
    ep = ctx.endpoint
    env: dict[str, str] = {}
    secret_env_keys: set[str] = set()
    if ep.base_url:
        env["ANTHROPIC_BASE_URL"] = ep.base_url
    if ep.credential:
        if ep.credential_source == "subscription_file":
            token = _extract_subscription_token(ep.credential)
            if token:
                env["ANTHROPIC_AUTH_TOKEN"] = token
                secret_env_keys.add("ANTHROPIC_AUTH_TOKEN")
            else:
                log.warning(
                    "endpoint %s: could not extract an OAuth token from the "
                    "subscription credential; relying on sandbox "
                    "inherit-credentials auth instead", ep.id,
                )
        else:
            env["ANTHROPIC_API_KEY"] = ep.credential
            secret_env_keys.add("ANTHROPIC_API_KEY")
    for slot, assignment in ctx.model_assignments.items():
        var = _SLOT_ENV.get(slot)
        if var:
            env[var] = assignment.model
    extra_env = (ep.extra or {}).get("env", {})
    env.update(extra_env)  # vendor quirks win
    # Vendor-quirk env values may carry secrets (routing tokens, custom auth
    # headers threaded through as env vars) — mark conservatively.
    secret_env_keys.update(extra_env.keys())
    return LaunchPlan(
        cmd=[sys.executable, "-m", "nightdesk.worker._sdk_runner"],
        env=env,
        transport=StdioTransport(),
        needs_claude_binary=True,
        secret_env_keys=frozenset(secret_env_keys),
    )


class ClaudeBackend(Backend):
    descriptor = bc.CLAUDE_SDK
    wants_http = False
    _renderers = {
        "anthropic": _render_anthropic,
        "anthropic_compat": _render_anthropic,
    }

    def prepare_launch(self, ctx: LaunchContext) -> LaunchPlan:
        cc_sessions_dir = str(ctx.scratch_root / "cc-sessions")
        ctx.backend_state["cc_sessions_dir"] = cc_sessions_dir

        plan = self.dispatch_renderer(ctx)
        if plan is NotImplemented:
            # Legacy/ambient path: no endpoint attached. Empty env — the
            # worker's _build_env supplies claude_credentials / ambient auth.
            plan = LaunchPlan(
                cmd=[sys.executable, "-m", "nightdesk.worker._sdk_runner"],
                env={},
                transport=StdioTransport(),
                needs_claude_binary=True,
            )
        plan.cc_sessions_dir = cc_sessions_dir
        return plan

    async def execute(self, req: ExecutionRequest) -> ExecutionResult:
        result = await ClaudeExecutor().run(req)
        if result.session_id and result.session_ref is None:
            result.session_ref = {"session_id": result.session_id}
        return result

    def after_run(self, ctx: LaunchContext, result: ExecutionResult) -> None:
        sid = result.session_id
        store = ctx.backend_state.get("cc_sessions_dir")
        if sid and store:
            # Best effort: publish the session so `claude --resume <id>` works.
            from nightdesk.worker.sandbox import publish_cc_session
            publish_cc_session(store, sid)

    def resume_descriptor(self, run) -> Optional[ResumeDescriptor]:
        sid = getattr(run, "session_id", None)
        worktree = getattr(run, "worktree_path", None)
        if not sid or not worktree:
            ref = getattr(run, "session_ref", None) or {}
            sid = sid or ref.get("session_id")
        if not sid or not worktree:
            return None
        command = (
            f"cd {shlex.quote(str(worktree))} && "
            f"claude --resume {shlex.quote(str(sid))}"
        )
        return ResumeDescriptor(
            backend=self.code, label="Claude Code", command=command,
        )
