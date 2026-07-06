"""Merge-semantics and provenance tests for the effective-config resolver."""
import json

import pytest

from nightdesk.domain.effective_config import (
    DERIVED,
    GLOBAL,
    MASKED_VALUE,
    PROFILE,
    PROJECT,
    TICKET,
    resolve_for_draft,
    resolve_for_ticket,
)
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.providers import create_endpoint, create_provider
from nightdesk.domain.tickets import create_ticket


@pytest.fixture
def box() -> ProfileSecretBox:
    return ProfileSecretBox("test-bearer-token")


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


def test_toolchain_inherited_from_project_applied_at_creation(session):
    """apply_project_defaults copies project toolchains into the ticket's enable
    list. The effective config resolver still attributes them to PROJECT, not
    TICKET, because it compares against the project's defaults directly."""
    p = _profile(session)
    proj = create_project(
        session, name="InheritProj", source_path="/tmp/inherit",
        default_toolchains=["user-python-tools", "rust-user-tools"],
    )
    # No explicit toolchain_overrides — defaults are applied.
    t = create_ticket(
        session, title="Inherited", prompt="x", profile_id=p.id,
        project_id=proj.id, source_path="/tmp/inherit",
    )
    # apply_project_defaults filled in the overrides.
    assert t.toolchain_overrides["enable"] == ["user-python-tools", "rust-user-tools"]
    # The resolver attributes both to PROJECT (not TICKET).
    fm = resolve_for_ticket(session, t).field_map()
    tc = _items(fm["toolchains"])
    assert ("user-python-tools", PROJECT) in tc
    assert ("rust-user-tools", PROJECT) in tc
    # No TICKET-sourced toolchains because both came from the project.
    assert not any(s == TICKET for _, s in tc)


def test_toolchain_mixed_project_default_and_explicit(session):
    """When a ticket inherits some presets from the project and explicitly
    enables others, provenance distinguishes the two."""
    p = _profile(session)
    proj = create_project(
        session, name="MixedProj", source_path="/tmp/mixed",
        default_toolchains=["user-python-tools"],
    )
    t = create_ticket(
        session, title="Mixed", prompt="x", profile_id=p.id,
        project_id=proj.id, source_path="/tmp/mixed",
        toolchain_overrides={
            "enable": ["user-python-tools", "rust-user-tools"],
            "disable": [],
            "extra_paths": [],
        },
    )
    fm = resolve_for_ticket(session, t).field_map()
    tc = _items(fm["toolchains"])
    # user-python-tools is in both project and enable → attributed to PROJECT.
    assert ("user-python-tools", PROJECT) in tc
    # rust-user-tools is only in enable → attributed to TICKET.
    assert ("rust-user-tools", TICKET) in tc


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


# --- item 1: toolchain provenance correctness ----------------------------------
# (badge logic lives in the template, but the resolver is the ground truth)


def test_toolchain_inherited_active_without_explicit_enable(session):
    """A preset in project.default_toolchains is active at runtime even when it
    is NOT in the ticket's explicit enable list. The resolver must attribute it
    to PROJECT so the template can render the Inherited badge truthfully."""
    p = _profile(session)
    proj = create_project(
        session, name="Proj", source_path="/tmp/proj",
        default_toolchains=["user-python-tools"],
    )
    # Ticket has NO toolchain_overrides: apply_project_defaults puts the
    # project default into the enable list.  We simulate the case where a
    # ticket was created before the project set a default by passing
    # toolchain_overrides={"enable": [], "disable": [], "extra_paths": []}
    # explicitly (a "no overrides" state that still merges with project defaults).
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
        toolchain_overrides={"enable": [], "disable": [], "extra_paths": []},
    )
    fm = resolve_for_ticket(session, t).field_map()
    tc = _items(fm["toolchains"])
    # The preset comes from the project default even when NOT in the enable list.
    assert ("user-python-tools", PROJECT) in tc


def test_toolchain_inherited_preset_with_disable_not_active(session):
    """When a project-default preset is explicitly disabled on the ticket, it
    must NOT appear in toolchains (it's neither inherited nor active)."""
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
    tc_values = [c.value for c in fm["toolchains"].items]
    assert "user-python-tools" not in tc_values


# --- item 2: git-metadata rw mounts in fs_write --------------------------------


def test_git_meta_writes_appear_for_worktree_workspace(tmp_path, session):
    """For a git_worktree workspace whose source_path is a real git repo, the
    repo's git-common-dir is included as a DERIVED contribution to fs_write."""
    import subprocess as _sp
    # Create a minimal git repo in tmp_path so git rev-parse succeeds.
    _sp.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _sp.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
        env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    p = _profile(session)
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, source_path=str(tmp_path),
        workspace_mode="git_worktree",
    )
    # Force the workspace kind so the resolver sees it as git_worktree.
    for ws in t.workspaces or []:
        ws.kind = "git_worktree"
    fm = resolve_for_ticket(session, t).field_map()
    write_paths = [c.value for c in fm["fs_write"].items if c.source == DERIVED]
    # At least one DERIVED entry should be the git dir (under tmp_path).
    assert any(str(tmp_path) in p for p in write_paths), (
        f"No git-metadata path found in fs_write derived entries: {write_paths}"
    )


