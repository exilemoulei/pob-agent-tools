"""Defensive brick detectors, tincture awareness, and core do-not-brick build synergy guards."""

from __future__ import annotations

from typing import Any

from ..formatters import strip_pob_escapes
from ..snapshot import _finite_float
from .pitfalls_offence import _item_display, _item_names_low
from .summary import (
    _active_item_lines,
    _has_doryanis_prototype_equipped,
    _is_reservation_aura_gem,
    _is_tincture_item,
    _normalize_support_base_name,
)


def _gem_matches(gm: dict[str, Any], *, skill_ids: tuple[str, ...] = (), tokens: tuple[str, ...] = ()) -> bool:
    gsid = str(gm.get("skillId") or "")
    if gsid and gsid in skill_ids:
        return True
    combined = " ".join(
        strip_pob_escapes(str(gm.get(k) or ""))
        for k in ("name", "nameSpec", "nameZh")
    ).lower()
    return any(tok in combined for tok in tokens)


def _check_tincture_setup(ctx: dict[str, Any], mechanics: dict[str, Any]) -> None:
    items_by_id = ctx["items_by_id"]
    active_tinctures: list[str] = []
    for s in ctx["slots_list"]:
        if s.get("isFlask") and s.get("active") and s.get("selItemId"):
            it = items_by_id.get(s["selItemId"], {})
            if _is_tincture_item(it):
                active_tinctures.append(f"{s['name']}: {_item_display(it)}")
    belt_item = ctx["active_items_by_slot"].get("Belt") or {}
    has_screams_belt = "screams of the desiccated" in _item_display(belt_item).lower()
    has_bloodsoaked_blade = ctx["has_bloodsoaked_blade"]
    has_living_blade = ctx["has_living_blade"]
    has_toads_price = ctx["has_toads_price"]
    if has_bloodsoaked_blade or active_tinctures or has_screams_belt:
        t_limit = int(_finite_float(ctx["stats"].get("TinctureLimit"), default=1.0))
        mechanics["tinctureSetup"] = {
            "bloodsoakedBlade": has_bloodsoaked_blade,
            "theLivingBlade": has_living_blade,
            "toadsPrice": has_toads_price,
            "screamsOfTheDesiccated": has_screams_belt,
            "tinctureLimit": t_limit,
            "activeTinctureCount": len(active_tinctures),
            "activeTinctures": active_tinctures,
            "note": (
                f"Active Tinctures: {len(active_tinctures)}/{t_limit}"
                + (" (valid +2 limit from Reliquarian 'The Living Blade' / 活木之刃)" if has_living_blade else "")
                + ". CRITICAL PoE1 rule: **Tinctures are NOT Flasks** — `Screams of the Desiccated` "
                "('while affected by no Flasks') works 100% with active Tinctures"
                + (" (reinforced by `Toad's Price` disabling Utility Flasks)" if has_toads_price else "")
                + (
                    "; `Bloodsoaked Blade` sustains Tinctures via Life Regen against Weeping Wounds (not temporary flask water)."
                    if has_bloodsoaked_blade
                    else "."
                )
            ),
        }


