"""nightdesk-runner: clone -> run (stubbed backend) -> POST transcript/diff/result.

No cluster and no real SDK: a local bare repo is the 'remote' and the backend is
a stub that writes canonical NDJSON and edits a file (per the _sdk_runner test
approach). The ApiSink is captured in memory.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from nightdesk.backends import LaunchPlan
from nightdesk.domain.cost import RunUsage
from nightdesk.runner import main as runner_main
from nightdesk.runner.runspec import RunSpec
from nightdesk.worker.executor import ExecutionResult


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def remote_repo(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "app.py").write_text("print('v1')\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    return remote


class _CapturingSink:
    def __init__(self):
        self.transcript_batches = []
        self.diff = None
        self.result = None

    def post_transcript(self, events):
        self.transcript_batches.append(events)
        return True

    def post_diff(self, payload):
        self.diff = payload
        return True

    def post_result(self, payload):
        self.result = payload
        return True


class _FakeBackend:
    code = "fake"
    wants_http = False

    def prepare_launch(self, ctx):
        return LaunchPlan(cmd=["true"], env={"FAKE": "1"})

    async def execute(self, req):
        # Simulate the agent: write canonical NDJSON + edit a tracked file.
        with req.transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "meta", "seq": 0, "ticket_id": req.ticket_id}) + "\n")
            f.write(json.dumps({"type": "assistant_text", "seq": 1, "text": "editing"}) + "\n")
        (Path(req.working_dir) / "app.py").write_text("print('v2')\n")
        (Path(req.working_dir) / "new.txt").write_text("brand new\n")
        return ExecutionResult(
            exit_status="success", session_id="sess-9",
            usage=RunUsage(model="claude-x", input_tokens=7, output_tokens=2,
                           cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.03),
        )


def _runspec(remote):
    from nightdesk.domain.permissions import PermissionSpec
    return RunSpec(
        run_id="run-1", ticket_id="tkt-1", ticket_title="T",
        backend_code="fake", base_prompt="do it", run_intent="first_run",
        api_url="http://nd.example:8765", run_token="ndr_x",
        remote_url=str(remote), base_ref="main", branch="nightdesk/tkt/run",
        spec=PermissionSpec(backend="fake"),
    )


def test_runner_clone_run_and_writeback(tmp_path, remote_repo, monkeypatch):
    monkeypatch.setattr(runner_main, "get_backend", lambda code: _FakeBackend())
    monkeypatch.setattr(runner_main, "POD_HOME", tmp_path / "home")
    sink = _CapturingSink()
    rc = asyncio.run(runner_main.run(_runspec(remote_repo), sink=sink,
                                     workdir=tmp_path / "work"))
    assert rc == 0

    # Transcript streamed.
    all_events = [e for batch in sink.transcript_batches for e in batch]
    types = [e["type"] for e in all_events]
    assert "meta" in types and "assistant_text" in types

    # Diff captured the edit + the new file.
    assert sink.diff is not None
    paths = {f["path"] for f in sink.diff["files"]}
    assert "app.py" in paths
    assert "new.txt" in paths
    assert sink.diff["branch"] == "nightdesk/tkt/run"

    # Result carries exit/usage/session + workspace shas.
    assert sink.result["exit_status"] == "success"
    assert sink.result["session_id"] == "sess-9"
    assert sink.result["usage"]["input_tokens"] == 7
    assert sink.result["workspace"]["run_start_sha"]
    assert sink.result["workspace"]["head_sha"]


def test_runner_clone_failure_reports_workspace_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_main, "get_backend", lambda code: _FakeBackend())
    monkeypatch.setattr(runner_main, "POD_HOME", tmp_path / "home")
    sink = _CapturingSink()
    spec = _runspec(tmp_path / "does-not-exist.git")
    rc = asyncio.run(runner_main.run(spec, sink=sink, workdir=tmp_path / "work"))
    assert rc == 3
    assert sink.result["exit_status"] == "failed"
    assert sink.result["failure_kind"] == "workspace_error"
