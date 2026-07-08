from nightdesk.domain.labels import create_label, set_ticket_labels
from nightdesk.domain.tickets import create_ticket
from nightdesk.api.routes.tickets import MAX_LIST_LIMIT


async def _seed_tickets(session, *, n, status="draft", title_prefix="t", pid=None):
    """Bulk-create ``n`` tickets straight through the domain layer (fast — no
    HTTP per row) so paging tests can exceed the 200-row default window."""
    ids = []
    for i in range(n):
        t = create_ticket(
            session,
            title=f"{title_prefix}-{i}",
            prompt="p",
            profile_id=pid,
            source_path="/tmp",
            status=status,
        )
        ids.append(t.id)
    return ids


async def _create_profile(client):
    r = await client.post("/api/v1/profiles", json={
        "name": "tickets-test",
        "fs_read": [], "fs_write": [], "allowed_tools": [], "denied_tools": [],
        "network_mode": "off", "network_allowlist": [], "secret_keys": [],
        "default_model": None,
        "claude_credentials": {"source": "inherit"},
    })
    return r.json()["id"]


async def test_full_ticket_lifecycle(client):
    pid = await _create_profile(client)

    r = await client.post("/api/v1/tickets", json={
        "title": "do thing", "prompt": "p",
        "priority": 1, "profile_id": pid, "source_path": "/tmp", "run_now": False,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    tid = body["id"]
    # v2: tickets default to draft.
    assert body["status"] == "draft"

    r = await client.get("/api/v1/tickets")
    assert r.status_code == 200
    assert any(t["id"] == tid for t in r.json())

    # Priority uses the named 0-4 scale; out-of-range values are rejected.
    r = await client.patch(f"/api/v1/tickets/{tid}", json={"priority": 9})
    assert r.status_code == 422

    r = await client.patch(f"/api/v1/tickets/{tid}", json={"priority": 4})
    assert r.json()["priority"] == 4

    # Run-now on a draft ticket must do BOTH: set the flag AND transition
    # to queued. The scheduler's WHERE status='queued' clause would
    # otherwise filter a flag-only ticket out forever.
    r = await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert r.status_code == 200
    body = r.json()
    assert body["run_now"] is True
    assert body["status"] == "queued"

    r = await client.delete(f"/api/v1/tickets/{tid}")
    assert r.status_code == 204


async def test_run_now_transitions_draft_to_queued(client):
    """Regression: the JSON Run-now endpoint used to only flip the flag,
    leaving draft tickets parked forever (scheduler filters by
    status='queued')."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "draft-then-run-now", "prompt": "p",
        "priority": 1, "profile_id": pid, "source_path": "/tmp",
    })
    assert r.status_code == 201
    tid = r.json()["id"]
    assert r.json()["status"] == "draft"

    r = await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["run_now"] is True


async def test_run_now_on_queued_is_idempotent(client):
    """Queued + run_now is the scheduler's pick-now trigger; the flag flip
    should be safe to repeat without changing status."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "already-queued", "prompt": "p",
        "profile_id": pid, "source_path": "/tmp", "status": "queued",
    })
    assert r.status_code == 201
    tid = r.json()["id"]

    for _ in range(2):
        r = await client.post(f"/api/v1/tickets/{tid}/run-now")
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["run_now"] is True


async def test_run_now_on_running_returns_409(client):
    """Don't let an accidental click restart a live run."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "live", "prompt": "p",
        "profile_id": pid, "source_path": "/tmp", "status": "running",
    })
    assert r.status_code == 201
    tid = r.json()["id"]

    r = await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert r.status_code == 409


async def test_run_now_transitions_review_to_queued(client):
    """Review tickets (post-run audit state) can be re-run, and Run-now is
    the one-click way to do it."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "rev", "profile_id": pid, "source_path": "/tmp", "status": "queued",
    })
    tid = r.json()["id"]
    # queued -> running -> review via the transition endpoint.
    await client.post(f"/api/v1/tickets/{tid}/transition",
                       json={"status": "running"})
    await client.post(f"/api/v1/tickets/{tid}/transition",
                       json={"status": "review"})

    r = await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["run_now"] is True


