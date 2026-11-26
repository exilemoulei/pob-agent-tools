"""Zero-HTTP L0 full-build shopping-list query generator and local trade URL encoder."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from ..locale_cfg import normalize_realm
from ..trade_client import map_tw_league
from ..trade_item import Mod
from ..trade_query import (
    apply_exact_item_filters,
    category_for_slot,
    infer_weapon_category_from_base,
    resolve_option_stat,
    resolve_stat,
    split_pipe_stat_id,
)
from ..trade_query.auto_query import COMPLEXITY_LIMIT, query_complexity
from .shopping_extract import ShoppingEntry, extract_build_shopping_entries
from .shopping_specials import (
    collect_item_query_warnings,
    encode_trade_search_url,
)

_INFLUENCE_FILTER_KEY: dict[str, str] = {
    "Shaper": "shaper_item",
    "Elder": "elder_item",
    "Warlord": "warlord_item",
    "Hunter": "hunter_item",
    "Crusader": "crusader_item",
    "Redeemer": "redeemer_item",
    "Searing Exarch": "searing_item",
    "Eater of Worlds": "tangled_item",
}

_RARE_PRIORITY_CATS: dict[str, int] = {
    "skill_level": 0,
    "resist": 1,
    "attributes": 2,
    "life": 3,
    "energy_shield": 3,
    "defense": 4,
    "speed": 5,
    "crit": 6,
    "damage": 7,
    "mana": 8,
}

_DISCRETE_ROLL_RE_KEYWORDS: tuple[str, ...] = (
    "level of",
    "magic utility flasks",
    "maximum endurance",
    "maximum frenzy",
    "maximum power",
    "additional arrow",
    "additional projectile",
    "additional curse",
    "socketed gems",
    "abyssal socket",
)


def _group_stats_by_domain(stat_entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Pre-bucket stat entries by domain once so `_EntryIndex` cache hits across all build items."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for entry in stat_entries:
        sid = str(entry.get("id") or "")
        dom = sid.split(".", 1)[0].lower() if "." in sid else ""
        if dom:
            buckets.setdefault(dom, []).append(entry)
    return buckets


def _stat_filter_from_mod(
    mod: Mod,
    stat_entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
    domain: str | None = None,
    roll_scale: float = 0.85,
    exact_value: bool = False,
    by_domain: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    dom = (domain or mod.kind or "explicit").strip().lower()
    if by_domain is not None:
        scoped = by_domain.get(dom, [])
    else:
        scoped = [e for e in stat_entries if str(e.get("id") or "").lower().startswith(f"{dom}.")]
    if not scoped:
        return None

    opt = resolve_option_stat(mod.raw, scoped, domain=dom)
    if opt is not None:
        return {"id": opt.id, "value": {"option": opt.option_id}, "disabled": False}

    m = resolve_stat(mod.raw, scoped, slot=slot, threshold=0.84)
    if m is None:
        return None
    base_id, pipe_opt = split_pipe_stat_id(m.id)
    if pipe_opt is not None:
        return {"id": base_id, "value": {"option": pipe_opt}, "disabled": False}

    val: dict[str, Any] = {}
    if mod.values:
        num = (mod.values[0] + mod.values[1]) / 2.0 if len(mod.values) == 2 and mod.template.startswith("adds ") else mod.values[0]
        if exact_value:
            val["min"] = int(num) if float(num).is_integer() else round(num, 2)
        elif num < 0:
            val["min"] = math.floor(num)
        elif num <= 8 and any(kw in mod.template for kw in _DISCRETE_ROLL_RE_KEYWORDS):
            val["min"] = math.floor(num)
        elif num >= 10:
            val["min"] = math.floor(num * roll_scale)
        elif num > 0:
            val["min"] = math.floor(num)
    return {"id": base_id, "value": val, "disabled": False}


def _dedupe_filters(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for flt in filters:
        key = (str(flt.get("id") or ""), json.dumps(flt.get("value") or {}, sort_keys=True))
        if key not in seen:
            seen.add(key)
            out.append(flt)
    return out


def _build_unique_filters(
    entry: ShoppingEntry,
    stat_entries: list[dict[str, Any]],
    *,
    by_domain: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    filters: list[dict[str, Any]] = []
    notes: list[str] = []
    parsed = entry.parsed
    if parsed is None:
        return filters, notes

    # 1. Always preserve Foulborn {mutated} mods
    for mod in parsed.mods:
        if "mutated" in mod.tags:
            flt = _stat_filter_from_mod(mod, stat_entries, slot=entry.slot, domain="explicit", roll_scale=0.90, by_domain=by_domain)
            if flt:
                filters.append(flt)
                notes.append(f"Foulborn: {mod.raw}")

    # 2. Corrupted implicit(s)
    if parsed.corrupted:
        for mod in parsed.mods:
            if mod.kind == "implicit":
                flt = _stat_filter_from_mod(mod, stat_entries, slot=entry.slot, domain="implicit", roll_scale=0.90, by_domain=by_domain)
                if flt:
                    filters.append(flt)
                    notes.append(f"Corrupted implicit: {mod.raw}")
                    break

    # 3. High-impact discrete tier or top roll explicit mods (at most 2 to avoid over-constraining)
    explicit_mods = [m for m in parsed.mods if m.kind == "explicit" and "mutated" not in m.tags]
    discrete_hits = [
        m for m in explicit_mods if m.values and any(kw in m.template for kw in _DISCRETE_ROLL_RE_KEYWORDS)
    ]
    for mod in discrete_hits[:2]:
        flt = _stat_filter_from_mod(mod, stat_entries, slot=entry.slot, domain="explicit", exact_value=True, by_domain=by_domain)
        if flt:
            filters.append(flt)
            notes.append(mod.raw)
    return _dedupe_filters(filters), notes


def _build_rare_filters(
    entry: ShoppingEntry,
    stat_entries: list[dict[str, Any]],
    *,
    max_stats: int = 5,
    by_domain: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    parsed = entry.parsed
    if parsed is None:
        return []
    candidates = [m for m in parsed.mods if m.kind in ("explicit", "fractured") and m.values]
    candidates.sort(key=lambda m: (_RARE_PRIORITY_CATS.get(m.category, 20), -abs(m.signed_value)))
    filters: list[dict[str, Any]] = []
    for mod in candidates:
        dom = "fractured" if mod.kind == "fractured" else "explicit"
        flt = _stat_filter_from_mod(mod, stat_entries, slot=entry.slot, domain=dom, roll_scale=0.85, by_domain=by_domain)
        if flt:
            filters.append(flt)
        if len(filters) >= max_stats:
            break
    return _dedupe_filters(filters)


def _apply_corrupted_jewel_constraints(
    entry: ShoppingEntry,
    stat_entries: list[dict[str, Any]],
    filters_blk: dict[str, Any],
    stat_filters: list[dict[str, Any]],
    notes: list[str],
    *,
    by_domain: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    if not entry.corrupted:
        return
    filters_blk.setdefault("misc_filters", {}).setdefault("filters", {})["corrupted"] = {"option": "true"}
    if entry.parsed is None:
        return
    for mod in entry.parsed.mods:
        if mod.kind == "implicit":
            flt = _stat_filter_from_mod(mod, stat_entries, slot=entry.slot, domain="implicit", roll_scale=0.90, by_domain=by_domain)
            if flt:
                stat_filters.append(flt)
                notes.append(f"Corrupted implicit: {mod.raw}")
                break


def _build_jewel_query(
    entry: ShoppingEntry,
    stat_entries: list[dict[str, Any]],
    *,
    status: str,
    by_domain: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    inner: dict[str, Any] = {"status": {"option": status}}
    filters_blk: dict[str, Any] = {}
    stat_filters: list[dict[str, Any]] = []
    notes: list[str] = []
    explicit_entries = (by_domain or {}).get("explicit") or stat_entries

    if entry.kind == "cluster_jewel":
        inner["type"] = entry.base_type
        filters_blk["type_filters"] = {"filters": {"category": {"option": "jewel.cluster"}}}
        if entry.cluster_passives is not None:
            stat_filters.append(
                {
                    "id": "enchant.stat_3086156145",
                    "value": {"min": entry.cluster_passives, "max": entry.cluster_passives},
                    "disabled": False,
                }
            )
            notes.append(f"{entry.cluster_passives} passives")
        if entry.cluster_grant:
            opt = resolve_option_stat(
                "enchant.stat_3948993189",
                stat_entries,
                option_value=entry.cluster_grant,
                domain="enchant",
            )
            if opt is not None:
                stat_filters.append({"id": opt.id, "value": {"option": opt.option_id}, "disabled": False})
                notes.append(entry.cluster_grant)
        for notable in entry.cluster_notables:
            m = resolve_stat(f"1 Added Passive Skill is {notable}", explicit_entries, threshold=0.85)
            if m is not None:
                stat_filters.append({"id": m.id, "value": {}, "disabled": False})
                notes.append(notable)

    elif entry.kind == "watchers_eye":
        inner["name"] = "Watcher's Eye"
        if entry.parsed:
            for mod in entry.parsed.mods:
                if "while affected by" in mod.template:
                    flt = _stat_filter_from_mod(mod, stat_entries, domain="explicit", roll_scale=0.85, by_domain=by_domain)
                    if flt:
                        stat_filters.append(flt)
                        notes.append(mod.raw)

    elif entry.kind in ("forbidden_jewel", "radius_jewel"):
        inner["name"] = entry.name
        if entry.parsed:
            for mod in entry.parsed.mods:
                if mod.kind != "explicit":
                    continue
                opt = resolve_option_stat(mod.raw, stat_entries, domain="explicit")
                if opt is not None:
                    stat_filters.append({"id": opt.id, "value": {"option": opt.option_id}, "disabled": False})
                    notes.append(opt.option_text)
                    continue
                if "aura skills other than" in mod.template or "in radius also grant" in mod.template or "all elemental resistances" in mod.template:
                    flt = _stat_filter_from_mod(mod, stat_entries, domain="explicit", exact_value=True, by_domain=by_domain)
                    if flt:
                        stat_filters.append(flt)
                        notes.append(mod.raw)

    elif entry.kind == "timeless_jewel":
        inner["name"] = entry.name
        inner["type"] = "Timeless Jewel"
        if entry.timeless_seed is not None and entry.timeless_conqueror:
            stat_filters.append(
                {
                    "id": f"explicit.pseudo_timeless_jewel_{entry.timeless_conqueror}",
                    "value": {"min": entry.timeless_seed, "max": entry.timeless_seed},
                    "disabled": False,
                }
            )
            notes.append(f"Seed {entry.timeless_seed} ({entry.timeless_conqueror.title()})")

    else:
        cat = "jewel.abyss" if "eye jewel" in (entry.base_type or "").lower() else "jewel"
        filters_blk["type_filters"] = {"filters": {"category": {"option": cat}, "rarity": {"option": "rare"}}}
        stat_filters = _build_rare_filters(entry, stat_entries, max_stats=4, by_domain=by_domain)

    _apply_corrupted_jewel_constraints(entry, stat_entries, filters_blk, stat_filters, notes, by_domain=by_domain)
    stat_filters = _dedupe_filters(stat_filters)
    if stat_filters:
        inner["stats"] = [{"type": "and", "filters": stat_filters}]
    if filters_blk:
        inner["filters"] = filters_blk
    return {"query": inner, "sort": {"price": "asc"}}, notes


def build_item_trade_query(
    entry: ShoppingEntry,
    stat_entries: list[dict[str, Any]],
    *,
    status: str = "securable",
    by_domain: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build a complexity-safe official `/api/trade/search` JSON body for one ``ShoppingEntry``."""
    if entry.section == "jewel" and entry.kind != "unique":
        return _build_jewel_query(entry, stat_entries, status=status, by_domain=by_domain)

    inner: dict[str, Any] = {"status": {"option": status}}
    filters_blk: dict[str, Any] = {}
    stat_filters: list[dict[str, Any]] = []
    notes: list[str] = []

    if entry.kind == "gem":
        inner["type"] = entry.name
        filters_blk["type_filters"] = {"filters": {"category": {"option": "gem"}}}
        body: dict[str, Any] = {"query": inner, "sort": {"price": "asc"}}
        apply_exact_item_filters(
            body,
            gem_level=entry.gem_level,
            gem_quality=entry.gem_quality if (entry.gem_quality or 0) > 0 else None,
        )
        if entry.corrupted:
            inner.setdefault("filters", {}).setdefault("misc_filters", {}).setdefault("filters", {})[
                "corrupted"
            ] = {"option": "true"}
        notes.append(f"Lv{entry.gem_level}/{entry.gem_quality or 0}Q" + (" Corrupted" if entry.corrupted else ""))
        return body, notes

    if entry.kind == "magic_flask":
        inner["type"] = entry.base_type
        filters_blk["type_filters"] = {"filters": {"category": {"option": "flask"}}}
        if entry.parsed:
            for mod in entry.parsed.mods:
                if mod.kind == "explicit":
                    flt = _stat_filter_from_mod(mod, stat_entries, slot=entry.slot, domain="explicit", roll_scale=0.90, by_domain=by_domain)
                    if flt:
                        stat_filters.append(flt)
                        notes.append(mod.raw)
        stat_filters = _dedupe_filters(stat_filters)[:3]

    elif entry.kind == "unique":
        inner["name"] = entry.name
        if entry.base_type and entry.base_type.lower() != entry.name.lower():
            inner["type"] = entry.base_type
        stat_filters, notes = _build_unique_filters(entry, stat_entries, by_domain=by_domain)
        if entry.corrupted:
            filters_blk.setdefault("misc_filters", {}).setdefault("filters", {})["corrupted"] = {"option": "true"}

    else:  # rare gear
        base_slot = entry.slot.replace(" Swap", "")
        cat = category_for_slot(base_slot) or infer_weapon_category_from_base(entry.base_type)
        tf: dict[str, Any] = {"rarity": {"option": "rare"}}
        if cat:
            tf["category"] = {"option": cat}
        filters_blk["type_filters"] = {"filters": tf}
        if entry.parsed:
            misc_f: dict[str, Any] = {}
            if entry.parsed.fractured or any(m.kind == "fractured" for m in entry.parsed.mods):
                misc_f["fractured_item"] = {"option": "true"}
                notes.append("Fractured")
            for inf in entry.parsed.influences:
                inf_key = _INFLUENCE_FILTER_KEY.get(inf)
                if inf_key:
                    misc_f[inf_key] = {"option": "true"}
                    notes.append(inf)
            if misc_f:
                filters_blk["misc_filters"] = {"filters": misc_f}
        stat_filters = _build_rare_filters(entry, stat_entries, max_stats=5, by_domain=by_domain)

    if stat_filters:
        inner["stats"] = [{"type": "and", "filters": stat_filters}]
    if filters_blk:
        inner["filters"] = filters_blk
    body = {"query": inner, "sort": {"price": "asc"}}
    if entry.links >= 5 or entry.foulborn:
        apply_exact_item_filters(
            body,
            links=entry.links if entry.links >= 5 else None,
            foulborn=entry.foulborn,
        )
        if entry.links >= 5:
            notes.insert(0, f"{entry.links}L")
        if entry.foulborn:
            notes.insert(0, "Foulborn")

    # Enforce GGG complexity <= 160 by trimming trailing stat filters if ever exceeded
    while query_complexity(body) > COMPLEXITY_LIMIT and inner.get("stats"):
        grp_filters = inner["stats"][0].get("filters") or []
        if len(grp_filters) <= 1:
            break
        grp_filters.pop()
    return body, notes


