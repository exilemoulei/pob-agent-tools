"""Minimal client for the official Path of Exile 1 trade API.

Only the endpoints needed by the upgrade workflow are implemented:

* ``GET  /api/trade/data/leagues``
* ``GET  /api/trade/data/stats``
* ``POST /api/trade/search/{league}``
* ``GET  /api/trade/fetch/{ids}?query={id}``

The API is rate limited.  The client keeps a conservative gap between requests,
surfaces ``Retry-After`` instead of hammering, and never prints the cookie.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

import httpx

from .importers import USER_AGENT, _get_cache_dir

BASE_URL = "https://www.pathofexile.com"
STATS_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_SEARCH_INTERVAL = 6.0
DEFAULT_FETCH_INTERVAL = 2.0
DEFAULT_DATA_INTERVAL = 1.0
MAX_FETCH_IDS = 10

_HEX_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")

# Categories that are never the current softcore trade league.
_SKIP_LEAGUE_PREFIXES = ("Hardcore", "HC ", "Ruthless", "SSF", "Private")


class TradeError(RuntimeError):
    """Base class for every trade API failure (message is safe to show)."""


class CookieRequired(TradeError):
    def __init__(self, status: int, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        message = (
            f"Trade API returned HTTP {status}: a browser session / Cloudflare "
            "check is required.\n"
            "Set POESESSID (or a full POE_COOKIE) in the environment, then retry. "
            "Never pass the cookie on the command line or paste it into a report."
        )
        if detail:
            message += f"\nServer response: {detail[:300]}"
        super().__init__(message)


class RateLimited(TradeError):
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        super().__init__(
            f"Trade API rate limit hit. Wait about {seconds}s before retrying; "
            "do not hammer the endpoint."
        )


class ApiError(TradeError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Trade API error {code}: {message}")


class NoResults(TradeError):
    def __init__(self) -> None:
        super().__init__("No matching online listings; loosen the budget or the stat filters.")


def redact(text: str) -> str:
    """Remove session-like hex tokens from text that may be shown to a user."""
    return _HEX_TOKEN_RE.sub("[redacted]", text or "")


def _env_cookie() -> str | None:
    source = os.environ.get("POE_COOKIE") or os.environ.get("POESESSID")
    if not source:
        return None
    value = source.strip()
    if "=" in value:
        return value
    return f"POESESSID={value}"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _snippet(response: httpx.Response, limit: int = 300) -> str:
    return redact(" ".join(response.text[:limit].split()))


class TradeClient:
    """Thin, testable wrapper around the PoE1 trade endpoints."""

    def __init__(
        self,
        cookie: str | None = None,
        user_agent: str | None = None,
        timeout: float = 30.0,
        search_interval: float = DEFAULT_SEARCH_INTERVAL,
        fetch_interval: float = DEFAULT_FETCH_INTERVAL,
        data_interval: float = DEFAULT_DATA_INTERVAL,
        cache_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        logger: Callable[[str], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cookie = cookie if cookie is not None else _env_cookie()
        self.user_agent = user_agent or os.environ.get("POE_TRADE_USER_AGENT") or USER_AGENT
        self.timeout = timeout
        self.transport = transport
        self.intervals = {
            "search": max(0.0, search_interval),
            "fetch": max(0.0, fetch_interval),
            "data": max(0.0, data_interval),
        }
        self._last_request = {"search": 0.0, "fetch": 0.0, "data": 0.0}
        self._sleep = sleep
        self._clock = clock
        self.cache_dir = Path(cache_dir) if cache_dir else _get_cache_dir()
        self.logger = logger

    # -- low level ------------------------------------------------------

    def _throttle(self, policy: str) -> None:
        interval = self.intervals.get(policy, 1.0)
        elapsed = self._clock() - self._last_request.get(policy, 0.0)
        if interval > 0 and elapsed < interval:
            self._sleep(interval - elapsed)
        self._last_request[policy] = self._clock()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

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

        self._throttle(policy)
        if self.logger:
            self.logger(f"[trade] {method} {path}")

        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                transport=self.transport,
            ) as client:
                response = client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=body,
                )
        except httpx.TimeoutException:
            raise TradeError("Trade API request timed out; check the network or proxy.") from None
        except httpx.HTTPError as error:
            raise TradeError(f"Could not reach the trade API: {redact(str(error))}") from None

        status = response.status_code
        if status == 429:
            raise RateLimited(_as_int(response.headers.get("Retry-After"), 60)) from None
        if status in (401, 403):
            raise CookieRequired(status, _snippet(response)) from None
        if status == 400:
            raise _api_error_from_response(response) from None
        if status >= 400:
            raise TradeError(f"Trade API failed: HTTP {status} {_snippet(response)}") from None

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise TradeError(f"Trade API returned non-JSON (HTTP {status})") from error

        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            raise ApiError(int(error.get("code") or 0), str(error.get("message") or "unknown error"))
        return payload

    # -- reference data -------------------------------------------------

    def leagues(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/trade/data/leagues")
        return list(payload.get("result") or [])

    def default_league(self) -> str:
        try:
            leagues = self.leagues()
        except TradeError:
            return "Standard"
        for league in leagues:
            league_id = str(league.get("id") or "")
            if str(league.get("realm") or "pc") != "pc":
                continue
            if not league_id or league_id.startswith(_SKIP_LEAGUE_PREFIXES):
                continue
            if league_id in {"Standard", "Hardcore"}:
                continue
            return league_id
        return "Standard"

    def stats(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        cache = self.cache_dir / "trade_stats.json"
        if not force_refresh:
            cached = _read_stats_cache(cache)
            if cached is not None:
                return cached

        payload = self._request("GET", "/api/trade/data/stats")
        groups = list(payload.get("result") or [])
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"fetchedAt": time.time(), "result": groups}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, cache)
        except OSError:
            pass
        return groups

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
        path = f"/api/trade/fetch/{','.join(ids)}"
        payload = self._request("GET", path, params={"query": query_id}, policy="fetch")
        return [row for row in (payload.get("result") or []) if row]

    def search_url(self, league: str, query_id: str) -> str:
        return f"https://www.pathofexile.com/trade/search/{_quote(league)}/{_quote(query_id)}"


def _quote(value: str) -> str:
    return quote(str(value), safe="")


def _api_error_from_response(response: httpx.Response) -> ApiError:
    try:
        payload = response.json()
        error = payload.get("error") or {}
        return ApiError(int(error.get("code") or 0), str(error.get("message") or "bad request"))
    except (ValueError, TypeError, AttributeError):
        return ApiError(0, _snippet(response))


def _read_stats_cache(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(payload.get("fetchedAt") or 0) > STATS_TTL_SECONDS:
            return None
        result = payload.get("result")
        return list(result) if isinstance(result, list) and result else None
    except (OSError, ValueError, TypeError):
        return None
