"""Sword of Ages, CoC/CwC/Spellslinger/CWDT, and unique item trigger cooldown detectors."""

from __future__ import annotations

import math
import re
from typing import Any

from ..formatters import strip_pob_escapes
from ..snapshot import _finite_float
from ..ticks import compute_server_tick_cooldown
from .pitfalls_offence import _item_names_low
from .summary import _awakened_coc_gem_cdr, _cwc_interval_ms


def _check_sword_of_ages_and_triggers(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    global_cdr_pct = ctx["global_cdr_pct"]
    cdr_data = ctx["cdr_data"]
    if ctx["has_sword_of_ages"]:
        tick_info = compute_server_tick_cooldown(0.50, global_cdr_pct)
        has_43_comp = bool(re.search(r"4[23](?:\.\d+)?%\s+more\s+spell\s+damage", ctx["custom_mods_joined"]))
        has_perfect_crime = ctx["has_perfect_crime"]
        soa_block: dict[str, Any] = {
            "equipped": True,
            "baseCooldownSec": 0.50,
            "globalCdrPct": global_cdr_pct,
            "cdrSources": cdr_data["sources"],
            "temporalMirageCooldown": tick_info,
            "hasPerfectCrime": has_perfect_crime,
            "hasTriggerbotCompensationCustomMod": has_43_comp,
            "advisories": [
                (
                    "Two separate `30% less` lines: Temporal Mirage clones DO take their own innate `30% less Damage` "
                    "(ALREADY modeled in PoB `other.lua` `SupportUniqueSuspendInTimeCanUse`), and do NOT take a SECOND "
                    "`-30% less` from Saboteur `Perfect Crime` (Triggerbots). PoB 2.68.1 omits the `0.50s` base cooldown "
                    "AND double-penalizes `0.7 * 0.7 = 0.49` instead of live `0.70` (`43% more Spell Damage` in customMods "
                    "cancels ONLY the erroneous second Perfect Crime penalty"
                    + ("; ALREADY PRESENT in customMods" if has_43_comp else ("; currently MISSING in customMods" if has_perfect_crime else ""))
                    + ")."
                ),
                (
                    "`on using a Movement Attack`: holding channelling `Cyclone` triggers ONLY ONCE on channel start "
                    "(never continuously while held); requires tap-macro (interval >= "
                    f"{tick_info['effectiveCdMs']}ms + jitter margin) or discrete movement attacks "
                    "(`Leap Slam`/`Flicker Strike`/`Consecrated Path`)."
                ),
            ],
        }
        mechanics["swordOfAges"] = soa_block
        if has_perfect_crime and not has_43_comp:
            gaps.append(
                {
                    "category": "PoBModeling",
                    "severity": "MEDIUM",
                    "message": (
                        "The Sword of Ages + Saboteur Perfect Crime: PoB 2.68.1 double-penalizes `0.7 * 0.7 = 0.49` by "
                        "stacking Perfect Crime `-30% less` on top of Temporal Mirage's own `-30% less` (live 3.30 only takes "
                        "Temporal Mirage's innate `-30% less`). Add `43% more Spell Damage` in customMods to cancel the second `-30%`."
                    ),
                }
            )

    triggers_detected: list[dict[str, Any]] = []
    seen_trigger_keys: set[str] = set()
    main_speed = _finite_float(ctx["stats"].get("Speed"), default=0.0)
    trigger_attack_aps = ctx["trigger_attack_aps"]
    eff_attack_aps = (
        float(trigger_attack_aps)
        if (trigger_attack_aps is not None and math.isfinite(float(trigger_attack_aps)) and float(trigger_attack_aps) > 0)
        else (main_speed if main_speed > 0 else None)
    )
    main_idx = ctx["main_idx"]

    for g in ctx["groups"]:
        if not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        g_idx = g.get("index")
        is_mg = bool(g.get("isMain")) or (main_idx is not None and g_idx == main_idx)
        g_gems = [gm for gm in (g.get("gems") or []) if gm.get("enabled", True)]
        act_gems_in_g = [gm for gm in g_gems if not gm.get("support")]

        for gm in g_gems:
            gnm = strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
            glow = gnm.lower()
            gsid = str(gm.get("skillId") or "")
            glvl = int(gm.get("level") or 20)

            if (
                "cast on critical strike" in glow
                or gsid in ("SupportCastOnCriticalStrike", "SupportAwakenedCastOnCriticalStrike")
            ):
                is_awk = "awakened" in glow or gsid == "SupportAwakenedCastOnCriticalStrike"
                gem_cdr = _awakened_coc_gem_cdr(glvl) if is_awk else 0
                total_cdr = round(global_cdr_pct + gem_cdr, 2)
                num_spells = max(1, len(act_gems_in_g) - 1) if len(act_gems_in_g) >= 2 else 1
                aps_for_group = eff_attack_aps if is_mg else None
                tick = compute_server_tick_cooldown(0.15, total_cdr, aps=aps_for_group, triggered_spells=num_spells)
                triggers_detected.append(
                    {
                        "kind": "cooldown_trigger",
                        "name": gnm,
                        "groupIndex": g_idx,
                        "slot": g.get("slot") or "-",
                        "baseCooldownSec": 0.15,
                        "globalCdrPct": global_cdr_pct,
                        "gemCdrPct": gem_cdr,
                        "totalCdrPct": total_cdr,
                        "cdrSources": cdr_data["sources"],
                        "triggeredSpells": num_spells,
                        "cooldown": tick,
                    }
                )
                if tick.get("overspeed") and aps_for_group is not None:
                    nb = tick.get("nextBreakpoint") or {}
                    next_hint = (
                        f" or reach next CDR breakpoint ({nb['requiredCdrPct']}% CDR -> {nb['targetTicks']}t = "
                        f"{nb['effectiveCdMs']}ms, cap {nb['effectiveCapPerSec']:.2f}/s)"
                        if nb
                        else ""
                    )
                    gaps.append(
                        {
                            "category": "TriggerOverspeed",
                            "severity": "HIGH",
                            "message": (
                                f"Trigger Overspeed ({gnm}, Group #{g_idx}): Attack rate ({aps_for_group:.2f}/s) "
                                f"exceeds CoC trigger cap ({tick['effectiveCapPerSec']:.2f}/s at {total_cdr:g}% CDR, "
                                f"{tick['ticks']}t = {tick['effectiveCdMs']}ms) — attacks land inside cooldown and "
                                f"HALVE effective trigger rate! Either lower APS <= {tick['effectiveCapPerSec']:.2f}/s{next_hint}."
                            ),
                        }
                    )
            elif (
                "cast while channelling" in glow
                or gsid in ("SupportCastWhileChannelling", "SupportAwakenedCastWhileChannelling")
            ):
                is_awk = "awakened" in glow or gsid == "SupportAwakenedCastWhileChannelling"
                iv_ms = _cwc_interval_ms(glvl, awakened=is_awk)
                triggers_detected.append(
                    {
                        "kind": "fixed_interval_channelling",
                        "name": gnm,
                        "groupIndex": g_idx,
                        "slot": g.get("slot") or "-",
                        "level": glvl,
                        "intervalMs": iv_ms,
                        "intervalSec": round(iv_ms / 1000.0, 3),
                        "scaledByCdr": False,
                        "note": (
                            f"`{gnm}` (Lv.{glvl}) uses a **fixed channelling trigger interval of {iv_ms}ms "
                            f"({iv_ms / 1000.0:.2f}s)**, NOT a Cooldown — `Cooldown Recovery Rate (CDR)` has "
                            "**ZERO effect** on CwC trigger rate!"
                        ),
                    }
                )
            elif glow == "spellslinger" or gsid == "Spellslinger":
                tkey = f"spellslinger:{g_idx}"
                if tkey not in seen_trigger_keys:
                    seen_trigger_keys.add(tkey)
                    tick = compute_server_tick_cooldown(0.60, global_cdr_pct)
                    triggers_detected.append(
                        {
                            "kind": "cooldown_trigger",
                            "name": "Spellslinger",
                            "groupIndex": g_idx,
                            "slot": g.get("slot") or "-",
                            "baseCooldownSec": 0.60,
                            "globalCdrPct": global_cdr_pct,
                            "totalCdrPct": global_cdr_pct,
                            "cooldown": tick,
                        }
                    )
            elif is_mg and ("cast when damage taken" in glow or gsid == "SupportCastWhenDamageTaken"):
                tick = compute_server_tick_cooldown(0.25, global_cdr_pct)
                triggers_detected.append(
                    {
                        "kind": "cooldown_trigger",
                        "name": gnm,
                        "groupIndex": g_idx,
                        "slot": g.get("slot") or "-",
                        "baseCooldownSec": 0.25,
                        "globalCdrPct": global_cdr_pct,
                        "totalCdrPct": global_cdr_pct,
                        "cooldown": tick,
                    }
                )

    _check_item_triggers(ctx, gaps, triggers_detected, seen_trigger_keys, eff_attack_aps)
    if triggers_detected:
        mechanics["triggers"] = triggers_detected


def _check_item_triggers(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    triggers_detected: list[dict[str, Any]],
    seen_trigger_keys: set[str],
    eff_attack_aps: float | None,
) -> None:
    global_cdr_pct = ctx["global_cdr_pct"]
    item_trigger_specs: tuple[tuple[str, str, float, bool], ...] = (
        ("cospri's malice", "Cospri's Malice", 0.25, True),
        ("mjölner", "Mjölner", 0.25, True),
        ("mjolner", "Mjölner", 0.25, True),
        ("the poet's pen", "The Poet's Pen", 0.25, True),
        ("the hidden blade", "The Hidden Blade", 0.50, False),
        ("kitava's thirst", "Kitava's Thirst", 0.10, False),
    )
    for sname, it in ctx["active_items_by_slot"].items():
        nm_low = _item_names_low(it)
        for pat, disp_name, b_cd, is_atk_trig in item_trigger_specs:
            if pat in nm_low and disp_name not in seen_trigger_keys:
                seen_trigger_keys.add(disp_name)
                aps_for_item = eff_attack_aps if is_atk_trig else None
                tick = compute_server_tick_cooldown(b_cd, global_cdr_pct, aps=aps_for_item)
                triggers_detected.append(
                    {
                        "kind": "item_cooldown_trigger",
                        "name": disp_name,
                        "slot": sname,
                        "baseCooldownSec": b_cd,
                        "globalCdrPct": global_cdr_pct,
                        "totalCdrPct": global_cdr_pct,
                        "cooldown": tick,
                    }
                )
                if is_atk_trig and tick.get("overspeed") and aps_for_item is not None:
                    nb = tick.get("nextBreakpoint") or {}
                    next_hint = (
                        f" or reach {nb['requiredCdrPct']}% CDR ({nb['targetTicks']}t={nb['effectiveCdMs']}ms, cap {nb['effectiveCapPerSec']:.2f}/s)"
                        if nb
                        else ""
                    )
                    gaps.append(
                        {
                            "category": "TriggerOverspeed",
                            "severity": "HIGH",
                            "message": (
                                f"Trigger Overspeed ({disp_name}, {sname}): Attack rate ({aps_for_item:.2f}/s) "
                                f"exceeds trigger cap ({tick['effectiveCapPerSec']:.2f}/s at {global_cdr_pct:g}% CDR, "
                                f"{tick['ticks']}t = {tick['effectiveCdMs']}ms) — lower APS <= {tick['effectiveCapPerSec']:.2f}/s{next_hint}."
                            ),
                        }
                    )
