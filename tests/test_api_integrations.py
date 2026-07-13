"""API surface for the GitLab integration: connections, repo-links, browse,
external-links (admin + run-token scopes), import."""
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.db.models import Profile, Project, Run
from nightdesk.domain import integrations as ints
from nightdesk.domain.run_tokens import issue_run_token
from nightdesk.domain.tickets import create_ticket


class FakeGitLab:
    def __init__(self, *, issues=None, mrs=None, projects=None, user=None):
        self.issues = issues or {}
        self.mrs = mrs or {}
        self.projects = projects or []
        self.user = user or {"id": 1, "username": "you"}

    def test_auth(self):
        return {"version": "17.0"}

    def current_user(self):
        return self.user

    def list_projects(self, *, search=None, page_token=None):
        from nightdesk.integrations.gitlab import Page
        return Page(items=self.projects, next_page_token=None)

    def list_issues(self, pid, *, state=None, search=None, page_token=None, iids=None):
        from nightdesk.integrations.gitlab import Page
        return Page(items=list(self.issues.values()), next_page_token=None)

    def get_issue(self, pid, iid):
        return self.issues[str(iid)]

    def list_mrs(self, pid, **kw):
        from nightdesk.integrations.gitlab import Page
        return Page(items=list(self.mrs.values()), next_page_token=None)

    def get_mr(self, pid, iid):
        return self.mrs[str(iid)]


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(ints, "client_for", lambda *a, **k: fake)


async def _mk_connection_and_repo(client):
    r = await client.post("/api/v1/connections", json={
        "name": "corp", "provider": "gitlab",
        "base_url": "https://gitlab.example.com", "credential_value": "glpat-secret",
    })
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r = await client.post("/api/v1/repo-links", json={
        "connection_id": cid, "external_id": "55", "external_path": "grp/app",
        "web_url": "https://gitlab.example.com/grp/app",
    })
    assert r.status_code == 201, r.text
    return cid, r.json()["id"]


# ---------------------------------------------------------------------------
# Connections + credential masking
# ---------------------------------------------------------------------------


