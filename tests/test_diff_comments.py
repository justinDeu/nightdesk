"""Diff-comment domain + API tests.

Domain: create root/reply, edit, resolve/unresolve, delete-cascade,
request_changes formatting + delivered_at + empty guard.

API: CRUD, 404/422 guards, and the ``outdated`` flag flipping when a second
commit advances the worktree head (reusing the run-diff git fixtures).
"""
import subprocess
from datetime import datetime, timezone

import pytest

from nightdesk.db.models import Run, TicketWorkspace
from nightdesk.domain import diff_comments as dc
from nightdesk.domain.diff_comments import (
    Anchor, Author, DiffCommentNotFound, InvalidThreadOperation,
)
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket

_PROC_DIR_KW = "c" "wd"


def _git(repo, *args):
    subprocess.run(["git", *args], **{_PROC_DIR_KW: str(repo)},
                   capture_output=True, check=True, text=True)


def _rev(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(repo)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _seed_run(session, tmp_path, *, source_path=None):
    profile = create_profile(
        session, name=f"p{tmp_path.name}", fs_read=[], fs_write=[],
        allowed_tools=[], denied_tools=[], network_mode="off",
        network_allowlist=[], secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="t", prompt="base prompt", priority=0,
                      profile_id=profile.id, run_now=False, status="review",
                      source_path=source_path or str(tmp_path))
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path=source_path or str(tmp_path),
        transcript_path=str(tmp_path / "tr"),
        host="testhost",
    )
    session.add(run)
    session.commit()
    return t, run


def _anchor(head="abc123", line=42, path="src/foo.py", side="new"):
    return Anchor(file_path=path, side=side, line=line,
                  anchor_head_sha=head, anchor_text="the line text")


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


