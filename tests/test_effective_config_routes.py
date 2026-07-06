"""Endpoint tests for the effective-config resolver (JSON API)."""
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.providers import create_endpoint, create_provider
from nightdesk.domain.tickets import create_ticket


def _seed(session):
    p = create_profile(
        session, name="Edit",
        fs_read=["/data"], fs_write=["/data"],
        allowed_tools=["Read"], denied_tools=["WebFetch"],
        network_mode="on", network_allowlist=[], secret_keys=["MY_KEY"],
        default_model="claude-sonnet", backend="claude_sdk",
        permission_mode="acceptEdits",
    )
    proj = create_project(session, name="Proj", source_path="/tmp/proj")
    t = create_ticket(
        session, title="T", prompt="x", profile_id=p.id, project_id=proj.id,
        source_path="/tmp/proj",
        permission_overrides={"default_model": "claude-opus"},
    )
    return p, proj, t


async def test_json_ticket_effective_config(client, session):
    _, _, t = _seed(session)
    r = await client.get(f"/api/v1/tickets/{t.id}/effective-config")
    assert r.status_code == 200
    data = r.json()
    assert "groups" in data and "issues" in data
    fields = {f["key"]: f for g in data["groups"] for f in g["fields"]}
    assert fields["default_model"]["value"] == "claude-opus"
    assert fields["default_model"]["source"] == "ticket"
    assert fields["backend"]["source"] == "profile"


async def test_json_ticket_effective_config_404(client, session):
    r = await client.get("/api/v1/tickets/does-not-exist/effective-config")
    assert r.status_code == 404


async def test_json_preview_for_draft(client, session):
    p, proj, _ = _seed(session)
    r = await client.post(
        "/api/v1/effective-config/preview",
        json={"profile_id": p.id, "project_id": proj.id},
    )
    assert r.status_code == 200
    data = r.json()
    fields = {f["key"]: f for g in data["groups"] for f in g["fields"]}
    assert fields["source_path"]["value"] == "/tmp/proj"
    assert fields["source_path"]["source"] == "project"


async def test_json_preview_flags_missing_profile(client, session):
    r = await client.post("/api/v1/effective-config/preview", json={"source_path": "/tmp"})
    assert r.status_code == 200
    assert any("no profile" in i.lower() for i in r.json()["issues"])


async def test_json_ticket_effective_config_includes_masked_launch_plan(client, session):
    # app fixture wires bearer_token="t" — the route's ProfileSecretBox
    # matches that, so credentials round-trip through the same key.
    box = ProfileSecretBox("t")
    provider = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=provider.id, label="Anthropic-compatible",
        protocol_kind="anthropic_compat", base_url="https://api.z.ai/api/anthropic",
        credential_source="api_key", credential=box.encrypt("secret-zai-key"),
        default_model="glm-5.2",
    )
    p = create_profile(
        session, name="ZaiSmart",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model="glm-5.2", backend="claude_sdk", endpoint_id=ep.id,
    )
    t = create_ticket(session, title="T", prompt="x", profile_id=p.id, source_path="/tmp")

    r = await client.get(f"/api/v1/tickets/{t.id}/effective-config")
    assert r.status_code == 200
    data = r.json()
    launch_group = next((g for g in data["groups"] if g["title"] == "Launch plan"), None)
    assert launch_group is not None
    fields = {f["key"]: f for f in launch_group["fields"]}
    assert fields["launch_env_ANTHROPIC_MODEL"]["value"] == "glm-5.2"
    assert "•••" in fields["launch_env_ANTHROPIC_API_KEY"]["value"]
    assert "secret-zai-key" not in r.text


async def test_json_preview_for_draft_includes_launch_plan(client, session):
    box = ProfileSecretBox("t")
    provider = create_provider(session, name="ZAI", vendor="zai")
    ep = create_endpoint(
        session, provider_id=provider.id, label="Anthropic-compatible",
        protocol_kind="anthropic_compat", credential_source="api_key",
        credential=box.encrypt("secret-zai-key"), default_model="glm-5.2",
    )
    p = create_profile(
        session, name="ZaiSmart2",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model="glm-5.2", backend="claude_sdk", endpoint_id=ep.id,
    )

    r = await client.post(
        "/api/v1/effective-config/preview",
        json={"profile_id": p.id, "source_path": "/tmp"},
    )
    assert r.status_code == 200
    data = r.json()
    launch_group = next((g for g in data["groups"] if g["title"] == "Launch plan"), None)
    assert launch_group is not None
    assert "secret-zai-key" not in r.text


async def test_effective_config_requires_auth(app):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/v1/tickets/x/effective-config")
    assert r.status_code == 401
