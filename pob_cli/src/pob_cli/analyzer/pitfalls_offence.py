"""Offensive mechanic, support anti-synergy, socket/swap, and resistance-inversion pitfall detectors."""

from __future__ import annotations

from typing import Any

from ..formatters import strip_pob_escapes
from ..snapshot import _finite_float
from .constants import (
    _AILMENT_DEPENDENT_SUPPORTS,
    _ELE_AURA_NAMES,
    _ELE_CURSE_OR_EXPOSURE_NAMES,
    _ELE_OR_CHAOS_DAMAGE_SUPPORTS,
    _PURE_ELE_DAMAGE_SUPPORTS,
)
from .summary import (
    _active_item_lines,
    _count_item_sockets,
    _normalize_support_base_name,
)


def _item_display(it: dict[str, Any]) -> str:
    return strip_pob_escapes(str(it.get("title") or it.get("name") or "")).strip()


def _item_names_low(it: dict[str, Any]) -> str:
    return " ".join(
        strip_pob_escapes(str(it.get(k) or ""))
        for k in ("title", "titleZh", "name", "nameZh", "baseName", "baseNameZh")
    ).lower()


def _check_main_group_anti_synergies(ctx: dict[str, Any], gaps: list[dict[str, str]]) -> None:
    main_group = ctx["main_group"]
    if not main_group or not main_group.get("enabled", True):
        return
    stats = ctx["stats"]
    main_mana_cost = ctx["main_mana_cost"]
    has_sword_of_ages = ctx["has_sword_of_ages"]
    all_active_non_swap_low = ctx["all_active_non_swap_low"]

    mg_idx = main_group.get("index")
    mg_slot = str(main_group.get("slot") or "")
    mg_gems = [gm for gm in (main_group.get("gems") or []) if gm.get("enabled", True)]
    mg_names_low = {strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower() for gm in mg_gems}
    mg_skill_ids = {str(gm.get("skillId") or "") for gm in mg_gems}
    mg_sup_norms = {
        _normalize_support_base_name(str(gm.get("name") or gm.get("nameSpec") or ""))
        for gm in mg_gems
        if gm.get("support")
    }

    is_zero_mana_group = (main_mana_cost == 0.0) or (has_sword_of_ages and mg_slot == "Weapon 1")
    has_arcane_surge_in_main = "SupportArcaneSurge" in mg_skill_ids or "arcane surge" in mg_sup_norms
    if is_zero_mana_group and has_arcane_surge_in_main:
        gaps.append(
            {
                "category": "Mechanic",
                "severity": "HIGH",
                "message": (
                    f"Dead Arcane Surge Support in 0-mana triggered Group #{mg_idx} ({mg_slot or 'main'}): "
                    "Arcane Surge requires spending mana and NEVER procs on a 0-mana triggered skill; "
                    "PoB buffArcaneSurge=True is fake water. Move Arcane Surge to a mana-spending utility skill "
                    "(e.g. Arcane Cloak/Frostblink) and replace with a real damage support."
                ),
            }
        )

    has_inspiration_in_main = (
        "SupportInspiration" in mg_skill_ids
        or "inspiration" in mg_sup_norms
        or any(
            gm.get("support")
            and any(
                tok in strip_pob_escapes(str(gm.get(k) or "")).lower()
                for k in ("name", "nameSpec", "nameZh")
                for tok in ("inspiration", "启迪", "啟發")
            )
            for gm in mg_gems
        )
    )
    if is_zero_mana_group and has_inspiration_in_main:
        gaps.append(
            {
                "category": "Mechanic",
                "severity": "HIGH",
                "message": (
                    f"Dead Inspiration Support in 0-mana Group #{mg_idx} ({mg_slot or 'main'}): "
                    "Inspiration Charges are gained only when supported skills spend mana on upfront costs, "
                    "so a 0-mana main skill generates none unless another skill in this group spends mana — "
                    "PoB max-charge assumption is fake water! Spend mana on a linked/supported skill or swap support."
                ),
            }
        )
    elif has_inspiration_in_main and main_mana_cost >= 400.0:
        gaps.append(
            {
                "category": "Sustain",
                "severity": "HIGH" if main_mana_cost >= 700.0 else "MEDIUM",
                "message": (
                    f"Inspiration Support mana-reset trap: single cast costs {round(main_mana_cost)} Mana "
                    "vs Lv20 800-Mana charge reset threshold (Lv21: 922) — charges reset every 1-2 casts, "
                    "so live DPS is far lower than PoB's assumed 5 Inspiration Charges!"
                ),
            }
        )

    if "elemental focus" in mg_sup_norms:
        ailment_conflicts = sorted(mg_sup_norms & _AILMENT_DEPENDENT_SUPPORTS)
        if ailment_conflicts:
            c_disp = ", ".join(x.title() for x in ailment_conflicts)
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "HIGH",
                    "message": (
                        f"Anti-synergy in Group #{mg_idx}: Elemental Focus prevents the supported skill from "
                        f"inflicting Elemental Ailments (Chill/Freeze/Ignite/Shock), disabling linked {c_disp} "
                        "unless a separate skill/ground effect reliably applies the ailment (verify PoB enemy "
                        "Chilled/Ignited config is NOT fake water!)."
                    ),
                }
            )

    if "brutality" in mg_sup_norms:
        brut_sup_conflicts = sorted(mg_sup_norms & _ELE_OR_CHAOS_DAMAGE_SUPPORTS)
        brut_aura_conflicts = sorted(all_active_non_swap_low & _ELE_AURA_NAMES)
        if brut_sup_conflicts or brut_aura_conflicts:
            parts = [x.title() for x in brut_sup_conflicts] + [f"{x.title()} (aura)" for x in brut_aura_conflicts]
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "HIGH",
                    "message": (
                        f"Anti-synergy in Group #{mg_idx}: Brutality Support causes supported skill to deal NO "
                        f"Elemental or Chaos damage, nullifying {', '.join(parts)}!"
                    ),
                }
            )

    if "void manipulation" in mg_sup_norms:
        vm_conflicts = sorted(mg_sup_norms & _PURE_ELE_DAMAGE_SUPPORTS)
        if vm_conflicts:
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "HIGH",
                    "message": (
                        f"Anti-synergy in Group #{mg_idx}: Void Manipulation prevents Elemental damage, "
                        f"nullifying {', '.join(x.title() for x in vm_conflicts)}!"
                    ),
                }
            )

    if "controlled destruction" in mg_sup_norms:
        has_coc_main = "cast on critical strike" in mg_sup_norms
        crit_val = _finite_float(stats.get("CritChance"), default=0.0)
        if has_coc_main:
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "HIGH",
                    "message": (
                        f"Anti-synergy in Group #{mg_idx}: Controlled Destruction (80% less Crit Chance) "
                        "bricks Cast On Critical Strike trigger reliability!"
                    ),
                }
            )
        elif crit_val >= 35.0:
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "MEDIUM",
                    "message": (
                        f"Caveat in Group #{mg_idx}: Controlled Destruction imposes 80% less Critical Strike Chance "
                        f"on a crit build (current CritChance={crit_val:.1f}%)."
                    ),
                }
            )

    if "immolate" in mg_sup_norms and "elemental focus" not in mg_sup_norms:
        ignite_ch = _finite_float(stats.get("IgniteChance"), default=-1.0)
        has_burn_skill = any(
            k in nm
            for nm in all_active_non_swap_low
            for k in ("righteous fire", "flame wall", "scorching ray", "searing bond", "fire trap")
        )
        if ignite_ch == 0.0 and not has_burn_skill:
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "MEDIUM",
                    "message": (
                        f"Conditional Support in Group #{mg_idx}: Immolate requires enemies to be Burning/Ignited, "
                        "but build has 0% Ignite Chance and no burning-ground skill."
                    ),
                }
            )

    if any("firestorm of pelting" in nm for nm in mg_names_low) and any(
        "greater spell cascade" in nm or "spell cascade" in nm for nm in mg_names_low
    ):
        gaps.append(
            {
                "category": "Mechanic",
                "severity": "HIGH",
                "message": (
                    f"Group #{mg_idx} links Firestorm of Pelting with Spell Cascade / Greater Spell Cascade: "
                    "all cascades share Firestorm of Pelting's global 12-storm cap (hurts single-target DPS "
                    "while adding Less Area Damage penalty)."
                ),
            }
        )


