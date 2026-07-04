import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import create_ticket


@pytest.fixture
def app(engine, tmp_path):
    return create_app(engine=engine, bearer_token="t",
                       static_root=tmp_path / "static",
                       transcript_root=tmp_path / "transcripts",
                       worktree_root=tmp_path / "work")


@pytest.fixture
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                              cookies={"nightdesk_token": "t"}) as ac:
        yield ac


async def test_board_page_renders(cookie_client, session):
    p = create_profile(session, name="ui", fs_read=[], fs_write=[], allowed_tools=[],
                        denied_tools=[], network_mode="off", network_allowlist=[],
                        secret_keys=[], default_model=None)
    create_ticket(session, title="my ticket", prompt="hi",
                    priority=0, profile_id=p.id, source_path="/tmp", run_now=False)
    r = await cookie_client.get("/")
    assert r.status_code == 200
    assert "my ticket" in r.text


async def test_analytics_page_renders_with_totals(cookie_client, session):
    """The /analytics page renders headline windows and breakdowns whose
    totals match the seeded runs."""
    from datetime import datetime, timezone
    from nightdesk.db.models import Run, Ticket

    p = create_profile(session, name="anlz", fs_read=[], fs_write=[], allowed_tools=[],
                       denied_tools=[], network_mode="off", network_allowlist=[],
                       secret_keys=[], default_model=None)
    t = create_ticket(session, title="spendy ticket", prompt="hi",
                      priority=0, profile_id=p.id, source_path="/tmp", run_now=False)
    now = datetime.now(timezone.utc)
    run = Run(ticket_id=t.id, started_at=now, finished_at=now,
              exit_status="success", worktree_path="/w",
              transcript_path="/x", host="h", cost_usd=3.25,
              input_tokens=1000, output_tokens=500, cache_read_tokens=500)
    run.model_used = "claude-opus-4-7"
    session.add(run)
    session.commit()

    r = await cookie_client.get("/analytics")
    assert r.status_code == 200
    body = r.text
    assert "Token &amp; usage analytics" in body
    # Token-focused sections present.
    assert "cache hit" in body.lower()
    assert "By model" in body
    # 2.0k total tokens (1000 + 500 + 500) surfaces via the token formatter.
    assert "2.0k" in body
    # Per-model row shows the model.
    assert "claude-opus-4-7" in body
    # Profile and ticket breakdowns surface.
    assert "anlz" in body
    assert "spendy ticket" in body


async def test_analytics_page_in_nav(cookie_client, session):
    r = await cookie_client.get("/")
    assert r.status_code == 200
    assert 'href="/analytics"' in r.text


async def test_board_sidebar_exposes_workspace_controls(cookie_client, session):
    p = create_profile(session, name="workspace-ui", fs_read=[], fs_write=[],
                       allowed_tools=[], denied_tools=[], network_mode="off",
                       network_allowlist=[], secret_keys=[], default_model=None)
    create_ticket(session, title="workspace ticket", prompt="hi",
                  priority=0, profile_id=p.id, source_path="/tmp", run_now=False)

    r = await cookie_client.get("/")

    assert r.status_code == 200
    # Workspace UI now lives in the unified workspaces section of the
    # create modal. Primary row uses a kind <select> (Directory /
    # Git worktree) instead of a "Run in git worktree" checkbox.
    assert 'name="primary_kind"' in r.text
    assert ">Git worktree<" in r.text
    assert "Worktree name" in r.text
    assert "disabled data-worktree-field" in r.text
    assert "data-worktree-path-preview" in r.text
    # The form's interactive behavior lives in /static/ticket_form.js
    # (loaded globally from base.html); the page wires it up via inline
    # event handlers + data-* markers.
    assert '/static/ticket_form.js' in r.text
    assert "data-ticket-form" in r.text
    # Primary kind select drives worktree fields via the shared helper.
    assert "ndSyncPrimaryWorkspaceKind" in r.text
    assert "ndAddLinkedWorkspaceRow" in r.text
    # Modal-scoped path suggest dropdown id ({modal_id}-wt-path-suggest).
    assert "-wt-path-suggest" in r.text
    # The suggestion dropdown host overlays the modal (position: fixed,
    # JS-positioned) instead of expanding inside the overflow-y-auto body
    # and resizing the modal. It also starts hidden until results render.
    assert '-source-path-suggest" class="nd-suggest-host fixed z-50 hidden"' in r.text
