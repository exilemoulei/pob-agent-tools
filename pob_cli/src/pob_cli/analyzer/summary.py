"""Build summary extraction and shared build-state helper predicates."""

from __future__ import annotations

import re
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..snapshot import _call_batch, _finite_float, _stat_float
from .constants import (
    _AWAKENED_CWC_INTERVAL_MS,
    _CWC_INTERVAL_MS,
    _GLOBAL_CDR_LINE_RE,
    _ITEM_SEL_VARIANT_RE,
    _ITEM_SOCKETS_RE,
    _KNOWN_SKILL_BASE_COOLDOWNS,
    _MOD_VARIANT_RE,
    _RESERVATION_AURA_EXACT,
    _RESERVATION_AURA_PREFIXES,
    _RESERVATION_AURA_ZH,
)


def extract_summary(worker: BridgeWorker) -> dict[str, Any]:
    """Extract a complete structured summary of the currently loaded build in one pipelined batch."""
    with worker.lock:
        stats_res, sidebar, info, header, skills_res, specs_res, items_res = _call_batch(
            worker,
            [
                ("get_stats", {}),
                ("get_sidebar", {}),
                ("get_build_info", {}),
                ("get_build_header", {}),
                ("list_skills", {}),
                ("list_specs", {}),
                ("list_items", {}),
            ],
        )

    stats = stats_res.get("stats", {}) if isinstance(stats_res, dict) else {}
    sidebar_warnings = sidebar.get("warnings", []) if isinstance(sidebar, dict) else []
    warnings = [
        strip_pob_escapes(w.get("text") or w.get("raw") or "")
        for w in sidebar_warnings
        if isinstance(w, dict) and (w.get("text") or w.get("raw"))
    ]

    main_idx = skills_res.get("mainSocketGroup") or header.get("mainSocketGroup", {}).get("index") or 1
    main_group: dict[str, Any] | None = None
    for g in skills_res.get("groups", []):
        if g.get("index") == main_idx or g.get("isMain"):
            main_group = g
            break

    minion_sidebar: list[dict[str, str]] = []
    for row in sidebar.get("rows", []):
        if row.get("actor") == "minion" and (row.get("lhs") or row.get("rhs")):
            minion_sidebar.append(
                {
                    "label": strip_pob_escapes(row.get("lhs", "")),
                    "value": strip_pob_escapes(row.get("rhs", "")),
                    "stat": row.get("stat") or "",
                }
            )

    has_minion_stats = bool(minion_sidebar) or _stat_float(stats.get("MinionCombinedDPS")) > 0

    offence = {
        "FullDPS": stats.get("FullDPS"),
        "CombinedDPS": stats.get("CombinedDPS"),
        "TotalDPS": stats.get("TotalDPS"),
        "TotalDot": stats.get("TotalDot"),
        "TotalDotDPS": stats.get("TotalDotDPS"),
        "WithImpaleDPS": stats.get("WithImpaleDPS"),
        "BleedDPS": stats.get("BleedDPS"),
        "IgniteDPS": stats.get("IgniteDPS"),
        "PoisonDPS": stats.get("PoisonDPS"),
        "ImpaleDPS": stats.get("ImpaleDPS"),
        "AverageDamage": stats.get("AverageDamage"),
        "AverageHit": stats.get("AverageHit"),
        "Speed": stats.get("Speed"),
        "CritChance": stats.get("CritChance"),
        "EffectiveCritChance": stats.get("EffectiveCritChance"),
        "CritMultiplier": stats.get("CritMultiplier"),
        "HitChance": stats.get("HitChance"),
        "ManaCost": stats.get("ManaCost"),
        "MainHandAccuracy": stats.get("MainHandAccuracy"),
    }

    minion_offence = None
    if has_minion_stats:
        minion_offence = {
            "MinionCombinedDPS": stats.get("MinionCombinedDPS"),
            "MinionTotalDPS": stats.get("MinionTotalDPS"),
            "MinionTotalDot": stats.get("MinionTotalDot"),
            "MinionAverageDamage": stats.get("MinionAverageDamage"),
            "MinionSpeed": stats.get("MinionSpeed"),
            "MinionCritChance": stats.get("MinionCritChance"),
            "MinionCritMultiplier": stats.get("MinionCritMultiplier"),
            "MinionHitChance": stats.get("MinionHitChance"),
            "MinionLife": stats.get("MinionLife"),
            "sidebarRows": minion_sidebar,
        }

    defence = {
        "TotalEHP": stats.get("TotalEHP"),
        "Life": stats.get("Life"),
        "LifeUnreserved": stats.get("LifeUnreserved"),
        "LifeUnreservedPercent": stats.get("LifeUnreservedPercent"),
        "Mana": stats.get("Mana"),
        "ManaUnreserved": stats.get("ManaUnreserved"),
        "ManaUnreservedPercent": stats.get("ManaUnreservedPercent"),
        "EnergyShield": stats.get("EnergyShield"),
        "Ward": stats.get("Ward"),
        "Armour": stats.get("Armour"),
        "PhysicalDamageReduction": stats.get("PhysicalDamageReduction"),
        "Evasion": stats.get("Evasion"),
        "MeleeEvadeChance": stats.get("MeleeEvadeChance"),
        "ProjectileEvadeChance": stats.get("ProjectileEvadeChance"),
        "PhysicalMaximumHitTaken": stats.get("PhysicalMaximumHitTaken"),
        "FireMaximumHitTaken": stats.get("FireMaximumHitTaken"),
        "ColdMaximumHitTaken": stats.get("ColdMaximumHitTaken"),
        "LightningMaximumHitTaken": stats.get("LightningMaximumHitTaken"),
        "ChaosMaximumHitTaken": stats.get("ChaosMaximumHitTaken"),
        "FireResist": stats.get("FireResist"),
        "FireResistOverCap": stats.get("FireResistOverCap"),
        "ColdResist": stats.get("ColdResist"),
        "ColdResistOverCap": stats.get("ColdResistOverCap"),
        "LightningResist": stats.get("LightningResist"),
        "LightningResistOverCap": stats.get("LightningResistOverCap"),
        "ChaosResist": stats.get("ChaosResist"),
        "ChaosResistOverCap": stats.get("ChaosResistOverCap"),
        "EffectiveBlockChance": stats.get("EffectiveBlockChance"),
        "EffectiveSpellBlockChance": stats.get("EffectiveSpellBlockChance"),
        "EffectiveSpellSuppressionChance": stats.get("EffectiveSpellSuppressionChance"),
        "NetLifeRegen": stats.get("LifeRegenRecovery") if stats.get("LifeRegenRecovery") is not None else stats.get("LifeRegen"),
        "NetManaRegen": stats.get("ManaRegenRecovery") if stats.get("ManaRegenRecovery") is not None else stats.get("ManaRegen"),
        "EnergyShieldRegen": stats.get("EnergyShieldRegenRecovery") if stats.get("EnergyShieldRegenRecovery") is not None else stats.get("EnergyShieldRegen"),
        "Str": stats.get("Str"),
        "ReqStr": stats.get("ReqStr"),
        "Dex": stats.get("Dex"),
        "ReqDex": stats.get("ReqDex"),
        "Int": stats.get("Int"),
        "ReqInt": stats.get("ReqInt"),
    }

    return {
        "buildInfo": info,
        "buildHeader": header,
        "activeSpec": specs_res.get("activeSpec"),
        "specs": specs_res.get("specs", []),
        "activeItemSetId": items_res.get("activeItemSetId"),
        "itemSets": items_res.get("itemSets", []),
        "activeSkillSetId": skills_res.get("activeSkillSetId"),
        "skillSets": skills_res.get("skillSets", []),
        "mainGroup": main_group,
        "offence": offence,
        "minionOffence": minion_offence,
        "defence": defence,
        "warnings": warnings,
    }


