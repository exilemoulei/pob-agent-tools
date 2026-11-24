"""Official Path of Exile 1 Trade API HTTP client."""

from __future__ import annotations

import base64
import copy
import gzip
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from ..importers import (
    DEFAULT_HTTP_TIMEOUT,
    USER_AGENT,
    _resolve_proxy,
    _shared_cache_dir,
)
from .ratelimit import TradeRateLimiter
from .utils import (
    _SKIP_LEAGUE_RE,
    BASE_URL,
    DEFAULT_LIMIT_MARGIN,
    DEFAULT_MAX_WAIT_SEC,
    MAX_FETCH_IDS,
    MAX_FETCH_MANY,
    STATS_TTL_SECONDS,
    ApiError,
    CookieRequired,
    NoResults,
    RateLimited,
    TradeError,
    _as_int,
    _atomic_write_json,
    _env_cookie,
    _normalize_cookie,
    _quote,
    _snippet,
    redact,
)


class TradeClient:
    """Official Path of Exile 1 Trade API client with proxy & GGG rate-limit support."""

    def __init__(
        self,
        cookie: str | None = None,
        proxy: str | None = None,
        user_agent: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
        cache_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        logger: Callable[[str], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cookie = _normalize_cookie(cookie) if cookie is not None else _env_cookie()
        self.proxy = _resolve_proxy(proxy)
        self.user_agent = user_agent or os.environ.get("POE_TRADE_USER_AGENT") or USER_AGENT
        if timeout is None:
            self.timeout: float | httpx.Timeout = DEFAULT_HTTP_TIMEOUT
        elif isinstance(timeout, httpx.Timeout):
            self.timeout = timeout
        else:
            self.timeout = float(timeout)
        self.max_wait_sec = float(max_wait_sec)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else _shared_cache_dir()
        self._sleep = sleep
        self._now = now
        self.logger = logger
        self.transport = transport
        self._client: httpx.Client | None = None
        self.rate_limiter = TradeRateLimiter(
            cache_dir=self.cache_dir,
            limit_margin=DEFAULT_LIMIT_MARGIN,
            max_wait_sec=self.max_wait_sec,
            sleep=self._sleep,
            now=self._now,
        )

    def __enter__(self) -> TradeClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the reusable underlying httpx.Client if opened."""
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # -- low-level HTTP -------------------------------------------------

    def _headers(self, *, has_body: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _open_client(self, req_headers: dict[str, str]) -> httpx.Client:
        if self.transport is None:
            import pob_cli.trade_client as _tc

            return _tc._make_httpx_client(
                timeout=self.timeout,
                proxy=self.proxy,
                headers=req_headers,
            )
        return httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers=req_headers,
            transport=self.transport,
        )

    def _get_client(self) -> httpx.Client:
        if self._client is None or getattr(self._client, "is_closed", False):
            self._client = self._open_client(self._headers(has_body=False))
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        policy: str = "data",
    ) -> Any:
        url = BASE_URL + path
        if params:
            url += "?" + urlencode(params)

        self.rate_limiter.before_request(policy)
        if self.logger:
            self.logger(f"[trade] {method} {path}")

        client = self._get_client()
        try:
            if body is not None:
                response = client.request(
                    method,
                    url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
            else:
                response = client.request(method, url)
        except httpx.TimeoutException:
            raise TradeError("Trade API request timed out; check the network or proxy.") from None
        except httpx.HTTPError as error:
            raise TradeError(f"Could not reach the trade API: {redact(str(error))}") from None

        status = response.status_code
        if status == 429:
            retry_after_sec = self.rate_limiter.record_429(policy, response.headers)
            raise RateLimited(retry_after_sec) from None

        self.rate_limiter.record_response(policy, response.headers)

        if status in (401, 403):
            raise CookieRequired(status, _snippet(response)) from None
        if status == 400:
            raise _api_error_from_response(response) from None
        if status >= 400:
            raise TradeError(f"Trade API failed: HTTP {status} {_snippet(response)}") from None

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            raise TradeError(f"Trade API returned non-JSON (HTTP {status})") from None

        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            if isinstance(err, dict):
                raise ApiError(
                    _as_int(err.get("code"), 0),
                    redact(str(err.get("message") or "unknown error")),
                )
            raise ApiError(0, redact(str(err)))
        return payload

    # -- reference data -------------------------------------------------

    def leagues(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/trade/data/leagues", policy="data")
        return list(payload.get("result") or [])

    def default_league(self) -> str:
        try:
            leagues = self.leagues()
        except TradeError:
            return "Standard"
        for league in leagues:
            league_id = str(league.get("id") or "").strip()
            if str(league.get("realm") or "pc").lower() != "pc":
                continue
            if not league_id or league_id.lower() in {"standard", "hardcore"}:
                continue
            if _SKIP_LEAGUE_RE.search(league_id):
                continue
            return league_id
        return "Standard"

    def _cached_data(self, name: str, force_refresh: bool = False) -> list[dict[str, Any]]:
        cache_path = self.cache_dir / f"trade_{name}.json"
        now = float(self._now())
        if not force_refresh:
            cached = _read_stats_cache(cache_path, now)
            if cached is not None:
                return cached

        payload = self._request("GET", f"/api/trade/data/{name}", policy="data")
        groups = list(payload.get("result") or [])
        _atomic_write_json(cache_path, {"fetchedAt": now, "result": groups})
        return groups

    def stats(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        return self._cached_data("stats", force_refresh)

    def static(self) -> list[dict[str, Any]]:
        """Currency and other static item ids with their names (same 7-day cache as ``stats``)."""
        return self._cached_data("static")

    # -- search / fetch -------------------------------------------------

    def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
        path = f"/api/trade/search/{_quote(league)}"
        payload = self._request("POST", path, body=query, policy="search")
        if not payload.get("result"):
            raise NoResults()
        return payload

    def fetch(self, ids: list[str], query_id: str) -> list[dict[str, Any]]:
        if not ids:
            return []
        if len(ids) > MAX_FETCH_IDS:
            raise ValueError(f"fetch accepts at most {MAX_FETCH_IDS} ids at once")
        joined = ",".join(_quote(item_id) for item_id in ids)
        path = f"/api/trade/fetch/{joined}"
        payload = self._request("GET", path, params={"query": query_id}, policy="fetch")
        return [row for row in (payload.get("result") or []) if row]

    def fetch_many(
        self,
        ids: list[str],
        query_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not ids or limit <= 0:
            return []
        cap = min(int(limit), MAX_FETCH_MANY)
        target_ids = list(ids[:cap])
        results: list[dict[str, Any]] = []
        for start in range(0, len(target_ids), MAX_FETCH_IDS):
            chunk = target_ids[start : start + MAX_FETCH_IDS]
            results.extend(self.fetch(chunk, query_id))
        return results

    def search_url(self, league: str, query_id: str) -> str:
        return f"{BASE_URL}/trade/search/{_quote(league)}/{_quote(query_id)}"


def seller_url(league: str, query: dict[str, Any], account: str) -> str:
    """Search link narrowed to one seller, so the page shows only their matching listing(s).

    The site's search id is the gzip + urlsafe-base64 of the ``query`` JSON, so it is built
    locally and costs no request.
    """
    # shortcut: relies on the site accepting self-describing ids (checked against the ids the
    # API issues); if it returns to stored ids, issue one search per listing instead.
    narrowed = copy.deepcopy(query)
    trade_filters = narrowed.setdefault("filters", {}).setdefault("trade_filters", {})
    trade_filters.setdefault("filters", {})["account"] = {"input": account}
    blob = gzip.compress(json.dumps(narrowed, separators=(",", ":")).encode("utf-8"), mtime=0)
    query_id = base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
    return f"{BASE_URL}/trade/search/{_quote(league)}/{query_id}"


def _api_error_from_response(response: httpx.Response) -> ApiError:
    try:
        payload = response.json()
        error = payload.get("error") or {}
        if isinstance(error, dict):
            return ApiError(
                _as_int(error.get("code"), 0),
                redact(str(error.get("message") or "bad request")),
            )
        return ApiError(0, redact(str(error)))
    except (ValueError, TypeError, AttributeError):
        return ApiError(0, _snippet(response))


def _read_stats_cache(path: Path, now: float) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = float(payload.get("fetchedAt") or 0.0)
        if now - fetched_at > STATS_TTL_SECONDS:
            return None
        result = payload.get("result")
        return list(result) if isinstance(result, list) and result else None
    except (OSError, ValueError, TypeError):
        return None