def _check_main_group_sockets_and_swap(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any] | None = None,
) -> None:
    main_group = ctx["main_group"]
    groups = ctx["groups"]
    raw_by_slot = ctx["raw_by_slot"]
    mg_slot = str(main_group.get("slot") or "") if main_group and main_group.get("enabled", True) else ""

    if main_group and main_group.get("enabled", True):
        mg_idx = main_group.get("index")
        mg_gems = [gm for gm in (main_group.get("gems") or []) if gm.get("enabled", True)]

        for gm in mg_gems:
            if gm.get("fromItem") or not gm.get("support"):
                continue
            g_lvl = int(gm.get("level") or 0)
            g_name = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
            g_low = g_name.lower()
            if g_lvl == 1 and not any(
                ex in g_low
                for ex in ("empower", "enlighten", "enhance", "cast when damage taken", "arcane surge", "culling strike", "invert the rules")
            ):
                gaps.append(
                    {
                        "category": "Skills",
                        "severity": "MEDIUM",
                        "message": f"Main Group #{mg_idx} has Level 1 support gem '{g_name}' (check if under-leveled).",
                    }
                )

        if mg_slot and mg_slot in raw_by_slot:
            # Gem sockets only: abyssal (A) and Delve (D) sockets never hold gems.
            total_socks = _count_item_sockets(raw_by_slot[mg_slot])
            if total_socks is not None and total_socks >= 5:
                used_socks = sum(
                    1
                    for g in groups
                    if g.get("slot") == mg_slot and not g.get("source")
                    for gm in (g.get("gems") or [])
                    if not gm.get("fromItem")
                )
                if used_socks < total_socks:
                    gaps.append(
                        {
                            "category": "Skills",
                            "severity": "HIGH",
                            "message": (
                                f"Main skill item '{mg_slot}' has {total_socks} sockets but only {used_socks} gem(s) socketed "
                                f"({total_socks - used_socks} empty socket(s))!"
                            ),
                        }
                    )

    main_gem_count = (
        sum(1 for gm in (main_group.get("gems") or []) if isinstance(gm, dict) and gm.get("enabled", True))
        if main_group and main_group.get("enabled", True)
        else 0
    )
    lvl_toks = ("empower", "enlighten", "enhance", "awakened")
    lvl_entries: list[dict[str, Any]] = []

    def _gn(gm: dict[str, Any]) -> str:
        return strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()

    def _add_lvl(g: dict[str, Any], g_slot: str, gems: list[dict[str, Any]]) -> None:
        lvl_entries.append({"group": g.get("index"), "slot": g_slot, "count": len(gems), "gems": [_gn(gm) for gm in gems]})

    for g in groups:
        g_slot = str(g.get("slot") or "")
        if "Swap" not in g_slot:
            continue
        g_gems = [gm for gm in (g.get("gems") or []) if not gm.get("fromItem")]
        if not g_gems:
            continue
        sup_gems = [gm for gm in g_gems if gm.get("support")]
        act_gems = [gm for gm in g_gems if not gm.get("support")]
        if sup_gems and not act_gems:
            norms = [_gn(gm).lower() for gm in sup_gems]
            if (
                main_gem_count >= 6
                or len(norms) > len(set(norms))
                or any(not gm.get("enabled", True) for gm in sup_gems)
                or any(tok in nm for nm in norms for tok in lvl_toks)
            ):
                _add_lvl(g, g_slot, sup_gems)
                continue
            names_s = ", ".join(strip_pob_escapes(str(gm.get("nameZh") or gm.get("name") or "")) for gm in sup_gems)
            gaps.append({
                "category": "Skills",
                "severity": "INFO",
                "message": f"Orphan support gem(s) in '{g_slot}' (Group #{g.get('index')}: {names_s}) — inactive in weapon swap (verify if leveling or left out of main link).",
            })
        elif any("archmage" in _gn(gm).lower() for gm in sup_gems):
            names_s = ", ".join(strip_pob_escapes(str(gm.get("nameZh") or gm.get("name") or "")) for gm in g_gems)
            gaps.append({
                "category": "Skills",
                "severity": "LOW",
                "message": f"Core support gem (Archmage) sitting in '{g_slot}' (Group #{g.get('index')}: {names_s}) — inactive in weapon swap.",
            })

    am_it = (ctx.get("active_items_by_slot") or {}).get("Amulet") or {}
    am_low = f"{_item_display(am_it)} {raw_by_slot.get('Amulet') or ''}".lower()
    has_vow = any(t in am_low for t in ("uul-netol's vow", "乌尔尼多的誓言", "烏爾尼多的誓言"))
    slot_has_active = {
        str(g.get("slot") or "")
        for g in groups
        if g.get("slot") and "Swap" not in str(g.get("slot") or "")
        and (g.get("source") or any(not gm.get("support") for gm in (g.get("gems") or []) if isinstance(gm, dict)))
    }
    for g in groups:
        g_slot = str(g.get("slot") or "")
        if not g_slot or "Swap" in g_slot or g_slot in slot_has_active or not g.get("enabled", True):
            continue
        if g_slot == "Amulet" and has_vow:
            continue
        raw_g = [gm for gm in (g.get("gems") or []) if isinstance(gm, dict) and not gm.get("fromItem")]
        g_gems = [gm for gm in raw_g if gm.get("enabled", True)]
        if g_gems and all(gm.get("support") for gm in g_gems):
            g_norm = [_gn(gm).lower() for gm in g_gems]
            if (
                g_slot == "Weapon 2"
                and mg_slot in ("Body Armour", "Weapon 1")
                and main_gem_count >= 6
                and (
                    len(g_norm) > len(set(g_norm))
                    or any(not gm.get("enabled", True) for gm in raw_g)
                    or all(any(tok in nm for tok in lvl_toks) for nm in g_norm)
                )
            ):
                _add_lvl(g, g_slot, g_gems)
                continue
            names_s = ", ".join(strip_pob_escapes(str(gm.get("nameZh") or gm.get("name") or "")) for gm in g_gems)
            gaps.append({
                "category": "Skills",
                "severity": "HIGH",
                "message": f"Orphan support gem(s) in '{g_slot}' (Group #{g.get('index')}: {names_s}) with no active skill in slot!",
            })
    if lvl_entries and mechanics is not None:
        mechanics["swapLevelingGems"] = {"count": sum(int(e["count"]) for e in lvl_entries), "groups": lvl_entries}


