"""PoE1 3.30 mechanic audits: Pact skills, Overloaded Intensity ramp, and under-configured ('negative water') checks (L2)."""

from __future__ import annotations

import re
from typing import Any

from ..formatters import strip_pob_escapes
from .dewater_mechanics import find_on_hit_charge_sources, is_proxy_main_skill
from .summary import _active_item_lines

_WATCHERS_EYE_AURAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Purity of Elements", ("purity of elements", "元素净化", "元素淨化")),
    ("Purity of Fire", ("purity of fire", "火焰净化", "火焰淨化")),
    ("Purity of Ice", ("purity of ice", "冰霜净化", "冰霜淨化")),
    ("Purity of Lightning", ("purity of lightning", "闪电净化", "閃電淨化")),
    ("Determination", ("determination", "坚定", "堅定")),
    ("Discipline", ("discipline", "纪律", "紀律")),
    ("Malevolence", ("malevolence", "怨毒")),
    ("Precision", ("precision", "精准", "精準")),
    ("Vitality", ("vitality", "活力")),
    ("Zealotry", ("zealotry", "狂热", "狂熱")),
    ("Clarity", ("clarity", "清晰")),
    ("Hatred", ("hatred", "憎恨")),
    ("Anger", ("anger", "愤怒", "憤怒")),
    ("Grace", ("grace", "优雅", "優雅")),
    ("Haste", ("haste", "迅捷")),
    ("Pride", ("pride", "骄傲", "驕傲")),
    ("Wrath", ("wrath", "雷霆")),
)
_WE_COND_RE = re.compile(
    r"\b(?:affected\s+by|using)\b|受[^，。；\n]+?影响|受[^，。；\n]+?影響|使用[^，。；\n]+?时|使用[^，。；\n]+?時",
    re.IGNORECASE,
)
_CHARGE_CANONICAL: dict[str, str] = {
    "useendurancecharges": "useEnduranceCharges",
    "usefrenzycharges": "useFrenzyCharges",
    "usepowercharges": "usePowerCharges",
}


def _cfg_map_low(cfg_res: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sec in (cfg_res or {}).get("sections") or []:
        if not isinstance(sec, dict):
            continue
        for item in sec.get("items") or []:
            if isinstance(item, dict) and item.get("var"):
                out[str(item["var"]).lower()] = item.get("value")
    return out


def _party_text_and_active(party_res: dict[str, Any] | None) -> tuple[str, bool]:
    if not isinstance(party_res, dict):
        return "", False
    fields = party_res.get("fields") if isinstance(party_res.get("fields"), dict) else {}
    parts = [str(v).strip() for v in fields.values() if str(v or "").strip()]
    has_party = bool(parts)
    return "\n".join(parts).lower(), has_party


def _gem_names_low(gm: dict[str, Any]) -> str:
    return " ".join(
        strip_pob_escapes(str(gm.get(k) or ""))
        for k in ("name", "nameSpec", "nameZh", "skillId")
    ).lower()


def _is_pact_buff_gem(gm: dict[str, Any]) -> bool:
    if not isinstance(gm, dict) or gm.get("support") or not gm.get("enabled", True):
        return False
    blob = _gem_names_low(gm)
    if "dark pact" in blob or "黑暗契约" in blob or "黑暗契約" in blob:
        return False
    return (
        "pact of " in blob
        or "pactof" in blob
        or "beidat" in blob
        or "贝达特契约" in blob
        or "貝達特契約" in blob
    )


def _is_automation_gem(gm: dict[str, Any]) -> bool:
    if not isinstance(gm, dict) or not gm.get("enabled", True):
        return False
    for k in ("name", "nameSpec", "nameZh", "skillId"):
        v = strip_pob_escapes(str(gm.get(k) or "")).strip().lower()
        if v in ("automation", "automation support", "supportautomation", "自动化", "自動化"):
            return True
    return False


def _check_pact_skills(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    cfg_low = _cfg_map_low(ctx.get("cfg_res"))
    all_eq_blob = " ".join((ctx.get("equipped_names_all") or {}).values())
    has_unholy_accomplice = any(
        tok in all_eq_blob for tok in ("unholy accomplice", "不洁帮凶", "不潔幫兇")
    )
    pact_mode = str(cfg_low.get("pactmode") or "")
    pact_entries: list[dict[str, Any]] = []

    for g in ctx.get("groups") or []:
        if not isinstance(g, dict) or not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        gems = [gm for gm in (g.get("gems") or []) if isinstance(gm, dict) and gm.get("enabled", True)]
        pact_gems = [gm for gm in gems if _is_pact_buff_gem(gm)]
        if not pact_gems:
            continue
        has_auto = any(_is_automation_gem(gm) for gm in gems)
        g_idx = g.get("index")
        g_slot = str(g.get("slot") or "-")
        for pg in pact_gems:
            p_name = strip_pob_escapes(str(pg.get("name") or pg.get("nameSpec") or "Pact")).strip()
            pact_entries.append(
                {
                    "group": g_idx,
                    "slot": g_slot,
                    "name": p_name,
                    "linkedWithAutomation": has_auto,
                    "hasUnholyAccomplice": has_unholy_accomplice,
                    "pactMode": pact_mode or None,
                }
            )
            if has_auto:
                gaps.append(
                    {
                        "category": "Mechanic",
                        "severity": "HIGH",
                        "message": (
                            f"Group #{g_idx} ({g_slot}) links {p_name} with Automation: Pact skills have a "
                            "non-instant cast time (~0.8s, no SkillType.Instant) and CANNOT be triggered by "
                            "Automation — manual self-cast is required (remove Automation or move to an Instant skill)."
                        ),
                    }
                )
            notes: list[str] = [
                "periodic manual self-cast animation lock (~0.8s base cast time unmodeled on main skill)"
            ]
            if pact_mode.upper() == "MAX":
                notes.append("pactMode=MAX assumes peak uptime")
            if not has_unholy_accomplice:
                notes.append(
                    "Unholy Accomplice belt is not equipped so random Affliction debuffs apply continuously (unmodeled in PoB)"
                )
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "MEDIUM",
                    "message": f"{p_name} (Group #{g_idx}, {g_slot}) combat caveat: {'; '.join(notes)}.",
                }
            )

    if pact_entries:
        mechanics["pactSkills"] = pact_entries


