from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from nightdesk.api.auth import require_bearer
from nightdesk.api.schemas import SearchHit as SearchHitSchema
from nightdesk.domain.search import FTS5SearchBackend


def build_router(get_session, bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["search"],
        dependencies=[Depends(require_bearer(bearer_token))],
    )

    @router.get("/search", response_model=list[SearchHitSchema])
    async def search(
        q: str = Query(default=""),
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
    ):
        backend = FTS5SearchBackend(session)
        hits = backend.search(q, limit=limit)
        return [SearchHitSchema(**h.__dict__) for h in hits]

    return router
