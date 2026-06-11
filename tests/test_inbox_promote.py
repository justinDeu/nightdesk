"""Tests for the Inbox → board promotion flow.

The previous Inbox surface (test_inbox_ui.py) covers the domain boundary and the
quick promote/decline buttons. This module covers the *promotion modal* flow:

  - the promote-modal route renders the shared edit modal in promote mode,
    pre-filled from the inbox item, with the missing required execution fields
    highlighted;
  - promoting via the full form persists the user's edits (workspace, profile,
    prompt, project, priority, labels) before crossing the completeness
    boundary, so an item can be fleshed out and promoted in one step;
  - the validation boundary still holds: a queued/draft promotion with required
    execution fields missing is rejected (422) and the item stays in the inbox;
  - the field-level ``ticket_missing_fields`` helper stays in lock-step with the
    sentence-level ``ticket_completeness`` boundary;
  - the optional execution-context preview hook is dormant when the endpoint is
    absent (the default in this branch).
"""
import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.domain.labels import create_label
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.tickets import (
    create_ticket,
    get_ticket,
    list_inbox,
    ticket_completeness,
    ticket_missing_fields,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine,
        bearer_token="t",
        static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts",
        worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def cookie_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"nightdesk_token": "t"},
    ) as ac:
        yield ac


@pytest.fixture
def profile(session):
    return create_profile(
        session,
        name="promote-test",
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
        default_model=None,
    )


def _inbox(session, profile, *, title="Vague idea", complete=False, **kw):
    fields = dict(title=title, prompt="", status="inbox", profile_id=profile.id)
    if complete:
        fields["source_path"] = "/tmp"
    fields.update(kw)
    return create_ticket(session, **fields)


def _promote_form(**overrides):
    """A minimal but complete promote-modal submission (workspace filled in)."""
    form = {
        "workspace_form": "1",
        "title": "Fleshed out title",
        "prompt": "Now it has a prompt",
        "source_path": "/tmp/project",
        "primary_kind": "directory",
        "priority": "0",
        "target": "draft",
        "project": "",
    }
    form.update(overrides)
    return form


# ---------------------------------------------------------------------------
# Field-level completeness helper
# ---------------------------------------------------------------------------


def test_missing_fields_for_bare_inbox_item(session, profile):
    t = _inbox(session, profile, title="")  # no title, no workspace
    missing = ticket_missing_fields(t)
    assert missing == {"title", "workspace"}  # profile is set by the fixture


def test_missing_fields_empty_when_complete(session, profile):
    t = _inbox(session, profile, complete=True)
    assert ticket_missing_fields(t) == set()


def test_missing_fields_in_lockstep_with_completeness(session, profile):
    """The field set is empty iff the sentence-level boundary is satisfied."""
    incomplete = _inbox(session, profile)
    assert bool(ticket_missing_fields(incomplete)) == bool(ticket_completeness(incomplete))
    complete = _inbox(session, profile, complete=True)
    assert bool(ticket_missing_fields(complete)) == bool(ticket_completeness(complete))


# ---------------------------------------------------------------------------
# Promote-modal route
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_promote_modal_renders_prefilled(cookie_client, session, profile):
    t = _inbox(session, profile, title="Carry me over", prompt="keep this note")
    r = await cookie_client.get(f"/inbox/tickets/{t.id}/promote-modal")
    assert r.status_code == 200
    # Pre-filled title + prompt are preserved into the modal.
    assert "Carry me over" in r.text
    assert "keep this note" in r.text
    # Promote-mode footer + form action.
    assert f"/inbox/tickets/{t.id}/promote" in r.text
    assert "Accept &amp; queue" in r.text
    assert "Promote to draft" in r.text


@pytest.mark.anyio
async def test_promote_modal_highlights_missing_fields(cookie_client, session, profile):
    t = _inbox(session, profile)  # missing workspace
    r = await cookie_client.get(f"/inbox/tickets/{t.id}/promote-modal")
    assert r.status_code == 200
    assert "Required before promoting" in r.text
    assert "a workspace source path" in r.text


@pytest.mark.anyio
async def test_promote_modal_shows_execution_context_preview(cookie_client, session, profile):
    """With the execution-context branch merged the promote modal includes
    the effective-config preview section so the user can see what the ticket
    will run with before promoting."""
    t = _inbox(session, profile, complete=True)
    r = await cookie_client.get(f"/inbox/tickets/{t.id}/promote-modal")
    assert "data-effective-preview-section" in r.text


