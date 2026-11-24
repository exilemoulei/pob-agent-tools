"""Unit tests for pob_cli.trade_client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from pob_cli import trade_client
from pob_cli.trade_client import (
    ApiError,
    CookieRequired,
    NoResults,
    RateLimited,
    TradeClient,
    TradeError,
    TradeRateLimiter,
    _env_cookie,
    redact,
)


def _make_clock(start: float = 1_700_000_000.0):
    state = {"t": float(start)}

    def now() -> float:
        return state["t"]

    def advance(seconds: float) -> None:
        state["t"] += seconds

    return now, advance


# ---------------------------------------------------------------------------
# Redaction & Env-Only Cookie Handling
# ---------------------------------------------------------------------------


def test_redact_masks_hex_tokens_and_cookie_secrets():
    sample = (
        "Cookie: POESESSID=0123456789abcdef0123456789abcdef; "
        "cf_clearance=abc.DEF-123_456.xyz; "
        "session=deadbeefcafebabe99 short=1234abcd"
    )
    cleaned = redact(sample)
    assert "0123456789abcdef" not in cleaned
    assert "POESESSID=" not in cleaned
    assert "cf_clearance=" not in cleaned
    assert "abc.DEF-123_456.xyz" not in cleaned
    assert "deadbeefcafebabe99" not in cleaned
    assert "short=1234abcd" in cleaned
    assert cleaned.count("[redacted]") == 3
    assert redact("") == ""


def test_env_cookie_reads_env_only_and_wraps_bare_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POE_COOKIE", raising=False)
    monkeypatch.delenv("POESESSID", raising=False)
    assert _env_cookie() is None

    monkeypatch.setenv("POESESSID", "  abcdef0123456789  ")
    assert _env_cookie() == "POESESSID=abcdef0123456789"

    monkeypatch.setenv("POE_COOKIE", "POESESSID=aaa; cf_clearance=bbb")
    assert _env_cookie() == "POESESSID=aaa; cf_clearance=bbb"


# ---------------------------------------------------------------------------
# Proxy Resolution
# ---------------------------------------------------------------------------


def test_proxy_resolution_honors_env_and_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    for key in (
        "POB_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("POB_PROXY", "http://127.0.0.1:7891")
    client_from_env = TradeClient(cache_dir=tmp_path)
    assert client_from_env.proxy == "http://127.0.0.1:7891"

    client_explicit = TradeClient(
        proxy="socks5://127.0.0.1:1080",
        cache_dir=tmp_path,
    )
    assert client_explicit.proxy == "socks5://127.0.0.1:1080"


def test_open_client_delegates_to_make_httpx_client_when_no_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("POB_PROXY", "http://127.0.0.1:7891")
    captured: dict[str, Any] = {}

    def fake_make_httpx_client(*, timeout: float, proxy: str | None, headers: dict[str, str]):
        captured["timeout"] = timeout
        captured["proxy"] = proxy
        captured["headers"] = headers
        return httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"result": [{"id": "Keepers"}]})
            ),
            headers=headers,
        )

    monkeypatch.setattr(trade_client, "_make_httpx_client", fake_make_httpx_client)
    client = TradeClient(cache_dir=tmp_path, timeout=12.5)
    leagues = client.leagues()
    assert leagues == [{"id": "Keepers"}]
    assert captured["proxy"] == "http://127.0.0.1:7891"
    assert captured["timeout"] == 12.5
    assert "Content-Type" not in captured["headers"]


# ---------------------------------------------------------------------------
# Request Headers (GET vs POST, Cookie)
# ---------------------------------------------------------------------------


def test_get_omits_content_type_and_post_includes_json_content_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("POE_COOKIE", raising=False)
    monkeypatch.delenv("POESESSID", raising=False)

    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"result": [{"id": "Keepers", "realm": "pc"}]})
        return httpx.Response(200, json={"id": "qid1", "result": ["item1"], "total": 1})

    client = TradeClient(
        cookie="0123456789abcdef0123456789abcdef",
        cache_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )
    client.leagues()
    client.search("Keepers", {"query": {"status": {"option": "online"}}})

    assert len(seen_requests) == 2
    get_req, post_req = seen_requests
    assert get_req.method == "GET"
    assert "content-type" not in get_req.headers
    assert get_req.headers["accept"] == "application/json"
    assert get_req.headers["cookie"] == "POESESSID=0123456789abcdef0123456789abcdef"

    assert post_req.method == "POST"
    assert post_req.headers["content-type"] == "application/json"
    assert post_req.headers["accept"] == "application/json"
    assert post_req.headers["cookie"] == "POESESSID=0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------------------
# Dynamic GGG Rate Limiter & Cross-Client Persistence
# ---------------------------------------------------------------------------


def test_first_request_has_zero_blind_sleep(tmp_path: Path):
    now, _ = _make_clock()
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "q1", "result": ["a1"], "total": 1},
            headers={
                "X-Rate-Limit-Policy": "trade-search-request-limit",
                "X-Rate-Limit-Rules": "Ip",
                "X-Rate-Limit-Ip": "8:10:60,15:60:120,60:300:1800",
                "X-Rate-Limit-Ip-State": "1:10:0,1:60:0,1:300:0",
            },
        )

    client = TradeClient(
        cache_dir=tmp_path,
        sleep=sleeps.append,
        now=now,
        transport=httpx.MockTransport(handler),
    )
    client.search("Keepers", {"query": {}})
    # Second request is still well below safety margin (cur=1 < 8-1=7).
    client.search("Keepers", {"query": {}})
    assert sleeps == []


def test_rate_limiter_safety_margin_waits_and_persists_across_clients(tmp_path: Path):
    now, advance = _make_clock(1_700_000_000.0)
    sleeps_a: list[float] = []
    sleeps_b: list[float] = []

    def handler_a(request: httpx.Request) -> httpx.Response:
        # 7 of 8 hits in 10s window hits effective limit (8 - 1 = 7).
        return httpx.Response(
            200,
            json={"id": "q1", "result": ["a1"], "total": 1},
            headers={
                "x-rate-limit-policy": "trade-search-request-limit",
                "x-rate-limit-rules": "Ip,Account",
                "x-rate-limit-ip": "8:10:60,15:60:120",
                "x-rate-limit-ip-state": "7:10:0,7:60:0",
                "x-rate-limit-account": "3:5:60",
                "x-rate-limit-account-state": "1:5:0",
            },
        )

    client_a = TradeClient(
        cache_dir=tmp_path,
        sleep=sleeps_a.append,
        now=now,
        transport=httpx.MockTransport(handler_a),
    )
    client_a.search("Keepers", {"query": {}})
    assert sleeps_a == []
    assert (tmp_path / "trade_ratelimit.json").is_file()

    # 2 seconds later, a brand-new TradeClient (simulating another CLI invocation)
    # reads trade_ratelimit.json and sleeps the remaining 8s in the 10s window.
    advance(2.0)

    def handler_b(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "q2", "result": ["a2"], "total": 1})

    client_b = TradeClient(
        cache_dir=tmp_path,
        sleep=sleeps_b.append,
        now=now,
        transport=httpx.MockTransport(handler_b),
    )
    client_b.search("Keepers", {"query": {}})
    assert len(sleeps_b) == 1
    assert sleeps_b[0] == pytest.approx(8.0)


def test_rate_limiter_raises_when_wait_exceeds_max_wait_sec(tmp_path: Path):
    now, advance = _make_clock(1_700_000_000.0)
    sleeps: list[float] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # 14 of 15 hits in 60s window => effective limit reached, needs 60s wait.
        return httpx.Response(
            200,
            json={"id": "q1", "result": ["a1"], "total": 1},
            headers={
                "X-Rate-Limit-Policy": "trade-search-request-limit",
                "X-Rate-Limit-Rules": "Ip",
                "X-Rate-Limit-Ip": "8:10:60,15:60:120",
                "X-Rate-Limit-Ip-State": "1:10:0,14:60:0",
            },
        )

    client = TradeClient(
        cache_dir=tmp_path,
        max_wait_sec=15.0,
        sleep=sleeps.append,
        now=now,
        transport=httpx.MockTransport(handler),
    )
    client.search("Keepers", {"query": {}})
    assert calls == 1

    advance(2.0)  # 58s remaining > 15.0s max_wait_sec
    with pytest.raises(RateLimited) as exc_info:
        client.search("Keepers", {"query": {}})

    assert exc_info.value.seconds == 58
    assert sleeps == []
    assert calls == 1  # Blocked before hitting network.

    # Once the 60s window expires, request proceeds with 0 sleep.
    advance(60.0)
    client.search("Keepers", {"query": {}})
    assert calls == 2
    assert sleeps == []


def test_rate_limiter_raises_immediately_on_active_penalty_timeout(tmp_path: Path):
    now, advance = _make_clock(1_700_000_000.0)
    sleeps: list[float] = []
    limiter = TradeRateLimiter(cache_dir=tmp_path, max_wait_sec=15.0, sleep=sleeps.append, now=now)
    limiter.record_response(
        "search",
        {
            "X-Rate-Limit-Policy": "trade-search-request-limit",
            "X-Rate-Limit-Rules": "Ip",
            "X-Rate-Limit-Ip": "8:10:60",
            "X-Rate-Limit-Ip-State": "8:10:10",
        },
    )
    advance(1.5)
    # Even though 8.5s <= max_wait_sec, active_timeout_sec > 0 must raise immediately.
    with pytest.raises(RateLimited) as exc_info:
        limiter.before_request("search")
    assert exc_info.value.seconds == 9
    assert sleeps == []


def test_429_persists_retry_after_for_immediate_retry(tmp_path: Path):
    now, advance = _make_clock(1_700_000_000.0)
    sleeps: list[float] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "45"})

    client_1 = TradeClient(
        cache_dir=tmp_path,
        max_wait_sec=15.0,
        sleep=sleeps.append,
        now=now,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RateLimited) as exc_1:
        client_1.search("Keepers", {"query": {}})
    assert exc_1.value.seconds == 45
    assert calls == 1

    # Verify persisted to disk before raising.
    persisted = json.loads((tmp_path / "trade_ratelimit.json").read_text(encoding="utf-8"))
    policy_state = persisted["policies"]["trade-search-request-limit"]
    assert policy_state["retry_after"] == pytest.approx(1_700_000_045.0)

    # Immediate retry from a fresh client respects persisted Retry-After without hitting network.
    advance(5.0)
    client_2 = TradeClient(
        cache_dir=tmp_path,
        max_wait_sec=15.0,
        sleep=sleeps.append,
        now=now,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RateLimited) as exc_2:
        client_2.search("Keepers", {"query": {}})
    assert exc_2.value.seconds == 40
    assert calls == 1
    assert sleeps == []

    # When remaining retry_after <= max_wait_sec, client sleeps instead of raising.
    advance(35.0)  # 5s remaining <= 15s
    ok_client = TradeClient(
        cache_dir=tmp_path,
        max_wait_sec=15.0,
        sleep=sleeps.append,
        now=now,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"id": "ok", "result": ["x1"], "total": 1})
        ),
    )
    res = ok_client.search("Keepers", {"query": {}})
    assert res["id"] == "ok"
    assert sleeps == [pytest.approx(5.0)]


def test_rate_limiter_ignores_oserror_on_readonly_cache_dir(tmp_path: Path):
    now, _ = _make_clock()
    limiter = TradeRateLimiter(cache_dir=tmp_path, now=now)
    with patch("pob_cli.trade_client.tempfile.mkstemp", side_effect=OSError("read-only")):
        limiter.record_response(
            "search",
            {
                "X-Rate-Limit-Policy": "trade-search-request-limit",
                "X-Rate-Limit-Rules": "Ip",
                "X-Rate-Limit-Ip": "8:10:60",
                "X-Rate-Limit-Ip-State": "7:10:0",
            },
        )
    # In-memory state still works even when disk persistence failed.
    sleeps: list[float] = []
    limiter._sleep = sleeps.append
    limiter.before_request("search")
    assert sleeps == [pytest.approx(10.0)]


# ---------------------------------------------------------------------------
# Error Mapping (401/403, 400, JSON error, NoResults)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [401, 403])
def test_cookie_required_on_401_and_403_redacts_detail(tmp_path: Path, status_code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text="Blocked POESESSID=0123456789abcdef0123456789abcdef cf_clearance=tok.123",
        )

    client = TradeClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(CookieRequired) as exc_info:
        client.leagues()
    assert exc_info.value.status == status_code
    assert "0123456789abcdef" not in exc_info.value.detail
    assert "tok.123" not in exc_info.value.detail
    assert "[redacted]" in exc_info.value.detail


def test_api_error_on_400_and_200_error_payload(tmp_path: Path):
    client_400 = TradeClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                400,
                json={"error": {"code": 2, "message": "Invalid query"}},
            )
        ),
    )
    with pytest.raises(ApiError) as exc_400:
        client_400.search("Keepers", {"query": {}})
    assert exc_400.value.code == 2
    assert exc_400.value.message == "Invalid query"

    client_200 = TradeClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"error": {"code": 6, "message": "Rate limit exceeded"}},
            )
        ),
    )
    with pytest.raises(ApiError) as exc_200:
        client_200.leagues()
    assert exc_200.value.code == 6
    assert exc_200.value.message == "Rate limit exceeded"


def test_search_raises_no_results_when_result_empty(tmp_path: Path):
    client = TradeClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"id": "empty1", "result": [], "total": 0})
        ),
    )
    with pytest.raises(NoResults):
        client.search("Keepers", {"query": {}})


def test_http_errors_wrapped_as_trade_error_with_redaction(tmp_path: Path):
    def fail_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed with POESESSID=deadbeef0123456789abcdef")

    client = TradeClient(cache_dir=tmp_path, transport=httpx.MockTransport(fail_handler))
    with pytest.raises(TradeError) as exc_info:
        client.leagues()
    assert "deadbeef0123456789abcdef" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Stats Disk Cache, Leagues, Fetch & Fetch Many
# ---------------------------------------------------------------------------


def test_stats_disk_cache_hit_force_refresh_and_expiry(tmp_path: Path):
    now, advance = _make_clock(1_700_000_000.0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"result": [{"label": "Explicit", "entries": [{"id": f"stat_{calls}"}]}]},
        )

    client = TradeClient(
        cache_dir=tmp_path,
        now=now,
        transport=httpx.MockTransport(handler),
    )
    first = client.stats()
    assert calls == 1
    assert first[0]["entries"][0]["id"] == "stat_1"
    assert (tmp_path / "trade_stats.json").is_file()

    # Second call uses disk cache without hitting network.
    second = client.stats()
    assert calls == 1
    assert second == first

    # force_refresh=True bypasses cache and overwrites trade_stats.json.
    refreshed = client.stats(force_refresh=True)
    assert calls == 2
    assert refreshed[0]["entries"][0]["id"] == "stat_2"

    # After >7 days, cache expires and fetches fresh stats.
    advance(7 * 24 * 3600 + 10)
    expired = client.stats()
    assert calls == 3
    assert expired[0]["entries"][0]["id"] == "stat_3"


def test_default_league_skips_hc_ssf_ruthless_private_standard(tmp_path: Path):
    leagues_payload = [
        {"id": "Standard", "realm": "pc"},
        {"id": "Hardcore", "realm": "pc"},
        {"id": "Hardcore Keepers", "realm": "pc"},
        {"id": "HC Keepers", "realm": "pc"},
        {"id": "SSF Keepers", "realm": "pc"},
        {"id": "Ruthless Keepers", "realm": "pc"},
        {"id": "Private League 1", "realm": "pc"},
        {"id": "Keepers Console", "realm": "sony"},
        {"id": "Keepers", "realm": "pc"},
    ]
    client = TradeClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"result": leagues_payload})
        ),
    )
    assert client.default_league() == "Keepers"
    assert (
        client.search_url("Keepers of the Flame", "abc123")
        == "https://www.pathofexile.com/trade/search/Keepers%20of%20the%20Flame/abc123"
    )


def test_fetch_many_chunks_batches_of_10_and_caps_at_20(tmp_path: Path):
    seen_batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "content-type" not in request.headers
        assert request.url.params.get("query") == "q99"
        ids_part = request.url.path.split("/api/trade/fetch/")[1]
        batch = ids_part.split(",")
        seen_batches.append(batch)
        return httpx.Response(200, json={"result": [{"id": item_id} for item_id in batch]})

    client = TradeClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))

    # 15 IDs with limit=15 -> 10 + 5.
    ids_15 = [f"id{i}" for i in range(15)]
    rows = client.fetch_many(ids_15, "q99", limit=15)
    assert [len(b) for b in seen_batches] == [10, 5]
    assert [r["id"] for r in rows] == ids_15

    # 25 IDs with limit=25 -> capped to 20 -> 10 + 10.
    seen_batches.clear()
    ids_25 = [f"id{i}" for i in range(25)]
    rows_capped = client.fetch_many(ids_25, "q99", limit=25)
    assert [len(b) for b in seen_batches] == [10, 10]
    assert len(rows_capped) == 20

    # fetch() directly rejects >10 IDs.
    with pytest.raises(ValueError):
        client.fetch(ids_15, "q99")


def test_trade_client_reuses_single_httpx_client_and_closes_via_context_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    opened_clients: list[httpx.Client] = []
    real_open = TradeClient._open_client

    def spy_open(self: TradeClient, req_headers: dict[str, str]) -> httpx.Client:
        c = real_open(self, req_headers)
        opened_clients.append(c)
        return c

    monkeypatch.setattr(TradeClient, "_open_client", spy_open)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "q1", "result": ["i1"], "total": 1})
        if "/api/trade/fetch/" in request.url.path:
            return httpx.Response(200, json={"result": [{"id": "i1"}]})
        return httpx.Response(200, json={"result": [{"id": "Covens", "realm": "pc"}]})

    with TradeClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler)) as client:
        client.leagues()
        client.search("Covens", {"query": {}})
        client.fetch(["i1"], "q1")
        assert len(opened_clients) == 1
        assert not opened_clients[0].is_closed

    assert opened_clients[0].is_closed


def test_trade_client_shares_ratelimit_and_stats_cache_across_runtime_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    shared_cache = tmp_path / "shared_cache"
    monkeypatch.setenv("POB_CACHE_DIR", str(shared_cache))
    now, _ = _make_clock(1_700_000_000.0)

    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "swarm_1"))
    c1 = TradeClient(
        now=now,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"result": [{"label": "Explicit", "entries": [{"id": "stat_shared"}]}]},
                headers={
                    "X-Rate-Limit-Policy": "trade-data-request-limit",
                    "X-Rate-Limit-Rules": "Ip",
                    "X-Rate-Limit-Ip": "5:10:60",
                    "X-Rate-Limit-Ip-State": "4:10:0",
                },
            )
        ),
    )
    assert c1.stats()[0]["entries"][0]["id"] == "stat_shared"
    assert (shared_cache / "trade_stats.json").is_file()
    assert (shared_cache / "trade_ratelimit.json").is_file()
    c1.close()

    # Worker 2 with a different POB_RUNTIME_DIR sees the shared stats cache & rate limit!
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "swarm_2"))
    sleeps: list[float] = []
    c2 = TradeClient(
        now=now,
        sleep=sleeps.append,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"result": []})),
    )
    assert c2.cache_dir == shared_cache
    assert c2.stats()[0]["entries"][0]["id"] == "stat_shared"  # cache hit, 0 HTTP
    c2.leagues()  # hits shared rate limit (cur=4 >= 5-1=4 -> sleeps 10s)
    assert sleeps == [pytest.approx(10.0)]
    c2.close()


def test_rate_limiter_in_flight_reservation_prevents_concurrent_burst_and_merges_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify optimistic in-flight reservation blocks 2nd concurrent worker at cur=effective_limit-1,
    timestamps union across workers, retry_after is preserved, and _atomic_write_json retries PermissionError."""
    import os

    now, advance = _make_clock(1_700_000_000.0)
    sleeps_a: list[float] = []
    sleeps_b: list[float] = []

    lim_a = TradeRateLimiter(cache_dir=tmp_path, limit_margin=1, max_wait_sec=15.0, sleep=sleeps_a.append, now=now)
    lim_b = TradeRateLimiter(cache_dir=tmp_path, limit_margin=1, max_wait_sec=15.0, sleep=sleeps_b.append, now=now)

    # Seed state at 6/7 (max_hits=8, margin=1 -> effective_limit=7)
    lim_a.record_response(
        "search",
        {
            "X-Rate-Limit-Policy": "trade-search-request-limit",
            "X-Rate-Limit-Rules": "Ip",
            "X-Rate-Limit-Ip": "8:10:60",
            "X-Rate-Limit-Ip-State": "6:10:0",
        },
    )
    advance(1.0)

    # Worker A proceeds immediately (6 < 7) and reserves an optimistic in-flight hit
    assert lim_a.before_request("search") == 0.0
    assert sleeps_a == []
    assert not lim_a.lock_path.exists()

    # Worker B arrives while Worker A is still in-flight: sees 6 + 1 = 7 >= 7 -> must sleep 9.0s!
    waited = lim_b.before_request("search")
    assert waited == pytest.approx(9.0)
    assert sleeps_b == [pytest.approx(9.0)]
    assert not lim_b.lock_path.exists()

    # Stale lockfile from dead PID is recovered cleanly
    lim_a.lock_path.write_text(json.dumps({"pid": 99999999, "token": "dead", "ts": 1.0}), encoding="utf-8")
    os.utime(lim_a.lock_path, (1.0, 1.0))
    lim_a.record_429("search", {"Retry-After": "30"})
    assert not lim_a.lock_path.exists()

    # Worker B updating a different policy ('fetch') must NOT erase Worker A's 'search' 429 Retry-After
    lim_b.record_response(
        "fetch",
        {
            "X-Rate-Limit-Policy": "trade-fetch-request-limit",
            "X-Rate-Limit-Rules": "Ip",
            "X-Rate-Limit-Ip": "10:10:60",
            "X-Rate-Limit-Ip-State": "1:10:0",
        },
    )
    persisted = json.loads(lim_a.state_path.read_text(encoding="utf-8"))
    assert "trade-search-request-limit" in persisted["policies"]
    assert persisted["policies"]["trade-search-request-limit"]["retry_after"] == pytest.approx(1_700_000_031.0)
    assert "trade-fetch-request-limit" in persisted["policies"]

    # _atomic_write_json retries PermissionError up to 3 attempts
    real_replace = os.replace
    attempts = {"n": 0}

    def flaky_replace(src, dst):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("busy")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    target = tmp_path / "retry_test.json"
    trade_client._atomic_write_json(target, {"ok": True})
    assert attempts["n"] == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_non_json_response_does_not_chain_json_decode_error(tmp_path: Path):
    client = TradeClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text="<html>POESESSID=0123456789abcdef0123456789abcdef</html>")
        ),
    )
    with pytest.raises(TradeError, match="non-JSON") as exc_info:
        client.leagues()
    assert exc_info.value.__cause__ is None
    assert "0123456789abcdef" not in str(exc_info.value)


