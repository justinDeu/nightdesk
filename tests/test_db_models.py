from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nightdesk.db.models import (
    Base, Profile, Ticket, Run, ConfigRow, WorkerHeartbeat,
    TicketWorkspace,
)


def make_engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def test_can_create_profile_and_ticket():
    eng = make_engine()
    with Session(eng) as s:
        p = Profile(
            name="readonly",
            fs_read=["/home/u/repo"],
            fs_write=[],
            allowed_tools=["Read", "Bash"],
            denied_tools=[],
            network_mode="off",
            network_allowlist=[],
            secret_keys=[],
        )
        s.add(p)
        s.flush()
        t = Ticket(
            title="hello",
            prompt="say hi",
            status="queued",
            priority=0,
            profile_id=p.id,
            cwd="/tmp",
            run_now=False,
        )
        s.add(t)
        s.commit()
        assert t.id is not None
        assert t.profile.name == "readonly"


def test_ticket_can_track_multiple_workspaces():
    eng = make_engine()
    with Session(eng) as s:
        p = Profile(name="p", fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
                    network_mode="off", network_allowlist=[], secret_keys=[])
        s.add(p); s.flush()
        t = Ticket(title="x", prompt="", status="queued", priority=0,
                   profile_id=p.id, cwd="/tmp")
        s.add(t); s.flush()
        primary = TicketWorkspace(
            ticket_id=t.id,
            role="primary",
            label="app",
            kind="git_worktree",
            access="read_write",
            source_path="/src/app",
            resolved_path="/work/app/feature",
            repo_root="/src/app",
            relative_path=".",
            worktree_name="feature",
            branch="nightdesk/feature",
            retention="preserve",
            state="active",
        )
        linked = TicketWorkspace(
            ticket_id=t.id,
            role="linked",
            label="docs",
            kind="directory",
            access="read_only",
            source_path="/src/docs",
            resolved_path="/src/docs",
            retention="preserve",
            state="active",
        )
        s.add_all([primary, linked])
        s.commit()

        s.refresh(t)
        assert [w.label for w in t.workspaces] == ["app", "docs"]
        assert t.workspaces[0].access == "read_write"
        assert t.workspaces[1].access == "read_only"

def test_run_links_to_ticket():
    eng = make_engine()
    with Session(eng) as s:
        p = Profile(name="p", fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
                    network_mode="off", network_allowlist=[], secret_keys=[])
        s.add(p); s.flush()
        t = Ticket(title="x", prompt="", status="running", priority=0, profile_id=p.id, cwd="/tmp")
        s.add(t); s.flush()
        r = Run(ticket_id=t.id, started_at=datetime.now(timezone.utc),
                worktree_path="/tmp/x", transcript_path="/tmp/x.log", host="h")
        s.add(r); s.commit()
        assert r.ticket.id == t.id



def test_ticket_persists_next_run_context():
    eng = make_engine()
    with Session(eng) as s:
        p = Profile(name="p", fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
                    network_mode="off", network_allowlist=[], secret_keys=[])
        s.add(p); s.flush()
        t = Ticket(
            title="x",
            prompt="base prompt",
            status="review",
            priority=0,
            profile_id=p.id,
            cwd="/tmp/project",
            next_run_context="answer the open question with polling",
            next_run_context_updated_at=datetime.now(timezone.utc),
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        assert t.next_run_context == "answer the open question with polling"
        assert t.next_run_context_updated_at is not None


def test_run_persists_intent_failure_kind_and_parent():
    eng = make_engine()
    with Session(eng) as s:
        p = Profile(name="p", fs_read=[], fs_write=[], allowed_tools=[], denied_tools=[],
                    network_mode="off", network_allowlist=[], secret_keys=[])
        s.add(p); s.flush()
        t = Ticket(title="x", prompt="", status="running", priority=0, profile_id=p.id, cwd="/tmp")
        s.add(t); s.flush()
        r1 = Run(ticket_id=t.id, started_at=datetime.now(timezone.utc),
                 worktree_path="/tmp/x", transcript_path="/tmp/x.log", host="h")
        s.add(r1); s.flush()
        r2 = Run(
            ticket_id=t.id,
            started_at=datetime.now(timezone.utc),
            worktree_path="/tmp/y",
            transcript_path="/tmp/y.log",
            host="h",
            intent="retry",
            parent_run_id=r1.id,
            headless_policy_version="v1",
            restart_workspace_policy="fresh_path",
            failure_kind="headless_requested_input",
        )
        s.add(r2); s.commit()
        s.refresh(r2)
        assert r2.intent == "retry"
        assert r2.parent_run_id == r1.id
        assert r2.failure_kind == "headless_requested_input"

def test_config_row_singleton():
    eng = make_engine()
    with Session(eng) as s:
        c = ConfigRow(id=1, window_start="22:00", window_end="07:00",
                      max_parallel=2, worktree_root="/x", transcript_root="/y")
        s.add(c); s.commit()
        assert s.get(ConfigRow, 1).max_parallel == 2


def test_heartbeat_row():
    eng = make_engine()
    with Session(eng) as s:
        h = WorkerHeartbeat(id=1, host="h", pid=42, last_seen_at=datetime.now(timezone.utc))
        s.add(h); s.commit()
        assert s.get(WorkerHeartbeat, 1).pid == 42
