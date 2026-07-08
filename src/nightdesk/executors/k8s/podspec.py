"""Manifest builders for a per-run Secret + Pod (plain dicts).

Kept pure and cluster-free so they can be golden-tested without ``kubernetes``.
The Secret carries the RunSpec (decrypted creds + run token); the Pod mounts it
as a file, is labelled for reconciliation, is deadline-bounded, and runs a
locked-down ``securityContext``. See docs/design/session-suite/k8s-executor.md
("Runner image", "Critical details").
"""
from __future__ import annotations

from typing import Optional

from nightdesk.domain.k8s_config import K8sConfig

LABEL_RUN_ID = "nightdesk/run-id"
LABEL_TICKET_ID = "nightdesk/ticket-id"
LABEL_MANAGED = "nightdesk/managed"

# Where the RunSpec Secret is mounted and the env var pointing the runner at it.
RUNSPEC_MOUNT_PATH = "/nightdesk/runspec"
RUNSPEC_FILENAME = "runspec.json"
RUNSPEC_ENV = "NIGHTDESK_RUNSPEC_PATH"
_RUNSPEC_VOLUME = "runspec"
_GIT_CREDS_VOLUME = "git-credentials"
_GIT_CREDS_MOUNT = "/nightdesk/git-credentials"


def secret_name(run_id: str) -> str:
    return f"nd-run-{run_id}"


def pod_name(run_id: str) -> str:
    return f"nd-run-{run_id}"


def _labels(run_id: str, ticket_id: str) -> dict:
    return {
        LABEL_MANAGED: "true",
        LABEL_RUN_ID: run_id,
        LABEL_TICKET_ID: ticket_id,
    }


def build_secret(run_id: str, ticket_id: str, runspec_json: str) -> dict:
    """A per-run Opaque Secret holding the RunSpec JSON.

    Returned as ``(name, string_data, labels)`` inputs for
    ``K8sClient.create_secret`` rather than a full manifest — the client wraps it
    in a V1Secret. One Secret per run, deleted on every exit path.
    """
    return {
        "name": secret_name(run_id),
        "string_data": {RUNSPEC_FILENAME: runspec_json},
        "labels": _labels(run_id, ticket_id),
    }


def build_pod(
    cfg: K8sConfig,
    *,
    run_id: str,
    ticket_id: str,
    deadline_seconds: int,
) -> dict:
    """A single-shot runner Pod manifest (dict).

    Labelled for reconciliation, deadline-bounded via ``activeDeadlineSeconds``,
    resource-shaped from config, non-root with all caps dropped, mounting the
    per-run Secret read-only and (optionally) a cluster git-credentials Secret.
    """
    resources: dict = {"requests": {}, "limits": {}}
    if cfg.cpu_request:
        resources["requests"]["cpu"] = cfg.cpu_request
    if cfg.mem_request:
        resources["requests"]["memory"] = cfg.mem_request
    if cfg.cpu_limit:
        resources["limits"]["cpu"] = cfg.cpu_limit
    if cfg.mem_limit:
        resources["limits"]["memory"] = cfg.mem_limit
    if not resources["requests"]:
        resources.pop("requests")
    if not resources["limits"]:
        resources.pop("limits")

    volumes = [{
        "name": _RUNSPEC_VOLUME,
        "secret": {"secretName": secret_name(run_id)},
    }]
    volume_mounts = [{
        "name": _RUNSPEC_VOLUME,
        "mountPath": RUNSPEC_MOUNT_PATH,
        "readOnly": True,
    }]
    env = [{
        "name": RUNSPEC_ENV,
        "value": f"{RUNSPEC_MOUNT_PATH}/{RUNSPEC_FILENAME}",
    }]
    if cfg.git_credentials_secret:
        volumes.append({
            "name": _GIT_CREDS_VOLUME,
            "secret": {"secretName": cfg.git_credentials_secret},
        })
        volume_mounts.append({
            "name": _GIT_CREDS_VOLUME,
            "mountPath": _GIT_CREDS_MOUNT,
            "readOnly": True,
        })
        env.append({"name": "NIGHTDESK_GIT_CREDENTIALS_DIR", "value": _GIT_CREDS_MOUNT})

    container: dict = {
        "name": "runner",
        "image": cfg.runner_image,
        "command": ["nightdesk-runner"],
        "env": env,
        "volumeMounts": volume_mounts,
        "securityContext": {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    if resources:
        container["resources"] = resources

    spec: dict = {
        "restartPolicy": "Never",
        "activeDeadlineSeconds": int(deadline_seconds),
        "automountServiceAccountToken": False,
        "containers": [container],
        "volumes": volumes,
    }
    if cfg.node_selector:
        spec["nodeSelector"] = dict(cfg.node_selector)
    if cfg.runtime_class:
        spec["runtimeClassName"] = cfg.runtime_class

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name(run_id),
            "labels": _labels(run_id, ticket_id),
        },
        "spec": spec,
    }


def label_selector(*, run_id: Optional[str] = None) -> str:
    """Selector for nightdesk-managed runner pods (optionally one run)."""
    parts = [f"{LABEL_MANAGED}=true"]
    if run_id:
        parts.append(f"{LABEL_RUN_ID}={run_id}")
    return ",".join(parts)
