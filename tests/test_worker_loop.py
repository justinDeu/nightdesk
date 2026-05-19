import asyncio
from datetime import time
import pytest

from nightdesk.db.models import Ticket
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket, get_ticket
from nightdesk.worker.executor import DummyExecutor
from nightdesk.worker.main import WorkerLoop, WorkerSettings


@pytest.mark.anyio
async def test_loop_runs_a_ticket_to_review(session, tmp_path):
    p = create_profile(session, name="p", fs_read=[], fs_write=[], allowed_tools=[],
                        denied_tools=[], network_mode="off", network_allowlist=[],
                        secret_keys=[], default_model=None)
    t = create_ticket(session, title="t", prompt="hello",
                       priority=0, profile_id=p.id, run_now=True, status="queued", cwd=str(tmp_path))
    tid = t.id

    settings = WorkerSettings(
        max_parallel=1,
        window_start=time(22, 0),
        window_end=time(7, 0),
        worktree_root=tmp_path / "work",
        transcript_root=tmp_path / "transcripts",
        secrets={},
        host="testhost",
        executor=DummyExecutor(),
    )
    loop = WorkerLoop(session_factory=lambda: session, settings=settings)
    await loop.tick_once()
    if loop._inproc:
        await asyncio.gather(*loop._inproc.values())

    refreshed = get_ticket(session, tid)
    assert refreshed.status == "review"
    # run-now flag was consumed on pickup.
    assert refreshed.run_now is False
    # Run records the started_as_run_now badge.
    from nightdesk.domain.runs import list_runs
    runs = list_runs(session, ticket_id=tid)
    assert len(runs) == 1
    assert runs[0].started_as_run_now is True
    assert runs[0].exit_status == "success"


@pytest.mark.anyio
async def test_loop_writes_worker_error_event_on_executor_crash(session, tmp_path):
    """A crashing executor must leave a ``worker_error`` event at the end of
    the run transcript so the failure is visible in the transcript view."""
    from nightdesk.domain.runs import list_runs
    from nightdesk.transcript import read_events
    from nightdesk.worker.executor import ExecutionRequest, ExecutionResult

    class CrashingExecutor:
        async def run(self, req: ExecutionRequest) -> ExecutionResult:
            req.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            req.transcript_path.write_text("")
            raise RuntimeError("boom from executor")

    p = create_profile(session, name="p", fs_read=[], fs_write=[], allowed_tools=[],
                        denied_tools=[], network_mode="off", network_allowlist=[],
                        secret_keys=[], default_model=None)
    t = create_ticket(session, title="t", prompt="hello",
                       priority=0, profile_id=p.id, run_now=True, status="queued", cwd=str(tmp_path))
    tid = t.id
    settings = WorkerSettings(
        max_parallel=1,
        window_start=time(22, 0),
        window_end=time(7, 0),
        worktree_root=tmp_path / "work",
        transcript_root=tmp_path / "transcripts",
        secrets={},
        host="testhost",
        executor=CrashingExecutor(),
    )
    loop = WorkerLoop(session_factory=lambda: session, settings=settings)
    await loop.tick_once()
    if loop._inproc:
        await asyncio.gather(*loop._inproc.values())

    runs = list_runs(session, ticket_id=tid)
    assert len(runs) == 1
    run = runs[0]
    assert run.exit_status == "failed"
    assert "boom from executor" in (run.error_summary or "")
    events = list(read_events(run.transcript_path))
    worker_errors = [e for e in events if e.get("type") == "worker_error"]
    assert worker_errors, f"expected worker_error event, got {events}"
    final = events[-1]
    assert final["type"] == "worker_error"
    assert final["kind"] == "executor_error"
    assert "boom from executor" in final["summary"]
    # Stacktrace lands in the same event so the UI can render it as a
    # collapsible block under the summary.
    assert "RuntimeError" in final.get("traceback", "")


