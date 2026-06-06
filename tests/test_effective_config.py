"""Merge-semantics and provenance tests for the effective-config resolver."""
import json

from nightdesk.domain.effective_config import (
    DERIVED,
    GLOBAL,
    PROFILE,
    PROJECT,
    TICKET,
    resolve_for_draft,
    resolve_for_ticket,
)
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import create_ticket


def _profile(session, **over):
    fields = dict(
        name="Edit",
        fs_read=["/data"],
        fs_write=["/data"],
        allowed_tools=["Read", "Edit"],
        denied_tools=["WebFetch"],
        network_mode="on",
        network_allowlist=["api.example.com"],
        secret_keys=["MY_KEY"],
        default_model="claude-sonnet",
        backend="claude_sdk",
        permission_mode="acceptEdits",
    )
    fields.update(over)
    return create_profile(session, **fields)


def _items(field):
    return [(c.value, c.source) for c in field.items]


# --- replace-semantics provenance ------------------------------------------


def test_replace_fields_use_profile_when_no_override(session):
    p = _profile(session)
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    fm = resolve_for_ticket(session, t).field_map()
    assert fm["backend"].value == "claude_sdk"
    assert fm["backend"].source == PROFILE
    assert fm["default_model"].value == "claude-sonnet"
    assert fm["default_model"].source == PROFILE
    assert fm["network_mode"].value == "on"
    assert fm["network_mode"].source == PROFILE
    assert fm["permission_mode"].source == PROFILE


def test_replace_field_override_wins_and_is_attributed_to_ticket(session):
    p = _profile(session)
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, source_path="/tmp",
        permission_overrides={"default_model": "claude-opus", "network_mode": "off"},
    )
    fm = resolve_for_ticket(session, t).field_map()
    assert fm["default_model"].value == "claude-opus"
    assert fm["default_model"].source == TICKET
    assert fm["network_mode"].value == "off"
    assert fm["network_mode"].source == TICKET


def test_model_falls_back_to_backend_default_display(session):
    p = _profile(session, default_model=None)
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    fm = resolve_for_ticket(session, t).field_map()
    assert fm["default_model"].value == "(backend default)"
    assert fm["default_model"].source == PROFILE


# --- additive-list provenance ----------------------------------------------


def test_additive_fs_write_merges_profile_ticket_and_derived(session):
    p = _profile(session)
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, source_path="/tmp/work",
        permission_overrides={"fs_write": ["/extra"]},
    )
    fm = resolve_for_ticket(session, t).field_map()
    items = _items(fm["fs_write"])
    assert ("/data", PROFILE) in items
    assert ("/extra", TICKET) in items
    # the working directory becomes a writable mount (derived behavior)
    assert ("/tmp/work", DERIVED) in items


def test_additional_dirs_split_into_read_and_write(session):
    p = _profile(session)
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, source_path="/tmp",
        additional_dirs=[{"path": "/ro", "mode": "ro"}, {"path": "/rw", "mode": "rw"}],
    )
    fm = resolve_for_ticket(session, t).field_map()
    assert ("/ro", TICKET) in _items(fm["fs_read"])
    assert ("/rw", TICKET) in _items(fm["fs_write"])


def test_allowed_tools_and_secret_keys_merge_with_provenance(session):
    p = _profile(session)
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, source_path="/tmp",
        permission_overrides={"allowed_tools": ["Bash"], "secret_keys": ["OTHER"]},
    )
    fm = resolve_for_ticket(session, t).field_map()
    assert ("Read", PROFILE) in _items(fm["allowed_tools"])
    assert ("Bash", TICKET) in _items(fm["allowed_tools"])
    assert ("MY_KEY", PROFILE) in _items(fm["secret_keys"])
    assert ("OTHER", TICKET) in _items(fm["secret_keys"])


def test_merge_dedupes_first_source_wins(session):
    p = _profile(session)
    # Override repeats a profile value; it must keep the profile attribution.
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, source_path="/tmp",
        permission_overrides={"fs_read": ["/data"]},
    )
    fm = resolve_for_ticket(session, t).field_map()
    reads = _items(fm["fs_read"])
    assert reads.count(("/data", PROFILE)) == 1
    assert ("/data", TICKET) not in reads


# --- git-push deny derived rules -------------------------------------------


def test_denied_tools_include_git_push_rules_by_default(session):
    p = _profile(session)
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    fm = resolve_for_ticket(session, t).field_map()
    denied = _items(fm["denied_tools"])
    assert ("WebFetch", PROFILE) in denied
    assert ("Bash(git push)", DERIVED) in denied
    assert ("Bash(git push:*)", DERIVED) in denied


def test_allow_git_push_override_removes_derived_push_rules(session):
    p = _profile(session)
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, source_path="/tmp",
        permission_overrides={"allow_git_push": True},
    )
    fm = resolve_for_ticket(session, t).field_map()
    denied_values = [c.value for c in fm["denied_tools"].items]
    assert "Bash(git push)" not in denied_values


