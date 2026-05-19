import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app


@pytest.fixture
def app(engine, tmp_path):
    return create_app(engine=engine, bearer_token="t", static_root=tmp_path,
                       transcript_root=tmp_path / "transcripts",
                       worktree_root=tmp_path / "work")


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_healthz_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
