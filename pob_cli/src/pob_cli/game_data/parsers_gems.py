"""PoB Lua gem and skill data parsers (Gems.lua + Data/Skills/*.lua)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .constants import (
    _ACTIVE_AWAKENED_EXCEPTIONAL_LOWER,
    _EXCEPTIONAL_GEM_FALLBACK_BOUNDS,
    _POB_ESCAPE_RE,
    COLOR_NAMES,
    ITEM_SKILL_FILES,
    LUA_SKIP_KEYS_SKILL,
    SKILL_DAMAGE_BASE_EFFECTIVENESS,
    SKILL_DAMAGE_INCREMENTAL_EFFECTIVENESS,
    SKILL_FILES,
    SKILL_KEEP_LEVELS,
)
from .lua_parser import (
    SKILL_HIGH_LEVEL_STRIP_RE,
    _as_seq,
    _parse_lua_assignments,
    _parse_lua_return,
)
from .tw_norm import is_legacy_awakened_gem
from .zh_dict import _ZhDictionary


def _round_pob(val: float) -> int:
    """PoB's round(x) uses math.floor(x + 0.5) for positive numbers."""
    return int(val + 0.5)


def _compute_skill_level_stats(skill: dict[str, Any], lvl_num: int) -> dict[str, Any] | None:
    levels = skill.get("levels")
    if not isinstance(levels, dict) or lvl_num not in levels:
        return None
    lvl = levels[lvl_num]
    if not isinstance(lvl, dict):
        return None

    stat_names = _as_seq(skill.get("stats"))
    interp_list = _as_seq(lvl.get("statInterpolation"))
    base_eff = float(skill.get("baseEffectiveness") or 1.0)
    inc_eff = float(skill.get("incrementalEffectiveness") or 0.0)
    actor_level = int(lvl.get("levelRequirement") or 1)

    avail_eff: float | None = None
    resolved_stats: dict[str, Any] = {}
    for idx, stat_name in enumerate(stat_names):
        if not isinstance(stat_name, str):
            continue
        raw_val = lvl.get(idx + 1, 1)
        interp = interp_list[idx] if idx < len(interp_list) else 1
        if interp == 3 and isinstance(raw_val, (int, float)):
            if avail_eff is None:
                avail_eff = (
                    (SKILL_DAMAGE_BASE_EFFECTIVENESS + SKILL_DAMAGE_INCREMENTAL_EFFECTIVENESS * (actor_level - 1))
                    * base_eff
                    * ((1.0 + inc_eff) ** (actor_level - 1))
                )
            resolved_stats[stat_name] = _round_pob(avail_eff * float(raw_val))
        elif interp == 2 and isinstance(raw_val, (int, float)):
            resolved_stats[stat_name] = _round_pob(float(raw_val))
        else:
            resolved_stats[stat_name] = raw_val

    cost_raw = lvl.get("cost")
    cost = {str(k): v for k, v in cost_raw.items()} if isinstance(cost_raw, dict) else {}

    entry: dict[str, Any] = {
        "levelRequirement": lvl.get("levelRequirement"),
        "critChance": lvl.get("critChance"),
        "damageEffectiveness": lvl.get("damageEffectiveness"),
        "manaMultiplier": lvl.get("manaMultiplier"),
        "cooldown": lvl.get("cooldown"),
        "storedUses": lvl.get("storedUses"),
        "cost": cost,
        "stats": resolved_stats,
    }
    return {k: v for k, v in entry.items() if v is not None and v != {}}


def _load_skill_files(data_dir: Path) -> tuple[dict[str, dict[Any, Any]], dict[str, str]]:
    skills: dict[str, dict[Any, Any]] = {}
    skill_source_file: dict[str, str] = {}
    for fn in SKILL_FILES:
        p = data_dir / "Skills" / fn
        if p.is_file():
            is_item_file = fn in ITEM_SKILL_FILES
            parsed_sk = _parse_lua_assignments(
                p,
                "skills",
                skip_keys=LUA_SKIP_KEYS_SKILL,
                keep_levels=None if is_item_file else SKILL_KEEP_LEVELS,
                prestrip_re=None if is_item_file else SKILL_HIGH_LEVEL_STRIP_RE,
            )
            skills.update(parsed_sk)
            for sk_id in parsed_sk:
                skill_source_file[sk_id] = fn
    return skills, skill_source_file