# --- toolchains & PATH ------------------------------------------------------


def test_toolchain_project_default_and_ticket_enable_provenance(session):
    p = _profile(session)
    proj = create_project(
        session, name="Proj", source_path="/tmp/proj",
        default_toolchains=["user-python-tools"],
    )
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
        toolchain_overrides={"enable": ["rust-user-tools"], "disable": [], "extra_paths": []},
    )
    fm = resolve_for_ticket(session, t).field_map()
    tc = _items(fm["toolchains"])
    assert ("user-python-tools", PROJECT) in tc
    assert ("rust-user-tools", TICKET) in tc


def test_toolchain_disable_removes_from_selection(session):
    p = _profile(session)
    proj = create_project(
        session, name="Proj", source_path="/tmp/proj",
        default_toolchains=["user-python-tools"],
    )
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
        toolchain_overrides={"enable": [], "disable": ["user-python-tools"], "extra_paths": []},
    )
    fm = resolve_for_ticket(session, t).field_map()
    assert "user-python-tools" not in [c.value for c in fm["toolchains"].items]


def test_extra_paths_project_vs_ticket_provenance(session):
    p = _profile(session)
    proj = create_project(
        session, name="Proj", source_path="/tmp/proj",
        default_tool_paths=["/opt/proj-tools"],
    )
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
        toolchain_overrides={"enable": [], "disable": [], "extra_paths": ["/opt/ticket-tools"]},
    )
    fm = resolve_for_ticket(session, t).field_map()
    extra = _items(fm["extra_paths"])
    assert ("/opt/proj-tools", PROJECT) in extra
    assert ("/opt/ticket-tools", TICKET) in extra


def test_resolved_tool_paths_are_derived(session):
    p = _profile(session)
    proj = create_project(
        session, name="Proj", source_path="/tmp/proj",
        default_tool_paths=["/opt/proj-tools"],
    )
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
    )
    fm = resolve_for_ticket(session, t).field_map()
    resolved = fm["resolved_tool_paths"]
    assert resolved.source == DERIVED
    assert ("/opt/proj-tools", DERIVED) in _items(resolved)


# --- workspace / project provenance ----------------------------------------


def test_source_path_attributed_to_project_when_matching_default(session):
    p = _profile(session)
    proj = create_project(session, name="Proj", source_path="/tmp/proj")
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
    )
    fm = resolve_for_ticket(session, t).field_map()
    assert fm["source_path"].value == "/tmp/proj"
    assert fm["source_path"].source == PROJECT
    assert fm["project"].value == "Proj"


def test_git_worktree_group_present_with_base_ref_provenance(session):
    p = _profile(session)
    proj = create_project(
        session, name="Proj", source_path="/tmp/proj",
        default_workspace_mode="git_worktree", default_base_ref="main",
    )
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj", workspace_mode="git_worktree",
    )
    fm = resolve_for_ticket(session, t).field_map()
    assert fm["workspace_mode"].value == "git_worktree"
    assert "git_worktree" in fm
    assert fm["base_ref"].value == "main"
    assert fm["base_ref"].source == PROJECT
    assert fm["git_worktree"].source == DERIVED


# --- validation (shallow) ---------------------------------------------------


def test_issue_when_source_path_missing(session):
    p = _profile(session)
    # A draft with no primary workspace source path.
    eff = resolve_for_draft(session, {"profile_id": p.id})
    assert any("source path" in issue.lower() for issue in eff.issues)


def test_issue_on_unknown_backend(session):
    p = _profile(session, backend="totally-made-up")
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t)
    assert any("unknown backend" in issue.lower() for issue in eff.issues)


def test_issue_on_non_absolute_source_path(session):
    p = _profile(session)
    eff = resolve_for_draft(session, {"profile_id": p.id, "source_path": "relative/path"})
    assert any("not absolute" in issue.lower() for issue in eff.issues)


def test_no_profile_is_flagged(session):
    eff = resolve_for_draft(session, {"source_path": "/tmp"})
    assert any("no profile" in issue.lower() for issue in eff.issues)


# --- draft parity & serialization ------------------------------------------


def test_draft_applies_project_defaults(session):
    p = _profile(session)
    proj = create_project(
        session, name="Proj", source_path="/tmp/proj",
        default_tool_paths=["/opt/proj-tools"],
    )
    eff = resolve_for_draft(session, {"profile_id": p.id, "project_id": proj.id})
    fm = eff.field_map()
    assert fm["source_path"].value == "/tmp/proj"
    assert ("/opt/proj-tools", PROJECT) in _items(fm["extra_paths"])


def test_as_dict_is_json_serializable_and_includes_source_labels(session):
    p = _profile(session)
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    data = resolve_for_ticket(session, t).as_dict()
    blob = json.dumps(data)  # must not raise
    assert '"source_label"' in blob
    assert data["groups"]
    # every field carries a provenance source
    for group in data["groups"]:
        for field in group["fields"]:
            assert field["source"] in {GLOBAL, PROJECT, PROFILE, TICKET, DERIVED}
