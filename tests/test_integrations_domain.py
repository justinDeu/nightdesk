"""Domain layer for integrations: CRUD, TTL cache, refresh pass, import."""
from datetime import datetime, timezone

import pytest

from nightdesk.db.models import Connection, Profile, Project
from nightdesk.domain import integrations as ints
from nightdesk.domain.tickets import create_ticket
from nightdesk.integrations import AuthError, RateLimited


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class FakeGitLab:
    """Stand-in for GitLabClient; canned issue/MR data, no network."""

    def __init__(self, *, issues=None, mrs=None, auth_error=None, rate_limited=False):
        self.issues = issues or {}
        self.mrs = mrs or {}
        self.auth_error = auth_error
        self.rate_limited = rate_limited
        self.calls = []

    def test_auth(self):
        if self.auth_error:
            raise self.auth_error
        return {"version": "17.0"}

    def _page(self, data, iids):
        from nightdesk.integrations.gitlab import Page
        if self.rate_limited:
            raise RateLimited("slow", retry_after=1)
        items = list(data.values())
        if iids:
            want = {str(i) for i in iids}
            items = [i for i in items if str(i["iid"]) in want]
        return Page(items=items, next_page_token=None)

    def list_issues(self, pid, *, state=None, search=None, page_token=None, iids=None):
        self.calls.append(("list_issues", iids))
        return self._page(self.issues, iids)

    def list_mrs(self, pid, *, state=None, search=None, source_branch=None, page_token=None, iids=None):
        self.calls.append(("list_mrs", iids))
        return self._page(self.mrs, iids)

    def get_issue(self, pid, iid):
        return self.issues[str(iid)]

    def get_mr(self, pid, iid):
        return self.mrs[str(iid)]


@pytest.fixture
def gitlab_connection(session):
    return ints.create_connection(
        session, name="corp", provider="gitlab",
        base_url="https://gitlab.example.com", auth_kind="pat",
        credential="enc",
    )


@pytest.fixture
def repo(session, gitlab_connection):
    return ints.create_repo_link(
        session, connection_id=gitlab_connection.id,
        external_kind="gitlab_project", external_id="55",
        external_path="grp/app", display_name="app",
        git_remote_url="git@gitlab.example.com:grp/app.git",
        web_url="https://gitlab.example.com/grp/app",
    )


@pytest.fixture
def a_ticket(session):
    p = Profile(name="p", fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
                network_mode="off", network_allowlist=[], secret_keys=[])
    session.add(p)
    session.commit()
    return create_ticket(session, title="t", prompt="x", priority=0,
                         profile_id=p.id, status="draft", source_path="/tmp")


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------


def test_ttl_cache_expires():
    c = ints.TTLCache(ttl_seconds=60)
    c.set("k", {"v": 1}, now=1000.0)
    assert c.get("k", now=1010.0) == {"v": 1}
    assert c.get("k", now=1061.0) is None      # expired
    assert c.get("missing") is None


# ---------------------------------------------------------------------------
# Connection CRUD
# ---------------------------------------------------------------------------


def test_connection_crud_and_provider_guard(session):
    c = ints.create_connection(session, name="a", provider="gitlab",
                               base_url="https://g", auth_kind="pat")
    assert ints.get_connection(session, c.id).name == "a"
    with pytest.raises(ints.UnknownProvider):
        ints.create_connection(session, name="b", provider="jira_cloud",
                               base_url="https://x", auth_kind="pat")
    with pytest.raises(ints.UnknownAuthKind):
        ints.create_connection(session, name="c", provider="gitlab",
                               base_url="https://g", auth_kind="oauth")
    with pytest.raises(ints.ConnectionNameTaken):
        ints.create_connection(session, name="a", provider="gitlab",
                               base_url="https://g2", auth_kind="pat")


def test_delete_connection_blocked_by_repo_links(session, gitlab_connection, repo):
    with pytest.raises(ints.ConnectionInUse):
        ints.delete_connection(session, gitlab_connection.id)
    ints.delete_repo_link(session, repo.id)
    ints.delete_connection(session, gitlab_connection.id)
    with pytest.raises(ints.ConnectionNotFound):
        ints.get_connection(session, gitlab_connection.id)


