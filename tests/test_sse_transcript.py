import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket
from nightdesk.transcript import now_iso, write_event


@pytest.fixture
def app(engine, tmp_path):
    return create_app(engine=engine, bearer_token="t",
                       static_root=tmp_path / "static",
                       transcript_root=tmp_path / "transcripts",
                       worktree_root=tmp_path / "work")


async def test_sse_transcript_sends_existing_content(app, session, tmp_path):
    from nightdesk.domain.runs import start_run
    p = create_profile(session, name="sse", fs_read=[], fs_write=[], allowed_tools=[],
                        denied_tools=[], network_mode="off", network_allowlist=[],
                        secret_keys=[], default_model=None)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, source_path="/tmp", run_now=False)
    log = tmp_path / "transcripts" / "run-abc.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("hello world\n")
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                              cookies={"nightdesk_token": "t"}) as ac:
        async with ac.stream("GET", f"/api/v1/tickets/{t.id}/transcript") as r:
            chunks = []
            async for chunk in r.aiter_text():
                chunks.append(chunk)
                if "event: end" in chunk or len("".join(chunks)) > 200:
                    break
        assert any("hello world" in c for c in chunks)


def _make_running_ticket(session, tmp_path):
    p = create_profile(session, name="sse", fs_read=[], fs_write=[], allowed_tools=[],
                       denied_tools=[], network_mode="off", network_allowlist=[],
                       secret_keys=[], default_model=None)
    t = create_ticket(session, title="t", prompt="p",
                      priority=0, profile_id=p.id, source_path="/tmp", run_now=False)
    return p, t


def _events_from_chunks(chunks):
    """Parse ``data: <json>`` payloads out of a collected SSE stream."""
    text = "".join(chunks)
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: "):]
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict):
                out.append(evt)
    return out


async def test_sse_since_seq_drops_already_rendered_events(app, session, tmp_path):
    """The page renders events up to ``data-last-seq`` server-side; the SSE
    replay must not re-emit them. With since_seq set, no meta dup and ordering
    is preserved for the events that do stream."""
    from nightdesk.domain.runs import start_run

    _p, t = _make_running_ticket(session, tmp_path)
    log = tmp_path / "transcripts" / "run-canonical.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as f:
        write_event(f, {"type": "meta", "ts": now_iso(), "seq": 0,
                        "run_id": "r1", "ticket_id": t.id})
        write_event(f, {"type": "assistant_text", "ts": now_iso(), "seq": 1,
                        "text": "first"})
        write_event(f, {"type": "assistant_text", "ts": now_iso(), "seq": 2,
                        "text": "second"})
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        # The page already rendered seq 0 and 1; only seq 2 should stream.
        async with ac.stream(
            "GET", f"/api/v1/tickets/{t.id}/transcript?since_seq=1"
        ) as r:
            chunks = []
            async for chunk in r.aiter_text():
                chunks.append(chunk)
                if "event: end" in chunk:
                    break

    evts = _events_from_chunks(chunks)
    seqs = [e.get("seq") for e in evts]
    # No duplicate meta: seq 0 (the meta) is below the watermark and dropped.
    assert not any(e.get("type") == "meta" for e in evts)
    assert seqs == [2]


async def test_sse_emits_event_ids(app, session, tmp_path):
    """Each canonical event carries an ``id: <seq>`` SSE field so the browser
    tracks Last-Event-ID and can resume on reconnect."""
    from nightdesk.domain.runs import start_run

    _p, t = _make_running_ticket(session, tmp_path)
    log = tmp_path / "transcripts" / "run-ids.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as f:
        write_event(f, {"type": "meta", "ts": now_iso(), "seq": 0,
                        "run_id": "r1", "ticket_id": t.id})
        write_event(f, {"type": "assistant_text", "ts": now_iso(), "seq": 1,
                        "text": "a"})
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        async with ac.stream("GET", f"/api/v1/tickets/{t.id}/transcript") as r:
            chunks = []
            async for chunk in r.aiter_text():
                chunks.append(chunk)
                if "event: end" in chunk:
                    break

    text = "".join(chunks)
    assert "id: 0\n" in text
    assert "id: 1\n" in text


async def test_sse_last_event_id_header_resumes_above_watermark(app, session, tmp_path):
    """On reconnect the browser sends Last-Event-ID; the endpoint must resume
    strictly above it rather than replaying the whole live transcript (which
    the client would re-append, ballooning the DOM). The header floor wins even
    when the since_seq query is lower or absent."""
    from nightdesk.domain.runs import start_run

    _p, t = _make_running_ticket(session, tmp_path)
    log = tmp_path / "transcripts" / "run-resume.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as f:
        for seq, txt in [(0, "zero"), (1, "one"), (2, "two"), (3, "three")]:
            write_event(f, {"type": "assistant_text", "ts": now_iso(),
                            "seq": seq, "text": txt})
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        # Simulate a reconnect that already received through seq 2. since_seq
        # is absent (default -1); the header alone must gate the replay.
        async with ac.stream(
            "GET", f"/api/v1/tickets/{t.id}/transcript",
            headers={"Last-Event-ID": "2"},
        ) as r:
            chunks = []
            async for chunk in r.aiter_text():
                chunks.append(chunk)
                if "event: end" in chunk:
                    break

    evts = _events_from_chunks(chunks)
    assert [e.get("seq") for e in evts] == [3]


