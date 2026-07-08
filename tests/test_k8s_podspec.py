"""Golden-ish checks for the Secret/Pod manifest builders and RunSpec wire form."""
from nightdesk.backends import Assignment
from nightdesk.domain.k8s_config import K8sConfig
from nightdesk.domain.permissions import PermissionSpec
from nightdesk.domain.providers import ResolvedEndpoint
from nightdesk.executors.k8s import podspec
from nightdesk.runner.runspec import RunSpec


def _cfg(**kw):
    base = dict(api_url="http://nd.example:8765", runner_image="img:9", namespace="nd")
    base.update(kw)
    return K8sConfig(**base)


def test_secret_carries_runspec_and_labels():
    s = podspec.build_secret("r1", "t1", '{"x":1}')
    assert s["name"] == "nd-run-r1"
    assert s["string_data"][podspec.RUNSPEC_FILENAME] == '{"x":1}'
    assert s["labels"][podspec.LABEL_RUN_ID] == "r1"
    assert s["labels"][podspec.LABEL_TICKET_ID] == "t1"


def test_pod_shape():
    cfg = _cfg(cpu_request="500m", cpu_limit="2", mem_request="1Gi", mem_limit="4Gi",
               node_selector={"disktype": "ssd"}, runtime_class="gvisor",
               git_credentials_secret="git-creds")
    pod = podspec.build_pod(cfg, run_id="r1", ticket_id="t1", deadline_seconds=1800)
    meta, spec = pod["metadata"], pod["spec"]
    assert meta["name"] == "nd-run-r1"
    assert meta["labels"][podspec.LABEL_MANAGED] == "true"
    assert spec["activeDeadlineSeconds"] == 1800
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    assert spec["nodeSelector"] == {"disktype": "ssd"}
    assert spec["runtimeClassName"] == "gvisor"
    c = spec["containers"][0]
    assert c["image"] == "img:9"
    assert c["command"] == ["nightdesk-runner"]
    assert c["resources"] == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "4Gi"},
    }
    sc = c["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    # RunSpec mount + git-creds mount present.
    mount_names = {m["name"] for m in c["volumeMounts"]}
    assert "runspec" in mount_names and "git-credentials" in mount_names
    assert any(e["name"] == podspec.RUNSPEC_ENV for e in c["env"])


def test_pod_omits_empty_resources_and_optional_fields():
    pod = podspec.build_pod(_cfg(), run_id="r", ticket_id="t", deadline_seconds=60)
    spec = pod["spec"]
    assert "nodeSelector" not in spec
    assert "runtimeClassName" not in spec
    assert "resources" not in spec["containers"][0]
    # Only the runspec volume when no git-credentials secret is configured.
    assert [v["name"] for v in spec["volumes"]] == ["runspec"]


def test_label_selector():
    assert podspec.label_selector() == "nightdesk/managed=true"
    assert podspec.label_selector(run_id="r1") == "nightdesk/managed=true,nightdesk/run-id=r1"


def test_runspec_round_trip():
    spec = PermissionSpec(backend="claude_sdk", allowed_tools=["Read"],
                          denied_tools=["Bash"], default_model="claude-x",
                          claude_credentials={"source": "api_key", "value": "sk-1"})
    ep = ResolvedEndpoint(id="e1", label="l", protocol_kind="anthropic",
                          base_url="https://api", credential="sk-2",
                          credential_source="api_key", vendor="anthropic",
                          default_model="claude-x", models=["claude-x"])
    rs = RunSpec(
        run_id="r1", ticket_id="t1", ticket_title="T", backend_code="claude_sdk",
        base_prompt="go", run_intent="first_run",
        api_url="http://nd.example:8765", run_token="ndr_x",
        remote_url="git@h:repo.git", base_ref="main", branch="feat/x",
        spec=spec, endpoints={"e1": ep}, primary_endpoint_id="e1",
        model_assignments={"primary": Assignment("e1", "claude-x")},
        base_env={"NIGHTDESK_RUN_ID": "r1"},
    )
    back = RunSpec.from_json(rs.to_json())
    assert back.run_id == "r1"
    assert back.spec.allowed_tools == ["Read"]
    assert back.spec.claude_credentials == {"source": "api_key", "value": "sk-1"}
    assert back.endpoints["e1"].credential == "sk-2"
    assert back.endpoints["e1"].protocol_kind == "anthropic"
    assert back.model_assignments["primary"].model == "claude-x"
    assert back.base_ref == "main" and back.branch == "feat/x"