async def test_create_modal_project_picker_exposes_workspace_defaults(cookie_client, session):
    p = create_profile(session, name="project-workspace-ui", fs_read=[], fs_write=[],
                       allowed_tools=[], denied_tools=[], network_mode="off",
                       network_allowlist=[], secret_keys=[], default_model=None)
    create_project(
        session,
        name="Nightdesk",
        source_path="/tmp/nightdesk",
        default_workspace_mode="git_worktree",
        default_base_ref="main",
        default_linked_workspaces=[{
            "role": "linked",
            "label": "docs",
            "kind": "directory",
            "access": "read_only",
            "source_path": "/tmp/docs",
        }],
    )
    create_ticket(session, title="workspace ticket", prompt="hi",
                  priority=0, profile_id=p.id, source_path="/tmp", run_now=False)

    r = await cookie_client.get("/")

    assert r.status_code == 200
    assert 'name="project_id"' in r.text
    assert "ndProjectChanged" in r.text
    assert "data-project-workspace-defaults" in r.text
    assert '"source_path": "/tmp/docs"' in r.text

async def test_workspace_preview_uses_debounced_updates(cookie_client, session):
    p = create_profile(session, name="workspace-debounce-ui", fs_read=[], fs_write=[],
                       allowed_tools=[], denied_tools=[], network_mode="off",
                       network_allowlist=[], secret_keys=[], default_model=None)
    create_ticket(session, title="workspace ticket", prompt="hi",
                  priority=0, profile_id=p.id, source_path="/tmp", run_now=False)

    r = await cookie_client.get("/")

    assert r.status_code == 200
    # Debounce wiring lives in /static/ticket_form.js; the page hooks it
    # via inline oninput handlers that call ndScheduleWorktreePreview.
    assert '/static/ticket_form.js' in r.text
    assert "ndScheduleWorktreePreview" in r.text
    assert "data-worktree-preview-source" in r.text
    assert "data-worktree-preview-path" in r.text


async def test_linked_workspace_ui_has_git_specific_controls(cookie_client, session):
    p = create_profile(session, name="linked-workspace-ui", fs_read=[], fs_write=[],
                       allowed_tools=[], denied_tools=[], network_mode="off",
                       network_allowlist=[], secret_keys=[], default_model=None)
    create_ticket(session, title="workspace ticket", prompt="hi",
                  priority=0, profile_id=p.id, source_path="/tmp", run_now=False)

    r = await cookie_client.get("/")

    assert r.status_code == 200
    # The git-specific row markup is added dynamically by ticket_form.js
    # when a row's kind is switched to git_worktree. The page just needs
    # to load that script and expose the host container + Add button so
    # the row factory can render into it.
    assert '/static/ticket_form.js' in r.text
    assert "ndAddLinkedWorkspaceRow" in r.text
    # Modal-scoped linked-workspaces host id ({modal_id}-linked).
    assert "-linked" in r.text
    assert "data-linked-workspace-row" in r.text or "ndAddLinkedWorkspaceRow" in r.text


async def test_profiles_page_lists_existing_profiles(cookie_client, session):
    """List view shows each profile by name with New / Edit entry points.

    The legacy inline backend-binding accordion was replaced in v1 by a
    dedicated per-profile editor at /profiles/{id}; this test only checks
    the table-of-profiles surface.
    """
    create_profile(
        session,
        name="zai",
        fs_read=[],
        fs_write=[],
        allowed_tools=[],
        denied_tools=[],
        network_mode="off",
        network_allowlist=[],
        secret_keys=["ZAI_API_KEY"],
        default_model="glm-5.1",
        backend="claude_sdk",
    )

    r = await cookie_client.get("/profiles")

    assert r.status_code == 200
    assert "zai" in r.text
    assert 'href="/profiles/new"' in r.text


async def test_profile_editor_renders_existing_values(cookie_client, session):
    """The two-pane profile UI lands in view mode at /profiles/{id} and the
    editable form lives at /profiles/{id}/edit. The form POSTs back to
    /profiles/{id} so saving updates the same row."""
    p = create_profile(
        session,
        name="zai-edit",
        fs_read=[],
        fs_write=[],
        allowed_tools=[],
        denied_tools=[],
        network_mode="off",
        network_allowlist=[],
        secret_keys=[],
        default_model="glm-5.1",
        backend="claude_sdk",
    )

    r = await cookie_client.get(f"/profiles/{p.id}/edit")

    assert r.status_code == 200
    body = r.text
    assert "zai-edit" in body
    assert "glm-5.1" in body
    assert f'action="/profiles/{p.id}"' in body




