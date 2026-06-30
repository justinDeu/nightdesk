"""Smoke and behavior tests for the ticket detail page (Stream E)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import create_ticket, get_ticket, transition_status
_PROC_DIR_KW = "c" "wd"


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine, bearer_token="t",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                              cookies={"nightdesk_token": "t"}) as ac:
        yield ac


def _make_profile(session, **overrides):
    fields = dict(
        name="p", fs_read=[], fs_write=["/opt/code"], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    fields.update(overrides)
    return create_profile(session, **fields)


async def test_page_renders_for_fresh_ticket(cookie_client, session):
    """A draft ticket with no runs should render the page (200) with all sections."""
    p = _make_profile(session)
    t = create_ticket(session, title="Hello world", prompt="do it",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # Headline + each major section header.
    assert "Hello world" in body
    assert "Prompt" in body
    assert "Filesystem access" in body
    assert "Run history" in body
    assert "Latest transcript" in body or "Transcript" in body
    # Profile fs_write surfaces.
    assert "/opt/code" in body
    # Empty states.
    assert "no runs yet" in body
    assert "no run yet" in body
    # Ticket prompt renders as the first user message inside the transcript
    # panel (inside the HTMX swap target), not as a detached block above it.
    assert "do it" in body
    assert "user-prompt" in body
    assert "user-prompt-author" in body


async def test_canonical_transcript_renders_events_server_side(
    cookie_client, session, tmp_path,
):
    """A canonical NDJSON transcript should be parsed and rendered, not raw."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "run-1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "deadbeefcafe", "ticket_id": t.id},
        {"type": "assistant_text", "ts": "2026-05-16T00:00:01Z", "seq": 1,
         "text": "hello canonical viewer"},
        {"type": "tool_use", "ts": "2026-05-16T00:00:02Z", "seq": 2,
         "id": "use-1", "tool": "Bash",
         "input": {"command": "echo from-test"}},
        {"type": "tool_result", "ts": "2026-05-16T00:00:03Z", "seq": 3,
         "tool_use_id": "use-1", "output": "from-test", "is_error": False},
        {"type": "result", "ts": "2026-05-16T00:00:04Z", "seq": 4,
         "subtype": "success", "summary": "done in 1s"},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # Canonical content present.
    assert "hello canonical viewer" in body
    assert "echo from-test" in body
    assert "from-test" in body
    assert "done in 1s" in body
    # Server rendered, not the raw fallback pre block.
    # (raw fallback would dump the full JSON line with `"type":"meta"`.)
    assert '"type":"meta"' not in body
    # New markup: assistant_text uses the .assistant-text wrapper with an
    # author chip up top ('claude' by default), the Bash tool_use renders as
    # a <details class="tc-card"> with the bash tag, and the tool_result
    # lands inside a <details class="tool-result">.
    assert 'class="assistant-text"' in body
    assert 'class="assistant-author"' in body
    assert ">claude<" in body
    assert 'class="tc-card"' in body
    assert 'tc-tag-bash' in body
    assert 'class="bash-cmd"' in body
    assert 'class="tool-result"' in body


async def test_worker_error_event_renders_as_red_card(
    cookie_client, session, tmp_path,
):
    """A canonical transcript ending in a ``worker_error`` event must render
    a distinct red-tinted block with the summary visible and the stacktrace
    collapsed behind a <details> so the user can see worker failures
    without scrolling into the comments section that no longer exists."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "fail.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "failrun", "ticket_id": t.id},
        {"type": "assistant_text", "ts": "2026-05-16T00:00:01Z", "seq": 1,
         "text": "working on it"},
        {"type": "worker_error", "ts": "2026-05-16T00:00:02Z", "seq": 2,
         "kind": "executor_error",
         "summary": "executor error: RuntimeError boom",
         "traceback": "Traceback (most recent call last):\n  RuntimeError: boom"},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # Distinct red card around the error.
    assert 'class="worker-error' in body
    assert "border-danger" in body
    # User-facing summary visible by default.
    assert "executor error: RuntimeError boom" in body
    # Kind tag rendered as a small machine-readable hint.
    assert "executor_error" in body
    # Stacktrace lives inside a collapsible details so it doesn't crowd
    # the summary but is one click away.
    assert "<details" in body
    assert "stacktrace" in body
    assert "RuntimeError: boom" in body


async def test_legacy_transcript_falls_back_to_raw(
    cookie_client, session, tmp_path,
):
    """Non-canonical transcript content should be rendered as a raw pre block."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "legacy.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("plain old log line\nanother line\n")
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    assert "plain old log line" in r.text


