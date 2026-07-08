"""Run-result sidecar — a remote run's reported outcome, stored for the host.

A k8s run executes in a DB-less pod. At finish the pod POSTs its structured
outcome (exit status, error summary, session handle, token usage) to
``POST /api/v1/runs/{rid}/result``; the route persists it here as a JSON sidecar
next to the run's transcript, mirroring the diff/fs-snapshot sidecar pattern.

The host stays the authority: ``K8sExecutor`` reads this sidecar back into an
``ExecutionResult`` and hands it to ``run_one``, which runs the *same*
``finish_run`` + usage-persist + pricing path a local run takes. The endpoint
never finalizes the run itself.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def result_sidecar_path(transcript_root: Path | str, run_id: str) -> Path:
    """Deterministic location of a run's uploaded result sidecar."""
    return Path(transcript_root) / "results" / f"{run_id}.json"


def write_result_sidecar(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_result_sidecar(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