@pytest.mark.anyio
async def test_promote_modal_rejects_non_inbox(cookie_client, session, profile):
    t = _inbox(session, profile, complete=True)
    # Promote it off the inbox first.
    await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={"target": "draft"},
        headers={"HX-Request": "true"},
    )
    session.expire_all()
    r = await cookie_client.get(f"/inbox/tickets/{t.id}/promote-modal")
    assert r.status_code == 409


@pytest.mark.anyio
async def test_promote_modal_missing_ticket_404(cookie_client, session, profile):
    r = await cookie_client.get("/inbox/tickets/nope/promote-modal")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Full-form promotion (flesh out + accept in one step)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_full_form_promote_fills_in_and_drafts(cookie_client, session, profile):
    t = _inbox(session, profile)  # incomplete: no workspace
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data=_promote_form(profile_id=profile.id, target="draft"),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    updated = get_ticket(session, t.id)
    assert updated.status == "draft"
    # Edits were persisted before promotion.
    assert updated.title == "Fleshed out title"
    assert updated.prompt == "Now it has a prompt"
    primary = next(w for w in updated.workspaces if w.role == "primary")
    assert primary.source_path == "/tmp/project"


@pytest.mark.anyio
async def test_full_form_promote_to_queued(cookie_client, session, profile):
    t = _inbox(session, profile)
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data=_promote_form(profile_id=profile.id, target="queued"),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    assert get_ticket(session, t.id).status == "queued"


@pytest.mark.anyio
async def test_full_form_queue_blocked_when_workspace_missing(cookie_client, session, profile):
    """Queued promotion must be rejected when required execution fields are
    still missing — even via the full form (empty source path)."""
    t = _inbox(session, profile)
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data=_promote_form(profile_id=profile.id, target="queued", source_path=""),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422
    # The required source path is what blocks it (enforced at the update step).
    assert "source_path" in r.text or "workspace" in r.text
    session.expire_all()
    # Stays in the inbox; not queued.
    assert get_ticket(session, t.id).status == "inbox"


@pytest.mark.anyio
async def test_full_form_draft_blocked_when_workspace_missing(cookie_client, session, profile):
    t = _inbox(session, profile)
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data=_promote_form(profile_id=profile.id, target="draft", source_path=""),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422
    session.expire_all()
    assert get_ticket(session, t.id).status == "inbox"


@pytest.mark.anyio
async def test_full_form_preserves_project_priority_and_labels(cookie_client, session, profile):
    proj = create_project(session, name="Carryover", source_path="/tmp")
    lbl = create_label(session, name="carry-label")  # noqa: keyword-only
    t = _inbox(session, profile)
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={
            **_promote_form(profile_id=profile.id, target="draft"),
            "project_id": proj.id,
            "priority": "3",
            "labels_form": "1",
            "label_ids": lbl.id,
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    updated = get_ticket(session, t.id)
    assert updated.status == "draft"
    assert updated.project_id == proj.id
    assert updated.priority == 3
    assert [lab.id for lab in updated.labels] == [lbl.id]


@pytest.mark.anyio
async def test_full_form_promote_refreshes_scoped_list(cookie_client, session, profile):
    """A promotion from a project-scoped view returns that scoped list."""
    proj = create_project(session, name="Scoped", source_path="/tmp")
    # Two inbox items: one scoped, one loose.
    scoped = _inbox(session, profile, title="scoped-item", project_id=proj.id)
    _inbox(session, profile, title="loose-item")
    r = await cookie_client.post(
        f"/inbox/tickets/{scoped.id}/promote",
        data=_promote_form(profile_id=profile.id, target="draft", project=proj.slug),
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    # Scoped refresh: the loose item is not part of this fragment.
    assert "loose-item" not in r.text


@pytest.mark.anyio
async def test_quick_promote_still_works(cookie_client, session, profile):
    """The lightweight (target-only) promote path is unchanged."""
    t = _inbox(session, profile, complete=True)
    r = await cookie_client.post(
        f"/inbox/tickets/{t.id}/promote",
        data={"target": "queued"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    session.expire_all()
    assert get_ticket(session, t.id).status == "queued"
    assert list_inbox(session) == []


@pytest.mark.anyio
async def test_inbox_row_exposes_promote_action(cookie_client, session, profile):
    _inbox(session, profile, title="needs-promoting")
    r = await cookie_client.get("/inbox")
    assert "ndOpenPromoteTicket" in r.text
    # Rows expose promote as a hover icon with a tooltip, split by readiness:
    # quick-queue for complete items, edit-modal-first when fields are missing.
    assert ("Promote to queued" in r.text
            or "Flesh out the required fields, then promote" in r.text)