def test_no_git_meta_writes_for_directory_workspace(session):
    """A plain directory workspace must not add git-metadata to fs_write."""
    p = _profile(session)
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, source_path="/tmp/work",
    )
    fm = resolve_for_ticket(session, t).field_map()
    # Derived entries are the workspace path only; no extra git-metadata paths.
    derived = [c.value for c in fm["fs_write"].items if c.source == DERIVED]
    # /tmp/work is the only derived entry for a plain directory ticket.
    assert derived == ["/tmp/work"]


# --- item 5: profile field in backend group ------------------------------------


def test_profile_field_present_with_ticket_provenance(session):
    """The resolved config must expose the profile's name as a TICKET-provenance
    field so the panel answers 'which profile is responsible'."""
    p = _profile(session, name="MyProfile")
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    fm = resolve_for_ticket(session, t).field_map()
    assert "profile" in fm
    assert fm["profile"].value == "MyProfile"
    assert fm["profile"].source == TICKET


def test_profile_field_none_when_no_profile(session):
    """When no profile is selected the profile field shows '(none)'."""
    eff = resolve_for_draft(session, {"source_path": "/tmp"})
    fm = eff.field_map()
    assert "profile" in fm
    assert fm["profile"].value == "(none)"
    assert fm["profile"].source == DERIVED


# --- item 7: deeper credentials / secret_keys validation -----------------------


def test_issue_credentials_missing_source(session):
    """A credentials blob without a 'source' key is flagged as an issue."""
    # Store a plain-JSON credentials blob (not Fernet-encrypted) so the
    # preview's lenient json.loads check can inspect the structure.
    p = create_profile(
        session, name="NoCreds",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None, backend="claude_sdk",
        claude_credentials=json.dumps({}),
    )
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t)
    assert any("missing a 'source'" in issue for issue in eff.issues)


def test_issue_credentials_empty_api_key(session):
    """A credentials blob with source='api_key' and empty value is flagged."""
    p = create_profile(
        session, name="EmptyKey",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None, backend="claude_sdk",
        claude_credentials=json.dumps({"source": "api_key", "value": ""}),
    )
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t)
    assert any("empty value" in issue for issue in eff.issues)


def test_issue_secret_keys_blank_entry(session):
    """A blank entry in profile.secret_keys is flagged."""
    p = create_profile(
        session, name="BlankKey",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[],
        secret_keys=["GOOD_KEY", ""],
        default_model=None, backend="claude_sdk",
    )
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t)
    assert any("blank entry" in issue.lower() for issue in eff.issues)


def test_no_spurious_issues_for_valid_encrypted_credentials(session):
    """A real Fernet-encrypted credentials blob must not trigger a spurious
    'missing source' issue — the json.loads check must fail silently."""
    # Simulate a Fernet token (not valid JSON) by using a clearly-not-JSON string.
    p = create_profile(
        session, name="EncCreds",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None, backend="claude_sdk",
        # A Fernet token is base64url; it's not valid JSON, so json.loads raises.
        claude_credentials="gAAAAABnotavalidfernettoken==",
    )
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t)
    # No credential-structure issue must appear — the Fernet token check is silent.
    assert not any("source" in issue and "credential" in issue.lower() for issue in eff.issues)


# --- Launch plan group (dry-run render + credential masking) ---------------


def _launch_group(eff):
    return next((g for g in eff.groups if g.title == "Launch plan"), None)


def test_legacy_profile_without_endpoint_has_no_launch_group(session, box):
    """Regression: a profile with no endpoint attached must not gain a
    Launch plan group, and every pre-existing group stays untouched."""
    p = _profile(session)
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t, box)
    assert _launch_group(eff) is None
    titles = [g.title for g in eff.groups]
    assert titles == [
        "Project & workspace", "Execution backend", "Filesystem reach",
        "Tools", "Network & secrets", "Toolchain & PATH",
    ]


def test_claude_code_compat_endpoint_shows_masked_credential(session, box):
    provider = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=provider.id, label="Anthropic-compatible",
        protocol_kind="anthropic_compat", base_url="https://api.z.ai/api/anthropic",
        credential_source="api_key", credential=box.encrypt("secret-zai-key"),
        default_model="glm-5.2",
    )
    p = _profile(session, endpoint_id=ep.id, default_model="glm-5.2")
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t, box)
    group = _launch_group(eff)
    assert group is not None
    fm = {f.key: f for f in group.fields}
    assert fm["launch_env_ANTHROPIC_BASE_URL"].value == "https://api.z.ai/api/anthropic"
    assert fm["launch_env_ANTHROPIC_MODEL"].value == "glm-5.2"
    assert fm["launch_env_ANTHROPIC_API_KEY"].value == MASKED_VALUE
    assert "secret-zai-key" not in json.dumps(eff.as_dict())


