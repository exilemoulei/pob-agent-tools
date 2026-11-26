"""Parse, rebuild, inspect, and Harvest-convert PoE1 3.30 trade/PoB item text.

Two sources are normalised into the same :class:`Item` / :class:`Mod` model:

* PoB clipboard text (``pob items --slot X --raw``), including ``{crafted}``,
  ``{fractured}``, ``{enchant}``, ``{scourge}``, ``{mutated}``, ``{crucible}``,
  and the ``Implicits: N`` block.
* Official PoE1 trade API ``/api/trade/fetch`` payloads (both 3.30 object
  ``explicitMods[].{description, flags, mods}`` and legacy string arrays +
  ``extended.mods.explicit``).

Harvest conversion implements PoE1 3.30 single-element same-tier swap rules
(Resistance and Elemental Damage) with strict legality and same-group conflict
guards.  Harvest preserves the modifier **tier** but re-rolls its numeric value
within that tier's range, so callers must always surface the re-roll caveat and
re-evaluate the resulting item in PoB.
"""

from __future__ import annotations

from .convert import (
    _fmt_roll as _fmt_roll,
)
from .convert import (
    _resolve_tier_info as _resolve_tier_info,
)
from .convert import (
    _rewrite_element as _rewrite_element,
)
from .convert import (
    conversion_summary,
    convert_item_text,
)
from .harvest import (
    _HARVEST_PATTERNS as _HARVEST_PATTERNS,
)
from .harvest import (
    ELEMENT_LABELS,
    ELEMENTAL_DAMAGE,
    ELEMENTS,
    HARVEST_GROUPS,
    RESISTANCE,
    HarvestGroup,
    HarvestMatch,
    conversion_note,
    convert_template,
    is_convertible_template,
    match_harvest,
)
from .models import (
    _GGG_LINK_PIPE_RE as _GGG_LINK_PIPE_RE,
)
from .models import (
    _GGG_LINK_PLAIN_RE as _GGG_LINK_PLAIN_RE,
)
from .models import (
    _GGG_TAG_RE as _GGG_TAG_RE,
)
from .models import (
    CATEGORY_RULES,
    CONTROL_RE,
    FRAME_RARITY,
    INFLUENCE_API_MAP,
    INFLUENCE_LINE_MAP,
    MOD_KIND_TAGS,
    NEGATIVE_KEYWORD_RE,
    NEGATIVE_KEYWORDS,
    NON_CONVERTIBLE_KINDS,
    NUMBER_RE,
    POB_TAG_RE,
    POSITIVE_REDUCED_PATTERNS,
    PROPERTY_KEYS,
    PROPERTY_RE,
    RANGE_RE,
    SEPARATOR_RE,
    SIMPLE_TAG_RE,
    SPACE_RE,
    STATE_LINES,
    TRAILING_KIND_RE,
    Conversion,
    ConvertedItem,
    Item,
    Mod,
    classify_mod,
    escape_ggg_string,
    normalize_template,
    sanitize_text,
)
from .models import (
    _is_positive_reduced as _is_positive_reduced,
)
from .parser import (
    _extract_mod_lines as _extract_mod_lines,
)
from .parser import (
    _extract_tier_meta as _extract_tier_meta,
)
from .parser import (
    _first_int as _first_int,
)
from .parser import (
    _format_property_line as _format_property_line,
)
from .parser import (
    _format_sockets as _format_sockets,
)
from .parser import (
    _is_meta_or_property_line as _is_meta_or_property_line,
)
from .parser import (
    _is_property_line as _is_property_line,
)
from .parser import (
    _kind_from as _kind_from,
)
from .parser import (
    _to_float as _to_float,
)
from .parser import (
    _to_int as _to_int,
)
from .parser import (
    _values_fit_mod as _values_fit_mod,
)
from .parser import (
    build_item_text,
    make_mod,
    mods_by_template,
    parse_item_text,
    trade_mod_tiers,
)
from .tw_reverse import (
    build_english_item_text,
    clear_tw_reverse_cache,
    decode_extended_item_text,
    has_cjk,
    reverse_tw_item_text,
    trade_mod_tiers_english,
)

__all__ = [
    "CATEGORY_RULES",
    "CONTROL_RE",
    "ELEMENTAL_DAMAGE",
    "ELEMENTS",
    "ELEMENT_LABELS",
    "FRAME_RARITY",
    "HARVEST_GROUPS",
    "INFLUENCE_API_MAP",
    "INFLUENCE_LINE_MAP",
    "MOD_KIND_TAGS",
    "NEGATIVE_KEYWORDS",
    "NEGATIVE_KEYWORD_RE",
    "NON_CONVERTIBLE_KINDS",
    "NUMBER_RE",
    "POB_TAG_RE",
    "POSITIVE_REDUCED_PATTERNS",
    "PROPERTY_KEYS",
    "PROPERTY_RE",
    "RANGE_RE",
    "RESISTANCE",
    "SEPARATOR_RE",
    "SIMPLE_TAG_RE",
    "SPACE_RE",
    "STATE_LINES",
    "TRAILING_KIND_RE",
    "Conversion",
    "ConvertedItem",
    "HarvestGroup",
    "HarvestMatch",
    "Item",
    "Mod",
    "build_english_item_text",
    "build_item_text",
    "classify_mod",
    "clear_tw_reverse_cache",
    "conversion_note",
    "conversion_summary",
    "convert_item_text",
    "convert_template",
    "decode_extended_item_text",
    "escape_ggg_string",
    "has_cjk",
    "is_convertible_template",
    "make_mod",
    "match_harvest",
    "mods_by_template",
    "normalize_template",
    "parse_item_text",
    "reverse_tw_item_text",
    "sanitize_text",
    "trade_mod_tiers",
    "trade_mod_tiers_english",
]