def _synthesize_vaal_transfigured(
    gems_raw: dict[Any, Any], skills: dict[str, dict[Any, Any]]
) -> None:
    """Replicate PoB's synthesized Vaal + Transfigured hybrid gems (Modules/Data.lua:1098-1118)."""
    synthesized: dict[str, dict[Any, Any]] = {}
    for gem_id, gem in gems_raw.items():
        if not isinstance(gem, dict) or not gem.get("vaalGem"):
            continue
        sec_id = gem.get("secondaryGrantedEffectId")
        if not isinstance(sec_id, str):
            continue
        for alt in ("AltX", "AltY"):
            alt_skill = skills.get(sec_id + alt)
            if isinstance(alt_skill, dict):
                alt_base_name = alt_skill.get("baseTypeName") or alt_skill.get("name") or ""
                cloned = dict(gem)
                cloned["name"] = f"Vaal {alt_base_name}"
                cloned["variantId"] = f"{gem.get('variantId', '')}{alt}"
                cloned["secondaryGrantedEffectId"] = sec_id + alt
                synthesized[f"{gem_id}{alt}"] = cloned
    gems_raw.update(synthesized)


def _build_gem_record(
    gem_id: str,
    gem: dict[Any, Any],
    skills: dict[str, dict[Any, Any]],
    zh: _ZhDictionary,
) -> dict[str, Any]:
    name = str(gem.get("name") or "").strip()
    ge_id = str(gem.get("grantedEffectId") or "")
    sec_id = gem.get("secondaryGrantedEffectId")
    skill = skills.get(ge_id, {})
    sec_skill = skills.get(sec_id, {}) if isinstance(sec_id, str) else {}
    tags_raw = gem.get("tags") if isinstance(gem.get("tags"), dict) else {}
    tags_list = sorted(str(k) for k, v in tags_raw.items() if v)
    is_support = bool(skill.get("support") or ("support" in tags_raw))
    is_vaal = bool(gem.get("vaalGem") or ("vaal" in tags_raw))
    variant_id = str(gem.get("variantId") or "")
    is_transfigured = bool(re.search(r"Alt[XYZ]$", variant_id))
    is_imbued = bool("imbued" in tags_raw or name.startswith("Imbued "))
    color = COLOR_NAMES.get(int(skill.get("color") or 0), "")
    if not color:
        if "strength" in tags_raw:
            color = "Red"
        elif "dexterity" in tags_raw:
            color = "Green"
        elif "intelligence" in tags_raw:
            color = "Blue"

    name_low_nosup = re.sub(
        r"\s+support$", "", _POB_ESCAPE_RE.sub("", name).strip().lower()
    ).strip()
    legacy_removed_328 = is_legacy_awakened_gem(name)
    is_exceptional = bool(
        tags_raw.get("exceptional")
        or name_low_nosup in _EXCEPTIONAL_GEM_FALLBACK_BOUNDS
    )

    raw_nat = gem.get("naturalMaxLevel")
    if raw_nat is not None:
        nat_max = int(raw_nat)
    elif name_low_nosup in _ACTIVE_AWAKENED_EXCEPTIONAL_LOWER:
        nat_max = 4
    elif legacy_removed_328:
        nat_max = 5
    elif is_exceptional:
        nat_max = 3
    else:
        nat_max = 20
    corrupted_max_level = nat_max + 1

    target_levels = sorted({1, nat_max, nat_max + 1, 20, 21})
    levels_out: dict[str, Any] = {}
    for lvl_num in target_levels:
        lvl_stats = _compute_skill_level_stats(skill, lvl_num)
        if sec_skill and not is_vaal:
            sec_lvl = _compute_skill_level_stats(sec_skill, lvl_num)
            if sec_lvl is not None:
                if lvl_stats is None:
                    lvl_stats = sec_lvl
                else:
                    merged_st = dict(sec_lvl.get("stats") or {})
                    merged_st.update(lvl_stats.get("stats") or {})
                    lvl_stats["stats"] = merged_st
                    for k_aux in ("cooldown", "storedUses", "critChance", "damageEffectiveness", "manaMultiplier", "cost"):
                        if lvl_stats.get(k_aux) is None and sec_lvl.get(k_aux) is not None:
                            lvl_stats[k_aux] = sec_lvl[k_aux]
        if lvl_stats is not None:
            levels_out[str(lvl_num)] = lvl_stats

    quality_stats: list[dict[str, Any]] = []
    seen_qs: set[str] = set()
    for src_sk in (skill, sec_skill) if (sec_skill and not is_vaal) else (skill,):
        for qs in _as_seq(src_sk.get("qualityStats")):
            if isinstance(qs, list) and len(qs) >= 2 and isinstance(qs[1], (int, float)) and str(qs[0]) not in seen_qs:
                seen_qs.add(str(qs[0]))
                quality_stats.append(
                    {"stat": qs[0], "per_quality": qs[1], "at_20_quality": round(float(qs[1]) * 20, 4)}
                )

    constant_stats: list[dict[str, Any]] = []
    seen_cs: set[str] = set()
    for src_sk in (skill, sec_skill) if (sec_skill and not is_vaal) else (skill,):
        for cs in _as_seq(src_sk.get("constantStats")):
            if isinstance(cs, list) and len(cs) >= 2 and str(cs[0]) not in seen_cs:
                seen_cs.add(str(cs[0]))
                constant_stats.append({"stat": cs[0], "value": cs[1]})

    lvl20 = levels_out.get(str(nat_max)) or levels_out.get("20") or levels_out.get("1") or {}
    raw_cd = lvl20.get("cooldown") if lvl20.get("cooldown") is not None else skill.get("cooldown")
    cooldown_val = float(raw_cd) if isinstance(raw_cd, (int, float)) else None
    raw_uses = lvl20.get("storedUses") if lvl20.get("storedUses") is not None else skill.get("storedUses")
    stored_uses_val = int(raw_uses) if isinstance(raw_uses, (int, float)) else None

    zh_name = zh.translate_gem(name)
    display_name = f"{name} Support" if (is_support and not name.endswith("Support") and ge_id != "SupportBarrage") else name

    return {
        "id": gem_id,
        "name": name,
        "display_name": display_name,
        "zh_name": zh_name,
        "game_id": str(gem.get("gameId") or ""),
        "variant_id": variant_id,
        "granted_effect_id": ge_id,
        "secondary_granted_effect_id": sec_id if isinstance(sec_id, str) else None,
        "plus_version_of": skill.get("plusVersionOf") if isinstance(skill.get("plusVersionOf"), str) else None,
        "is_support": is_support,
        "is_vaal": is_vaal,
        "is_transfigured": is_transfigured,
        "is_imbued": is_imbued,
        "is_exceptional": is_exceptional,
        "legacy_removed_328": legacy_removed_328,
        "is_item_skill": False,
        "color": color,
        "tag_string": str(gem.get("tagString") or ""),
        "tags": tags_list,
        "req_str": int(gem.get("reqStr") or 0),
        "req_dex": int(gem.get("reqDex") or 0),
        "req_int": int(gem.get("reqInt") or 0),
        "natural_max_level": nat_max,
        "corrupted_max_level": corrupted_max_level,
        "description": str(skill.get("description") or ""),
        "cast_time": skill.get("castTime"),
        "cooldown": cooldown_val,
        "stored_uses": stored_uses_val,
        "crit_chance": lvl20.get("critChance"),
        "damage_effectiveness": lvl20.get("damageEffectiveness"),
        "mana_multiplier": lvl20.get("manaMultiplier"),
        "mana_cost": {lvl_k: lvl_v.get("cost") for lvl_k, lvl_v in levels_out.items() if lvl_v.get("cost")},
        "quality_stats": quality_stats,
        "constant_stats": constant_stats,
        "levels": levels_out,
        "secondary_skill_name": sec_skill.get("name") if isinstance(sec_skill, dict) else None,
        "variants": [],
    }


