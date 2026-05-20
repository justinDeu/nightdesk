"""Smoke and behavior tests for the ticket detail page (Stream E)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.runs import start_run
from nightdesk.domain.tickets import create_ticket, get_ticket, transition_status


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
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # Headline + each major section header.
    assert "Hello world" in body
    assert "Prompt" in body
    assert "Filesystem access" in body
    assert "Run history" in body
    assert "Latest transcript" in body
    # Profile fs_write surfaces.
    assert "/opt/code" in body
    # Empty states.
    assert "no runs yet" in body
    assert "no run yet" in body


async def test_canonical_transcript_renders_events_server_side(
    cookie_client, session, tmp_path,
):
    """A canonical NDJSON transcript should be parsed and rendered, not raw."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
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
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
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
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
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
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
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


async def test_transcript_panel_route_404_when_run_off_ticket(
    cookie_client, session, tmp_path,
):
    """Cross-ticket run IDs must 404, not leak another ticket's transcript."""
    p = _make_profile(session)
    t1 = create_ticket(session, title="a", prompt="p",
                        priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
    t2 = create_ticket(session, title="b", prompt="p",
                        priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
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
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
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
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
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
                                         {"path": "/srv/y", "mode": "rw"}], cwd="/tmp")
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
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
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
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
    r = await cookie_client.post(
        f"/tickets/{t.id}/run-now-toggle", follow_redirects=False,
    )
    assert r.status_code == 404


async def test_review_ticket_shows_run_again_modal(cookie_client, session):
    """Review/archived tickets surface a 'Run again' modal in the header.

    The modal carries the two-axis picker (workspace × conversation) plus
    secondary clone/merge actions. The JS submit handler maps the picked
    combination to the existing /resume, /retry, or /restart endpoints, so
    the route URLs appear in the inline script even though no static <form>
    targets them.
    """
    p = _make_profile(session)
    t = create_ticket(session, title="rerun", prompt="fix it",
                       priority=0, profile_id=p.id, status="queued",
                       run_now=False, cwd="/tmp")
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
    # builds resume/retry/restart paths by concatenating the ticket id onto
    # path fragments, so the literal full URL never appears in the body —
    # just the fragments. Clone/merge use static formaction attributes.
    assert f'var TID = "{t.id}"' in body
    assert "/resume'" in body
    assert "/retry'" in body
    assert "/restart'" in body
    assert f'formaction="/tickets/{t.id}/clone"' in body
    assert f'formaction="/tickets/{t.id}/merge-next-run-context"' in body


async def test_run_row_omits_host(cookie_client, session, tmp_path):
    """Run-history rows must not echo the worker hostname. It's already shown
    in the top-nav worker pill and this is a local-first app, so repeating it
    on every row is redundant noise. The log link takes the ml-auto slot the
    host span used to occupy so the row stays right-aligned."""
    p = _make_profile(session)
    t = create_ticket(session, title="t", prompt="p",
                       priority=0, profile_id=p.id, run_now=False, cwd="/tmp")
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
    # The log link inherits the right-alignment slot.
    assert 'class="text-fg-muted hover:text-fg ml-auto underline decoration-dotted"' in body


async def test_resume_route_queues_ticket_and_stores_context(cookie_client, session):
    p = _make_profile(session)
    t = create_ticket(session, title="rerun", prompt="fix it",
                       priority=0, profile_id=p.id, status="queued",
                       run_now=False, cwd="/tmp")
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
    # Create a .claude/settings.json in a tmp cwd so the route detects it.
    cwd = tmp_path / "project"
    cwd.mkdir()
    settings_dir = cwd / ".claude"
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
                           cwd=str(cwd))
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