async def test_sse_default_since_seq_streams_all_in_order(app, session, tmp_path):
    """Without since_seq the full file replays once, in seq order."""
    from nightdesk.domain.runs import start_run

    _p, t = _make_running_ticket(session, tmp_path)
    log = tmp_path / "transcripts" / "run-canonical2.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as f:
        write_event(f, {"type": "meta", "ts": now_iso(), "seq": 0,
                        "run_id": "r1", "ticket_id": t.id})
        write_event(f, {"type": "assistant_text", "ts": now_iso(), "seq": 1,
                        "text": "a"})
        write_event(f, {"type": "tool_use", "ts": now_iso(), "seq": 2,
                        "id": "u1", "tool": "Read", "input": {}})
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        async with ac.stream(
            "GET", f"/api/v1/tickets/{t.id}/transcript"
        ) as r:
            chunks = []
            async for chunk in r.aiter_text():
                chunks.append(chunk)
                if "event: end" in chunk:
                    break

    evts = _events_from_chunks(chunks)
    metas = [e for e in evts if e.get("type") == "meta"]
    assert len(metas) == 1
    assert [e.get("seq") for e in evts] == [0, 1, 2]


# --- per-run transcript (GET /api/v1/runs/{rid}/transcript) ----------------
#
# Distinct from the per-ticket stream above: it targets one specific run by
# id (so a ticket's run history is individually viewable, not just the
# latest/current run), and its "keep tailing or stop" decision is the run's
# own ``finished_at`` rather than the ticket's status.

async def test_run_transcript_replays_finished_run_and_ends(app, session, tmp_path):
    """A finished run replays its transcript once and ends immediately — it
    will never be appended to again, so there is nothing to tail."""
    from nightdesk.domain.runs import finish_run, start_run

    _p, t = _make_running_ticket(session, tmp_path)
    log = tmp_path / "transcripts" / "run-finished.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("hello from an old run\n")
    run = start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
                     transcript_path=str(log), pid=None, host="testhost")
    finish_run(session, run.id, exit_status="success", error_summary=None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        async with ac.stream("GET", f"/api/v1/runs/{run.id}/transcript") as r:
            assert r.status_code == 200
            chunks = []
            async for chunk in r.aiter_text():
                chunks.append(chunk)
                if "event: end" in chunk:
                    break
    text = "".join(chunks)
    assert "hello from an old run" in text
    assert "event: end" in text


async def test_run_transcript_canonical_events_and_since_seq(app, session, tmp_path):
    from nightdesk.domain.runs import finish_run, start_run

    _p, t = _make_running_ticket(session, tmp_path)
    log = tmp_path / "transcripts" / "run-canonical-byid.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as f:
        write_event(f, {"type": "meta", "ts": now_iso(), "seq": 0,
                        "run_id": "r1", "ticket_id": t.id})
        write_event(f, {"type": "assistant_text", "ts": now_iso(), "seq": 1,
                        "text": "first"})
        write_event(f, {"type": "assistant_text", "ts": now_iso(), "seq": 2,
                        "text": "second"})
    run = start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
                     transcript_path=str(log), pid=None, host="testhost")
    finish_run(session, run.id, exit_status="success", error_summary=None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        async with ac.stream(
            "GET", f"/api/v1/runs/{run.id}/transcript?since_seq=1"
        ) as r:
            chunks = []
            async for chunk in r.aiter_text():
                chunks.append(chunk)
                if "event: end" in chunk:
                    break
    evts = _events_from_chunks(chunks)
    assert [e.get("seq") for e in evts] == [2]
    text = "".join(chunks)
    assert "id: 2\n" in text


async def test_run_transcript_still_live_tails_then_ends_when_finished(app, session, tmp_path):
    """A run with no ``finished_at`` yet is still in flight: the endpoint
    must keep tailing rather than treating it like a finished run's one-shot
    replay. Proven here by finishing the run shortly *after* the stream
    opens — if the endpoint had already ended the stream on connect (the
    finished-run behavior), the "still going" line would arrive with no
    "event: end" ever following it once the run finishes.
    """
    from nightdesk.domain.runs import finish_run, start_run

    _p, t = _make_running_ticket(session, tmp_path)
    log = tmp_path / "transcripts" / "run-live.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("still going\n")
    run = start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
                     transcript_path=str(log), pid=None, host="testhost")
    assert run.finished_at is None

    async def _finish_shortly_after_connect():
        await asyncio.sleep(0.1)
        finish_run(session, run.id, exit_status="success", error_summary=None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        finisher = asyncio.ensure_future(_finish_shortly_after_connect())
        try:
            async with ac.stream("GET", f"/api/v1/runs/{run.id}/transcript") as r:
                chunks = []
                async for chunk in r.aiter_text():
                    chunks.append(chunk)
                    if "event: end" in chunk:
                        break
        finally:
            await finisher
    text = "".join(chunks)
    assert "still going" in text
    assert "event: end" in text


async def test_run_transcript_404_for_unknown_run(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        r = await ac.get("/api/v1/runs/does-not-exist/transcript")
    assert r.status_code == 404


async def test_run_transcript_404_when_transcript_file_missing(app, session, tmp_path):
    from nightdesk.domain.runs import start_run

    _p, t = _make_running_ticket(session, tmp_path)
    missing = tmp_path / "transcripts" / "never-written.log"
    run = start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
                     transcript_path=str(missing), pid=None, host="testhost")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        r = await ac.get(f"/api/v1/runs/{run.id}/transcript")
    assert r.status_code == 404
