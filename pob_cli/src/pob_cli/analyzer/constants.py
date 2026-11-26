"""Shared constants and regex patterns for build analysis, de-watering, and pitfall detection."""

from __future__ import annotations

import re
from typing import Any

# Config variables that represent permanent character choices and must NEVER be stripped in de-watering
PERMANENT_CONFIG_VARS = frozenset({
    "bandit",
    "pantheonMajorGod",
    "pantheonMinorGod",
    "resistancePenalty",
    "characterLevel",
})

# Config variables that represent temporary buffs, conditional states, or boss/target overrides
INFLATION_CONFIG_PREFIXES = (
    "use",
    "buff",
    "condition",
    "multiplier",
    "override",
)

INFLATION_CONFIG_EXACT = frozenset({
    "onslaught",
    "adrenaline",
    "fortify",
    "tailwind",
    "elusive",
    "unholyMight",
    "divineBlessing",
    "innervation",
    "infusedChannelling",
})

TEMPORARY_SKILL_PATTERNS = (
    "molten shell",
    "steelskin",
    "immortal call",
    "arcane cloak",
    "berserk",
    "frost shield",
    "sigil of power",
    "focus",
    "vaal breach",
    "熔岩护盾",
    "钢铁之肤",
    "不朽怒嚎",
    "奥术斗篷",
    "狂战",
    "冰霜护盾",
    "威能法印",
    "瓦尔裂隙",
)

# Gem flag values PoB assumes when a gem omits them: SkillsTab:Load reads
# `enableGlobal1 = not attrib or attrib == "true"` and `enableGlobal2 = attrib == "true"`.
POB_GEM_FLAG_DEFAULTS: dict[str, bool] = {"enabled": True, "enableGlobal1": True, "enableGlobal2": False}

# PoB NewConfigSet truthy defaultState values (Modules/ConfigOptions.lua) for inflation-matching
# vars. bridge.lua's list_config does not serialize defaultState/isDefault, and reset_config
# restores these to their defaultState (a no-op when already at defaultState).
_POB_TRUTHY_CONFIG_DEFAULTS: dict[str, Any] = {
    "conditionCorruptingCryStages": 1,
    "conditionChampionIntimidate": True,
    "conditionSacrificeMinion": True,
}

_RESERVATION_AURA_EXACT = frozenset(
    {
        "haste",
        "zealotry",
        "wrath",
        "hatred",
        "anger",
        "grace",
        "determination",
        "discipline",
        "malevolence",
        "vitality",
        "clarity",
        "precision",
        "pride",
        "petrified blood",
        "tempest shield",
        "arctic armour",
        "flesh and stone",
        "blood and sand",
        "defiance banner",
        "dread banner",
        "war banner",
        "summon skitterbots",
        "skitterbots",
    }
)
_RESERVATION_AURA_PREFIXES = (
    "purity of ",
    "aspect of ",
    "herald of ",
)
_RESERVATION_AURA_ZH = (
    "迅捷",
    "狂热",
    "狂熱",
    "雷霆",
    "憎恨",
    "愤怒",
    "憤怒",
    "优雅",
    "優雅",
    "坚定",
    "堅定",
    "纪律",
    "紀律",
    "怨毒",
    "活力",
    "清晰",
    "精准",
    "精準",
    "骄傲",
    "驕傲",
    "纯净",
    "純淨",
    "石化之血",
    "暴风之盾",
    "暴風之盾",
    "极地装甲",
    "極地裝甲",
    "血肉与岩石",
    "血肉與岩石",
    "血与沙",
    "血與沙",
    "之捷",
    "之势",
    "之勢",
    "召唤飞掠者",
    "召喚飛掠者",
)

_KNOWN_SKILL_BASE_COOLDOWNS: dict[str, float] = {
    "hexblast of contradiction": 1.0,
}