get_summary = extract_summary


def _has_mageblood_equipped(items_res: dict[str, Any]) -> bool:
    """Return True if a Belt slot has Mageblood equipped."""
    items_by_id = {it["id"]: it for it in items_res.get("items", [])}
    for slot in items_res.get("slots", []):
        if slot.get("name") == "Belt" and slot.get("selItemId"):
            item = items_by_id.get(slot["selItemId"], {})
            text = " ".join(
                str(item.get(k) or "")
                for k in ("name", "nameZh", "title", "titleZh", "baseName", "baseNameZh")
            ).lower()
            if "mageblood" in text or "法师之血" in text:
                return True
    return False


def _has_doryanis_prototype_equipped(items_res: dict[str, Any] | None) -> bool:
    """Return True if Body Armour slot has Doryani's Prototype equipped."""
    if not isinstance(items_res, dict):
        return False
    items_by_id = {it["id"]: it for it in items_res.get("items", []) if isinstance(it, dict) and "id" in it}
    for slot in items_res.get("slots", []):
        if not isinstance(slot, dict) or slot.get("inactive"):
            continue
        if slot.get("name") == "Body Armour" and slot.get("selItemId"):
            item = items_by_id.get(slot["selItemId"], {})
            text = " ".join(
                strip_pob_escapes(str(item.get(k) or ""))
                for k in ("name", "nameZh", "title", "titleZh", "baseName", "baseNameZh")
            ).lower()
            if any(tok in text for tok in ("doryani's prototype", "doryanis prototype", "多里亚尼", "多里亞尼")):
                return True
    return False


