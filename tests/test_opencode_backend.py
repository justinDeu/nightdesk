"""Unit tests for the opencode backend's pure pieces: config/permission
rendering and SSE event translation."""
from __future__ import annotations

import json

from nightdesk.backends.base import Assignment, LaunchContext
from nightdesk.backends.opencode import OpencodeBackend
from nightdesk.backends.opencode_config import (
    block_id,
    render_auth,
    render_config,
    render_permission,
    resolve_model,
    resolve_provider_ids,
)
from nightdesk.backends.opencode_translate import new_state, translate_event, usage_by_model
from nightdesk.domain.permissions import PermissionSpec
from nightdesk.domain.providers import ResolvedEndpoint


def _ep(**kw):
    base = dict(id="ep1", label="prov", protocol_kind="anthropic_compat",
                base_url="https://api.z.ai/api/anthropic", credential="sk-1",
                credential_source="api_key",
                default_model="glm-4.6", models=["glm-4.6"], provider_name="prov")
    base.update(kw)
    return ResolvedEndpoint(**base)


def test_permission_allowlist_denies_complement():
    spec = PermissionSpec(allowed_tools=["Read", "Grep"])
    perm = render_permission(spec)
    assert perm["read"] == "allow"
    assert perm["grep"] == "allow"
    assert perm["edit"] == "deny"
    assert perm["bash"] == "deny"
    # Headless safety defaults regardless of the allowlist.
    assert perm["question"] == "deny"
    assert perm["external_directory"] == "deny"


def test_permission_default_allows_except_denied():
    spec = PermissionSpec(denied_tools=["Bash", "WebFetch"])
    perm = render_permission(spec)
    assert perm["read"] == "allow"
    assert perm["webfetch"] == "deny"
    # bash is denied via the git-push guard map (still a dict), not "allow".
    assert isinstance(perm["bash"], dict) or perm["bash"] == "deny"


def test_permission_git_push_blocked_by_default():
    spec = PermissionSpec()
    perm = render_permission(spec)
    assert isinstance(perm["bash"], dict)
    assert perm["bash"]["git push*"] == "deny"


def test_permission_git_push_allowed_when_opted_in():
    spec = PermissionSpec(allow_git_push=True)
    perm = render_permission(spec)
    assert perm["bash"] == "allow"


def test_resolve_model_strips_provider_prefix():
    spec = PermissionSpec(backend_config={"model": "anthropic/claude-sonnet-4-5"})
    assert resolve_model(spec, None) == "claude-sonnet-4-5"
    spec2 = PermissionSpec(default_model="glm-4.6")
    assert resolve_model(spec2, None) == "glm-4.6"
    assert resolve_model(PermissionSpec(), _ep()) == "glm-4.6"


def test_block_id():
    assert block_id("ep1") == "nd_ep1"


def test_render_config_single_endpoint():
    """Legacy-shaped single-endpoint call: one endpoint, one assignment."""
    spec = PermissionSpec(allowed_tools=["Read"], system_prompt="be terse")
    ep = _ep()
    assignments = {"primary": Assignment("ep1", "glm-4.6")}
    cfg = render_config(spec, {"ep1": ep}, assignments)
    assert cfg["autoupdate"] is False
    assert cfg["share"] == "disabled"
    block = cfg["provider"]["nd_ep1"]
    assert block["npm"] == "@ai-sdk/anthropic"
    assert block["options"]["baseURL"] == "https://api.z.ai/api/anthropic"
    assert block["options"]["apiKey"] == "sk-1"
    assert cfg["model"] == "nd_ep1/glm-4.6"
    assert "small_model" not in cfg  # unpinned slot renders nothing


def test_render_config_ollama_default_base_url():
    ep = _ep(id="ep2", protocol_kind="ollama", base_url=None, default_model="llama3")
    cfg = render_config(PermissionSpec(), {"ep2": ep}, {"primary": Assignment("ep2", "llama3")})
    block = cfg["provider"]["nd_ep2"]
    assert block["npm"] == "@ai-sdk/openai-compatible"
    assert block["options"]["baseURL"] == "http://localhost:11434/v1"


def test_render_config_options_merge_extra_wins():
    ep = _ep(extra={"options": {"apiKey": "override-key", "headers": {"X-Foo": "bar"}}})
    cfg = render_config(PermissionSpec(), {"ep1": ep}, {})
    block = cfg["provider"]["nd_ep1"]
    assert block["options"]["apiKey"] == "override-key"
    assert block["options"]["headers"] == {"X-Foo": "bar"}