def _check_defensive_bricks(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    stats = ctx["stats"]
    ks_lower = ctx["ks_lower"]
    equipped_names_all = ctx["equipped_names_all"]
    raw_by_slot = ctx["raw_by_slot"]
    items_by_id = ctx["items_by_id"]

    defensive_bricks: list[str] = []
    life_val = _finite_float(stats.get("Life"), default=0.0)
    life_unres = _finite_float(stats.get("LifeUnreserved"), default=life_val)
    life_unres_pct = (
        _finite_float(stats.get("LifeUnreservedPercent"), default=100.0)
        if stats.get("LifeUnreservedPercent") is not None
        else ((life_unres / life_val * 100.0) if life_val > 0 else 100.0)
    )
    mana_unres = _finite_float(stats.get("ManaUnreserved"), default=-1.0)
    mana_unres_pct = (
        _finite_float(stats.get("ManaUnreservedPercent"), default=100.0)
        if stats.get("ManaUnreservedPercent") is not None
        else 100.0
    )
    es_val = _finite_float(stats.get("EnergyShield"), default=0.0)
    chaos_res = _finite_float(stats.get("ChaosResist"), default=0.0)
    has_ci = ("chaos inoculation" in ks_lower) or (0.0 < life_val <= 1.0)
    has_eb = "eldritch battery" in ks_lower

    if has_eb and has_ci:
        msg = (
            "FATAL Keystone Brick — Eldritch Battery + Chaos Inoculation (EB + CI): "
            "CI sets Maximum Life to 1 while EB moves Energy Shield to protect Mana instead of Life, "
            "leaving 1 Life and 0 ES protecting your Life pool (instant death to any hit)!"
        )
        gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
        defensive_bricks.append(msg)

    is_low_life = (not has_ci) and life_val > 1.0 and (
        life_unres_pct <= 50.0 or (life_val > 0 and (life_unres / life_val) <= 0.50)
    )
    esh_slot = next(
        (sn for sn, nl in equipped_names_all.items() if "esh's visage" in nl or "艾许之面" in nl),
        None,
    )
    has_shavs = any("shavronne's wrappings" in nl or "薛朗的护身长袍" in nl for nl in equipped_names_all.values())
    has_solaris = any("solaris lorica" in nl or "烈炎之袍" in nl for nl in equipped_names_all.values())
    has_ivory = any("the ivory tower" in nl or "象牙之塔" in nl for nl in equipped_names_all.values())
    has_active_coruscating = any(
        s.get("isFlask")
        and s.get("active")
        and s.get("selItemId")
        and (
            "coruscating elixir" in _item_names_low(items_by_id.get(s["selItemId"], {}))
            or "闪耀精华" in _item_names_low(items_by_id.get(s["selItemId"], {}))
        )
        for s in ctx["slots_list"]
    )
    has_unconditional_chaos_shield = False
    for sn, raw_txt in raw_by_slot.items():
        if "Swap" in sn or sn == esh_slot:
            continue
        for ln in _active_item_lines(raw_txt):
            ll = ln.lower()
            if "chaos damage" in ll and "does not bypass energy shield" in ll and "low life" not in ll:
                has_unconditional_chaos_shield = True
                break

    if esh_slot is not None:
        esh_raw = raw_by_slot.get(esh_slot, "")
        esh_checks_low_mana = any("low mana" in ln.lower() for ln in _active_item_lines(esh_raw))
        is_low_mana = mana_unres_pct <= 35.0
        if is_low_life or (esh_checks_low_mana and is_low_mana):
            reason = (
                f"Low Life (LifeUnreserved={life_unres:.0f}/{life_val:.0f} = {life_unres_pct:.1f}% <= 50%)"
                if is_low_life
                else f"Low Mana (ManaUnreserved={mana_unres_pct:.1f}% <= 35%)"
            )
            msg = (
                f"CRITICAL Low-Life Brick — Esh's Visage ({esh_slot}: 'Chaos Damage taken does not bypass ES "
                f"while NOT on Low Life') is BROKEN because character is on {reason}! "
                "Chaos damage bypasses Energy Shield completely and one-shots unreserved Life! "
                "Fix: equip Shavronne's Wrappings, Solaris Lorica, The Ivory Tower (with unreserved Mana), "
                "100%-uptime Coruscating Elixir, or switch to Chaos Inoculation (CI)."
            )
            gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
            defensive_bricks.append(msg)
    elif (
        is_low_life
        and es_val > max(life_unres * 2.0, 500.0)
        and not (has_shavs or has_solaris or has_ivory or has_active_coruscating or has_unconditional_chaos_shield)
    ):
        msg = (
            f"Low-Life Chaos Bypass Risk: character is on Low Life ({life_unres:.0f}/{life_val:.0f} = "
            f"{life_unres_pct:.1f}% unreserved, ES {es_val:.0f}, Chaos Res {chaos_res:.0f}%) with NO active "
            "Chaos-bypass protection (Shavronne's Wrappings / Solaris Lorica / The Ivory Tower / Coruscating Elixir) — "
            "Chaos damage bypasses ES directly into low unreserved Life!"
        )
        gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
        defensive_bricks.append(msg)

    if has_ivory and is_low_life and mana_unres == 0.0:
        msg = (
            "The Ivory Tower Brick: 'Chaos Damage is taken from Mana before Life' provides ZERO protection "
            "when Unreserved Mana is 0 — leave enough Unreserved Mana to absorb Chaos hits!"
        )
        gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
        defensive_bricks.append(msg)

    has_dissolution = any(
        "dissolution of the flesh" in nl or "血肉溶解" in nl for nl in equipped_names_all.values()
    )
    if has_dissolution:
        diss_conflicts: list[str] = []
        if any("righteous fire" in nm for nm in ctx["all_active_non_swap_low"]):
            diss_conflicts.append("Righteous Fire (continuous self-burn prevents Dissolution 2s unreserve!)")
        if any("rathpith globe" in nl or "拉斯皮斯之球" in nl for nl in equipped_names_all.values()):
            diss_conflicts.append("Rathpith Globe (sacrifices 10% Life per cast)")
        all_enabled_sups_low = {
            _normalize_support_base_name(str(gm.get("name") or gm.get("nameSpec") or ""))
            for g in ctx["groups"]
            if g.get("enabled", True) and "Swap" not in str(g.get("slot") or "")
            for gm in (g.get("gems") or [])
            if gm.get("enabled", True) and gm.get("support")
        }
        if "sacrifice" in all_enabled_sups_low:
            diss_conflicts.append("Sacrifice Support (sacrifices 20% current Life per cast)")
        if "lifetap" in all_enabled_sups_low:
            diss_conflicts.append("Lifetap Support (spends Life directly while Life pool is reserved)")
        if diss_conflicts:
            msg = (
                "Dissolution of the Flesh conflict: reserves Life on damage (and removes all ES) until 2s "
                f"with no Life damage taken — conflicts with {'; '.join(diss_conflicts)}!"
            )
            gaps.append({"category": "DefensiveBrick", "severity": "HIGH", "message": msg})
            defensive_bricks.append(msg)

    if defensive_bricks:
        mechanics["defensiveBricks"] = defensive_bricks


def _check_defensive_and_reservation_synergies(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    core_synergies: list[str],
) -> None:
    equipped_names_all = ctx["equipped_names_all"]
    all_enabled_non_swap_gems = ctx["all_enabled_non_swap_gems"]

    if _has_doryanis_prototype_equipped(ctx["items_res"]):
        doryani_msg = (
            "[Doryani's Prototype] Negative Lightning Res (-200% cap) is INTENTIONAL: "
            "enemies within 6m share your Lightning Res (huge DPS multiplier) and Armour applies "
            "to Lightning Hits (stay within 6m; note Lightning DoT still bypasses Armour and is deadly!)."
        )
        core_synergies.append(doryani_msg)
        gaps.append({"category": "Mechanic", "severity": "INFO", "message": doryani_msg})

    has_the_agnostic = (
        19732 in ctx["ks_ids"]
        or "the agnostic" in ctx["ks_lower"]
        or "不可知论" in ctx["ks_lower"]
        or "不可知論" in ctx["ks_lower"]
    )
    has_valyrium = any(
        any(tok in nl for tok in ("valyrium", "女武神", "战日", "戰日"))
        for nl in equipped_names_all.values()
    )
    has_bloodnotch = any(
        any(tok in nl for tok in ("bloodnotch", "血痕", "缺血症"))
        for nl in equipped_names_all.values()
    )
    has_cws_anywhere = any(
        _gem_matches(
            gm,
            skill_ids=("SupportCastWhenStunned",),
            tokens=("cast when stunned", "晕眩时施放", "暈眩時施放"),
        )
        for gm in all_enabled_non_swap_gems
    )
    if has_the_agnostic and (has_valyrium or has_bloodnotch or has_cws_anywhere):
        core_synergies.append(
            "[DO NOT UNSPEC] The Agnostic + Valyrium/Bloodnotch/CWS: The Agnostic removes all ES (ES=0) "
            "so Valyrium's ES-based Stun Threshold = 0 AND removes inherent 50% ES Stun Avoidance! "
            "Unspeccing it (even with Blood Magic) BRICKS the build."
        )

    has_defiance_of_destiny = any(
        any(tok in nl for tok in ("defiance of destiny", "命运之抗争", "命運之抗爭", "拒絕命運", "拒绝命运"))
        for nl in equipped_names_all.values()
    )
    if has_defiance_of_destiny and (has_bloodnotch or has_cws_anywhere):
        core_synergies.append(
            "[CORE DEFENSE] Defiance of Destiny + Bloodnotch + Immutable Force forms an immortal "
            "hit-recovery loop; do NOT replace Defiance of Destiny with a damage amulet."
        )

    raw_mana_unres = ctx["stats"].get("ManaUnreserved")
    has_neg_mana_unres = (
        raw_mana_unres is not None
        and _finite_float(raw_mana_unres, default=0.0) < 0.0
    )
    has_res_engine_warn = any(
        (
            "reserv" in w.lower()
            or "保留" in w
            or ("mana" in w.lower() and ("negative" in w.lower() or "exceed" in w.lower()))
        )
        for w in (ctx["warnings"] or [])
    )
    if has_neg_mana_unres or has_res_engine_warn:
        unequipped_aura_groups: list[str] = []
        for g in ctx["groups"]:
            if not isinstance(g, dict) or not g.get("enabled", True):
                continue
            g_slot = str(g.get("slot") or "").strip()
            if g_slot not in ("", "-"):
                continue
            aura_names = [
                strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or gm.get("nameZh") or "")).strip()
                for gm in (g.get("gems") or [])
                if _is_reservation_aura_gem(gm)
            ]
            if aura_names:
                unequipped_aura_groups.append(f"Group #{g.get('index')} ({', '.join(aura_names)})")
        if unequipped_aura_groups:
            sim_msg = (
                f"[SIMULATED AURA RESERVATION] Unequipped (`slot: -`) reservation aura group(s) detected while "
                f"Mana is over-reserved: {', '.join(unequipped_aura_groups)}. These are likely "
                "Spectre/Party/Mercenary simulated auras whose reservation is erroneously charged to the player's "
                "mana pool in PoB — check disabling them before blaming or cutting player defensive auras "
                "(e.g. Petrified Blood)!"
            )
            core_synergies.append(sim_msg)
            gaps.append({"category": "Sustain", "severity": "MEDIUM", "message": sim_msg})
