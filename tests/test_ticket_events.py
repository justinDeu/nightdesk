"""Layer 0/1 of the post-review acknowledgement flow: transition events with
actor attribution, and the auto-ack rules driven off the acting principal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from nightdesk.db.models import Profile, TicketEvent
from nightdesk.domain import ack as ack_domain
from nightdesk.domain.events import ADMIN, run_actor
from nightdesk.domain.tickets import (
    archive, create_ticket, get_ticket, requeue, request_run_now,
    transition_status, unarchive,
)


@pytest.fixture
def profile(session) -> Profile:
    p = Profile(
        name="p", fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(p)
    session.commit()
    return p


def _draft(session, profile, **kw):
    return create_ticket(
        session, title="t", profile_id=profile.id, source_path="/tmp", **kw,
    )


def _events(session, ticket_id):
    return list(session.scalars(
        select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.created_at.asc(), TicketEvent.id.asc())
    ))


def _to_review(session, tid, *, actor):
    """draft -> running -> review, the review hop by the given actor."""
    transition_status(session, tid, "running")
    return transition_status(session, tid, "review", actor=actor)


# --- Layer 0: events on every transition path --------------------------------


def test_transition_records_event(session, profile):
    t = _draft(session, profile)
    transition_status(session, t.id, "queued")
    evs = _events(session, t.id)
    assert len(evs) == 1
    assert (evs[0].from_status, evs[0].to_status) == ("draft", "queued")
    assert evs[0].actor_kind == "admin"


def test_noop_transition_records_no_event(session, profile):
    t = _draft(session, profile)
    transition_status(session, t.id, "draft")  # same column
    assert _events(session, t.id) == []


def test_helpers_and_run_now_all_record_events(session, profile):
    t = _draft(session, profile)
    request_run_now(session, t.id)              # draft -> queued
    _to_review(session, t.id, actor=run_actor("r1"))  # queued->running->review
    archive(session, t.id)                      # review -> archived
    unarchive(session, t.id)                    # archived -> queued
    paths = [(e.from_status, e.to_status) for e in _events(session, t.id)]
    assert paths == [
        ("draft", "queued"),
        ("queued", "running"),
        ("running", "review"),
        ("review", "archived"),
        ("archived", "queued"),
    ]


def test_worker_review_event_carries_run_actor(session, profile):
    t = _draft(session, profile)
    _to_review(session, t.id, actor=run_actor("run-xyz"))
    review_ev = [e for e in _events(session, t.id) if e.to_status == "review"][0]
    assert review_ev.actor_kind == "run"
    assert review_ev.run_id == "run-xyz"


# --- Layer 1: auto-ack rules -------------------------------------------------


def test_run_transition_to_review_never_acks(session, profile):
    t = _draft(session, profile)
    _to_review(session, t.id, actor=run_actor("r1"))
    t = get_ticket(session, t.id)
    assert t.acknowledged_at is None
    assert t.acknowledged_by is None


def test_human_archive_from_review_acks(session, profile):
    t = _draft(session, profile)
    _to_review(session, t.id, actor=run_actor("r1"))
    archive(session, t.id, actor=ADMIN)
    t = get_ticket(session, t.id)
    assert t.acknowledged_at is not None
    assert t.acknowledged_by == "admin"


def test_run_archive_does_not_ack(session, profile):
    t = _draft(session, profile)
    _to_review(session, t.id, actor=run_actor("r1"))
    archive(session, t.id, actor=run_actor("r1"))
    assert get_ticket(session, t.id).acknowledged_at is None


def test_requeue_clears_ack(session, profile):
    t = _draft(session, profile)
    _to_review(session, t.id, actor=run_actor("r1"))
    archive(session, t.id, actor=ADMIN)          # acked
    assert get_ticket(session, t.id).acknowledged_at is not None
    requeue(session, t.id)                        # archived -> queued (active)
    assert get_ticket(session, t.id).acknowledged_at is None


def test_human_archive_from_draft_acks(session, profile):
    # A human archiving never-ran work implies awareness (design open q #3).
    t = _draft(session, profile)
    archive(session, t.id, actor=ADMIN)
    assert get_ticket(session, t.id).acknowledged_at is not None


# --- entering_event / archived-at --------------------------------------------


def test_entering_event_is_the_latest_matching_status(session, profile):
    t = _draft(session, profile)
    _to_review(session, t.id, actor=run_actor("r1"))
    archive(session, t.id, actor=run_actor("r1"))
    ev = ack_domain.entering_event(session, get_ticket(session, t.id))
    assert ev is not None
    assert ev.to_status == "archived"
    assert ev.actor_kind == "run"
