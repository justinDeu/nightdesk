"""FTS5 search backend tests.

We seed the FTS table directly because the SQLAlchemy ORM doesn't fire the
triggers we create in 0002 (the triggers fire on raw SQL writes to ``tickets``,
which `Base.metadata.create_all` doesn't install). Tests for trigger
behavior live in ``test_migration_0002``; tests here exercise the search
backend's query/escaping logic against a hand-populated FTS table.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from nightdesk.domain.search import FTS5SearchBackend, _escape_fts_query


def _setup_fts(session):
    session.execute(text(
        "CREATE VIRTUAL TABLE IF NOT EXISTS tickets_fts USING fts5("
        "title, prompt, id UNINDEXED)"
    ))
    session.commit()


def _seed_ticket(session, *, tid, title, prompt="", status="draft"):
    """Insert a row into both `tickets` and `tickets_fts`."""
    from nightdesk.db.models import Ticket
    t = Ticket(
        id=tid, title=title, prompt=prompt,
        status=status, priority=0, position=0, profile_id=_profile_id(session),
        additional_dirs=[], cwd="/tmp", run_now=False,
    )
    session.add(t)
    session.commit()
    session.execute(text(
        "INSERT INTO tickets_fts(rowid, title, prompt, id) "
        "VALUES ((SELECT rowid FROM tickets WHERE id=:id), :title, :prompt, :id)"
    ), {"id": tid, "title": title, "prompt": prompt})
    session.commit()


def _profile_id(session) -> str:
    from nightdesk.db.models import Profile
    existing = session.query(Profile).first()
    if existing is not None:
        return existing.id
    p = Profile(name="searchp", fs_read=[], fs_write=[], allowed_tools=[],
                 denied_tools=[], network_mode="off", network_allowlist=[],
                 secret_keys=[])
    session.add(p); session.commit()
    return p.id


def test_escape_double_quotes_user_input():
    # Each token becomes a quoted prefix term joined by spaces (implicit AND).
    assert _escape_fts_query("test ti") == '"test"* "ti"*'
    # Embedded quotes are stripped so they can't close the wrapper.
    assert _escape_fts_query('foo "bar"') == '"foo"* "bar"*'
    # Empty / whitespace -> empty string (no search).
    assert _escape_fts_query("") == ""
    assert _escape_fts_query("   ") == ""
    # Operators are wrapped per-token, not honored.
    assert _escape_fts_query("foo OR bar") == '"foo"* "OR"* "bar"*'


def test_search_prefix_matches_partial_token(session):
    # Regression: "test ti" must find a ticket titled "Test ticket".
    _setup_fts(session)
    _seed_ticket(session, tid="a", title="Test ticket")
    _seed_ticket(session, tid="b", title="unrelated thing")
    backend = FTS5SearchBackend(session)
    assert [h.id for h in backend.search("test ti")] == ["a"]


def test_search_returns_empty_on_blank_query(session):
    _setup_fts(session)
    backend = FTS5SearchBackend(session)
    assert backend.search("") == []
    assert backend.search("   ") == []


def test_search_matches_title(session):
    _setup_fts(session)
    _seed_ticket(session, tid="a", title="implement search bar")
    _seed_ticket(session, tid="b", title="unrelated task", prompt="banana")
    backend = FTS5SearchBackend(session)
    hits = backend.search("search")
    assert [h.id for h in hits] == ["a"]
    assert "search" in hits[0].snippet.lower() or "<b>" in hits[0].snippet


def test_search_matches_prompt(session):
    _setup_fts(session)
    _seed_ticket(session, tid="a", title="x", prompt="lorem ipsum dolor")
    _seed_ticket(session, tid="b", title="y", prompt="needle in prompt")
    backend = FTS5SearchBackend(session)
    assert [h.id for h in backend.search("lorem")] == ["a"]
    assert [h.id for h in backend.search("needle")] == ["b"]


def test_search_limit_respected(session):
    _setup_fts(session)
    for i in range(5):
        _seed_ticket(session, tid=f"t{i}", title=f"title-{i} common")
    backend = FTS5SearchBackend(session)
    hits = backend.search("common", limit=3)
    assert len(hits) == 3