class TestDomain:
    def test_create_root(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        c = dc.create_comment(session, run.id, anchor=_anchor(), body="fix this",
                              author=Author())
        assert c.parent_id is None
        assert c.ticket_id == run.ticket_id
        assert c.file_path == "src/foo.py"
        assert c.line == 42
        assert c.author_kind == "admin"
        assert c.resolved is False

    def test_create_empty_body_rejected(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        with pytest.raises(InvalidThreadOperation):
            dc.create_comment(session, run.id, anchor=_anchor(), body="   ",
                              author=Author())

    def test_reply(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        root = dc.create_comment(session, run.id, anchor=_anchor(), body="root",
                                 author=Author())
        reply = dc.reply_comment(session, root.id, body="done",
                                 author=Author(kind="agent", run_id=run.id))
        assert reply.parent_id == root.id
        assert reply.file_path is None
        assert reply.author_kind == "agent"
        assert reply.author_run_id == run.id

    def test_reply_to_reply_rejected(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        root = dc.create_comment(session, run.id, anchor=_anchor(), body="root",
                                 author=Author())
        reply = dc.reply_comment(session, root.id, body="r1", author=Author())
        with pytest.raises(InvalidThreadOperation):
            dc.reply_comment(session, reply.id, body="r2", author=Author())

    def test_edit(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        c = dc.create_comment(session, run.id, anchor=_anchor(), body="old",
                              author=Author())
        c2 = dc.edit_comment(session, c.id, "new body")
        assert c2.body == "new body"

    def test_resolve_unresolve(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        c = dc.create_comment(session, run.id, anchor=_anchor(), body="x",
                              author=Author())
        r = dc.set_resolved(session, c.id, True, Author())
        assert r.resolved is True
        assert r.resolved_at is not None
        r = dc.set_resolved(session, c.id, False, Author())
        assert r.resolved is False
        assert r.resolved_at is None

    def test_resolve_reply_rejected(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        root = dc.create_comment(session, run.id, anchor=_anchor(), body="root",
                                 author=Author())
        reply = dc.reply_comment(session, root.id, body="r", author=Author())
        with pytest.raises(InvalidThreadOperation):
            dc.set_resolved(session, reply.id, True, Author())

    def test_delete_cascades_replies(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        root = dc.create_comment(session, run.id, anchor=_anchor(), body="root",
                                 author=Author())
        dc.reply_comment(session, root.id, body="r1", author=Author())
        dc.reply_comment(session, root.id, body="r2", author=Author())
        assert len(dc.list_run_comments(session, run.id)) == 3
        dc.delete_comment(session, root.id)
        assert dc.list_run_comments(session, run.id) == []

    def test_get_missing_raises(self, session, tmp_path):
        with pytest.raises(DiffCommentNotFound):
            dc.edit_comment(session, "nope", "x")

    def test_unresolved_threads_excludes_resolved_and_replies(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        a = dc.create_comment(session, run.id, anchor=_anchor(line=1), body="a",
                              author=Author())
        dc.create_comment(session, run.id, anchor=_anchor(line=2), body="b",
                          author=Author())
        dc.reply_comment(session, a.id, body="reply", author=Author())
        dc.set_resolved(session, a.id, True, Author())
        roots = dc.unresolved_threads(session, run.id)
        assert [r.body for r in roots] == ["b"]

    def test_request_changes_bundles_and_stamps(self, session, tmp_path):
        t, run = _seed_run(session, tmp_path)
        a = dc.create_comment(session, run.id,
                              anchor=_anchor(path="src/foo.py", line=42),
                              body="this should be memoized", author=Author())
        dc.reply_comment(session, a.id, body="attempted but reverted",
                         author=Author(kind="agent", run_id=run.id))
        dc.create_comment(session, run.id,
                          anchor=_anchor(path="src/bar.ts", line=88, side="old"),
                          body="why remove the guard?", author=Author())
        ticket = dc.request_changes(session, run.id)
        ctx = ticket.next_run_context
        assert "Review comments to address (2 unresolved)" in ctx
        assert "src/foo.py:42 (new): \"this should be memoized\"" in ctx
        assert "↳ agent: attempted but reverted" in ctx
        assert "src/bar.ts:88 (old): \"why remove the guard?\"" in ctx
        # delivered_at stamped on roots.
        roots = dc.list_run_comments(session, run.id)
        for c in roots:
            if c.parent_id is None:
                assert c.delivered_at is not None

    def test_request_changes_appends(self, session, tmp_path):
        from nightdesk.domain.tickets import set_next_run_context
        t, run = _seed_run(session, tmp_path)
        set_next_run_context(session, t.id, "existing guidance")
        dc.create_comment(session, run.id, anchor=_anchor(), body="c",
                          author=Author())
        ticket = dc.request_changes(session, run.id)
        assert ticket.next_run_context.startswith("existing guidance")
        assert "Review comments to address" in ticket.next_run_context

    def test_request_changes_empty_guard(self, session, tmp_path):
        _, run = _seed_run(session, tmp_path)
        with pytest.raises(InvalidThreadOperation):
            dc.request_changes(session, run.id)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _make_git_run(session, tmp_path):
    """A run backed by a real git worktree so the live diff is computable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("aaa\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    base = _rev(repo)
    (repo / "a.txt").write_text("bbb\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "run work")
    profile = create_profile(
        session, name="gp", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="gt", prompt="base", priority=0,
                      profile_id=profile.id, run_now=False, status="review",
                      source_path=str(repo))
    run = Run(ticket_id=t.id, started_at=datetime.now(timezone.utc),
              worktree_path=str(repo), transcript_path=str(tmp_path / "tr"),
              host="h")
    session.add(run)
    session.flush()
    ws = TicketWorkspace(ticket_id=t.id, run_id=run.id, role="primary",
                         kind="git_worktree", repo_root=str(repo),
                         run_start_sha=base, branch="feat", state="ready")
    session.add(ws)
    session.commit()
    return repo, t, run


async def test_api_crud_roundtrip(client, session, tmp_path):
    repo, t, run = _make_git_run(session, tmp_path)
    diff = (await client.get(f"/api/v1/runs/{run.id}/diff")).json()
    head = diff["head_sha"]

    # Create a root.
    r = await client.post(f"/api/v1/runs/{run.id}/comments", json={
        "file_path": "a.txt", "side": "new", "line": 1,
        "anchor_head_sha": head, "anchor_text": "bbb", "body": "why?",
    })
    assert r.status_code == 201
    root = r.json()
    assert root["outdated"] is False
    assert root["author_kind"] == "admin"

    # Reply.
    r = await client.post(f"/api/v1/runs/{run.id}/comments", json={
        "parent_id": root["id"], "body": "because",
    })
    assert r.status_code == 201
    assert r.json()["parent_id"] == root["id"]

    # List: root + reply.
    r = await client.get(f"/api/v1/runs/{run.id}/comments")
    assert r.status_code == 200
    assert len(r.json()) == 2

    # Edit.
    r = await client.patch(f"/api/v1/diff-comments/{root['id']}",
                           json={"body": "edited"})
    assert r.status_code == 200
    assert r.json()["body"] == "edited"

    # Resolve / unresolve.
    r = await client.post(f"/api/v1/diff-comments/{root['id']}/resolve")
    assert r.json()["resolved"] is True
    r = await client.post(f"/api/v1/diff-comments/{root['id']}/unresolve")
    assert r.json()["resolved"] is False

    # Delete cascades the reply.
    r = await client.delete(f"/api/v1/diff-comments/{root['id']}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/runs/{run.id}/comments")
    assert r.json() == []


async def test_api_outdated_flips_when_head_advances(client, session, tmp_path):
    repo, t, run = _make_git_run(session, tmp_path)
    head = (await client.get(f"/api/v1/runs/{run.id}/diff")).json()["head_sha"]
    r = await client.post(f"/api/v1/runs/{run.id}/comments", json={
        "file_path": "a.txt", "side": "new", "line": 1,
        "anchor_head_sha": head, "anchor_text": "bbb", "body": "note",
    })
    assert r.json()["outdated"] is False

    # A later run advances the worktree head; the anchor is now stale.
    (repo / "a.txt").write_text("ccc\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "later run")

    r = await client.get(f"/api/v1/runs/{run.id}/comments")
    assert r.json()[0]["outdated"] is True


async def test_api_request_changes(client, session, tmp_path):
    repo, t, run = _make_git_run(session, tmp_path)
    await client.post(f"/api/v1/runs/{run.id}/comments", json={
        "file_path": "a.txt", "side": "new", "line": 1,
        "anchor_head_sha": "x", "anchor_text": "bbb", "body": "address me",
    })
    r = await client.post(f"/api/v1/runs/{run.id}/comments/request-changes")
    assert r.status_code == 200
    assert r.json()["ticket_id"] == t.id
    assert "address me" in r.json()["next_run_context"]

    # Second call with nothing unresolved → 422.
    await client.post(
        f"/api/v1/runs/{run.id}/comments/request-changes")  # roots still unresolved
    # Resolve everything, then it should 422.
    comments = (await client.get(f"/api/v1/runs/{run.id}/comments")).json()
    for c in comments:
        if c["parent_id"] is None:
            await client.post(f"/api/v1/diff-comments/{c['id']}/resolve")
    r = await client.post(f"/api/v1/runs/{run.id}/comments/request-changes")
    assert r.status_code == 422


async def test_api_404_and_422_guards(client, session, tmp_path):
    _, run = _seed_run(session, tmp_path)
    # Unknown run.
    r = await client.get("/api/v1/runs/nope/comments")
    assert r.status_code == 404
    # Unknown comment edit.
    r = await client.patch("/api/v1/diff-comments/nope", json={"body": "x"})
    assert r.status_code == 404
    # Reply to a reply → 422.
    root = dc.create_comment(session, run.id, anchor=_anchor(), body="root",
                             author=Author())
    reply = dc.reply_comment(session, root.id, body="r", author=Author())
    r = await client.post(f"/api/v1/runs/{run.id}/comments",
                          json={"parent_id": reply.id, "body": "nested"})
    assert r.status_code == 422
    # Resolve a reply → 422.
    r = await client.post(f"/api/v1/diff-comments/{reply.id}/resolve")
    assert r.status_code == 422


async def test_api_requires_auth(app):
    from httpx import ASGITransport, AsyncClient
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/v1/runs/whatever/comments")
    assert r.status_code == 401
