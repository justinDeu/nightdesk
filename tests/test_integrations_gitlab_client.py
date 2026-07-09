"""GitLab REST client, driven against a fake httpx transport (no network)."""
import httpx
import pytest

from nightdesk.integrations import (
    AuthError,
    IntegrationError,
    NotFoundError,
    RateLimited,
    Unreachable,
)
from nightdesk.integrations.gitlab import GitLabClient, normalize_remote_url


def _client(handler, *, base_url="https://gitlab.example.com", token="tok"):
    transport = httpx.MockTransport(handler)
    return GitLabClient(base_url, token, client=httpx.Client(transport=transport))


def test_auth_header_and_base_url():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("PRIVATE-TOKEN")
        return httpx.Response(200, json={"version": "17.0"})

    c = _client(handler)
    assert c.test_auth() == {"version": "17.0"}
    assert seen["url"] == "https://gitlab.example.com/api/v4/version"
    assert seen["token"] == "tok"


def test_project_id_path_encoding():
    seen = {}

    def handler(request):
        # raw_path reflects the wire; GitLab requires the %2F to survive.
        seen["path"] = request.url.raw_path.decode().split("?")[0]
        return httpx.Response(200, json=[])

    # Numeric id passes through; a path is percent-encoded (slash included).
    _client(handler).list_issues("42")
    assert seen["path"] == "/api/v4/projects/42/issues"
    _client(handler).list_issues("group/repo")
    assert seen["path"] == "/api/v4/projects/group%2Frepo/issues"


def test_list_issues_pagination_next_page_token():
    def handler(request):
        page = request.url.params.get("page")
        if page is None:
            return httpx.Response(
                200, json=[{"iid": 1, "title": "one"}],
                headers={"X-Next-Page": "2"},
            )
        return httpx.Response(200, json=[{"iid": 2, "title": "two"}], headers={"X-Next-Page": ""})

    c = _client(handler)
    first = c.list_issues("1")
    assert [i["iid"] for i in first.items] == [1]
    assert first.next_page_token == "2"
    second = c.list_issues("1", page_token=first.next_page_token)
    assert [i["iid"] for i in second.items] == [2]
    assert second.next_page_token is None


def test_iids_batch_filter_serialized():
    seen = {}

    def handler(request):
        seen["iids"] = request.url.params.get_list("iids[]")
        return httpx.Response(200, json=[])

    _client(handler).list_mrs("1", iids=[1, 5, 9])
    assert seen["iids"] == ["1", "5", "9"]


def test_error_mapping():
    def make(status, body=None, headers=None):
        def handler(request):
            return httpx.Response(status, json=body or {"message": "x"}, headers=headers or {})
        return _client(handler)

    with pytest.raises(AuthError):
        make(401).get_issue("1", "1")
    with pytest.raises(AuthError):
        make(403).get_issue("1", "1")
    with pytest.raises(NotFoundError):
        make(404).get_issue("1", "1")
    with pytest.raises(IntegrationError):
        make(500).get_issue("1", "1")


def test_rate_limited_carries_retry_after():
    def handler(request):
        return httpx.Response(429, json={"message": "slow down"}, headers={"Retry-After": "30"})

    with pytest.raises(RateLimited) as ei:
        _client(handler).list_issues("1")
    assert ei.value.retry_after == 30.0


def test_test_auth_falls_back_to_user_on_version_403():
    def handler(request):
        if request.url.path.endswith("/version"):
            return httpx.Response(403, json={"message": "insufficient scope"})
        return httpx.Response(200, json={"username": "bot"})

    assert _client(handler).test_auth() == {"username": "bot"}


def test_unreachable_on_transport_error():
    def handler(request):
        raise httpx.ConnectError("dns")

    with pytest.raises(Unreachable):
        _client(handler).list_issues("1")


def test_find_mr_by_branch_returns_first_or_none():
    def hit(request):
        assert request.url.params.get("source_branch") == "fix/x"
        return httpx.Response(200, json=[{"iid": 7, "source_branch": "fix/x"}])

    assert _client(hit).find_mr_by_branch("1", "fix/x")["iid"] == 7

    def miss(request):
        return httpx.Response(200, json=[])

    assert _client(miss).find_mr_by_branch("1", "fix/x") is None


def test_create_mr_is_v2_and_raises():
    with pytest.raises(NotImplementedError):
        GitLabClient("https://gitlab.com", "t").create_mr()


@pytest.mark.parametrize("raw,expected", [
    ("git@gitlab.com:grp/repo.git", "gitlab.com/grp/repo"),
    ("https://gitlab.com/grp/repo", "gitlab.com/grp/repo"),
    ("https://user@gitlab.com/grp/sub/repo.git", "gitlab.com/grp/sub/repo"),
    ("ssh://git@gitlab.example.com:22/grp/repo.git", "gitlab.example.com:22/grp/repo"),
    ("", None),
    (None, None),
])
def test_normalize_remote_url(raw, expected):
    assert normalize_remote_url(raw) == expected
