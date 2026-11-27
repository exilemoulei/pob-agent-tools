"""3.30 Greater Viscera prefix strip and wombgift <-> viscera sibling fallback for `ninja_price`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_SIBLING_CATEGORY: dict[str, tuple[str, tuple[str, ...]]] = {
    "wombgift": ("viscera", ("Viscera",)),
    "viscera": ("wombgift", ("Wombgift",)),
}


def apply_viscera_price_fallback(
    *,
    raw_query: str,
    res_q: str,
    res_alias: dict[str, str] | None,
    cat_key: str,
    primary_result: dict[str, Any],
    overviews: dict[str, list[dict[str, Any]]],
    fetched_stash: dict[str, list[dict[str, Any]]],
    resolved_league: str,
    variant: str | None,
    is_detailed: bool,
    limit: int,
    divine_chaos_rate: float | None,
    is_stale: bool,
    proxy: str | None,
    score_fn: Callable[..., dict[str, Any]],
    fetch_stash_fn: Callable[..., tuple[dict[str, list[dict[str, Any]]], set[str], set[str]]],
) -> dict[str, Any]:
    """Retry zero-match 3.30 graft queries with `Greater ` prefix stripping and `wombgift` <-> `viscera` fallback."""
    if primary_result.get("totalMatches", 0) > 0:
        return primary_result
    if cat_key not in ("all", "viscera", "wombgift"):
        return primary_result

    stripped_q = res_q[8:].strip() if res_q.lower().startswith("greater ") else ""
    fetched_stale_types: set[str] = set()

    def _source_for(types: tuple[str, ...]) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        if is_detailed:
            missing = [t for t in types if t not in fetched_stash]
            if missing:
                new_stash, new_stale, new_failed = fetch_stash_fn(resolved_league, missing, proxy=proxy)
                fetched_stash.update(new_stash)
                fetched_stale_types.update((new_stale | new_failed) & set(missing))
            stale_flag = is_stale or any(t in fetched_stale_types for t in types)
            return {t: (fetched_stash.get(t) or overviews.get(t, [])) for t in types}, stale_flag
        return {t: overviews.get(t, []) for t in types}, is_stale

    def _try(
        search_q: str,
        types: tuple[str, ...],
        alias: dict[str, str] | None,
    ) -> dict[str, Any]:
        src, stale_flag = _source_for(types)
        return score_fn(
            raw_query,
            search_query=search_q,
            resolved_alias=alias,
            resolved_league=resolved_league,
            cat_key=cat_key,
            variant=variant,
            is_detailed=is_detailed,
            limit=limit,
            divine_chaos_rate=divine_chaos_rate,
            target_types=types,
            source_by_type=src,
            is_stale=stale_flag,
        )

    if stripped_q:
        strip_alias = {"from": raw_query, "to": stripped_q}
        own_types = (
            ("Viscera", "Wombgift")
            if cat_key == "all"
            else (("Viscera",) if cat_key == "viscera" else ("Wombgift",))
        )
        cand = _try(stripped_q, own_types, strip_alias)
        if cand.get("totalMatches", 0) > 0:
            return cand

    sibling = _SIBLING_CATEGORY.get(cat_key)
    if sibling is not None:
        sibling_cat, sibling_types = sibling
        cand_exact = _try(res_q, sibling_types, res_alias)
        if cand_exact.get("totalMatches", 0) > 0:
            cand_exact["categoryFallback"] = sibling_cat
            return cand_exact
        if stripped_q:
            cand_strip = _try(stripped_q, sibling_types, {"from": raw_query, "to": stripped_q})
            if cand_strip.get("totalMatches", 0) > 0:
                cand_strip["categoryFallback"] = sibling_cat
                return cand_strip

    return primary_result
