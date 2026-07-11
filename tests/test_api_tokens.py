"""Phase 1 agent token / permission system: mint, hashing, scope enforcement."""
import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nightdesk.api.app import create_app
from nightdesk.db.models import ApiToken, Base, Profile, Ticket
from nightdesk.domain.api_tokens import (
    HumanOnlyScopeError,
    mint_api_token,
    resolve_token,
)


ADMIN = {"Authorization": "Bearer bearer-admin"}


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/db.sqlite")
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine, bearer_token="bearer-admin",
        static_root=tmp_path / "s", transcript_root=tmp_path / "tx",
        worktree_root=tmp_path / "w",
    )


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        yield c


def _seed_profile(engine, name="p1") -> str:
    with Session(engine) as s:
        p = Profile(name=name)
        s.add(p)
        s.commit()
        return p.id


def _seed_ticket(engine, profile_id, *, status="draft", title="t") -> str:
    with Session(engine) as s:
        t = Ticket(title=title, prompt="x", profile_id=profile_id, status=status)
        s.add(t)
        s.commit()
        return t.id


# --- mint / hash / reveal-once --------------------------------------------


async def test_mint_reveals_once_and_stores_only_hash(client, engine):
    r = await client.post("/api/v1/tokens", headers=ADMIN,
                          json={"name": "obs", "bundle": "observer"})
    assert r.status_code == 201
    body = r.json()
    token = body["token"]
    assert token.startswith("ndk_")
    assert body["prefix_hint"] == token[:12]

    # Only the hash is persisted; the cleartext appears nowhere in the row.
    with Session(engine) as s:
        row = s.get(ApiToken, body["id"])
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in (row.prefix_hint or "")

    # The list endpoint never returns a value.
    lst = await client.get("/api/v1/tokens", headers=ADMIN)
    assert lst.status_code == 200
    entry = next(t for t in lst.json() if t["id"] == body["id"])
    assert "token" not in entry
    assert "token_hash" not in entry


async def test_mint_requires_admin(client):
    # No auth -> 401.
    r = await client.post("/api/v1/tokens", json={"name": "x", "bundle": "observer"})
    assert r.status_code == 401