def test_claude_subscription_endpoint_masks_auth_token(session, box, tmp_path):
    creds_file = tmp_path / "creds.json"
    creds_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-abc-123"}}))
    provider = create_provider(session, name="Anthropic", vendor="anthropic")
    ep = create_endpoint(
        session, provider_id=provider.id, label="Subscription",
        protocol_kind="anthropic", credential_source="subscription_file",
        credential=box.encrypt(str(creds_file)), harness_lock="claude_sdk",
    )
    p = _profile(session, endpoint_id=ep.id, default_model=None)
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t, box)
    group = _launch_group(eff)
    assert group is not None
    fm = {f.key: f for f in group.fields}
    assert fm["launch_env_ANTHROPIC_AUTH_TOKEN"].value == MASKED_VALUE
    assert "tok-abc-123" not in json.dumps(eff.as_dict())


def test_opencode_profile_masks_config_and_auth_content(session, box):
    provider = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=provider.id, label="OpenAI-compatible",
        protocol_kind="openai_compat", base_url="https://api.z.ai/api/paas/v4",
        credential_source="api_key", credential=box.encrypt("secret-zai-openai-key"),
        default_model="glm-5.2",
    )
    p = _profile(
        session, endpoint_id=ep.id, backend="opencode", default_model="glm-5.2",
    )
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t, box)
    group = _launch_group(eff)
    assert group is not None
    fm = {f.key: f for f in group.fields}
    assert fm["launch_env_OPENCODE_CONFIG_CONTENT"].value == MASKED_VALUE
    assert fm["launch_env_OPENCODE_SERVER_PASSWORD"].value == MASKED_VALUE
    assert "secret-zai-openai-key" not in json.dumps(eff.as_dict())


def test_extra_env_values_masked_as_vendor_quirks(session, box):
    provider = create_provider(session, name="Requesty", vendor="requesty")
    ep = create_endpoint(
        session, provider_id=provider.id, label="Anthropic-compatible",
        protocol_kind="anthropic_compat", base_url="https://router.requesty.ai",
        credential_source="none",
        extra=box.encrypt({"env": {"X-ROUTING-TOKEN": "route-secret-789"}}),
    )
    p = _profile(session, endpoint_id=ep.id, default_model="glm-5.2")
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t, box)
    group = _launch_group(eff)
    assert group is not None
    fm = {f.key: f for f in group.fields}
    assert fm["launch_env_X-ROUTING-TOKEN"].value == MASKED_VALUE
    assert "route-secret-789" not in json.dumps(eff.as_dict())


def test_incompatible_endpoint_yields_issue_not_error(session, box):
    """A CC profile pointed at an endpoint locked to another harness must
    degrade to an issue message, never raise."""
    provider = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=provider.id, label="opencode-only",
        protocol_kind="openai_compat",
        credential_source="none",
    )
    p = _profile(session, endpoint_id=ep.id, backend="claude_sdk", default_model="glm-5.2")
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")
    eff = resolve_for_ticket(session, t, box)
    assert any("not compatible" in issue or "does not support" in issue for issue in eff.issues)


def test_launch_plan_dry_run_creates_no_directories(session, box, tmp_path, monkeypatch):
    """Under dry_run, prepare_launch must not touch the filesystem."""
    provider = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=provider.id, label="Anthropic-compatible",
        protocol_kind="anthropic_compat", credential_source="api_key",
        credential=box.encrypt("secret-key"), default_model="glm-5.2",
    )
    p = _profile(session, endpoint_id=ep.id, default_model="glm-5.2")
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")

    before = set(tmp_path.iterdir())
    resolve_for_ticket(session, t, box)
    after = set(tmp_path.iterdir())
    assert before == after


def test_bare_resolve_input_without_session_skips_launch_group(session):
    """No-session case in a bare ResolveInput (e.g. constructed directly
    without a live session) must not crash and simply omits the group."""
    from nightdesk.domain.effective_config import ResolveInput, resolve_effective_config

    p = _profile(session)
    inp = ResolveInput(
        profile=p, project=None, config=None, permission_overrides=None,
        toolchain_overrides=None, additional_dirs=[], workspaces=[],
        source_path="/tmp", workspace_mode="directory", base_ref=None,
        session=None, secret_box=None,
    )
    eff = resolve_effective_config(inp)
    assert _launch_group(eff) is None
