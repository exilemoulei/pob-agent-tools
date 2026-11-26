#!/usr/bin/env python3
"""Offline deterministic generator for `pob_cli/src/pob_cli/data/map_mods_3_30.json`.

Builds a clean-room T16 + T17 (Nightmare / Uber) PoE1 map modifier and collision corpus from:
- SnosMe/awakened-poe-trade (MIT): `renderer/public/data/{en,cmn-Hant}/{stats,items}.ndjson`, `client_strings.js`
- Optional local PoB `PoE1/Data/ModMap.lua` (MIT)

Never called at runtime; never calls `api.github.com`.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
DEFAULT_OUTPUT = _SRC_DIR / "pob_cli" / "data" / "map_mods_3_30.json"

# (id, tiers, en_lines, cn_fallback, aliases)
# Traditional Chinese (`zh_tw`) is resolved automatically from APT `cmn-Hant/stats.ndjson`,
# falling back to `_TW_FALLBACK` only for legacy pre-3.28 / ModMap.lua lines.
_TW_FALLBACK: dict[str, str] = {
    "monsters reflect #% of elemental damage": "怪物反射 #% 元素傷害",
    "monsters reflect #% of physical damage": "怪物反射 #% 物理傷害",
    "players cannot gain endurance, frenzy or power charges": "玩家無法獲得耐力、狂怒或暴擊球",
    "players have #% reduced action speed for each time they've used a skill recently": "玩家近期每使用過一個技能，行動速度減少 #%",
    "area has patches of awakeners' desolation": "區域內含有覺醒者之殤",
    "rare and unique monsters remove #% of life, mana and energy shield from players or their minions on hit": "稀有和傳奇怪物擊中玩家或其召喚物時移除 #% 生命、魔力和能量護盾",
    "players are marked for death for # seconds after killing a rare or unique monster": "玩家擊殺稀有或傳奇怪物後被標記死亡，持續 # 秒",
    "all magic and normal monsters in area are in a union of souls": "區域內的所有魔法和普通怪物處於聚魂狀態",
}

_PRESET_SPECS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    ("reflect_ele", ("t16", "t17"), ("Rare Monsters have Elemental Thorns reflecting # Elemental Damage", "Monsters reflect #% of Elemental Damage"), ("稀有怪物带有元素荆棘，反射 # 点元素伤害", "怪物会反射 #% 元素伤害"), ("元反", "元素反射", "元素荆棘", "ele reflect", "elemental reflect", "elemental thorns")),
    ("reflect_phys", ("t16", "t17"), ("Rare Monsters have Physical Thorns reflecting # Physical Damage", "Monsters reflect #% of Physical Damage"), ("稀有怪物带有物理荆棘，反射 # 点物理伤害", "怪物会反射 #% 物理伤害"), ("物反", "物理反射", "物理荆棘", "phys reflect", "physical reflect", "physical thorns")),
    ("no_regen", ("t16", "t17"), ("Players cannot Regenerate Life, Mana or Energy Shield",), ("玩家无法回复生命、魔力和能量护盾",), ("无回复", "不能回复", "无法回复", "cannot regen", "no regen")),
    ("no_leech", ("t16", "t17"), ("Monsters cannot be Leeched from",), ("无法偷取怪物",), ("无偷取", "无法偷取", "cannot leech", "no leech")),
    ("less_recovery", ("t16", "t17"), ("Players have #% less Recovery Rate of Life and Energy Shield",), ("玩家的生命和能量护盾回复速率总降 #%",), ("少回复", "低回复", "恢复率", "recovery rate", "less recovery")),
    ("max_res", ("t16", "t17"), ("Players have #% to all maximum Resistances",), ("玩家的所有抗性上限 #%",), ("减大抗", "降最大抗", "最大抗性", "抗性上限", "-max", "minus max", "max res")),
    ("less_cdr", ("t16", "t17"), ("Players have #% less Cooldown Recovery Rate",), ("玩家的冷却回复速度总降 #%",), ("少冷却", "减冷却", "冷却回复", "cdr", "cooldown")),
    ("less_aura", ("t16", "t17"), ("Players have #% reduced effect of Non-Curse Auras from Skills",), ("玩家的技能非诅咒光环效果降低 #%",), ("减光环", "少光环", "光环效果", "aura effect", "less aura")),
    ("steal_charges", ("t16",), ("Monsters steal Power, Frenzy and Endurance charges on Hit",), ("怪物击中时偷取暴击球、狂怒球和耐力球",), ("偷球", "steal charges")),
    ("no_charges", ("t17",), ("Players cannot gain Endurance, Frenzy or Power Charges",), ("玩家无法获得耐力球、狂怒球或暴击球",), ("无球", "不能得球", "无法得球", "no charges", "cannot gain charges")),
    ("crit_res", ("t16", "t17"), ("Monsters take #% reduced Extra Damage from Critical Strikes",), ("怪物受到的暴击额外伤害降低 #%",), ("减暴伤", "怪物减暴伤", "reduced crit", "crit res")),
    ("monster_crit", ("t16", "t17"), ("Monsters have #% increased Critical Strike Chance", "#% to Monster Critical Strike Multiplier"), ("怪物的暴击率提高 #%", "怪物的暴击伤害加成 #%"), ("怪物暴击", "暴击怪", "monster crit", "crit chance")),
    ("avoid_ailments", ("t16", "t17"), ("Monsters have #% chance to Avoid Elemental Ailments",), ("怪物有 #% 几率避免元素异常状态",), ("免异常", "避免元素异常", "avoid ailments", "elemental ailments")),
    ("avoid_poison_bleed", ("t16", "t17"), ("Monsters have a #% chance to avoid Poison, Impale, and Bleeding",), ("怪物有 #% 的几率避免中毒、穿刺和流血",), ("免毒", "免流血", "避免中毒", "avoid poison", "avoid bleed")),
    ("ele_weakness", ("t16", "t17"), ("Players are Cursed with Elemental Weakness",), ("玩家受到元素要害诅咒",), ("元素要害", "ele weak", "elemental weakness")),
    ("vulnerability", ("t16", "t17"), ("Players are Cursed with Vulnerability",), ("玩家受到脆弱诅咒",), ("脆弱", "脆弱诅咒", "vuln", "vulnerability")),
    ("temp_chains", ("t16", "t17"), ("Players are Cursed with Temporal Chains",), ("玩家受到时空锁链诅咒",), ("时空锁链", "时锁", "temp chains", "temporal chains")),
    ("enfeeble", ("t16",), ("Players are Cursed with Enfeeble",), ("玩家受到衰弱诅咒",), ("衰弱", "衰弱诅咒", "enfeeble")),
    ("less_armour_block", ("t16", "t17"), ("Players have #% less Armour", "Players have #% reduced Chance to Block"), ("玩家的护甲总降 #%", "玩家的格挡几率降低 #%"), ("少护甲", "减格挡", "破甲", "less armour", "reduced block")),
    ("less_suppression", ("t16", "t17"), ("Players have #% to amount of Suppressed Spell Damage Prevented",), ("玩家阻挡被压制的法术伤害量 #%",), ("减法压", "少法压", "法术压制", "suppression", "suppressed")),
    ("less_aoe", ("t16", "t17"), ("Players have #% less Area of Effect",), ("玩家技能的范围总降 #%",), ("少范围", "减范围", "less aoe", "area of effect")),
    ("less_accuracy", ("t16",), ("Players have #% less Accuracy Rating",), ("玩家的命中值总降 #%",), ("少命中", "减命中", "less accuracy", "accuracy")),
    ("monsters_hexproof", ("t16", "t17"), ("Monsters are Hexproof",), ("怪物免疫诅咒",), ("免诅咒", "无咒", "hexproof")),
    ("less_curse_effect", ("t16", "t17"), ("#% less effect of Curses on Monsters",), ("怪物身上的诅咒效果总降 #%",), ("少诅咒效果", "less curse")),
    ("less_flask_charges", ("t16",), ("Players gain #% reduced Flask Charges",), ("玩家获得的药剂充能降低 #%",), ("少药水充能", "reduced flask charges", "flask charges")),
    ("cannot_inflict_exposure", ("t16",), ("Players cannot inflict Exposure",), ("玩家无法施加元素曝露",), ("无法曝露", "不能曝晒", "no exposure", "exposure")),
    ("buffs_expire_faster", ("t16", "t17"), ("Buffs on Players expire #% faster",), ("玩家身上的增益效果消减速度加快 #%",), ("增益加速失效", "buffs expire")),
    ("burning_ground", ("t16", "t17"), ("Area has patches of Burning Ground",), ("区域内有燃烧地面",), ("燃烧地面", "火地面", "burning ground")),
    ("shocked_ground", ("t16", "t17"), ("Area has patches of Shocked Ground which increase Damage taken by #%",), ("区域内有感电地面，使受到的伤害提高 #%",), ("感电地面", "电地面", "shocked ground")),
    ("desecrated_ground", ("t16", "t17"), ("Area has patches of desecrated ground",), ("区域内有腐化地面",), ("腐化地面", "混沌地面", "desecrated ground")),
    ("action_speed", ("t17",), ("Players have #% reduced Action Speed for each time they've used a Skill Recently",), ("玩家近期每使用过一个技能，行动速度降低 #%",), ("减行速", "行动速度", "石化", "petrification", "action speed")),
    ("desolation", ("t17",), ("Area has patches of Awakeners' Desolation",), ("区域内有觉醒者之殇",), ("觉醒者之殇", "希鲁斯地面", "awakeners desolation", "desolation")),
    ("remove_life_es_on_hit", ("t17",), ("Rare and Unique Monsters remove #% of Life, Mana and Energy Shield from Players or their Minions on Hit",), ("稀有和传奇怪物击中时移除 #% 的生命、魔力和能量护盾",), ("移除血蓝盾", "扣血盾", "remove life", "remove on hit")),
    ("less_defences", ("t17",), ("Players have #% less Defences",), ("玩家的防御属性总降 #%",), ("少防御", "减防御", "less defences", "defences")),
    ("marked_for_death", ("t17",), ("Players are Marked for Death for # seconds after killing a Rare or Unique monster",), ("玩家击败稀有或传奇怪物后被标记死亡，持续 # 秒",), ("标记死亡", "死亡标记", "marked for death")),
    ("volatile_cores", ("t17",), ("Rare Monsters have Volatile Cores",), ("稀有怪物有无常之核",), ("无常之核", "核息", "火球", "volatile", "volatile cores")),
    ("drowning_orbs", ("t17",), ("Area contains Drowning Orbs",), ("区域内有淹没法球",), ("溺亡之球", "淹没法球", "水球", "drowning", "drowning orbs")),
    ("sawblades", ("t17",), ("Players are assaulted by Bloodstained Sawblades",), ("玩家会受到血痕锯刃魔的袭击",), ("锯刃", "血痕锯刃", "血渍锯刃", "sawblades")),
    ("meteor_on_flask", ("t17",), ("Players are targeted by a Meteor when they use a Flask",), ("玩家使用药剂时成为陨石的目标",), ("喝药陨石", "陨石", "meteor", "meteor on flask")),
    ("union_of_souls", ("t17",), ("All Magic and Normal Monsters in Area are in a Union of Souls",), ("区域内的所有魔法和普通怪物为聚魂",), ("聚魂", "灵魂联结", "union of souls")),
    ("debuff_expire_slower", ("t17",), ("Debuffs on Monsters expire #% faster",), ("怪物受到的减益效果消减速度加快 #%",), ("减益加速", "debuff expire", "debuffs expire")),
    ("tentacle_fiends", ("t17",), ("Area contains Unstable Tentacle Fiends",), ("区域包含不稳定的恶魔触手",), ("恶魔触手", "触手", "impale_reflect", "tentacle", "tentacle fiends")),
    ("runes_exarch", ("t17",), ("Area contains Runes of the Searing Exarch",), ("区域内包含焚界者符文",), ("焚界者符文", "总督符文", "exarch runes", "runes")),
    ("labyrinth_hazards", ("t17",), ("Area contains Labyrinth Hazards",), ("区域内包含迷宫陷阱",), ("迷宫陷阱", "labyrinth", "lab hazards")),
    ("marked_ground", ("t17",), ("Area contains patches of moving Marked Ground, inflicting random Marks",), ("区域内有移动的印记地面，施加随机印记",), ("印记地面", "marked ground")),
    ("less_flask_effect", ("t17",), ("Players have #% less effect of Flasks applied to them",), ("玩家受到的药剂效果总降 #%",), ("少药剂效果", "less flask effect", "flask effect")),
    ("less_leech_cap", ("t17",), ("Players have #% reduced Maximum total Life, Mana and Energy Shield Recovery per second from Leech",), ("玩家从偷取中获得的每秒最大生命、魔力和能量护盾总回复量降低 #%",), ("减偷取上限", "less leech", "leech cap")),
    ("enemy_auras", ("t17",), ("Auras from Player Skills which affect Allies also affect Enemies",), ("影响友军的玩家光环技能同样影响敌人",), ("光环影响敌人", "enemy auras")),
    ("maven_interferes", ("t17",), ("The Maven interferes with Players",), ("贤主会干涉玩家",), ("贤主干涉", "释界干扰", "maven")),
    ("shaper_touched", ("t17",), ("Rare monsters in area are Shaper-Touched",), ("区域内的稀有怪物被塑界者触碰",), ("塑界之触", "shaper touched")),
    ("monsters_block", ("t17",), ("Monsters have #% Chance to Block Attack Damage",), ("怪物的攻击伤害格挡几率 #%",), ("怪物格挡", "monster block")),
    ("terrain_chain", ("t17",), ("Monsters' Projectiles can Chain when colliding with Terrain",), ("怪物的投射物击中地形时产生连锁",), ("地形连锁", "terrain chain")),
)

_NORM_WS_RE = re.compile(r"\s+")
_CLIENT_STR_RE = re.compile(r"^\s*([A-Z0-9_]+)\s*:\s*'([^']*)'\s*,?\s*$")
_MODMAP_LINE_RE = re.compile(r"tooltipLines\s*=\s*\{([^}]+)\}")
_LUA_STR_RE = re.compile(r'"([^"]+)"')
_NON_MAP_NOISE_RE = re.compile(
    r"blight|tower|explosive|remnant|runic|sulphur|artifact|excavated|voyage|expedition|oil|ritual|allflame|sentinel|mirage|calcified|perandus|vaal vessel|smuggler|wildwood|sacred grove|invasion|memory strand|originator|evocator|metamorph|incursion|abyss|harbinger|legion|rogue giant|ultimatum",
    re.IGNORECASE,
)


def _norm_key(text: str) -> str:
    s = _NORM_WS_RE.sub(" ", text.replace("\n", " ")).strip().lower()
    return s.replace("minus #", "#").replace("+#", "#")


def _load_ndjson(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _iter_stats(objs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for obj in objs:
        if "ref" in obj:
            out.append(obj)
        for nested in obj.get("stats") or ():
            if isinstance(nested, dict) and "ref" in nested:
                out.append(nested)
    return out


def _parse_client_strings(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = _CLIENT_STR_RE.match(ln)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _extract_modmap_lines(engine_dir: Path | None) -> list[str]:
    if engine_dir is None:
        return []
    modmap = engine_dir / "PoE1" / "Data" / "ModMap.lua"
    if not modmap.is_file():
        return []
    lines: list[str] = []
    text = modmap.read_text(encoding="utf-8", errors="replace")
    for blk in _MODMAP_LINE_RE.findall(text):
        for raw in _LUA_STR_RE.findall(blk):
            clean = re.sub(r"\(%d to %d\)|%d", "#", raw).replace("%%", "%").strip()
            if clean and clean not in lines:
                lines.append(clean)
    return lines


def _build_headers(cs_en: dict[str, str], cs_tw: dict[str, str]) -> dict[str, list[str]]:
    return {
        "en": [
            f"{cs_en.get('ITEM_CLASS', 'Item Class: ')}Maps",
            f"{cs_en.get('RARITY', 'Rarity: ')}{cs_en.get('RARITY_RARE', 'Rare')}",
            "Map Tier: 16",
            f"{cs_en.get('MAP_ITEM_QUANTITY', 'Item Quantity: ')}+100%",
            f"{cs_en.get('MAP_ITEM_RARITY', 'Item Rarity: ')}+50%",
            f"{cs_en.get('MAP_MONSTER_PACK_SIZE', 'Monster Pack Size: ')}+35%",
            f"{cs_en.get('MAP_MORE_CURRENCY', 'More Currency: ')}+50%",
            "More Scarabs: +50%",
            f"{cs_en.get('MAP_MORE_MAPS', 'More Maps: ')}+50%",
            f"{cs_en.get('MAP_MORE_DIVINATION_CARDS', 'More Divination Cards: ')}+50%",
            f"{cs_en.get('QUALITY', 'Quality: ')}+20%",
            cs_en.get("CORRUPTED", "Corrupted"),
            cs_en.get("UNIDENTIFIED", "Unidentified"),
        ],
        "zh_tw": [
            f"{cs_tw.get('ITEM_CLASS', '物品種類: ')}地圖",
            f"{cs_tw.get('RARITY', '稀有度: ')}{cs_tw.get('RARITY_RARE', '稀有')}",
            "地圖階級: 16",
            f"{cs_tw.get('MAP_ITEM_QUANTITY', '物品數量: ')}+100%",
            f"{cs_tw.get('MAP_ITEM_RARITY', '物品稀有度: ')}+50%",
            f"{cs_tw.get('MAP_MONSTER_PACK_SIZE', '怪物群大小: ')}+35%",
            f"{cs_tw.get('MAP_MORE_CURRENCY', '更多通貨: ')}+50%",
            "更多聖甲蟲: +50%",
            f"{cs_tw.get('MAP_MORE_MAPS', '更多地圖: ')}+50%",
            f"{cs_tw.get('MAP_MORE_DIVINATION_CARDS', '更多命運卡: ')}+50%",
            f"{cs_tw.get('QUALITY', '品質: ')}+20%",
            cs_tw.get("CORRUPTED", "已汙染"),
            cs_tw.get("UNIDENTIFIED", "未鑑定"),
        ],
        "zh_cn": [
            "物品类别: 地图",
            "稀 有 度: 稀有",
            "地图阶级: 16",
            "物品数量: +100%",
            "物品稀有度: +50%",
            "怪物群规模: +35%",
            "更多通货: +50%",
            "更多圣甲虫: +50%",
            "更多地图: +50%",
            "更多命运卡: +50%",
            "品质: +20%",
            "已腐化",
            "未鉴定",
        ],
    }


def _build_apt_maps(
    apt_dir: Path,
) -> tuple[dict[str, str], list[tuple[str, str, set[str]]], dict[str, list[str]], dict[str, list[str]]]:
    data_root = apt_dir / "renderer" / "public" / "data"
    en_stats = _iter_stats(_load_ndjson(data_root / "en" / "stats.ndjson"))
    tw_stats = _iter_stats(_load_ndjson(data_root / "cmn-Hant" / "stats.ndjson"))
    tw_by_ref = {st["ref"]: st for st in tw_stats if isinstance(st.get("ref"), str)}

    en_to_tw: dict[str, str] = {}
    area_entries: list[tuple[str, str, set[str]]] = []
    for st in en_stats:
        tw_st = tw_by_ref.get(st.get("ref", ""), {})
        en_objs = [m for m in st.get("matchers") or () if isinstance(m, dict) and m.get("string")]
        tw_objs = [m for m in tw_st.get("matchers") or () if isinstance(m, dict) and m.get("string")]
        en_ms = [str(m["string"]) for m in en_objs]
        tw_ms = [str(m["string"]) for m in tw_objs]
        stat_keys = {_norm_key(em) for em in en_ms}
        for idx, em in enumerate(en_ms):
            tm = tw_ms[idx] if idx < len(tw_ms) else (tw_ms[0] if tw_ms else "")
            if tm:
                en_to_tw.setdefault(_norm_key(em), tm)
        if st.get("fromAreaMods") in ("yes", "ubermap_exclusive"):
            for idx, mobj in enumerate(en_objs):
                if mobj.get("negate"):
                    continue
                flat_en = str(mobj["string"]).replace("\n", " ").strip()
                if _NON_MAP_NOISE_RE.search(flat_en):
                    continue
                tm = (tw_ms[idx] if idx < len(tw_ms) else (tw_ms[0] if tw_ms else "")).replace("\n", " ").strip()
                area_entries.append((flat_en, tm, stat_keys))

    en_items = _load_ndjson(data_root / "en" / "items.ndjson")
    tw_items = _load_ndjson(data_root / "cmn-Hant" / "items.ndjson")
    en_areas = sorted({str(it["name"]).strip() for it in en_items if it.get("namespace") == "AREA" and it.get("name")})
    tw_areas = sorted({str(it["name"]).strip() for it in tw_items if it.get("namespace") == "AREA" and it.get("name")})
    map_names = {"en": en_areas, "zh_tw": tw_areas}
    cs_en = _parse_client_strings(data_root / "en" / "client_strings.js")
    cs_tw = _parse_client_strings(data_root / "cmn-Hant" / "client_strings.js")
    return en_to_tw, area_entries, map_names, _build_headers(cs_en, cs_tw)


def generate_map_mods(
    *,
    apt_dir: Path,
    engine_dir: Path | None = None,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    en_to_tw, area_entries, map_names, headers = _build_apt_maps(apt_dir)
    modmap_lines = _extract_modmap_lines(engine_dir)

    mods: list[dict[str, Any]] = []
    covered_en: set[str] = set()
    for mod_id, tiers, en_lines, cn_fb, aliases in sorted(_PRESET_SPECS, key=lambda x: x[0]):
        tw_resolved: list[str] = []
        for eline in en_lines:
            nk = _norm_key(eline)
            covered_en.add(nk)
            tw_hit = en_to_tw.get(nk) or _TW_FALLBACK.get(nk)
            if not tw_hit:
                raise RuntimeError(f"Missing cmn-Hant translation for {mod_id}: {eline!r}")
            tw_resolved.append(tw_hit)
        mods.append(
            {
                "id": mod_id,
                "tiers": list(tiers),
                "en": list(en_lines),
                "zh_tw": tw_resolved,
                "zh_cn_fallback": list(cn_fb),
                "aliases": list(aliases),
            }
        )

    other_en: list[str] = []
    other_tw: list[str] = []
    seen_en: set[str] = set()
    seen_tw: set[str] = set()

    def _add_other(en_s: str, tw_s: str, stat_keys: set[str] | None = None) -> None:
        ek = _norm_key(en_s)
        if not ek or ek in covered_en or ek in seen_en:
            return
        if stat_keys and (stat_keys & covered_en):
            return
        seen_en.add(ek)
        other_en.append(en_s)
        if tw_s and tw_s not in seen_tw:
            seen_tw.add(tw_s)
            other_tw.append(tw_s)

    for em, tm, skeys in sorted(area_entries, key=lambda pair: pair[0].lower()):
        _add_other(em, tm, skeys)
    for ml in sorted(modmap_lines, key=str.lower):
        _add_other(ml, en_to_tw.get(_norm_key(ml), ""))

    payload: dict[str, Any] = {
        "meta": {
            "schemaVersion": 1,
            "patch": "3.30",
            "sources": [
                "SnosMe/awakened-poe-trade (MIT)",
                "PathOfBuildingCommunity/PathOfBuilding PoE1/Data/ModMap.lua (MIT)",
            ],
            "modCount": len(mods),
            "otherModCount": len(other_en),
            "mapNameCount": len(map_names["en"]),
        },
        "mods": mods,
        "safeCorpus": {
            "otherMods": {"en": other_en, "zh_tw": other_tw},
            "mapNames": map_names,
            "headers": headers,
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    output_path.write_text(text, encoding="utf-8")
    return {"output": str(output_path), "bytes": len(text.encode("utf-8")), **payload["meta"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate clean-room map_mods_3_30.json from MIT APT data.")
    parser.add_argument("--apt-dir", type=Path, required=True, help="Local checkout of SnosMe/awakened-poe-trade.")
    parser.add_argument("--engine-dir", type=Path, default=None, help="Optional local PoB engine root for ModMap.lua.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output JSON file path.")
    args = parser.parse_args(argv)
    summary = generate_map_mods(apt_dir=args.apt_dir, engine_dir=args.engine_dir, output_path=args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