def test_rate_limiter_records_each_request_once_so_the_wait_ends_with_the_oldest_hit(tmp_path: Path):
    """5 per 10 s with margin 1: after 4 searches a second apart, the 5th waits for the first to expire."""
    now, advance = _make_clock(1_700_000_000.0)
    sleeps: list[float] = []
    limiter = TradeRateLimiter(cache_dir=tmp_path, limit_margin=1, max_wait_sec=15.0, sleep=sleeps.append, now=now)

    for hits in (1, 2, 3, 4):
        assert limiter.before_request("search") == 0.0
        advance(0.3)  # the response arrives
        limiter.record_response(
            "search",
            {
                "X-Rate-Limit-Policy": "trade-search-request-limit",
                "X-Rate-Limit-Rules": "Ip",
                "X-Rate-Limit-Ip": "5:10:60",
                "X-Rate-Limit-Ip-State": f"{hits}:10:0",
            },
        )
        advance(0.7)

    # t = 4.0. The first hit was recorded at 0.3 and leaves the 10 s window at 10.3. Recording every
    # request twice (once reserved, once on its response) used to stretch this to 8.0 s.
    assert limiter.before_request("search") == pytest.approx(6.3)
    stamps = json.loads(limiter.state_path.read_text(encoding="utf-8"))["policies"][
        "trade-search-request-limit"
    ]["timestamps"]
    assert len(stamps) == 5