async def test_session_cookie_manages_tokens(app):
    # The browser's signed session cookie IS the admin session: the Settings UI
    # must be able to list/mint/revoke/delete without the raw bearer.
    from nightdesk.api.auth import SESSION_COOKIE, issue_session_cookie

    cookies = {SESSION_COOKIE: issue_session_cookie("bearer-admin")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t", cookies=cookies) as c:
        lst = await c.get("/api/v1/tokens")
        assert lst.status_code == 200

        cat = await c.get("/api/v1/tokens/catalog")
        assert cat.status_code == 200

        r = await c.post("/api/v1/tokens",
                         json={"name": "via-cookie", "bundle": "observer"})
        assert r.status_code == 201
        tid = r.json()["id"]

        rev = await c.post(f"/api/v1/tokens/{tid}/revoke")
        assert rev.status_code == 200

        d = await c.delete(f"/api/v1/tokens/{tid}")
        assert d.status_code == 204


async def test_agent_token_cannot_manage_tokens(client, engine):
    # Even an operator token (broad) cannot reach ANY tokens route — the router
    # gates on tokens.admin, which is human-only and unmintable, so a scoped
    # ndk_ token always 403s with the human-only hint.
    with Session(engine) as s:
        issued = mint_api_token(s, name="op", scopes=["tickets.read", "tickets.run"])
    hdr = {"Authorization": f"Bearer {issued.cleartext}"}

    r = await client.post("/api/v1/tokens", headers=hdr,
                          json={"name": "x", "bundle": "observer"})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "tokens.admin" in detail["missing_scopes"]
    assert "human-only" in detail["hint"]

    for method, path in [
        ("GET", "/api/v1/tokens"),
        ("GET", "/api/v1/tokens/catalog"),
        ("POST", "/api/v1/tokens/some-id/revoke"),
        ("DELETE", "/api/v1/tokens/some-id"),
    ]:
        resp = await client.request(method, path, headers=hdr)
        assert resp.status_code == 403, (method, path, resp.status_code)


async def test_garbage_bearer_cannot_manage_tokens(client):
    hdr = {"Authorization": "Bearer ndk_not_a_real_token"}
    r = await client.get("/api/v1/tokens", headers=hdr)
    assert r.status_code == 401


# --- human-only mint rejection --------------------------------------------


@pytest.mark.parametrize("scope", [
    "profiles.write", "cron.write", "config.write", "providers.write",
    "agents.message", "agents.admin", "tickets.delete", "tokens.admin",
])
async def test_human_only_scopes_rejected_at_mint(client, scope):
    r = await client.post("/api/v1/tokens", headers=ADMIN,
                          json={"name": "bad", "scopes": ["tickets.read", scope]})
    assert r.status_code == 422
    assert scope in r.json()["detail"]["human_only_scopes"]


def test_mint_domain_raises_on_human_only(engine):
    with Session(engine) as s:
        with pytest.raises(HumanOnlyScopeError):
            mint_api_token(s, name="bad", scopes=["profiles.write"])


# --- resolver: unknown / revoked / expired --------------------------------


def test_resolver_rejects_garbage(engine):
    with Session(engine) as s:
        assert resolve_token(s, "not-a-token") is None
        assert resolve_token(s, "ndk_nope") is None


def test_resolver_rejects_revoked(engine):
    with Session(engine) as s:
        issued = mint_api_token(s, name="r", scopes=["tickets.read"])
        row = s.get(ApiToken, issued.id)
        row.revoked_at = datetime.now(timezone.utc)
        s.commit()
        assert resolve_token(s, issued.cleartext) is None


def test_resolver_rejects_expired(engine):
    with Session(engine) as s:
        issued = mint_api_token(
            s, name="e", scopes=["tickets.read"],
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert resolve_token(s, issued.cleartext) is None


# --- scope enforcement per bundle -----------------------------------------


async def _mint(client, **body):
    r = await client.post("/api/v1/tokens", headers=ADMIN, json=body)
    assert r.status_code == 201, r.text
    return r.json()["token"]


async def test_observer_reads_but_cannot_write(client, engine):
    tok = await _mint(client, name="obs", bundle="observer")
    hdr = {"Authorization": f"Bearer {tok}"}
    assert (await client.get("/api/v1/tickets", headers=hdr)).status_code == 200
    # Cannot create.
    r = await client.post("/api/v1/tickets", headers=hdr,
                          json={"title": "x", "prompt": "y"})
    assert r.status_code == 403
    assert "tickets.create" in r.json()["detail"]["missing_scopes"]


async def test_pm_agent_can_transition_but_not_run_or_config(client, engine):
    pid = _seed_profile(engine)
    tid = _seed_ticket(engine, pid, status="queued")
    tok = await _mint(client, name="pm", bundle="pm-agent")
    hdr = {"Authorization": f"Bearer {tok}"}

    # transition allowed
    r = await client.post(f"/api/v1/tickets/{tid}/transition", headers=hdr,
                          json={"target": "draft"})
    assert r.status_code in (200, 409, 422), r.text  # allowed past auth (409/422 = domain)
    assert r.status_code != 403

    # run-now denied (operator-only)
    r = await client.post(f"/api/v1/tickets/{tid}/run-now", headers=hdr, json={})
    assert r.status_code == 403
    assert "tickets.run" in r.json()["detail"]["missing_scopes"]

    # config write denied (pm-agent lacks config entirely: the router-level
    # config.read gate fires first).
    r = await client.patch("/api/v1/config", headers=hdr, json={})
    assert r.status_code == 403

    # provider write denied
    r = await client.post("/api/v1/providers", headers=hdr, json={"name": "z"})
    assert r.status_code == 403

    # profile write denied
    r = await client.post("/api/v1/profiles", headers=hdr, json={"name": "z"})
    assert r.status_code == 403


async def test_human_only_hint_on_write_when_token_can_read(client, engine):
    # A token that CAN read config but not write it gets the human-only hint on
    # the write — the reason it can never be granted config.write.
    tok = await _mint(client, name="cfg", scopes=["config.read"])
    hdr = {"Authorization": f"Bearer {tok}"}
    assert (await client.get("/api/v1/config", headers=hdr)).status_code == 200
    r = await client.patch("/api/v1/config", headers=hdr, json={})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "config.write" in detail["missing_scopes"]
    assert "human-only" in detail["hint"]


async def test_operator_can_run(client, engine):
    pid = _seed_profile(engine)
    tid = _seed_ticket(engine, pid, status="queued")
    tok = await _mint(client, name="op", bundle="operator")
    hdr = {"Authorization": f"Bearer {tok}"}
    r = await client.post(f"/api/v1/tickets/{tid}/run-now", headers=hdr, json={})
    assert r.status_code != 403  # passes auth (may 200/409 on domain state)


async def test_reviewer_can_comment_not_transition(client, engine):
    pid = _seed_profile(engine)
    tid = _seed_ticket(engine, pid, status="queued")
    tok = await _mint(client, name="rev", bundle="reviewer")
    hdr = {"Authorization": f"Bearer {tok}"}
    # cannot transition
    r = await client.post(f"/api/v1/tickets/{tid}/transition", headers=hdr,
                          json={"target": "draft"})
    assert r.status_code == 403
    assert "tickets.transition" in r.json()["detail"]["missing_scopes"]


# --- allowlist restriction -------------------------------------------------


async def test_profile_allowlist_restricts_create(client, engine):
    allowed = _seed_profile(engine, "allowed")
    forbidden = _seed_profile(engine, "forbidden")
    tok = await _mint(client, name="pm", bundle="pm-agent",
                      profile_allowlist=[allowed])
    hdr = {"Authorization": f"Bearer {tok}"}

    ws = [{"role": "primary", "label": "primary", "kind": "directory",
           "access": "read_write", "source_path": "/tmp", "retention": "preserve"}]
    # forbidden profile -> 403
    r = await client.post("/api/v1/tickets", headers=hdr, json={
        "title": "x", "prompt": "y", "profile_id": forbidden, "workspaces": ws,
    })
    assert r.status_code == 403
    assert "profile" in r.json()["detail"]["detail"]

    # allowed profile -> passes auth (201 or domain error, never 403)
    r = await client.post("/api/v1/tickets", headers=hdr, json={
        "title": "x", "prompt": "y", "profile_id": allowed, "workspaces": ws,
    })
    assert r.status_code != 403


# --- revocation is immediate ----------------------------------------------


async def test_revoke_is_immediate(client, engine):
    r = await client.post("/api/v1/tokens", headers=ADMIN,
                          json={"name": "obs", "bundle": "observer"})
    body = r.json()
    hdr = {"Authorization": f"Bearer {body['token']}"}
    assert (await client.get("/api/v1/tickets", headers=hdr)).status_code == 200

    rev = await client.post(f"/api/v1/tokens/{body['id']}/revoke", headers=ADMIN)
    assert rev.status_code == 200
    assert rev.json()["revoked_at"] is not None
    # Immediately rejected.
    assert (await client.get("/api/v1/tickets", headers=hdr)).status_code == 401


async def test_delete_token(client, engine):
    r = await client.post("/api/v1/tokens", headers=ADMIN,
                          json={"name": "obs", "bundle": "observer"})
    tid = r.json()["id"]
    d = await client.delete(f"/api/v1/tokens/{tid}", headers=ADMIN)
    assert d.status_code == 204
    with Session(engine) as s:
        assert s.get(ApiToken, tid) is None


# --- cookie/admin unaffected ----------------------------------------------


async def test_admin_bearer_bypasses_scope_checks(client, engine):
    # The root bearer can hit a human-only route the strongest token cannot.
    r = await client.patch("/api/v1/config", headers=ADMIN, json={})
    assert r.status_code != 403  # admin passes auth entirely


async def test_catalog_lists_scopes_and_human_only(client):
    r = await client.get("/api/v1/tokens/catalog", headers=ADMIN)
    assert r.status_code == 200
    cat = r.json()
    assert "tickets.read" in cat["scopes"]
    assert "profiles.write" in cat["human_only"]
    assert set(cat["bundles"]) == {"observer", "reviewer", "pm-agent", "operator"}


# --- run-token grant wiring (ndr_ ticket.create now reaches the route) ------


async def test_run_token_ticket_create_grant_wired(client, engine):
    """A run token granted the legacy ``ticket.create`` scope can POST /tickets.

    Before Phase 1 the route only accepted cookie/bearer, so the declared grant
    was dead. The scope alias maps ``ticket.create`` -> ``tickets.create``.
    """
    from datetime import datetime, timezone
    from nightdesk.db.models import Run
    from nightdesk.domain.run_tokens import issue_run_token

    pid = _seed_profile(engine)
    tid = _seed_ticket(engine, pid, status="queued")
    with Session(engine) as s:
        r = Run(ticket_id=tid, started_at=datetime.now(timezone.utc),
                worktree_path="/tmp", transcript_path="/tmp/log", host="h")
        s.add(r)
        s.commit()
        rid = r.id
        issued = issue_run_token(
            s, run_id=rid, ticket_id=tid, extra_scopes=["ticket.create"],
            max_run_duration_seconds=600, grace_seconds=60,
        )
    hdr = {"Authorization": f"Bearer {issued.cleartext}"}
    ws = [{"role": "primary", "label": "primary", "kind": "directory",
           "access": "read_write", "source_path": "/tmp", "retention": "preserve"}]
    r = await client.post("/api/v1/tickets", headers=hdr, json={
        "title": "child", "prompt": "y", "profile_id": pid, "workspaces": ws,
    })
    # Passes auth (create scope satisfied); may 201 or a domain 422, never 401/403.
    assert r.status_code not in (401, 403), r.text


async def test_run_token_without_grant_cannot_create(client, engine):
    from datetime import datetime, timezone
    from nightdesk.db.models import Run
    from nightdesk.domain.run_tokens import issue_run_token

    pid = _seed_profile(engine)
    tid = _seed_ticket(engine, pid, status="queued")
    with Session(engine) as s:
        r = Run(ticket_id=tid, started_at=datetime.now(timezone.utc),
                worktree_path="/tmp", transcript_path="/tmp/log", host="h")
        s.add(r)
        s.commit()
        issued = issue_run_token(
            s, run_id=r.id, ticket_id=tid,
            max_run_duration_seconds=600, grace_seconds=60,
        )
    hdr = {"Authorization": f"Bearer {issued.cleartext}"}
    ws = [{"role": "primary", "label": "primary", "kind": "directory",
           "access": "read_write", "source_path": "/tmp", "retention": "preserve"}]
    r = await client.post("/api/v1/tickets", headers=hdr, json={
        "title": "child", "prompt": "y", "profile_id": pid, "workspaces": ws,
    })
    assert r.status_code == 403
    assert "tickets.create" in r.json()["detail"]["missing_scopes"]
