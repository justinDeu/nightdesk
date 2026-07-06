"""Contracts for the backend abstraction: registry, descriptors, launch
planning, and the worker's backend-agnostic dispatch surface."""
from __future__ import annotations

from pathlib import Path

import pytest

from nightdesk.backends import (
    Assignment,
    HttpTransport,
    LaunchContext,
    StdioTransport,
    available_backends,
    get_backend,
)
from nightdesk.backends.base import IncompatibleEndpoint
from nightdesk.backends.registry import UnknownBackend
from nightdesk.domain.permissions import PermissionSpec
from nightdesk.domain.providers import ResolvedEndpoint


def _ep(**kw) -> ResolvedEndpoint:
    base = dict(
        id="ep1", label="ep", protocol_kind="anthropic_compat",
        base_url=None, credential=None, credential_source="api_key",
    )
    base.update(kw)
    return ResolvedEndpoint(**base)


def _ctx(spec, endpoint=None, endpoints=None, model_assignments=None,
         http_port=None, tmp=Path("/tmp/nd-scratch")):
    return LaunchContext(
        spec=spec, endpoint=endpoint, run_id="r1", ticket_id="t1",
        workspace_dir=Path("/tmp/ws"), scratch_root=tmp, http_port=http_port,
        endpoints=endpoints or ({endpoint.id: endpoint} if endpoint else {}),
        model_assignments=model_assignments or {},
    )


def test_registry_lists_builtin_backends():
    codes = set(available_backends())
    assert {"claude_sdk", "opencode", "dummy"} <= codes


def test_unknown_backend_raises():
    with pytest.raises(UnknownBackend):
        get_backend("nope")


def test_claude_launch_plan_needs_binary_and_sessions():
    backend = get_backend("claude_sdk")
    assert backend.wants_http is False
    plan = backend.prepare_launch(_ctx(PermissionSpec()))
    assert plan.needs_claude_binary is True
    assert plan.cc_sessions_dir and plan.cc_sessions_dir.endswith("cc-sessions")
    assert "_sdk_runner" in " ".join(plan.cmd)
    assert isinstance(plan.transport, StdioTransport)


def test_claude_legacy_path_when_no_endpoint():
    """None endpoint takes the pre-endpoint legacy path: empty env, letting
    the worker's _build_env supply claude_credentials / ambient auth."""
    backend = get_backend("claude_sdk")
    plan = backend.prepare_launch(_ctx(PermissionSpec()))
    assert plan.env == {}


def test_claude_endpoint_api_key_env():
    backend = get_backend("claude_sdk")
    ep = _ep(base_url="https://z.ai", credential="sk-z")
    plan = backend.prepare_launch(_ctx(PermissionSpec(), endpoint=ep))
    assert plan.env["ANTHROPIC_API_KEY"] == "sk-z"
    assert plan.env["ANTHROPIC_BASE_URL"] == "https://z.ai"


def test_claude_endpoint_subscription_token_extraction():
    backend = get_backend("claude_sdk")
    raw = '{"claudeAiOauth": {"accessToken": "tok-abc"}}'
    ep = _ep(protocol_kind="anthropic", credential=raw,
             credential_source="subscription_file")
    plan = backend.prepare_launch(_ctx(PermissionSpec(), endpoint=ep))
    assert plan.env["ANTHROPIC_AUTH_TOKEN"] == "tok-abc"


def test_claude_endpoint_subscription_top_level_access_token():
    backend = get_backend("claude_sdk")
    raw = '{"accessToken": "tok-xyz"}'
    ep = _ep(protocol_kind="anthropic", credential=raw,
             credential_source="subscription_file")
    plan = backend.prepare_launch(_ctx(PermissionSpec(), endpoint=ep))
    assert plan.env["ANTHROPIC_AUTH_TOKEN"] == "tok-xyz"


def test_claude_endpoint_subscription_malformed_json_emits_nothing():
    backend = get_backend("claude_sdk")
    ep = _ep(protocol_kind="anthropic", credential="not-json-at-all",
             credential_source="subscription_file")
    plan = backend.prepare_launch(_ctx(PermissionSpec(), endpoint=ep))
    assert "ANTHROPIC_AUTH_TOKEN" not in plan.env
    assert "ANTHROPIC_API_KEY" not in plan.env


def test_claude_endpoint_extra_env_wins():
    backend = get_backend("claude_sdk")
    ep = _ep(credential="sk-z", extra={"env": {"ANTHROPIC_API_KEY": "sk-override"}})
    plan = backend.prepare_launch(_ctx(PermissionSpec(), endpoint=ep))
    assert plan.env["ANTHROPIC_API_KEY"] == "sk-override"


def test_claude_endpoint_model_assignments_render_slot_env():
    backend = get_backend("claude_sdk")
    ep = _ep(credential="sk-z")
    assignments = {
        "primary": Assignment("ep1", "glm-5.2"),
        "haiku_alias": Assignment("ep1", "glm-4.5"),
    }
    plan = backend.prepare_launch(
        _ctx(PermissionSpec(), endpoint=ep, model_assignments=assignments),
    )
    assert plan.env["ANTHROPIC_MODEL"] == "glm-5.2"
    assert plan.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "glm-4.5"
    # Slots absent from the assignment map emit nothing (unpinned state).
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in plan.env


def test_claude_incompatible_endpoint_raises():
    backend = get_backend("claude_sdk")
    ep = _ep(protocol_kind="openai_codex")
    with pytest.raises(IncompatibleEndpoint):
        backend.prepare_launch(_ctx(PermissionSpec(), endpoint=ep))


def test_opencode_launch_plan_is_http_with_mounts():
    backend = get_backend("opencode")
    assert backend.wants_http is True
    ep = _ep(protocol_kind="openai", credential="k")
    plan = backend.prepare_launch(_ctx(PermissionSpec(), endpoint=ep, http_port=44321))
    assert plan.needs_claude_binary is False
    assert isinstance(plan.transport, HttpTransport)
    assert plan.transport.port == 44321
    assert "serve" in plan.cmd and "44321" in plan.cmd
    # Binary + data-dir mounts present.
    sandboxes = {m.sandbox for m in plan.mounts}
    assert "/sandbox-bin/opencode" in sandboxes
    # Config + password injected via env, no files written.
    assert "OPENCODE_CONFIG_CONTENT" in plan.env
    assert plan.env["OPENCODE_SERVER_PASSWORD"]
    assert plan.env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"


def test_resume_descriptor_round_trips():
    claude = get_backend("claude_sdk")

    class _Run:
        session_id = "sess-1"
        worktree_path = "/tmp/ws"
        session_ref = {"session_id": "sess-1"}

    rd = claude.resume_descriptor(_Run())
    assert rd is not None and "claude --resume sess-1" in rd.command

    oc = get_backend("opencode")

    class _OcRun:
        worktree_path = "/tmp/ws"
        session_ref = {"session_id": "ses_abc", "data_dir": "/d"}

    rd2 = oc.resume_descriptor(_OcRun())
    assert rd2 is not None and "opencode --session ses_abc" in rd2.command
