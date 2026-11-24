"""Authoritative PoE1 crafting bench catalog and bilingual search.

Dual-source design:
- Primary (when local PoB engine is installed): reads `ModMaster.lua` records
  from the local `game_data` SQLite index (`source = 'Bench'`), guaranteeing
  100% authoritative GGPK Prefix/Suffix classification, exact PoB item types,
  bilingual zh-rCN translations, and automatic updates via `pob update`,
  overlaid with crafting costs and unlock locations from `data/bench_costs.json`.
- Fallback (zero-engine / CI / fresh install before `pob bootstrap`): loads the
  clean embedded snapshot inside `data/bench_costs.json`.
"""

from __future__ import annotations

from .. import game_data
from .loader import (
    _COSTS_FILE_CACHE as _COSTS_FILE_CACHE,
)
from .loader import (
    _DASH_RE as _DASH_RE,
)
from .loader import (
    _LEADING_TAG_RE as _LEADING_TAG_RE,
)
from .loader import (
    _NEG_RANGE_RE as _NEG_RANGE_RE,
)
from .loader import (
    _RANGE_RE as _RANGE_RE,
)
from .loader import (
    _RECIPES_CACHE as _RECIPES_CACHE,
)
from .loader import (
    _SLOT_ALIASES as _SLOT_ALIASES,
)
from .loader import (
    _TEMPLATE_NUM_RE as _TEMPLATE_NUM_RE,
)
from .loader import (
    _TEMPLATE_RANGE_RE as _TEMPLATE_RANGE_RE,
)
from .loader import (
    _VALUE_TOKEN_RE as _VALUE_TOKEN_RE,
)
from .loader import (
    DATA_FILE,
    POB_TYPE_TO_SLOTS,
    UNKNOWN_COST,
    BenchRecipe,
    BenchSearchResult,
    classes_to_slots,
    clear_cache,
    load_recipes,
    normalize_line,
    normalize_template,
    parse_values,
    slot_key,
    values_signature,
)
from .loader import (
    _build_recipe as _build_recipe,
)
from .loader import (
    _CostLookup as _CostLookup,
)
from .loader import (
    _fmt_num as _fmt_num,
)
from .loader import (
    _load_costs_payload as _load_costs_payload,
)
from .loader import (
    _load_from_modmaster as _load_from_modmaster,
)
from .loader import (
    _load_from_snapshot as _load_from_snapshot,
)
from .loader import (
    _normalize_affix as _normalize_affix,
)
from .loader import (
    _resolve_data_file as _resolve_data_file,
)
from .loader import (
    _resolve_range_in_line as _resolve_range_in_line,
)
from .search import (
    _CJK_CHAR_RE as _CJK_CHAR_RE,
)
from .search import (
    _EN_STOPWORDS as _EN_STOPWORDS,
)
from .search import (
    _EN_WORD_RE as _EN_WORD_RE,
)
from .search import (
    _ZH_NOISE_RE as _ZH_NOISE_RE,
)
from .search import (
    _clean_en_for_match as _clean_en_for_match,
)
from .search import (
    _clean_zh_for_match as _clean_zh_for_match,
)
from .search import (
    _en_tokens as _en_tokens,
)
from .search import (
    _is_subsequence as _is_subsequence,
)
from .search import (
    _score_chinese as _score_chinese,
)
from .search import (
    _score_english as _score_english,
)
from .search import (
    _tier_strength as _tier_strength,
)
from .search import (
    best,
    can_craft_on_item,
    format_recipe,
    list_for_slot,
    recipes_for_slot,
    search,
    source_metadata,
)

__all__ = [
    "DATA_FILE",
    "POB_TYPE_TO_SLOTS",
    "UNKNOWN_COST",
    "BenchRecipe",
    "BenchSearchResult",
    "best",
    "can_craft_on_item",
    "classes_to_slots",
    "clear_cache",
    "format_recipe",
    "game_data",
    "list_for_slot",
    "load_recipes",
    "normalize_line",
    "normalize_template",
    "parse_values",
    "recipes_for_slot",
    "search",
    "slot_key",
    "source_metadata",
    "values_signature",
]
