"""Endpoint tests for the effective-config resolver (JSON API)."""
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
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


async def test_effective_config_requires_auth(app):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/v1/tickets/x/effective-config")
    assert r.status_code == 401