def _build_item_skill_record(
    sk_id: str,
    skill: dict[Any, Any],
    src_fn: str,
    zh: _ZhDictionary,
) -> dict[str, Any]:
    name = str(skill.get("name") or "").strip()
    is_support = bool(skill.get("support"))
    color = COLOR_NAMES.get(int(skill.get("color") or 0), "")
    raw_levels = skill.get("levels")
    avail_lvl_nums = sorted(k for k in raw_levels if isinstance(k, int)) if isinstance(raw_levels, dict) else []
    if len(avail_lvl_nums) > 8:
        pick = sorted({avail_lvl_nums[0], avail_lvl_nums[-1], *(lv for lv in (1, 10, 15, 20, 21, 25, 30) if lv in avail_lvl_nums)})
    else:
        pick = avail_lvl_nums

    levels_out: dict[str, Any] = {}
    for lvl_num in pick:
        lvl_stats = _compute_skill_level_stats(skill, lvl_num)
        if lvl_stats is not None:
            levels_out[str(lvl_num)] = lvl_stats

    quality_stats: list[dict[str, Any]] = []
    for qs in _as_seq(skill.get("qualityStats")):
        if isinstance(qs, list) and len(qs) >= 2 and isinstance(qs[1], (int, float)):
            quality_stats.append(
                {"stat": qs[0], "per_quality": qs[1], "at_20_quality": round(float(qs[1]) * 20, 4)}
            )

    constant_stats: list[dict[str, Any]] = []
    for cs in _as_seq(skill.get("constantStats")):
        if isinstance(cs, list) and len(cs) >= 2:
            constant_stats.append({"stat": cs[0], "value": cs[1]})

    base_flags = skill.get("baseFlags") if isinstance(skill.get("baseFlags"), dict) else {}
    tags_list = sorted(str(k) for k, v in base_flags.items() if v)
    if is_support and "support" not in tags_list:
        tags_list.append("support")
    if bool(skill.get("fromItem")):
        skill_source = "item"
    elif bool(skill.get("fromTree")):
        skill_source = "tree"
    else:
        skill_source = src_fn.removesuffix(".lua") or "skill"

    ref_lvl = (
        levels_out.get("20")
        or (levels_out.get(str(pick[-1])) if pick else None)
        or levels_out.get("1")
        or {}
    )
    zh_name = zh.translate_gem(name) or (zh.passives.get(name) if zh.available else None)

    item_nat_max = pick[-1] if pick else 1
    raw_item_cd = ref_lvl.get("cooldown") if ref_lvl.get("cooldown") is not None else skill.get("cooldown")
    item_cd_val = float(raw_item_cd) if isinstance(raw_item_cd, (int, float)) else None
    raw_item_uses = ref_lvl.get("storedUses") if ref_lvl.get("storedUses") is not None else skill.get("storedUses")
    item_uses_val = int(raw_item_uses) if isinstance(raw_item_uses, (int, float)) else None

    return {
        "id": f"Skill/{sk_id}",
        "name": name,
        "display_name": name,
        "zh_name": zh_name,
        "game_id": "",
        "variant_id": "",
        "granted_effect_id": sk_id,
        "secondary_granted_effect_id": None,
        "plus_version_of": None,
        "is_support": is_support,
        "is_vaal": False,
        "is_transfigured": False,
        "is_imbued": False,
        "is_exceptional": False,
        "legacy_removed_328": False,
        "is_item_skill": True,
        "skill_source": skill_source,
        "color": color,
        "tag_string": ", ".join(t.title() for t in tags_list),
        "tags": tags_list,
        "req_str": 0,
        "req_dex": 0,
        "req_int": 0,
        "natural_max_level": item_nat_max,
        "corrupted_max_level": item_nat_max + 1,
        "description": str(skill.get("description") or ""),
        "cast_time": skill.get("castTime"),
        "cooldown": item_cd_val,
        "stored_uses": item_uses_val,
        "crit_chance": ref_lvl.get("critChance"),
        "damage_effectiveness": ref_lvl.get("damageEffectiveness"),
        "mana_multiplier": ref_lvl.get("manaMultiplier"),
        "mana_cost": {lvl_k: lvl_v.get("cost") for lvl_k, lvl_v in levels_out.items() if lvl_v.get("cost")},
        "quality_stats": quality_stats,
        "constant_stats": constant_stats,
        "levels": levels_out,
        "secondary_skill_name": None,
        "variants": [],
    }