async def test_connection_credential_is_write_only(client):
    r = await client.post("/api/v1/connections", json={
        "name": "corp", "provider": "gitlab", "credential_value": "glpat-x",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["credential_set"] is True
    assert "credential" not in body and "credential_value" not in body
    assert body["status"] == "unchecked"
    lst = (await client.get("/api/v1/connections")).json()
    assert lst[0]["credential_set"] is True
    assert "credential" not in lst[0]


async def test_connection_rejects_non_gitlab_provider(client):
    r = await client.post("/api/v1/connections", json={"name": "j", "provider": "jira_cloud"})
    assert r.status_code == 400
    assert "GitLab only" in r.json()["detail"]


async def test_connection_name_conflict(client):
    await client.post("/api/v1/connections", json={"name": "dup", "provider": "gitlab"})
    r = await client.post("/api/v1/connections", json={"name": "dup", "provider": "gitlab"})
    assert r.status_code == 409


async def test_connection_delete_blocked_by_repo_links(client):
    cid, _rid = await _mk_connection_and_repo(client)
    r = await client.delete(f"/api/v1/connections/{cid}")
    assert r.status_code == 409


async def test_connection_test_action_writes_status(client, monkeypatch):
    r = await client.post("/api/v1/connections", json={
        "name": "corp", "provider": "gitlab", "credential_value": "glpat-x",
    })
    cid = r.json()["id"]
    _patch_client(monkeypatch, FakeGitLab())
    r = await client.post(f"/api/v1/connections/{cid}/test")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_connection_projects_typeahead(client, monkeypatch):
    r = await client.post("/api/v1/connections", json={"name": "c", "provider": "gitlab"})
    cid = r.json()["id"]
    _patch_client(monkeypatch, FakeGitLab(projects=[{
        "id": 55, "path_with_namespace": "grp/app", "name_with_namespace": "Grp / App",
        "web_url": "https://g/grp/app", "http_url_to_repo": "https://g/grp/app.git",
    }]))
    r = await client.get(f"/api/v1/connections/{cid}/projects?search=app")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["external_id"] == "55"
    assert row["external_path"] == "grp/app"
    assert row["git_remote_url"] == "g/grp/app"


# ---------------------------------------------------------------------------
# Repo links + project attach + suggest
# ---------------------------------------------------------------------------


async def test_repo_link_project_attach_roundtrip(client, session):
    _cid, rid = await _mk_connection_and_repo(client)
    proj = Project(name="P", slug="p", source_path="/tmp")
    session.add(proj)
    session.commit()
    r = await client.put(f"/api/v1/projects/{proj.id}/repo-links",
                         json={"repo_link_ids": [rid]})
    assert r.status_code == 200
    assert [x["id"] for x in r.json()] == [rid]
    got = await client.get(f"/api/v1/projects/{proj.id}/repo-links")
    assert [x["id"] for x in got.json()] == [rid]


async def test_repo_suggest_matches_normalized_remote(client, session, monkeypatch):
    _cid, rid = await _mk_connection_and_repo(client)
    # Give the repo link a normalized remote to match against.
    rl = ints.get_repo_link(session, rid)
    rl.git_remote_url = "gitlab.example.com/grp/app"
    session.commit()
    proj = Project(name="P", slug="p", source_path="/tmp/app")
    session.add(proj)
    session.commit()
    import nightdesk.api.routes.integrations as route_mod
    monkeypatch.setattr(route_mod, "git_value",
                        lambda *a, **k: "git@gitlab.example.com:grp/app.git")
    r = await client.get(f"/api/v1/projects/{proj.id}/repo-suggest")
    assert r.status_code == 200
    body = r.json()
    assert body["git_remote_url"] == "gitlab.example.com/grp/app"
    assert body["matched_repo_link_id"] == rid


# ---------------------------------------------------------------------------
# Browse (admin)
# ---------------------------------------------------------------------------


async def test_browse_issues_admin_and_cache(client, monkeypatch):
    _cid, rid = await _mk_connection_and_repo(client)
    fake = FakeGitLab(issues={"482": {"iid": 482, "title": "heartbeat", "state": "opened"}})
    _patch_client(monkeypatch, fake)
    r = await client.get(f"/api/v1/repo-links/{rid}/issues")
    assert r.status_code == 200
    assert r.json()["items"][0]["iid"] == 482
    r = await client.get(f"/api/v1/repo-links/{rid}/issues/482")
    assert r.json()["title"] == "heartbeat"


async def test_mr_list_awaiting_your_review_flag(client, monkeypatch):
    """Each MR list item carries ``awaiting_your_review`` — true when the
    connection user (the token's own user) is a requested reviewer on an open
    MR. Derived from the proxied read; no persistence."""
    _cid, rid = await _mk_connection_and_repo(client)
    # Connection user is id=42; only !412 lists them as a reviewer.
    fake = FakeGitLab(
        user={"id": 42, "username": "you"},
        mrs={
            "412": {
                "iid": 412, "title": "Retry with backoff", "state": "opened",
                "reviewers": [{"id": 42, "username": "you"}],
            },
            "413": {
                "iid": 413, "title": "Someone else's MR", "state": "opened",
                "reviewers": [{"id": 7, "username": "teammate"}],
            },
            # Reviewer match but MR is merged → not awaiting.
            "414": {
                "iid": 414, "title": "Already merged", "state": "merged",
                "reviewers": [{"id": 42, "username": "you"}],
            },
        },
    )
    _patch_client(monkeypatch, fake)

    r = await client.get(f"/api/v1/repo-links/{rid}/merge-requests")
    assert r.status_code == 200, r.text
    by_iid = {it["iid"]: it for it in r.json()["items"]}
    assert by_iid[412]["awaiting_your_review"] is True
    assert by_iid[413]["awaiting_your_review"] is False
    assert by_iid[414]["awaiting_your_review"] is False


async def test_mr_list_awaiting_flag_without_reviewers(client, monkeypatch):
    """MRs that carry no reviewers (older GitLab or stripped payload) are
    simply not-awaiting; the flag is always present."""
    _cid, rid = await _mk_connection_and_repo(client)
    _patch_client(monkeypatch, FakeGitLab(
        user={"id": 42},
        mrs={"1": {"iid": 1, "title": "no reviewers field", "state": "opened"}},
    ))
    r = await client.get(f"/api/v1/repo-links/{rid}/merge-requests")
    assert r.status_code == 200
    assert r.json()["items"][0]["awaiting_your_review"] is False


async def test_mr_awaiting_degrades_and_does_not_cache_on_transient_user_failure(
    client, monkeypatch,
):
    """A transient /user failure must NOT 502 the list nor cache all-False flags.

    The MR list succeeds but the connection-user lookup fails; this pass returns
    awaiting_your_review=False for every item AND skips the items cache, so the
    next list re-resolves the user (instead of serving stale False for the TTL).
    """
    from nightdesk.integrations import IntegrationError

    _cid, rid = await _mk_connection_and_repo(client)

    class FlakyUser:
        def __init__(self):
            self._inner = FakeGitLab(
                user={"id": 42},
                mrs={"412": {
                    "iid": 412, "title": "x", "state": "opened",
                    "reviewers": [{"id": 42}],
                }},
            )
            self.user_calls = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def current_user(self):
            self.user_calls += 1
            if self.user_calls == 1:
                raise IntegrationError("transient 503", status=503)
            return self._inner.current_user()

    fake = FlakyUser()
    _patch_client(monkeypatch, fake)

    r1 = await client.get(f"/api/v1/repo-links/{rid}/merge-requests")
    assert r1.status_code == 200  # degraded, not 502
    assert r1.json()["items"][0]["awaiting_your_review"] is False

    # Second call: /user succeeds → flag resolves True (proves r1 was not cached).
    r2 = await client.get(f"/api/v1/repo-links/{rid}/merge-requests")
    assert r2.status_code == 200
    assert r2.json()["items"][0]["awaiting_your_review"] is True


# ---------------------------------------------------------------------------
# External links: admin + run-token scopes
# ---------------------------------------------------------------------------


def _mk_running_ticket_with_token(session, *, scopes):
    p = Profile(name=f"rp{id(scopes)}", fs_read=[], fs_write=[], allowed_tools=[],
                denied_tools=[], network_mode="off", network_allowlist=[], secret_keys=[])
    session.add(p)
    session.commit()
    t = create_ticket(session, title="t", prompt="x", priority=0, profile_id=p.id,
                      status="running", source_path="/tmp")
    run = Run(ticket_id=t.id, started_at=datetime.now(timezone.utc),
              worktree_path="", transcript_path="/tmp/c.log", host="h")
    session.add(run)
    session.commit()
    tok = issue_run_token(session, run_id=run.id, ticket_id=t.id, extra_scopes=scopes,
                          max_run_duration_seconds=3600, grace_seconds=300)
    return t.id, tok.cleartext


async def _token_client(app, token):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"Authorization": f"Bearer {token}"})


