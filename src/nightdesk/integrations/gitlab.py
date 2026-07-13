"""GitLab REST v4 client.

PAT-family auth only (``PRIVATE-TOKEN`` header), which covers personal,
project, and group access tokens identically. Self-hosted is first-class: every
call rides the connection's ``base_url`` (default ``https://gitlab.com``) and
the paths are the same on gitlab.com and self-managed. See
docs/design/gitlab-jira-integrations.md §1.

The client is deliberately transport-injectable: pass ``client=httpx.Client(
transport=...)`` to drive it against a fake transport in tests, with no network.
Pagination leaks through as an opaque ``page_token`` (the GitLab page number as
a string); callers must not interpret it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence
from urllib.parse import quote

import httpx

from nightdesk.integrations import (
    AuthError,
    IntegrationError,
    NotFoundError,
    RateLimited,
    Unreachable,
)


log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://gitlab.com"
_DEFAULT_TIMEOUT = 15.0
_PER_PAGE = 50
_MAX_IIDS = 100  # GitLab caps iids[] filters; batch refresh stays under this.


@dataclass(frozen=True)
class Page:
    """One page of a listing plus the opaque token for the next page (or None)."""

    items: list[dict]
    next_page_token: Optional[str]


def _encode_project_id(project_id: str) -> str:
    """GitLab ``:id`` accepts a numeric id verbatim or a URL-encoded
    ``group/repo`` path. A numeric id is passed through; anything else is
    percent-encoded (slashes included)."""
    pid = str(project_id)
    if pid.isdigit():
        return pid
    return quote(pid, safe="")


class GitLabClient:
    """Thin REST v4 wrapper. One instance per (base_url, token)."""

    def __init__(
        self,
        base_url: Optional[str],
        token: Optional[str],
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        client: Optional[httpx.Client] = None,
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._token = token or ""
        self._timeout = timeout
        # Injected client (tests) is used as-is and NOT closed by us.
        self._client = client
        self._owns_client = client is None

    # -- lifecycle -------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "GitLabClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- request core ----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._token:
            h["PRIVATE-TOKEN"] = self._token
        return h

    def _get(self, path: str, params: Optional[dict] = None) -> httpx.Response:
        url = f"{self.base_url}/api/v4{path}"
        try:
            resp = self._http().get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            raise Unreachable(f"could not reach {self.base_url}: {exc}") from exc
        return self._check(resp)

    def _check(self, resp: httpx.Response) -> httpx.Response:
        code = resp.status_code
        if code == 401 or code == 403:
            raise AuthError(_error_message(resp, "authentication failed"), status=code)
        if code == 404:
            raise NotFoundError(_error_message(resp, "not found"), status=404)
        if code == 429:
            retry = resp.headers.get("Retry-After")
            try:
                retry_after = float(retry) if retry is not None else None
            except ValueError:
                retry_after = None
            raise RateLimited("rate limited by GitLab", retry_after=retry_after)
        if code >= 400:
            raise IntegrationError(
                _error_message(resp, f"GitLab returned HTTP {code}"), status=code,
            )
        return resp

    @staticmethod
    def _page(resp: httpx.Response) -> Page:
        items = resp.json()
        if not isinstance(items, list):
            items = []
        nxt = resp.headers.get("X-Next-Page") or ""
        return Page(items=items, next_page_token=nxt.strip() or None)

    # -- read operations -------------------------------------------------

    def test_auth(self) -> dict:
        """Hit ``GET /version`` (falls back to ``/user`` on 403 for tokens
        without the admin/version scope) so a Test action can confirm the
        credential works and surface a friendly status."""
        try:
            resp = self._get("/version")
            return resp.json()
        except AuthError:
            # ``/version`` needs a broader scope on some instances; ``/user``
            # works for any valid token, so a 403 there is a real auth failure.
            resp = self._get("/user")
            return resp.json()

    def current_user(self) -> dict:
        """The token's own user (``GET /user``). Used to derive the
        "awaiting your review" flag on MR list items — the connection user is
        a requested reviewer when its id appears in an MR's ``reviewers``."""
        return self._get("/user").json()

    def list_issues(
        self,
        project_id: str,
        *,
        state: Optional[str] = None,
        search: Optional[str] = None,
        page_token: Optional[str] = None,
        iids: Optional[Sequence[int | str]] = None,
    ) -> Page:
        params = _list_params(state=state, search=search, page_token=page_token, iids=iids)
        return self._page(self._get(f"/projects/{_encode_project_id(project_id)}/issues", params))

    def get_issue(self, project_id: str, iid: str) -> dict:
        return self._get(
            f"/projects/{_encode_project_id(project_id)}/issues/{iid}",
        ).json()

    def list_mrs(
        self,
        project_id: str,
        *,
        state: Optional[str] = None,
        search: Optional[str] = None,
        source_branch: Optional[str] = None,
        page_token: Optional[str] = None,
        iids: Optional[Sequence[int | str]] = None,
    ) -> Page:
        params = _list_params(state=state, search=search, page_token=page_token, iids=iids)
        if source_branch:
            params["source_branch"] = source_branch
        return self._page(
            self._get(f"/projects/{_encode_project_id(project_id)}/merge_requests", params)
        )

    def get_mr(self, project_id: str, iid: str) -> dict:
        return self._get(
            f"/projects/{_encode_project_id(project_id)}/merge_requests/{iid}",
        ).json()

    def find_mr_by_branch(self, project_id: str, source_branch: str) -> Optional[dict]:
        page = self.list_mrs(project_id, source_branch=source_branch)
        return page.items[0] if page.items else None

    def list_projects(self, *, search: Optional[str] = None, page_token: Optional[str] = None) -> Page:
        """Project typeahead for the repo-link picker. ``membership=true`` so a
        PAT only sees projects it can actually reach."""
        params: dict[str, Any] = {
            "per_page": _PER_PAGE,
            "membership": "true",
            "order_by": "last_activity_at",
            "simple": "true",
        }
        if search:
            params["search"] = search
        if page_token:
            params["page"] = page_token
        return self._page(self._get("/projects", params))

    def get_project(self, project_id: str) -> dict:
        return self._get(f"/projects/{_encode_project_id(project_id)}").json()

    # -- write operations (v2 — intentionally unimplemented in v1) -------

    def create_mr(self, *args, **kwargs):  # noqa: D401 — v2 seam
        raise NotImplementedError(
            "MR creation is a v2 feature; v1 GitLab integration is read-only"
        )