def test_repo_link_unique_per_connection(session, gitlab_connection, repo):
    with pytest.raises(ints.RepoLinkExists):
        ints.create_repo_link(session, connection_id=gitlab_connection.id,
                              external_kind="gitlab_project", external_id="55")


def test_repo_link_normalizes_remote_url(session, repo):
    assert repo.git_remote_url == "gitlab.example.com/grp/app"


# ---------------------------------------------------------------------------
# Project attach (M:N, ordered)
# ---------------------------------------------------------------------------


def test_project_repo_link_attach_is_ordered(session, gitlab_connection, repo):
    r2 = ints.create_repo_link(session, connection_id=gitlab_connection.id,
                               external_kind="gitlab_project", external_id="66",
                               external_path="grp/infra")
    proj = Project(name="P", slug="p", source_path="/tmp")
    session.add(proj)
    session.commit()
    ints.set_project_repo_links(session, proj.id, [r2.id, repo.id])
    got = ints.list_project_repo_links(session, proj.id)
    assert [r.id for r in got] == [r2.id, repo.id]
    # Reorder + subset.
    ints.set_project_repo_links(session, proj.id, [repo.id])
    assert [r.id for r in ints.list_project_repo_links(session, proj.id)] == [repo.id]
    assert ints.projects_for_repo_link(session, repo.id)[0].id == proj.id


# ---------------------------------------------------------------------------
# External links
# ---------------------------------------------------------------------------


def test_external_link_create_delete_and_uniqueness(session, repo, a_ticket):
    link = ints.create_external_link(session, ticket_id=a_ticket.id,
                                     repo_link_id=repo.id, kind="issue",
                                     external_iid="12", role="fixes")
    assert link.role == "fixes"
    assert ints.list_ticket_external_links(session, a_ticket.id)[0].id == link.id
    with pytest.raises(ints.ExternalLinkExists):
        ints.create_external_link(session, ticket_id=a_ticket.id,
                                  repo_link_id=repo.id, kind="issue", external_iid="12")
    with pytest.raises(ValueError):
        ints.create_external_link(session, ticket_id=a_ticket.id,
                                  repo_link_id=repo.id, kind="issue",
                                  external_iid="9", role="bogus")
    ints.delete_external_link(session, link.id)
    assert ints.list_ticket_external_links(session, a_ticket.id) == []


def test_delete_repo_link_blocked_by_external_links(session, repo, a_ticket):
    ints.create_external_link(session, ticket_id=a_ticket.id, repo_link_id=repo.id,
                              kind="issue", external_iid="1")
    with pytest.raises(ints.RepoLinkInUse):
        ints.delete_repo_link(session, repo.id)


# ---------------------------------------------------------------------------
# Refresh pass
# ---------------------------------------------------------------------------


def test_refresh_updates_snapshot_and_emits_change(session, repo, a_ticket, monkeypatch):
    link = ints.create_external_link(session, ticket_id=a_ticket.id, repo_link_id=repo.id,
                                     kind="merge_request", external_iid="7",
                                     role="produced_mr", state="opened")
    fake = FakeGitLab(mrs={"7": {
        "iid": 7, "title": "fix: x", "state": "merged",
        "web_url": "https://g/mr/7", "merge_status": "merged",
        "source_branch": "fix/x", "target_branch": "main",
    }})
    monkeypatch.setattr(ints, "client_for", lambda *a, **k: fake)
    emitted = []
    monkeypatch.setattr(ints, "emit_external_link_state_changed",
                        lambda l, old: emitted.append((l.id, old, l.state)))
    summary = ints.refresh_all_links(session, secret_box=None)
    session.refresh(link)
    assert link.state == "merged"
    assert link.state_detail["source_branch"] == "fix/x"
    assert link.synced_at is not None
    assert summary.updated == 1
    assert emitted == [(link.id, "opened", "merged")]
    # Batched: one list call carrying the iid, not a per-item GET.
    assert fake.calls == [("list_mrs", ["7"])]