async def test_cancel_run_now_clears_flag_keeps_status(client):
    """cancel-run-now is the inverse of run-now: it clears the flag WITHOUT
    changing status (a queued+run-now ticket stays queued, it just stops asking
    the scheduler to bypass the queue)."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "armed", "prompt": "p",
        "profile_id": pid, "source_path": "/tmp", "status": "queued",
    })
    tid = r.json()["id"]
    # Arm it first.
    r = await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert r.json()["run_now"] is True
    assert r.json()["status"] == "queued"

    # Cancel — the JSON smoke from the ticket's verification section:
    #   curl -X POST .../cancel-run-now | jq '{id, status, run_now}'
    r = await client.post(f"/api/v1/tickets/{tid}/cancel-run-now")
    assert r.status_code == 200
    body = r.json()
    assert body["run_now"] is False
    assert body["status"] == "queued"  # unchanged
    assert body["id"] == tid


async def test_cancel_run_now_round_trips_run_now(client):
    """Run-now then cancel-run-now returns the ticket to its pre-arm state
    (run_now=false, status still queued) — the toggle must be reversible."""
    pid = await _create_profile(client)
    r = await client.post("/api/v1/tickets", json={
        "title": "toggle", "prompt": "p",
        "profile_id": pid, "source_path": "/tmp", "status": "queued",
    })
    tid = r.json()["id"]
    await client.post(f"/api/v1/tickets/{tid}/run-now")
    assert (await _get(client, tid))["run_now"] is True
    await client.post(f"/api/v1/tickets/{tid}/cancel-run-now")
    state = await _get(client, tid)
    assert state["run_now"] is False
    assert state["status"] == "queued"


async def test_cancel_run_now_unknown_ticket_404(client):
    r = await client.post("/api/v1/tickets/does-not-exist/cancel-run-now")
    assert r.status_code == 404


async def _get(client, tid):
    r = await client.get(f"/api/v1/tickets/{tid}")
    assert r.status_code == 200
    return r.json()


async def test_ticket_api_includes_labels(client, session):
    """Labels attached to a ticket must appear in both list and single-fetch."""
    pid = await _create_profile(client)
    ticket = create_ticket(
        session, title="labeled-ticket", prompt="do work",
        profile_id=pid, source_path="/tmp",
    )
    label = create_label(session, name="urgent", color="#ef4444")
    set_ticket_labels(session, ticket.id, [label.id])

    r = await client.get(f"/api/v1/tickets/{ticket.id}")
    assert r.status_code == 200
    body = r.json()
    assert "labels" in body
    assert len(body["labels"]) == 1
    assert body["labels"][0] == {"id": label.id, "name": "urgent", "color": "#ef4444"}

    r = await client.get("/api/v1/tickets")
    assert r.status_code == 200
    match = next(t for t in r.json() if t["id"] == ticket.id)
    assert match["labels"] == [{"id": label.id, "name": "urgent", "color": "#ef4444"}]


# --- limit / offset paging + truncation metadata ----------------------------
#
# Regression for: GET /api/v1/tickets?limit=500 silently returned 200 rows
# because the route never declared a ``limit`` param (FastAPI dropped it), and
# there was no signal of truncation. Now ``limit`` is honored up to a hard max,
# over-max is a 422 (never a silent clamp), ``offset`` pages, and
# X-Total-Count / X-Has-More make a truncated slice detectable.


async def test_limit_param_is_honored_with_truncation_headers(client, session):
    """A limit smaller than the total returns exactly that many rows AND flags
    that more exist — the missing signal in the original bug."""
    pid = await _create_profile(client)
    ids = await _seed_tickets(session, n=3, pid=pid)

    r = await client.get("/api/v1/tickets?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2  # limit honored, not the 200 default
    assert r.headers["X-Total-Count"] == "3"
    assert r.headers["X-Has-More"] == "true"
    assert r.headers["X-Limit"] == "2"
    assert r.headers["X-Offset"] == "0"
    returned = {t["id"] for t in body}
    assert returned.issubset(set(ids))


async def test_offset_pages_through_every_row(client, session):
    """Walking offset by the limit covers the full set with no dupes/gaps, and
    X-Has-More flips to false exactly on the last page."""
    pid = await _create_profile(client)
    ids = set(await _seed_tickets(session, n=5, pid=pid))

    seen: list[str] = []
    has_more_seq: list[str] = []
    for offset in (0, 2, 4):
        r = await client.get(f"/api/v1/tickets?limit=2&offset={offset}")
        assert r.status_code == 200
        seen += [t["id"] for t in r.json()]
        has_more_seq.append(r.headers["X-Has-More"])
        assert r.headers["X-Total-Count"] == "5"
        assert r.headers["X-Offset"] == str(offset)

    assert set(seen) == ids  # every row exactly once
    assert len(seen) == len(ids) == 5
    # pages 0 and 2 have more (2/5 and 4/5), the last page (4..5) does not.
    assert has_more_seq == ["true", "true", "false"]


async def test_default_limit_returns_everything_below_the_window(client, session):
    """When the total is under the 200 default, the whole set comes back and
    X-Has-More is false — no behavior change for ordinary callers."""
    pid = await _create_profile(client)
    await _seed_tickets(session, n=4, pid=pid)

    r = await client.get("/api/v1/tickets")
    assert r.status_code == 200
    assert len(r.json()) == 4
    assert r.headers["X-Total-Count"] == "4"
    assert r.headers["X-Has-More"] == "false"


async def test_large_limit_is_no_longer_clamped_to_200(client, session):
    """The exact repro: with >200 tickets, limit=500 used to return 200 (the
    param was ignored). It must now return the full set and report no more."""
    pid = await _create_profile(client)
    await _seed_tickets(session, n=205, pid=pid)

    r = await client.get("/api/v1/tickets?limit=500")
    assert r.status_code == 200
    assert len(r.json()) == 205  # was 200 before the fix
    assert r.headers["X-Total-Count"] == "205"
    assert r.headers["X-Has-More"] == "false"


async def test_limit_above_hard_max_is_422_not_silently_clamped(client, session):
    """Asking for more than the hard max must ERROR, never silently clamp — a
    clamp is the failure mode this ticket exists to kill."""
    pid = await _create_profile(client)
    await _seed_tickets(session, n=3, pid=pid)

    r = await client.get(f"/api/v1/tickets?limit={MAX_LIST_LIMIT + 1}")
    assert r.status_code == 422  # validation error, not a 200 with truncated rows
    # The body must point at the limit param so the caller knows what to fix.
    assert any("limit" in str(loc) for loc in r.json()["detail"][0]["loc"])


async def test_limit_at_hard_max_is_honored(client, session):
    """The boundary itself is allowed (inclusive)."""
    pid = await _create_profile(client)
    await _seed_tickets(session, n=3, pid=pid)

    r = await client.get(f"/api/v1/tickets?limit={MAX_LIST_LIMIT}")
    assert r.status_code == 200
    assert len(r.json()) == 3


async def test_invalid_limit_and_offset_rejected(client):
    """limit<1 and offset<0 are 422s — paging is bounded on both ends."""
    assert (await client.get("/api/v1/tickets?limit=0")).status_code == 422
    assert (await client.get("/api/v1/tickets?offset=-1")).status_code == 422


async def test_total_count_respects_status_filter(client, session):
    """X-Total-Count matches the filtered set, not the whole table, so a caller
    paging through archived tickets gets honest per-filter counts."""
    pid = await _create_profile(client)
    await _seed_tickets(session, n=2, pid=pid, status="archived", title_prefix="arch")
    await _seed_tickets(session, n=3, pid=pid, status="draft", title_prefix="drf")

    r = await client.get("/api/v1/tickets?status=archived&limit=500")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(t["status"] == "archived" for t in rows)
    assert r.headers["X-Total-Count"] == "2"  # filtered count, not 5
    assert r.headers["X-Has-More"] == "false"


# --- sort=recent (newest-first, for the Archive page) ------------------------


def _stamp_updated_at(session, ids, times):
    """Force distinct ``updated_at`` values so recency ordering is deterministic
    (creation is too fast to guarantee tie-free timestamps)."""
    from nightdesk.domain.tickets import get_ticket
    for tid, ts in zip(ids, times):
        get_ticket(session, tid).updated_at = ts
    session.commit()


async def test_sort_recent_orders_newest_first(client, session):
    """``sort=recent`` returns most-recently-updated first — the order the
    Archive page pages through so page 1 is the freshest, not the oldest."""
    from datetime import datetime, timezone
    pid = await _create_profile(client)
    ids = await _seed_tickets(session, n=3, pid=pid, status="archived")
    # ids[0] oldest, ids[2] newest
    _stamp_updated_at(session, ids, [
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 1, tzinfo=timezone.utc),
    ])

    r = await client.get("/api/v1/tickets?status=archived&sort=recent")
    assert r.status_code == 200
    got = [t["id"] for t in r.json()]
    assert got == [ids[2], ids[1], ids[0]]


async def test_sort_recent_pages_stay_newest_first(client, session):
    """Recency order is stable across pages: page 1 is strictly newer than
    page 2, so the Archive page's limit/offset paging never repeats or skips."""
    from datetime import datetime, timezone
    pid = await _create_profile(client)
    ids = await _seed_tickets(session, n=4, pid=pid, status="archived")
    _stamp_updated_at(session, ids, [
        datetime(2026, 1, d, tzinfo=timezone.utc) for d in (1, 2, 3, 4)
    ])  # ids[3] newest

    page1 = await client.get("/api/v1/tickets?status=archived&sort=recent&limit=2&offset=0")
    page2 = await client.get("/api/v1/tickets?status=archived&sort=recent&limit=2&offset=2")
    assert [t["id"] for t in page1.json()] == [ids[3], ids[2]]
    assert [t["id"] for t in page2.json()] == [ids[1], ids[0]]
    assert page1.headers["X-Has-More"] == "true"
    assert page2.headers["X-Has-More"] == "false"


