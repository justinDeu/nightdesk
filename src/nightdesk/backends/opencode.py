"""opencode backend.

A per-run ``opencode serve`` daemon is launched inside the sandbox and
driven over ``127.0.0.1:{port}`` (the sandbox shares the host net
namespace). Config and credentials are injected entirely through env
(``OPENCODE_CONFIG_CONTENT`` / ``OPENCODE_AUTH_CONTENT``) so nothing is
written into the workspace; the per-run opencode data dir is bound from
host scratch so ``opencode --session <id>`` can resume later.

The driver: wait for ``/global/health`` → create a session → POST the
prompt to ``/session/{id}/prompt_async`` → consume the ``/event`` SSE
stream, translating to canonical transcript events → finish on
``session.idle``. Cancellation aborts the session and tears the server
down. Token usage / cost come off the assistant message info.

Protocol verified against opencode 1.16.2; see ``docs/backends.md``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
from pathlib import Path
from typing import Optional

from nightdesk.backends.base import (
    Assignment,
    Backend,
    HttpTransport,
    LaunchContext,
    LaunchPlan,
    Mount,
    ResumeDescriptor,
)
from nightdesk.backends import opencode_config as ocfg
from nightdesk.backends.opencode_translate import new_state, translate_event
from nightdesk.domain import backend_capabilities as bc
from nightdesk.worker.executor import ExecutionRequest, ExecutionResult


log = logging.getLogger(__name__)

SANDBOX_OPENCODE_BIN = "/sandbox-bin/opencode"
SANDBOX_OPENCODE_DATA = "/sandbox-opencode-data"


def _resolve_binary(spec) -> str:
    cfg = getattr(spec, "backend_config", None) or {}
    return (
        cfg.get("opencode_binary_path")
        or shutil.which("opencode")
        or os.path.expanduser("~/.opencode/bin/opencode")
    )


class OpencodeBackend(Backend):
    descriptor = bc.OPENCODE
    wants_http = True

    def prepare_launch(self, ctx: LaunchContext) -> LaunchPlan:
        binary = _resolve_binary(ctx.spec)
        data_dir = ctx.scratch_root / "opencode-data"
        password = secrets.token_urlsafe(24)

        endpoints = dict(ctx.endpoints)
        assignments = dict(ctx.model_assignments)
        if not assignments and ctx.endpoint is not None:
            # Legacy fallback: no assignments were resolved (pre-provider
            # profiles). Fill both model and small_model from the single
            # legacy resolve_model path, mirroring the old single-block
            # behaviour.
            legacy_model = ocfg.resolve_model(ctx.spec, ctx.endpoint)
            if legacy_model:
                legacy_assignment = Assignment(ctx.endpoint.id, legacy_model)
                assignments = {"primary": legacy_assignment, "small_model": legacy_assignment}
            endpoints.setdefault(ctx.endpoint.id, ctx.endpoint)

        config = ocfg.render_config(ctx.spec, endpoints, assignments)
        auth = ocfg.render_auth(endpoints)

        primary_assignment = assignments.get("primary")
        model = (
            ocfg.model_str(primary_assignment) if primary_assignment is not None else None
        )

        env: dict[str, str] = {
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "XDG_DATA_HOME": SANDBOX_OPENCODE_DATA,
            "OPENCODE_SERVER_PASSWORD": password,
            # Fail-closed network/footprint posture for an unattended run.
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_SHARE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",  # repo can't inject config
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",     # don't pick up host CLAUDE.md
        }
        # The config document embeds provider apiKey values and the password
        # gates the local HTTP server — both are credential-derived.
        secret_env_keys = {"OPENCODE_CONFIG_CONTENT", "OPENCODE_SERVER_PASSWORD"}
        if auth is not None:
            env["OPENCODE_AUTH_CONTENT"] = json.dumps(auth)
            secret_env_keys.add("OPENCODE_AUTH_CONTENT")

        ctx.backend_state.update({
            "password": password,
            "data_dir": str(data_dir),
            "model": model,
        })

        return LaunchPlan(
            cmd=[
                SANDBOX_OPENCODE_BIN, "serve",
                # Under dry_run ``http_port`` may be unset (no port needed
                # for a preview render) — placeholder 0 keeps the argv shape
                # honest without requiring a real allocation.
                "--port", str(ctx.http_port or 0),
                "--hostname", "127.0.0.1",
                "--pure",
            ],
            env=env,
            transport=HttpTransport(port=ctx.http_port or 0, ready_path="/global/health"),
            mounts=[
                Mount(host=binary, sandbox=SANDBOX_OPENCODE_BIN, mode="ro"),
                Mount(host=str(data_dir), sandbox=SANDBOX_OPENCODE_DATA, mode="rw"),
            ],
            needs_claude_binary=False,
            secret_env_keys=frozenset(secret_env_keys),
        )

    async def execute(self, req: ExecutionRequest) -> ExecutionResult:
        from nightdesk.backends.opencode_driver import drive_opencode
        return await drive_opencode(req)

    def resume_descriptor(self, run) -> Optional[ResumeDescriptor]:
        ref = getattr(run, "session_ref", None) or {}
        sid = ref.get("session_id")
        worktree = getattr(run, "worktree_path", None)
        if not sid or not worktree:
            return None
        # The per-run data dir is gone after teardown; resume re-points
        # XDG_DATA_HOME at the published host scratch if the user kept it.
        data_dir = ref.get("data_dir")
        prefix = f"XDG_DATA_HOME={data_dir} " if data_dir else ""
        command = f"cd {worktree} && {prefix}opencode --session {sid}"
        return ResumeDescriptor(backend=self.code, label="opencode", command=command)