def build_shopping_list(
    items_res: dict[str, Any],
    skills_res: dict[str, Any] | None,
    stat_entries: list[dict[str, Any]],
    *,
    league: str,
    realm: str = "global",
    status: str = "securable",
    include_gems: bool = True,
    pob_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate a complete L0 zero-HTTP shopping list for a loaded PoB build."""
    t0 = time.perf_counter()
    http_requests = 0
    eff_realm = normalize_realm(realm)
    eff_league = map_tw_league(league) if eff_realm == "tw" else (league.strip() or "Standard")
    report_warnings: list[str] = []
    if eff_realm == "tw" and eff_league and eff_league.isascii() and any(c.isalpha() for c in eff_league):
        report_warnings.append(
            f"TW realm unmapped ASCII league={eff_league!r}; pass Traditional Chinese league name via --league."
        )
    extracted = extract_build_shopping_entries(
        items_res,
        skills_res,
        include_gems=include_gems,
        pob_dir=pob_dir,
        warnings_out=report_warnings,
    )
    by_domain = _group_stats_by_domain(stat_entries)
    items_out: list[dict[str, Any]] = []
    anoint_out: dict[str, Any] | None = None

    for entry in extracted:
        if entry.kind == "anoint":
            anoint_out = {
                "slot": entry.slot,
                "notable": entry.anoint_notable,
                "oils": list(entry.anoint_oils),
            }
            continue
        query_body, notes = build_item_trade_query(entry, stat_entries, status=status, by_domain=by_domain)
        direct_url, query_id, query_param_url, localized_body = encode_trade_search_url(
            league, query_body, realm=eff_realm, pob_dir=pob_dir
        )
        item_warnings, defining_unresolved = collect_item_query_warnings(entry, localized_body, eff_realm=eff_realm)
        rec: dict[str, Any] = {
            "section": entry.section,
            "slot": entry.slot,
            "kind": entry.kind,
            "name": entry.name,
            "baseType": entry.base_type or None,
            "rarity": entry.rarity,
            "links": entry.links or None,
            "corrupted": entry.corrupted,
            "foulborn": entry.foulborn,
            "notes": notes,
            "url": direct_url,
            "encodedQueryId": query_id,
            "queryParamUrl": query_param_url,
            "complexity": query_complexity(localized_body),
            "query": localized_body,
        }
        if defining_unresolved:
            rec["definingModsUnresolved"] = True
        if item_warnings:
            rec["warnings"] = item_warnings
            report_warnings.append(f"{entry.slot} ({entry.name}): {'; '.join(item_warnings)}")
        if entry.kind == "gem":
            rec["gemLevel"] = entry.gem_level
            rec["gemQuality"] = entry.gem_quality
            rec["quantity"] = entry.quantity
            rec["isAwakened"] = entry.is_awakened
            rec["isTransfigured"] = entry.is_transfigured
            rec["isExceptional"] = entry.is_exceptional
        if entry.kind == "cluster_jewel":
            rec["clusterPassives"] = entry.cluster_passives
            rec["clusterGrant"] = entry.cluster_grant
            rec["clusterNotables"] = list(entry.cluster_notables)
        if entry.kind == "timeless_jewel":
            rec["timelessSeed"] = entry.timeless_seed
            rec["timelessConqueror"] = entry.timeless_conqueror
        items_out.append(rec)

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    by_section: dict[str, int] = {}
    for it in items_out:
        sec = str(it["section"])
        by_section[sec] = by_section.get(sec, 0) + 1

    out: dict[str, Any] = {
        "mode": "L0_zero_http",
        "realm": eff_realm,
        "league": eff_league,
        "status": status,
        "httpRequests": http_requests,
        "elapsedMs": elapsed_ms,
        "totalItems": len(items_out),
        "sectionCounts": by_section,
        "buildInfo": items_res.get("buildInfo") if isinstance(items_res, dict) else None,
        "anoint": anoint_out,
        "items": items_out,
    }
    if report_warnings:
        out["warnings"] = report_warnings
    return out
