import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from nightdesk.api.auth import require_bearer


@pytest.fixture
def secured_app():
    app = FastAPI()
    router = APIRouter()

    @router.get("/secret", dependencies=[Depends(require_bearer("good"))])
    async def secret():
        return {"ok": True}

    app.include_router(router)
    return app


@pytest.fixture
async def client(secured_app):
    transport = ASGITransport(app=secured_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_no_header_rejected(client):
    r = await client.get("/secret")
    assert r.status_code == 401


async def test_wrong_token_rejected(client):
    r = await client.get("/secret", headers={"Authorization": "Bearer bad"})
    assert r.status_code == 401


async def test_good_token_accepted(client):
    r = await client.get("/secret", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200
