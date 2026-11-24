"""Compare PoE1 trade candidates against an equipped item, verify via PoB, rank, and report.

No DPS or EHP is ever estimated in Python (R5). Template scoring only summarizes
numeric modifier deltas and asking price; real DPS/EHP comes exclusively from the
local headless PoB daemon (`trade_eval_items` batch RPC with `item_try` fallback)
and is rendered as `-` when unverified.
"""

from __future__ import annotations

from .evaluator import (
    _COMPLEX_TRADE_RAW_RE as _COMPLEX_TRADE_RAW_RE,
)
from .evaluator import (
    _PARTNER_WEAPON_SLOT as _PARTNER_WEAPON_SLOT,
)
from .evaluator import (
    _TWO_HANDED_ITEM_TYPES as _TWO_HANDED_ITEM_TYPES,
)
from .evaluator import (
    _TWO_HANDED_RAW_RE as _TWO_HANDED_RAW_RE,
)
from .evaluator import (
    _WEAPON_SLOTS as _WEAPON_SLOTS,
)
from .evaluator import (
    _is_complex_trade_item as _is_complex_trade_item,
)
from .evaluator import (
    _pob_eval_from_snapshot,
    eval_trade_items,
    verify_candidate,
    verify_candidates_batch,
)
from .report import (
    _BENCH_RANGE_RE as _BENCH_RANGE_RE,
)
from .report import (
    _DASH_RE as _DASH_RE,
)
from .report import (
    _build_crafted_min_line as _build_crafted_min_line,
)
from .report import (
    _escape as _escape,
)
from .report import (
    _format_delta as _format_delta,
)
from .report import (
    format_report,
    report_json,
    sort_candidates,
)
from .scoring import (
    DEFAULT_WEIGHTS,
    POB_EHP_WEIGHT,
    PREFERRED_BONUS,
    Candidate,
    ModDelta,
    PobEval,
    _safe_pct,
    compare_items,
    deltas_for_example,
    listing_age_hours,
    parse_weights,
    preferred_templates,
    score_deltas,
)
from .scoring import (
    _finite_float as _finite_float,
)
from .scoring import (
    _finite_sort_key as _finite_sort_key,
)
from .scoring import (
    _is_pos_inf as _is_pos_inf,
)
from .scoring import (
    _json_safe_optional_float as _json_safe_optional_float,
)
from .scoring import (
    _json_safe_stat as _json_safe_stat,
)
from .scoring import (
    _matches_preferred as _matches_preferred,
)

__all__ = [
    "DEFAULT_WEIGHTS",
    "POB_EHP_WEIGHT",
    "PREFERRED_BONUS",
    "Candidate",
    "ModDelta",
    "PobEval",
    "_pob_eval_from_snapshot",
    "_safe_pct",
    "compare_items",
    "deltas_for_example",
    "eval_trade_items",
    "format_report",
    "listing_age_hours",
    "parse_weights",
    "preferred_templates",
    "report_json",
    "score_deltas",
    "sort_candidates",
    "verify_candidate",
    "verify_candidates_batch",
]
