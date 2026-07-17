"""Slot -> Assignment resolution shared by ticket runs and resident agents.

Split out of ``worker.run_one`` so callers that need model/endpoint
resolution but not the rest of ticket-execution (workspace prep, sandbox,
webhooks, run tokens, pricing, ...) can import just this. The resident-agent
host (``worker.session_host``) is exactly such a caller: a lightweight,
per-agent process that must resolve a profile's model assignments the same
way ticket runs do, without pulling in ``run_one``'s module-scope imports.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from nightdesk.backends.base import Assignment
from nightdesk.domain import backend_capabilities as bc

if TYPE_CHECKING:
    from nightdesk.domain.providers import ResolvedEndpoint


def compute_model_assignments(
    descriptor: bc.BackendCapability,
    backend_config: dict,
    *,
    primary: "Optional[ResolvedEndpoint]",
    default_model: Optional[str],
) -> dict[str, Assignment]:
    """Resolve the (partial) slot -> Assignment map for a launch.

    See ``docs/design/providers-and-endpoints.md``, "The rendering contract":
    full-pin only applies on ``*_compat`` endpoints with a default model set;
    a first-party primary with no explicit overrides stays unpinned so the
    harness's own alias resolution (e.g. CC's opus/haiku aliases) still
    works. Static per-slot overrides in ``backend_config`` win over full-pin.
    Per-agent slots (``agent:<name>``, opencode only) read their own
    ``model``/``endpoint_id`` out of the matching ``backend_config["agents"]``
    entry, defaulting to the primary's assignment/id when unset.
    """
    backend_config = backend_config or {}
    slots = bc.slots_for(descriptor, backend_config)
    assignments: dict[str, Assignment] = {}

    full_pin = (
        primary is not None
        and primary.protocol_kind.endswith("_compat")
        and bool(default_model)
    )
    if full_pin:
        for slot in slots:
            if not slot.name.startswith("agent:"):
                assignments[slot.name] = Assignment(primary.id, default_model)

    for slot in slots:
        if slot.name.startswith("agent:"):
            continue
        override = backend_config.get(slot.name)
        if isinstance(override, str) and override and primary is not None:
            assignments[slot.name] = Assignment(primary.id, override)

    for slot in slots:
        if not slot.name.startswith("agent:"):
            continue
        agent_name = slot.name.split(":", 1)[1]
        agent_cfg = next(
            (a for a in (backend_config.get("agents") or [])
             if isinstance(a, dict) and a.get("name") == agent_name),
            {},
        )
        endpoint_id = agent_cfg.get("endpoint_id") or (
            primary.id if primary is not None else None
        )
        model = agent_cfg.get("model")
        if not model:
            inherited = assignments.get("primary")
            model = inherited.model if inherited is not None else default_model
        if endpoint_id and model:
            assignments[slot.name] = Assignment(endpoint_id, model)

    return assignments
