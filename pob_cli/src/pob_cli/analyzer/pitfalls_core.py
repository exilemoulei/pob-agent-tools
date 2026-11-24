"""Core orchestrator for build mechanics, anti-synergy links, triggers, and defensive brick detection."""

from __future__ import annotations

from typing import Any

from ..formatters import strip_pob_escapes
from ..snapshot import _finite_float
from .pitfalls_defence import (
    _check_defensive_and_reservation_synergies,
    _check_defensive_bricks,
    _check_tincture_setup,
)
from .pitfalls_offence import (
    _check_dark_pact_echo_and_crit_acc,
    _check_main_group_anti_synergies,
    _check_main_group_sockets_and_swap,
    _check_resistance_inversion,
    _item_names_low,
)
from .pitfalls_sustain import _check_seven_link_and_class_synergies
from .pitfalls_triggers import _check_sword_of_ages_and_triggers
from .summary import _extract_global_cdr_sources


def _build_pitfall_context(
    *,
    stats: dict[str, Any],
    skills_res: dict[str, Any] | None,
    items_res: dict[str, Any] | None,
    cfg_res: dict[str, Any] | None,
    tree_summary: dict[str, Any] | None,
    equipped_slots_raw: list[dict[str, Any]] | None,
    alloc_node_stats: list[tuple[str, list[str]]] | None,
    trigger_attack_aps: float | None,
    warnings: list[str] | None,
    build_info: dict[str, Any] | None,
    stripped_flasks: list[str] | None,
) -> dict[str, Any]:
    groups = (skills_res or {}).get("groups") or []
    main_idx = (skills_res or {}).get("mainSocketGroup")
    items_by_id = {it["id"]: it for it in (items_res or {}).get("items", []) if isinstance(it, dict) and "id" in it}
    slots_list = (items_res or {}).get("slots") or []

    active_items_by_slot: dict[str, dict[str, Any]] = {}
    for s in slots_list:
        if not isinstance(s, dict):
            continue
        iid = s.get("selItemId")
        sname = str(s.get("name") or "")
        if not sname and s.get("nodeId") is not None:
            sname = f"Jewel {s.get('nodeId')}"
        if iid and not s.get("inactive") and "Swap" not in sname:
            active_items_by_slot[sname] = items_by_id.get(iid, {})

    raw_by_slot: dict[str, str] = {}
    for entry in equipped_slots_raw or []:
        sn = str(entry.get("slotName") or "")
        if sn and entry.get("raw"):
            raw_by_slot[sn] = str(entry["raw"])

    w1_item = active_items_by_slot.get("Weapon 1") or {}
    has_sword_of_ages = "the sword of ages" in _item_names_low(w1_item)

    ks_lower: set[str] = set()
    ks_ids: set[int] = set()
    asc_names_lower: set[str] = set()
    asc_ids: set[int] = set()
    asc_classes_lower: set[str] = set()
    if isinstance(tree_summary, dict):
        for ks in tree_summary.get("keystones") or []:
            if isinstance(ks, dict):
                nm = strip_pob_escapes(str(ks.get("name") or "")).strip().lower()
                if nm:
                    ks_lower.add(nm)
                nm_zh = strip_pob_escapes(str(ks.get("nameZh") or "")).strip().lower()
                if nm_zh:
                    ks_lower.add(nm_zh)
                if ks.get("id") is not None:
                    try:
                        ks_ids.add(int(ks["id"]))
                    except (TypeError, ValueError):
                        pass
            elif isinstance(ks, str) and ks.strip():
                ks_lower.add(strip_pob_escapes(ks).strip().lower())
        for an in tree_summary.get("ascendancyNodes") or []:
            if isinstance(an, dict):
                nm = strip_pob_escapes(str(an.get("name") or "")).strip().lower()
                if nm:
                    asc_names_lower.add(nm)
                nm_zh = strip_pob_escapes(str(an.get("nameZh") or "")).strip().lower()
                if nm_zh:
                    asc_names_lower.add(nm_zh)
                if an.get("id") is not None:
                    try:
                        asc_ids.add(int(an["id"]))
                    except (TypeError, ValueError):
                        pass
                asc_tag = strip_pob_escapes(str(an.get("asc") or an.get("ascendancyName") or "")).strip().lower()
                if asc_tag:
                    asc_classes_lower.add(asc_tag)
    for n_lbl, _ in alloc_node_stats or []:
        lbl_low = strip_pob_escapes(str(n_lbl or "")).strip().lower()
        if lbl_low:
            asc_names_lower.add(lbl_low)
            ks_lower.add(lbl_low)

    active_custom_lines: list[str] = []
    for blk in (cfg_res or {}).get("customMods") or []:
        if isinstance(blk, dict) and blk.get("enabled", True) is not False:
            for ln in str(blk.get("text") or "").splitlines():
                s_ln = strip_pob_escapes(ln).strip()
                if s_ln and not s_ln.startswith("#"):
                    active_custom_lines.append(s_ln)

    cdr_data = _extract_global_cdr_sources(
        alloc_node_stats or [],
        equipped_slots_raw or [],
        cfg_res,
    )

    all_active_non_swap_low: set[str] = set()
    for g in groups:
        if not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        for gm in g.get("gems") or []:
            if gm.get("enabled", True) and not gm.get("support"):
                for k in ("name", "nameSpec", "nameZh"):
                    val = strip_pob_escapes(str(gm.get(k) or "")).strip().lower()
                    if val:
                        all_active_non_swap_low.add(val)

    main_group = next(
        (g for g in groups if g.get("isMain") or (main_idx is not None and g.get("index") == main_idx)),
        None,
    )
    all_enabled_non_swap_gems: list[dict[str, Any]] = [
        gm
        for g in groups
        if isinstance(g, dict) and g.get("enabled", True) and "Swap" not in str(g.get("slot") or "")
        for gm in (g.get("gems") or [])
        if isinstance(gm, dict) and gm.get("enabled", True)
    ]

    return {
        "stats": stats,
        "skills_res": skills_res,
        "items_res": items_res,
        "cfg_res": cfg_res,
        "groups": groups,
        "main_idx": main_idx,
        "main_group": main_group,
        "main_mana_cost": _finite_float(stats.get("ManaCost"), default=-1.0),
        "items_by_id": items_by_id,
        "slots_list": slots_list,
        "active_items_by_slot": active_items_by_slot,
        "equipped_names_all": {sn: _item_names_low(it) for sn, it in active_items_by_slot.items()},
        "raw_by_slot": raw_by_slot,
        "alloc_node_stats": alloc_node_stats,
        "has_sword_of_ages": has_sword_of_ages,
        "ks_lower": ks_lower,
        "ks_ids": ks_ids,
        "asc_names_lower": asc_names_lower,
        "asc_ids": asc_ids,
        "asc_classes_lower": asc_classes_lower,
        "has_perfect_crime": "perfect crime" in asc_names_lower,
        "has_bloodsoaked_blade": "bloodsoaked blade" in ks_lower,
        "has_living_blade": "the living blade" in asc_names_lower,
        "has_toads_price": "toad's price" in asc_names_lower,
        "custom_mods_joined": "\n".join(active_custom_lines).lower(),
        "cdr_data": cdr_data,
        "global_cdr_pct": float(cdr_data["totalGlobalCdrPct"]),
        "all_active_non_swap_low": all_active_non_swap_low,
        "all_enabled_non_swap_gems": all_enabled_non_swap_gems,
        "trigger_attack_aps": trigger_attack_aps,
        "warnings": warnings,
        "build_info": build_info,
        "stripped_flasks": stripped_flasks,
    }


