"""Tests for the backend capability descriptors.

These pin the contract the profile editor and effective-config preview lean
on: which field groups each backend consumes, which are shared, and which are
inert for a given backend.
"""
from __future__ import annotations

from nightdesk.domain import backend_capabilities as bc


def test_known_backends_are_described_and_enabled():
    codes = {b.code for b in bc.all_capabilities()}
    assert codes == {"claude_sdk", "omp_rpc"}
    assert bc.enabled_backends() == {"claude_sdk", "omp_rpc"}
    # Dropdown choices are (code, label, enabled) and stay in sync.
    choices = dict((c, (label, on)) for c, label, on in bc.backend_choices())
    assert choices["claude_sdk"] == ("Claude Code", True)
    assert choices["omp_rpc"][1] is True


def test_shared_groups_are_common_to_every_backend():
    shared = set(bc.SHARED_GROUP_KEYS)
    # Exact shared surface: silent additions (e.g. a new group accidentally
    # marked shared=True) must fail this test rather than pass silently.
    assert shared == {"filesystem", "tools", "network", "env", "system_prompt", "run_token_scopes"}
    for cap in bc.all_capabilities():
        assert shared <= set(cap.group_keys), cap.code
        # Shared groups are never reported as backend-specific or inert.
        assert not (set(g.key for g in cap.specific_groups) & shared)
        assert not (set(g.key for g in cap.inert_groups) & shared)


def test_claude_sdk_specific_and_inert_groups():
    cap = bc.get_capability("claude_sdk")
    specific = {g.key for g in cap.specific_groups}
    assert specific == {
        "claude_auth", "claude_binary", "permission_mode",
        "claude_models", "claude_behavior",
    }
    # OMP connection is meaningless for Claude → inert.
    assert {g.key for g in cap.inert_groups} == {"omp_connection"}
    assert cap.consumes("claude_auth")
    assert not cap.consumes("omp_connection")


def test_omp_rpc_specific_and_inert_groups():
    cap = bc.get_capability("omp_rpc")
    assert {g.key for g in cap.specific_groups} == {"omp_connection"}
    # All the Claude-only knobs are inert for OMP/RPC.
    assert {g.key for g in cap.inert_groups} == {
        "claude_auth", "claude_binary", "permission_mode",
        "claude_models", "claude_behavior",
    }
    assert cap.consumes("omp_connection")
    assert not cap.consumes("permission_mode")


def test_unknown_backend_falls_back_to_default():
    assert bc.get_capability("nope") is None
    assert bc.get_capability(None) is None
    # capability_or_default never returns None and shared groups still apply.
    cap = bc.capability_or_default("nope")
    assert cap.code == bc.DEFAULT_BACKEND
    assert bc.consumes("nope", "filesystem")  # shared, via the default
    assert not bc.consumes("nope", "omp_connection")


def test_every_group_key_resolves_to_a_field_group():
    for cap in bc.all_capabilities():
        for key in cap.group_keys:
            assert key in bc.FIELD_GROUPS, key
            assert bc.FIELD_GROUPS[key].label


def test_executor_registry_covers_enabled_backends():
    """Every selectable backend must have a registered executor so a saved
    profile can actually be dispatched (even if the executor is a stub)."""
    from nightdesk.worker.backends import available_backends

    registered = set(available_backends())
    assert bc.enabled_backends() <= registered
