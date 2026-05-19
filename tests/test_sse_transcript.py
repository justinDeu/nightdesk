import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.tickets import create_ticket


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