def _detect_build_mechanics_and_pitfalls(
    *,
    stats: dict[str, Any],
    skills_res: dict[str, Any] | None,
    items_res: dict[str, Any] | None,
    cfg_res: dict[str, Any] | None,
    tree_summary: dict[str, Any] | None = None,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
    alloc_node_stats: list[tuple[str, list[str]]] | None = None,
    trigger_attack_aps: float | None = None,
    warnings: list[str] | None = None,
    build_info: dict[str, Any] | None = None,
    stripped_flasks: list[str] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Detect high-impact PoE1 3.30 build mechanics, dead/anti-synergy links, triggers, & defensive bricks."""
    ctx = _build_pitfall_context(
        stats=stats,
        skills_res=skills_res,
        items_res=items_res,
        cfg_res=cfg_res,
        tree_summary=tree_summary,
        equipped_slots_raw=equipped_slots_raw,
        alloc_node_stats=alloc_node_stats,
        trigger_attack_aps=trigger_attack_aps,
        warnings=warnings,
        build_info=build_info,
        stripped_flasks=stripped_flasks,
    )
    gaps: list[dict[str, str]] = []
    mechanics: dict[str, Any] = {}
    core_synergies: list[str] = []
    amulet_7th_link: list[str] = []

    _check_main_group_anti_synergies(ctx, gaps)
    _check_main_group_sockets_and_swap(ctx, gaps)
    _check_sword_of_ages_and_triggers(ctx, gaps, mechanics)
    _check_resistance_inversion(ctx, gaps, mechanics)
    _check_tincture_setup(ctx, mechanics)
    _check_defensive_bricks(ctx, gaps, mechanics)
    _check_dark_pact_echo_and_crit_acc(ctx, gaps)
    _check_defensive_and_reservation_synergies(ctx, gaps, core_synergies)
    _check_seven_link_and_class_synergies(ctx, core_synergies, amulet_7th_link)

    if core_synergies:
        mechanics["coreSynergies"] = list(dict.fromkeys(core_synergies))
    if amulet_7th_link:
        mechanics["amulet7thLink"] = amulet_7th_link

    return gaps, mechanics
