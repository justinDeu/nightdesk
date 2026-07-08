"""Run-token write-back surface: POST transcript/diff/result.

These are the endpoints an off-host agent (a k8s pod) uses to stream its
transcript and upload its diff/result back over HTTP, authenticated only by the
run's own scoped ``ndr_`` token. See docs/design/session-suite/k8s-executor.md.
"""
import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.db.models import Run
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.run_tokens import issue_run_token
from nightdesk.domain.tickets import create_ticket


_pname = iter(range(10_000))


def _mk_run_with_token(session, tmp_path, *, transcript_name="conv.log"):
    profile = create_profile(
        session, name=f"p{next(_pname)}", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(session, title="t", prompt="hi", priority=0,
                      profile_id=profile.id, run_now=False,
                      status="running", source_path="/tmp")
    transcript = tmp_path / transcript_name
    run = Run(
        ticket_id=t.id,
        started_at=datetime.now(timezone.utc),
        worktree_path="",
        transcript_path=str(transcript),
        host="testhost",
    )
    session.add(run)
    session.commit()
    token = issue_run_token(
        session, run_id=run.id, ticket_id=t.id,
        max_run_duration_seconds=3600, grace_seconds=300,
    )
    return run.id, t.id, token.cleartext, transcript


async def _token_client(app, token):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test",
                       headers={"Authorization": f"Bearer {token}"})


async def test_transcript_append_reassigns_seq(app, session, tmp_path):
    rid, _tid, token, transcript = _mk_run_with_token(session, tmp_path)
    body = "\n".join(json.dumps(e) for e in [
        {"type": "assistant_text", "text": "hello", "seq": 999},
        {"type": "tool_use", "tool": "Read", "input": {}, "seq": 1000},
    ])
    async with await _token_client(app, token) as ac:
        r = await ac.post(f"/api/v1/runs/{rid}/transcript", content=body)
    assert r.status_code == 200
    assert r.json() == {"appended": 2}
    lines = [json.loads(x) for x in transcript.read_text().splitlines() if x.strip()]
    assert [l["type"] for l in lines] == ["assistant_text", "tool_use"]
    # Host reassigns seq to a monotonic space starting at 0, ignoring 999/1000.
    assert [l["seq"] for l in lines] == [0, 1]


async def test_transcript_append_rejects_unknown_type(app, session, tmp_path):
    rid, _tid, token, _tr = _mk_run_with_token(session, tmp_path)
    async with await _token_client(app, token) as ac:
        r = await ac.post(f"/api/v1/runs/{rid}/transcript",
                          content=json.dumps({"type": "bogus"}))
    assert r.status_code == 400


async def test_run_token_rejected_cross_ticket(app, session, tmp_path):
    rid_a, _ta, _token_a, _tr = _mk_run_with_token(session, tmp_path, transcript_name="a.log")
    rid_b, _tb, token_b, _tr2 = _mk_run_with_token(session, tmp_path, transcript_name="b.log")
    # Token B tries to write to run A (a different ticket) -> 403.
    async with await _token_client(app, token_b) as ac:
        r = await ac.post(f"/api/v1/runs/{rid_a}/transcript",
                          content=json.dumps({"type": "assistant_text", "text": "x"}))
    assert r.status_code == 403


async def test_diff_sidecar_upload_and_get_prefers_it(app, session, tmp_path):
    rid, _tid, token, _tr = _mk_run_with_token(session, tmp_path)
    diff_payload = {
        "files": [{
            "path": "a.py", "old_path": "", "new_path": "a.py", "binary": False,
            "lines_added": 1, "lines_deleted": 0,
            "hunks": [{"kind": "ins", "gutter": "+", "text": "print('hi')",
                       "line_no_old": "", "line_no_new": "1"}],
        }],
        "total_added": 1, "total_deleted": 0, "total_files": 1,
        "branch": "feat/x", "base_sha": "aaa", "head_sha": "bbb",
        "repo_root": "/pod/workspace",
    }
    async with await _token_client(app, token) as ac:
        r = await ac.post(f"/api/v1/runs/{rid}/diff", content=json.dumps(diff_payload))
        assert r.status_code == 200
    # GET /diff is a UI read route (cookie/bearer), not a run-token route, so
    # read the served sidecar back with the admin bearer.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Authorization": "Bearer t"}) as ac:
        g = await ac.get(f"/api/v1/runs/{rid}/diff")
    assert g.status_code == 200
    got = g.json()
    assert got["total_added"] == 1
    assert got["branch"] == "feat/x"
    assert got["files"][0]["path"] == "a.py"


async def test_result_sidecar_upload(app, session, tmp_path):
    rid, _tid, token, transcript = _mk_run_with_token(session, tmp_path)
    payload = {
        "exit_status": "success",
        "session_id": "sess-1",
        "usage": {"model": "claude-x", "input_tokens": 10, "output_tokens": 5,
                  "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.01},
    }
    async with await _token_client(app, token) as ac:
        r = await ac.post(f"/api/v1/runs/{rid}/result", content=json.dumps(payload))
    assert r.status_code == 200
    from nightdesk.domain.run_result import read_result_sidecar, result_sidecar_path
    stored = read_result_sidecar(result_sidecar_path(transcript.parent, rid))
    assert stored["exit_status"] == "success"
    assert stored["usage"]["input_tokens"] == 10


async def test_result_rejects_bad_status(app, session, tmp_path):
    rid, _tid, token, _tr = _mk_run_with_token(session, tmp_path)
    async with await _token_client(app, token) as ac:
        r = await ac.post(f"/api/v1/runs/{rid}/result",
                          content=json.dumps({"exit_status": "weird"}))
    assert r.status_code == 400


async def test_admin_bearer_also_allowed(app, session, tmp_path):
    rid, _tid, _token, _tr = _mk_run_with_token(session, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Authorization": "Bearer t"}) as ac:
        r = await ac.post(f"/api/v1/runs/{rid}/transcript",
                          content=json.dumps({"type": "assistant_text", "text": "x"}))
    assert r.status_code == 200