def _is_reservation_aura_gem(gm: dict[str, Any]) -> bool:
    """Return True if an enabled active gem dict represents a mana/life reservation aura."""
    if not isinstance(gm, dict) or gm.get("support") or not gm.get("enabled", True):
        return False
    for key in ("name", "nameSpec", "nameZh"):
        raw = strip_pob_escapes(str(gm.get(key) or "")).strip().lower()
        if not raw:
            continue
        base = raw[5:].strip() if raw.startswith("vaal ") else raw
        if base in _RESERVATION_AURA_EXACT:
            return True
        if any(base.startswith(pref) for pref in _RESERVATION_AURA_PREFIXES):
            return True
        if any(tok in raw for tok in _RESERVATION_AURA_ZH):
            return True
    return False


def _detect_repeat_multiplier(skills_res: dict[str, Any] | None) -> tuple[int, list[str]]:
    """Return (repeat_multiplier, labels) for main skill group's enabled repeat supports.

    Repeat supports pay mana ONLY on the initial cast/attack, NOT on free repeats:
    - Greater Spell Echo -> 3x (1 + 2 repeats)
    - Spell Echo / Awakened Spell Echo -> 2x (1 + 1 repeat)
    - Awakened Multistrike -> 4x (1 + 3 repeats)
    - Multistrike -> 3x (1 + 2 repeats)
    """
    groups = (skills_res or {}).get("groups") or []
    main_idx = (skills_res or {}).get("mainSocketGroup")
    main_group = next(
        (g for g in groups if isinstance(g, dict) and (g.get("isMain") or (main_idx is not None and g.get("index") == main_idx))),
        None,
    )
    if not main_group or not main_group.get("enabled", True):
        return 1, []

    has_gse = False
    has_se = False
    has_awk_ms = False
    has_ms = False
    labels: list[str] = []

    for gm in main_group.get("gems") or []:
        if not isinstance(gm, dict) or not gm.get("enabled", True) or not gm.get("support"):
            continue
        gsid = str(gm.get("skillId") or "")
        combined = " ".join(
            strip_pob_escapes(str(gm.get(k) or ""))
            for k in ("name", "nameSpec", "nameZh")
        ).lower()
        if (
            gsid == "SupportGreaterSpellEcho"
            or "greater spell echo" in combined
            or "高阶施法回响" in combined
            or "高階施放迴響" in combined
        ):
            if not has_gse:
                has_gse = True
                labels.append("Greater Spell Echo (3x)")
        elif (
            gsid in ("SupportSpellEcho", "SupportMulticast", "SupportAwakenedSpellEcho")
            or "spell echo" in combined
            or "法术回响" in combined
            or "施放迴響" in combined
        ):
            if not has_se:
                has_se = True
                labels.append("Spell Echo (2x)")
        elif (
            gsid == "SupportAwakenedMultistrike"
            or "awakened multistrike" in combined
            or "觉醒多重打击" in combined
            or "覺醒多重打擊" in combined
        ):
            if not has_awk_ms:
                has_awk_ms = True
                labels.append("Awakened Multistrike (4x)")
        elif (
            gsid == "SupportMultistrike"
            or "multistrike" in combined
            or "多重打击" in combined
            or "多重打擊" in combined
        ):
            if not has_ms:
                has_ms = True
                labels.append("Multistrike (3x)")

    spell_repeats = 1 + (2 if has_gse else 0) + (1 if has_se else 0)
    attack_repeats = 4 if has_awk_ms else (3 if has_ms else 1)
    return max(spell_repeats, attack_repeats), labels


