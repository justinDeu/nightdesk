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
                       priority=0, profile_id=p.id, cwd="/tmp", run_now=False)
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
                      priority=0, profile_id=p.id, cwd="/tmp", run_now=False)
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