def _check_overloaded_intensity(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    cfg_low = _cfg_map_low(ctx.get("cfg_res"))
    mg = ctx.get("main_group") or {}
    mg_gems = [gm for gm in (mg.get("gems") or []) if isinstance(gm, dict) and gm.get("enabled", True)]
    has_overloaded_sup = any(
        any(tok in _gem_names_low(gm) for tok in ("overloaded intensity", "过载强度", "過載強度"))
        for gm in mg_gems
    )
    has_pinpoint = any(
        any(tok in _gem_names_low(gm) for tok in ("pinpoint", "精准破坏", "精準破壞", "凝聚"))
        for gm in mg_gems
    )
    ov_stacks = int(float(cfg_low.get("overloadedintensity") or cfg_low.get("multiplieroverloadedintensity") or 0))
    int_stacks = int(float(cfg_low.get("intensifyintensity") or 0))
    all_eq_blob = " ".join((ctx.get("equipped_names_all") or {}).values())
    has_apex = "apex mode" in all_eq_blob
    has_nadir = "nadir mode" in all_eq_blob

    if not (has_overloaded_sup or ov_stacks > 0 or (int_stacks >= 3 and (has_pinpoint or has_apex or has_nadir))):
        return

    non_instant_travel = [
        nm
        for nm in (ctx.get("all_active_non_swap_low") or set())
        if any(k in nm for k in ("frostblink of wintry blast", "flame dash", "lightning warp", "bodyswap"))
    ]
    ramp_bits: list[str] = []
    if has_overloaded_sup or ov_stacks > 0:
        if has_apex and has_nadir:
            ramp_bits.append(
                f"Apex Mode + Nadir Mode with Overloaded Intensity ({ov_stacks or 3} stacks) requires ~9s stationary ramp"
            )
        else:
            ramp_bits.append(
                f"Overloaded Intensity ({ov_stacks or 'stacked'}) requires sustained stationary casting to ramp"
            )
    if int_stacks >= 3:
        ramp_bits.append(f"Intensity={int_stacks} drops sharply on cold-start or movement")
    if non_instant_travel:
        ramp_bits.append(
            f"non-instant travel skill ({', '.join(sorted(non_instant_travel))}) resets regular Intensity stacks"
        )
    else:
        ramp_bits.append("non-instant travel / movement resets regular Intensity stacks, lowering crit and live DPS")

    mechanics["intensityRamp"] = {
        "overloadedSupport": has_overloaded_sup,
        "overloadedStacks": ov_stacks,
        "intensityStacks": int_stacks,
        "hasApexMode": has_apex,
        "hasNadirMode": has_nadir,
        "hasPinpoint": has_pinpoint,
        "nonInstantTravel": sorted(non_instant_travel),
    }
    gaps.append(
        {
            "category": "Mechanic",
            "severity": "INFO",
            "message": f"Intensity / Overloaded ramp sensitivity: {'; '.join(ramp_bits)}.",
        }
    )


def _aura_token_present(token: str, haystack: str) -> bool:
    if token.isascii():
        return re.search(rf"\b{re.escape(token)}\b", haystack) is not None
    return token in haystack


def _is_vaal_gem(gm: dict[str, Any]) -> bool:
    for k in ("name", "nameSpec", "nameZh"):
        low = strip_pob_escapes(str(gm.get(k) or "")).strip().lower()
        if low.startswith("vaal ") or " vaal " in low or "瓦尔" in low or "瓦爾" in low:
            return True
    return str(gm.get("skillId") or "").strip().lower().startswith("vaal")


_VAAL_SECONDARY_AURA_MAP: dict[str, str] = {
    "vaalimpurityoffire": "purity of fire",
    "vaal impurity of fire": "purity of fire",
    "vaalimpurityofice": "purity of ice",
    "vaal impurity of ice": "purity of ice",
    "vaalimpurityoflightning": "purity of lightning",
    "vaal impurity of lightning": "purity of lightning",
}


def _is_generosity_support(gm: dict[str, Any]) -> bool:
    if not isinstance(gm, dict) or not gm.get("enabled", True) or not gm.get("support"):
        return False
    sid = str(gm.get("skillId") or "").strip().lower()
    if sid in ("supportgenerosity", "supportgenerosityplus"):
        return True
    for k in ("name", "nameSpec", "nameZh"):
        val = strip_pob_escapes(str(gm.get(k) or "")).strip().lower()
        if "generosity" in val or "慷慨" in val:
            return True
    return False


def _active_permanent_aura_haystack(ctx: dict[str, Any], party_low: str, custom_low: str) -> str:
    """Build active-aura search text excluding Vaal-only gems and Generosity-supported auras.

    Per PoB Data.lua:1106 / SkillsTab.lua:500, on Vaal gems `enableGlobal1` is the Vaal burst
    (default True) and `enableGlobal2` is the permanent base aura (default False).
    """
    enabled_names: list[str] = []
    for g in ctx.get("groups") or []:
        if not isinstance(g, dict) or not g.get("enabled", True) or "Swap" in str(g.get("slot") or ""):
            continue
        group_gems = g.get("gems") or []
        if any(_is_generosity_support(gm) for gm in group_gems):
            continue
        for gm in group_gems:
            if not isinstance(gm, dict) or not gm.get("enabled", True) or gm.get("support"):
                continue
            is_vaal = _is_vaal_gem(gm)
            if is_vaal:
                if not bool(gm.get("enableGlobal2", False)):
                    continue
            elif gm.get("enableGlobal1") is False:
                continue
            for k in ("name", "nameSpec", "nameZh", "skillId"):
                val = strip_pob_escapes(str(gm.get(k) or "")).strip().lower()
                if not val:
                    continue
                if k != "skillId":
                    enabled_names.append(val)
                if is_vaal:
                    mapped = _VAAL_SECONDARY_AURA_MAP.get(val)
                    if mapped:
                        enabled_names.append(mapped)
    filtered_custom = "\n".join(
        ln for ln in custom_low.splitlines() if ln.strip() and not _WE_COND_RE.search(ln)
    )
    return "\n".join([" | ".join(enabled_names), party_low, filtered_custom])


def _extract_watchers_eye_auras(raw_txt: str) -> list[str]:
    auras: list[str] = []
    for raw_ln in _active_item_lines(raw_txt):
        ln = strip_pob_escapes(raw_ln).strip()
        if not ln or not _WE_COND_RE.search(ln):
            continue
        low = ln.lower()
        for canon, tokens in _WATCHERS_EYE_AURAS:
            if any(_aura_token_present(tok, low) for tok in tokens):
                auras.append(canon)
                break
    return auras


def _check_underconfigured_negative_water(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    party_low, has_party = _party_text_and_active(ctx.get("party_res"))
    custom_low = str(ctx.get("custom_mods_joined") or "")
    active_aura_haystack = _active_permanent_aura_haystack(ctx, party_low, custom_low)

    # 1. Watcher's Eye 0/N active aura mods
    raw_by_slot: dict[str, str] = ctx.get("raw_by_slot") or {}
    eq_names: dict[str, str] = ctx.get("equipped_names_all") or {}
    for sname, nm_low in eq_names.items():
        if "Swap" in sname:
            continue
        if "watcher's eye" not in nm_low and "watchers eye" not in nm_low and "守望之眼" not in nm_low:
            continue
        raw_txt = raw_by_slot.get(sname) or str((ctx.get("active_items_by_slot") or {}).get(sname, {}).get("raw") or "")
        req_auras = _extract_watchers_eye_auras(raw_txt)
        if not req_auras:
            continue
        active_mods = 0
        for aura_name in req_auras:
            tokens = next((toks for c, toks in _WATCHERS_EYE_AURAS if c == aura_name), (aura_name.lower(),))
            if any(_aura_token_present(tok, active_aura_haystack) for tok in tokens):
                active_mods += 1
        mechanics["watchersEye"] = {
            "slot": sname,
            "requiredAuras": req_auras,
            "activeMods": active_mods,
            "totalMods": len(req_auras),
        }
        if active_mods == 0:
            uniq_auras = list(dict.fromkeys(req_auras))
            gaps.append(
                {
                    "category": "UnderConfigured",
                    "severity": "MEDIUM",
                    "message": (
                        f"Watcher's Eye ({sname}) has 0/{len(req_auras)} active aura mod(s) "
                        f"(needs: {', '.join(uniq_auras)}): none found in enabled skills, Party tab, or customMods "
                        "— verify whether dead jewel mods or unconfigured Mercenary/Ally auras."
                    ),
                }
            )

    # 2. Luminary ascendancy with empty <Party> and empty customMods
    info = ctx.get("build_info") or {}
    asc_cls_str = strip_pob_escapes(str(info.get("ascendClassName") or info.get("ascendClassNameZh") or "")).lower()
    is_luminary = (
        "luminary" in asc_cls_str
        or "光明使者" in asc_cls_str
        or "luminary" in (ctx.get("asc_names_lower") or set())
        or "光明使者" in (ctx.get("asc_names_lower") or set())
        or "luminary" in (ctx.get("asc_classes_lower") or set())
    )
    if is_luminary and not has_party and not custom_low.strip():
        mechanics["luminaryUnconfigured"] = True
        gaps.append(
            {
                "category": "UnderConfigured",
                "severity": "INFO",
                "message": (
                    "Luminary ascendancy has empty Party tab and empty customMods: PoB is NOT modeling "
                    "Mercenary/Ally auras, taunt, or damage-sharing buffs (configure Party tab or customMods "
                    "to reflect real Mercenary support)."
                ),
            }
        )

    # 3. Unchecked Endurance/Frenzy/Power charges despite Boss-sustainable on-hit generation
    if is_proxy_main_skill(ctx.get("skills_res")):
        return
    all_mod_lines: list[str] = []
    for sn, rtxt in raw_by_slot.items():
        if "Swap" not in sn:
            all_mod_lines.extend(strip_pob_escapes(ln) for ln in _active_item_lines(rtxt))
    for _lbl, st_lines in ctx.get("alloc_node_stats") or []:
        all_mod_lines.extend(strip_pob_escapes(str(x)) for x in st_lines)

    sustained = find_on_hit_charge_sources(all_mod_lines, ctx.get("stats"))
    if sustained:
        cfg_low = _cfg_map_low(ctx.get("cfg_res"))
        unchecked: list[str] = []
        for ck_low, reason in sustained.items():
            if not cfg_low.get(ck_low):
                canon = _CHARGE_CANONICAL.get(ck_low, ck_low)
                unchecked.append(f"{canon} ({reason})")
                gaps.append(
                    {
                        "category": "UnderConfigured",
                        "severity": "INFO",
                        "message": (
                            f"Under-configured charge sustain ('negative water'): {canon} is unchecked, "
                            f"but build has Boss-sustainable on-hit generation ({reason})."
                        ),
                    }
                )
        if unchecked:
            mechanics["uncheckedSustainedCharges"] = unchecked


def _check_330_mechanics_and_underconfig(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    """Run PoE1 3.30 Pact, Overloaded Intensity, and under-configured ('negative water') checks."""
    _check_pact_skills(ctx, gaps, mechanics)
    _check_overloaded_intensity(ctx, gaps, mechanics)
    _check_underconfigured_negative_water(ctx, gaps, mechanics)
