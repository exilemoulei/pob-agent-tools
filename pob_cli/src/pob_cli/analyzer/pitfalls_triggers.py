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

NON_APS_TRIGGER_KEYS: frozenset[str] = frozenset({
    "automation", "autoexertion", "call to arms", "cast when damage taken", "cast when stunned",
    "cast on ward break", "cast on death", "cast on melee kill", "vengeance", "riposte", "reckoning",
    "counterattack", "mark on hit", "hextouch", "arcanist brand", "focus", "tempest shield", "shattershard",
    "svalinn cast on block", "festering resentment cast on block", "battlemage's cry", "combust",
    "seize the flesh", "intuitive link", "avenging flame", "hex on trap", "snipe", "trigger craft",
    "hidden blade", "unseen strike", "kitava's thirst", "foulborn kitava's thirst",
})

_OVERSPEED_TRIG_RE = re.compile(r"^Trigger Overspeed \(([^,)]+)(?:,\s*([^)]+))?\)")
_OVERSPEED_CDR_RE = re.compile(r"at\s+([0-9]+(?:\.[0-9]+)?)%\s+CDR")
_USED_MINION_RECENTLY_RE = re.compile(
    r"used\s+a\s+minion\s+skill\s+recently|近期(?:[内內]|有)?使用[了过過]?(?:召唤|召喚)(?:生物|物)技能|\boverseer['’]s\s+whip\b",
    re.IGNORECASE,
)
# PoB 2.69's Minion-typed skills that are neither `permanentMinion` nor Triggered (Data/Skills/act_*.lua,
# other.lua), less Herald of Purity, which is reserved, not cast: what a build uses again and again.
_HAND_CAST_MINION_SKILL_IDS: frozenset[str] = frozenset(
    s.lower()
    for s in (
        "AnimateWeapon", "AnimateWeaponAltX", "AnimateWeaponAltY", "VaalAnimateWeapon",
        "BlinkArrow", "BlinkArrowAltX", "BlinkArrowAltY", "MirrorArrow", "MirrorArrowAltX", "MirrorArrowAltY",
        "BodyswapAltX", "BoneOffering", "FleshOffering", "SpiritOffering", "BloodOffering", "DarkPact", "DeathWish",
        "RaiseSpectreAltX", "RaiseZombieAltY", "SummonRagingSpirit", "SummonRagingSpiritAltX",
        "SummonReaper", "SummonReaperAltX", "SummonReaperAltY",
        "SummonSkeletons", "SummonSkeletonsAltX", "SummonSkeletonsAltY", "VaalSummonSkeletons",
        "Absolution", "AbsolutionAltX", "VaalAbsolution", "DominatingBlow", "DominatingBlowAltX", "VaalDomination",
        "HolyStrike",
    )
)
# For a gem row that carries no skill id.
_COMBAT_MINION_TOKENS: tuple[str, ...] = (
    "summon raging spirit", "愤怒狂灵", "憤怒狂靈", "summon skeletons", "召唤魔卫", "召喚魔衛",
    "absolution", "赦罪", "dominating blow", "霸气之击", "霸氣之擊", "animate weapon", "幻化武器",
    "bodyswap of sacrifice", "牺牲之灵体转换", "献祭之移形换位", "獻祭之移形換位", "summon reaper", "召唤追命死灵",
    "blink arrow", "闪现射击", "閃現射擊", "mirror arrow", "魅影射击", "魅影射擊", "holy strike", "神圣打击",
    "flesh offering", "血肉奉献", "bone offering", "骸骨奉献", "spirit offering", "灵魂奉献",
    "dark bargain", "黑暗交易", "death wish", "死亡之愿", "咒死", "vaal domination",
    "raise spectre of transience", "raise zombie of falling",
)
_TRIGGER_SUPPORT_ID_PREFIXES = ("SupportCastOn", "SupportCastWh", "SupportAwakenedCast")


def canonical_trigger_key(name: str) -> str:
    """Normalize gem/item trigger names for cross-source deduplication."""
    s = strip_pob_escapes(str(name or "")).strip().lower()
    if s.startswith("awakened "):
        s = s[len("awakened ") :].strip()
    if s.endswith(" support"):
        s = s[: -len(" support")].strip()
    if s.startswith("the "):
        s = s[len("the ") :].strip()
    return s.replace("ö", "o")


