"""Unit tests for the PoE1 trade API client (httpx MockTransport, no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pob_cli.trade_api import (
    ApiError,
    CookieRequired,
    NoResults,
    RateLimited,
    TradeClient,
    TradeError,
    redact,
)

FIXTURES = Path(__file__).parent / "fixtures"
STATS = json.loads((FIXTURES / "trade_stats.json").read_text(encoding="utf-8"))
FETCH = json.loads((FIXTURES / "trade_fetch.json").read_text(encoding="utf-8"))


def make_client(tmp_path, handler, **kwargs: Any) -> TradeClient:
    return TradeClient(
        cache_dir=tmp_path,
        sleep=lambda _: None,
        clock=lambda: 0.0,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_search_returns_payload(tmp_path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/trade/search/Standard"
        body = json.loads(request.content)
        assert body == {"query": {"status": {"option": "online"}}}
        return httpx.Response(200, json={"id": "q1", "result": ["a", "b"], "total": 2})

    client = make_client(tmp_path, handler)
    payload = client.search("Standard", {"query": {"status": {"option": "online"}}})
    assert payload["id"] == "q1"
    assert payload["result"] == ["a", "b"]
    assert len(calls) == 1


def test_search_no_results(tmp_path) -> None:
    client = make_client(
        tmp_path,
        lambda request: httpx.Response(200, json={"id": "q", "result": [], "total": 0}),
    )
    with pytest.raises(NoResults):
        client.search("Standard", {})


def test_rate_limited_reports_retry_after(tmp_path) -> None:
    client = make_client(
        tmp_path,
        lambda request: httpx.Response(429, headers={"Retry-After": "45"}, text="slow down"),
    )
    with pytest.raises(RateLimited) as excinfo:
        client.search("Standard", {})
    assert excinfo.value.seconds == 45


def test_cookie_required_on_403(tmp_path) -> None:
    client = make_client(tmp_path, lambda request: httpx.Response(403, text="cf challenge"))
    with pytest.raises(CookieRequired) as excinfo:
        client.stats(force_refresh=True)
    assert excinfo.value.status == 403
    assert "POESESSID" in str(excinfo.value)


def test_api_error_on_400(tmp_path) -> None:
    client = make_client(
        tmp_path,
        lambda request: httpx.Response(
            400, json={"error": {"code": 2, "message": "Unknown stat"}}
        ),
    )
    with pytest.raises(ApiError) as excinfo:
        client.search("Standard", {})
    assert excinfo.value.code == 2
    assert "Unknown stat" in str(excinfo.value)


def test_network_error_is_trade_error(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = make_client(tmp_path, handler)
    with pytest.raises(TradeError):
        client.search("Standard", {})


def test_cookie_header_is_sent_but_redacted_from_errors(tmp_path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Cookie", ""))
        return httpx.Response(403, text="bad session 0123456789abcdef0123456789abcdef")

    client = make_client(tmp_path, handler, cookie="POESESSID=0123456789abcdef0123456789abcdef")
    with pytest.raises(CookieRequired) as excinfo:
        client.stats(force_refresh=True)
    assert seen == ["POESESSID=0123456789abcdef0123456789abcdef"]
    assert "0123456789abcdef" not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)


def test_redact_helper() -> None:
    assert "deadbeefdeadbeefdeadbeef" not in redact("POESESSID=deadbeefdeadbeefdeadbeef")
    assert "short" in redact("short")


def test_stats_are_cached(tmp_path) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=STATS)

    client = make_client(tmp_path, handler)
    first = client.stats()
    second = client.stats()
    assert len(calls) == 1
    assert first == second


def test_fetch_chunks_and_query(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/api/trade/fetch/")
        assert request.url.params.get("query") == "q1"
        return httpx.Response(200, json=FETCH)

    client = make_client(tmp_path, handler)
    rows = client.fetch(["a", "b"], "q1")
    assert len(rows) == 3
    with pytest.raises(ValueError):
        client.fetch([str(index) for index in range(11)], "q1")


def test_search_url_and_throttle(tmp_path) -> None:
    sleeps: list[float] = []
    client = TradeClient(
        cache_dir=tmp_path,
        sleep=sleeps.append,
        clock=lambda: 0.0,
        search_interval=6.0,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "q", "result": ["a"], "total": 1})
        ),
    )
    client.search("My League", {})
    assert sleeps == [6.0]
    assert client.search_url("My League", "q 1") == (
        "https://www.pathofexile.com/trade/search/My%20League/q%201"
    )