def test_render_config_multi_endpoint_with_agent():
    primary = _ep(id="ep_primary", label="codex", protocol_kind="openai_codex",
                   base_url=None, credential=None, credential_source="oauth_file",
                   default_model="gpt-5.4", models=[])
    secondary = _ep(id="ep_zai", label="zai-openai", protocol_kind="openai_compat",
                     base_url="https://api.z.ai/api/paas/v4", credential="zai-key",
                     credential_source="api_key", default_model="glm-5.2",
                     models=["glm-5.2"])
    endpoints = {"ep_primary": primary, "ep_zai": secondary}
    assignments = {
        "primary": Assignment("ep_primary", "gpt-5.4"),
        "agent:researcher": Assignment("ep_zai", "glm-5.2"),
    }
    spec = PermissionSpec(backend_config={
        "agents": [
            {"name": "researcher", "endpoint_id": "ep_zai", "model": "glm-5.2",
             "tools": ["webfetch", "websearch"], "prompt": "dig deep"},
        ],
    })
    cfg = render_config(spec, endpoints, assignments)

    # The openai_codex primary renders onto opencode's bundled "openai"
    # provider id (see resolve_provider_ids), not its own nd_ namespace.
    assert set(cfg["provider"].keys()) == {"openai", "nd_ep_zai"}
    assert "npm" not in cfg["provider"]["openai"]  # extends the bundled provider, no ai-sdk override
    assert "options" not in cfg["provider"]["openai"]  # no apiKey — oauth lives in OPENCODE_AUTH_CONTENT
    assert cfg["provider"]["nd_ep_zai"]["options"]["apiKey"] == "zai-key"

    assert cfg["model"] == "openai/gpt-5.4"
    assert "small_model" not in cfg

    agent_cfg = cfg["agent"]["researcher"]
    assert agent_cfg["model"] == "nd_ep_zai/glm-5.2"
    assert agent_cfg["prompt"] == "dig deep"
    assert agent_cfg["permission"]["webfetch"] == "allow"
    assert agent_cfg["permission"]["websearch"] == "allow"
    assert agent_cfg["permission"]["bash"] == "deny"


def test_render_config_agent_without_assignment_is_skipped():
    spec = PermissionSpec(backend_config={"agents": [{"name": "orphan"}]})
    cfg = render_config(spec, {}, {})
    assert "agent" not in cfg


def test_render_config_small_model_present_when_pinned():
    ep = _ep()
    assignments = {
        "primary": Assignment("ep1", "glm-4.6"),
        "small_model": Assignment("ep1", "glm-4.5-flash"),
    }
    cfg = render_config(PermissionSpec(), {"ep1": ep}, assignments)
    assert cfg["small_model"] == "nd_ep1/glm-4.5-flash"


def test_render_auth_api_key_multi_block():
    ep1 = _ep(id="ep1", credential="sk-1", credential_source="api_key")
    ep2 = _ep(id="ep2", credential="sk-2", credential_source="api_key")
    auth = render_auth({"ep1": ep1, "ep2": ep2})
    assert auth == {
        "nd_ep1": {"type": "api", "key": "sk-1"},
        "nd_ep2": {"type": "api", "key": "sk-2"},
    }


def test_render_auth_empty_when_no_credentials():
    ep = _ep(credential=None, credential_source="none")
    assert render_auth({"ep1": ep}) is None
    assert render_auth({}) is None


def _fake_jwt(exp: int) -> str:
    """A structurally valid unsigned JWT whose payload carries ``exp``."""
    import base64 as _b64
    enc = lambda d: _b64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


def test_render_auth_codex_oauth_parses_tokens():
    """Bug 7 + bug 8: a Codex oauth entry renders onto the native "openai"
    provider id (not nd_<eid>), carries accountId (renamed from
    tokens.account_id), and expires comes from the access token's JWT exp
    claim in MILLISECONDS — never 0 when decodable, because expires=0 forces
    an eager refresh whose token rotation strands the file's single-use
    refresh token (see _parse_codex_oauth's docstring)."""
    access = _fake_jwt(exp=1_784_000_000)
    raw = json.dumps({"tokens": {
        "access_token": access, "refresh_token": "ref-1", "account_id": "acct-1",
    }})
    ep = _ep(id="ep_codex", protocol_kind="openai_codex", base_url=None,
             credential=raw, credential_source="oauth_file")
    auth = render_auth({"ep_codex": ep})
    assert auth == {"openai": {
        "type": "oauth", "access": access, "refresh": "ref-1",
        "expires": 1_784_000_000_000, "accountId": "acct-1",
    }}