async def test_sort_default_is_board_order_unchanged(client, session):
    """Omitting ``sort`` (and ``sort=board``) keeps the original position-stable
    order, so the board and existing agents see no change."""
    pid = await _create_profile(client)
    ids = await _seed_tickets(session, n=3, pid=pid)  # positions 0,1,2 in order

    default = await client.get("/api/v1/tickets")
    board = await client.get("/api/v1/tickets?sort=board")
    assert [t["id"] for t in default.json()] == ids
    assert [t["id"] for t in board.json()] == ids


async def test_sort_invalid_value_rejected(client):
    """An unknown ``sort`` is a 422, not a silent fallback to some ordering."""
    r = await client.get("/api/v1/tickets?sort=sideways")
    assert r.status_code == 422


# --- Archive filter dimensions: priority / label / q / outcome ---------------
#
# These compose with the existing status filter and with limit/offset paging;
# X-Total-Count reflects the *filtered* set so the Archive page never
# undercounts by filtering only the rows it has already loaded.


def _attach_run(session, ticket_id, *, exit_status, cost_usd=None, started_at=None):
    """Insert a finished Run row directly so a ticket has a controllable latest
    run (outcome + cost). ``started_at`` decides which run is 'latest' when a
    ticket has several."""
    from datetime import datetime, timezone
    from nightdesk.db.models import Run
    run = Run(
        ticket_id=ticket_id,
        started_at=started_at or datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        exit_status=exit_status,
        cost_usd=cost_usd,
        worktree_path="/tmp/wt",
        transcript_path="/tmp/t.jsonl",
        pid=None,
        host="test",
    )
    session.add(run)
    session.commit()
    return run