def _link_gem_variants(
    records: list[dict[str, Any]],
    by_game_id: dict[str, list[dict[str, Any]]],
    by_granted_id: dict[str, list[dict[str, Any]]],
    by_name_lower: dict[str, list[dict[str, Any]]],
) -> None:
    by_sec_id: dict[str, list[dict[str, Any]]] = {}
    by_plus_ver: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec["secondary_granted_effect_id"]:
            by_sec_id.setdefault(rec["secondary_granted_effect_id"], []).append(rec)
        if rec["plus_version_of"]:
            by_plus_ver.setdefault(rec["plus_version_of"], []).append(rec)

    for rec in records:
        related: dict[str, dict[str, Any]] = {}

        def _add_rel(
            other: dict[str, Any],
            relation: str,
            _rec: dict[str, Any] = rec,
            _related: dict[str, dict[str, Any]] = related,
        ) -> None:
            if other["id"] == _rec["id"]:
                return
            if other["id"] not in _related:
                _related[other["id"]] = {
                    "id": other["id"],
                    "name": other["name"],
                    "zh_name": other["zh_name"],
                    "relation": relation,
                }

        for other in by_game_id.get(rec["game_id"], []):
            rel = "imbued" if other["is_imbued"] else ("transfigured" if other["is_transfigured"] else "base")
            _add_rel(other, rel)

        if rec["secondary_granted_effect_id"]:
            for other in by_granted_id.get(rec["secondary_granted_effect_id"], []):
                _add_rel(other, "base")
        for other in by_sec_id.get(rec["granted_effect_id"], []):
            _add_rel(other, "vaal")

        if rec["plus_version_of"]:
            for other in by_granted_id.get(rec["plus_version_of"], []):
                _add_rel(other, "base")
        for other in by_plus_ver.get(rec["granted_effect_id"], []):
            _add_rel(other, "awakened")

        for prefix, rel in (("Imbued ", "imbued"), ("Vaal ", "vaal"), ("Awakened ", "awakened")):
            if rec["name"].startswith(prefix):
                base_n = rec["name"][len(prefix) :].lower()
                for other in by_name_lower.get(base_n, []):
                    _add_rel(other, "base")
            else:
                for other in by_name_lower.get((prefix + rec["name"]).lower(), []):
                    _add_rel(other, rel)

        rec["variants"] = list(related.values())