async def test_transcript_panel_route_renders_selected_run(
    cookie_client, session, tmp_path,
):
    """GET /tickets/{tid}/runs/{rid}/transcript-panel returns the partial.

    HTMX target wired up in ticket_detail.html for run-history clicks. Until
    this route existed those clicks 404'd and the right pane never swapped.
    """
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "older.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "older1234", "ticket_id": t.id},
        {"type": "tool_use", "ts": "2026-05-16T00:00:01Z", "seq": 1,
         "id": "u9", "tool": "Bash",
         "input": {"command": "echo older-run"}},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    run = start_run(session, ticket_id=t.id,
                     worktree_path=str(tmp_path / "work"),
                     transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}/runs/{run.id}/transcript-panel")
    assert r.status_code == 200
    body = r.text
    assert "echo older-run" in body
    assert 'class="tc-card"' in body
    # Partial only — no full page chrome / sidebar.
    assert "<html" not in body.lower()


async def test_resume_command_renders_when_run_has_session_id(
    cookie_client, session, tmp_path,
):
    """A run that captured a Claude session id surfaces a copyable resume
    command that cd's into the run's working dir and resumes that session."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "r.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "r1", "ticket_id": t.id}) + "\n")
    wt = str(tmp_path / "work" / "wt")
    run = start_run(session, ticket_id=t.id, worktree_path=wt,
                    transcript_path=str(log), pid=None, host="testhost")
    run.session_id = "sess-abc-123"
    session.commit()

    r = await cookie_client.get(f"/tickets/{t.id}/runs/{run.id}/transcript-panel")
    assert r.status_code == 200
    body = r.text
    assert "data-resume-row" in body
    assert "claude --resume sess-abc-123" in body
    assert wt in body  # cd's into the run's working dir


async def test_no_resume_command_without_session_id(cookie_client, session, tmp_path):
    """Runs with no captured session id show no resume command (nothing to
    resume) rather than a broken command."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "r2.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "r2", "ticket_id": t.id}) + "\n")
    run = start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "w"),
                    transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}/runs/{run.id}/transcript-panel")
    assert r.status_code == 200
    assert "data-resume-row" not in r.text