async def test_priority_filter_and_count(client, session):
    """``priority=N`` returns only that band and X-Total-Count is the filtered
    total, not the whole table."""
    pid = await _create_profile(client)
    from nightdesk.domain.tickets import create_ticket
    hi = create_ticket(session, title="hi", prompt="p", profile_id=pid,
                        source_path="/tmp", priority=4, status="archived")
    create_ticket(session, title="lo", prompt="p", profile_id=pid,
                  source_path="/tmp", priority=1, status="archived")

    r = await client.get("/api/v1/tickets?status=archived&priority=4")
    assert r.status_code == 200
    rows = r.json()
    assert [t["id"] for t in rows] == [hi.id]
    assert r.headers["X-Total-Count"] == "1"


async def test_priority_out_of_range_rejected(client):
    """Priority is the 0-4 scale; 9 is a 422, never a silent empty result."""
    assert (await client.get("/api/v1/tickets?priority=9")).status_code == 422


async def test_label_filter_by_name_and_id(client, session):
    """``label=`` matches by case-insensitive name OR id, via EXISTS (no dupes)."""
    pid = await _create_profile(client)
    from nightdesk.domain.tickets import create_ticket
    tagged = create_ticket(session, title="tagged", prompt="p", profile_id=pid,
                           source_path="/tmp", status="archived")
    create_ticket(session, title="bare", prompt="p", profile_id=pid,
                  source_path="/tmp", status="archived")
    label = create_label(session, name="Urgent", color="#ef4444")
    set_ticket_labels(session, tagged.id, [label.id])

    by_name = await client.get("/api/v1/tickets?status=archived&label=urgent")
    assert [t["id"] for t in by_name.json()] == [tagged.id]
    assert by_name.headers["X-Total-Count"] == "1"

    by_id = await client.get(f"/api/v1/tickets?status=archived&label={label.id}")
    assert [t["id"] for t in by_id.json()] == [tagged.id]