def _resolve_main_skill_cooldown(
    stats: dict[str, Any],
    skills_res: dict[str, Any] | None,
) -> float | None:
    """Resolve effective cooldown (seconds) for the main skill from PoB stats or local game_data."""
    for stat_key in ("SkillCooldown", "Cooldown", "EffectiveCooldown", "CooldownTime"):
        raw = stats.get(stat_key)
        if raw is not None:
            val = _finite_float(raw, default=0.0)
            if val > 0.0:
                return val

    groups = (skills_res or {}).get("groups") or []
    main_idx = (skills_res or {}).get("mainSocketGroup")
    main_group = next(
        (g for g in groups if isinstance(g, dict) and (g.get("isMain") or (main_idx is not None and g.get("index") == main_idx))),
        None,
    )
    if not main_group or not main_group.get("enabled", True):
        return None

    active_names = [
        strip_pob_escapes(str(gm.get("name") or gm.get("nameSpec") or "")).strip()
        for gm in (main_group.get("gems") or [])
        if isinstance(gm, dict) and gm.get("enabled", True) and not gm.get("support")
    ]
    active_names = [n for n in active_names if n]
    if not active_names:
        return None

    primary_name = active_names[0]
    primary_low = primary_name.lower()
    base_cd = 0.0
    try:
        from ..game_data import query_local_data

        res = query_local_data("gem", primary_name, limit=5)
        hits = res.get("results") or []
        gem_rec = None
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            cand_names = {
                strip_pob_escapes(str(hit.get(k) or "")).strip().lower()
                for k in ("name", "display_name", "zh_name")
                if hit.get(k)
            }
            if primary_low in cand_names:
                gem_rec = hit
                break
        if gem_rec is not None:
            base_cd = _finite_float(gem_rec.get("cooldown"), default=0.0)
            if base_cd <= 0.0:
                lvls = gem_rec.get("levels") or {}
                if isinstance(lvls, dict):
                    lvl20 = lvls.get("20") or lvls.get(20) or (next(iter(lvls.values())) if lvls else {})
                    if isinstance(lvl20, dict):
                        base_cd = _finite_float(lvl20.get("cooldown"), default=0.0)
    except Exception:
        base_cd = 0.0

    if base_cd <= 0.0:
        base_cd = _KNOWN_SKILL_BASE_COOLDOWNS.get(primary_low, 0.0)

    if base_cd > 0.0:
        cdr_raw = _finite_float(stats.get("CooldownRecovery"), default=0.0)
        if cdr_raw > 10.0:
            return base_cd / (1.0 + cdr_raw / 100.0)
        if cdr_raw > 0.0:
            return base_cd / cdr_raw
        return base_cd
    return None