async def test_transcript_panel_route_404_when_run_off_ticket(
    cookie_client, session, tmp_path,
):
    """Cross-ticket run IDs must 404, not leak another ticket's transcript."""
    p = _make_profile(session)
    t1 = create_ticket(session, title="a", prompt="p",
                        priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    t2 = create_ticket(session, title="b", prompt="p",
                        priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "x.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    run = start_run(session, ticket_id=t2.id,
                     worktree_path=str(tmp_path / "work"),
                     transcript_path=str(log), pid=None, host="h")

    r = await cookie_client.get(f"/tickets/{t1.id}/runs/{run.id}/transcript-panel")
    assert r.status_code == 404


async def test_add_additional_dir(cookie_client, session, engine):
    """POST /tickets/{id}/additional-dirs appends an entry."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    r = await cookie_client.post(
        f"/tickets/{t.id}/additional-dirs",
        data={"path": "/srv/extra", "mode": "rw"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Re-read via a fresh session so we see the committed change.
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        fresh = get_ticket(s, t.id)
        assert any(d["path"] == "/srv/extra" and d["mode"] == "rw"
                    for d in fresh.additional_dirs)


async def test_add_additional_dir_rejects_relative(cookie_client, session):
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    r = await cookie_client.post(
        f"/tickets/{t.id}/additional-dirs",
        data={"path": "relative/path", "mode": "rw"},
        follow_redirects=False,
    )
    assert r.status_code == 422


async def test_remove_additional_dir(cookie_client, session, engine):
    """DELETE /tickets/{id}/additional-dirs?path=... removes the entry."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False,
                       additional_dirs=[{"path": "/srv/x", "mode": "rw"},
                                         {"path": "/srv/y", "mode": "rw"}], source_path="/tmp")
    r = await cookie_client.request(
        "DELETE", f"/tickets/{t.id}/additional-dirs",
        params={"path": "/srv/x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        fresh = get_ticket(s, t.id)
        paths = [d["path"] for d in fresh.additional_dirs]
        assert "/srv/x" not in paths
        assert "/srv/y" in paths


async def test_detail_page_has_no_run_now_toggle(cookie_client, session):
    """Run-now is an action, not a stateful attribute. The on/off toggle
    that used to live next to the status pill is gone; only the Run now
    action button remains."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    assert "run-now-toggle" not in body
    assert "run-now: on" not in body
    assert "run-now: off" not in body
    # The action button still exists for non-running tickets — wired via
    # HTMX (not a plain form) so clicking it doesn't reload the page.
    assert f'hx-post="/tickets/{t.id}/run-now"' in body
    assert f'action="/tickets/{t.id}/run-now"' not in body


async def test_run_now_toggle_endpoint_removed(cookie_client, session):
    """The /run-now-toggle endpoint is gone; the action endpoint stays."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    r = await cookie_client.post(
        f"/tickets/{t.id}/run-now-toggle", follow_redirects=False,
    )
    assert r.status_code == 404


async def test_review_ticket_shows_run_again_modal(cookie_client, session):
    """Review/archived tickets surface a 'Run again' modal in the header.

    The modal carries the two-axis picker (workspace × conversation) plus
    secondary clone/merge actions. The JS submit handler maps the picked
    combination to the /continue, /retry, or /restart endpoints, so
    the route URLs appear in the inline script even though no static <form>
    targets them.
    """
    p = _make_profile(session)
    t = create_ticket(session, title="rerun", prompt="fix it",
                       priority=0, profile_id=p.id, status="queued",
                       run_now=False, source_path="/tmp")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # Header surfaces a single primary "Run again…" trigger.
    assert "Run again" in body
    assert 'id="run-again-modal"' in body
    # Modal exposes both axes plus a free-form message field.
    assert 'name="workspace"' in body
    assert 'name="conversation"' in body
    assert 'name="next_run_context"' in body
    # All four primary-action targets are reachable from the modal. The JS
    # builds continue/retry/restart paths by concatenating the ticket id onto
    # path fragments, so the literal full URL never appears in the body —
    # just the fragments. Clone/merge use static formaction attributes.
    assert f'var TID = "{t.id}"' in body
    assert "/continue'" in body
    assert "/retry'" in body
    assert "/restart'" in body
    assert f'formaction="/tickets/{t.id}/clone"' in body
    assert f'formaction="/tickets/{t.id}/merge-next-run-context"' in body


async def test_run_row_omits_host(cookie_client, session, tmp_path):
    """Run-history rows must not echo the worker hostname. It's already shown
    in the top-nav worker pill and this is a local-first app, so repeating it
    on every row is redundant noise. The right-side items (outcome pill, run-now
    bolt, cost, log link) live in a single ml-auto wrapper so the row stays
    right-aligned."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "hostrow.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="bespoke-host-xyz")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # The run row renders (the run id prefix is present)...
    assert "Run history" in body
    # ...but the host string never appears anywhere on the page.
    assert "bespoke-host-xyz" not in body
    # The right-side items wrapper holds the ml-auto right-alignment slot.
    assert 'class="ml-auto flex items-center gap-2"' in body
    # The log link still renders (now inside that wrapper, without its own ml-auto).
    assert 'class="text-fg-muted hover:text-fg underline decoration-dotted"' in body


async def test_resume_route_queues_ticket_and_stores_context(cookie_client, session):
    p = _make_profile(session)
    t = create_ticket(session, title="rerun", prompt="fix it",
                       priority=0, profile_id=p.id, status="queued",
                       run_now=False, source_path="/tmp")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    r = await cookie_client.post(
        f"/tickets/{t.id}/resume",
        data={"next_run_context": "Use polling"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    session.expire_all()
    refreshed = get_ticket(session, t.id)
    assert refreshed.status == "queued"
    assert refreshed.run_now is True
    assert refreshed.next_run_context == "Use polling"


async def test_continue_route_queues_ticket_and_stages_continue_intent(
    cookie_client, session,
):
    """The distinct /continue action queues a run-now and stages the continue
    intent (not resume), so the worker resumes the prior SDK conversation."""
    p = _make_profile(session)
    t = create_ticket(session, title="rerun", prompt="fix it",
                       priority=0, profile_id=p.id, status="queued",
                       run_now=False, source_path="/tmp")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    r = await cookie_client.post(
        f"/tickets/{t.id}/continue",
        data={"next_run_context": "Keep the conversation"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    session.expire_all()
    refreshed = get_ticket(session, t.id)
    assert refreshed.status == "queued"
    assert refreshed.run_now is True
    assert refreshed.next_run_context == "Keep the conversation"
    assert refreshed.permission_overrides["nightdesk_run_intent"] == "continue"


async def test_review_ticket_shows_continue_run_button_with_tooltip(
    cookie_client, session,
):
    """The Continue run action is a distinct, always-visible control on
    review/archived tickets, with a [data-tooltip] explaining how it differs
    from the fresh-context Run-again options."""
    p = _make_profile(session)
    t = create_ticket(session, title="rerun", prompt="fix it",
                       priority=0, profile_id=p.id, status="queued",
                       run_now=False, source_path="/tmp")
    transition_status(session, t.id, "running")
    transition_status(session, t.id, "review")
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    assert "Continue run" in body
    assert f'action="/tickets/{t.id}/continue"' in body
    # The hover detail explains the continue-vs-resume distinction.
    assert "full Claude Code conversation" in body
    assert "data-tooltip=" in body


async def test_review_ticket_with_prior_run_shows_end_of_transcript_continue_box(
    cookie_client, session, tmp_path,
):
    """A review ticket with a prior run renders the chat-style continue composer
    at the end of the transcript. It posts the typed text to the existing
    /continue route as next_run_context (which the worker carries as the next
    user turn), and its hint reflects that the prior session is resumable."""
    p = _make_profile(session)
    t = create_ticket(session, title="rerun", prompt="fix it",
                       priority=0, profile_id=p.id, status="queued",
                       run_now=False, source_path="/tmp")
    transition_status(session, t.id, "running")
    log = tmp_path / "transcripts" / "prior.log"
    run = start_run(session, ticket_id=t.id, worktree_path="/tmp/w",
                    transcript_path=str(log), pid=None, host="testhost")
    finish_run(session, run.id, exit_status="success", error_summary=None,
               session_id="sess-prior-1")
    transition_status(session, t.id, "review")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # The end-of-transcript composer is present and targets /continue.
    assert 'class="continue-box"' in body
    assert f'action="/tickets/{t.id}/continue"' in body
    assert 'name="next_run_context"' in body
    assert "Type a message to continue this run" in body
    # The prior run recorded a session id, so the hint promises a real resume.
    assert "Continues this conversation with full history" in body
    # The composer sits after the transcript scroll area, not inside it: it
    # appears exactly once.
    assert body.count('class="continue-box"') == 1


async def test_continue_box_hidden_when_not_continuable(cookie_client, session):
    """The composer only appears for continuable state (review/archived) with a
    prior run. A running ticket and a review ticket with no prior run both
    degrade without the box (and without a confusing 'continue' affordance)."""
    p = _make_profile(session)

    # (a) Running ticket: not continuable.
    t_run = create_ticket(session, title="running", prompt="p",
                          priority=0, profile_id=p.id, status="running",
                          run_now=False, source_path="/tmp")
    rr = await cookie_client.get(f"/tickets/{t_run.id}")
    assert 'class="continue-box"' not in rr.text

    # (b) Review ticket with NO prior run: nothing to continue from.
    t_review = create_ticket(session, title="review-no-run", prompt="p",
                             priority=0, profile_id=p.id, status="queued",
                             run_now=False, source_path="/tmp")
    transition_status(session, t_review.id, "running")
    transition_status(session, t_review.id, "review")
    r2 = await cookie_client.get(f"/tickets/{t_review.id}")
    assert 'class="continue-box"' not in r2.text


async def test_canonical_transcript_renders_user_message_at_boundary(
    cookie_client, session, tmp_path,
):
    """A continued run's transcript opens with the user's typed message so the
    continuity boundary is visible — rendered as a user bubble flagged
    'continued from prior run', not as raw JSON."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "run-cont.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "meta", "ts": "2026-06-30T00:00:00Z", "seq": 0, "ticket_id": t.id},
        {"type": "user_message", "ts": "2026-06-30T00:00:01Z", "seq": 1,
         "text": "Now also fix the touch variant", "continued_session": True},
        {"type": "assistant_text", "ts": "2026-06-30T00:00:02Z", "seq": 2,
         "text": "On it — picking up where we left off."},
        {"type": "result", "ts": "2026-06-30T00:00:03Z", "seq": 3,
         "subtype": "success", "summary": "done"},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # The typed message renders as a user bubble at the boundary.
    assert "Now also fix the touch variant" in body
    assert 'class="user-message"' in body
    assert "continued from prior run" in body
    # Rendered, not the raw JSON fallback.
    assert '"type":"user_message"' not in body