def _parse_gems_and_skills(poe1_dir: Path, zh: _ZhDictionary) -> list[dict[str, Any]]:
    data_dir = poe1_dir / "Data"
    skills, skill_source_file = _load_skill_files(data_dir)

    gems_raw = _parse_lua_return(data_dir / "Gems.lua")
    if not isinstance(gems_raw, dict):
        gems_raw = {}

    _synthesize_vaal_transfigured(gems_raw, skills)

    by_game_id: dict[str, list[dict[str, Any]]] = {}
    by_granted_id: dict[str, list[dict[str, Any]]] = {}
    by_name_lower: dict[str, list[dict[str, Any]]] = {}
    referenced_skill_ids: set[str] = set()
    records: list[dict[str, Any]] = []

    for gem_id, gem in gems_raw.items():
        if not isinstance(gem_id, str) or not isinstance(gem, dict):
            continue
        if not str(gem.get("name") or "").strip():
            continue
        ge_id = str(gem.get("grantedEffectId") or "")
        sec_id = gem.get("secondaryGrantedEffectId")
        if ge_id:
            referenced_skill_ids.add(ge_id)
        if isinstance(sec_id, str) and sec_id:
            referenced_skill_ids.add(sec_id)

        rec = _build_gem_record(gem_id, gem, skills, zh)
        records.append(rec)
        if rec["game_id"]:
            by_game_id.setdefault(rec["game_id"], []).append(rec)
        if ge_id:
            by_granted_id.setdefault(ge_id, []).append(rec)
        by_name_lower.setdefault(rec["name"].lower(), []).append(rec)

    # Index item-, ascendancy-, tree-, enchant-, and minion-granted skills from Data/Skills/*.lua
    for sk_id, skill in skills.items():
        if sk_id in referenced_skill_ids or not isinstance(skill, dict):
            continue
        src_fn = skill_source_file.get(sk_id, "")
        if src_fn not in ITEM_SKILL_FILES and not skill.get("fromItem") and not skill.get("fromTree"):
            continue
        if not str(skill.get("name") or "").strip():
            continue

        rec = _build_item_skill_record(sk_id, skill, src_fn, zh)
        records.append(rec)
        by_granted_id.setdefault(sk_id, []).append(rec)
        by_name_lower.setdefault(rec["name"].lower(), []).append(rec)

    _link_gem_variants(records, by_game_id, by_granted_id, by_name_lower)
    return records
