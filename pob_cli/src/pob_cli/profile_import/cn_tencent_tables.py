"""Static Tencent CN cluster enchant tables and re-exported entity aliases (L1)."""

from __future__ import annotations

import re

from ..game_data import fold_han
from ..game_data.tables_cn_tencent import (
    TENCENT_BASE_ALIASES,
    TENCENT_RUNEGRAFT_ALIASES,
    TENCENT_TATTOO_ALIASES,
    TENCENT_UNIQUE_ALIASES,
)

__all__ = [
    "CLUSTER_BASE_ENCHANT_MAP",
    "CLUSTER_GRANT_PREFIX",
    "CLUSTER_REVERSED_PAIRS",
    "TENCENT_BASE_ALIASES",
    "TENCENT_RUNEGRAFT_ALIASES",
    "TENCENT_TATTOO_ALIASES",
    "TENCENT_UNIQUE_ALIASES",
]

CLUSTER_GRANT_PREFIX = "Added Small Passive Skills grant: "

# All 60 unique ClusterJewels.lua base enchant suffixes mapped from compact folded CN.
_CLUSTER_BASE_RAW: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("4% increased maximum Life", ("生命上限提高4%", "最大生命提高4%", "增加4%最大生命")),
    ("6% increased maximum Energy Shield", ("能量护盾上限提高6%", "最大能量护盾提高6%", "增加6%最大能量护盾")),
    ("6% increased maximum Mana", ("魔力上限提高6%", "最大魔力提高6%", "增加6%最大魔力")),
    ("15% increased Armour", ("护甲提高15%", "护甲值提高15%", "增加15%护甲")),
    ("15% increased Evasion Rating", ("闪避值提高15%", "增加15%闪避值")),
    ("+2% Chance to Block Attack Damage", ("+2%攻击伤害格挡几率", "+2%攻击伤害格挡率", "攻击伤害格挡几率+2%")),
    ("2% Chance to Block Spell Damage", ("2%法术伤害格挡几率", "2%法术伤害格挡率", "+2%法术伤害格挡几率", "法术伤害格挡几率+2%")),
    ("+15% to Fire Resistance", ("+15%火焰抗性",)),
    ("+15% to Cold Resistance", ("+15%冰霜抗性", "+15%冰冷抗性")),
    ("+15% to Lightning Resistance", ("+15%闪电抗性",)),
    ("+12% to Chaos Resistance", ("+12%混沌抗性",)),
    ("+4% chance to Suppress Spell Damage", ("法术伤害压制率+4%", "+4%法术伤害压制率", "+4%压抑法术伤害率")),
    ("+10 to Strength", ("+10力量",)),
    ("+10 to Dexterity", ("+10敏捷",)),
    ("+10 to Intelligence", ("+10智慧",)),
    ("6% increased Mana Reservation Efficiency of Skills", ("技能的魔力保留效能提高6%", "技能的魔力保留效率提高6%", "增加6%技能的魔力保留效用")),
    ("2% increased Effect of your Curses", ("你所施放诅咒的效果提高2%", "你的诅咒效果提高2%", "增加2%你的诅咒效果")),
    ("12% increased Burning Damage", ("燃烧伤害提高12%", "增加12%燃烧伤害")),
    ("12% increased Chaos Damage over Time", ("持续混沌伤害提高12%", "混沌持续伤害提高12%", "增加12%混沌持续伤害")),
    ("12% increased Physical Damage over Time", ("持续物理伤害提高12%", "物理持续伤害提高12%", "增加12%物理持续伤害")),
    ("12% increased Cold Damage over Time", ("持续冰霜伤害提高12%", "冰霜持续伤害提高12%", "持续冰冷伤害提高12%", "增加12%冰冷持续伤害")),
    ("10% increased Damage over Time", ("持续伤害提高10%", "增加10%持续伤害")),
    ("10% increased Effect of Non-Damaging Ailments", ("非伤害型异常状态效果提高10%", "增加10%非伤害型异常状态的效果")),
    ("3% increased effect of Non-Curse Auras from your Skills", ("你技能的非诅咒类光环效果提高3%", "你技能的非诅咒光环效果提高3%", "增加3%你技能的非诅咒光环效果")),
    ("10% increased Damage while affected by a Herald", ("受捷影响时，伤害提高10%", "受捷影响时,伤害提高10%", "被捷光环影响时，增加10%伤害")),
    ("Minions deal 10% increased Damage while you are affected by a Herald", ("你受捷影响时，召唤生物的伤害提高10%", "你受捷影响时,召唤生物的伤害提高10%", "被捷影响时，召唤生物的伤害提高10%")),
    ("Exerted Attacks deal 20% increased Damage", ("增助攻击的伤害提高20%", "竭尽攻击增加20%伤害")),
    ("15% increased Critical Strike Chance", ("暴击率提高15%", "增加15%暴击率")),
    ("Minions have 12% increased maximum Life", ("召唤生物的最大生命提高12%", "召唤生物的生命上限提高12%", "召唤物增加12%最大生命")),
    ("10% increased Area Damage", ("范围伤害提高10%", "增加10%范围伤害")),
    ("10% increased Projectile Damage", ("投射物伤害提高10%", "增加10%投射物伤害")),
    ("12% increased Trap Damage", ("陷阱伤害提高12%", "增加12%陷阱伤害")),
    ("12% increased Mine Damage", ("地雷伤害提高12%", "增加12%地雷伤害")),
    ("12% increased Totem Damage", ("图腾伤害提高12%", "增加12%图腾伤害")),
    ("12% increased Brand Damage", ("烙印伤害提高12%", "增加12%烙印伤害")),
    ("Channelling Skills deal 12% increased Damage", ("持续吟唱技能伤害提高12%", "持续吟唱技能的伤害提高12%", "引导施放技能增加12%伤害")),
    ("6% increased Flask Effect Duration", ("药剂效果的持续时间延长6%", "药剂效果持续时间延长6%", "增加6%药剂效果持续时间")),
    ("10% increased Life Recovery from Flasks", ("药剂给予的生命恢复提高10%", "增加10%来自药剂的生命回复")),
    ("10% increased Mana Recovery from Flasks", ("药剂给予的魔力恢复提高10%", "增加10%来自药剂的魔力回复")),
    ("Axe Attacks deal 12% increased Damage with Hits and Ailments", ("斧类攻击造成的击中和异常状态伤害提高12%", "斧攻击增加12%击中和异常状态伤害")),
    ("Sword Attacks deal 12% increased Damage with Hits and Ailments", ("剑类攻击造成的击中和异常状态伤害提高12%", "剑攻击增加12%击中和异常状态伤害")),
    ("Staff Attacks deal 12% increased Damage with Hits and Ailments", ("长杖攻击造成的击中和异常状态伤害提高12%", "长杖攻击增加12%击中和异常状态伤害")),
    ("Mace or Sceptre Attacks deal 12% increased Damage with Hits and Ailments", ("锤类或短杖攻击造成的击中和异常状态伤害提高12%", "锤或权杖攻击增加12%击中和异常状态伤害")),
    ("Claw Attacks deal 12% increased Damage with Hits and Ailments", ("爪类攻击造成的击中和异常状态伤害提高12%", "爪攻击增加12%击中和异常状态伤害")),
    ("Dagger Attacks deal 12% increased Damage with Hits and Ailments", ("匕首攻击造成的击中和异常状态伤害提高12%", "匕首攻击增加12%击中和异常状态伤害")),
    ("12% increased Damage with Bows", ("弓类伤害提高12%", "持弓类武器时增加12%伤害")),
    ("12% increased Damage Over Time with Bow Skills", ("弓类技能的持续伤害效果提高12%", "弓类技能的持续伤害提高12%", "增加12%弓技能持续伤害")),
    ("Wand Attacks deal 12% increased Damage with Hits and Ailments", ("法杖攻击造成的击中和异常状态伤害提高12%", "法杖攻击增加12%击中和异常状态伤害")),
    ("12% increased Damage with Two Handed Weapons", ("双手武器的攻击伤害提高12%", "双手武器的伤害提高12%", "持双手武器时伤害提高12%")),
    ("12% increased Attack Damage while Dual Wielding", ("攻击伤害在双持武器时提高12%", "双持武器时攻击伤害提高12%", "双持时攻击伤害提高12%")),
    ("12% increased Attack Damage while holding a Shield", ("持盾牌时造成的攻击伤害提高12%", "持盾时造成的攻击伤害提高12%", "持盾牌时造成的攻击伤害增加12%")),
    ("10% increased Attack Damage", ("攻击伤害提高10%", "增加10%攻击伤害")),
    ("10% increased Spell Damage", ("法术伤害提高10%", "增加10%法术伤害")),
    ("10% increased Elemental Damage", ("元素伤害提高10%", "增加10%元素伤害")),
    ("12% increased Physical Damage", ("物理伤害提高12%", "增加12%物理伤害")),
    ("12% increased Fire Damage", ("火焰伤害提高12%", "增加12%火焰伤害")),
    ("12% increased Lightning Damage", ("闪电伤害提高12%", "增加12%闪电伤害")),
    ("12% increased Cold Damage", ("冰霜伤害提高12%", "冰冷伤害提高12%", "增加12%冰冷伤害")),
    ("12% increased Chaos Damage", ("混沌伤害提高12%", "增加12%混沌伤害")),
    ("Minions deal 10% increased Damage", ("召唤生物的伤害提高10%", "召唤物增加10%伤害")),
)

CLUSTER_BASE_ENCHANT_MAP: dict[str, str] = {
    re.sub(r"\s+", "", fold_han(zh)).lower(): f"{CLUSTER_GRANT_PREFIX}{en}"
    for en, zhs in _CLUSTER_BASE_RAW
    for zh in zhs
}

# Swapped 2-line cluster pairs (second_suffix, first_suffix) that must be reversed to match ClusterJewels.lua.
CLUSTER_REVERSED_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Mace or Sceptre Attacks deal 12% increased Damage with Hits and Ailments", "Staff Attacks deal 12% increased Damage with Hits and Ailments"),
        ("Dagger Attacks deal 12% increased Damage with Hits and Ailments", "Claw Attacks deal 12% increased Damage with Hits and Ailments"),
        ("Sword Attacks deal 12% increased Damage with Hits and Ailments", "Axe Attacks deal 12% increased Damage with Hits and Ailments"),
        ("12% increased Mine Damage", "12% increased Trap Damage"),
        ("10% increased Mana Recovery from Flasks", "10% increased Life Recovery from Flasks"),
        ("12% increased Damage Over Time with Bow Skills", "12% increased Damage with Bows"),
    }
)