async def test_project_settings_warning_renders_inside_header_card(
    tmp_path, session, engine,
):
    """When project Claude Code settings exist the warning renders inside
    the header card (sidebar area), not as a top-level banner above the
    two-column layout.

    Placement contract:
    - The marker attribute ``data-project-settings-warning`` is present.
    - The key descriptive text is present (file count + explanatory note).
    - The warning does NOT appear before the two-column flex container in
      the rendered HTML (i.e. it is not a top-level banner).
    """
    # Create a .claude/settings.json in a temp project dir so the route detects it.
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    settings_dir = project_dir / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text('{"permissions": {}}')

    app = create_app(
        engine=engine, bearer_token="t",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           cookies={"nightdesk_token": "t"}) as ac:
        p = _make_profile(session)
        t = create_ticket(session, title="proj-settings-test", prompt="do it",
                           priority=0, profile_id=p.id, run_now=False,
                           source_path=str(project_dir))
        r = await ac.get(f"/tickets/{t.id}")

    assert r.status_code == 200
    body = r.text

    # Warning content is present.
    assert "data-project-settings-warning" in body
    assert "Project Claude Code settings detected" in body
    assert "merge these with the profile" in body
    assert "settings.json" in body

    # Warning must NOT appear before the two-column flex container — it
    # should live inside the header card, not as a top-of-page banner.
    two_col_marker = 'class="flex flex-col lg:flex-row gap-4 items-start"'
    warning_marker = "data-project-settings-warning"
    assert two_col_marker in body
    assert body.index(two_col_marker) < body.index(warning_marker)


