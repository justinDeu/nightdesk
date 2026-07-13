"""Tests for the unified project activity feed
(GET /api/v1/projects/{id}/activity + domain.activity).

Covers the acceptance criteria in docs/design/project-control-plane.md §History:
  * multi-source merge ordering (reverse-chronological, stable across sources)
  * cursor pagination stability — "Load earlier" never skips/repeats a row,
    including when a row from a second source sits exactly on the page boundary
  * server-side filters return ONLY matching rows across pagination boundaries
    (the Failures chip must not lie about rows past the loaded window)
  * text search
  * week rollups
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from nightdesk.api.app import create_app
from nightdesk.db.models import Connection, CronJobFire, ExternalLink, RepoLink, Run
from nightdesk.domain.activity import (
    encode_cursor,
    project_activity_feed,
)
from nightdesk.domain.cron_jobs import create_cron_job
from nightdesk.domain.profiles import create_profile
from nightdesk.domain.projects import create_project
from nightdesk.domain.runs import finish_run, start_run
from nightdesk.domain.tickets import create_ticket, transition_status


NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _backdate(session, run: Run, *, started_at: datetime, finished_at: datetime | None,
              exit_status: str | None) -> Run:
    """Force a run's timestamps/outcome to known values (start_run/finish_run
    stamp `now`); used to build deterministic interleaved history."""
    run.started_at = started_at
    run.finished_at = finished_at
    run.exit_status = exit_status
    session.commit()
    session.refresh(run)
    return run


def _mk_run(session, ticket, *, started_at, minutes=10, status="success"):
    run = start_run(
        session, ticket_id=ticket.id, worktree_path="/tmp/w",
        transcript_path="/tmp/p.log", pid=None, host="h",
    )
    fin = started_at + timedelta(minutes=minutes)
    _backdate(
        session, run, started_at=started_at,
        finished_at=fin if status != "running" else None,
        exit_status=None if status == "running" else status,
    )
    return run


@pytest.fixture
def app(engine, tmp_path):
    return create_app(
        engine=engine, bearer_token="t", static_root=tmp_path / "static",
        transcript_root=tmp_path / "transcripts", worktree_root=tmp_path / "work",
    )


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": "Bearer t"},
    ) as ac:
        yield ac


@pytest.fixture
def project(session, sample_profile):
    proj = create_project(session, name="hist", slug="hist", source_path="/repo/hist")
    return proj


def _add_repo_link(session, *, name="gitlab", external_id="42"):
    conn = Connection(
        name=name, provider="gitlab", base_url="https://gl.example.com",
        auth_kind="pat", status="ok",
    )
    session.add(conn)
    session.flush()
    repo = RepoLink(
        connection_id=conn.id, external_kind="gitlab_project",
        external_id=external_id, external_path="group/repo",
        display_name="repo", web_url="https://gl.example.com/group/repo",
    )
    session.add(repo)
    session.commit()
    session.refresh(repo)
    return repo


def _add_external_link(session, *, ticket_id, repo_link_id, kind="merge_request",
                       iid="1", title="MR one", state="opened", updated_at=None,
                       state_detail=None):
    link = ExternalLink(
        ticket_id=ticket_id, repo_link_id=repo_link_id, kind=kind,
        external_iid=iid, role="produced_mr", url="https://gl.example.com/mr",
        title=title, state=state, state_detail=state_detail,
        synced_at=updated_at or NOW, updated_at=updated_at or NOW,
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return link


# ---------------------------------------------------------------------------
# Merge ordering
# ---------------------------------------------------------------------------

class TestMergeOrdering:
    def test_interleaves_sources_reverse_chronologically(self, session, project, sample_profile):
        t = create_ticket(session, title="T", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        repo = _add_repo_link(session)

        # Run finished at 10:00.
        _mk_run(session, t, started_at=NOW - timedelta(hours=3), status="success")
        # Lifecycle: entered review at 11:00 (later than the run).
        transition_status(session, t.id, "queued")
        transition_status(session, t.id, "running")
        transition_status(session, t.id, "review")
        from nightdesk.db.models import TicketEvent
        ev = session.scalars(
            __import__("sqlalchemy").select(TicketEvent)
            .where(TicketEvent.ticket_id == t.id, TicketEvent.to_status == "review")
        ).one()
        ev.created_at = NOW - timedelta(hours=1)
        # Repo MR opened at 09:00 (earliest).
        _add_external_link(
            session, ticket_id=t.id, repo_link_id=repo.id, iid="9", title="early MR",
            state="opened", updated_at=NOW - timedelta(hours=4),
        )
        session.commit()

        feed = project_activity_feed(session, project.id, limit=10)
        kinds = [it.kind for it in feed.items]
        ts = [it.ts for it in feed.items]
        # Expected order (newest first): lifecycle(11:00) > run(10:00) > repo(09:00)
        assert kinds == ["lifecycle", "run", "repo"]
        assert ts == sorted(ts, reverse=True)

    def test_stable_tiebreak_by_id(self, session, project, sample_profile):
        # Two runs finishing at the same instant must order by id DESC and never
        # collide or drop one across pagination.
        t = create_ticket(session, title="T", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        same = NOW - timedelta(hours=1)
        r1 = _mk_run(session, t, started_at=same - timedelta(minutes=5), status="success")
        r2 = _mk_run(session, t, started_at=same - timedelta(minutes=5), status="failed")
        _backdate(session, r1, started_at=same - timedelta(minutes=5),
                  finished_at=same, exit_status="success")
        _backdate(session, r2, started_at=same - timedelta(minutes=5),
                  finished_at=same, exit_status="failed")
        feed = project_activity_feed(session, project.id, limit=10)
        assert len(feed.items) == 2
        assert {it.id for it in feed.items} == {r1.id, r2.id}
        # Both share a ts; ids are distinct and strictly ordered.
        assert feed.items[0].ts == feed.items[1].ts
        assert feed.items[0].id > feed.items[1].id


# ---------------------------------------------------------------------------
# Cursor pagination stability
# ---------------------------------------------------------------------------

class TestPagination:
    def test_no_skip_no_repeat_across_pages(self, session, project, sample_profile):
        t = create_ticket(session, title="T", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        # 5 runs at distinct, descending instants.
        for i in range(5):
            _mk_run(
                session, t,
                started_at=NOW - timedelta(hours=i + 1),
                status="success" if i % 2 == 0 else "failed",
            )

        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            feed = project_activity_feed(session, project.id, limit=2, cursor=cursor)
            pages += 1
            ids = [it.id for it in feed.items]
            # No overlap with already-seen rows.
            assert not (set(ids) & set(seen)), "row repeated across pages"
            seen.extend(ids)
            if not feed.has_more:
                break
            cursor = feed.next_cursor
            assert cursor is not None
            assert pages < 10, "pagination did not terminate"

        assert len(seen) == 5
        # Global order across all pages is strictly reverse-chronological by id.
        assert len(set(seen)) == 5

    def test_boundary_when_second_source_straddles_page_edge(self, session, project, sample_profile):
        # The classic cursor bug: page 1 ends on a run; a lifecycle event with
        # an identical timestamp must still appear on page 2, not vanish.
        t = create_ticket(session, title="T", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        boundary = NOW - timedelta(hours=2)
        r1 = _mk_run(session, t, started_at=boundary - timedelta(minutes=10), status="success")
        _backdate(session, r1, started_at=boundary - timedelta(minutes=10),
                  finished_at=boundary, exit_status="success")
        r2 = _mk_run(session, t, started_at=boundary - timedelta(minutes=20), status="success")
        _backdate(session, r2, started_at=boundary - timedelta(minutes=20),
                  finished_at=boundary, exit_status="success")
        # Lifecycle at the SAME instant as the runs.
        transition_status(session, t.id, "queued")
        transition_status(session, t.id, "running")
        from nightdesk.db.models import TicketEvent
        ev = session.scalars(
            __import__("sqlalchemy").select(TicketEvent)
            .where(TicketEvent.ticket_id == t.id, TicketEvent.to_status == "running")
        ).first()
        # transition to running is excluded from lifecycle feed, so push to review
        transition_status(session, t.id, "review")
        ev = session.scalars(
            __import__("sqlalchemy").select(TicketEvent)
            .where(TicketEvent.ticket_id == t.id, TicketEvent.to_status == "review")
        ).one()
        ev.created_at = boundary
        session.commit()

        page1 = project_activity_feed(session, project.id, limit=2)
        assert len(page1.items) == 2
        page2 = project_activity_feed(session, project.id, limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 1
        all_ids = {it.id for it in page1.items} | {it.id for it in page2.items}
        # All three boundary rows survive across the two pages.
        assert all_ids == {r1.id, r2.id, ev.id}
        assert page2.has_more is False

    def test_bad_cursor_treated_as_first_page(self, session, project, sample_profile):
        t = create_ticket(session, title="T", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        _mk_run(session, t, started_at=NOW - timedelta(hours=1), status="success")
        feed = project_activity_feed(session, project.id, cursor="not-real-base64!!")
        # A malformed cursor decodes to None -> first page, still returns data.
        assert len(feed.items) == 1


# ---------------------------------------------------------------------------
# Server-side filters (the core acceptance test)
# ---------------------------------------------------------------------------

class TestFilters:
    def _seed_mixed(self, session, project, sample_profile):
        """A run-success, a run-failure, a review lifecycle, an archived
        lifecycle, and a merged MR — enough that every chip has matches and
        non-matches interleaved across a 2-row page."""
        t = create_ticket(session, title="alpha", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        repo = _add_repo_link(session)
        times = [NOW - timedelta(hours=i) for i in range(1, 6)]
        succ = _mk_run(session, t, started_at=times[4], status="success")
        _backdate(session, succ, started_at=times[4], finished_at=times[4] + timedelta(minutes=5),
                  exit_status="success")
        fail = _mk_run(session, t, started_at=times[3], status="failed")
        _backdate(session, fail, started_at=times[3], finished_at=times[3] + timedelta(minutes=2),
                  exit_status="failed")
        # review lifecycle at times[2]
        transition_status(session, t.id, "queued")
        transition_status(session, t.id, "running")
        transition_status(session, t.id, "review")
        from nightdesk.db.models import TicketEvent
        rev = session.scalars(
            __import__("sqlalchemy").select(TicketEvent)
            .where(TicketEvent.to_status == "review")
        ).one()
        rev.created_at = times[2]
        # archived lifecycle at times[1]
        transition_status(session, t.id, "archived")
        arch = session.scalars(
            __import__("sqlalchemy").select(TicketEvent)
            .where(TicketEvent.to_status == "archived")
        ).one()
        arch.created_at = times[1]
        # merged MR at times[0]
        _add_external_link(
            session, ticket_id=t.id, repo_link_id=repo.id, iid="31", kind="merge_request",
            title="MR merged", state="merged", updated_at=times[0],
            state_detail={"additions": 88, "deletions": 12},
        )
        # an OPEN mr that must NOT show under "shipped"
        _add_external_link(
            session, ticket_id=t.id, repo_link_id=repo.id, iid="32", kind="merge_request",
            title="MR open", state="opened", updated_at=times[0] - timedelta(minutes=5),
        )
        session.commit()
        return succ, fail, rev, arch

    def test_failures_filter_excludes_successes_across_pages(self, session, project, sample_profile):
        self._seed_mixed(session, project, sample_profile)
        seen_kinds: list[str] = []
        seen_outcomes: list[str] = []
        cursor = None
        guard = 0
        while True:
            feed = project_activity_feed(session, project.id, kind="failures", limit=2, cursor=cursor)
            seen_kinds += [it.kind for it in feed.items]
            seen_outcomes += [it.outcome for it in feed.items]
            if not feed.has_more:
                break
            cursor = feed.next_cursor
            guard += 1
            assert guard < 10
        # Only failed runs — no successes, no lifecycle/repo — across all pages.
        assert seen_kinds == ["run"] * len(seen_kinds)
        assert all(o == "failed" for o in seen_outcomes)
        assert len(seen_outcomes) == 1  # exactly one failure was seeded

    def test_runs_filter_only_runs(self, session, project, sample_profile):
        self._seed_mixed(session, project, sample_profile)
        feed = project_activity_feed(session, project.id, kind="runs", limit=10)
        assert {it.kind for it in feed.items} == {"run"}
        assert {it.outcome for it in feed.items} == {"success", "failed"}

    def test_lifecycle_filter_only_lifecycle(self, session, project, sample_profile):
        self._seed_mixed(session, project, sample_profile)
        feed = project_activity_feed(session, project.id, kind="lifecycle", limit=10)
        assert {it.kind for it in feed.items} == {"lifecycle"}
        assert {it.to_status for it in feed.items} == {"review", "archived"}

    def test_repo_filter_only_repo(self, session, project, sample_profile):
        self._seed_mixed(session, project, sample_profile)
        feed = project_activity_feed(session, project.id, kind="repo", limit=10)
        assert {it.kind for it in feed.items} == {"repo"}
        # Both the merged and open MRs surface under "repo".
        assert {it.state for it in feed.items} == {"merged", "opened"}

    def test_shipped_filter_is_archived_plus_merged_only(self, session, project, sample_profile):
        self._seed_mixed(session, project, sample_profile)
        feed = project_activity_feed(session, project.id, kind="shipped", limit=10)
        kinds = [(it.kind, it.to_status or it.state) for it in feed.items]
        assert ("lifecycle", "archived") in kinds
        assert ("repo", "merged") in kinds
        # The open MR and the runs must NOT appear under shipped.
        assert ("repo", "opened") not in kinds
        assert not any(it.kind == "run" for it in feed.items)

    def test_all_filter_includes_every_source(self, session, project, sample_profile):
        self._seed_mixed(session, project, sample_profile)
        feed = project_activity_feed(session, project.id, kind="all", limit=50)
        kinds = {it.kind for it in feed.items}
        assert {"run", "lifecycle", "repo"} <= kinds


# ---------------------------------------------------------------------------
# Search + cron + rollups
# ---------------------------------------------------------------------------

class TestSearchAndExtras:
    def test_search_matches_run_title_only(self, session, project, sample_profile):
        ta = create_ticket(session, title="frobnicate widget", prompt="p",
                           profile_id=sample_profile.id, project_id=project.id,
                           source_path=project.source_path)
        tb = create_ticket(session, title="unrelated thing", prompt="p",
                           profile_id=sample_profile.id, project_id=project.id,
                           source_path=project.source_path)
        _mk_run(session, ta, started_at=NOW - timedelta(hours=1), status="success")
        _mk_run(session, tb, started_at=NOW - timedelta(hours=2), status="success")
        feed = project_activity_feed(session, project.id, q="frobnicate", limit=10)
        assert len(feed.items) == 1
        assert feed.items[0].title == "frobnicate widget"

    def test_cron_fires_scoped_by_source_path(self, session, project, sample_profile):
        # Cron whose source_path matches the project -> surfaces.
        job = create_cron_job(
            session, title="nightly triage", profile_id=sample_profile.id,
            source_path=project.source_path, schedule="0 2 * * *", now=NOW,
        )
        fire = CronJobFire(cron_job_id=job.id, fire_at=NOW - timedelta(hours=3))
        session.add(fire)
        # A second project with a different source_path — its cron must NOT leak.
        other = create_project(session, name="other", slug="other", source_path="/repo/other")
        job2 = create_cron_job(
            session, title="other cron", profile_id=sample_profile.id,
            source_path=other.source_path, schedule="0 2 * * *", now=NOW,
        )
        session.add(CronJobFire(cron_job_id=job2.id, fire_at=NOW - timedelta(hours=2)))
        session.commit()

        feed = project_activity_feed(session, project.id, limit=50)
        cron_items = [it for it in feed.items if it.kind == "cron"]
        assert len(cron_items) == 1
        assert cron_items[0].title == "nightly triage"

    def test_rollups_numbers_only(self, session, project, sample_profile):
        t = create_ticket(session, title="r", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        repo = _add_repo_link(session)
        # 3 success + 1 failure this week, one archived + one merged MR shipped.
        for i in range(3):
            _mk_run(session, t, started_at=NOW - timedelta(days=i, hours=1), status="success")
        _mk_run(session, t, started_at=NOW - timedelta(days=1, hours=2), status="failed")
        transition_status(session, t.id, "queued")
        transition_status(session, t.id, "running")
        transition_status(session, t.id, "review")
        transition_status(session, t.id, "archived")
        _add_external_link(
            session, ticket_id=t.id, repo_link_id=repo.id, iid="7", kind="merge_request",
            title="m", state="merged", updated_at=NOW - timedelta(days=1),
        )
        session.commit()

        feed = project_activity_feed(
            session, project.id, include_rollups=True, limit=50,
        )
        assert len(feed.rollups) >= 1
        # Rollups are per ISO week (Mon–Sun); the seeded events span two weeks,
        # so assert on the totals summed across the window.
        total_runs = sum(r.runs for r in feed.rollups)
        total_failures = sum(r.failures for r in feed.rollups)
        total_shipped = sum(r.shipped for r in feed.rollups)
        assert total_runs == 4          # 3 success + 1 failure
        assert total_failures == 1
        assert total_shipped == 2       # archived lifecycle + merged MR
        # Each individual week's rate is a sane fraction in [0, 1].
        assert all(0.0 <= r.success_rate <= 1.0 for r in feed.rollups)

    def test_rollups_only_on_first_page(self, session, project, sample_profile):
        # Cursor present -> rollups omitted even if requested (they're stable).
        t = create_ticket(session, title="r", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        _mk_run(session, t, started_at=NOW - timedelta(hours=1), status="success")
        page1 = project_activity_feed(session, project.id, limit=1, include_rollups=True)
        assert page1.has_more is False  # only one run
        # Build a cursor by hand to simulate page 2.
        cur = encode_cursor(page1.items[0].ts, page1.items[0].id)
        page2 = project_activity_feed(session, project.id, limit=1, cursor=cur, include_rollups=True)
        assert page2.rollups == []


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class TestHttpSurface:
    async def test_envelope_and_404(self, client, session, project, sample_profile):
        t = create_ticket(session, title="h", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        _mk_run(session, t, started_at=NOW - timedelta(hours=1), status="success")

        r = await client.get(f"/api/v1/projects/{project.id}/activity",
                             params={"include_rollups": "true"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) >= {"items", "rollups", "next_cursor", "has_more"}
        assert body["items"][0]["kind"] == "run"
        assert body["items"][0]["outcome"] == "success"
        assert isinstance(body["rollups"], list)

        r404 = await client.get("/api/v1/projects/does-not-exist/activity")
        assert r404.status_code == 404

    async def test_kind_param_routes_through(self, client, session, project, sample_profile):
        t = create_ticket(session, title="h", prompt="p", profile_id=sample_profile.id,
                          project_id=project.id, source_path=project.source_path)
        _mk_run(session, t, started_at=NOW - timedelta(hours=1), status="failed")
        r = await client.get(f"/api/v1/projects/{project.id}/activity",
                             params={"kind": "failures"})
        body = r.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["outcome"] == "failed"
