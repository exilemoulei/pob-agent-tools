"""Shared constants, exceptions, redaction, atomic JSON I/O, and lockfile helpers for trade_client."""

from __future__ import annotations

import json
import math
import os
import re
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..json_io import atomic_write_json as _atomic_write_json  # noqa: F401  (re-exported)
from ..os_util import _is_lockfile_stale as _is_lockfile_stale_generic
from ..os_util import _is_pid_alive as _is_pid_alive
from ..os_util import _try_break_stale_lock as _try_break_stale_lockfile  # noqa: F401  (re-exported)

BASE_URL = "https://www.pathofexile.com"
STATS_TTL_SECONDS = 7 * 24 * 3600
MAX_FETCH_IDS = 10
MAX_FETCH_MANY = 20
DEFAULT_LIMIT_MARGIN = 1
DEFAULT_MAX_WAIT_SEC = 15.0
DEFAULT_RETRY_AFTER_SEC = 60
_MAX_TRACKED_TIMESTAMPS = 500

_POLICY_ALIASES: dict[str, str] = {
    "search": "trade-search-request-limit",
    "fetch": "trade-fetch-request-limit",
    "data": "trade-data-request-limit",
}

_COOKIE_SECRET_RE = re.compile(r"\b(?:POESESSID|cf_clearance)=[^;\s]+", re.IGNORECASE)
_HEX_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_RULE_STATE_HEADER_RE = re.compile(r"^x-rate-limit-([a-z0-9_-]+)-state$")

_SKIP_LEAGUE_RE = re.compile(
    r"\b(?:hardcore|hc|ruthless|ssf|private)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TradeError(RuntimeError):
    """Base class for every trade API failure (message is safe to show)."""


class CookieRequired(TradeError):
    """Raised on HTTP 401/403 when a PoE session cookie / CF clearance is needed."""

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = int(status)
        self.detail = redact(detail) if detail else ""
        message = (
            f"Trade API returned HTTP {self.status}: a browser session / Cloudflare "
            "check is required.\n"
            "Set POESESSID (or a full POE_COOKIE) in the environment, then retry. "
            "Never pass the cookie on the command line or paste it into a report."
        )
        if self.detail:
            message += f"\nServer response: {self.detail[:300]}"
        super().__init__(message)


class RateLimited(TradeError):
    """Raised when GGG rate-limit wait exceeds max_wait_sec or HTTP 429 is returned."""

    def __init__(self, seconds: int) -> None:
        self.seconds = int(seconds)
        super().__init__(
            f"Trade API rate limit hit. Wait about {self.seconds}s before retrying; "
            "do not hammer the endpoint."
        )


class ApiError(TradeError):
    """Raised on HTTP 400 or JSON payload containing an ``error`` object."""

    def __init__(self, code: int, message: str) -> None:
        self.code = int(code)
        self.message = str(message)
        super().__init__(f"Trade API error {self.code}: {self.message}")


class NoResults(TradeError):
    """Raised when a trade search succeeds with zero matching listings."""

    def __init__(self) -> None:
        super().__init__("No matching online listings; loosen the budget or the stat filters.")


# ---------------------------------------------------------------------------
# Redaction, env-only cookie, atomic write, and cross-process lockfile helpers
# ---------------------------------------------------------------------------


def redact(text: str) -> str:
    """Remove cookie secrets and >=16-char hex tokens from user-facing text."""
    if not text:
        return ""
    cleaned = _COOKIE_SECRET_RE.sub("[redacted]", text)
    return _HEX_TOKEN_RE.sub("[redacted]", cleaned)


def _normalize_cookie(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if "=" in value:
        return value
    return f"POESESSID={value}"


def _env_cookie() -> str | None:
    """Read session cookie strictly from ``POE_COOKIE`` or ``POESESSID`` env vars."""
    for env_key in ("POE_COOKIE", "POESESSID"):
        normalized = _normalize_cookie(os.environ.get(env_key))
        if normalized:
            return normalized
    return None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_retry_after(value: str | None, now: float, default: int | None = None) -> int | None:
    if value is None:
        return default
    raw = str(value).strip()
    if not raw:
        return default
    try:
        return max(1, math.ceil(float(raw)))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        delta = dt.timestamp() - now
        return max(1, math.ceil(delta))
    except (TypeError, ValueError, OverflowError):
        return default


def _snippet(response: httpx.Response, limit: int = 300) -> str:
    return redact(" ".join(response.text[:limit].split()))


def _quote(value: str) -> str:
    return quote(str(value), safe="")


def _is_lockfile_stale(lock_path: Path, *, stale_sec: float = 15.0) -> bool:
    """Trade rate-limit lock variant (15s default); logic lives in os_util (L0)."""
    return _is_lockfile_stale_generic(lock_path, stale_sec=stale_sec)


def _release_lockfile(lock_path: Path, owner_token: str) -> None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("token") != owner_token:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _parse_buckets(header_value: str | None) -> dict[int, tuple[int, int, int]]:
    """Parse ``hits:period_sec:timeout_sec,...`` into ``{period_sec: (hits, period, timeout)}``."""
    buckets: dict[int, tuple[int, int, int]] = {}
    if not header_value:
        return buckets
    for part in header_value.split(","):
        piece = part.strip()
        if not piece:
            continue
        fields = [f.strip() for f in piece.split(":")]
        if len(fields) != 3:
            continue
        try:
            hits = int(float(fields[0]))
            period = int(float(fields[1]))
            timeout = int(float(fields[2]))
        except ValueError:
            continue
        if period <= 0:
            continue
        buckets[period] = (hits, period, timeout)
    return buckets
