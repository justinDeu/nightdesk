"""External integrations API (JSON, ``/api/v1``).

GitLab v1: read + link + import, no writes to GitLab. Credentials are
Fernet-encrypted with :class:`ProfileSecretBox` (same scheme as providers) and
never returned — responses expose only ``credential_set``. Browse reads
(issue/MR lists + single-item detail) are proxied live through a per-URL TTL
cache; linked items (``external_links``) are the only persisted external state.

Three auth models share this file, following the runs-route split:

- Admin (cookie/bearer): connections + repo-links CRUD, project attach, import.
- ``integrations.read`` (admin OR run token): browse + external-link reads.
- ``integrations.link.self`` (admin OR run token on its OWN ticket):
  external-link create/delete.

See docs/design/gitlab-jira-integrations.md §4-5.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from nightdesk.api.auth import (
    AdminPrincipal,
    Principal,
    enforce_self_ticket,
    require_scopes,
    require_token_cookie_or_bearer,
)
from nightdesk.api.schemas import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionTestResult,
    ConnectionUpdate,
    ExternalLinkCreate,
    ExternalLinkOut,
    ImportTicketRequest,
    ProjectRepoLinksReplace,
    ProviderProjectOut,
    RepoLinkCreate,
    RepoLinkOut,
    RepoSuggestOut,
)
from nightdesk.db.models import Project
from nightdesk.domain.profile_secrets import ProfileSecretBox
from nightdesk.domain.worktree_preview import git_value
from nightdesk.integrations import (
    AuthError,
    IntegrationError,
    NotFoundError,
    RateLimited,
    Unreachable,
)
from nightdesk.integrations.gitlab import normalize_remote_url
from nightdesk.domain import integrations as ints


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _connection_out(c) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "provider": c.provider,
        "base_url": c.base_url,
        "auth_kind": c.auth_kind,
        "credential_set": bool(c.credential),
        "status": c.status,
        "status_detail": c.status_detail,
        "last_checked_at": c.last_checked_at,
        "repo_link_count": len(c.repo_links),
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _repo_link_out(rl, *, project_ids: Optional[list[str]] = None) -> dict:
    return {
        "id": rl.id,
        "connection_id": rl.connection_id,
        "external_kind": rl.external_kind,
        "external_id": rl.external_id,
        "external_path": rl.external_path,
        "display_name": rl.display_name,
        "git_remote_url": rl.git_remote_url,
        "web_url": rl.web_url,
        "project_ids": project_ids if project_ids is not None else [p.id for p in rl.projects],
        "created_at": rl.created_at,
        "updated_at": rl.updated_at,
    }


def _external_link_out(link) -> dict:
    return {
        "id": link.id,
        "ticket_id": link.ticket_id,
        "repo_link_id": link.repo_link_id,
        "kind": link.kind,
        "external_iid": link.external_iid,
        "role": link.role,
        "url": link.url,
        "title": link.title,
        "state": link.state,
        "state_detail": link.state_detail,
        "author_kind": link.author_kind,
        "synced_at": link.synced_at,
        "created_at": link.created_at,
        "updated_at": link.updated_at,
    }


def _gitlab_project_out(item: dict) -> dict:
    return {
        "external_id": str(item.get("id")),
        "external_path": item.get("path_with_namespace") or "",
        "display_name": item.get("name_with_namespace") or item.get("name") or "",
        "web_url": item.get("web_url") or "",
        "git_remote_url": normalize_remote_url(
            item.get("http_url_to_repo") or item.get("ssh_url_to_repo")
        ),
    }


def _map_integration_error(exc: IntegrationError) -> HTTPException:
    if isinstance(exc, AuthError):
        return HTTPException(502, f"connection auth failed: {exc.message}")
    if isinstance(exc, NotFoundError):
        return HTTPException(404, exc.message)
    if isinstance(exc, RateLimited):
        return HTTPException(429, exc.message)
    if isinstance(exc, Unreachable):
        return HTTPException(502, exc.message)
    return HTTPException(502, exc.message)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_router(get_session, bearer_token: str, engine=None) -> APIRouter:
    box = ProfileSecretBox(bearer_token) if bearer_token else None
    browse_cache = ints.TTLCache()

    # Admin-only surface (UI cookie / bearer).
    admin = APIRouter(
        prefix="/api/v1",
        tags=["integrations"],
        dependencies=[Depends(require_token_cookie_or_bearer(bearer_token))],
    )
    # Scoped surface: admin OR a run token holding the named scope. Falls back to
    # a plain cookie/bearer gate only when no engine is wired (tests without run
    # tokens), so the UI still reaches these routes.
    scoped = APIRouter(prefix="/api/v1", tags=["integrations"])
    if engine is not None:
        _read_gate = require_scopes(bearer_token, engine, ["integrations.read"])
        _link_gate = require_scopes(bearer_token, engine, ["integrations.link.self"])
    else:
        _read_gate = require_token_cookie_or_bearer(bearer_token)
        _link_gate = require_token_cookie_or_bearer(bearer_token)

    def _encrypt(value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        if box is None:
            raise HTTPException(
                500, "credential cannot be stored: bearer_token is empty",
            )
        return box.encrypt(value)

    def _live(session: Session, connection, fn):
        """Run a live GitLab call, mapping errors and stamping connection.status
        on auth/unreachable failures so Settings reflects a dead token."""
        try:
            client = ints.client_for(connection, box)
            return fn(client)
        except (AuthError, Unreachable) as exc:
            connection.status = "auth_failed" if isinstance(exc, AuthError) else "unreachable"
            connection.status_detail = exc.message
            connection.last_checked_at = datetime.now(timezone.utc)
            session.commit()
            raise _map_integration_error(exc)
        except IntegrationError as exc:
            raise _map_integration_error(exc)

    # -- connections ----------------------------------------------------

    @admin.get("/connections", response_model=list[ConnectionOut])
    async def list_connections_api(session: Session = Depends(get_session)):
        return [_connection_out(c) for c in ints.list_connections(session)]

    @admin.post("/connections", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
    async def create_connection_api(payload: ConnectionCreate, session: Session = Depends(get_session)):
        try:
            c = ints.create_connection(
                session,
                name=payload.name,
                provider=payload.provider,
                base_url=payload.base_url,
                auth_kind=payload.auth_kind,
                credential=_encrypt(payload.credential_value),
            )
        except ints.UnknownProvider:
            raise HTTPException(400, f"unsupported provider {payload.provider!r} (v1 is GitLab only)")
        except ints.UnknownAuthKind:
            raise HTTPException(400, f"unsupported auth_kind {payload.auth_kind!r}")
        except ints.ConnectionNameTaken:
            raise HTTPException(409, "name taken")
        return _connection_out(c)

    @admin.patch("/connections/{cid}", response_model=ConnectionOut)
    async def update_connection_api(
        cid: str, payload: ConnectionUpdate, session: Session = Depends(get_session),
    ):
        raw = payload.model_dump(exclude_unset=True)
        fields: dict[str, Any] = {}
        for key in ("name", "base_url", "auth_kind"):
            if key in raw:
                fields[key] = raw[key]
        if "credential_value" in raw:
            fields["credential"] = _encrypt(raw["credential_value"])
        try:
            c = ints.update_connection(session, cid, **fields)
        except ints.ConnectionNotFound:
            raise HTTPException(404, "not found")
        except ints.UnknownAuthKind:
            raise HTTPException(400, "unsupported auth_kind")
        except ints.ConnectionNameTaken:
            raise HTTPException(409, "name taken")
        return _connection_out(c)

    @admin.delete("/connections/{cid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_connection_api(cid: str, session: Session = Depends(get_session)):
        try:
            ints.delete_connection(session, cid)
        except ints.ConnectionNotFound:
            raise HTTPException(404, "not found")
        except ints.ConnectionInUse:
            raise HTTPException(409, "connection has repo links; remove them first")
        return None

    @admin.post("/connections/{cid}/test", response_model=ConnectionTestResult)
    async def test_connection_api(cid: str, session: Session = Depends(get_session)):
        try:
            c = ints.get_connection(session, cid)
        except ints.ConnectionNotFound:
            raise HTTPException(404, "not found")
        c = ints.test_connection(session, c, box)
        return {
            "status": c.status,
            "status_detail": c.status_detail,
            "last_checked_at": c.last_checked_at,
        }

    @admin.get("/connections/{cid}/projects", response_model=list[ProviderProjectOut])
    async def connection_projects_api(
        cid: str,
        search: Optional[str] = Query(default=None),
        session: Session = Depends(get_session),
    ):
        try:
            c = ints.get_connection(session, cid)
        except ints.ConnectionNotFound:
            raise HTTPException(404, "not found")
        page = _live(session, c, lambda cl: cl.list_projects(search=search))
        return [_gitlab_project_out(it) for it in page.items]

    # -- repo links -----------------------------------------------------

    @admin.get("/repo-links", response_model=list[RepoLinkOut])
    async def list_repo_links_api(
        connection_id: Optional[str] = Query(default=None),
        session: Session = Depends(get_session),
    ):
        return [_repo_link_out(rl) for rl in ints.list_repo_links(session, connection_id)]

    @admin.post("/repo-links", response_model=RepoLinkOut, status_code=status.HTTP_201_CREATED)
    async def create_repo_link_api(payload: RepoLinkCreate, session: Session = Depends(get_session)):
        try:
            rl = ints.create_repo_link(
                session,
                connection_id=payload.connection_id,
                external_kind=payload.external_kind,
                external_id=payload.external_id,
                external_path=payload.external_path,
                display_name=payload.display_name,
                git_remote_url=payload.git_remote_url,
                web_url=payload.web_url,
            )
        except ints.ConnectionNotFound:
            raise HTTPException(404, "connection not found")
        except ints.RepoLinkExists:
            raise HTTPException(409, "repo link already exists for this connection")
        return _repo_link_out(rl)

    @admin.delete("/repo-links/{rid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_repo_link_api(rid: str, session: Session = Depends(get_session)):
        try:
            ints.delete_repo_link(session, rid)
        except ints.RepoLinkNotFound:
            raise HTTPException(404, "not found")
        except ints.RepoLinkInUse:
            raise HTTPException(409, "repo link has external links; remove them first")
        return None

    # -- project <-> repo-link attachment -------------------------------

    @admin.get("/projects/{pid}/repo-links", response_model=list[RepoLinkOut])
    async def list_project_repo_links_api(pid: str, session: Session = Depends(get_session)):
        if session.get(Project, pid) is None:
            raise HTTPException(404, "project not found")
        return [_repo_link_out(rl) for rl in ints.list_project_repo_links(session, pid)]

    @admin.put("/projects/{pid}/repo-links", response_model=list[RepoLinkOut])
    async def set_project_repo_links_api(
        pid: str, payload: ProjectRepoLinksReplace, session: Session = Depends(get_session),
    ):
        try:
            links = ints.set_project_repo_links(session, pid, payload.repo_link_ids)
        except KeyError:
            raise HTTPException(404, "project not found")
        except ints.RepoLinkNotFound:
            raise HTTPException(404, "one or more repo links not found")
        return [_repo_link_out(rl) for rl in links]

    @admin.get("/projects/{pid}/repo-suggest", response_model=RepoSuggestOut)
    async def repo_suggest_api(pid: str, session: Session = Depends(get_session)):
        """Read ``git remote get-url origin`` under the project's source_path,
        normalize it, and pre-select a matching repo link if one exists."""
        project = session.get(Project, pid)
        if project is None:
            raise HTTPException(404, "project not found")
        remote = None
        try:
            source = Path(os.path.expanduser((project.source_path or "").strip()))
            remote = git_value(source, "remote", "get-url", "origin")
        except Exception:
            remote = None
        normalized = normalize_remote_url(remote)
        matched = None
        if normalized:
            for rl in ints.list_repo_links(session):
                if rl.git_remote_url == normalized:
                    matched = rl.id
                    break
        return {"git_remote_url": normalized, "matched_repo_link_id": matched}

    # -- browse (scoped: admin OR integrations.read) --------------------

    def _repo(session: Session, rid: str):
        try:
            return ints.get_repo_link(session, rid)
        except ints.RepoLinkNotFound:
            raise HTTPException(404, "not found")

    @scoped.get("/repo-links/{rid}/issues", dependencies=[Depends(_read_gate)])
    async def list_issues_api(
        rid: str,
        state: Optional[str] = Query(default=None),
        search: Optional[str] = Query(default=None),
        page_token: Optional[str] = Query(default=None),
        session: Session = Depends(get_session),
    ):
        repo = _repo(session, rid)
        key = f"issues:{rid}:{state}:{search}:{page_token}"
        cached = browse_cache.get(key)
        if cached is not None:
            return cached
        connection = ints.get_connection(session, repo.connection_id)
        page = _live(
            session, connection,
            lambda cl: cl.list_issues(
                repo.external_id, state=state, search=search, page_token=page_token,
            ),
        )
        result = {"items": page.items, "next_page_token": page.next_page_token}
        browse_cache.set(key, result)
        return result

    @scoped.get("/repo-links/{rid}/issues/{iid}", dependencies=[Depends(_read_gate)])
    async def get_issue_api(rid: str, iid: str, session: Session = Depends(get_session)):
        repo = _repo(session, rid)
        key = f"issue:{rid}:{iid}"
        cached = browse_cache.get(key)
        if cached is not None:
            return cached
        connection = ints.get_connection(session, repo.connection_id)
        item = _live(session, connection, lambda cl: cl.get_issue(repo.external_id, iid))
        browse_cache.set(key, item)
        return item

    @scoped.get("/repo-links/{rid}/merge-requests", dependencies=[Depends(_read_gate)])
    async def list_mrs_api(
        rid: str,
        state: Optional[str] = Query(default=None),
        search: Optional[str] = Query(default=None),
        page_token: Optional[str] = Query(default=None),
        session: Session = Depends(get_session),
    ):
        repo = _repo(session, rid)
        key = f"mrs:{rid}:{state}:{search}:{page_token}"
        cached = browse_cache.get(key)
        if cached is not None:
            return cached
        connection = ints.get_connection(session, repo.connection_id)
        page = _live(
            session, connection,
            lambda cl: cl.list_mrs(
                repo.external_id, state=state, search=search, page_token=page_token,
            ),
        )
        result = {"items": page.items, "next_page_token": page.next_page_token}
        browse_cache.set(key, result)
        return result

    @scoped.get("/repo-links/{rid}/merge-requests/{iid}", dependencies=[Depends(_read_gate)])
    async def get_mr_api(rid: str, iid: str, session: Session = Depends(get_session)):
        repo = _repo(session, rid)
        key = f"mr:{rid}:{iid}"
        cached = browse_cache.get(key)
        if cached is not None:
            return cached
        connection = ints.get_connection(session, repo.connection_id)
        item = _live(session, connection, lambda cl: cl.get_mr(repo.external_id, iid))
        browse_cache.set(key, item)
        return item

    # -- external links (ticket <-> issue/MR) ---------------------------

    @scoped.get(
        "/tickets/{tid}/external-links",
        response_model=list[ExternalLinkOut],
        dependencies=[Depends(_read_gate)],
    )
    async def list_ticket_links_api(tid: str, session: Session = Depends(get_session)):
        return [_external_link_out(l) for l in ints.list_ticket_external_links(session, tid)]

    @scoped.post(
        "/tickets/{tid}/external-links",
        response_model=ExternalLinkOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_ticket_link_api(
        tid: str,
        payload: ExternalLinkCreate,
        session: Session = Depends(get_session),
        principal: Principal = Depends(_link_gate),
    ):
        enforce_self_ticket(principal, tid)
        is_admin = isinstance(principal, AdminPrincipal)
        author_kind = "admin" if is_admin else "agent"
        author_run_id = None if is_admin else getattr(principal, "run_id", None)
        try:
            link = ints.create_external_link(
                session,
                ticket_id=tid,
                repo_link_id=payload.repo_link_id,
                kind=payload.kind,
                external_iid=payload.external_iid,
                role=payload.role,
                author_kind=author_kind,
                author_run_id=author_run_id,
            )
        except KeyError:
            raise HTTPException(404, "ticket not found")
        except ints.RepoLinkNotFound:
            raise HTTPException(404, "repo link not found")
        except ints.ExternalLinkExists:
            raise HTTPException(409, "this ticket already links that item")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return _external_link_out(link)

    @scoped.delete(
        "/tickets/{tid}/external-links/{link_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_ticket_link_api(
        tid: str,
        link_id: str,
        session: Session = Depends(get_session),
        principal: Principal = Depends(_link_gate),
    ):
        enforce_self_ticket(principal, tid)
        try:
            link = ints.get_external_link(session, link_id)
        except ints.ExternalLinkNotFound:
            raise HTTPException(404, "not found")
        if link.ticket_id != tid:
            raise HTTPException(404, "not found")
        ints.delete_external_link(session, link_id)
        return None

    @admin.post("/external-links/{link_id}/refresh", response_model=ExternalLinkOut)
    async def refresh_external_link_api(link_id: str, session: Session = Depends(get_session)):
        try:
            link = ints.get_external_link(session, link_id)
        except ints.ExternalLinkNotFound:
            raise HTTPException(404, "not found")
        repo = ints.get_repo_link(session, link.repo_link_id)
        connection = ints.get_connection(session, repo.connection_id)
        now = datetime.now(timezone.utc)

        def _fetch(cl):
            if link.kind == "issue":
                return cl.get_issue(repo.external_id, link.external_iid)
            return cl.get_mr(repo.external_id, link.external_iid)

        item = _live(session, connection, _fetch)
        old = ints._apply_snapshot(link, item, now)
        if old is not None:
            ints.emit_external_link_state_changed(link, old)
        session.commit()
        session.refresh(link)
        return _external_link_out(link)

    # -- import as draft ticket -----------------------------------------

    @admin.post("/repo-links/{rid}/import-ticket", status_code=status.HTTP_201_CREATED)
    async def import_ticket_api(
        rid: str, payload: ImportTicketRequest, session: Session = Depends(get_session),
    ):
        from nightdesk.api.routes.tickets import _ticket_to_out

        repo = _repo(session, rid)
        if payload.kind != "issue":
            raise HTTPException(422, "only issue import is supported in v1")
        try:
            ticket = ints.import_issue_as_draft(
                session, repo, payload.external_iid, box,
                project_id=payload.project_id, profile_id=payload.profile_id,
            )
        except ints.ImportError_ as exc:
            raise HTTPException(422, str(exc))
        except IntegrationError as exc:
            raise _map_integration_error(exc)
        return _ticket_to_out(ticket)

    root = APIRouter()
    root.include_router(admin)
    root.include_router(scoped)
    return root
