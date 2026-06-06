# tests/test_permissions.py
from nightdesk.domain.permissions import PermissionSpec, merge_permissions


def base_profile():
    return PermissionSpec(
        fs_read=["/a"],
        fs_write=["/b"],
        allowed_tools=["Read"],
        denied_tools=["Bash"],
        network_mode="off",
        network_allowlist=[],
        secret_keys=["ONE"],
        default_model=None,
    )


def test_merge_with_no_override_returns_profile_copy():
    p = base_profile()
    out = merge_permissions(p, None)
    assert out == p
    assert out is not p


def test_additive_lists_combine_unique():
    p = base_profile()
    over = {"fs_read": ["/c", "/a"], "allowed_tools": ["Edit"], "secret_keys": ["TWO"]}
    out = merge_permissions(p, over)
    assert sorted(out.fs_read) == ["/a", "/c"]
    assert sorted(out.allowed_tools) == ["Edit", "Read"]
    assert sorted(out.secret_keys) == ["ONE", "TWO"]


def test_denied_tools_are_restrictive_wins():
    p = base_profile()
    over = {"denied_tools": ["WebFetch"]}
    out = merge_permissions(p, over)
    assert sorted(out.denied_tools) == ["Bash", "WebFetch"]


def test_network_mode_replace_wins():
    p = base_profile()
    out = merge_permissions(p, {"network_mode": "open"})
    assert out.network_mode == "open"


def test_default_model_replace_wins():
    p = base_profile()
    out = merge_permissions(p, {"default_model": "claude-opus-4-7"})
    assert out.default_model == "claude-opus-4-7"


def test_allow_git_push_defaults_to_false():
    # The push gate is on by default: a fresh spec does not allow pushing.
    assert PermissionSpec().allow_git_push is False


def test_allow_git_push_override_opts_in():
    p = base_profile()
    assert p.allow_git_push is False
    out = merge_permissions(p, {"allow_git_push": True})
    assert out.allow_git_push is True


def test_allow_git_push_absent_override_keeps_profile_value():
    p = merge_permissions(base_profile(), {"allow_git_push": True})
    # A later override that doesn't mention the flag must not silently reset it.
    out = merge_permissions(p, {"network_mode": "on"})
    assert out.allow_git_push is True
