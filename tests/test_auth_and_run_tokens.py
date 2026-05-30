"""End-to-end checks for the new auth/run-token machinery."""
import pytest
import httpx
from datetime import timedelta, datetime, timezone

from nightdesk.api.app import create_app
from nightdesk.db.models import Base, Profile, Ticket, Run
from nightdesk.domain.run_tokens import issue_run_token, revoke_run_token
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine, bearer_token="bearer-admin",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "tx",
        worktree_root=tmp_path / "w",
    )


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        yield c


def _seed_ticket(engine, *, profile_name: str | None = None) -> tuple[str, str]:
    import uuid as _u
    with Session(engine) as s:
        p = Profile(name=profile_name or f"p-{_u.uuid4().hex[:6]}")
        s.add(p)
        s.commit()
        t = Ticket(title="t", prompt="x", profile_id=p.id)
        s.add(t)
        s.commit()
        r = Run(ticket_id=t.id, started_at=datetime.now(timezone.utc),
                worktree_path="/tmp", transcript_path="/tmp/log", host="h")
        s.add(r)
        s.commit()
        return t.id, r.id


async def test_run_token_resolves_to_run_principal(engine):
    tid, rid = _seed_ticket(engine)
    with Session(engine) as s:
        issued = issue_run_token(
            s, run_id=rid, ticket_id=tid,
            extra_scopes=[], max_run_duration_seconds=60, grace_seconds=10,
        )
    from nightdesk.domain.run_tokens import resolve_run_token
    with Session(engine) as s:
        principal = resolve_run_token(s, issued.cleartext)
    assert principal is not None
    assert principal.run_id == rid
    assert principal.ticket_id == tid
    assert "ticket.comment.self" in principal.scopes
    assert "ticket.create" not in principal.scopes


async def test_run_token_grants_ticket_create_when_asked(engine):
    tid, rid = _seed_ticket(engine)
    with Session(engine) as s:
        issued = issue_run_token(
            s, run_id=rid, ticket_id=tid,
            extra_scopes=["ticket.create"],
            max_run_duration_seconds=60, grace_seconds=10,
        )
    from nightdesk.domain.run_tokens import resolve_run_token
    with Session(engine) as s:
        principal = resolve_run_token(s, issued.cleartext)
    assert principal is not None
    assert "ticket.create" in principal.scopes


async def test_revoked_run_token_does_not_resolve(engine):
    tid, rid = _seed_ticket(engine)
    with Session(engine) as s:
        issued = issue_run_token(
            s, run_id=rid, ticket_id=tid,
            extra_scopes=[], max_run_duration_seconds=60, grace_seconds=10,
        )
        revoke_run_token(s, issued.token_hash)
    from nightdesk.domain.run_tokens import resolve_run_token
    with Session(engine) as s:
        assert resolve_run_token(s, issued.cleartext) is None


async def test_garbage_token_does_not_resolve(engine):
    from nightdesk.domain.run_tokens import resolve_run_token
    with Session(engine) as s:
        assert resolve_run_token(s, "not-a-token") is None
        assert resolve_run_token(s, "ndr_invalid") is None


async def test_login_with_wrong_bearer_fails(client):
    r = await client.post("/auth/login", data={"bearer": "nope"})
    assert r.status_code == 401


async def test_login_with_right_bearer_sets_cookie(client):
    r = await client.post("/auth/login", data={"bearer": "bearer-admin"},
                          follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "nightdesk_session" in r.cookies


async def test_handshake_consumes_one_shot(app):
    one_shot = app.state.one_shot_store.mint()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get(f"/auth/handshake?token={one_shot}",
                        follow_redirects=False)
        assert r.status_code in (302, 303)
        assert "nightdesk_session" in r.cookies
        # Second attempt with the same token should fail.
        r2 = await c.get(f"/auth/handshake?token={one_shot}")
        assert r2.status_code == 400


async def test_diagnostics_requires_admin(client):
    # Unauth: 401.
    r = await client.get("/diagnostics")
    assert r.status_code == 401
    # Admin: 200.
    r = await client.get("/diagnostics",
                          headers={"Authorization": "Bearer bearer-admin"})
    assert r.status_code == 200
    assert "Diagnostics" in r.text


def test_profile_secret_box_roundtrip():
    from nightdesk.domain.profile_secrets import ProfileSecretBox
    box = ProfileSecretBox("some-bearer")
    ct = box.encrypt({"source": "api_key", "value": "sk-xxx"})
    assert box.decrypt(ct) == {"source": "api_key", "value": "sk-xxx"}
    # Rotating bearer invalidates the blob.
    other = ProfileSecretBox("other-bearer")
    with pytest.raises(ValueError, match="unreadable"):
        other.decrypt(ct)
