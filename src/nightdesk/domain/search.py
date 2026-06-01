"""Search backend abstraction.

The default backend uses SQLite FTS5 (``tickets_fts``). The Protocol exists so
a later backend (Postgres, Tantivy, Meilisearch) can swap in without API
changes.
"""
from __future__ import annotations

import html
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
    project_id: str | None = None
    project_name: str | None = None
    project_color: str | None = None


def hit_from_ticket(ticket) -> SearchHit:
    """Render a Ticket as a SearchHit for the header/palette/API result lists."""
    proj = ticket.project
    snippet = html.escape((ticket.prompt or "").strip().replace("\n", " ")[:120])
    return SearchHit(
        id=ticket.id,
        title=ticket.title,
        snippet=snippet,
        status=ticket.status,
        project_id=ticket.project_id,
        project_name=proj.name if proj else None,
        project_color=proj.color if proj else None,
    )


class SearchBackend(Protocol):
    def search(
        self, query: str, limit: int = 20, project_id: str | None = None,
    ) -> list[SearchHit]: ...


def _escape_fts_query(q: str) -> str:
    """Build a per-token PREFIX query for FTS5.

    FTS5 treats unquoted strings as a tokenized query with implicit AND. Free
    text from the search box can contain operators (``"``, ``*``, ``:``,
    ``-``, ``AND``, etc.) that would change matching behavior. To keep things
    predictable and avoid operator injection, each whitespace-separated token
    is emitted as a double-quoted prefix term (``"<tok>"*``) joined by spaces,
    which FTS5 combines with implicit AND. Embedded double quotes inside a
    token are stripped so they can't close the wrapper prematurely.

    Example: ``test ti`` -> ``"test"* "ti"*`` (matches "Test ticket").
    """
    cleaned = q.strip()
    if not cleaned:
        return ""
    terms = []
    for token in cleaned.split():
        tok = token.replace('"', "")
        if tok:
            terms.append(f'"{tok}"*')
    return " ".join(terms)


class FTS5SearchBackend:
    """SQLite FTS5 backend tied to a SQLAlchemy session."""

    def __init__(self, session: Session):
        self._session = session

    def search(self, query: str, limit: int = 20, project_id: str | None = None) -> list[SearchHit]:
        match = _escape_fts_query(query)
        if not match:
            return []
        # snippet(): column index 0 = title, with the result limited to 8
        # tokens around the match.
        where = "WHERE tickets_fts MATCH :q "
        params = {"q": match, "n": int(limit)}
        if project_id == "null":
            where += "AND tickets.project_id IS NULL "
        elif project_id:
            where += "AND tickets.project_id = :project_id "
            params["project_id"] = project_id
        stmt = text(
            "SELECT tickets.id AS id, tickets.title AS title, "
            "snippet(tickets_fts, 0, '<b>', '</b>', '…', 8) AS snippet, "
            "tickets.status AS status, tickets.project_id AS project_id, "
            "projects.name AS project_name, projects.color AS project_color "
            "FROM tickets_fts "
            "JOIN tickets ON tickets.id = tickets_fts.id "
            "LEFT JOIN projects ON projects.id = tickets.project_id "
            f"{where}"
            "LIMIT :n"
        )
        try:
            rows = self._session.execute(stmt, params).all()
        except Exception:
            return []
        return [
            SearchHit(
                id=row.id,
                title=row.title,
                snippet=row.snippet,
                status=row.status,
                project_id=row.project_id,
                project_name=row.project_name,
                project_color=row.project_color,
            )
            for row in rows
        ]