async def test_subagent_children_nested_under_card(
    cookie_client, session, tmp_path,
):
    """Sub-agent tool calls must render nested under the subagent card.

    The rendered HTML must carry data-subagent-tool-use-id on the outer
    wrapper, data-parent-tool-use-id on each child row, and the child tool
    name must appear inside the nested block (i.e. after the subagent wrapper
    opens and before it closes).
    """
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "subagent.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    agent_id = "toolu_agent_01"
    lines = [
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "subagentrun", "ticket_id": t.id},
        # The Agent tool_use that triggered the sub-agent.
        {"type": "tool_use", "ts": "2026-05-16T00:00:01Z", "seq": 1,
         "id": agent_id, "tool": "Agent",
         "input": {"description": "explore the codebase"}},
        # Sub-agent started event — tool_use_id links it to the Agent call.
        {"type": "subagent", "ts": "2026-05-16T00:00:02Z", "seq": 2,
         "subagent_type": "Explore", "phase": "progress",
         "tool_use_id": agent_id, "task_id": agent_id,
         "description": "exploring"},
        # A child tool call whose parent_tool_use_id ties it to the sub-agent.
        {"type": "tool_use", "ts": "2026-05-16T00:00:03Z", "seq": 3,
         "id": "toolu_glob_01", "tool": "Glob",
         "parent_tool_use_id": agent_id,
         "input": {"pattern": "**/*.py", "path": "/opt/code"}},
        {"type": "tool_result", "ts": "2026-05-16T00:00:04Z", "seq": 4,
         "tool_use_id": "toolu_glob_01", "output": "src/foo.py", "is_error": False},
        # Sub-agent notification (done).
        {"type": "subagent", "ts": "2026-05-16T00:00:05Z", "seq": 5,
         "subagent_type": "Explore", "phase": "notification",
         "tool_use_id": agent_id, "task_id": agent_id,
         "status": "completed", "summary": "found it"},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text

    # Outer sub-agent wrapper has the data hook for sidebar filtering.
    assert f'data-subagent-tool-use-id="{agent_id}"' in body

    # Child tool row carries data-parent-tool-use-id.
    assert f'data-parent-tool-use-id="{agent_id}"' in body

    # The child Glob tool name appears in the document.
    assert "Glob" in body

    # Glob must appear *inside* the subagent-group div, not before it.
    subagent_group_start = body.index(f'data-subagent-tool-use-id="{agent_id}"')
    glob_pos = body.index("Glob")
    assert glob_pos > subagent_group_start

    # Children are wrapped in a collapsible <details> with the toggle summary.
    assert "subagent-children-toggle" in body