def _compute_effective_mana_spend(
    stats: dict[str, Any],
    skills_res: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compute effective mana spend rate accounting for repeat supports and active skill cooldown."""
    mana_cost = _finite_float(stats.get("ManaCost"))
    speed = _finite_float(stats.get("Speed"))
    repeats, repeat_labels = _detect_repeat_multiplier(skills_res)
    eff_casts = (speed / repeats) if (speed > 0.0 and repeats > 0) else 0.0
    eff_cd = _resolve_main_skill_cooldown(stats, skills_res)
    cooldown_capped = False
    if eff_cd is not None and eff_cd > 0.0 and eff_casts > 0.0:
        cd_cap = 1.0 / eff_cd
        if cd_cap < eff_casts:
            eff_casts = cd_cap
            cooldown_capped = True
    return {
        "manaCost": mana_cost,
        "rawSpeed": speed,
        "repeats": repeats,
        "repeatLabels": repeat_labels,
        "effectiveCooldown": eff_cd,
        "cooldownCapped": cooldown_capped,
        "effectiveCastsPerSec": eff_casts,
        "spendRate": mana_cost * eff_casts,
    }


def _is_tincture_item(item: dict[str, Any] | None) -> bool:
    """Return True if a PoB item entry represents a PoE1 Tincture (NOT a Flask)."""
    if not isinstance(item, dict):
        return False
    if str(item.get("type") or "").strip().lower() == "tincture":
        return True
    text = " ".join(
        str(item.get(k) or "")
        for k in ("name", "nameZh", "title", "titleZh", "baseName", "baseNameZh")
    ).lower()
    return "tincture" in text or "酊剂" in text or "sap of the seasons" in text or "四季之侵" in text


def _has_bloodsoaked_blade_keystone(
    worker: BridgeWorker,
    tree_summary: dict[str, Any] | None = None,
) -> bool:
    """Detect whether the Bloodsoaked Blade (`喋血之刃`) keystone is allocated on the passive tree."""
    if isinstance(tree_summary, dict):
        for ks in tree_summary.get("keystones") or []:
            if isinstance(ks, dict):
                nm = strip_pob_escapes(str(ks.get("name") or "")).lower()
                nm_zh = strip_pob_escapes(str(ks.get("nameZh") or "")).lower()
                if "bloodsoaked blade" in nm or "喋血之刃" in nm_zh or int(ks.get("id") or 0) == 13019:
                    return True
        return False
    try:
        tree_state = worker.call("get_tree_state", {})
        alloc_ids = {int(x) for x in (tree_state.get("allocatedNodes") or [])}
        if not alloc_ids:
            return False
        if 13019 in alloc_ids:
            return True
        nodes_db = worker.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
        for nid in alloc_ids:
            nd = nodes_db.get(str(nid)) or {}
            nm = str(nd.get("name") or "").lower()
            if "bloodsoaked blade" in nm:
                return True
    except Exception:
        return False
    return False


def _normalize_support_base_name(name: str) -> str:
    low = strip_pob_escapes(str(name or "")).strip().lower()
    if low.startswith("awakened "):
        low = low[len("awakened ") :].strip()
    if low.endswith(" support"):
        low = low[: -len(" support")].strip()
    return low


def _awakened_coc_gem_cdr(level: int) -> int:
    """Exact 3.30 Awakened CoC (`SupportCastOnCritTriggeredPlus`) spell CDR by gem level."""
    lv = max(1, int(level or 1))
    return 10 + (lv - 1) * 3


def _cwc_interval_ms(level: int, *, awakened: bool = False) -> int:
    lv = max(1, int(level or 1))
    if awakened:
        if lv in _AWAKENED_CWC_INTERVAL_MS:
            return _AWAKENED_CWC_INTERVAL_MS[lv]
        return 300 if lv >= 5 else 340
    if lv in _CWC_INTERVAL_MS:
        return _CWC_INTERVAL_MS[lv]
    return 320 if lv >= 20 else max(260, 400 - (lv - 1) * 4)


def _active_item_lines(raw_text: str | None) -> list[str]:
    """Return active mod lines from a PoB item raw block (respecting Selected Variant if present)."""
    if not raw_text:
        return []
    m_sel = _ITEM_SEL_VARIANT_RE.search(raw_text)
    sel_var = int(m_sel.group(1)) if m_sel else None
    out: list[str] = []
    for raw_ln in raw_text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if sel_var is not None:
            mv = _MOD_VARIANT_RE.search(ln)
            if mv:
                allowed = {int(x) for x in mv.group(1).split(",") if x.isdigit()}
                if allowed and sel_var not in allowed:
                    continue
        out.append(ln)
    return out


def _count_item_sockets(raw_text: str | None) -> int | None:
    """Count gem sockets (R/G/B/W) on an item; abyssal (A) and Delve (D) sockets hold no gems."""
    if not raw_text:
        return None
    m = _ITEM_SOCKETS_RE.search(raw_text)
    if not m:
        return None
    letters = re.findall(r"[RGBWrgbw]", m.group(1))
    return len(letters) if letters else None


def _extract_global_cdr_sources(
    alloc_node_stats: list[tuple[str, list[str]]],
    equipped_slots: list[dict[str, Any]],
    cfg_res: dict[str, Any] | None,
) -> dict[str, Any]:
    """Collect global Cooldown Recovery Rate sources (excluding Travel/Guard/Warcry/other-group gem CDR)."""
    sources: list[dict[str, Any]] = []
    total_cdr = 0.0

    for node_label, stats_lines in alloc_node_stats:
        for ln in stats_lines:
            clean = strip_pob_escapes(str(ln or "")).strip()
            m = _GLOBAL_CDR_LINE_RE.match(clean)
            if m:
                val = float(m.group(1))
                total_cdr += val
                sources.append({"source": f"Tree: {node_label}", "value": val, "line": clean})

    for slot_entry in equipped_slots:
        sname = str(slot_entry.get("slotName") or "")
        if "Swap" in sname or slot_entry.get("inactive"):
            continue
        if slot_entry.get("isFlask") and not slot_entry.get("active"):
            continue
        raw_txt = slot_entry.get("raw")
        if not raw_txt:
            continue
        it = slot_entry.get("item") or {}
        it_name = strip_pob_escapes(str(it.get("title") or it.get("name") or sname))
        for ln in _active_item_lines(raw_txt):
            clean = strip_pob_escapes(ln).strip()
            m = _GLOBAL_CDR_LINE_RE.match(clean)
            if m:
                val = float(m.group(1))
                total_cdr += val
                sources.append({"source": f"{sname} ({it_name})", "value": val, "line": clean})

    for blk in (cfg_res or {}).get("customMods") or []:
        if not isinstance(blk, dict) or blk.get("enabled", True) is False:
            continue
        for raw_ln in str(blk.get("text") or "").splitlines():
            clean = strip_pob_escapes(raw_ln).strip()
            if not clean or clean.startswith("#"):
                continue
            m = _GLOBAL_CDR_LINE_RE.match(clean)
            if m:
                val = float(m.group(1))
                total_cdr += val
                sources.append({"source": "customMods", "value": val, "line": clean})

    return {
        "totalGlobalCdrPct": round(total_cdr, 2),
        "sources": sources,
    }
