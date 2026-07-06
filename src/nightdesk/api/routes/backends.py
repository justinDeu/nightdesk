"""Backend capability listing API (JSON, ``/api/v1/backends``).

Exposes the declarative :mod:`nightdesk.domain.backend_capabilities` catalog
so the profile editor can render backend choice, field-group visibility, and
model slots from data instead of a hard-coded list. See
``docs/design/providers-and-endpoints.md`` ("Layer 2: Harnesses").
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from nightdesk.api.auth import require_token_cookie_or_bearer
from nightdesk.api.schemas import BackendOut
from nightdesk.domain.backend_capabilities import all_capabilities


def _backend_out(cap) -> dict:
    return {
        "code": cap.code,
        "label": cap.label,
        "summary": cap.summary,
        "protocol_kinds": sorted(cap.protocol_kinds),
        "multi_endpoint": cap.multi_endpoint,
        "requires_provider": cap.requires_provider,
        "enabled": cap.enabled,
        "executable": cap.executable,
        "group_keys": list(cap.group_keys),
        "model_slots": [
            {"name": s.name, "label": s.label, "required": s.required}
            for s in cap.model_slots
        ],
        "capabilities": sorted(c.value for c in cap.capabilities),
    }


def build_router(bearer_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/backends",
        tags=["backends"],
        dependencies=[Depends(require_token_cookie_or_bearer(bearer_token))],
    )

    @router.get("", response_model=list[BackendOut])
    async def list_backends():
        return [_backend_out(cap) for cap in all_capabilities()]

    return router
