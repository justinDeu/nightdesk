"""nightdesk-runner — the in-pod entrypoint (pod PID 1).

DB-less, FastAPI-less. Reads a RunSpec (mounted Secret file), clones the repo at
``base_ref``, branches, runs the *same* backend the host would run (no bwrap —
the pod is the sandbox), streams the transcript to the API over the run token,
and at finish uploads the structured diff + result. See
docs/design/session-suite/k8s-executor.md ("nightdesk-runner").

Deliberately reuses the pure host pieces: ``backends`` (prepare_launch +
execute/translate), ``domain.diff`` (structured diff), ``worker.headless_prompt``.
Only the source (worktree -> clone) and sink (file -> HTTP) differ.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import traceback
from pathlib import Path
from typing import Optional

from nightdesk.backends import LaunchContext, get_backend
from nightdesk.domain.diff import compute_run_diff, diff_to_json
from nightdesk.runner.api_sink import ApiSink
from nightdesk.runner.clone import CloneError, clone_workspace
from nightdesk.runner.runspec import RunSpec
from nightdesk.worker.executor import ExecutionRequest
from nightdesk.worker.headless_prompt import build_headless_prompt

log = logging.getLogger(__name__)

# In-pod layout (writable). The repo is cloned under WORKDIR/repo.
WORKDIR = Path(os.environ.get("NIGHTDESK_POD_WORKDIR", "/workspace"))
POD_HOME = Path(os.environ.get("NIGHTDESK_POD_HOME", "/nightdesk/home"))
_TRANSCRIPT_POLL_SECONDS = 1.0


def load_runspec() -> RunSpec:
    path = os.environ.get("NIGHTDESK_RUNSPEC_PATH")
    if not path:
        raise SystemExit("NIGHTDESK_RUNSPEC_PATH is not set")
    return RunSpec.from_json(Path(path).read_text(encoding="utf-8"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _creds_env(spec) -> dict:
    """Legacy claude_credentials -> ANTHROPIC_* env (mirrors host _build_env).

    Endpoint-based creds are rendered by ``prepare_launch``; this covers a
    profile that carries a raw api_key/auth_token instead of an endpoint.
    """
    env: dict[str, str] = {}
    creds = getattr(spec, "claude_credentials", None) or {}
    src = creds.get("source")
    if src == "api_key" and creds.get("value"):
        env["ANTHROPIC_API_KEY"] = str(creds["value"])
    elif src == "auth_token" and creds.get("value"):
        env["ANTHROPIC_AUTH_TOKEN"] = str(creds["value"])
    if creds.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = str(creds["base_url"])
    for k, v in (getattr(spec, "custom_env", None) or {}).items():
        env[str(k)] = str(v)
    return env


def _pod_env(spec, runspec: RunSpec) -> dict:
    """Base pod env: writable HOME/XDG dirs + creds + the NIGHTDESK_* metadata.

    No bwrap SANDBOX_HOME here — the pod is the sandbox, so paths are real."""
    home = str(POD_HOME)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "USER": "nightdesk",
        "LOGNAME": "nightdesk",
        "SHELL": "/bin/sh",
        "CLAUDE_CONFIG_DIR": f"{home}/.claude",
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
    }
    env.update(_creds_env(spec))
    env.update(runspec.base_env)  # NIGHTDESK_* callback metadata
    return env


class _TranscriptTailer:
    """Poll a growing NDJSON transcript file and POST complete new lines.

    Batches whole lines only (a partial trailing line is held back until its
    newline arrives) so a POSTed batch never splits an event.
    """

    def __init__(self, path: Path, sink: ApiSink):
        self.path = path
        self.sink = sink
        self._offset = 0
        self._buf = ""

    def _drain_once(self) -> None:
        if not self.path.exists():
            return
        data = self.path.read_text(encoding="utf-8", errors="replace")
        if len(data) <= self._offset:
            return
        chunk = data[self._offset:]
        self._offset = len(data)
        self._buf += chunk
        lines = self._buf.split("\n")
        self._buf = lines.pop()  # trailing partial (or "")
        events = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                import json
                events.append(json.loads(line))
            except ValueError:
                continue
        if events:
            self.sink.post_transcript(events)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                self._drain_once()
            except Exception:
                log.exception("transcript tail iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=_TRANSCRIPT_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    def flush(self) -> None:
        # Final drain; also emit any held partial line if it is a full event.
        self._drain_once()
        tail = self._buf.strip()
        if tail:
            try:
                import json
                self.sink.post_transcript([json.loads(tail)])
                self._buf = ""
            except ValueError:
                pass


async def run(runspec: RunSpec, *, sink: ApiSink, workdir: Optional[Path] = None) -> int:
    """Execute one turn end-to-end in the pod. Returns a process exit code."""
    workdir = Path(workdir or WORKDIR)
    repo_path = workdir / "repo"
    transcript_path = workdir / "transcript.ndjson"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    POD_HOME.mkdir(parents=True, exist_ok=True)

    # 1. Clone. A clone failure is a workspace_error (reported, non-zero exit).
    try:
        run_start_sha = clone_workspace(
            runspec.remote_url, runspec.base_ref, runspec.branch, repo_path,
        )
    except CloneError as exc:
        log.error("clone failed: %s", exc)
        sink.post_result({
            "exit_status": "failed",
            "error_summary": f"workspace clone failed: {exc}",
            "failure_kind": "workspace_error",
        })
        return 3

    backend = get_backend(runspec.backend_code)
    primary = runspec.endpoints.get(runspec.primary_endpoint_id) if runspec.primary_endpoint_id else None
    launch_ctx = LaunchContext(
        spec=runspec.spec, endpoint=primary, run_id=runspec.run_id,
        ticket_id=runspec.ticket_id, workspace_dir=repo_path,
        scratch_root=workdir / "scratch",
        http_port=_free_port() if backend.wants_http else None,
        endpoints=runspec.endpoints, model_assignments=runspec.model_assignments,
    )
    plan = backend.prepare_launch(launch_ctx)
    env = {**_pod_env(runspec.spec, runspec), **plan.env}
    prompt = build_headless_prompt(
        ticket_id=runspec.ticket_id, ticket_title=runspec.ticket_title,
        base_prompt=runspec.base_prompt, run_intent=runspec.run_intent,
        workspace_path=str(repo_path),
        next_run_context=None, last_run_summary=None,
    )
    request = ExecutionRequest(
        ticket_id=runspec.ticket_id, prompt=prompt, working_dir=repo_path,
        transcript_path=transcript_path, bwrap_argv=list(plan.cmd), env=env,
        permission_spec=runspec.spec, cancel_event=asyncio.Event(),
        seq_start=0, http_port=launch_ctx.http_port,
        launch_meta=launch_ctx.backend_state,
    )

    # 2. Run the backend + stream the transcript concurrently.
    tailer = _TranscriptTailer(transcript_path, sink)
    stop = asyncio.Event()
    tail_task = asyncio.create_task(tailer.run(stop))
    try:
        result = await backend.execute(request)
    finally:
        stop.set()
        try:
            await tail_task
        except Exception:
            log.exception("transcript tailer failed")
        tailer.flush()

    # 3. Structured diff (run_start_sha..HEAD + untracked) -> sidecar upload.
    try:
        diff = compute_run_diff(str(repo_path), run_start_sha, branch=runspec.branch)
        sink.post_diff(diff_to_json(diff))
        head_sha = diff.head_sha or run_start_sha
    except Exception:
        log.exception("diff computation/upload failed")
        head_sha = run_start_sha

    # 4. Result upload (host reads this back into an ExecutionResult).
    usage = getattr(result, "usage", None)
    payload = {
        "exit_status": result.exit_status,
        "error_summary": result.error_summary,
        "session_id": result.session_id,
        "session_ref": getattr(result, "session_ref", None),
        "usage_by_model": getattr(result, "usage_by_model", None),
        "workspace": {
            "run_start_sha": run_start_sha,
            "base_sha": run_start_sha,
            "head_sha": head_sha,
        },
    }
    if usage is not None:
        payload["usage"] = {
            "model": getattr(usage, "model", None),
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "cache_read_tokens": getattr(usage, "cache_read_tokens", 0),
            "cache_write_tokens": getattr(usage, "cache_write_tokens", 0),
            "cost_usd": getattr(usage, "cost_usd", None),
        }
    sink.post_result(payload)

    return 0 if result.exit_status == "success" else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        runspec = load_runspec()
    except Exception as exc:
        log.error("could not load RunSpec: %s\n%s", exc, traceback.format_exc())
        return 2
    sink = ApiSink(runspec.api_url, runspec.run_token, runspec.run_id)
    try:
        return asyncio.run(run(runspec, sink=sink))
    except Exception as exc:
        log.error("runner crashed: %s\n%s", exc, traceback.format_exc())
        try:
            sink.post_result({
                "exit_status": "failed",
                "error_summary": f"runner crashed: {exc}",
                "failure_kind": "run_failed",
            })
        except Exception:
            pass
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
