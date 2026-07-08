"""Tests for the backend capability descriptors.

These pin the contract the profile editor and effective-config preview lean
on: which field groups each backend consumes, which are shared, and which are
inert for a given backend, plus the run-time capability declarations.
"""
from __future__ import annotations

from nightdesk.domain import backend_capabilities as bc


def test_known_backends_are_described_and_enabled():
    codes = {b.code for b in bc.all_capabilities()}
    assert codes == {"claude_sdk", "opencode"}
    assert bc.enabled_backends() == {"claude_sdk", "opencode"}
    choices = dict((c, (label, on)) for c, label, on in bc.backend_choices())
    assert choices["claude_sdk"] == ("Claude Code", True)
    assert choices["opencode"][1] is True


def test_shared_groups_are_common_to_every_backend():
    shared = set(bc.SHARED_GROUP_KEYS)
    assert shared == {"filesystem", "tools", "network", "env", "system_prompt", "run_token_scopes"}
    for cap in bc.all_capabilities():
        assert shared <= set(cap.group_keys), cap.code
        assert not (set(g.key for g in cap.specific_groups) & shared)
        assert not (set(g.key for g in cap.inert_groups) & shared)


def test_claude_sdk_specific_and_inert_groups():
    cap = bc.get_capability("claude_sdk")
    specific = {g.key for g in cap.specific_groups}
    assert specific == {
        "provider", "claude_auth", "claude_binary", "permission_mode",
        "claude_models", "claude_behavior",
    }
    # Claude consumes every described group, so nothing is inert.
    assert {g.key for g in cap.inert_groups} == set()
    assert cap.consumes("claude_auth")
    assert cap.consumes("provider")


def test_opencode_specific_and_inert_groups():
    cap = bc.get_capability("opencode")
    assert {g.key for g in cap.specific_groups} == {"provider"}
    # Claude-only knobs are inert for opencode.
    assert {g.key for g in cap.inert_groups} == {
        "claude_auth", "claude_binary", "permission_mode",
        "claude_models", "claude_behavior",
    }
    assert cap.consumes("provider")
    assert not cap.consumes("permission_mode")


def test_runtime_capabilities_are_declared():
    claude = bc.get_capability("claude_sdk")
    opencode = bc.get_capability("opencode")
    # Claude provides every capability EXCEPT STEER_INJECT — its one-shot
    # query() has no live input channel into a running turn, so the "claude has
    # every capability" shortcut is deliberately broken here (see mid-run
    # steering). It still provides STEER_QUEUE.
    assert claude.capabilities == frozenset(bc.Capability) - {bc.Capability.STEER_INJECT}
    assert not claude.provides(bc.Capability.STEER_INJECT)
    assert claude.provides(bc.Capability.STEER_QUEUE)
    assert opencode.provides(bc.Capability.COST_USAGE)
    assert opencode.provides(bc.Capability.TOOL_POLICY)
    assert not opencode.provides(bc.Capability.THINKING)
    assert not opencode.provides(bc.Capability.RATE_LIMIT_SIGNAL)


def test_steer_capabilities_gate():
    claude = bc.get_capability("claude_sdk")
    opencode = bc.get_capability("opencode")
    # Both accept a queued follow-up; only opencode injects into the live run.
    assert claude.provides(bc.Capability.STEER_QUEUE)
    assert opencode.provides(bc.Capability.STEER_QUEUE)
    assert opencode.provides(bc.Capability.STEER_INJECT)
    assert not claude.provides(bc.Capability.STEER_INJECT)


def test_provider_requirements():
    claude = bc.get_capability("claude_sdk")
    opencode = bc.get_capability("opencode")
    assert not claude.requires_provider        # falls back to claude_credentials
    assert opencode.requires_provider
    assert "anthropic" in claude.protocol_kinds
    assert "ollama" in opencode.protocol_kinds


def test_unknown_backend_falls_back_to_default():
    assert bc.get_capability("nope") is None
    assert bc.get_capability(None) is None
    cap = bc.capability_or_default("nope")
    assert cap.code == bc.DEFAULT_BACKEND
    assert bc.consumes("nope", "filesystem")
    # Unknown backends fall back to the claude default, which consumes claude_auth.
    assert bc.consumes("nope", "claude_auth")


def test_every_group_key_resolves_to_a_field_group():
    for cap in bc.all_capabilities():
        for key in cap.group_keys:
            assert key in bc.FIELD_GROUPS, key
            assert bc.FIELD_GROUPS[key].label


def test_slots_for_claude_sdk_static_only():
    cap = bc.get_capability("claude_sdk")
    slots = bc.slots_for(cap, {})
    names = [s.name for s in slots]
    assert names == [
        "primary", "opus_alias", "sonnet_alias", "haiku_alias",
        "small_fast", "subagent",
    ]


def test_slots_for_opencode_appends_agent_slots():
    cap = bc.get_capability("opencode")
    backend_config = {"agents": [{"name": "researcher"}, {"name": "writer"}]}
    slots = bc.slots_for(cap, backend_config)
    names = [s.name for s in slots]
    assert names == ["primary", "small_model", "agent:researcher", "agent:writer"]


def test_slots_for_opencode_ignores_agents_without_names():
    cap = bc.get_capability("opencode")
    backend_config = {"agents": [{"name": ""}, {}, {"name": "ok"}]}
    slots = bc.slots_for(cap, backend_config)
    names = [s.name for s in slots]
    assert names == ["primary", "small_model", "agent:ok"]


def test_slots_for_no_agents_key_returns_static_only():
    cap = bc.get_capability("opencode")
    assert [s.name for s in bc.slots_for(cap, {})] == ["primary", "small_model"]


def test_multi_endpoint_flags():
    assert bc.get_capability("claude_sdk").multi_endpoint is False
    assert bc.get_capability("opencode").multi_endpoint is True


def test_executor_registry_covers_enabled_backends():
    """Every selectable backend must have a registered backend so a saved
    profile can actually be dispatched."""
    from nightdesk.worker.backends import available_backends

    registered = set(available_backends())
    assert bc.enabled_backends() <= registered
