"""Tests for subscription usage: GET /api/v1/providers/usage plus the vendor
response parsers. Fixtures under tests/data are scrubbed captures of the real
Anthropic /api/oauth/usage and ChatGPT /backend-api/wham/usage responses."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from nightdesk.api.routes import providers as providers_route
from nightdesk.api.routes.providers import (
    _parse_anthropic_usage,
    _parse_codex_usage,
    _severity_for,
)


DATA_DIR = Path(__file__).parent / "data"


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text())


# --- parser unit tests -----------------------------------------------------


def test_severity_thresholds():
    assert _severity_for(0) == "normal"
    assert _severity_for(74.9) == "normal"
    assert _severity_for(75) == "warning"
    assert _severity_for(89.9) == "warning"
    assert _severity_for(90) == "critical"
    assert _severity_for(100) == "critical"


def test_parse_anthropic_prefers_limits_array():
    payload = _load("anthropic_oauth_usage_sample.json")
    plan, windows = _parse_anthropic_usage(payload)
    assert plan is None
    # limits[] has session, weekly_all, weekly_scoped(model=Fable) — all three
    # kept even though is_active is false on the first two.
    labels = [w.label for w in windows]
    assert labels == ["5h", "Weekly", "Weekly · Fable"]
    session_w = windows[0]
    assert session_w.used_percent == 13.0
    assert session_w.severity == "normal"
    assert session_w.resets_at is not None
    assert session_w.resets_at.tzinfo is not None
    # model-scoped weekly carries its own percent.
    assert windows[2].used_percent == 42.0


def test_parse_anthropic_falls_back_to_top_level_blocks():
    payload = {
        "five_hour": {"utilization": 80.0, "resets_at": "2026-07-19T16:00:00+00:00"},
        "seven_day": {"utilization": 33.0, "resets_at": "2026-07-20T16:00:00+00:00"},
    }
    plan, windows = _parse_anthropic_usage(payload)
    assert plan is None
    assert [(w.label, w.used_percent, w.severity) for w in windows] == [
        ("5h", 80.0, "warning"),
        ("Weekly", 33.0, "normal"),
    ]
    assert windows[0].resets_at == datetime(2026, 7, 19, 16, 0, tzinfo=timezone.utc)


def test_parse_anthropic_computes_severity_when_absent():
    payload = {"limits": [{"kind": "session", "percent": 95, "resets_at": None}]}
    _, windows = _parse_anthropic_usage(payload)
    assert windows[0].severity == "critical"


def test_parse_codex_windows_and_plan():
    payload = _load("wham_usage_sample.json")
    plan, windows = _parse_codex_usage(payload)
    assert plan == "prolite"
    # primary_window (weekly) + one additional_rate_limits entry.
    labels = [w.label for w in windows]
    assert labels == ["Weekly", "GPT-5.3-Codex-Spark"]
    primary = windows[0]
    assert primary.used_percent == 36.0
    # epoch seconds -> aware UTC datetime.
    assert primary.resets_at == datetime.fromtimestamp(1784983664, tz=timezone.utc)
    assert primary.severity == "normal"


def test_parse_codex_window_label_humanizes_seconds():
    payload = {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {"used_percent": 10, "limit_window_seconds": 18000,
                               "reset_at": 1784983664},
            "secondary_window": {"used_percent": 20, "limit_window_seconds": 43200,
                                 "reset_at": 1784983664},
        },
    }
    _, windows = _parse_codex_usage(payload)
    assert [w.label for w in windows] == ["5h", "12h"]


def test_parse_codex_severity_thresholds():
    payload = {
        "rate_limit": {
            "primary_window": {"used_percent": 92, "limit_window_seconds": 604800,
                               "reset_at": 1784983664},
        },
    }
    _, windows = _parse_codex_usage(payload)
    assert windows[0].severity == "critical"


# --- route tests -----------------------------------------------------------


_RealClient = httpx.Client


def _patch_client(monkeypatch, handler) -> None:
    def _fake_client(**kwargs):
        kwargs.pop("transport", None)
        return _RealClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", _fake_client)


def _clear_cache(monkeypatch):
    """Isolate the module-level TTL cache from other tests."""
    monkeypatch.setattr(providers_route, "_usage_cache", {})


async def _create_claude_subscription(client, *, cred_path) -> str:
    r = await client.post("/api/v1/providers", json={
        "name": "Claude", "vendor": "anthropic",
        "endpoints": [{
            "label": "Subscription", "protocol_kind": "anthropic",
            "credential_source": "subscription_file", "credential_value": str(cred_path),
        }],
    })
    assert r.status_code == 201, r.text
    return r.json()["endpoints"][0]["id"]


async def _create_codex(client, *, auth_path) -> str:
    r = await client.post("/api/v1/providers", json={
        "name": "Codex", "vendor": "openai",
        "endpoints": [{
            "label": "Codex", "protocol_kind": "openai_codex",
            "credential_source": "oauth_file", "credential_value": str(auth_path),
        }],
    })
    assert r.status_code == 201, r.text
    return r.json()["endpoints"][0]["id"]


async def test_usage_empty_when_no_subscription_endpoints(client, monkeypatch):
    _clear_cache(monkeypatch)
    r = await client.get("/api/v1/providers/usage")
    assert r.status_code == 200, r.text
    assert r.json() == {"endpoints": []}


async def test_usage_anthropic_success(client, monkeypatch, tmp_path):
    _clear_cache(monkeypatch)
    cred = tmp_path / "creds.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-oauth-abc"}}))
    eid = await _create_claude_subscription(client, cred_path=cred)

    payload = _load("anthropic_oauth_usage_sample.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.anthropic.com/api/oauth/usage"
        assert request.headers["authorization"] == "Bearer sk-oauth-abc"
        assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
        assert request.headers["user-agent"] == "claude-code/2.0.32"
        return httpx.Response(200, json=payload)

    _patch_client(monkeypatch, handler)

    r = await client.get("/api/v1/providers/usage")
    assert r.status_code == 200, r.text
    entries = r.json()["endpoints"]
    assert len(entries) == 1
    e = entries[0]
    assert e["endpoint_id"] == eid
    assert e["provider_name"] == "Claude"
    assert e["error"] is None
    assert [w["label"] for w in e["windows"]] == ["5h", "Weekly", "Weekly · Fable"]


async def test_usage_codex_success(client, monkeypatch, tmp_path):
    _clear_cache(monkeypatch)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {
        "access_token": "codex-access", "account_id": "acct-xyz",
    }}))
    eid = await _create_codex(client, auth_path=auth)

    payload = _load("wham_usage_sample.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://chatgpt.com/backend-api/wham/usage"
        assert request.headers["authorization"] == "Bearer codex-access"
        assert request.headers["chatgpt-account-id"] == "acct-xyz"
        return httpx.Response(200, json=payload)

    _patch_client(monkeypatch, handler)

    r = await client.get("/api/v1/providers/usage")
    assert r.status_code == 200, r.text
    e = r.json()["endpoints"][0]
    assert e["endpoint_id"] == eid
    assert e["plan"] == "prolite"
    assert [w["label"] for w in e["windows"]] == ["Weekly", "GPT-5.3-Codex-Spark"]


async def test_usage_upstream_error_returns_200_with_error(client, monkeypatch, tmp_path):
    _clear_cache(monkeypatch)
    cred = tmp_path / "creds.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-oauth-abc"}}))
    await _create_claude_subscription(client, cred_path=cred)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    _patch_client(monkeypatch, handler)

    r = await client.get("/api/v1/providers/usage")
    assert r.status_code == 200, r.text
    e = r.json()["endpoints"][0]
    assert e["error"] is not None
    assert e["windows"] == []


async def test_usage_cache_serves_stale_on_later_error(client, monkeypatch, tmp_path):
    _clear_cache(monkeypatch)
    cred = tmp_path / "creds.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-oauth-abc"}}))
    await _create_claude_subscription(client, cred_path=cred)

    payload = _load("anthropic_oauth_usage_sample.json")
    calls = {"n": 0}

    def ok_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=payload)

    _patch_client(monkeypatch, ok_handler)
    r = await client.get("/api/v1/providers/usage")
    assert r.json()["endpoints"][0]["error"] is None
    assert calls["n"] == 1

    # Second call within the TTL is served straight from cache — no fetch.
    r = await client.get("/api/v1/providers/usage")
    assert calls["n"] == 1
    assert r.json()["endpoints"][0]["error"] is None

    # Expire the cache, then fail upstream: the last good windows come back
    # with error set rather than a 5xx or an empty result.
    monkeypatch.setattr(providers_route, "_USAGE_CACHE_TTL", 0.0)

    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _patch_client(monkeypatch, fail_handler)
    r = await client.get("/api/v1/providers/usage")
    assert r.status_code == 200
    e = r.json()["endpoints"][0]
    assert e["error"] is not None
    assert [w["label"] for w in e["windows"]] == ["5h", "Weekly", "Weekly · Fable"]
