from pathlib import Path

import pytest

from nightdesk.worker.executor import DummyExecutor, ShellExecutor, ExecutionRequest


@pytest.mark.anyio
async def test_dummy_executor_writes_transcript_and_returns_success(tmp_path):
    transcript = tmp_path / "t.log"
    req = ExecutionRequest(
        ticket_id="t1",
        prompt="say hi",
        working_dir=tmp_path,
        transcript_path=transcript,
        bwrap_argv=["bwrap", "echo", "hi"],
        env={},
    )
    res = await DummyExecutor().run(req)
    assert res.exit_status == "success"
    assert "say hi" in transcript.read_text()


@pytest.mark.anyio
async def test_shell_executor_runs_real_command(tmp_path):
    transcript = tmp_path / "t.log"
    req = ExecutionRequest(
        ticket_id="t1",
        prompt="ignored",
        working_dir=tmp_path,
        transcript_path=transcript,
        bwrap_argv=["sh", "-c", "echo hello"],
        env={},
    )
    res = await ShellExecutor().run(req)
    assert res.exit_status == "success"
    assert "hello" in transcript.read_text()
