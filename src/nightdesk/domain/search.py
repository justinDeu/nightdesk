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


_FTS_CREATE_TRIGGERS = (
    "CREATE TRIGGER tickets_ai AFTER INSERT ON tickets BEGIN "
    "INSERT INTO tickets_fts(rowid, title, prompt, id) "
    "VALUES (new.rowid, new.title, new.prompt, new.id); END;",
    "CREATE TRIGGER tickets_ad AFTER DELETE ON tickets BEGIN "
    "DELETE FROM tickets_fts WHERE rowid = old.rowid; END;",
    "CREATE TRIGGER tickets_au AFTER UPDATE ON tickets BEGIN "
    "DELETE FROM tickets_fts WHERE rowid = old.rowid; "
    "INSERT INTO tickets_fts(rowid, title, prompt, id) "
    "VALUES (new.rowid, new.title, new.prompt, new.id); END;",
)


def ensure_fts_index(engine) -> None:
    """Make ``tickets_fts`` consistent and self-maintaining.

    SQLite ``batch_alter_table`` rebuilds the tickets table, which silently
    drops its triggers (this happened in migration 0013, leaving every ticket
    created since unindexed and therefore invisible to text search). Run this
    after migrations on startup: it recreates the FTS triggers and rebuilds the
    index whenever it has drifted out of sync with the tickets table, so a
    forgotten trigger can never again leave tickets unsearchable.
    """
    with engine.begin() as conn:
        has_fts = conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tickets_fts'"
        )).first()
        if not has_fts:
            return
        for name in ("tickets_ai", "tickets_ad", "tickets_au"):
            conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        for ddl in _FTS_CREATE_TRIGGERS:
            conn.execute(text(ddl))
        n_tickets = conn.execute(text("SELECT COUNT(*) FROM tickets")).scalar() or 0
        n_fts = conn.execute(text("SELECT COUNT(*) FROM tickets_fts")).scalar() or 0
        if n_tickets != n_fts:
            conn.execute(text("DELETE FROM tickets_fts"))
            conn.execute(text(
                "INSERT INTO tickets_fts(rowid, title, prompt, id) "
                "SELECT rowid, title, prompt, id FROM tickets"
            ))


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
