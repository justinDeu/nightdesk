"""Tests for the shared property-picker primitive's domain layer
(domain.properties): the registry, per-property chip rendering, and the
apply() commit path for priority/status/project. These backed the shared
GET/POST /board/tickets/{id}/picker|property/{prop} HTMX routes, removed
with the HTMX rip-out; the SPA's property pickers call the focused JSON
routes instead (PATCH /api/v1/tickets/{tid}/priority|status|project|profile,
see test_metadata_update_routes.py), which share this same domain layer.
"""
from __future__ import annotations

import pytest

from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.properties import (
    PROPERTY_REGISTRY,
    get_property,
    property_chip,
    status_targets,
)
from nightdesk.domain.tickets import create_ticket, get_ticket


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def _profile(session):
    return create_profile(
        session, name="prop-test", fs_read=[], fs_write=[],
        allowed_tools=[], denied_tools=[], network_mode="off",
        network_allowlist=[], secret_keys=[], default_model=None,
    )


@pytest.fixture
def _project(session):
    return create_project(
        session, name="Nightdesk", color="#34d399", source_path="/tmp/nd",
    )


def _make(session, profile, **kw):
    kw.setdefault("title", "t")
    kw.setdefault("prompt", "")
    kw.setdefault("source_path", "/tmp")
    return create_ticket(session, profile_id=profile.id, **kw)


def _reload(session, tid):
    """Re-read a ticket after a route committed it through a different session
    (the in-memory DB is shared via one connection; we just need to drop the
    test session's identity-map cache first)."""
    session.expire_all()
    return get_ticket(session, tid)


# --------------------------------------------------------------------------- #
# Registry / domain layer
# --------------------------------------------------------------------------- #

class TestRegistry:
    def test_registers_core_properties(self):
        assert set(PROPERTY_REGISTRY) >= {"priority", "status", "project"}

    def test_unknown_property_raises(self):
        with pytest.raises(KeyError):
            get_property("nope")

    def test_only_project_is_searchable(self):
        assert get_property("project").searchable is True
        assert get_property("priority").searchable is False
        assert get_property("status").searchable is False


class TestStatusTargets:
    def test_draft_offers_queued_not_running(self):
        # Inbox promotion: draft -> queued must be offered; running is the
        # worker's job and is never a picker target.
        targets = status_targets("draft")
        assert "queued" in targets
        assert "running" not in targets
        assert targets[0] == "draft"  # current first

    def test_review_offers_queue_and_archive(self):
        assert set(status_targets("review")) == {"review", "queued", "archived"}

    def test_archived_offers_requeue(self):
        assert "queued" in status_targets("archived")


class TestChips:
    def test_priority_chip(self, session, _profile):
        t = _make(session, _profile, priority=4)
        chip = property_chip("priority", t)
        assert chip["label"] == "Urgent"
        assert "text-danger" in chip["css"]

    def test_status_chip(self, session, _profile):
        t = _make(session, _profile, status="review")
        chip = property_chip("status", t)
        assert chip["label"] == "Review"

    def test_project_chip_none(self, session, _profile):
        t = _make(session, _profile)
        chip = property_chip("project", t)
        assert chip["label"] == "No project"
        assert chip["swatch_color"] is None

    def test_project_chip_with_project(self, session, _profile, _project):
        t = _make(session, _profile, project_id=_project.id)
        chip = property_chip("project", t, _project)
        assert chip["label"] == "Nightdesk"
        assert chip["swatch_color"] == "#34d399"


class TestApply:
    def test_priority_apply_named(self, session, _profile):
        t = _make(session, _profile, priority=0)
        get_property("priority").apply(session, t.id, "urgent")
        assert get_ticket(session, t.id).priority == 4

    def test_priority_apply_invalid_raises(self, session, _profile):
        t = _make(session, _profile)
        with pytest.raises(ValueError):
            get_property("priority").apply(session, t.id, "banana")

    def test_status_apply_promote(self, session, _profile):
        t = _make(session, _profile, status="draft")
        get_property("status").apply(session, t.id, "queued")
        assert _reload(session, t.id).status == "queued"

    def test_status_apply_invalid_value(self, session, _profile):
        t = _make(session, _profile, status="draft")
        with pytest.raises(ValueError):
            get_property("status").apply(session, t.id, "bogus")

    def test_status_apply_running_rejected(self, session, _profile):
        t = _make(session, _profile, status="draft")
        with pytest.raises(ValueError):
            get_property("status").apply(session, t.id, "running")

    def test_status_apply_same_is_noop(self, session, _profile):
        t = _make(session, _profile, status="draft")
        out = get_property("status").apply(session, t.id, "draft")
        assert out.status == "draft"

    def test_project_apply_and_clear(self, session, _profile, _project):
        t = _make(session, _profile)
        get_property("project").apply(session, t.id, _project.id)
        assert get_ticket(session, t.id).project_id == _project.id
        get_property("project").apply(session, t.id, "")
        assert _reload(session, t.id).project_id is None
