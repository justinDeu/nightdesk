"""Search backend abstraction.

The default backend uses SQLite FTS5 (``tickets_fts``). The Protocol exists so
a later backend (Postgres, Tantivy, Meilisearch) can swap in without API
changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class SearchHit:
    id: str
    title: str
    snippet: str
    status: str


class SearchBackend(Protocol):
    def search(self, query: str, limit: int = 20) -> list[SearchHit]: ...


def _escape_fts_query(q: str) -> str:
    """Quote the user query for FTS5 phrase syntax.

    FTS5 treats unquoted strings as a tokenized query with implicit AND. Free
    text from the search box can contain operators (``"``, ``*``, ``:``,
    ``-``, ``AND``, etc.) that would change matching behavior. To keep things
    predictable we double-quote the whole query and strip any embedded double
    quotes that would close it prematurely.
    """
    cleaned = q.replace('"', "").strip()
    if not cleaned:
        return ""
    return f'"{cleaned}"'


class FTS5SearchBackend:
    """SQLite FTS5 backend tied to a SQLAlchemy session."""

    def __init__(self, session: Session):
        self._session = session

    def search(self, query: str, limit: int = 20) -> list[SearchHit]:
        match = _escape_fts_query(query)
        if not match:
            return []
        # snippet(): column index 0 = title, with the result limited to 8
        # tokens around the match.
        stmt = text(
            "SELECT tickets.id AS id, tickets.title AS title, "
            "snippet(tickets_fts, 0, '<b>', '</b>', '…', 8) AS snippet, "
            "tickets.status AS status "
            "FROM tickets_fts "
            "JOIN tickets ON tickets.id = tickets_fts.id "
            "WHERE tickets_fts MATCH :q "
            "LIMIT :n"
        )
        try:
            rows = self._session.execute(stmt, {"q": match, "n": int(limit)}).all()
        except Exception:
            return []
        return [
            SearchHit(
                id=row.id, title=row.title, snippet=row.snippet, status=row.status
            )
            for row in rows
        ]
