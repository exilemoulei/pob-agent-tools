"""poe.ninja economy price search, scoring, and multi-query orchestration."""

from __future__ import annotations

import re
from typing import Any

from .constants import (
    _AUTO_DETAIL_PATTERNS,
    _LAST_STALE,
    ALL_OVERVIEW_TYPES,
    CATEGORY_MAP,
)
from .ninja_http import (
    _extract_challenge_league_from_index_state,
    _fetch_dense_overviews,
    _fetch_stash_overview,
    _fetch_stash_overviews_batch,
    _pkg,
    _read_fresh_stash_cache,
    _stash_cache_path,
    resolve_current_trade_league,
)
from .ninja_viscera import apply_viscera_price_fallback

__all__ = [
    "_build_searchable_fields",
    "_extract_challenge_league_from_index_state",
    "_extract_cluster_passives",
    "_fetch_dense_overviews",
    "_fetch_stash_overview",
    "_fetch_stash_overviews_batch",
    "_matches_text_or_tokens",
    "_narrow_detailed_types",
    "_read_fresh_stash_cache",
    "_resolve_types_to_search",
    "_score_price_matches",
    "_should_auto_detail",
    "_stash_cache_path",
    "query_price",
    "query_price_multi",
    "resolve_current_trade_league",
]


def _should_auto_detail(query: str, variant: str | None) -> bool:
    combined = f"{query} {variant or ''}"
    return any(pat.search(combined) for pat in _AUTO_DETAIL_PATTERNS)


_AWAKENED_ZH_RE = re.compile(r"^(?:觉醒|覺醒|强辅|強輔)[\s·・]*(.+?)$|^(.+?)(?:[（(](?:强辅|強輔)[）)]|强辅|強輔)$")
_FOULBORN_ZH_RE = re.compile(r"^(?:秽生|穢生)[\s·・]+(.+)$|^(?:秽生|穢生)(.+)$")
_PRICE_BRACKETS_RE = re.compile(r"[【】「」『』《》]")


