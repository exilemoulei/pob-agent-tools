"""Bilingual crafting bench recipe search, slot listing, and item craftability checks."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..game_data import fold_han
from .loader import BenchRecipe, BenchSearchResult, normalize_line, normalize_template, slot_key

_EN_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_ZH_NOISE_RE = re.compile(r"[\s+\-()（）%#.,，。:：/0-9]+")

_EN_STOPWORDS: frozenset[str] = frozenset(
    {"to", "of", "the", "a", "an", "and", "in", "on", "with", "for", "by", "as", "is", "are", "have", "your", "you"}
)


def source_metadata(*, allow_engine: bool = True) -> dict[str, Any]:
    """Return metadata describing the active bench data source (`local_modmaster` or `embedded_snapshot`)."""
    import pob_cli.bench as _bench

    _, meta = _bench.load_recipes(allow_engine=allow_engine)
    return dict(meta)


def recipes_for_slot(slot: str, *, allow_engine: bool = True) -> list[BenchRecipe]:
    """Return all crafting bench recipes applicable to `slot` (or all recipes if `slot` is empty/`all`)."""
    import pob_cli.bench as _bench

    recipes, _ = _bench.load_recipes(allow_engine=allow_engine)
    target = slot_key(slot)
    if not target or target == "all":
        return list(recipes)
    return [r for r in recipes if target in r.slots]


def _tier_strength(recipe: BenchRecipe) -> float:
    """Numeric bench tier strength: `min_value` for positive stats, min magnitude for negative stats."""
    if not recipe.values:
        return 0.0
    lo, hi = recipe.values[0]
    if lo < 0 and hi < 0:
        return min(abs(lo), abs(hi))
    return lo


def list_for_slot(
    slot: str,
    *,
    affix: str | None = None,
    allow_engine: bool = True,
) -> list[BenchRecipe]:
    """List crafting bench recipes for `slot`, optionally filtered by `affix` (`prefix` or `suffix`)."""
    import pob_cli.bench as _bench

    items = _bench.recipes_for_slot(slot, allow_engine=allow_engine)
    if affix is not None and affix.strip():
        want = affix.strip().lower()
        if want not in ("prefix", "suffix"):
            raise ValueError(f"Invalid affix filter {affix!r}; expected 'prefix' or 'suffix'.")
        items = [r for r in items if r.affix == want]
    items.sort(key=lambda r: (r.affix, r.group, -_tier_strength(r), -r.level, r.text))
    return items


# ---------------------------------------------------------------------------
# Bilingual search & ranking
# ---------------------------------------------------------------------------


def _clean_en_for_match(template: str) -> str:
    s = template.replace("#%", "").replace("#", "")
    return re.sub(r"\s+", " ", s).strip()


def _en_tokens(text: str) -> list[str]:
    words = _EN_WORD_RE.findall(text.lower())
    filtered = [w for w in words if w not in _EN_STOPWORDS]
    return filtered or words


def _clean_zh_for_match(text: str) -> str:
    # Used on the query and on the recipe text, so Traditional and Simplified compare equal.
    return fold_han(_ZH_NOISE_RE.sub("", text.lower()))


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _score_english(query_raw: str, query_tpl: str, recipe: BenchRecipe) -> float:
    q_clean = _clean_en_for_match(query_tpl)
    if not q_clean:
        return 0.0
    r_tpl = recipe.template
    r_clean = _clean_en_for_match(r_tpl)

    if query_tpl == r_tpl or q_clean == r_clean:
        return 1.0

    # Check exact/substring against individual lines as well as full template
    line_cleans = [_clean_en_for_match(normalize_template(ln)) for ln in recipe.lines]
    if any(q_clean == lc for lc in line_cleans):
        return 0.96

    if q_clean in r_clean:
        coverage = len(q_clean) / max(len(r_clean), 1)
        return 0.85 + 0.14 * min(coverage, 1.0)

    q_words = _en_tokens(q_clean)
    if not q_words:
        return 0.0
    r_words = _en_tokens(r_clean)
    r_word_set = set(r_words)

    exact_hits = sum(1 for w in q_words if w in r_word_set)
    substr_hits = sum(1 for w in q_words if w in r_word_set or any(w in rw for rw in r_words))

    if exact_hits == len(q_words):
        coverage = sum(len(w) for w in q_words) / max(sum(len(w) for w in r_words), 1)
        return 0.72 + 0.12 * min(coverage, 1.0)

    if substr_hits == len(q_words):
        coverage = sum(len(w) for w in q_words) / max(sum(len(w) for w in r_words), 1)
        return 0.62 + 0.12 * min(coverage, 1.0)

    if recipe.group and q_clean.replace(" ", "") == recipe.group.lower():
        return 0.78

    if substr_hits > 0:
        recall = substr_hits / len(q_words)
        precision = substr_hits / max(len(r_words), 1)
        score = 0.58 * (0.75 * recall + 0.25 * precision)
        return score if recall >= 0.5 else 0.0

    # Raw text fallback (handles explicit numbers in query like "+2 to level")
    q_low = normalize_line(query_raw).lower()
    if q_low and q_low in recipe.text.lower():
        coverage = len(q_low) / max(len(recipe.text), 1)
        return 0.82 + 0.12 * min(coverage, 1.0)

    return 0.0


def _score_chinese(query_raw: str, recipe: BenchRecipe) -> float:
    if not recipe.text_zh:
        return 0.0
    q_zh = _clean_zh_for_match(query_raw)
    if not q_zh:
        return 0.0
    r_zh = _clean_zh_for_match(recipe.text_zh)
    if not r_zh:
        return 0.0

    if q_zh == r_zh:
        return 1.0

    line_zhs = [_clean_zh_for_match(ln) for ln in recipe.lines_zh]
    if any(q_zh == lz for lz in line_zhs):
        return 0.96

    if q_zh in r_zh:
        coverage = len(q_zh) / max(len(r_zh), 1)
        return 0.85 + 0.14 * min(coverage, 1.0)

    # Space-separated Chinese keywords (e.g. "冰霜 抗性", "攻击 速度")
    parts = [_clean_zh_for_match(p) for p in re.split(r"\s+", query_raw.strip()) if _clean_zh_for_match(p)]
    if len(parts) > 1 and all(p in r_zh for p in parts):
        coverage = sum(len(p) for p in parts) / max(len(r_zh), 1)
        return 0.74 + 0.12 * min(coverage, 1.0)

    # Ordered CJK character subsequence fallback (e.g. "冰抗" -> "冰霜抗性")
    q_cjk = "".join(_CJK_CHAR_RE.findall(q_zh))
    r_cjk = "".join(_CJK_CHAR_RE.findall(r_zh))
    if len(q_cjk) >= 2 and r_cjk:
        if _is_subsequence(q_cjk, r_cjk):
            coverage = len(q_cjk) / max(len(r_cjk), 1)
            return 0.45 + 0.20 * min(coverage, 1.0)
        hits = sum(1 for ch in set(q_cjk) if ch in r_cjk)
        recall = hits / len(set(q_cjk))
        if recall >= 0.7:
            return 0.30 + 0.15 * recall

    return 0.0


def search(
    slot: str,
    text: str,
    limit: int = 5,
    *,
    affix: str | None = None,
    allow_engine: bool = True,
) -> list[BenchSearchResult]:
    """Search crafting bench recipes for `slot` using English or Chinese text.

    Ranks by match score descending, then highest `min_value` (best bench tier),
    then highest `level`.
    """
    import pob_cli.bench as _bench

    candidates = _bench.list_for_slot(slot, affix=affix, allow_engine=allow_engine)
    if limit <= 0:
        return []

    q_raw = (text or "").strip()
    if not q_raw:
        candidates.sort(key=lambda r: (-_tier_strength(r), -r.level, r.text))
        return [BenchSearchResult(recipe=r, score=1.0) for r in candidates[:limit]]

    q_tpl = normalize_template(q_raw)
    has_cjk = bool(_CJK_CHAR_RE.search(q_raw))

    scored: list[BenchSearchResult] = []
    for recipe in candidates:
        en_score = _score_english(q_raw, q_tpl, recipe)
        zh_score = _score_chinese(q_raw, recipe) if has_cjk else 0.0
        best_score = max(en_score, zh_score)
        if best_score >= 0.25:
            scored.append(BenchSearchResult(recipe=recipe, score=best_score))

    scored.sort(
        key=lambda item: (
            -round(item.score, 6),
            -_tier_strength(item.recipe),
            -item.recipe.level,
            item.recipe.text,
        )
    )
    return scored[:limit]


def best(
    slot: str,
    text: str,
    *,
    affix: str | None = None,
    allow_engine: bool = True,
) -> BenchRecipe | None:
    """Return the highest-ranked crafting bench recipe matching `text` for `slot`."""
    import pob_cli.bench as _bench

    hits = _bench.search(slot, text, limit=1, affix=affix, allow_engine=allow_engine)
    return hits[0].recipe if hits else None


def can_craft_on_item(
    recipe: BenchRecipe,
    existing_templates: Iterable[str],
    *,
    has_crafted_mod: bool = False,
) -> tuple[bool, str]:
    """Check whether `recipe` can be crafted on an item with `existing_templates`.

    Returns `(allowed, message)`:
    - `(False, ...)` if any normalized stat template in `recipe` conflicts with
      an existing modifier on the item.
    - `(True, ...)` if craftable; when `has_crafted_mod=True`, notes that the
      existing crafted modifier can be replaced at the crafting bench.
    """
    norm_existing = {normalize_template(t) for t in existing_templates if t and normalize_template(t)}
    recipe_templates = [recipe.template]
    for ln in recipe.lines:
        lt = normalize_template(ln)
        if lt and lt not in recipe_templates:
            recipe_templates.append(lt)

    for rt in recipe_templates:
        if rt in norm_existing:
            return (
                False,
                f"Conflict: item already has matching modifier template '{rt}'.",
            )

    if has_crafted_mod:
        return (
            True,
            "Item already has a crafted modifier; existing craft can be replaced at the crafting bench.",
        )

    return True, "Craftable on item."


def format_recipe(recipe: BenchRecipe, *, show_zh: bool = True) -> str:
    """Format a single bench recipe as a concise human-readable line."""
    parts = [f"[{recipe.affix}] {recipe.text}"]
    if show_zh and recipe.text_zh:
        parts.append(recipe.text_zh)
    parts.append(f"Cost: {recipe.cost}")
    if recipe.unlock:
        parts.append(f"Unlock: {recipe.unlock}")
    return " | ".join(parts)
