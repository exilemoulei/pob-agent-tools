"""Shared 3.30 mod-entry unwrapping, rich-text markup stripping, and mainSocketGroup heuristic (L1)."""

from __future__ import annotations

import re
from typing import Any

_POE_MARKUP_RE = re.compile(r"\[(?:[^|\[\]]*\|)?([^\[\]]+)\]")

_PURE_UTILITY_ACTIVES = frozenset(
    {
        "leap slam", "leap slam of groundbreaking", "shield charge", "whirling blades",
        "flame dash", "dash", "frostblink", "blink arrow", "lightning warp", "smoke mine",
        "phase run", "withering step", "arcane cloak", "molten shell", "steelskin",
        "immortal call", "bone armour", "automation", "autoexertion", "blood rage",
        "sigil of power", "frost shield", "convocation", "portal", "detonate mines",
        "assassin's mark", "sniper's mark", "poacher's mark", "warlord's mark", "alchemist's mark",
        "flammability", "conductivity", "frostbite", "elemental weakness", "despair",
        "punishment", "temporal chains", "enfeeble", "vulnerability", "wrath", "hatred",
        "anger", "grace", "determination", "discipline", "clarity", "precision", "vitality",
        "zealotry", "malevolence", "pride", "haste", "purity of fire", "purity of ice",
        "purity of lightning", "purity of elements", "flesh and stone", "blood and sand",
        "petrified blood", "tempest shield", "arctic armour", "summon skitterbots",
        "dread banner", "defiance banner", "war banner", "enduring cry", "ancestral cry",
        "rallying cry", "intimidating cry", "battlemage's cry", "infernal cry", "seismic cry",
        "vengeful cry", "berserk", "flesh offering", "bone offering", "spirit offering",
    }
)
_SECONDARY_UTILITY_ACTIVES = frozenset(
    {
        "raise spectre", "animate guardian", "righteous fire",
        "summon stone golem", "summon chaos golem", "summon lightning golem",
        "summon ice golem", "summon flame golem", "summon carrion golem",
    }
)
_SWAP_SLOTS = frozenset({"Weapon 1 Swap", "Weapon 2 Swap"})


def strip_poe_markup(text: str) -> str:
    """Strip PoE 3.30 rich-text bracket tags (`[id|Display]` -> `Display`, `[Keyword]` -> `Keyword`)."""
    return _POE_MARKUP_RE.sub(r"\1", str(text or "")).strip()


def extract_mod_entries(raw_list: Any) -> list[tuple[str, dict[str, Any]]]:
    """Extract `(clean_line, flags_dict)` from legacy string lists or 3.30 `{description, flags}` dicts."""
    if not isinstance(raw_list, list):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for entry in raw_list:
        flags: dict[str, Any] = {}
        if isinstance(entry, str):
            raw_text = entry
        elif isinstance(entry, dict):
            raw_text = str(entry.get("description") or entry.get("text") or "")
            raw_flags = entry.get("flags")
            if isinstance(raw_flags, dict):
                flags = raw_flags
        else:
            continue
        for sub in raw_text.split("\n"):
            cleaned = strip_poe_markup(sub)
            if cleaned:
                out.append((cleaned, flags))
    return out


def apply_mod_flags(line: str, flags: dict[str, Any], *, forced_tag: str | None = None) -> str:
    """Prefix a PoB item mod line with `{crafted}`/`{fractured}`/`{mutated}`/etc. from flags."""
    prefixes: list[str] = []
    if forced_tag:
        prefixes.append(f"{{{forced_tag}}}")
    for key in ("fractured", "crafted", "mutated", "scourge", "crucible", "vestigial"):
        if flags.get(key) and key != forced_tag:
            prefixes.append(f"{{{key}}}")
    return "".join(prefixes) + line


def pick_main_socket_group(groups: list[tuple[int, str, list[tuple[str, bool]]]]) -> int:
    """Pick the most likely main damage skill group index (1-based) over travel/aura/utility links."""
    if not groups:
        return 1

    def _score(entry: tuple[int, str, list[tuple[str, bool]]]) -> tuple[int, int, int, int, int, int]:
        idx, slot_name, gems = entry
        not_swap = 0 if slot_name in _SWAP_SLOTS else 1
        actives = [name for name, is_sup in gems if not is_sup and name]
        sup_count = sum(1 for _, is_sup in gems if is_sup)
        has_active = 1 if actives else 0
        tier = 0
        for act in actives:
            low = act.removeprefix("Vaal ").strip().lower()
            if low in _PURE_UTILITY_ACTIVES or low.startswith(("purity of ", "herald of ")):
                continue
            if low in _SECONDARY_UTILITY_ACTIVES:
                # 4L+ RF or 5L+ Raise Spectre can be main; 1-4L Spectre/Golem/1-3L RF is utility
                min_links = 4 if low == "righteous fire" else 5
                tier = max(tier, 2 if len(gems) >= min_links else 1)
            else:
                tier = 2
                break
        return (not_swap, has_active, tier, len(gems), sup_count, -idx)

    return max(groups, key=_score)[0]


CLASS_ZH_TO_EN: dict[str, str] = {
    "贵族": "Scion", "升华使徒": "Ascendant",
    "野蛮人": "Marauder", "勇士": "Juggernaut", "暴徒": "Berserker", "酋长": "Chieftain",
    "游侠": "Ranger", "锐眼": "Deadeye", "侠客": "Raider", "追猎者": "Pathfinder", "守望者": "Warden",
    "女巫": "Witch", "秘术家": "Occultist", "元素使": "Elementalist", "死灵师": "Necromancer",
    "决斗者": "Duelist", "处刑者": "Slayer", "卫士": "Gladiator", "冠军": "Champion",
    "圣堂武僧": "Templar", "判官": "Inquisitor", "圣宗": "Hierophant", "守护者": "Guardian",
    "暗影刺客": "Shadow", "刺客": "Assassin", "破坏者": "Saboteur", "欺诈师": "Trickster",
}

PROP_ZH_TO_EN: dict[str, str] = {
    "品质": "Quality", "护甲": "Armour", "闪避值": "Evasion Rating", "能量护盾": "Energy Shield",
    "结界": "Ward", "范围": "Radius", "仅限": "Limited to", "等级": "Level",
    "格挡几率": "Chance to Block", "暴击几率": "Critical Strike Chance",
    "每秒攻击次数": "Attacks per Second", "物理伤害": "Physical Damage",
    "元素伤害": "Elemental Damage", "混沌伤害": "Chaos Damage",
}

REQ_ZH_TO_EN: dict[str, str] = {
    "等级": "Level", "职业：": "Class:", "职业:": "Class:", "力量": "Str", "敏捷": "Dex", "智慧": "Int",
}

INFLUENCE_WRAPPERS: tuple[tuple[str, str], ...] = (
    ("传奇怪物在场时，", "While a Unique Enemy is in your Presence, "),
    ("传奇怪物在场时：", "While a Unique Enemy is in your Presence, "),
    ("异界终极首领在场时，", "While a Pinnacle Atlas Boss is in your Presence, "),
    ("异界终极首领在场时：", "While a Pinnacle Atlas Boss is in your Presence, "),
)
