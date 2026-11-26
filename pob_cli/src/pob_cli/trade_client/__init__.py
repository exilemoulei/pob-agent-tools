"""Client for the official Path of Exile 1 Trade API with dynamic GGG rate limiting.

Implements the endpoints needed for upgrade search and market lookup:

* ``GET  /api/trade/data/leagues``
* ``GET  /api/trade/data/stats`` (7-day atomic disk cache)
* ``POST /api/trade/search/{league}``
* ``GET  /api/trade/fetch/{ids}?query={query_id}``

Design contracts:
* Env-only cookie handling (``POE_COOKIE`` / ``POESESSID``); never read from CLI
  flags, never logged, and always redacted from errors.
* Dynamic GGG ``X-Rate-Limit-*`` header parsing (matching PoB's
  ``TradeQueryRateLimiter.lua``) with atomic cross-process persistence in
  ``<cache_dir>/trade_ratelimit.json`` and 0ms sleep on the first request.
* Full proxy support via ``importers._make_httpx_client`` / ``_resolve_proxy``
  (explicit ``proxy``, ``POB_PROXY``, ``HTTPS_PROXY``, ``HTTP_PROXY``,
  ``ALL_PROXY``, including SOCKS5).
"""

from __future__ import annotations

import tempfile

from ..importers import _make_httpx_client
from .client import (
    TradeClient,
    seller_url,
)
from .client import (
    _api_error_from_response as _api_error_from_response,
)
from .client import (
    _read_stats_cache as _read_stats_cache,
)
from .ratelimit import TradeRateLimiter
from .tw_names import (
    clear_tw_name_cache,
    localize_query_for_tw,
    to_tw_trade_name,
    unmapped_tw_ascii_fields,
)
from .utils import (
    _COOKIE_SECRET_RE as _COOKIE_SECRET_RE,
)
from .utils import (
    _HEX_TOKEN_RE as _HEX_TOKEN_RE,
)
from .utils import (
    _MAX_TRACKED_TIMESTAMPS as _MAX_TRACKED_TIMESTAMPS,
)
from .utils import (
    _POLICY_ALIASES as _POLICY_ALIASES,
)
from .utils import (
    _RULE_STATE_HEADER_RE as _RULE_STATE_HEADER_RE,
)
from .utils import (
    _SKIP_LEAGUE_RE as _SKIP_LEAGUE_RE,
)
from .utils import (
    BASE_URL,
    DEFAULT_LIMIT_MARGIN,
    DEFAULT_MAX_WAIT_SEC,
    DEFAULT_RETRY_AFTER_SEC,
    MAX_FETCH_IDS,
    MAX_FETCH_MANY,
    STATS_TTL_SECONDS,
    TW_BASE_URL,
    TW_LEAGUE_ALIASES,
    ApiError,
    CookieRequired,
    NoResults,
    RateLimited,
    TradeError,
    _env_cookie,
    redact,
)
from .utils import (
    _as_int as _as_int,
)
from .utils import (
    _atomic_write_json as _atomic_write_json,
)
from .utils import (
    _is_lockfile_stale as _is_lockfile_stale,
)
from .utils import (
    _is_pid_alive as _is_pid_alive,
)
from .utils import (
    _normalize_cookie as _normalize_cookie,
)
from .utils import (
    _parse_buckets as _parse_buckets,
)
from .utils import (
    _parse_retry_after as _parse_retry_after,
)
from .utils import (
    _quote as _quote,
)
from .utils import (
    _release_lockfile as _release_lockfile,
)
from .utils import (
    _snippet as _snippet,
)
from .utils import (
    _try_break_stale_lockfile as _try_break_stale_lockfile,
)

__all__ = [
    "BASE_URL",
    "DEFAULT_LIMIT_MARGIN",
    "DEFAULT_MAX_WAIT_SEC",
    "DEFAULT_RETRY_AFTER_SEC",
    "MAX_FETCH_IDS",
    "MAX_FETCH_MANY",
    "STATS_TTL_SECONDS",
    "TW_BASE_URL",
    "TW_LEAGUE_ALIASES",
    "ApiError",
    "CookieRequired",
    "NoResults",
    "RateLimited",
    "TradeClient",
    "TradeError",
    "TradeRateLimiter",
    "_env_cookie",
    "_make_httpx_client",
    "clear_tw_name_cache",
    "localize_query_for_tw",
    "redact",
    "seller_url",
    "tempfile",
    "to_tw_trade_name",
    "unmapped_tw_ascii_fields",
]
