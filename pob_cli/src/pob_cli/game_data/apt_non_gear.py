"""Offline bilingual non-gear dictionary (divination cards, currencies, fragments, maps) from APT."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .tw_norm import _fold_search_text, normalize_tw_query

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "apt_non_gear_items.json.gz"

_NS_LABEL: dict[str, str] = {
    "D": "divination_card",
    "I": "item",
    "A": "area",
}
_NS_RANK: dict[str, int] = {
    "item": 0,
    "divination_card": 1,
    "area": 2,
}


@dataclass(frozen=True)
class _NonGearEntry:
    en: str
    en_folded: str
    zh: str
    aliases_folded: tuple[str, ...]
    category: str


@dataclass(frozen=True)
class _NonGearIndex:
    entries: tuple[_NonGearEntry, ...]
    by_zh_exact: dict[str, str]
    by_en_exact: dict[str, _NonGearEntry]


@lru_cache(maxsize=1)
def _load_non_gear_index() -> _NonGearIndex:
    if not _DATA_PATH.is_file():
        return _NonGearIndex(entries=(), by_zh_exact={}, by_en_exact={})
    payload = json.loads(gzip.decompress(_DATA_PATH.read_bytes()).decode("utf-8"))
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return _NonGearIndex(entries=(), by_zh_exact={}, by_en_exact={})

    entries: list[_NonGearEntry] = []
    by_zh_exact: dict[str, str] = {}
    by_en_exact: dict[str, _NonGearEntry] = {}
    for row in raw_items:
        if not isinstance(row, list) or len(row) < 3:
            continue
        ns_code = str(row[0] or "I")
        en = str(row[1] or "").strip()
        zh = str(row[2] or "").strip()
        if not en:
            continue
        cat = _NS_LABEL.get(ns_code, "item")
        en_f = _fold_search_text(en)
        zh_terms: list[str] = []
        if zh:
            zh_terms.append(_fold_search_text(zh))
        if len(row) >= 4 and row[3]:
            for alt in str(row[3]).split("|"):
                alt_f = _fold_search_text(alt.strip())
                if alt_f and alt_f not in zh_terms:
                    zh_terms.append(alt_f)
        entry = _NonGearEntry(
            en=en,
            en_folded=en_f,
            zh=zh,
            aliases_folded=tuple(zh_terms),
            category=cat,
        )
        entries.append(entry)
        by_en_exact.setdefault(en_f, entry)
        for zf in zh_terms:
            by_zh_exact.setdefault(zf, en)

    return _NonGearIndex(entries=tuple(entries), by_zh_exact=by_zh_exact, by_en_exact=by_en_exact)


def resolve_non_gear_en(query: str | None) -> str | None:
    """Resolve an exact CN/TW/EN non-gear item, currency, divcard, or map name to canonical English."""
    q = str(query or "").strip()
    if not q:
        return None
    idx = _load_non_gear_index()
    for cand in (q, normalize_tw_query(q)):
        qf = _fold_search_text(cand)
        if not qf:
            continue
        if qf in idx.by_zh_exact:
            return idx.by_zh_exact[qf]
        en_hit = idx.by_en_exact.get(qf)
        if en_hit is not None:
            return en_hit.en
    return None


def search_apt_non_gear(query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Search bundled APT non-gear bilingual dictionary (works 100% offline with zero engine)."""
    q_raw = str(query or "").strip()
    if not q_raw:
        return []
    q_folded = _fold_search_text(q_raw)
    q_tw_folded = _fold_search_text(normalize_tw_query(q_raw))
    probes = tuple(dict.fromkeys(p for p in (q_folded, q_tw_folded) if p))
    if not probes:
        return []

    idx = _load_non_gear_index()
    scored: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    for ent in idx.entries:
        best_tier: int | None = None
        for probe in probes:
            if ent.en_folded == probe or probe in ent.aliases_folded:
                tier = 0
            elif ent.en_folded.startswith(probe) or any(a.startswith(probe) for a in ent.aliases_folded):
                tier = 1
            elif probe in ent.en_folded or any(probe in a for a in ent.aliases_folded):
                tier = 2
            else:
                continue
            if best_tier is None or tier < best_tier:
                best_tier = tier
                if best_tier == 0:
                    break
        if best_tier is None:
            continue
        mtype = "exact" if best_tier == 0 else ("prefix" if best_tier == 1 else "substring")
        scored.append(
            (
                (best_tier, _NS_RANK.get(ent.category, 9), len(ent.en_folded), ent.en_folded),
                {
                    "en": ent.en,
                    "zh": ent.zh or None,
                    "category": ent.category,
                    "match_type": mtype,
                    "source": "apt",
                },
            )
        )

    scored.sort(key=lambda item: item[0])
    out = [item[1] for item in scored]
    return out[:limit] if limit > 0 else out


def translate_offline_fallback(
    q_clean: str,
    q_tw: str,
    *,
    limit: int = 25,
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """Engine-free `pob data translate` fallback using bundled APT non-gear + player slang."""
    from .slang import resolve_player_slang

    results = search_apt_non_gear(q_clean, limit=limit)
    if not results and q_tw and q_tw != q_clean:
        results = search_apt_non_gear(q_tw, limit=limit)

    resolved_alias: dict[str, str] | None = None
    if not any(r.get("match_type") == "exact" for r in results):
        slang_hit = resolve_player_slang(q_tw) or (resolve_player_slang(q_clean) if q_tw != q_clean else None)
        if slang_hit is not None:
            canon_en, cat_hint = slang_hit
            apt_by_en = search_apt_non_gear(canon_en, limit=limit)
            if apt_by_en:
                results = apt_by_en
            else:
                results = [{"en": canon_en, "zh": q_clean, "category": cat_hint, "match_type": "exact", "source": "slang"}]
            resolved_alias = {"from": q_clean, "to": canon_en}
    return results, resolved_alias


def build_offline_translate_response(
    q_clean: str,
    q_tw: str,
    *,
    slot: str | None,
    ilvl: int | None,
    mod_clean: str | None,
    limit: int,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    results, alias = translate_offline_fallback(q_clean, q_tw, limit=limit)
    out: dict[str, Any] = {
        "kind": "translate",
        "query": q_clean,
        "filters": {"slot": slot, "ilvl": ilvl, "mod": mod_clean, "limit": limit},
        "pobVersion": "offline",
        "dataVersion": "apt",
        "zhAvailable": True,
        "count": len(results),
        "results": results,
    }
    if alias is not None:
        out["resolvedAlias"] = alias
    if warnings:
        out["warnings"] = warnings
    return out


def build_offline_translate_multi_response(
    clean_queries: list[str],
    q_tw_map: dict[str, str],
    *,
    slot: str | None,
    ilvl: int | None,
    mod_clean: str | None,
    limit: int,
    warnings_by_query: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    res_map = {
        q: build_offline_translate_response(
            q,
            q_tw_map[q],
            slot=slot,
            ilvl=ilvl,
            mod_clean=mod_clean,
            limit=limit,
            warnings=warnings_by_query.get(q),
        )
        for q in clean_queries
    }
    return {
        "kind": "translate",
        "queries": clean_queries,
        "filters": {"slot": slot, "ilvl": ilvl, "mod": mod_clean, "limit": limit},
        "pobVersion": "offline",
        "dataVersion": "apt",
        "zhAvailable": True,
        "count": sum(int(s["count"]) for s in res_map.values()),
        "results": res_map,
    }
