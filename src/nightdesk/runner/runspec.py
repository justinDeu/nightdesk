"""RunSpec — the self-contained job description a k8s runner pod executes.

The host ``K8sExecutor`` serializes a fully-resolved run into a ``RunSpec`` and
delivers it to the pod (as a file/env value inside the per-run Secret). The pod
(``nightdesk.runner``) deserializes it, clones the repo, reconstructs a
``LaunchContext``, and runs the *same* backend the host would have run — no DB,
no bwrap, just the run token to write results back over HTTP.

This module is deliberately dependency-light (stdlib + the pure permission /
provider dataclasses) so both the host side and the DB-less, FastAPI-less pod
image can import it.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

from nightdesk.backends import Assignment
from nightdesk.domain.permissions import PermissionSpec
from nightdesk.domain.providers import ResolvedEndpoint


def _spec_to_dict(spec: PermissionSpec) -> dict:
    return asdict(spec)


def _spec_from_dict(d: dict) -> PermissionSpec:
    known = PermissionSpec().__dict__.keys()
    return PermissionSpec(**{k: v for k, v in d.items() if k in known})


def _endpoint_to_dict(ep: ResolvedEndpoint) -> dict:
    return asdict(ep)


def _endpoint_from_dict(d: dict) -> ResolvedEndpoint:
    known = {
        "id", "label", "protocol_kind", "base_url", "credential",
        "credential_source", "extra", "default_model", "models",
        "provider_id", "provider_name", "vendor", "harness_lock",
    }
    return ResolvedEndpoint(**{k: v for k, v in d.items() if k in known})


@dataclass
class RunSpec:
    """Everything a runner pod needs to execute one turn and report back.

    Carries decrypted provider credentials (in ``spec``/``endpoints``) and the
    run token, so it is only ever delivered inside the per-run k8s Secret, never
    a ConfigMap or the image (see the design doc's "Secret tradeoff").
    """

    run_id: str
    ticket_id: str
    ticket_title: str
    backend_code: str
    base_prompt: str
    run_intent: str

    # API write-back.
    api_url: str
    run_token: str

    # Primary git workspace to clone.
    remote_url: str
    base_ref: str
    branch: str

    # Resolved launch inputs (mirror the host's LaunchContext build).
    spec: PermissionSpec
    endpoints: dict[str, ResolvedEndpoint] = field(default_factory=dict)
    primary_endpoint_id: Optional[str] = None
    model_assignments: dict[str, Assignment] = field(default_factory=dict)

    # Extra run env (NIGHTDESK_* callback metadata etc.); merged under the
    # backend's rendered env in-pod. Never carries the bearer token.
    base_env: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "run_id": self.run_id,
            "ticket_id": self.ticket_id,
            "ticket_title": self.ticket_title,
            "backend_code": self.backend_code,
            "base_prompt": self.base_prompt,
            "run_intent": self.run_intent,
            "api_url": self.api_url,
            "run_token": self.run_token,
            "remote_url": self.remote_url,
            "base_ref": self.base_ref,
            "branch": self.branch,
            "spec": _spec_to_dict(self.spec),
            "endpoints": {k: _endpoint_to_dict(v) for k, v in self.endpoints.items()},
            "primary_endpoint_id": self.primary_endpoint_id,
            "model_assignments": {
                k: {"endpoint_id": v.endpoint_id, "model": v.model}
                for k, v in self.model_assignments.items()
            },
            "base_env": dict(self.base_env),
        }
        return json.dumps(payload)

    @classmethod
    def from_json(cls, raw: str) -> "RunSpec":
        d = json.loads(raw)
        return cls(
            run_id=d["run_id"],
            ticket_id=d["ticket_id"],
            ticket_title=d.get("ticket_title", ""),
            backend_code=d["backend_code"],
            base_prompt=d.get("base_prompt", ""),
            run_intent=d.get("run_intent", "first_run"),
            api_url=d["api_url"],
            run_token=d["run_token"],
            remote_url=d["remote_url"],
            base_ref=d.get("base_ref") or "HEAD",
            branch=d["branch"],
            spec=_spec_from_dict(d.get("spec") or {}),
            endpoints={
                k: _endpoint_from_dict(v)
                for k, v in (d.get("endpoints") or {}).items()
            },
            primary_endpoint_id=d.get("primary_endpoint_id"),
            model_assignments={
                k: Assignment(v["endpoint_id"], v["model"])
                for k, v in (d.get("model_assignments") or {}).items()
            },
            base_env=dict(d.get("base_env") or {}),
        )
