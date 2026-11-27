"""Trade stat polarity inversion (`reduced`/`less` and Chinese negative forms -> canonical `increased`/`more`)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..trade_item import normalize_template
from .aliases import _CJK_RE

# Only negative -> positive swaps are valid: native `reduced`/`less` stats in TradeSiteStats
# store positive numbers (`reduced Mana Cost>=10` -> min=10), so reversing `increased->reduced`
# or `more->less` into negative bounds would silently match zero listings.
POLARITY_SWAP_PAIRS: tuple[tuple[str, str], ...] = (
    ("reduced", "increased"),
    ("less", "more"),
)

_ZH_NEGATIVE_POLARITY_TOKENS: tuple[str, ...] = ("减少", "降低", "较少")
_CANONICAL_POSITIVE_TOKENS: frozenset[str] = frozenset({"increased", "more"})
_CANONICAL_NEGATIVE_TOKENS: frozenset[str] = frozenset({"reduced", "less"})


def has_zh_negative_polarity(raw_text: str) -> bool:
    """Return True when raw Chinese stat text expresses a negative polarity (`减少`/`降低`/`较少`)."""
    if not raw_text or not _CJK_RE.search(raw_text):
        return False
    from ..game_data import fold_han

    folded = fold_han(raw_text)
    return any(tok in folded for tok in _ZH_NEGATIVE_POLARITY_TOKENS)


def is_zh_negative_to_positive_match(raw_text: str, matched_tokens: frozenset[str]) -> bool:
    """Return True when a negative Chinese query resolved onto a canonical `increased`/`more` trade stat."""
    if not has_zh_negative_polarity(raw_text):
        return False
    if matched_tokens & _CANONICAL_NEGATIVE_TOKENS:
        return False
    return bool(matched_tokens & _CANONICAL_POSITIVE_TOKENS)


def try_polarity_inversion(
    query_base: str,
    target_tokens: frozenset[str],
    direct_scored: list[tuple[float, int, int, float, Any]],
    threshold: float,
    *,
    strip_leading_placeholder: Callable[[str], str],
    extract_tokens: Callable[[str], frozenset[str]],
    score_entries: Callable[
        [str, str, frozenset[str], float],
        list[tuple[float, int, int, float, Any]],
    ],
) -> tuple[float, Any] | None:
    """Fallback ``reduced -> increased`` / ``less -> more`` only when no native stat matches."""
    swap_pair = next(
        ((s, d) for s, d in POLARITY_SWAP_PAIRS if s in target_tokens and d not in target_tokens),
        None,
    )
    if swap_pair is None:
        return None
    src_word, dst_word = swap_pair
    if direct_scored and src_word in direct_scored[0][4].tokens:
        return None

    swapped_base = re.sub(rf"\b{src_word}\b", dst_word, query_base, flags=re.IGNORECASE)
    swapped_target = normalize_template(swapped_base)
    if not swapped_target:
        return None
    swapped_core = strip_leading_placeholder(swapped_target)
    swapped_tokens = extract_tokens(swapped_core)
    swapped_scored = [
        row
        for row in score_entries(swapped_target, swapped_core, swapped_tokens, max(threshold, 0.85))
        if dst_word in row[4].tokens and src_word not in row[4].tokens
    ]
    if not swapped_scored:
        return None
    best_eff, _, _, base_score, item = swapped_scored[0]
    if direct_scored and best_eff < direct_scored[0][0] and item.stat_id != direct_scored[0][4].stat_id:
        return None
    return base_score, item