def _resolve_zh_entity_name(term: str, gd: Any) -> tuple[str | None, str | None]:
    """Resolve a single CN/TW/slang entity name to `(canonical_en, kind_hint)`."""
    t_clean = _PRICE_BRACKETS_RE.sub("", term).strip()
    if not t_clean:
        return None, None
    t_tw = gd.normalize_tw_query(t_clean)
    slang_hit = gd.resolve_player_slang(t_clean) or (gd.resolve_player_slang(t_tw) if t_tw != t_clean else None)
    if slang_hit is not None:
        return slang_hit[0], slang_hit[1]
    non_gear_en = gd.resolve_non_gear_en(t_clean) or (gd.resolve_non_gear_en(t_tw) if t_tw != t_clean else None)
    if non_gear_en:
        return non_gear_en, "non_gear"
    try:
        en_name = gd.english_for_zh(t_clean) or (gd.english_for_zh(t_tw) if t_tw != t_clean else None)
    except Exception:
        en_name = None
    if en_name:
        return en_name, None
    # Fallback to local SQLite unique / gem / base tables when engine is available;
    # accept only exact normalized matches (never arbitrary substring hits).
    fold = getattr(gd, "fold_han", lambda s: s.strip().lower())
    wanted_norms = {fold(t_clean).strip().lower(), fold(t_tw).strip().lower()}
    wanted_norms.discard("")
    for kind in ("unique", "gem", "base"):
        try:
            res = gd.query_local_data(kind, t_clean, limit=5)
        except Exception:
            break
        rows = res.get("results") if isinstance(res, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            cand = str(row.get("name") or "").strip()
            if not (cand and cand.isascii()):
                continue
            row_labels = (
                cand,
                str(row.get("zh_name") or ""),
                str(row.get("name_zh") or ""),
                str(row.get("nameZh") or ""),
            )
            if any(fold(lbl).strip().lower() in wanted_norms for lbl in row_labels if lbl.strip()):
                return cand, kind
    return None, None


def _resolve_price_query(raw_query: str) -> tuple[str, dict[str, str] | None]:
    """Translate non-ASCII (CN/TW/slang) price queries into canonical English before poe.ninja lookup."""
    q_clean = raw_query.strip()
    if not q_clean or q_clean.isascii():
        return q_clean, None
    from .. import game_data as gd

    en_name, _kind = _resolve_zh_entity_name(q_clean, gd)
    if en_name:
        return en_name, {"from": q_clean, "to": en_name}

    stripped = _PRICE_BRACKETS_RE.sub("", q_clean).strip()
    m_awk = _AWAKENED_ZH_RE.match(stripped)
    if m_awk:
        stem = (m_awk.group(1) or m_awk.group(2) or "").strip()
        stem_en, _ = _resolve_zh_entity_name(stem, gd)
        if stem_en:
            canon = stem_en if stem_en.lower().startswith("awakened ") else f"Awakened {stem_en}"
            if not canon.lower().endswith(" support"):
                canon = f"{canon} Support"
            return canon, {"from": q_clean, "to": canon}

    m_fb = _FOULBORN_ZH_RE.match(stripped)
    if m_fb:
        stem = (m_fb.group(1) or m_fb.group(2) or "").strip()
        stem_en, _ = _resolve_zh_entity_name(stem, gd)
        if stem_en:
            return stem_en, {"from": q_clean, "to": stem_en}

    q_tw = gd.normalize_tw_query(q_clean)
    if q_tw and q_tw != q_clean and q_tw.isascii():
        return q_tw, {"from": q_clean, "to": q_tw}
    return q_clean, None


def _extract_cluster_passives(line: dict[str, Any]) -> int | None:
    variant = str(line.get("variant") or "")
    m = re.search(r"(\d+)\s*passives?", variant, re.IGNORECASE)
    if m:
        return int(m.group(1))
    for ti in line.get("tradeInfo") or []:
        if isinstance(ti, dict) and str(ti.get("mod") or "") == "enchant.stat_3086156145":
            val = ti.get("min") or ti.get("max")
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    pass
    return None


def _build_searchable_fields(line: dict[str, Any]) -> tuple[str, str, str]:
    """Return `(name_low, primary_blob, variant_blob)` for query & variant filtering."""
    name = str(line.get("name") or "")
    variant = str(line.get("variant") or "")
    details_id = str(line.get("detailsId") or "")
    base_type = str(line.get("baseType") or "")
    metadata = line.get("metadata") or {}
    meta_parts = [str(v) for v in metadata.values() if v] if isinstance(metadata, dict) else []

    extras: list[str] = []
    if line.get("corrupted") is True:
        extras.append("corrupted")
    elif line.get("corrupted") is False:
        extras.append("uncorrupted")
    gem_lv = line.get("gemLevel")
    gem_q = line.get("gemQuality")
    if gem_lv is not None:
        q_val = int(gem_q) if gem_q is not None else 0
        extras.append(f"{gem_lv}/{q_val}")
        if line.get("corrupted") is True:
            extras.append(f"{gem_lv}/{q_val}c")
    links = line.get("links")
    if links:
        extras.append(f"{links}l {links}-link")

    name_low = name.lower()
    primary_blob = " | ".join([name, variant, details_id, base_type, *extras]).lower()
    variant_blob = " | ".join([variant, details_id, name, base_type, *meta_parts, *extras]).lower()
    return name_low, primary_blob, variant_blob


def _matches_text_or_tokens(needle_low: str, blob: str) -> bool:
    if needle_low in blob:
        return True
    tokens = [tok for tok in re.split(r"\s+", needle_low) if tok]
    return len(tokens) > 1 and all(tok in blob for tok in tokens)


def _resolve_types_to_search(cat_key: str, available_keys: tuple[str, ...]) -> tuple[str, ...]:
    if cat_key == "all":
        return available_keys or ALL_OVERVIEW_TYPES
    if cat_key in CATEGORY_MAP:
        return CATEGORY_MAP[cat_key]
    for k in available_keys or ALL_OVERVIEW_TYPES:
        if k.lower() == cat_key:
            return (k,)
    return available_keys or ALL_OVERVIEW_TYPES


def _narrow_detailed_types(
    q: str,
    cat_key: str,
    types_to_search: tuple[str, ...],
    overviews: dict[str, list[dict[str, Any]]],
    *,
    variant: str | None = None,
) -> tuple[str, ...]:
    if cat_key == "all" and overviews:
        q_tokens = [tok for tok in re.split(r"\s+", q) if len(tok) >= 3]
        narrowed: list[str] = []
        for ov_type in types_to_search:
            ov_lines = overviews.get(ov_type, [])
            matched_type = False
            for ln in ov_lines:
                _, p_blob, _ = _build_searchable_fields(ln)
                if _matches_text_or_tokens(q, p_blob) or (q_tokens and any(tok in p_blob for tok in q_tokens)):
                    matched_type = True
                    break
            if matched_type:
                narrowed.append(ov_type)
        if narrowed:
            return tuple(narrowed)
        # Fallback when dense overview lacks a stash-only variant (e.g. 21/20 gem or 6L unique)
        if variant and re.search(r"\b\d{1,2}/\d{1,2}c?\b", variant, re.IGNORECASE):
            return ("SkillGem", "ImbuedGem")
        if variant and re.search(r"\b[56]l\b|\b[56]-?link\b", variant, re.IGNORECASE):
            return ("UniqueWeapon", "UniqueArmour")
        return ()
    return types_to_search


def _score_price_matches(
    query: str,
    *,
    search_query: str | None = None,
    resolved_alias: dict[str, str] | None = None,
    resolved_league: str,
    cat_key: str,
    variant: str | None,
    is_detailed: bool,
    limit: int,
    divine_chaos_rate: float | None,
    target_types: tuple[str, ...],
    source_by_type: dict[str, list[dict[str, Any]]],
    is_stale: bool = False,
) -> dict[str, Any]:
    q = (search_query if search_query is not None else query).strip().lower()
    v_low = variant.strip().lower() if variant and variant.strip() else None

    matches: list[dict[str, Any]] = []
    for ov_type in target_types:
        for line in source_by_type.get(ov_type, []):
            name = str(line.get("name") or "")
            raw_variant = str(line.get("variant") or "")
            name_low, primary_blob, variant_blob = _build_searchable_fields(line)

            if not _matches_text_or_tokens(q, primary_blob):
                continue
            if v_low and not _matches_text_or_tokens(v_low, variant_blob):
                continue

            if q == name_low:
                score = 3.0
            elif name_low.startswith(q):
                score = 2.0
            elif q in primary_blob:
                score = 1.0
            else:
                score = 0.5

            chaos_val = float(line.get("chaos") or line.get("chaosValue") or 0.0)
            raw_div = line.get("divineValue")
            if raw_div is not None:
                div_val: float | None = float(raw_div)
            elif divine_chaos_rate and divine_chaos_rate > 0:
                div_val = chaos_val / divine_chaos_rate
            else:
                div_val = None

            spark_obj = line.get("sparkLine")
            if isinstance(spark_obj, dict) and spark_obj.get("totalChange") is not None:
                sparkline_change: float | None = round(float(spark_obj["totalChange"]), 2)
            else:
                graph = line.get("graph") or []
                latest_trend = graph[-1] if (isinstance(graph, list) and graph and graph[-1] is not None) else None
                sparkline_change = round(float(latest_trend), 1) if latest_trend is not None else None

            entry: dict[str, Any] = {
                "name": name,
                "type": ov_type,
                "variant": raw_variant or None,
                "baseType": line.get("baseType"),
                "detailsId": line.get("detailsId"),
                "chaosValue": chaos_val,
                "divineValue": div_val,
                "gemLevel": line.get("gemLevel"),
                "gemQuality": line.get("gemQuality"),
                "corrupted": line.get("corrupted"),
                "links": line.get("links"),
                "listingCount": line.get("listingCount") if line.get("listingCount") is not None else line.get("count"),
                "sparklineChange": sparkline_change,
                "trend24hPct": sparkline_change,
                "_score": score,
            }
            if ov_type == "ClusterJewel" or line.get("tradeInfo") is not None:
                entry["tradeInfo"] = line.get("tradeInfo")
                entry["clusterPassives"] = _extract_cluster_passives(line)
            matches.append(entry)

    matches.sort(key=lambda m: (float(m.get("_score") or 0), float(m.get("chaosValue") or 0.0)), reverse=True)
    for m in matches:
        m.pop("_score", None)

    out: dict[str, Any] = {
        "query": query,
        "league": resolved_league,
        "category": cat_key,
        "variant": variant,
        "detailed": is_detailed,
        "divineChaosRate": divine_chaos_rate,
        "totalMatches": len(matches),
        "matches": matches[:limit],
    }
    if resolved_alias is not None:
        out["resolvedQuery"] = resolved_alias["to"]
        out["resolvedAlias"] = resolved_alias
    if is_stale:
        out["stale"] = True
    return out


def query_price(
    query: str,
    *,
    league: str | None = None,
    category: str = "all",
    limit: int = 20,
    variant: str | None = None,
    detailed: bool = False,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Search poe.ninja economy prices with 15-minute local disk cache.

    Supports `--variant` substring filtering, `--detailed` (or auto-upgraded)
    concurrent stash overview queries, explicit proxy, and stale-cache fallback.
    """
    from ..guardrails import GuardrailScope, check_input, validate_url

    q_raw = query.strip()
    if not q_raw:
        raise ValueError("Price query cannot be empty.")
    validate_url(q_raw, expected_game="poe1", cli_subcommand="price")
    check_input(q_raw, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    if category and category.strip().lower() != "all":
        check_input(category, scope=GuardrailScope.CATEGORY, expected_game="poe1")

    resolved_q, resolved_alias = _resolve_price_query(q_raw)
    q = resolved_q.lower()

    pkg = _pkg()
    is_detailed = bool(detailed or _should_auto_detail(resolved_q, variant))

    resolved_league = league or pkg.resolve_current_trade_league(proxy=proxy)
    _LAST_STALE.value = False
    overviews = (
        pkg._fetch_dense_overviews(resolved_league, proxy=proxy)
        if proxy is not None
        else pkg._fetch_dense_overviews(resolved_league)
    )
    is_stale = bool(getattr(_LAST_STALE, "value", False))

    cat_key = category.strip().lower()
    types_to_search = _resolve_types_to_search(cat_key, tuple(overviews.keys()))

    divine_chaos_rate: float | None = None
    for line in overviews.get("Currency", []):
        if str(line.get("name") or "").lower() == "divine orb":
            divine_chaos_rate = float(line.get("chaos") or line.get("chaosValue") or 0.0)
            break

    fetched_stash: dict[str, list[dict[str, Any]]] = {}
    if is_detailed:
        target_types = _narrow_detailed_types(q, cat_key, types_to_search, overviews, variant=variant)
        if target_types:
            fetched_stash, stash_stale_types, stash_failed_types = _fetch_stash_overviews_batch(
                resolved_league, target_types, proxy=proxy
            )
            if any(t in stash_stale_types or t in stash_failed_types for t in target_types):
                is_stale = True
            source_by_type = {
                ov_type: (fetched_stash.get(ov_type) or overviews.get(ov_type, []))
                for ov_type in target_types
            }
        else:
            source_by_type = {}
    else:
        target_types = types_to_search
        source_by_type = {ov_type: overviews.get(ov_type, []) for ov_type in target_types}

    primary_res = _score_price_matches(
        query,
        search_query=resolved_q,
        resolved_alias=resolved_alias,
        resolved_league=resolved_league,
        cat_key=cat_key,
        variant=variant,
        is_detailed=is_detailed,
        limit=limit,
        divine_chaos_rate=divine_chaos_rate,
        target_types=target_types,
        source_by_type=source_by_type,
        is_stale=is_stale,
    )
    return apply_viscera_price_fallback(
        raw_query=q_raw,
        res_q=resolved_q,
        res_alias=resolved_alias,
        cat_key=cat_key,
        primary_result=primary_res,
        overviews=overviews,
        fetched_stash=fetched_stash,
        resolved_league=resolved_league,
        variant=variant,
        is_detailed=is_detailed,
        limit=limit,
        divine_chaos_rate=divine_chaos_rate,
        is_stale=is_stale,
        proxy=proxy,
        score_fn=_score_price_matches,
        fetch_stash_fn=_fetch_stash_overviews_batch,
    )


def query_price_multi(
    queries: list[str],
    *,
    league: str | None = None,
    category: str = "all",
    limit: int = 20,
    variant: str | None = None,
    detailed: bool = False,
    proxy: str | None = None,
    max_workers: int = 6,
) -> dict[str, Any]:
    """Search multiple poe.ninja economy queries sharing ONE dense/stash fetch batch."""
    from ..guardrails import GuardrailScope, check_input, validate_url

    clean_queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not clean_queries:
        raise ValueError("Price queries cannot be empty.")
    if category and category.strip().lower() != "all":
        check_input(category, scope=GuardrailScope.CATEGORY, expected_game="poe1")
    for q_item in clean_queries:
        validate_url(q_item, expected_game="poe1", cli_subcommand="price")
        check_input(q_item, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")

    pkg = _pkg()
    resolved_league = league or pkg.resolve_current_trade_league(proxy=proxy)
    _LAST_STALE.value = False
    overviews = (
        pkg._fetch_dense_overviews(resolved_league, proxy=proxy)
        if proxy is not None
        else pkg._fetch_dense_overviews(resolved_league)
    )
    dense_stale = bool(getattr(_LAST_STALE, "value", False))

    cat_key = category.strip().lower()
    types_to_search = _resolve_types_to_search(cat_key, tuple(overviews.keys()))

    divine_chaos_rate: float | None = None
    for line in overviews.get("Currency", []):
        if str(line.get("name") or "").lower() == "divine orb":
            divine_chaos_rate = float(line.get("chaos") or line.get("chaosValue") or 0.0)
            break

    per_query_plan: list[tuple[str, str, dict[str, str] | None, bool, tuple[str, ...]]] = []
    all_stash_types: list[str] = []
    seen_stash: set[str] = set()
    for q_raw in clean_queries:
        res_q, res_alias = _resolve_price_query(q_raw)
        q_det = bool(detailed or _should_auto_detail(res_q, variant))
        if q_det:
            t_types = _narrow_detailed_types(res_q.lower(), cat_key, types_to_search, overviews, variant=variant)
            for ov_type in t_types:
                if ov_type not in seen_stash:
                    seen_stash.add(ov_type)
                    all_stash_types.append(ov_type)
        else:
            t_types = types_to_search
        per_query_plan.append((q_raw, res_q, res_alias, q_det, t_types))

    fetched_stash: dict[str, list[dict[str, Any]]] = {}
    stash_stale_types: set[str] = set()
    stash_failed_types: set[str] = set()
    if all_stash_types:
        fetched_stash, stash_stale_types, stash_failed_types = _fetch_stash_overviews_batch(
            resolved_league, all_stash_types, proxy=proxy, max_workers=max_workers
        )

    results: list[dict[str, Any]] = []
    for q_raw, res_q, res_alias, q_det, t_types in per_query_plan:
        q_stale = dense_stale
        if q_det:
            if any(t in stash_stale_types or t in stash_failed_types for t in t_types):
                q_stale = True
            src = {ov_type: (fetched_stash.get(ov_type) or overviews.get(ov_type, [])) for ov_type in t_types}
        else:
            src = {ov_type: overviews.get(ov_type, []) for ov_type in t_types}
        primary_r = _score_price_matches(
            q_raw,
            search_query=res_q,
            resolved_alias=res_alias,
            resolved_league=resolved_league,
            cat_key=cat_key,
            variant=variant,
            is_detailed=q_det,
            limit=limit,
            divine_chaos_rate=divine_chaos_rate,
            target_types=t_types,
            source_by_type=src,
            is_stale=q_stale,
        )
        results.append(
            apply_viscera_price_fallback(
                raw_query=q_raw,
                res_q=res_q,
                res_alias=res_alias,
                cat_key=cat_key,
                primary_result=primary_r,
                overviews=overviews,
                fetched_stash=fetched_stash,
                resolved_league=resolved_league,
                variant=variant,
                is_detailed=q_det,
                limit=limit,
                divine_chaos_rate=divine_chaos_rate,
                is_stale=q_stale,
                proxy=proxy,
                score_fn=_score_price_matches,
                fetch_stash_fn=_fetch_stash_overviews_batch,
            )
        )

    out: dict[str, Any] = {
        "queries": clean_queries,
        "league": resolved_league,
        "category": cat_key,
        "variant": variant,
        "detailed": any(r["detailed"] for r in results),
        "divineChaosRate": divine_chaos_rate,
        "count": len(results),
        "results": results,
    }
    if dense_stale or any(bool(r.get("stale")) for r in results):
        out["stale"] = True
    return out