async def test_external_link_admin_create_list_delete(client, session):
    _cid, rid = await _mk_connection_and_repo(client)
    t = create_ticket(session, title="t", prompt="x", priority=0,
                      profile_id=None, status="inbox")
    r = await client.post(f"/api/v1/tickets/{t.id}/external-links", json={
        "repo_link_id": rid, "kind": "issue", "external_iid": "482", "role": "fixes",
    })
    assert r.status_code == 201, r.text
    link_id = r.json()["id"]
    assert r.json()["author_kind"] == "admin"
    got = await client.get(f"/api/v1/tickets/{t.id}/external-links")
    assert [x["id"] for x in got.json()] == [link_id]
    r = await client.delete(f"/api/v1/tickets/{t.id}/external-links/{link_id}")
    assert r.status_code == 204


async def test_run_token_can_link_own_ticket_only(app, session):
    # Build a connection+repo via a plain admin client.
    admin = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer t"})
    async with admin as ac:
        _cid, rid = await _mk_connection_and_repo(ac)

    tid, token = _mk_running_ticket_with_token(session, scopes=["integrations.link.self"])
    other_tid, _ = _mk_running_ticket_with_token(session, scopes=["integrations.link.self"])

    async with await _token_client(app, token) as ac:
        # Own ticket: allowed, recorded as agent.
        r = await ac.post(f"/api/v1/tickets/{tid}/external-links", json={
            "repo_link_id": rid, "kind": "issue", "external_iid": "1",
        })
        assert r.status_code == 201, r.text
        assert r.json()["author_kind"] == "agent"
        # Someone else's ticket: 403.
        r = await ac.post(f"/api/v1/tickets/{other_tid}/external-links", json={
            "repo_link_id": rid, "kind": "issue", "external_iid": "1",
        })
        assert r.status_code == 403