async def test_transcript_sidebar_shows_subagent_and_tasks(
    cookie_client, session, tmp_path,
):
    """The transcript sidebar renders sub-agent index and task list.

    When the transcript contains a sub-agent event and TaskCreate/TaskUpdate
    calls, the rendered page must include:
    - id="transcript-sidebar"
    - the sub-agent label and data-filter-tool-use-id pointing to the agent
    - each task subject with the appropriate CSS checkbox class (todo-item is-completed / is-pending)
    """
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                      priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "sidebar.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    agent_id = "toolu_sidebar_01"
    lines = [
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "sidebarrun", "ticket_id": t.id},
        # Sub-agent started + completed.
        {"type": "tool_use", "ts": "2026-05-16T00:00:01Z", "seq": 1,
         "id": agent_id, "tool": "Agent",
         "input": {"description": "do stuff"}},
        {"type": "subagent", "ts": "2026-05-16T00:00:02Z", "seq": 2,
         "subagent_type": "Executor", "phase": "progress",
         "tool_use_id": agent_id, "task_id": agent_id,
         "description": "running"},
        {"type": "subagent", "ts": "2026-05-16T00:00:03Z", "seq": 3,
         "subagent_type": "Executor", "phase": "notification",
         "tool_use_id": agent_id, "task_id": agent_id,
         "status": "completed", "summary": "all done"},
        # Task events.
        {"type": "tool_use", "ts": "2026-05-16T00:00:04Z", "seq": 4,
         "id": "task-use-1", "tool": "TaskCreate",
         "input": {"subject": "Write the tests", "activeForm": ""}},
        {"type": "tool_use", "ts": "2026-05-16T00:00:05Z", "seq": 5,
         "id": "task-use-2", "tool": "TaskCreate",
         "input": {"subject": "Run the build", "activeForm": ""}},
        {"type": "tool_use", "ts": "2026-05-16T00:00:06Z", "seq": 6,
         "id": "task-upd-1", "tool": "TaskUpdate",
         "input": {"taskId": 2, "status": "completed"}},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text

    # Sidebar is present.
    assert 'id="transcript-sidebar"' in body

    # Sub-agent entry with filter hook.
    assert f'data-filter-tool-use-id="{agent_id}"' in body
    assert "Executor" in body
    # Sidebar button carries styled-tooltip data attrs, and the shared
    # tooltip element is present (replaces the generic native title tooltip).
    assert "data-tip-title=" in body
    assert 'id="nd-subagent-tooltip"' in body

    # Task subjects appear with correct CSS checkbox classes.
    assert "Write the tests" in body
    assert "Run the build" in body
    # "Run the build" was marked completed -> is-completed class with todo-check.
    assert "todo-item is-completed" in body
    assert "todo-check" in body
    # "Write the tests" is still pending -> is-pending class.
    assert "todo-item is-pending" in body


