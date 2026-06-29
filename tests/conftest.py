import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nightdesk.api.app import create_app
from nightdesk.db.models import Base, Profile


@pytest.fixture(autouse=True)
def _isolate_nightdesk_env(monkeypatch):
    """Clear NIGHTDESK_* env vars so config loading is deterministic per test.

    Tests that need a var set it themselves via monkeypatch.setenv / patch.dict,
    which layer on top of this clean slate and are restored automatically.
    """
    import os
    for key in list(os.environ):
        if key.startswith("NIGHTDESK_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def sample_profile(session) -> Profile:
    p = Profile(
        name="default",
        fs_read=["/tmp"], fs_write=["/tmp"],
        allowed_tools=["Read"], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(p)
    session.commit()
    return p


def workspace_payload(path="/tmp", *, kind="directory", **overrides):
    data = {
        "role": "primary",
        "label": "primary",
        "kind": kind,
        "access": "read_write",
        "source_path": str(path),
        "retention": "preserve",
    }
    data.update(overrides)
    return [data]


@pytest.fixture
def app(engine, tmp_path):
    return create_app(engine=engine, bearer_token="t", static_root=tmp_path / "static",
                       transcript_root=tmp_path / "transcripts",
                       worktree_root=tmp_path / "work")


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                              headers={"Authorization": "Bearer t"}) as ac:
        yield ac