def test_render_auth_codex_oauth_without_account_id():
    access = _fake_jwt(exp=2_000_000_000)
    raw = json.dumps({"tokens": {"access_token": access, "refresh_token": "ref-1"}})
    ep = _ep(id="ep_codex", protocol_kind="openai_codex", base_url=None,
             credential=raw, credential_source="oauth_file")
    auth = render_auth({"ep_codex": ep})
    assert auth == {"openai": {
        "type": "oauth", "access": access, "refresh": "ref-1",
        "expires": 2_000_000_000_000,
    }}


def test_render_auth_codex_oauth_non_jwt_access_falls_back_to_eager_refresh():
    """An opaque (non-JWT) access token has no readable expiry; expires=0 is
    the least-bad fallback (eager refresh on first request)."""
    raw = json.dumps({"tokens": {"access_token": "opaque-token", "refresh_token": "ref-1"}})
    ep = _ep(id="ep_codex", protocol_kind="openai_codex", base_url=None,
             credential=raw, credential_source="oauth_file")
    auth = render_auth({"ep_codex": ep})
    assert auth["openai"]["expires"] == 0


def test_render_auth_codex_oauth_malformed_json_omits_block():
    ep = _ep(id="ep_codex", protocol_kind="openai_codex", base_url=None,
             credential="not json", credential_source="oauth_file")
    assert render_auth({"ep_codex": ep}) is None


def test_render_auth_codex_oauth_missing_tokens_omits_block():
    raw = json.dumps({"not_tokens": {}})
    ep = _ep(id="ep_codex", protocol_kind="openai_codex", base_url=None,
             credential=raw, credential_source="oauth_file")
    assert render_auth({"ep_codex": ep}) is None


def test_render_auth_subscription_file_omitted():
    ep = _ep(credential="raw-subscription-blob", credential_source="subscription_file",
              harness_lock="claude_sdk")
    assert render_auth({"ep1": ep}) is None


def test_render_auth_non_codex_endpoints_still_nd_namespaced():
    """Every protocol besides openai_codex keeps nd_<eid> namespacing —
    only the native-provider mapping changes."""
    ep1 = _ep(id="ep1", protocol_kind="anthropic_compat", credential="sk-1",
              credential_source="api_key")
    ep2 = _ep(id="ep2", protocol_kind="openai_compat", credential="sk-2",
              credential_source="api_key")
    auth = render_auth({"ep1": ep1, "ep2": ep2})
    assert auth == {
        "nd_ep1": {"type": "api", "key": "sk-1"},
        "nd_ep2": {"type": "api", "key": "sk-2"},
    }


def test_resolve_provider_ids_native_collision_primary_wins(caplog):
    """Two openai_codex endpoints in one profile both want the "openai"
    native id: the endpoint backing the 'primary' assignment wins, the
    other is dropped (and a warning is logged), never silently overwritten
    or split across both."""
    primary_ep = _ep(id="ep_codex_a", protocol_kind="openai_codex",
                      credential=None, credential_source="oauth_file")
    other_ep = _ep(id="ep_codex_b", protocol_kind="openai_codex",
                    credential=None, credential_source="oauth_file")
    endpoints = {"ep_codex_b": other_ep, "ep_codex_a": primary_ep}
    assignments = {"primary": Assignment("ep_codex_a", "gpt-5.4")}
    with caplog.at_level("WARNING"):
        provider_ids = resolve_provider_ids(endpoints, assignments)
    assert provider_ids == {"ep_codex_a": "openai"}
    assert "ep_codex_b" not in provider_ids
    assert any("both map to the native provider id" in r.message for r in caplog.records)


def test_render_config_codex_model_menu_and_prompt_body_split(tmp_path):
    """The rendered config's model menu lands on the "openai" provider block,
    and the prompt-body split (build_prompt_body) picks providerID "openai"
    for a codex-pinned model string — the two things opencode actually needs
    to route a turn onto its bundled Codex plugin."""
    from nightdesk.backends.opencode_config import build_prompt_body

    ep = _ep(id="ep_codex", label="Codex", provider_name="Codex",
             protocol_kind="openai_codex",
             base_url=None, credential=None, credential_source="oauth_file",
             default_model="gpt-5.6-sol", models=["gpt-5.6-sol", "gpt-5.5"])
    assignments = {"primary": Assignment("ep_codex", "gpt-5.6-sol")}
    cfg = render_config(PermissionSpec(), {"ep_codex": ep}, assignments)
    assert cfg["provider"]["openai"]["models"] == {"gpt-5.6-sol": {}, "gpt-5.5": {}}
    assert cfg["provider"]["openai"]["name"] == "Codex"
    assert cfg["model"] == "openai/gpt-5.6-sol"

    body = build_prompt_body("hi", model=cfg["model"])
    assert body["model"] == {"providerID": "openai", "modelID": "gpt-5.6-sol"}


