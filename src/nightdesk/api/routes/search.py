from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from nightdesk.domain import scopes as sc
from nightdesk.api.schemas import SearchHit as SearchHitSchema
from nightdesk.db.models import Project
from nightdesk.domain.query import parse_query, search_tickets
from nightdesk.domain.search import hit_from_ticket


def build_router(get_session, bearer_token: str, scoped) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["search"],
        dependencies=[Depends(scoped(sc.TICKETS_READ))],
    )

    @router.get("/search", response_model=list[SearchHitSchema])
    async def search(
        q: str = Query(default=""),
        limit: int = Query(default=20, ge=1, le=100),
        project_id: str | None = Query(default=None),
        session: Session = Depends(get_session),
    ):
        """Ticket search with the full query language.

        ``q`` accepts ``field=value`` terms, OR/NOT, parens and quoted phrases,
        with bare words falling through to full-text search. The legacy
        ``project_id`` param is still honoured and ANDed onto the query.
        """
        query = (q or "").strip()
        if not query:
            # Preserve the original contract: a blank query matches nothing.
            return []
        if project_id == "null":
            query = "project=none " + query
        elif project_id:
            proj = session.get(Project, project_id)
            slug = proj.slug if proj else "__missing__"
            query = f"project={slug} " + query
        tickets = search_tickets(session, parse_query(query), limit=limit)
        return [SearchHitSchema(**hit_from_ticket(t).__dict__) for t in tickets]

    return router