_GLOBAL_CDR_LINE_RE = re.compile(
    r"^(?:\{[^}]+\})*\s*\+?(\d+(?:\.\d+)?)%\s+increased\s+cooldown\s+recovery\s+rate\s*$",
    re.IGNORECASE,
)
_ITEM_SOCKETS_RE = re.compile(r"^Sockets:\s*([RGBWADrgbwad\-\s]+)$", re.MULTILINE)
_ITEM_SEL_VARIANT_RE = re.compile(r"^Selected Variant:\s*(\d+)\s*$", re.MULTILINE)
_MOD_VARIANT_RE = re.compile(r"\{variant:([0-9,]+)\}")
_ELE_CURSE_OR_EXPOSURE_NAMES = frozenset(
    {
        "conductivity",
        "flammability",
        "frostbite",
        "elemental weakness",
        "wave of conviction",
        "hydrosphere",
        "frost bomb",
    }
)

# Exact PoE1 3.30 Cast while Channelling (sup_int.lua) fixed trigger intervals (ms) by gem level
_CWC_INTERVAL_MS: dict[int, int] = {
    1: 400, 2: 390, 3: 390, 4: 380, 5: 380, 6: 380, 7: 370, 8: 370, 9: 360, 10: 360,
    11: 360, 12: 350, 13: 350, 14: 340, 15: 340, 16: 340, 17: 330, 18: 330, 19: 320, 20: 320,
    21: 320, 22: 310, 23: 310, 24: 300, 25: 300, 26: 300, 27: 290, 28: 290, 29: 280, 30: 280,
}
_AWAKENED_CWC_INTERVAL_MS: dict[int, int] = {
    1: 340, 2: 330, 3: 320, 4: 310, 5: 300, 6: 300, 7: 290, 8: 290, 9: 280, 10: 280,
}

# Supports whose value is non-DPS utility, multi-hit overlap, or handled by dedicated checks
# (exempt from generic 0%-delta "dead support" alarm so we NEVER false-alarm).
_DEAD_SUPPORT_EXEMPT_NAMES = frozenset(
    {
        "enlighten",
        "empower",
        "enhance",
        "lifetap",
        "item rarity",
        "item quantity",
        "culling strike",
        "life leech",
        "mana leech",
        "energy shield leech",
        "life gain on hit",
        "fortify",
        "blind",
        "maim",
        "power charge on critical",
        "endurance charge on melee stun",
        "rage",
        "arcane surge",
        "greater multiple projectiles",
        "awakened greater multiple projectiles",
        "lesser multiple projectiles",
        "volley",
        "greater volley",
        "returning projectiles",
        "spell cascade",
        "greater spell cascade",
        "awakened spell cascade",
        "multistrike",
        "greater multistrike",
        "awakened multistrike",
        "spell echo",
        "greater spell echo",
        "awakened spell echo",
        "unleash",
        "chain",
        "greater chain",
        "awakened chain",
        "fork",
        "greater fork",
        "awakened fork",
        "pierce",
        "greater pierce",
        "awakened pierce",
        "ancestral call",
        "greater ancestral call",
        "awakened ancestral call",
        "melee splash",
        "awakened melee splash",
        "increased area of effect",
        "increased duration",
        "less duration",
        "second wind",
        "cast on critical strike",
        "awakened cast on critical strike",
        "cast while channelling",
        "awakened cast while channelling",
        "cast when damage taken",
        "cast when stunned",
        "cast on death",
        "cast on melee kill",
        "cast on ward break",
        "spellslinger",
        "arcanist brand",
        "spell totem",
        "ballista totem",
        "multiple totems",
        "trap",
        "cluster traps",
        "multiple traps",
        "high-impact mine",
        "blastchain mine",
        "minefield",
        "automation",
        "autoexertion",
        "generosity",
        "awakened generosity",
        "arrogance",
        "eternal blessing",
        "guardian's blessing",
        "hextouch",
        "awakened hextouch",
        "mark on hit",
        "blasphemy",
        "awakened blasphemy",
        "manaforged arrows",
        "bonechill",
        "feeding frenzy",
        "meat shield",
        "elemental army",
        "minion life",
        "minion speed",
        "summon phantasm",
        "withering touch",
        "mirage archer",
        "focused ballista",
        "shockwave",
        "prismatic burst",
    }
)