def test_refresh_skips_archived_tickets(session, repo, a_ticket, monkeypatch):
    a_ticket.status = "archived"
    session.commit()
    ints.create_external_link(session, ticket_id=a_ticket.id, repo_link_id=repo.id,
                              kind="issue", external_iid="1", state="opened")
    fake = FakeGitLab(issues={"1": {"iid": 1, "state": "closed"}})
    monkeypatch.setattr(ints, "client_for", lambda *a, **k: fake)
    summary = ints.refresh_all_links(session, secret_box=None)
    assert summary.checked == 0
    assert fake.calls == []


def test_refresh_honors_rate_limit(session, repo, a_ticket, monkeypatch):
    ints.create_external_link(session, ticket_id=a_ticket.id, repo_link_id=repo.id,
                              kind="issue", external_iid="1", state="opened")
    fake = FakeGitLab(issues={"1": {"iid": 1, "state": "closed"}}, rate_limited=True)
    monkeypatch.setattr(ints, "client_for", lambda *a, **k: fake)
    summary = ints.refresh_all_links(session, secret_box=None)
    assert summary.updated == 0  # deferred, no crash


# ---------------------------------------------------------------------------
# test_connection status writing
# ---------------------------------------------------------------------------


def test_test_connection_writes_status(session, gitlab_connection, monkeypatch):
    monkeypatch.setattr(ints, "client_for", lambda *a, **k: FakeGitLab())
    c = ints.test_connection(session, gitlab_connection, secret_box=None)
    assert c.status == "ok"
    assert c.last_checked_at is not None

    monkeypatch.setattr(ints, "client_for",
                        lambda *a, **k: FakeGitLab(auth_error=AuthError("401 expired", status=401)))
    c = ints.test_connection(session, gitlab_connection, secret_box=None)
    assert c.status == "auth_failed"
    assert "401" in c.status_detail


# ---------------------------------------------------------------------------
# Import as draft
# ---------------------------------------------------------------------------


def test_import_creates_draft_with_imported_from_link(session, gitlab_connection, repo, monkeypatch):
    profile = Profile(name="only", fs_read=[], fs_write=[], allowed_tools=[],
                      denied_tools=[], network_mode="off", network_allowlist=[], secret_keys=[])
    session.add(profile)
    proj = Project(name="Proj", slug="proj", source_path="/tmp")
    session.add(proj)
    session.commit()
    ints.set_project_repo_links(session, proj.id, [repo.id])

    fake = FakeGitLab(issues={"12": {
        "iid": 12, "title": "Fix heartbeat", "state": "opened",
        "web_url": "https://g/issues/12", "description": "It breaks on suspend.",
    }})
    monkeypatch.setattr(ints, "client_for", lambda *a, **k: fake)

    ticket = ints.import_issue_as_draft(session, repo, "12", secret_box=None)
    assert ticket.status == "draft"
    assert ticket.title == "Fix heartbeat"
    assert ticket.project_id == proj.id       # inferred from the single attachment
    assert ticket.profile_id == profile.id    # sole profile
    # Body is quoted, framed as data, never a bare instruction.
    assert "> It breaks on suspend." in ticket.prompt
    assert "quoted as reference data" in ticket.prompt
    links = ints.list_ticket_external_links(session, ticket.id)
    assert len(links) == 1
    assert links[0].role == "imported_from"
    assert links[0].external_iid == "12"


def test_import_requires_resolvable_project(session, repo, monkeypatch):
    # Repo attached to no project and none passed -> cannot build a workspace.
    fake = FakeGitLab(issues={"1": {"iid": 1, "title": "x", "state": "opened"}})
    monkeypatch.setattr(ints, "client_for", lambda *a, **k: fake)
    with pytest.raises(ints.ImportError_):
        ints.import_issue_as_draft(session, repo, "1", secret_box=None)
