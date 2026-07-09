"""Backend capability listing API (JSON, ``/api/v1/backends``).

Exposes the declarative :mod:`nightdesk.domain.backend_capabilities` catalog
so the profile editor can render backend choice, field-group visibility, and
model slots from data instead of a hard-coded list. See
``docs/design/providers-and-endpoints.md`` ("Layer 2: Harnesses").

Each entry also carries a ``runtime`` status (binary found/not-found, source,
version) computed by :mod:`nightdesk.domain.backend_runtime` so the Settings
UI can render a hard yes/no instead of an ambiguous "auto" placeholder.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from nightdesk.domain import scopes as sc
from nightdesk.api.schemas import BackendOut
from nightdesk.db.models import ConfigRow
from nightdesk.domain.backend_capabilities import all_capabilities
from nightdesk.domain.backend_runtime import BackendRuntimeStatus, runtime_status_for


def _runtime_out(status: BackendRuntimeStatus | None) -> dict | None:
    if status is None:
        return None
    return {
        "binary_path_override": status.binary_path_override,
        "resolved_path": status.resolved_path,
        "source": status.source,
        "found": status.found,
        "version": status.version,
    }


def _backend_out(cap, runtime: BackendRuntimeStatus | None) -> dict:
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
        "runtime": _runtime_out(runtime),
    }


def build_router(get_session, bearer_token: str, scoped) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/backends",
        tags=["backends"],
        dependencies=[Depends(scoped(sc.TICKETS_READ))],
    )

    @router.get("", response_model=list[BackendOut])
    async def list_backends(session: Session = Depends(get_session)):
        config_row = session.get(ConfigRow, 1)
        return [
            _backend_out(cap, runtime_status_for(cap.code, config_row))
            for cap in all_capabilities()
        ]

    return router
