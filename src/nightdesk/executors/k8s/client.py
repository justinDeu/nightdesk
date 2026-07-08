"""Kubernetes API access for the executor — the only module that imports
``kubernetes``.

``K8sClient`` wraps ``CoreV1Api`` behind a tiny, normalized surface
(create/read/delete pod+secret, list, logs) so the executor and reconciler never
touch raw k8s objects. ``PodStatus`` collapses the phase + container-termination
detail the failure matrix keys off (OOMKilled, DeadlineExceeded, image-pull
failure) into one flat struct.

``FakeK8sClient`` is a scripted in-memory implementation used by the whole test
suite — there is no cluster in CI. The ``kubernetes`` import is lazy so importing
this module (and running ``uv run pytest``) never requires the optional
``[k8s]`` extra.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

# Terminal container reasons the failure matrix distinguishes.
REASON_OOM = "OOMKilled"
REASON_DEADLINE = "DeadlineExceeded"
REASON_IMAGE_PULL = "ImagePullBackOff"


@dataclass
class PodStatus:
    """Normalized pod state. ``phase`` is the k8s pod phase; ``reason`` carries
    the container-termination / waiting reason when present."""

    phase: str = "Unknown"           # Pending | Running | Succeeded | Failed | Unknown
    ready: bool = False
    exit_code: Optional[int] = None
    reason: Optional[str] = None
    message: Optional[str] = None

    @property
    def terminal(self) -> bool:
        return self.phase in ("Succeeded", "Failed")


class K8sClientProtocol(Protocol):
    def create_secret(self, namespace: str, name: str, string_data: dict,
                      labels: dict) -> None: ...
    def create_pod(self, namespace: str, manifest: dict) -> None: ...
    def read_pod_status(self, namespace: str, name: str) -> Optional[PodStatus]: ...
    def delete_pod(self, namespace: str, name: str) -> None: ...
    def delete_secret(self, namespace: str, name: str) -> None: ...
    def list_pods(self, namespace: str, label_selector: str) -> list[dict]: ...
    def read_pod_log(self, namespace: str, name: str) -> str: ...


class K8sClient:
    """Real cluster client. Imports ``kubernetes`` lazily (optional extra)."""

    def __init__(self, *, in_cluster: bool = False,
                 kubeconfig_path: Optional[str] = None):
        try:
            from kubernetes import client, config  # type: ignore
        except ImportError as exc:  # pragma: no cover - requires the extra
            raise RuntimeError(
                "the kubernetes client is not installed; install nightdesk with "
                "the [k8s] extra to use the k8s execution target"
            ) from exc
        if in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config(config_file=kubeconfig_path or None)
        self._core = client.CoreV1Api()
        self._client = client

    def create_secret(self, namespace, name, string_data, labels):  # pragma: no cover
        body = self._client.V1Secret(
            metadata=self._client.V1ObjectMeta(name=name, labels=labels),
            string_data=string_data,
            type="Opaque",
        )
        self._core.create_namespaced_secret(namespace=namespace, body=body)

    def create_pod(self, namespace, manifest):  # pragma: no cover
        self._core.create_namespaced_pod(namespace=namespace, body=manifest)

    def read_pod_status(self, namespace, name):  # pragma: no cover
        from kubernetes.client.rest import ApiException
        try:
            pod = self._core.read_namespaced_pod_status(name=name, namespace=namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return _normalize_pod_status(pod)

    def delete_pod(self, namespace, name):  # pragma: no cover
        from kubernetes.client.rest import ApiException
        try:
            self._core.delete_namespaced_pod(name=name, namespace=namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def delete_secret(self, namespace, name):  # pragma: no cover
        from kubernetes.client.rest import ApiException
        try:
            self._core.delete_namespaced_secret(name=name, namespace=namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def list_pods(self, namespace, label_selector):  # pragma: no cover
        pods = self._core.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector,
        )
        out = []
        for pod in pods.items:
            out.append({
                "name": pod.metadata.name,
                "labels": dict(pod.metadata.labels or {}),
                "status": _normalize_pod_status(pod),
            })
        return out

    def read_pod_log(self, namespace, name):  # pragma: no cover
        from kubernetes.client.rest import ApiException
        try:
            return self._core.read_namespaced_pod_log(name=name, namespace=namespace)
        except ApiException:
            return ""


def _normalize_pod_status(pod) -> PodStatus:  # pragma: no cover - needs real objects
    """Collapse a k8s V1Pod into a flat PodStatus."""
    phase = getattr(pod.status, "phase", None) or "Unknown"
    exit_code: Optional[int] = None
    reason: Optional[str] = None
    message: Optional[str] = None
    ready = False
    statuses = getattr(pod.status, "container_statuses", None) or []
    for cs in statuses:
        state = cs.state
        if getattr(state, "terminated", None) is not None:
            term = state.terminated
            exit_code = term.exit_code
            reason = term.reason
            message = term.message
        elif getattr(state, "waiting", None) is not None:
            reason = state.waiting.reason
            message = state.waiting.message
        ready = bool(getattr(cs, "ready", False))
    return PodStatus(phase=phase, ready=ready, exit_code=exit_code,
                     reason=reason, message=message)


# ---------------------------------------------------------------------------
# In-memory fake for tests (no cluster).
# ---------------------------------------------------------------------------


class FakeK8sClient:
    """Scripted in-memory K8s client for the executor/reconciler tests.

    A created pod walks a ``status_script`` of :class:`PodStatus` values, one per
    ``read_pod_status`` call, repeating the last once exhausted. Tests inject the
    script (per pod name, or a default) to drive every failure-matrix row —
    Pending->Running->Succeeded, ImagePullBackOff-forever, DeadlineExceeded,
    OOMKilled, node-loss. Every create/delete is recorded so tests can assert
    the Secret lifecycle (created before the pod, deleted on every exit path).
    """

    def __init__(self, *, default_script: Optional[list[PodStatus]] = None):
        self.secrets: dict[tuple[str, str], dict] = {}
        self.pods: dict[tuple[str, str], dict] = {}
        self.default_script = default_script or [
            PodStatus(phase="Pending"),
            PodStatus(phase="Running", ready=True),
            PodStatus(phase="Succeeded", exit_code=0, reason="Completed"),
        ]
        self.scripts: dict[str, list[PodStatus]] = {}
        self.logs: dict[str, str] = {}
        self.calls: list[tuple] = []
        self._cursor: dict[tuple[str, str], int] = {}

    def script_pod(self, name: str, statuses: list[PodStatus]) -> None:
        self.scripts[name] = list(statuses)

    def set_log(self, name: str, text: str) -> None:
        self.logs[name] = text

    def create_secret(self, namespace, name, string_data, labels):
        self.calls.append(("create_secret", namespace, name))
        self.secrets[(namespace, name)] = {
            "string_data": dict(string_data), "labels": dict(labels),
        }

    def create_pod(self, namespace, manifest):
        name = manifest["metadata"]["name"]
        self.calls.append(("create_pod", namespace, name))
        self.pods[(namespace, name)] = {
            "manifest": manifest,
            "labels": dict(manifest["metadata"].get("labels") or {}),
            "deleted": False,
        }
        self._cursor[(namespace, name)] = 0

    def read_pod_status(self, namespace, name):
        key = (namespace, name)
        if key not in self.pods or self.pods[key]["deleted"]:
            return None
        script = self.scripts.get(name, self.default_script)
        idx = self._cursor.get(key, 0)
        status = script[min(idx, len(script) - 1)]
        self._cursor[key] = idx + 1
        return status

    def delete_pod(self, namespace, name):
        self.calls.append(("delete_pod", namespace, name))
        if (namespace, name) in self.pods:
            self.pods[(namespace, name)]["deleted"] = True

    def delete_secret(self, namespace, name):
        self.calls.append(("delete_secret", namespace, name))
        self.secrets.pop((namespace, name), None)

    def list_pods(self, namespace, label_selector):
        wanted = _parse_label_selector(label_selector)
        out = []
        for (ns, name), pod in self.pods.items():
            if ns != namespace or pod["deleted"]:
                continue
            labels = pod["labels"]
            if all(labels.get(k) == v for k, v in wanted.items()):
                status = self.read_pod_status(namespace, name) or PodStatus()
                out.append({"name": name, "labels": labels, "status": status})
        return out

    def read_pod_log(self, namespace, name):
        return self.logs.get(name, "")


def _parse_label_selector(selector: str) -> dict:
    out: dict[str, str] = {}
    for part in (selector or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out
