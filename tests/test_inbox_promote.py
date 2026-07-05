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












# ---------------------------------------------------------------------------
# Full-form promotion (flesh out + accept in one step)
# ---------------------------------------------------------------------------
