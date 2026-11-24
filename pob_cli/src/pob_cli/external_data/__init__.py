"""External PoE 1 data queries: poe.ninja economy prices/meta (15m cache) and poewiki.net MediaWiki API (24h cache)."""

from __future__ import annotations

from ..importers import (
    _fetch_poe_ninja_build_index_state,
    _fetch_poe_ninja_index_state,
)
from .constants import (
    _AUTO_DETAIL_PATTERNS,
    _LAST_STALE,
    ALL_OVERVIEW_TYPES,
    CATEGORY_MAP,
    CURRENCY_TYPES,
    ITEM_TYPES,
    NINJA_DENSE_URL,
    NINJA_STASH_CURRENCY_URL,
    NINJA_STASH_ITEM_URL,
    POE_LEAGUES_URL,
)
from .meta_wiki import (
    _strip_html,
    query_meta,
    query_wiki,
    query_wiki_multi,
)
from .ninja_http import (
    _extract_challenge_league_from_index_state,
    _fetch_dense_overviews,
    _fetch_stash_overview,
    _fetch_stash_overviews_batch,
    _read_fresh_stash_cache,
    _stash_cache_path,
    fetch_currency_chaos_rates,
    resolve_current_trade_league,
)
from .ninja_price import (
    _build_searchable_fields,
    _extract_cluster_passives,
    _matches_text_or_tokens,
    _narrow_detailed_types,
    _resolve_types_to_search,
    _score_price_matches,
    _should_auto_detail,
    query_price,
    query_price_multi,
)

__all__ = [
    "ALL_OVERVIEW_TYPES",
    "CATEGORY_MAP",
    "CURRENCY_TYPES",
    "ITEM_TYPES",
    "NINJA_DENSE_URL",
    "NINJA_STASH_CURRENCY_URL",
    "NINJA_STASH_ITEM_URL",
    "POE_LEAGUES_URL",
    "_AUTO_DETAIL_PATTERNS",
    "_LAST_STALE",
    "_build_searchable_fields",
    "_extract_challenge_league_from_index_state",
    "_extract_cluster_passives",
    "_fetch_dense_overviews",
    "_fetch_poe_ninja_build_index_state",
    "_fetch_poe_ninja_index_state",
    "_fetch_stash_overview",
    "_fetch_stash_overviews_batch",
    "_matches_text_or_tokens",
    "_narrow_detailed_types",
    "_read_fresh_stash_cache",
    "_resolve_types_to_search",
    "_score_price_matches",
    "_should_auto_detail",
    "_stash_cache_path",
    "_strip_html",
    "fetch_currency_chaos_rates",
    "query_meta",
    "query_price",
    "query_price_multi",
    "query_wiki",
    "query_wiki_multi",
    "resolve_current_trade_league",
]