@pytest.mark.anyio
async def test_tick_once_returns_immediately_with_rolling_horizon(engine, tmp_path):
    from sqlalchemy.orm import Session
    with Session(engine) as session:
        p = create_profile(session, name="p", fs_read=[], fs_write=[], allowed_tools=[],
                            denied_tools=[], network_mode="off", network_allowlist=[],
                            secret_keys=[], default_model=None)
        t1 = create_ticket(session, title="t1", prompt="hello",
                            priority=0, profile_id=p.id, run_now=True, status="queued", cwd=str(tmp_path))
        t2 = create_ticket(session, title="t2", prompt="hello",
                            priority=0, profile_id=p.id, run_now=True, status="queued", cwd=str(tmp_path))
        t1_id, t2_id = t1.id, t2.id
    settings = WorkerSettings(
        max_parallel=2,
        window_start=time(22, 0),
        window_end=time(7, 0),
        worktree_root=tmp_path / "work",
        transcript_root=tmp_path / "transcripts",
        secrets={},
        host="testhost",
        executor=DummyExecutor(),
    )
    loop = WorkerLoop(session_factory=lambda: Session(engine), settings=settings)
    await loop.tick_once()
    # tick_once should have started both tasks but not awaited them
    assert len(loop._inproc) == 2
    await asyncio.gather(*loop._inproc.values())
    with Session(engine) as session:
        assert get_ticket(session, t1_id).status == "review"
        assert get_ticket(session, t2_id).status == "review"