_AILMENT_DEPENDENT_SUPPORTS = frozenset({"hypothermia", "immolate", "combustion", "ice bite", "innervate"})
_ELE_OR_CHAOS_DAMAGE_SUPPORTS = frozenset(
    {
        "added fire damage",
        "added cold damage",
        "added lightning damage",
        "added chaos damage",
        "elemental damage with attacks",
        "elemental focus",
        "void manipulation",
        "trinity",
        "cold penetration",
        "fire penetration",
        "lightning penetration",
        "elemental penetration",
        "hypothermia",
        "immolate",
    }
)
_PURE_ELE_DAMAGE_SUPPORTS = frozenset(
    {
        "added fire damage",
        "added cold damage",
        "added lightning damage",
        "elemental damage with attacks",
        "elemental focus",
        "trinity",
        "cold penetration",
        "fire penetration",
        "lightning penetration",
        "elemental penetration",
        "hypothermia",
        "immolate",
    }
)
_ELE_AURA_NAMES = frozenset({"hatred", "anger", "wrath"})

_KEEP_KEYWORDS = ("flasks", "custommods", "party")
DEWATER_SENTINEL_TITLE = "pob-cli dewater"

_KNOWN_CONFIG_DEFAULTS: dict[str, Any] = {
    "enemyIsBoss": "Pinnacle",
    "resistancePenalty": -60,
    "bandit": "None",
    "pantheonMajorGod": "None",
    "pantheonMinorGod": "None",
    "presetBossSkills": "None",
    **_POB_TRUTHY_CONFIG_DEFAULTS,
}

_ALWAYS_SHOW_VISIBLE_CONFIG_VARS = frozenset({
    "enemyIsBoss",
})

JEWEL_RADII_316: list[tuple[str, float]] = [
    ("Small", 960.0),
    ("Medium", 1440.0),
    ("Large", 1800.0),
    ("Very Large", 2400.0),
    ("Massive", 2880.0),
]

JEWEL_RADII_315: list[tuple[str, float]] = [
    ("Small", 800.0),
    ("Medium", 1200.0),
    ("Large", 1500.0),
]

_TRIGGER_ATTACK_HINTS = (
    "cyclone",
    "lancing steel",
    "blast rain",
    "barrage",
    "kinetic blast",
    "power siphon",
    "frenzy",
    "rain of arrows",
    "split arrow",
    "spectral throw",
    "blade flurry",
    "flicker strike",
    "smite",
    "lightning strike",
    "molten strike",
    "frost blades",
    "double strike",
    "reave",
    "static strike",
    "shield charge",
    "whirling blades",
    "leap slam",
    "consecrated path",
    "tornado shot",
    "ice shot",
    "lightning arrow",
    "cobra lash",
    "venom gyre",
    "splitting steel",
    "shattering steel",
)

_PANTHEON_HINTS: dict[str, str] = {
    "TheBrineKing": "Freeze Immune + Stun Lock Protection",
    "Arakaali": "Reduced DoT Taken + Chaos DoT Res",
    "Lunaris": "Phys DR / Move Speed / Avoid Projectiles",
    "Solaris": "Boss Phys DR + Reduced Crit Damage",
    "Tukohama": "Stationary Phys DR & Life Regen",
    "Gruthkul": "Phys DR on Hit + Maim Attackers",
    "Abberath": "Unaffected by Burning Ground + Faster Ignite Expire",
    "Shakari": "Immune to Poison + Reduced Chaos Taken",
    "Ralakesh": "25% Reduced Phys DoT + Bleed/CB Mitigation",
    "Garukhan": "Reduced Shock Effect + Blind Immunity",
    "Ryslatha": "Life Flask Charges Over Time",
    "Yugul": "50% Reduced Reflected Damage + 20% Reduced Curse Effect",
}