def _check_resistance_inversion(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    invert_sources: list[str] = []
    for g in ctx["groups"]:
        if not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        for gm in g.get("gems") or []:
            if not gm.get("enabled", True):
                continue
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
            if "invert the rules" in gnm.lower() or str(gm.get("skillId") or "") == "SupportInvertTheRules":
                invert_sources.append(f"Group #{g.get('index')} Invert the Rules Support")
    for sname, raw_txt in ctx["raw_by_slot"].items():
        if "Swap" in sname:
            continue
        for ln in _active_item_lines(raw_txt):
            if "resistance values as inverted" in ln.lower() or "resistances as inverted" in ln.lower():
                it_nm = _item_display(ctx["active_items_by_slot"].get(sname) or {}) or sname
                invert_sources.append(f"{sname} ({it_nm}): {strip_pob_escapes(ln)}")
    for n_lbl, n_stats in ctx["alloc_node_stats"] or []:
        for ln in n_stats:
            if "resistance values as inverted" in str(ln).lower() or "resistances as inverted" in str(ln).lower():
                invert_sources.append(f"Tree ({n_lbl}): {strip_pob_escapes(ln)}")

    if not invert_sources:
        return

    curse_hits_active: list[str] = []
    curse_hits_swap: list[str] = []
    for g in ctx["groups"]:
        if not g.get("enabled", True):
            continue
        is_swap = "Swap" in str(g.get("slot") or "")
        for gm in g.get("gems") or []:
            if not gm.get("enabled", True):
                continue
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
            if gnm.lower() in _ELE_CURSE_OR_EXPOSURE_NAMES:
                label = f"{gnm} (Group #{g.get('index')} {g.get('slot') or '-'})"
                if is_swap:
                    curse_hits_swap.append(label)
                else:
                    curse_hits_active.append(label)
    from .pitfalls_ag import find_ag_exposure_conflicts

    ag_exposure_hits = find_ag_exposure_conflicts(ctx, invert_sources)
    mechanics["resistanceInversion"] = {
        "sources": invert_sources,
        "conflictingActiveCursesOrExposure": curse_hits_active,
        "conflictingSwapCursesOrExposure": curse_hits_swap,
        "conflictingAgExposure": ag_exposure_hits,
        "note": (
            "Lowering enemy elemental resistance via Curses/Exposure BEFORE inversion shrinks the inverted "
            "negative-resistance bonus; Elemental Penetration applies AFTER inversion and still works fully."
        ),
    }
    if curse_hits_active or ag_exposure_hits:
        all_conflicts = curse_hits_active + ag_exposure_hits
        gaps.append(
            {
                "category": "Mechanic",
                "severity": "HIGH",
                "message": (
                    f"Resistance Inversion anti-synergy: {', '.join(invert_sources[:2])} is active alongside "
                    f"resistance-lowering source(s) {', '.join(all_conflicts)} — lowering enemy resistance before "
                    "inversion REDUCES inverted bonus (use Elemental Penetration instead, which applies after inversion)."
                ),
            }
        )
    elif curse_hits_swap:
        gaps.append(
            {
                "category": "Mechanic",
                "severity": "LOW",
                "message": (
                    f"Resistance Inversion note ({invert_sources[0]}): do NOT pair with elemental curses/exposure "
                    f"(seen in swap: {', '.join(curse_hits_swap)}); Penetration still works after inversion."
                ),
            }
        )


def _check_dark_pact_echo_and_crit_acc(ctx: dict[str, Any], gaps: list[dict[str, str]]) -> None:
    for g in ctx["groups"]:
        if not g.get("enabled", True) or not g.get("includeInFullDPS"):
            continue
        g_gems = [gm for gm in (g.get("gems") or []) if gm.get("enabled", True)]
        echo_gem = next(
            (
                strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
                for gm in g_gems
                if gm.get("support")
                and "spell echo" in strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).lower()
            ),
            None,
        )
        if not echo_gem:
            continue
        for gm in g_gems:
            if gm.get("support"):
                continue
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or ""))
            cnt = int(gm.get("count") or 1)
            if cnt > 1 and any(k in gnm.lower() for k in ("dark bargain", "dark pact")):
                gaps.append(
                    {
                        "category": "PoBWater",
                        "severity": "HIGH",
                        "message": (
                            f"Group #{g.get('index')} '{gnm}' has count={cnt} with '{echo_gem}' enabled (includeInFullDPS=True): "
                            f"PoB already models Spell Echo repeats in skill DPS, so count={cnt} double-counts FullDPS by {cnt}x!"
                        ),
                    }
                )

    stats = ctx["stats"]
    raw_hc = stats.get("HitChance")
    crit_ch = _finite_float(stats.get("CritChance"), default=0.0)
    mh_acc = _finite_float(stats.get("MainHandAccuracy"), default=0.0)
    if raw_hc is not None:
        hc_val = _finite_float(raw_hc, default=100.0)
        if hc_val < 99.0 and crit_ch >= 25.0 and mh_acc > 0.0:
            eff_hit_sq = (hc_val / 100.0) ** 2 * 100.0
            gaps.append(
                {
                    "category": "Offence",
                    "severity": "MEDIUM",
                    "message": (
                        f"Crit Attack Accuracy Double-Roll: Hit Chance is {hc_val:.1f}% (< 99%) with "
                        f"{crit_ch:.1f}% Crit Chance — PoE1 rolls Accuracy TWICE to confirm critical strikes "
                        f"(effective crit hit factor ~{eff_hit_sq:.1f}%). Cap Accuracy to 100%!"
                    ),
                }
            )