async def test_login_rejects_bad_token(cookie_client):
    cookie_client.cookies.clear()
    r = await cookie_client.post("/login", data={"token": "wrong"})
    assert r.status_code == 401


async def test_dashboard_route_removed(cookie_client):
    r = await cookie_client.get("/dashboard")
    assert r.status_code == 404


async def test_run_now_htmx_returns_204_and_queues_draft(cookie_client, session):
    """Hard requirement on the run-now ticket: HTMX clients (the only path
    used in the browser) get 204 No Content + HX-Trigger event. No 303, no
    redirect — those navigate the browser, which we forbid.

    Also verifies the underlying status transition: a draft ticket must
    actually become queued (otherwise the scheduler's WHERE status='queued'
    clause never matches and the click is a silent no-op)."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket, get_ticket

    p = create_profile(
        session, name="rn-ui", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="rn", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False,
    )
    assert t.status == "draft"

    r = await cookie_client.post(
        f"/tickets/{t.id}/run-now",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 204
    assert r.headers.get("HX-Trigger") == "nd-run-now-queued"
    # Body is empty (204 No Content) so HTMX leaves the DOM alone.
    assert r.content == b""

    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "queued"
    assert after.run_now is True


async def test_run_now_non_htmx_falls_back_to_303(cookie_client, session):
    """curl / no-JS clients keep working: a plain POST gets the 303 back to
    the ticket detail page. Browser users hit the HTMX path above."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket, get_ticket

    p = create_profile(
        session, name="rn-ui-curl", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="rn", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False,
    )
    r = await cookie_client.post(
        f"/tickets/{t.id}/run-now",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/tickets/{t.id}"

    session.expire_all()
    after = get_ticket(session, t.id)
    # Status transition still happened — non-HTMX path is not a no-op.
    assert after.status == "queued"
    assert after.run_now is True


async def test_run_now_on_running_returns_409(cookie_client, session):
    """A live run is not something we want to restart by accident; clicking
    Run-now on it must error rather than silently set a flag."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket

    p = create_profile(
        session, name="rn-ui-running", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="rn", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False, status="running",
    )
    r = await cookie_client.post(
        f"/tickets/{t.id}/run-now",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 409


async def test_archive_htmx_returns_204_and_transitions(cookie_client, session):
    """The cookie-auth Archive route mirrors run-now: HTMX clients get 204
    so the template can gate `window.location='/archive'` on success without
    a redirect fighting it. Also verifies the underlying review->archived
    transition actually happens — the previous detail-page button hit the
    bearer-only /api/v1/... twin and silently 401'd from browser sessions,
    leaving the ticket in 'review' forever while the user was bounced to /.
    """
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket, get_ticket

    p = create_profile(
        session, name="arch-ui", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="arch", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False, status="review",
    )

    r = await cookie_client.post(
        f"/tickets/{t.id}/archive",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 204
    assert r.headers.get("HX-Trigger") == "nd-ticket-archived"
    assert r.content == b""

    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "archived"


async def test_archive_non_htmx_falls_back_to_303(cookie_client, session):
    """curl / no-JS clients keep working: plain POST gets a 303 back to the
    ticket detail page so the endpoint is still usable from a shell."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket, get_ticket

    p = create_profile(
        session, name="arch-ui-curl", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="arch", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False, status="review",
    )

    r = await cookie_client.post(
        f"/tickets/{t.id}/archive",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/tickets/{t.id}"

    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "archived"


async def test_archive_on_draft_succeeds(cookie_client, session):
    """Archive is valid from draft/queued/review per _VALID_TRANSITIONS; a
    draft ticket archives cleanly instead of erroring."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket, get_ticket

    p = create_profile(
        session, name="arch-ui-draft", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="arch", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False,  # default status='draft'
    )

    r = await cookie_client.post(
        f"/tickets/{t.id}/archive",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 204

    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "archived"


async def test_archive_missing_ticket_404(cookie_client):
    r = await cookie_client.post(
        "/tickets/no-such-id/archive",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 404


async def test_sidebar_review_pane_shows_archive_button(cookie_client, session):
    """Acceptance: the sidebar in edit mode for a review-status ticket
    surfaces an Archive button that POSTs to the inline /board/... route
    with hx-target=#sidebar so the rail updates in place (no full board
    navigation, same rule as Run-now)."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket

    p = create_profile(
        session, name="arch-sb", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="rev", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False, status="review",
    )

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert "data-archive-btn" in body
    assert f'hx-post="/board/tickets/{t.id}/archive"' in body
    assert 'hx-target="#sidebar"' in body
    assert ">Archive<" in body


async def test_sidebar_draft_pane_shows_archive_button(cookie_client, session):
    """Same gate as the detail page: Archive renders for
    draft/queued/review. A draft ticket now sees the button — archiving
    directly from draft is a legal transition (_ARCHIVABLE_SOURCES)."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket

    p = create_profile(
        session, name="arch-sb-draft", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="dft", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False,  # default status='draft'
    )

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert "data-archive-btn" in body
    assert f'hx-post="/board/tickets/{t.id}/archive"' in body


async def test_sidebar_running_pane_omits_archive_button(cookie_client, session):
    """A live run is excluded from _ARCHIVABLE_SOURCES — archiving a running
    ticket would orphan an in-flight process, so the button must not render."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket

    p = create_profile(
        session, name="arch-sb-running", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="run", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False, status="running",
    )

    r = await cookie_client.get(f"/board/sidebar?ticket_id={t.id}")
    assert r.status_code == 200
    body = r.text
    assert "data-archive-btn" not in body
    assert f"/board/tickets/{t.id}/archive" not in body


async def test_board_archive_route_returns_sidebar_partial(cookie_client, session):
    """POST /board/tickets/{tid}/archive performs the transition AND returns
    the re-rendered sidebar so HTMX can swap it in place. After the call,
    the returned sidebar is in edit mode for the now-archived ticket and no
    longer carries the Archive button (status gate flipped)."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket, get_ticket

    p = create_profile(
        session, name="arch-board", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="rev2", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False, status="review",
    )

    r = await cookie_client.post(
        f"/board/tickets/{t.id}/archive",
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.text
    # Sidebar partial (not the full page).
    assert "<html" not in body.lower()
    assert 'id="sidebar"' in body
    # Sidebar in edit mode for the now-archived ticket: ticket id is set
    # on the aside, the title renders, and the edit modal is included.
    assert f'data-selected-ticket-id="{t.id}"' in body
    assert "rev2" in body
    assert 'id="ticket-edit-modal"' in body
    # Archive button gone (status is now 'archived', not 'review').
    assert "data-archive-btn" not in body

    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "archived"


async def test_board_archive_on_draft_succeeds(cookie_client, session):
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket, get_ticket

    p = create_profile(
        session, name="arch-board-draft", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="d", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False,
    )

    r = await cookie_client.post(
        f"/board/tickets/{t.id}/archive",
        follow_redirects=False,
    )
    assert r.status_code == 200

    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "archived"


async def test_detail_page_archive_button_uses_cookie_auth_route(
    cookie_client, session,
):
    """Regression guard for the original bug: the detail-page Archive button
    must POST to the cookie-auth /tickets/.../archive route, NOT the
    bearer-only /api/v1/... twin. The latter 401s for browser sessions and
    the old unconditional after-request handler still navigated, making the
    failure invisible."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket

    p = create_profile(
        session, name="arch-detail", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="rev3", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False, status="review",
    )
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # Cookie-auth route is wired up.
    assert f'hx-post="/tickets/{t.id}/archive"' in body
    # Bearer-only twin must not appear on the page.
    assert f"/api/v1/tickets/{t.id}/archive" not in body
    # Navigation is success-gated, not unconditional.
    assert "if (event.detail.successful) window.location='/archive'" in body
    assert "hx-on::after-request=\"window.location='/'\"" not in body
    # The hx-on handler must use htmx's documented double-colon namespaced-event
    # shorthand: hx-on::after-request listens for "htmx:after-request".
    # hx-on::htmx:after-request expands to a listener for the never-emitted
    # "htmx:htmx:after-request", so the redirect would be dead. Guard the
    # attribute name itself, not just the inner JS substring.
    assert "hx-on::after-request=\"if (event.detail.successful)" in body
    assert "hx-on::htmx:after-request" not in body


async def test_detail_page_delete_button_uses_cookie_auth_route(
    cookie_client, session,
):
    """Same fix as Archive: the Delete button must hit the cookie-auth
    /board/tickets/{id} route (returns 204 + HX-Redirect on success, plain
    error otherwise). The old /api/v1/... wiring + unconditional
    after-request handler silently failed for browser sessions."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket

    p = create_profile(
        session, name="del-detail", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="d", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False,
    )
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    assert f'hx-delete="/board/tickets/{t.id}"' in body
    assert f'hx-delete="/api/v1/tickets/{t.id}"' not in body
    # No unconditional navigation handler remains on the Delete button.
    assert "hx-on::after-request=\"window.location='/'\"" not in body


async def test_run_now_on_queued_only_sets_flag(cookie_client, session):
    """Queued tickets stay queued; Run-now just flips the bypass flag so the
    next scheduler tick picks them ahead of the window/capacity check."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket, get_ticket

    p = create_profile(
        session, name="rn-ui-queued", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="rn", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False, status="queued",
    )
    r = await cookie_client.post(
        f"/tickets/{t.id}/run-now",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 204

    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "queued"
    assert after.run_now is True


def _running_ticket(session, name):
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket, transition_status

    p = create_profile(
        session, name=name, fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="cnl", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False, status="queued",
    )
    transition_status(session, t.id, "running")
    return t


async def test_board_cancel_htmx_returns_204_and_transitions(cookie_client, session):
    """POST /board/tickets/{tid}/cancel performs running->review and returns
    204 + HX-Redirect to the ticket page so HTMX reloads it showing the new
    review status. The redirect fires on the success path only, so a rejected
    transition won't yank the user off the page. Mirrors the cookie-auth
    Archive route — the old detail-page button hit the bearer-only
    /api/v1/.../cancel twin and 401'd / dumped raw JSON for browser sessions."""
    from nightdesk.domain.tickets import get_ticket

    t = _running_ticket(session, "cnl-htmx")
    r = await cookie_client.post(
        f"/board/tickets/{t.id}/cancel",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 204
    assert r.headers.get("HX-Redirect") == f"/tickets/{t.id}"
    assert r.content == b""

    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "review"


async def test_board_cancel_non_htmx_falls_back_to_303(cookie_client, session):
    """curl / no-JS clients keep working: a plain POST gets a 303 back to the
    ticket detail page, matching the Archive precedent."""
    from nightdesk.domain.tickets import get_ticket

    t = _running_ticket(session, "cnl-curl")
    r = await cookie_client.post(
        f"/board/tickets/{t.id}/cancel",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/tickets/{t.id}"

    session.expire_all()
    after = get_ticket(session, t.id)
    assert after.status == "review"


async def test_board_cancel_on_non_running_returns_409(cookie_client, session):
    """Cancel is running-only per _VALID_TRANSITIONS (running -> review).
    Firing it on a draft must 409 rather than silently no-op, so the template
    leaves the user on the page (no HX-Redirect on the error path)."""
    from nightdesk.domain.profiles import create_profile
    from nightdesk.domain.tickets import create_ticket

    p = create_profile(
        session, name="cnl-draft", fs_read=[], fs_write=[], allowed_tools=[],
        denied_tools=[], network_mode="off", network_allowlist=[],
        secret_keys=[], default_model=None,
    )
    t = create_ticket(
        session, title="d", prompt="", priority=0, profile_id=p.id,
        source_path="/tmp", run_now=False,  # default status='draft'
    )
    r = await cookie_client.post(
        f"/board/tickets/{t.id}/cancel",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 409


async def test_board_cancel_missing_ticket_404(cookie_client):
    r = await cookie_client.post(
        "/board/tickets/no-such-id/cancel",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 404


async def test_detail_page_cancel_button_uses_cookie_auth_route(cookie_client, session):
    """Regression guard: the detail-page Cancel button for a running ticket
    must POST to the cookie-auth /board/tickets/{id}/cancel route via HTMX,
    NOT a plain <form> hitting the bearer-only /api/v1/.../cancel twin (which
    401s for browser sessions and otherwise navigates the page to raw JSON)."""
    t = _running_ticket(session, "cnl-detail")
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    # Cookie-auth HTMX route is wired up.
    assert f'hx-post="/board/tickets/{t.id}/cancel"' in body
    # Bearer-only twin must not appear anywhere on the page.
    assert f"/api/v1/tickets/{t.id}/cancel" not in body
    # Not a plain form post to the old UI cancel route either.
    assert f'action="/tickets/{t.id}/cancel"' not in body


async def test_detail_page_cancel_button_confirms_before_cancel(cookie_client, session):
    """A misclick must not kill a running agent: the Cancel button carries an
    hx-confirm prompt (intercepted by confirm.js for htmx requests) so the user
    confirms before the running->review transition fires."""
    t = _running_ticket(session, "cnl-confirm")
    r = await cookie_client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    body = r.text
    assert 'hx-confirm="Cancel this running ticket? The agent will stop."' in body
