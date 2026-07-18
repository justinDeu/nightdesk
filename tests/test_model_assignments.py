"""Unit tests for ``compute_model_assignments``, the pure function the
worker uses to resolve the (partial) slot -> Assignment map before launch.

See ``docs/design/providers-and-endpoints.md``, "The rendering contract" and
"The CC alias-escape pitfall, and the unpinned state".
"""
from __future__ import annotations

from nightdesk.backends import Assignment
from nightdesk.domain import backend_capabilities as bc
from nightdesk.domain.providers import ResolvedEndpoint
from nightdesk.worker.run_one import compute_model_assignments


def _ep(**kw) -> ResolvedEndpoint:
    base = dict(
        id="ep1", label="ep", protocol_kind="anthropic_compat",
        base_url=None, credential=None, credential_source="api_key",
    )
    base.update(kw)
    return ResolvedEndpoint(**base)


def test_full_pin_on_compat_endpoint_with_default_model():
    ep = _ep(protocol_kind="anthropic_compat")
    result = compute_model_assignments(
        bc.CLAUDE_SDK, {}, primary=ep, default_model="glm-5.2",
    )
    assert result == {
        "primary": Assignment("ep1", "glm-5.2"),
        "opus_alias": Assignment("ep1", "glm-5.2"),
        "sonnet_alias": Assignment("ep1", "glm-5.2"),
        "haiku_alias": Assignment("ep1", "glm-5.2"),
        "small_fast": Assignment("ep1", "glm-5.2"),
        "subagent": Assignment("ep1", "glm-5.2"),
    }


def test_unpinned_on_first_party_endpoint_with_no_overrides():
    ep = _ep(protocol_kind="anthropic")
    result = compute_model_assignments(
        bc.CLAUDE_SDK, {}, primary=ep, default_model=None,
    )
    assert result == {}


def test_first_party_endpoint_stays_unpinned_even_with_default_model():
    """Full-pin only fires on *_compat protocols, never first-party ones."""
    ep = _ep(protocol_kind="anthropic")
    result = compute_model_assignments(
        bc.CLAUDE_SDK, {}, primary=ep, default_model="claude-opus-4-6",
    )
    assert result == {}


def test_no_primary_endpoint_stays_unpinned():
    result = compute_model_assignments(
        bc.CLAUDE_SDK, {}, primary=None, default_model="glm-5.2",
    )
    assert result == {}


def test_per_slot_override_wins_over_full_pin():
    ep = _ep(protocol_kind="anthropic_compat")
    result = compute_model_assignments(
        bc.CLAUDE_SDK, {"primary": "glm-4.5"}, primary=ep, default_model="glm-4.5-flash",
    )
    assert result["primary"] == Assignment("ep1", "glm-4.5")
    assert result["haiku_alias"] == Assignment("ep1", "glm-4.5-flash")


def test_per_slot_override_without_full_pin():
    """A static override still applies even when full-pin doesn't fire
    (first-party endpoint, no default_model)."""
    ep = _ep(protocol_kind="anthropic")
    result = compute_model_assignments(
        bc.CLAUDE_SDK, {"primary": "claude-haiku-5"}, primary=ep, default_model=None,
    )
    assert result == {"primary": Assignment("ep1", "claude-haiku-5")}


def test_opencode_agent_slot_own_endpoint_and_model():
    primary_ep = _ep(id="ep-primary", protocol_kind="openai_codex")
    backend_config = {
        "agents": [
            {"name": "researcher", "endpoint_id": "ep-zai", "model": "glm-5.2"},
        ],
    }
    result = compute_model_assignments(
        bc.OPENCODE, backend_config, primary=primary_ep, default_model="gpt-5.4",
    )
    assert result["agent:researcher"] == Assignment("ep-zai", "glm-5.2")


def test_opencode_agent_slot_inherits_primary_when_unset():
    primary_ep = _ep(id="ep-primary", protocol_kind="openai_compat")
    backend_config = {"agents": [{"name": "researcher"}]}
    result = compute_model_assignments(
        bc.OPENCODE, backend_config, primary=primary_ep, default_model="glm-5.2",
    )
    # Primary is compat + default_model set, so full-pin fills "primary" first;
    # the agent slot with no explicit model/endpoint inherits both.
    assert result["primary"] == Assignment("ep-primary", "glm-5.2")
    assert result["agent:researcher"] == Assignment("ep-primary", "glm-5.2")


def test_opencode_agent_slot_falls_back_to_default_model_when_no_primary_endpoint():
    # No primary endpoint at all means full-pin never fires (regardless of
    # alias_native_protocols), so the agent slot's own model, unset, falls
    # back straight to default_model.
    backend_config = {"agents": [{"name": "researcher", "endpoint_id": "ep-zai"}]}
    result = compute_model_assignments(
        bc.OPENCODE, backend_config, primary=None, default_model="gpt-5.4",
    )
    assert "primary" not in result
    assert result["agent:researcher"] == Assignment("ep-zai", "gpt-5.4")


def test_opencode_pins_on_openai_codex_endpoint():
    """opencode has no alias_native_protocols, so it full-pins even on
    protocol kinds that stay unpinned for claude_sdk (e.g. openai_codex)."""
    ep = _ep(id="ep-codex", protocol_kind="openai_codex")
    result = compute_model_assignments(
        bc.OPENCODE, {}, primary=ep, default_model="gpt-5.6-sol",
    )
    assert result["primary"] == Assignment("ep-codex", "gpt-5.6-sol")
    assert result["small_model"] == Assignment("ep-codex", "gpt-5.6-sol")


def test_claude_sdk_anthropic_compat_still_pins():
    """Regression guard: the alias_native_protocols gate must not widen
    unpinning beyond first-party anthropic for claude_sdk."""
    ep = _ep(protocol_kind="anthropic_compat")
    result = compute_model_assignments(
        bc.CLAUDE_SDK, {}, primary=ep, default_model="glm-5.2",
    )
    assert result["primary"] == Assignment("ep1", "glm-5.2")


def test_claude_sdk_first_party_anthropic_stays_unpinned():
    """claude_sdk declares anthropic as alias_native, so first-party stays
    unpinned even with a default_model set (CC's own aliases resolve it)."""
    ep = _ep(protocol_kind="anthropic")
    result = compute_model_assignments(
        bc.CLAUDE_SDK, {}, primary=ep, default_model="claude-opus-4-6",
    )
    assert result == {}


def test_endpoint_default_model_fallback_feeds_full_pin():
    """The caller computes default_model = profile.default_model or
    primary.default_model; verify that fallback flows through full-pin."""
    ep = _ep(protocol_kind="anthropic_compat", default_model="glm-4.5")
    profile_default_model = None
    effective_default = profile_default_model or ep.default_model
    result = compute_model_assignments(
        bc.CLAUDE_SDK, {}, primary=ep, default_model=effective_default,
    )
    assert result["primary"] == Assignment("ep1", "glm-4.5")
