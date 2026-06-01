"""Tests for the unified search query language (parser + compiler)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from nightdesk.db.models import Profile, Project, Run, Ticket
from nightdesk.domain.query import (
    And,
    Cmp,
    MatchAll,
    Not,
    Or,
    Text,
    parse_query,
    search_runs,
    search_tickets,
)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def test_empty_is_match_all():
    assert parse_query("") == MatchAll()
    assert parse_query("   ") == MatchAll()


def test_single_comparison():
    assert parse_query("project=nightdesk") == Cmp("project", "=", "nightdesk")


def test_implicit_and():
    assert parse_query("project=nightdesk latest_status=failed") == And((
        Cmp("project", "=", "nightdesk"),
        Cmp("latest_status", "=", "failed"),
    ))


def test_free_text_words():
    assert parse_query("fix the bug") == And((
        Text("fix"), Text("the"), Text("bug"),
    ))


def test_colon_alias_kept():
    assert parse_query("model:opus") == Cmp("model", ":", "opus")


def test_not_equals():
    assert parse_query("status!=archived") == Cmp("status", "!=", "archived")


def test_numeric_and_date_ranges():
    assert parse_query("cost>0.5") == Cmp("cost", ">", "0.5")
    assert parse_query("created<=2026-01-01") == Cmp("created", "<=", "2026-01-01")


def test_comma_list_value_kept():
    assert parse_query("status=review,running") == Cmp("status", "=", "review,running")


def test_or_two_terms():
    assert parse_query("project=a OR project=b") == Or((
        Cmp("project", "=", "a"), Cmp("project", "=", "b"),
    ))


def test_or_flattens():
    assert parse_query("a OR b OR c") == Or((Text("a"), Text("b"), Text("c")))


def test_not_keyword():
    assert parse_query("NOT status=archived") == Not(Cmp("status", "=", "archived"))


def test_dash_negation_on_field_and_text():
    assert parse_query("-status=archived") == Not(Cmp("status", "=", "archived"))
    assert parse_query("-flaky") == Not(Text("flaky"))


def test_parens_group():
    assert parse_query("(project=a OR project=b) status=review") == And((
        Or((Cmp("project", "=", "a"), Cmp("project", "=", "b"))),
        Cmp("status", "=", "review"),
    ))


def test_quoted_phrase_is_text():
    assert parse_query('"exact phrase"') == Text("exact phrase", phrase=True)


def test_quoted_field_value():
    assert parse_query('project="my proj"') == Cmp("project", "=", "my proj")


def test_unknown_field_is_free_text():
    assert parse_query("foo=bar") == Text("foo=bar")


def test_and_keyword_is_optional():
    assert parse_query("a AND b") == And((Text("a"), Text("b")))


def test_full_boolean_example():
    got = parse_query('(project=nightdesk OR project=omc) NOT status=archived "exact phrase"')
    assert got == And((
        Or((Cmp("project", "=", "nightdesk"), Cmp("project", "=", "omc"))),
        Not(Cmp("status", "=", "archived")),
        Text("exact phrase", phrase=True),
    ))


def test_malformed_unbalanced_parens_do_not_crash():
    # Should parse without raising; exact shape is lenient.
    parse_query("(((project=a")
    parse_query("project=a) ) )")
    parse_query("NOT")
    parse_query("OR OR")


# --------------------------------------------------------------------------- #
# Compiler / search helpers (against an in-memory DB)
# --------------------------------------------------------------------------- #
def _setup_fts(session):
    session.execute(text(
        "CREATE VIRTUAL TABLE IF NOT EXISTS tickets_fts USING fts5("
        "title, prompt, id UNINDEXED)"
    ))
    session.commit()


def _fts_insert(session, t: Ticket):
    session.execute(text(
        "INSERT INTO tickets_fts(rowid, title, prompt, id) "
        "VALUES ((SELECT rowid FROM tickets WHERE id=:id), :title, :prompt, :id)"
    ), {"id": t.id, "title": t.title, "prompt": t.prompt})
    session.commit()


def _profile(session, name="default", backend="claude_sdk") -> Profile:
    p = Profile(
        name=name, backend=backend,
        fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
        network_mode="off", network_allowlist=[], secret_keys=[],
    )
    session.add(p)
    session.commit()
    return p


def _project(session, slug, name=None) -> Project:
    p = Project(name=name or slug, slug=slug, source_path=f"/src/{slug}")
    session.add(p)
    session.commit()
    return p


def _ticket(session, *, tid, title, profile, status="draft", project=None, prompt=""):
    t = Ticket(
        id=tid, title=title, prompt=prompt, status=status, priority=0, position=0,
        profile_id=profile.id, project_id=project.id if project else None,
    )
    session.add(t)
    session.commit()
    return t


def _run(session, *, rid, ticket, exit_status=None, model=None, cost=None,
          intent="first_run", finished=True, started=None):
    started = started or datetime.now(timezone.utc)
    r = Run(
        id=rid, ticket_id=ticket.id, started_at=started,
        finished_at=started if finished else None,
        exit_status=exit_status, model_used=model, cost_usd=cost, intent=intent,
        worktree_path="/wt", transcript_path="/tr", host="h",
    )
    session.add(r)
    session.flush()
    ticket.current_run_id = r.id
    session.commit()
    return r


@pytest.fixture
def seeded(session):
    _setup_fts(session)
    pdef = _profile(session, "default", backend="claude_sdk")
    palt = _profile(session, "research", backend="codex")
    proj_nd = _project(session, "nightdesk")
    proj_omc = _project(session, "omc")

    t1 = _ticket(session, tid="t1", title="add search bar", profile=pdef,
                 status="review", project=proj_nd, prompt="rich filter")
    t2 = _ticket(session, tid="t2", title="fix the poller", profile=pdef,
                 status="running", project=proj_nd, prompt="board polling")
    t3 = _ticket(session, tid="t3", title="Spike on indexing", profile=palt,
                 status="draft", project=proj_omc, prompt="explore options")
    t4 = _ticket(session, tid="t4", title="orphan task", profile=pdef,
                 status="draft", project=None, prompt="no project here")
    for t in (t1, t2, t3, t4):
        _fts_insert(session, t)

    # latest runs
    _run(session, rid="r1", ticket=t1, exit_status="failed",
         model="claude-opus-4-x", cost=0.82)
    _run(session, rid="r3", ticket=t3, exit_status="success",
         model="claude-sonnet-4-6", cost=0.10)
    # t2 has a running (unfinished) latest run; t4 has no run at all
    _run(session, rid="r2", ticket=t2, exit_status=None, finished=False,
         model=None, cost=None)
    return session


def _ids(rows):
    return sorted(r.id for r in rows)


def test_status_filter(seeded):
    got = search_tickets(seeded, parse_query("status=draft"))
    assert _ids(got) == ["t3", "t4"]


def test_status_comma_list(seeded):
    got = search_tickets(seeded, parse_query("status=review,running"))
    assert _ids(got) == ["t1", "t2"]


def test_project_slug(seeded):
    got = search_tickets(seeded, parse_query("project=nightdesk"))
    assert _ids(got) == ["t1", "t2"]


def test_project_none(seeded):
    got = search_tickets(seeded, parse_query("project=none"))
    assert _ids(got) == ["t4"]


def test_latest_status_failed(seeded):
    got = search_tickets(seeded, parse_query("latest_status=failed"))
    assert _ids(got) == ["t1"]


def test_latest_status_running(seeded):
    got = search_tickets(seeded, parse_query("latest_status=running"))
    assert _ids(got) == ["t2"]


def test_latest_status_none(seeded):
    got = search_tickets(seeded, parse_query("latest_status=none"))
    assert _ids(got) == ["t4"]


def test_profile_name(seeded):
    got = search_tickets(seeded, parse_query("profile=research"))
    assert _ids(got) == ["t3"]


def test_backend_filter(seeded):
    got = search_tickets(seeded, parse_query("backend=codex"))
    assert _ids(got) == ["t3"]


def test_model_contains(seeded):
    got = search_tickets(seeded, parse_query("model:opus"))
    assert _ids(got) == ["t1"]


def test_cost_range(seeded):
    got = search_tickets(seeded, parse_query("cost>0.5"))
    assert _ids(got) == ["t1"]


def test_free_text_matches_title_and_prompt(seeded):
    assert _ids(search_tickets(seeded, parse_query("search"))) == ["t1"]
    assert _ids(search_tickets(seeded, parse_query("poller"))) == ["t2"]


def test_free_text_prefix_and_case_insensitive(seeded):
    # FTS does word-prefix, case-insensitive matching: "ind" finds the token
    # "indexing", "POLL" finds "poller".
    assert _ids(search_tickets(seeded, parse_query("ind"))) == ["t3"]
    assert _ids(search_tickets(seeded, parse_query("POLL"))) == ["t2"]


def test_ensure_fts_index_recreates_triggers_and_reindexes(engine, session):
    # A ticket created while the FTS triggers were missing is invisible to text
    # search until ensure_fts_index rebuilds the index.
    from sqlalchemy import text as _text
    from nightdesk.domain.search import ensure_fts_index
    _setup_fts(session)
    p = _profile(session)
    _ticket(session, tid="z1", title="Update the widget", profile=p, prompt="")
    # No trigger and no manual insert, so it is absent from the index.
    assert _ids(search_tickets(session, parse_query("widget"))) == []

    ensure_fts_index(engine)
    session.expire_all()
    assert _ids(search_tickets(session, parse_query("widget"))) == ["z1"]
    # Triggers are restored, so a later edit stays indexed.
    triggers = {r[0] for r in session.execute(_text(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ))}
    assert {"tickets_ai", "tickets_ad", "tickets_au"} <= triggers


def test_and_combination(seeded):
    got = search_tickets(seeded, parse_query("project=nightdesk latest_status=failed"))
    assert _ids(got) == ["t1"]


def test_or_combination(seeded):
    got = search_tickets(seeded, parse_query("project=omc OR latest_status=failed"))
    assert _ids(got) == ["t1", "t3"]


def test_not_combination(seeded):
    got = search_tickets(seeded, parse_query("project=nightdesk NOT status=running"))
    assert _ids(got) == ["t1"]


def test_status_narrowing_for_board(seeded):
    # Board passes status per column; a status= term empties excluded columns.
    assert _ids(search_tickets(seeded, parse_query("status=review"), status="review")) == ["t1"]
    assert search_tickets(seeded, parse_query("status=review"), status="draft") == []


def test_match_all_returns_everything(seeded):
    got = search_tickets(seeded, parse_query(""))
    assert _ids(got) == ["t1", "t2", "t3", "t4"]


# ---- runs search ---------------------------------------------------------- #
def test_search_runs_by_outcome(seeded):
    got = search_runs(seeded, parse_query("outcome=failed"))
    assert _ids(got) == ["r1"]


def test_search_runs_by_model(seeded):
    got = search_runs(seeded, parse_query("model:sonnet"))
    assert _ids(got) == ["r3"]


def test_search_runs_running(seeded):
    got = search_runs(seeded, parse_query("outcome=running"))
    assert _ids(got) == ["r2"]


def test_search_runs_by_project(seeded):
    got = search_runs(seeded, parse_query("project=nightdesk"))
    assert _ids(got) == ["r1", "r2"]


def test_search_runs_cost_range(seeded):
    got = search_runs(seeded, parse_query("cost>0.5"))
    assert _ids(got) == ["r1"]


def test_search_runs_free_text(seeded):
    # Free text on runs matches the parent ticket's title/prompt.
    got = search_runs(seeded, parse_query("search"))
    assert _ids(got) == ["r1"]