async def test_q_free_text_matches_title_and_prompt(client, session):
    """``q`` is a substring match over title and prompt, case-insensitive."""
    pid = await _create_profile(client)
    from nightdesk.domain.tickets import create_ticket
    a = create_ticket(session, title="Migrate database", prompt="do it",
                      profile_id=pid, source_path="/tmp", status="archived")
    b = create_ticket(session, title="unrelated", prompt="touch the DATABASE layer",
                      profile_id=pid, source_path="/tmp", status="archived")
    create_ticket(session, title="nope", prompt="nope", profile_id=pid,
                  source_path="/tmp", status="archived")

    r = await client.get("/api/v1/tickets?status=archived&q=database")
    ids = {t["id"] for t in r.json()}
    assert ids == {a.id, b.id}
    assert r.headers["X-Total-Count"] == "2"


async def test_outcome_filter_uses_latest_run(client, session):
    """``outcome`` keys off the *latest* run: succeeded == last exit 'success',
    failed == any other finished status. A later failed run flips a ticket whose
    first run succeeded."""
    pid = await _create_profile(client)
    from datetime import datetime, timezone
    from nightdesk.domain.tickets import create_ticket
    won = create_ticket(session, title="won", prompt="p", profile_id=pid,
                        source_path="/tmp", status="archived")
    lost = create_ticket(session, title="lost", prompt="p", profile_id=pid,
                        source_path="/tmp", status="archived")
    flipped = create_ticket(session, title="flipped", prompt="p", profile_id=pid,
                            source_path="/tmp", status="archived")
    _attach_run(session, won.id, exit_status="success")
    _attach_run(session, lost.id, exit_status="failed")
    # flipped: an early success then a later failure — latest wins.
    _attach_run(session, flipped.id, exit_status="success",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _attach_run(session, flipped.id, exit_status="failed",
                started_at=datetime(2026, 2, 1, tzinfo=timezone.utc))

    ok = await client.get("/api/v1/tickets?status=archived&outcome=succeeded")
    assert {t["id"] for t in ok.json()} == {won.id}
    assert ok.headers["X-Total-Count"] == "1"

    bad = await client.get("/api/v1/tickets?status=archived&outcome=failed")
    assert {t["id"] for t in bad.json()} == {lost.id, flipped.id}
    assert bad.headers["X-Total-Count"] == "2"


async def test_outcome_invalid_value_rejected(client):
    """Outcome is a closed set; a typo is a 422, not a silent all-rows result."""
    assert (await client.get("/api/v1/tickets?outcome=maybe")).status_code == 422


async def test_sort_cost_orders_by_latest_run_cost(client, session):
    """``sort=cost`` orders by the latest run's cost; a runless ticket (NULL
    cost) sorts last under the default desc order."""
    pid = await _create_profile(client)
    from nightdesk.domain.tickets import create_ticket
    cheap = create_ticket(session, title="cheap", prompt="p", profile_id=pid,
                          source_path="/tmp", status="archived")
    dear = create_ticket(session, title="dear", prompt="p", profile_id=pid,
                        source_path="/tmp", status="archived")
    runless = create_ticket(session, title="runless", prompt="p", profile_id=pid,
                            source_path="/tmp", status="archived")
    _attach_run(session, cheap.id, exit_status="success", cost_usd=0.10)
    _attach_run(session, dear.id, exit_status="success", cost_usd=5.00)

    desc = await client.get("/api/v1/tickets?status=archived&sort=cost")
    got = [t["id"] for t in desc.json()]
    assert got[:2] == [dear.id, cheap.id]
    assert got[-1] == runless.id  # NULL cost sinks to the bottom

    asc = await client.get("/api/v1/tickets?status=archived&sort=cost&order=asc")
    got_asc = [t["id"] for t in asc.json()]
    # cheap before dear once the NULL-cost ticket is set aside.
    non_null = [i for i in got_asc if i in (cheap.id, dear.id)]
    assert non_null == [cheap.id, dear.id]


async def test_sort_created_respects_order_direction(client, session):
    """``sort=created`` with ``order`` flips oldest/newest first."""
    from datetime import datetime, timezone
    pid = await _create_profile(client)
    ids = await _seed_tickets(session, n=3, pid=pid, status="archived")
    from nightdesk.domain.tickets import get_ticket
    for tid, d in zip(ids, (1, 2, 3)):
        get_ticket(session, tid).created_at = datetime(2026, 1, d, tzinfo=timezone.utc)
    session.commit()

    newest = await client.get("/api/v1/tickets?status=archived&sort=created&order=desc")
    oldest = await client.get("/api/v1/tickets?status=archived&sort=created&order=asc")
    assert [t["id"] for t in newest.json()] == [ids[2], ids[1], ids[0]]
    assert [t["id"] for t in oldest.json()] == [ids[0], ids[1], ids[2]]


async def test_filter_composes_with_offset_paging(client, session):
    """A filter + limit/offset pages the *filtered* set: total is the filtered
    count and walking offset yields every match once."""
    pid = await _create_profile(client)
    match_ids = set(await _seed_tickets(
        session, n=5, pid=pid, status="archived", title_prefix="keep-me"))
    await _seed_tickets(session, n=4, pid=pid, status="archived", title_prefix="other")

    seen: list[str] = []
    for offset in (0, 2, 4):
        r = await client.get(
            f"/api/v1/tickets?status=archived&q=keep-me&limit=2&offset={offset}")
        assert r.headers["X-Total-Count"] == "5"  # filtered, not 9
        seen += [t["id"] for t in r.json()]
    assert set(seen) == match_ids
    assert len(seen) == 5