async def test_run_token_without_link_scope_is_forbidden(app, session):
    admin = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer t"})
    async with admin as ac:
        _cid, rid = await _mk_connection_and_repo(ac)
    tid, token = _mk_running_ticket_with_token(session, scopes=[])  # no grant
    async with await _token_client(app, token) as ac:
        r = await ac.post(f"/api/v1/tickets/{tid}/external-links", json={
            "repo_link_id": rid, "kind": "issue", "external_iid": "1",
        })
        assert r.status_code == 403


async def test_run_token_read_scope_can_browse(app, session, monkeypatch):
    admin = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer t"})
    async with admin as ac:
        _cid, rid = await _mk_connection_and_repo(ac)
    _patch_client(monkeypatch, FakeGitLab(issues={"1": {"iid": 1, "state": "opened"}}))
    tid, token = _mk_running_ticket_with_token(session, scopes=["integrations.read"])
    async with await _token_client(app, token) as ac:
        r = await ac.get(f"/api/v1/repo-links/{rid}/issues")
        assert r.status_code == 200
        # But without link.self it cannot mutate.
        r = await ac.post(f"/api/v1/tickets/{tid}/external-links", json={
            "repo_link_id": rid, "kind": "issue", "external_iid": "1",
        })
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


async def test_import_creates_draft_ticket(client, session, monkeypatch):
    _cid, rid = await _mk_connection_and_repo(client)
    profile = Profile(name="only", fs_read=[], fs_write=[], allowed_tools=[],
                      denied_tools=[], network_mode="off", network_allowlist=[], secret_keys=[])
    session.add(profile)
    proj = Project(name="P", slug="p", source_path="/tmp")
    session.add(proj)
    session.commit()
    ints.set_project_repo_links(session, proj.id, [rid])
    _patch_client(monkeypatch, FakeGitLab(issues={"482": {
        "iid": 482, "title": "Fix heartbeat", "state": "opened",
        "web_url": "https://g/i/482", "description": "breaks on suspend",
    }}))
    r = await client.post(f"/api/v1/repo-links/{rid}/import-ticket",
                         json={"kind": "issue", "external_iid": "482"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft"
    assert body["title"] == "Fix heartbeat"
    assert body["project_id"] == proj.id
    # Seam C: the imported issue body lands in the human-facing description.
    assert body["description"] and "breaks on suspend" in body["description"]
    links = await client.get(f"/api/v1/tickets/{body['id']}/external-links")
    assert links.json()[0]["role"] == "imported_from"


async def test_import_ambiguous_project_is_422(client, session, monkeypatch):
    _cid, rid = await _mk_connection_and_repo(client)
    _patch_client(monkeypatch, FakeGitLab(issues={"1": {"iid": 1, "title": "x", "state": "opened"}}))
    r = await client.post(f"/api/v1/repo-links/{rid}/import-ticket",
                         json={"kind": "issue", "external_iid": "1"})
    assert r.status_code == 422