@pytest.mark.anyio
async def test_cancellation_terminates_executor_cleanly(engine, tmp_path):
    """A slow executor honors cancel_event when ticket is flipped to cancelled."""
    from nightdesk.domain.tickets import transition_status
    from nightdesk.worker.executor import ExecutionResult, ExecutionRequest
    from nightdesk.domain.runs import list_runs
    from sqlalchemy.orm import Session

    class SlowExecutor:
        def __init__(self):
            self.raised = False

        async def run(self, req: ExecutionRequest) -> ExecutionResult:
            req.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            req.transcript_path.write_text("slow start\n")
            try:
                await asyncio.wait_for(req.cancel_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                return ExecutionResult(exit_status="success")
            return ExecutionResult(exit_status="cancelled")

    with Session(engine) as session:
        p = create_profile(session, name="p", fs_read=[], fs_write=[], allowed_tools=[],
                            denied_tools=[], network_mode="off", network_allowlist=[],
                            secret_keys=[], default_model=None)
        t = create_ticket(session, title="t", prompt="hi",
                            priority=0, profile_id=p.id, run_now=True, status="queued", cwd=str(tmp_path))
        t_id = t.id

    executor = SlowExecutor()
    settings = WorkerSettings(
        max_parallel=1,
        window_start=time(22, 0),
        window_end=time(7, 0),
        worktree_root=tmp_path / "work",
        transcript_root=tmp_path / "transcripts",
        secrets={},
        host="testhost",
        executor=executor,
        tick_seconds=0.1,
    )
    loop = WorkerLoop(session_factory=lambda: Session(engine), settings=settings)
    await loop.tick_once()
    assert len(loop._inproc) == 1

    # Wait briefly so executor is in its wait, then force-transition to review
    # (the v2 cancel pathway: API moves the ticket out of 'running').
    await asyncio.sleep(0.2)
    with Session(engine) as session:
        transition_status(session, t_id, "review")

    await asyncio.gather(*loop._inproc.values())

    with Session(engine) as session:
        ticket = get_ticket(session, t_id)
        # Worker observes the forced transition, finishes the run as
        # cancelled, and the ticket remains in 'review'.
        assert ticket.status == "review"
        runs = list_runs(session, ticket_id=t_id)
        assert len(runs) == 1
        assert runs[0].exit_status == "cancelled"


@pytest.mark.anyio
async def test_two_runs_of_same_ticket_produce_distinct_transcripts(engine, tmp_path):
    """Re-running a ticket yields a fresh transcript file keyed by run id."""
    from nightdesk.domain.tickets import requeue
    from nightdesk.domain.runs import list_runs
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        p = create_profile(session, name="p", fs_read=[], fs_write=[], allowed_tools=[],
                            denied_tools=[], network_mode="off", network_allowlist=[],
                            secret_keys=[], default_model=None)
        t = create_ticket(session, title="t", prompt="hi",
                            priority=0, profile_id=p.id, run_now=True, status="queued", cwd=str(tmp_path))
        t_id = t.id

    settings = WorkerSettings(
        max_parallel=1,
        window_start=time(22, 0),
        window_end=time(7, 0),
        worktree_root=tmp_path / "work",
        transcript_root=tmp_path / "transcripts",
        secrets={},
        host="testhost",
        executor=DummyExecutor(),
    )
    loop = WorkerLoop(session_factory=lambda: Session(engine), settings=settings)
    await loop.tick_once()
    await asyncio.gather(*loop._inproc.values())

    with Session(engine) as session:
        requeue(session, t_id)
        # ensure run_now is still True so it picks up
        from nightdesk.domain.tickets import set_run_now
        set_run_now(session, t_id, True)

    await loop.tick_once()
    await asyncio.gather(*loop._inproc.values())

    with Session(engine) as session:
        runs = list_runs(session, ticket_id=t_id)
        assert len(runs) == 2
        paths = {r.transcript_path for r in runs}
        assert len(paths) == 2, f"expected distinct transcript paths, got {paths}"
        from pathlib import Path as _P
        for p_ in paths:
            assert _P(p_).exists(), f"transcript {p_} missing"


@pytest.mark.anyio
async def test_tick_re_reads_max_parallel_from_config(engine, tmp_path):
    """PATCHing max_parallel changes how many tickets the next tick_once picks."""
    from nightdesk.db.models import ConfigRow
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        # Use a 24-hour window so the test is timezone-insensitive.
        cfg = ConfigRow(id=1, worktree_root=str(tmp_path / "work"),
                          transcript_root=str(tmp_path / "transcripts"),
                          window_start="00:00", window_end="23:59", max_parallel=1)
        session.add(cfg); session.commit()
        p = create_profile(session, name="p", fs_read=[], fs_write=[], allowed_tools=[],
                            denied_tools=[], network_mode="off", network_allowlist=[],
                            secret_keys=[], default_model=None)
        # Normal (non run-now) tickets so capacity governs picks.
        t1 = create_ticket(session, title="t1", prompt="hi",
                            priority=0, profile_id=p.id, run_now=False, status="queued", cwd=str(tmp_path))
        t2 = create_ticket(session, title="t2", prompt="hi",
                            priority=0, profile_id=p.id, run_now=False, status="queued", cwd=str(tmp_path))
        t3 = create_ticket(session, title="t3", prompt="hi",
                            priority=0, profile_id=p.id, run_now=False, status="queued", cwd=str(tmp_path))

    class BlockingExecutor:
        def __init__(self):
            self.ev = asyncio.Event()

        async def run(self, req):
            from nightdesk.worker.executor import ExecutionResult
            await self.ev.wait()
            return ExecutionResult(exit_status="success")

    executor = BlockingExecutor()
    settings = WorkerSettings(
        max_parallel=1,  # initial fallback; should be overridden by ConfigRow
        window_start=time(22, 0),
        window_end=time(7, 0),
        worktree_root=tmp_path / "work",
        transcript_root=tmp_path / "transcripts",
        secrets={},
        host="testhost",
        executor=executor,
    )
    loop = WorkerLoop(session_factory=lambda: Session(engine), settings=settings)
    await loop.tick_once()
    assert len(loop._inproc) == 1, "ConfigRow.max_parallel=1 limits to one"

    # Bump max_parallel via DB; next tick should pick up two more.
    with Session(engine) as session:
        row = session.get(ConfigRow, 1)
        row.max_parallel = 3
        session.commit()

    await loop.tick_once()
    assert len(loop._inproc) == 3, f"expected 3 running after raising parallel, got {len(loop._inproc)}"

    # Release executors and let tasks finish so the test cleans up.
    executor.ev.set()
    await asyncio.gather(*loop._inproc.values())


@pytest.mark.anyio
async def test_always_on_window_allows_picks_at_any_time(engine, tmp_path):
    """ConfigRow with window_start == window_end ("00:00" -> "00:00") means
    the worker may dispatch at any wall-clock time. Regression guard against
    the previous in_window behavior, which only matched a single minute when
    start equaled end and silently parked overnight queues during the day.
    """
    from datetime import time as _time
    from nightdesk.db.models import ConfigRow
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        session.add(ConfigRow(
            id=1, worktree_root=str(tmp_path / "w"),
            transcript_root=str(tmp_path / "t"),
            window_start="00:00", window_end="00:00", max_parallel=2,
        ))
        session.commit()
        p = create_profile(session, name="p", fs_read=[], fs_write=[], allowed_tools=[],
                            denied_tools=[], network_mode="off", network_allowlist=[],
                            secret_keys=[], default_model=None)
        t = create_ticket(session, title="t", prompt="hi", priority=0,
                            profile_id=p.id, run_now=False, status="queued",
                            cwd=str(tmp_path))
        tid = t.id

    settings = WorkerSettings(
        max_parallel=1,
        # Deliberately set window to a single minute on the WorkerSettings
        # fallback; the ConfigRow value should win and unlock the pick.
        window_start=_time(3, 14), window_end=_time(3, 14),
        worktree_root=tmp_path / "w", transcript_root=tmp_path / "t",
        secrets={}, host="th", executor=DummyExecutor(),
    )
    loop = WorkerLoop(session_factory=lambda: Session(engine), settings=settings)
    await loop.tick_once()
    assert len(loop._inproc) == 1, "always-on window should permit the pick"
    await asyncio.gather(*loop._inproc.values())
    with Session(engine) as session:
        assert get_ticket(session, tid).status == "review"


@pytest.mark.anyio
async def test_run_forever_exits_when_signal_handler_fires(session, tmp_path):
    """SIGINT/SIGTERM must wake the tick loop and let run_forever return.

    Regression: ``_shutdown`` only forwarded SIGTERM to child subprocesses
    and never broke the ``while True`` loop. Worse, registering an asyncio
    signal handler for SIGINT swallowed Ctrl-C (overriding the default
    KeyboardInterrupt), so the only way to stop the worker was kill -9.
    We simulate the signal by calling the handler the loop installed.
    """
    import signal as _signal
    from datetime import time as _time

    settings = WorkerSettings(
        max_parallel=1,
        window_start=_time(0, 0), window_end=_time(23, 59),
        worktree_root=tmp_path / "w", transcript_root=tmp_path / "t",
        secrets={}, host="testhost", executor=DummyExecutor(),
        tick_seconds=0.05,
    )
    loop = WorkerLoop(session_factory=lambda: session, settings=settings)

    async def fire_signal_soon():
        await asyncio.sleep(0.02)
        asyncio.get_running_loop()._signal_handlers  # ensure loop is up
        # Trigger the same handler the asyncio loop would invoke for SIGINT.
        sig_loop = asyncio.get_running_loop()
        handle = sig_loop._signal_handlers.get(_signal.SIGINT)
        assert handle is not None, "run_forever did not install a SIGINT handler"
        handle._callback(*handle._args)

    fire_task = asyncio.create_task(fire_signal_soon())
    # If the fix regresses, this hangs forever. wait_for caps the damage.
    await asyncio.wait_for(loop.run_forever(), timeout=3.0)
    await fire_task


@pytest.mark.anyio
async def test_additional_dirs_merge_into_fs_write(session, sample_profile, tmp_path):
    """ticket.additional_dirs rw entries are unioned into spec.fs_write."""
    from nightdesk.worker.main import WorkerLoop, WorkerSettings
    from nightdesk.worker.executor import DummyExecutor
    from datetime import time as _time
    from nightdesk.db.models import Ticket
    t = create_ticket(
        session, title="t", prompt="p", priority=0,
        profile_id=sample_profile.id, run_now=False, status="queued",
        additional_dirs=[
            {"path": "/srv/extra", "mode": "rw"},
            {"path": "/srv/readonly", "mode": "ro"},  # warned + dropped
        ],
        cwd="/tmp",
    )
    settings = WorkerSettings(
        max_parallel=1, window_start=_time(0, 0), window_end=_time(23, 59),
        worktree_root=tmp_path / "w", transcript_root=tmp_path / "t",
        secrets={}, host="th", executor=DummyExecutor(),
    )
    from nightdesk.worker.run_one import _profile_to_spec
    spec = _profile_to_spec(session.get(Ticket, t.id))
    assert "/srv/extra" in spec.fs_write
    assert "/srv/readonly" not in spec.fs_write
    # Profile fs_write is preserved.
    assert "/tmp" in spec.fs_write
