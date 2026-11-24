"""PoE1 trade stat catalog loader, bilingual/slot-aware stat resolver, and query builder."""

from __future__ import annotations

from ..updater import resolve_engine_root
from .aliases import (
    _ARMOUR_SLOTS as _ARMOUR_SLOTS,
)
from .aliases import (
    _BASE_ITEM_TYPE_TO_CATEGORY as _BASE_ITEM_TYPE_TO_CATEGORY,
)
from .aliases import (
    _BRACE_NUM_RE as _BRACE_NUM_RE,
)
from .aliases import (
    _CJK_RE as _CJK_RE,
)
from .aliases import (
    _SHIELD_SLOTS as _SHIELD_SLOTS,
)
from .aliases import (
    _SLOT_COMPACT_ALIASES as _SLOT_COMPACT_ALIASES,
)
from .aliases import (
    _WEAPON_BASE_KEYWORDS as _WEAPON_BASE_KEYWORDS,
)
from .aliases import (
    _WEAPON_SLOTS as _WEAPON_SLOTS,
)
from .aliases import (
    CATEGORY_BY_SLOT,
    INDEXED_WINDOWS,
    OPEN_AFFIX_ID,
    OPEN_PREFIX_ID,
    OPEN_SUFFIX_ID,
    POB_SLOT_BY_SLOT,
    STAT_TYPE_ORDER,
    VALID_STATUSES,
    ZH_STAT_ALIASES,
    category_for_slot,
    infer_weapon_category_from_base,
    pob_slot_for_slot,
    translate_stat_query,
)
from .aliases import (
    _normalize_slot as _normalize_slot,
)
from .planner import (
    _SUM_MIN_SUFFIX_RE as _SUM_MIN_SUFFIX_RE,
)
from .planner import (
    _WEIGHT_SUFFIX_RE as _WEIGHT_SUFFIX_RE,
)
from .planner import (
    ConversionPlan,
    StatPlan,
    StatRequest,
    WeightedGroup,
    WeightedStatEntry,
    build_query,
    parse_stat_argument,
    parse_sum_group_argument,
    plan_stats,
    resolve_requests,
)
from .planner import (
    _conversion_plan as _conversion_plan,
)
from .stats_index import (
    _LOCAL_STATS_LOCK as _LOCAL_STATS_LOCK,
)
from .stats_index import (
    _LOCAL_STATS_MEMO as _LOCAL_STATS_MEMO,
)
from .stats_index import (
    _normalize_remote_stats as _normalize_remote_stats,
)
from .stats_index import (
    _parse_trade_site_stats_lua as _parse_trade_site_stats_lua,
)
from .stats_index import (
    _unescape_lua_string as _unescape_lua_string,
)
from .stats_index import (
    flatten_stats,
    load_local_trade_stats,
    load_trade_stats,
)
from .stats_matcher import (
    _ENTRY_INDEX_CACHE as _ENTRY_INDEX_CACHE,
)
from .stats_matcher import (
    _ENTRY_INDEX_LOCK as _ENTRY_INDEX_LOCK,
)
from .stats_matcher import (
    _LEADING_PLACEHOLDER_RE as _LEADING_PLACEHOLDER_RE,
)
from .stats_matcher import (
    _MULTI_SPACE_RE as _MULTI_SPACE_RE,
)
from .stats_matcher import (
    _SCOPE_SUFFIX_RE as _SCOPE_SUFFIX_RE,
)
from .stats_matcher import (
    _SLOW_NORMALIZE_CHARS_RE as _SLOW_NORMALIZE_CHARS_RE,
)
from .stats_matcher import (
    _STOPWORDS as _STOPWORDS,
)
from .stats_matcher import (
    _TOKEN_RE as _TOKEN_RE,
)
from .stats_matcher import (
    _UNREQUESTED_MULTIPLIER_TOKENS as _UNREQUESTED_MULTIPLIER_TOKENS,
)
from .stats_matcher import (
    _WEAPON_FLAT_DMG_RE as _WEAPON_FLAT_DMG_RE,
)
from .stats_matcher import (
    StatMatch,
    resolve_stat,
    suggest_stats,
)
from .stats_matcher import (
    _base_similarity as _base_similarity,
)
from .stats_matcher import (
    _EntryIndex as _EntryIndex,
)
from .stats_matcher import (
    _extract_tokens as _extract_tokens,
)
from .stats_matcher import (
    _fast_normalize_stat_text as _fast_normalize_stat_text,
)
from .stats_matcher import (
    _get_entry_index as _get_entry_index,
)
from .stats_matcher import (
    _is_armour_local_template as _is_armour_local_template,
)
from .stats_matcher import (
    _is_shield_block_template as _is_shield_block_template,
)
from .stats_matcher import (
    _is_weapon_local_template as _is_weapon_local_template,
)
from .stats_matcher import (
    _meaningful_tokens as _meaningful_tokens,
)
from .stats_matcher import (
    _PreparedEntry as _PreparedEntry,
)
from .stats_matcher import (
    _scope_adjustment as _scope_adjustment,
)
from .stats_matcher import (
    _select_candidates as _select_candidates,
)
from .stats_matcher import (
    _split_scope_tag as _split_scope_tag,
)
from .stats_matcher import (
    _stat_priority as _stat_priority,
)
from .stats_matcher import (
    _strip_leading_placeholder as _strip_leading_placeholder,
)
from .stats_matcher import (
    _verb_penalty as _verb_penalty,
)

__all__ = [
    "CATEGORY_BY_SLOT",
    "INDEXED_WINDOWS",
    "OPEN_AFFIX_ID",
    "OPEN_PREFIX_ID",
    "OPEN_SUFFIX_ID",
    "POB_SLOT_BY_SLOT",
    "STAT_TYPE_ORDER",
    "VALID_STATUSES",
    "ZH_STAT_ALIASES",
    "ConversionPlan",
    "StatMatch",
    "StatPlan",
    "StatRequest",
    "WeightedGroup",
    "WeightedStatEntry",
    "build_query",
    "category_for_slot",
    "flatten_stats",
    "infer_weapon_category_from_base",
    "load_local_trade_stats",
    "load_trade_stats",
    "parse_stat_argument",
    "parse_sum_group_argument",
    "plan_stats",
    "pob_slot_for_slot",
    "resolve_engine_root",
    "resolve_requests",
    "resolve_stat",
    "suggest_stats",
    "translate_stat_query",
]