def _list_params(
    *,
    state: Optional[str],
    search: Optional[str],
    page_token: Optional[str],
    iids: Optional[Sequence[int | str]],
) -> dict[str, Any]:
    params: dict[str, Any] = {"per_page": _PER_PAGE, "order_by": "updated_at"}
    if state:
        params["state"] = state
    if search:
        params["search"] = search
    if page_token:
        params["page"] = page_token
    if iids:
        # httpx serializes a list value as repeated ``iids[]=`` params.
        params["iids[]"] = [str(i) for i in list(iids)[:_MAX_IIDS]]
    return params


def _error_message(resp: httpx.Response, fallback: str) -> str:
    """GitLab error bodies are ``{"message": ...}`` or ``{"error": ...}``;
    surface the upstream text when present so Settings shows a real reason."""
    try:
        body = resp.json()
    except ValueError:
        return f"{fallback} (HTTP {resp.status_code})"
    if isinstance(body, dict):
        detail = body.get("message") or body.get("error") or body.get("error_description")
        if detail:
            return f"{fallback}: {detail}"
    return f"{fallback} (HTTP {resp.status_code})"


def normalize_remote_url(raw: Optional[str]) -> Optional[str]:
    """Normalize a git clone URL to ``host/group/repo`` for suggestion matching.

    ``git@host:g/r.git`` and ``https://host/g/r`` both collapse to ``host/g/r``.
    Remote URLs are too ambiguous (mirrors, ssh aliases) to be an authoritative
    key — this only powers the pre-select in the attach typeahead.
    """
    if not raw:
        return None
    url = raw.strip()
    if not url:
        return None
    # scp-like syntax: git@host:group/repo.git
    if url.startswith("git@") or ("@" in url.split("/", 1)[0] and ":" in url and "://" not in url):
        _, _, rest = url.partition("@")
        host, _, path = rest.partition(":")
    else:
        # scheme://[user@]host/path
        after_scheme = url.split("://", 1)[-1]
        creds_host, _, path = after_scheme.partition("/")
        host = creds_host.rpartition("@")[2]
    host = host.strip().rstrip("/")
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not host or not path:
        return None
    return f"{host}/{path}".lower()