def _ctx(tmp_path, **kw):
    base = dict(
        spec=PermissionSpec(),
        endpoint=None,
        run_id="r1",
        ticket_id="t1",
        workspace_dir=tmp_path,
        scratch_root=tmp_path,
        http_port=9999,
        endpoints={},
        model_assignments={},
    )
    base.update(kw)
    return LaunchContext(**base)


def test_prepare_launch_legacy_single_endpoint_fallback(tmp_path):
    """No model_assignments resolved (pre-provider profile): both model and
    small_model fill from resolve_model, mirroring the old single-block
    behaviour."""
    ep = _ep()
    ctx = _ctx(tmp_path, endpoint=ep, spec=PermissionSpec(default_model="glm-4.6"))
    plan = OpencodeBackend().prepare_launch(ctx)
    config = json.loads(plan.env["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == "nd_ep1/glm-4.6"
    assert config["small_model"] == "nd_ep1/glm-4.6"
    assert set(config["provider"].keys()) == {"nd_ep1"}
    auth = json.loads(plan.env["OPENCODE_AUTH_CONTENT"])
    assert auth == {"nd_ep1": {"type": "api", "key": "sk-1"}}
    assert ctx.backend_state["model"] == "nd_ep1/glm-4.6"


def test_prepare_launch_multi_endpoint_uses_resolved_assignments(tmp_path):
    primary = _ep(id="ep_primary", default_model="gpt-5.4")
    secondary = _ep(id="ep_zai", credential="zai-key")
    assignments = {
        "primary": Assignment("ep_primary", "gpt-5.4"),
        "agent:researcher": Assignment("ep_zai", "glm-5.2"),
    }
    ctx = _ctx(
        tmp_path,
        endpoint=primary,
        endpoints={"ep_primary": primary, "ep_zai": secondary},
        model_assignments=assignments,
        spec=PermissionSpec(backend_config={
            "agents": [{"name": "researcher", "endpoint_id": "ep_zai", "model": "glm-5.2"}],
        }),
    )
    plan = OpencodeBackend().prepare_launch(ctx)
    config = json.loads(plan.env["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == "nd_ep_primary/gpt-5.4"
    assert "small_model" not in config
    assert config["agent"]["researcher"]["model"] == "nd_ep_zai/glm-5.2"
    assert set(config["provider"].keys()) == {"nd_ep_primary", "nd_ep_zai"}
    assert ctx.backend_state["model"] == "nd_ep_primary/gpt-5.4"


def test_translate_text_emits_deltas_only():
    state = new_state()
    e1 = {"type": "message.part.updated",
          "properties": {"part": {"id": "t1", "type": "text", "text": "Hello"}}}
    out1 = translate_event(e1, state)
    assert out1 == [{"type": "assistant_text", "text": "Hello"}]
    e2 = {"type": "message.part.updated",
          "properties": {"part": {"id": "t1", "type": "text", "text": "Hello world"}}}
    out2 = translate_event(e2, state)
    assert out2 == [{"type": "assistant_text", "text": " world"}]


def test_translate_suppresses_parts_belonging_to_the_user_message():
    """Bug 6: opencode emits message.part.updated for the USER's own message
    (its prompt, split into parts the same way an assistant reply is) — those
    parts must NOT be translated into assistant_text. The host already
    records the user message via its own user_message transcript write."""
    state = new_state()
    user_msg = {"type": "message.updated", "properties": {"info": {
        "id": "msg-user-1", "role": "user"}}}
    assert translate_event(user_msg, state) == []

    echoed = {"type": "message.part.updated", "properties": {"part": {
        "id": "p1", "messageID": "msg-user-1", "type": "text",
        "text": "do the thing"}}}
    assert translate_event(echoed, state) == []

    # An assistant message's parts still translate normally.
    asst_msg = {"type": "message.updated", "properties": {"info": {
        "id": "msg-asst-1", "role": "assistant"}}}
    assert translate_event(asst_msg, state) == []
    reply = {"type": "message.part.updated", "properties": {"part": {
        "id": "p2", "messageID": "msg-asst-1", "type": "text",
        "text": "here you go"}}}
    assert translate_event(reply, state) == [
        {"type": "assistant_text", "text": "here you go"}]


def test_translate_part_with_unknown_message_id_still_emits():
    """A part whose message id hasn't been seen via message.updated yet
    (role unknown) falls through as assistant — matching observed ordering
    where message.updated always precedes its part events, and keeping the
    existing no-messageID test fixtures (below) working unchanged."""
    state = new_state()
    evt = {"type": "message.part.updated", "properties": {"part": {
        "id": "p1", "messageID": "not-seen-yet", "type": "text",
        "text": "hi"}}}
    assert translate_event(evt, state) == [{"type": "assistant_text", "text": "hi"}]


def test_translate_tool_lifecycle():
    state = new_state()
    start = {"type": "message.part.updated", "properties": {"part": {
        "callID": "c1", "type": "tool", "tool": "bash",
        "state": {"status": "running", "input": {"command": "ls"}}}}}
    out = translate_event(start, state)
    assert out == [{"type": "tool_use", "id": "c1", "tool": "bash",
                    "input": {"command": "ls"}}]
    done = {"type": "message.part.updated", "properties": {"part": {
        "callID": "c1", "type": "tool", "tool": "bash",
        "state": {"status": "completed", "output": "file.txt"}}}}
    out = translate_event(done, state)
    assert out == [{"type": "tool_result", "tool_use_id": "c1",
                    "output": "file.txt", "is_error": False}]
    # The same completed event a second time fires nothing.
    assert translate_event(done, state) == []


def test_translate_session_error_and_usage():
    state = new_state()
    err = {"type": "session.error", "properties": {
        "sessionID": "s", "error": {"name": "ProviderAuthError",
                                     "data": {"message": "bad key"}}}}
    out = translate_event(err, state)
    assert out == [{"type": "worker_error", "kind": "ProviderAuthError",
                    "summary": "bad key"}]
    assert state["error"] == "bad key"

    upd = {"type": "message.updated", "properties": {"info": {
        "modelID": "glm-4.6", "cost": 0.01,
        "tokens": {"input": 100, "output": 50, "cache": {"read": 10, "write": 0}}}}}
    assert translate_event(upd, state) == []
    assert state["usage"]["cost"] == 0.01


def test_usage_by_model_accumulates_across_messages_and_models():
    """message.updated for two different messages/models is attributed
    per-model, not collapsed into the single ``usage`` aggregate."""
    state = new_state()
    msg1 = {"type": "message.updated", "properties": {"info": {
        "id": "msg-1", "modelID": "gpt-5.4",
        "tokens": {"input": 1000, "output": 200, "cache": {"read": 0, "write": 0}}}}}
    msg2 = {"type": "message.updated", "properties": {"info": {
        "id": "msg-2", "modelID": "glm-5.2",
        "tokens": {"input": 500, "output": 100, "cache": {"read": 10, "write": 5}}}}}
    # A second update to msg-1 (streaming totals growing) must replace, not
    # add to, that message's contribution.
    msg1_grown = {"type": "message.updated", "properties": {"info": {
        "id": "msg-1", "modelID": "gpt-5.4",
        "tokens": {"input": 1200, "output": 300, "cache": {"read": 0, "write": 0}}}}}

    translate_event(msg1, state)
    translate_event(msg2, state)
    translate_event(msg1_grown, state)

    by_model = usage_by_model(state)
    assert by_model == {
        "gpt-5.4": {"input_tokens": 1200, "output_tokens": 300,
                    "cache_read_tokens": 0, "cache_write_tokens": 0},
        "glm-5.2": {"input_tokens": 500, "output_tokens": 100,
                    "cache_read_tokens": 10, "cache_write_tokens": 5},
    }
    # The single-value aggregate keeps its own last-write-wins semantics,
    # unaffected by the per-model tracking.
    assert state["usage"]["model"] == "gpt-5.4"


def test_usage_by_model_sums_repeated_messages_from_the_same_model():
    """Two distinct completed messages from the SAME model sum together."""
    state = new_state()
    translate_event({"type": "message.updated", "properties": {"info": {
        "id": "msg-1", "modelID": "gpt-5.4",
        "tokens": {"input": 100, "output": 50}}}}, state)
    translate_event({"type": "message.updated", "properties": {"info": {
        "id": "msg-2", "modelID": "gpt-5.4",
        "tokens": {"input": 200, "output": 25}}}}, state)

    assert usage_by_model(state) == {
        "gpt-5.4": {"input_tokens": 300, "output_tokens": 75,
                    "cache_read_tokens": 0, "cache_write_tokens": 0},
    }


def test_usage_by_model_empty_when_no_usage_events():
    assert usage_by_model(new_state()) == {}