def _fmt_overspeed_gap(disp: str, loc: Any, aps: float, cdr: float, tick: dict[str, Any]) -> dict[str, str]:
    nb = tick.get("nextBreakpoint") or {}
    nh = f" or reach {nb['requiredCdrPct']}% CDR ({nb['targetTicks']}t={nb['effectiveCdMs']}ms, cap {nb['effectiveCapPerSec']:.2f}/s)" if nb else ""
    return {
        "category": "TriggerOverspeed", "severity": "HIGH",
        "message": (
            f"Trigger Overspeed ({disp}, {loc}): Attack rate ({aps:.2f}/s) "
            f"exceeds trigger cap ({tick['effectiveCapPerSec']:.2f}/s at {cdr:g}% CDR, "
            f"{tick['ticks']}t = {tick['effectiveCdMs']}ms) — lower APS <= {tick['effectiveCapPerSec']:.2f}/s{nh}."
        ),
    }


def deduplicate_trigger_gaps(
    mech_gaps: list[dict[str, str]],
    agent_probe: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Suppress conflicting or duplicate `TriggerOverspeed` entries covered by `agent_probe`."""
    cliff_exceeded: set[str] = set()
    cliff_by_key: dict[str, dict[str, Any]] = {}
    if isinstance(agent_probe, dict):
        for cliff in agent_probe.get("triggerCliffs") or []:
            if not isinstance(cliff, dict):
                continue
            tkey = canonical_trigger_key(str(cliff.get("triggerName") or ""))
            if not tkey or tkey in NON_APS_TRIGGER_KEYS:
                continue
            if cliff.get("overspeedCliffExceeded"):
                cliff_exceeded.add(tkey)
            prev = cliff_by_key.get(tkey)
            if prev is None or _finite_float(cliff.get("currentIcdrPct"), default=-1.0) > _finite_float(
                prev.get("currentIcdrPct"), default=-1.0
            ):
                cliff_by_key[tkey] = cliff

    out: list[dict[str, str]] = []
    seen_trig_idx: dict[str, int] = {}
    for gap in mech_gaps:
        if gap.get("category") != "TriggerOverspeed":
            out.append(gap)
            continue
        msg = str(gap.get("message") or "")
        m_trig = _OVERSPEED_TRIG_RE.match(msg)
        tkey = canonical_trigger_key(m_trig.group(1)) if m_trig else ""
        if not tkey:
            out.append(gap)
            continue
        if tkey in NON_APS_TRIGGER_KEYS or tkey in cliff_exceeded:
            continue

        m_cdr = _OVERSPEED_CDR_RE.search(msg)
        msg_cdr = float(m_cdr.group(1)) if m_cdr else 0.0
        cliff_info = cliff_by_key.get(tkey)
        if cliff_info is not None:
            probe_cdr = _finite_float(cliff_info.get("currentIcdrPct"), default=-1.0)
            b_cd = _finite_float(cliff_info.get("baseActionCd"), default=0.25)
            src_aps = _finite_float(cliff_info.get("sourceAps"), default=0.0)
            if probe_cdr >= msg_cdr and b_cd > 0.0 and src_aps > 0.0:
                spells = max(1, int(_finite_float(cliff_info.get("linkedTriggeredSpells"), default=1.0)))
                tick = compute_server_tick_cooldown(b_cd, probe_cdr, aps=src_aps, triggered_spells=spells)
                if not tick.get("overspeed"):
                    continue
                disp = m_trig.group(1) if m_trig else tkey
                loc = m_trig.group(2) if (m_trig and m_trig.group(2)) else "-"
                gap = _fmt_overspeed_gap(disp, loc, src_aps, probe_cdr, tick)
                msg_cdr = probe_cdr

        if tkey in seen_trig_idx:
            prev_idx = seen_trig_idx[tkey]
            prev_m = _OVERSPEED_CDR_RE.search(str(out[prev_idx].get("message") or ""))
            prev_cdr = float(prev_m.group(1)) if prev_m else -1.0
            if msg_cdr >= prev_cdr:
                out[prev_idx] = gap
            continue
        seen_trig_idx[tkey] = len(out)
        out.append(gap)
    return out


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
            "equipped": True, "baseCooldownSec": 0.50, "globalCdrPct": global_cdr_pct,
            "cdrSources": cdr_data["sources"], "temporalMirageCooldown": tick_info,
            "hasPerfectCrime": has_perfect_crime, "hasTriggerbotCompensationCustomMod": has_43_comp,
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
                    f"(never continuously while held); requires tap-macro (interval >= {tick_info['effectiveCdMs']}ms "
                    "+ jitter margin) or discrete movement attacks (`Leap Slam`/`Flicker Strike`/`Consecrated Path`)."
                ),
            ],
        }
        mechanics["swordOfAges"] = soa_block
        if has_perfect_crime and not has_43_comp:
            gaps.append({
                "category": "PoBModeling", "severity": "MEDIUM",
                "message": (
                    "The Sword of Ages + Saboteur Perfect Crime: PoB 2.68.1 double-penalizes `0.7 * 0.7 = 0.49` by "
                    "stacking Perfect Crime `-30% less` on top of Temporal Mirage's own `-30% less` (live 3.30 only takes "
                    "Temporal Mirage's innate `-30% less`). Add `43% more Spell Damage` in customMods to cancel the second `-30%`."
                ),
            })

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
            glow, gsid, glvl = gnm.lower(), str(gm.get("skillId") or ""), int(gm.get("level") or 20)

            if "cast on critical strike" in glow or gsid in ("SupportCastOnCriticalStrike", "SupportAwakenedCastOnCriticalStrike"):
                is_awk = "awakened" in glow or gsid == "SupportAwakenedCastOnCriticalStrike"
                gem_cdr = _awakened_coc_gem_cdr(glvl) if is_awk else 0
                total_cdr = round(global_cdr_pct + gem_cdr, 2)
                num_spells = max(1, len(act_gems_in_g) - 1) if len(act_gems_in_g) >= 2 else 1
                aps_for_group = eff_attack_aps if is_mg else None
                tick = compute_server_tick_cooldown(0.15, total_cdr, aps=aps_for_group, triggered_spells=num_spells)
                triggers_detected.append({
                    "kind": "cooldown_trigger", "name": gnm, "groupIndex": g_idx, "slot": g.get("slot") or "-",
                    "baseCooldownSec": 0.15, "globalCdrPct": global_cdr_pct, "gemCdrPct": gem_cdr,
                    "totalCdrPct": total_cdr, "cdrSources": cdr_data["sources"],
                    "triggeredSpells": num_spells, "cooldown": tick,
                })
                if tick.get("overspeed") and aps_for_group is not None:
                    nb = tick.get("nextBreakpoint") or {}
                    nh = (
                        f" or reach next CDR breakpoint ({nb['requiredCdrPct']}% CDR -> {nb['targetTicks']}t = "
                        f"{nb['effectiveCdMs']}ms, cap {nb['effectiveCapPerSec']:.2f}/s)" if nb else ""
                    )
                    gaps.append({
                        "category": "TriggerOverspeed", "severity": "HIGH",
                        "message": (
                            f"Trigger Overspeed ({gnm}, Group #{g_idx}): Attack rate ({aps_for_group:.2f}/s) "
                            f"exceeds CoC trigger cap ({tick['effectiveCapPerSec']:.2f}/s at {total_cdr:g}% CDR, "
                            f"{tick['ticks']}t = {tick['effectiveCdMs']}ms) — attacks land inside cooldown and "
                            f"HALVE effective trigger rate! Either lower APS <= {tick['effectiveCapPerSec']:.2f}/s{nh}."
                        ),
                    })
            elif "cast while channelling" in glow or gsid in ("SupportCastWhileChannelling", "SupportAwakenedCastWhileChannelling"):
                iv_ms = _cwc_interval_ms(glvl, awakened=("awakened" in glow or gsid == "SupportAwakenedCastWhileChannelling"))
                triggers_detected.append({
                    "kind": "fixed_interval_channelling", "name": gnm, "groupIndex": g_idx,
                    "slot": g.get("slot") or "-", "level": glvl, "intervalMs": iv_ms,
                    "intervalSec": round(iv_ms / 1000.0, 3), "scaledByCdr": False,
                    "note": (
                        f"`{gnm}` (Lv.{glvl}) uses a **fixed channelling trigger interval of {iv_ms}ms "
                        f"({iv_ms / 1000.0:.2f}s)**, NOT a Cooldown — `Cooldown Recovery Rate (CDR)` has "
                        "**ZERO effect** on CwC trigger rate!"
                    ),
                })
            elif glow == "spellslinger" or gsid == "Spellslinger":
                tkey = f"spellslinger:{g_idx}"
                if tkey not in seen_trigger_keys:
                    seen_trigger_keys.add(tkey)
                    triggers_detected.append({
                        "kind": "cooldown_trigger", "name": "Spellslinger", "groupIndex": g_idx,
                        "slot": g.get("slot") or "-", "baseCooldownSec": 0.60, "globalCdrPct": global_cdr_pct,
                        "totalCdrPct": global_cdr_pct, "cooldown": compute_server_tick_cooldown(0.60, global_cdr_pct),
                    })
            elif is_mg and ("cast when damage taken" in glow or gsid == "SupportCastWhenDamageTaken"):
                triggers_detected.append({
                    "kind": "cooldown_trigger", "name": gnm, "groupIndex": g_idx,
                    "slot": g.get("slot") or "-", "baseCooldownSec": 0.25, "globalCdrPct": global_cdr_pct,
                    "totalCdrPct": global_cdr_pct, "cooldown": compute_server_tick_cooldown(0.25, global_cdr_pct),
                })

    _check_item_triggers(ctx, gaps, triggers_detected, seen_trigger_keys, eff_attack_aps)
    if triggers_detected:
        mechanics["triggers"] = triggers_detected
    _check_minion_convocation_trigger(ctx, gaps, mechanics)


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
                triggers_detected.append({
                    "kind": "item_cooldown_trigger", "name": disp_name, "slot": sname,
                    "baseCooldownSec": b_cd, "globalCdrPct": global_cdr_pct,
                    "totalCdrPct": global_cdr_pct, "cooldown": tick,
                })
                if is_atk_trig and tick.get("overspeed") and aps_for_item is not None:
                    gaps.append(_fmt_overspeed_gap(disp_name, sname, aps_for_item, global_cdr_pct, tick))


def _find_recent_minion_sources(ctx: dict[str, Any]) -> list[str]:
    out: list[str] = []
    c_blocks = ((ctx.get("cfg_res") or {}).get("customMods")) or []
    c_lines = [
        s for b in c_blocks if isinstance(b, dict) and b.get("enabled", True) is not False
        for ln in str(b.get("text") or "").splitlines()
        if (s := strip_pob_escapes(ln).strip()) and not s.startswith(("#", "//"))
    ]
    if _USED_MINION_RECENTLY_RE.search("\n".join(c_lines) if c_blocks else str(ctx.get("custom_mods_joined") or "")):
        out.append("customMods")

    act_it, raw_map = ctx.get("active_items_by_slot") or {}, ctx.get("raw_by_slot") or {}
    for sn in dict.fromkeys([*act_it.keys(), *raw_map.keys()]):
        if not sn or "Swap" in str(sn):
            continue
        it = act_it.get(sn) if isinstance(act_it.get(sn), dict) else {}
        blobs = [str(raw_map.get(sn) or "")]
        blobs.extend(strip_pob_escapes(str(it[k])) for k in ("raw", "title", "titleZh", "name", "nameZh") if it.get(k))
        for lk in ("implicits", "explicits", "mods", "craftedMods", "enchantMods", "fracturedMods"):
            if isinstance(it.get(lk), list):
                blobs.extend(strip_pob_escapes(str(v)) for v in it[lk] if v)
        if _USED_MINION_RECENTLY_RE.search("\n".join(blobs)):
            out.append(f"slot:{sn}")

    for n_lbl, st_lines in ctx.get("alloc_node_stats") or []:
        if _USED_MINION_RECENTLY_RE.search("\n".join([str(n_lbl or ""), *(str(x) for x in (st_lines or []))])):
            out.append(f"tree:{strip_pob_escapes(str(n_lbl or '')).strip() or 'passive'}")
    return list(dict.fromkeys(out))


def _check_minion_convocation_trigger(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    recent = _find_recent_minion_sources(ctx)
    raw_map = ctx.get("raw_by_slot") or {}
    trig_conv: list[tuple[Any, str, str]] = []
    man_conv: list[tuple[Any, str]] = []
    has_manual_combat = False

    for g in ctx.get("groups") or []:
        if not isinstance(g, dict) or not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        g_idx, g_slot = g.get("index"), str(g.get("slot") or "-")
        parsed = [
            (
                [t for k in ("name", "nameSpec", "nameZh", "skillId") if (t := strip_pob_escapes(str(gm.get(k) or "")).strip().lower())],
                str(gm.get("skillId") or ""),
                bool(gm.get("support")),
            )
            for gm in (g.get("gems") or [])
            if isinstance(gm, dict) and gm.get("enabled", True)
        ]
        slot_low = str(raw_map.get(g_slot) or "").lower()
        has_auto = any(gsid in ("Automation", "SupportAutomation") or any(t in ("automation", "automation support", "自动化", "自動化") for t in toks) for toks, gsid, _ in parsed)
        has_cwdt = any(gsid == "SupportCastWhenDamageTaken" or any("cast when damage taken" in t or "受伤时施放" in t or "受傷時施放" in t for t in toks) for toks, gsid, _ in parsed)
        has_other = any(p in slot_low for p in ("trigger a socketed", "trigger socketed", "触发插槽内")) or any(
            gsid.startswith(_TRIGGER_SUPPORT_ID_PREFIXES)
            or any(
                p in " ".join(toks)
                for p in ("cast when stunned", "cast on critical strike", "cast while channelling", "cast on melee kill", "cast on death", "cast on ward break", "arcanist brand", "spellslinger")
            )
            for toks, gsid, _ in parsed
        )
        is_trig = has_auto or has_cwdt or has_other
        t_lbl = "Automation Support" if has_auto else ("Cast when Damage Taken Support" if has_cwdt else "triggered socket")
        for toks, gsid, is_sup in parsed:
            joined = " ".join(toks)
            if gsid == "Convocation" or any(t in ("convocation", "号召", "號召") for t in toks):
                (trig_conv.append((g_idx, g_slot, t_lbl)) if is_trig else man_conv.append((g_idx, g_slot)))
            elif not is_sup and gsid not in ("Automation", "SupportAutomation"):
                known = gsid.lower() in _HAND_CAST_MINION_SKILL_IDS if gsid else any(tok in joined for tok in _COMBAT_MINION_TOKENS)
                if known and not is_trig:
                    has_manual_combat = True

    if recent and trig_conv and not man_conv and not has_manual_combat:
        f_idx, f_slot, f_trig = trig_conv[0]
        loc = f"Group #{f_idx}" if f_idx is not None else f_slot
        gaps.append({
            "category": "TriggerMechanic", "severity": "HIGH",
            "message": (
                f"Convocation + {f_trig} ({loc}): `{f_trig}` triggers `Convocation` rather than 'using' it, "
                f"failing `if you've used a Minion Skill Recently` ({', '.join(recent[:3])}, e.g. Wickerman "
                "`Greater Overseer's Whip` minion aggression) with no manual combat minion skill. "
                "Unlink `Automation Support` / trigger support and cast `Convocation` manually."
            ),
        })
        mechanics["minionSkillTrigger"] = {
            "status": "bricked_by_trigger",
            "automatedConvocationGroups": [i for i, _, _ in trig_conv],
            "hasUsedMinionSkillRecentlyMod": True,
            "recentMinionModSources": recent,
        }
    elif man_conv and recent and not has_manual_combat:
        # Only a build that carries the condition, and feeds it with nothing else, depends on this.
        f_idx, f_slot = man_conv[0]
        loc = f"Group #{f_idx}" if f_idx is not None else f_slot
        note = (
            f"Manual Convocation (unautomated, {loc}) is what sustains "
            f"`if you've used a Minion Skill Recently` ({', '.join(recent[:3])}, e.g. Wickerman "
            "`Greater Overseer's Whip` minion aggression). Do NOT link `Automation Support` / `CwDT` or remove `Convocation`!"
        )
        mechanics["minionSkillTrigger"] = {
            "status": "active_dependency",
            "manualConvocationGroups": [i for i, _ in man_conv],
            "hasUsedMinionSkillRecentlyMod": True,
            "recentMinionModSources": recent,
            "note": note,
        }
        mechanics.setdefault("coreSynergies", []).append(note)