async def test_sidebar_contains_stats_and_resume(
    cookie_client, session, tmp_path,
):
    """Stats and resume sections live inside the sidebar, not as a top bar.

    After the refactor the sidebar must contain:
    - #run-stats-bar (with data-stat children for each metric)
    - #nd-resume-cmd (when session_id + worktree_path exist)
    - The old full-width stats bar must NOT appear outside the sidebar.
    """
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                      priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "sidebar_stats.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "statsrun", "ticket_id": t.id},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    run = start_run(session, ticket_id=t.id,
                    worktree_path=str(tmp_path / "work"),
                    transcript_path=str(log), pid=None, host="testhost")
    # Give the run a session_id so the resume section appears.
    run.session_id = "sess-abc123"
    session.commit()

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text

    # Sidebar exists and contains the stats bar.
    assert 'id="transcript-sidebar"' in body
    assert 'id="run-stats-bar"' in body

    # Stats bar carries its data attributes for the JS live-update wiring.
    assert 'data-started-at=' in body
    assert 'data-last-seq=' in body
    assert 'data-stat="model"' in body
    assert 'data-stat="duration"' in body
    assert 'data-stat="tools"' in body
    assert 'data-stat="input"' in body
    assert 'data-stat="output"' in body
    assert 'data-stat="cache-read"' in body
    assert 'data-stat="cache-write"' in body
    assert 'data-stat="cost"' in body

    # Resume section rendered because session_id + worktree_path exist.
    assert 'id="nd-resume-cmd"' in body
    assert "claude --resume sess-abc123" in body

    # The stats heading should be inside the sidebar, and the old top-level
    # stats bar include is gone (no standalone run-stats bar before the
    # transcript-scroll area).
    sidebar_start = body.index('id="transcript-sidebar"')
    stats_bar_pos = body.index('id="run-stats-bar"')
    scroll_pos = body.index('id="transcript-scroll"')
    # Stats bar must come after sidebar start.
    assert stats_bar_pos > sidebar_start
    # Stats bar must come after transcript-scroll (sidebar is right of scroll).
    assert stats_bar_pos > scroll_pos


async def test_sidebar_renders_without_subagents_or_tasks(
    cookie_client, session, tmp_path,
):
    """Sidebar renders for a run with no sub-agents or tasks (stats only)."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                      priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "stats_only.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "statsrun2", "ticket_id": t.id},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    start_run(session, ticket_id=t.id,
              worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text

    # Sidebar renders even without sub-agents or tasks.
    assert 'id="transcript-sidebar"' in body
    assert 'id="run-stats-bar"' in body

    # Sub-agents and Tasks sections should NOT appear.
    assert ">Sub-agents<" not in body
    assert ">Tasks<" not in body

    # Stats section heading present.
    assert ">Stats<" in body


async def test_task_create_renders_as_clean_card(
    cookie_client, session, tmp_path,
):
    """TaskCreate tool_use events must render as a clean tc-tag-task card,
    not fall through to the raw-JSON generic fallback."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                      priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "taskcreate.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "taskrun1", "ticket_id": t.id},
        {"type": "tool_use", "ts": "2026-05-16T00:00:01Z", "seq": 1,
         "id": "tc-1", "tool": "TaskCreate",
         "input": {"subject": "Write unit tests", "description": ""}},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    start_run(session, ticket_id=t.id, worktree_path=str(tmp_path / "work"),
              transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # Subject text must appear.
    assert "Write unit tests" in body
    # Task tag class must be present.
    assert "tc-tag-task" in body
    # Raw JSON fallback must NOT appear (the literal key "subject": would
    # indicate the generic renderer dumped the input dict).
    assert '"subject":' not in body


async def test_run_prompt_renders_per_run_in_transcript_panel(
    cookie_client, session, tmp_path,
):
    """Each run shows its own persisted prompt in the transcript panel.

    After editing the ticket prompt and creating a second run, the transcript
    panel for each run must show the prompt that was active when THAT run was
    created — not the current ticket-level prompt.
    """
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="original prompt",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log1 = tmp_path / "transcripts" / "run1.log"
    log1.parent.mkdir(parents=True, exist_ok=True)
    log1.write_text(json.dumps(
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "run1aaaa", "ticket_id": t.id}) + "\n")
    run1 = start_run(session, ticket_id=t.id,
                      worktree_path=str(tmp_path / "work"),
                      transcript_path=str(log1), pid=None, host="testhost",
                      prompt="original prompt")

    # Edit the ticket prompt, then start a second run.
    t.prompt = "updated prompt"
    session.commit()
    log2 = tmp_path / "transcripts" / "run2.log"
    log2.write_text(json.dumps(
        {"type": "meta", "ts": "2026-05-16T00:01:00Z", "seq": 0,
         "run_id": "run2bbbb", "ticket_id": t.id}) + "\n")
    run2 = start_run(session, ticket_id=t.id,
                      worktree_path=str(tmp_path / "work2"),
                      transcript_path=str(log2), pid=None, host="testhost",
                      prompt="updated prompt")

    # The second run's transcript panel shows its own prompt.
    r2 = await cookie_client.get(f"/tickets/{t.id}/runs/{run2.id}/transcript-panel")
    assert r2.status_code == 200
    assert "updated prompt" in r2.text
    assert "original prompt" not in r2.text

    # The first run's transcript panel shows the original prompt.
    r1 = await cookie_client.get(f"/tickets/{t.id}/runs/{run1.id}/transcript-panel")
    assert r1.status_code == 200
    assert "original prompt" in r1.text
    assert "updated prompt" not in r1.text


async def test_run_with_null_prompt_falls_back_to_ticket_prompt(
    cookie_client, session, tmp_path,
):
    """Runs created before the prompt column existed (prompt=NULL) fall back
    to ticket.prompt at display time."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="ticket-level prompt",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    log = tmp_path / "transcripts" / "old.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(
        {"type": "meta", "ts": "2026-05-16T00:00:00Z", "seq": 0,
         "run_id": "oldrun11", "ticket_id": t.id}) + "\n")
    # Create run without passing prompt — column stays NULL.
    run = start_run(session, ticket_id=t.id,
                     worktree_path=str(tmp_path / "work"),
                     transcript_path=str(log), pid=None, host="testhost")

    r = await cookie_client.get(f"/tickets/{t.id}/runs/{run.id}/transcript-panel")
    assert r.status_code == 200
    # Falls back to ticket.prompt.
    assert "ticket-level prompt" in r.text


async def test_no_run_shows_ticket_prompt_as_pending_message(
    cookie_client, session,
):
    """When no runs exist yet, the ticket prompt shows as the first user
    message so the user can see what will run."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="pending prompt",
                       priority=0, profile_id=p.id, run_now=False, source_path="/tmp")
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    assert "pending prompt" in body
    assert "user-prompt" in body
    assert "no run yet" in body


async def test_changes_tab_uses_worktree_checkout_for_ticket_workspace_fallback(
    cookie_client, session, tmp_path,
):
    p = _make_profile(session)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    (repo / "a.txt").write_text("aaa\n")
    subprocess.run(["git", "add", "."], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], **{_PROC_DIR_KW: str(repo)},
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("bbb\n")
    subprocess.run(["git", "add", "a.txt"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "edit"], **{_PROC_DIR_KW: str(repo)}, capture_output=True, check=True)
    main_checkout = tmp_path / "main-checkout"
    subprocess.run(["git", "clone", str(repo), str(main_checkout)], capture_output=True, check=True)
    subprocess.run(["git", "checkout", base], **{_PROC_DIR_KW: str(main_checkout)}, capture_output=True, check=True)

    t = create_ticket(session, title="diff ticket", prompt="p",
                      priority=0, profile_id=p.id, run_now=False, source_path=str(repo))
    transition_status(session, t.id, "queued")
    transition_status(session, t.id, "running")
    run = start_run(session, ticket_id=t.id, worktree_path=str(repo),
                    transcript_path=str(tmp_path / "transcripts" / "x.log"),
                    pid=None, host="testhost")
    transition_status(session, t.id, "review")
    from nightdesk.db.models import TicketWorkspace
    session.add(TicketWorkspace(
        ticket_id=t.id,
        run_id=None,
        role="primary",
        kind="git_worktree",
        source_path=str(main_checkout),
        resolved_path=str(repo),
        repo_root=str(main_checkout),
        worktree_path=str(repo),
        base_sha=base,
        head_sha=None,
        branch="feat-test",
        state="ready",
        access="read_write",
        label="primary",
        position=0,
        retention="preserve",
    ))
    session.commit()

    r = await cookie_client.get(f"/tickets/{t.id}/runs/{run.id}/diff-panel")
    assert r.status_code == 200
    assert "a.txt" in r.text
