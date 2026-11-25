"""Shared constants, regex patterns, and module-level caches for PoE1 game_data."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

from ..updater import detect_source_checkout_root, get_runtime_dir

# 12: Index ModDelve/ModVeiled + DelveDropOnly/IncursionDropOnly/Essence drop-slot tags.
# 13: Mods keep their weight keys in PoB's order, bases their influence tags (`mod --slot`).
SCHEMA_VERSION = 13
_INDEX_BUILD_LOCK = threading.RLock()

_POB_ESCAPE_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")
_ACTIVE_AWAKENED_EXCEPTIONAL_LOWER: frozenset[str] = frozenset(
    {"awakened empower", "awakened enhance", "awakened enlighten"}
)

# Built-in offline fallback bounds for PoE1 3.28+ Exceptional supports (nat 3 / corrupt 4;
# Awakened Empower/Enhance/Enlighten nat 4 / corrupt 5) so offline tests work without SQLite.
_EXCEPTIONAL_LV3_NAMES: tuple[str, ...] = (
    "Annihilation",
    "Bloodsoaked Banner",
    "Bonespire",
    "Cast on Ward Break",
    "Communion",
    "Companionship",
    "Congregation",
    "Cooldown Recovery",
    "Coursing Current",
    "Crystalfall",
    "Cull the Weak",
    "Divine Sentinel",
    "Eclipse",
    "Eldritch Blasphemy",
    "Empower",
    "Enhance",
    "Enlighten",
    "Fissure",
    "Foulgrasp",
    "Frostmage",
    "Gluttony",
    "Greater Ancestral Call",
    "Greater Chain",
    "Greater Devour",
    "Greater Fork",
    "Greater Kinetic Instability",
    "Greater Multistrike",
    "Greater Spell Cascade",
    "Greater Spell Echo",
    "Greater Unleash",
    "Hexpass",
    "Hextoad",
    "Hiveborn",
    "Invention",
    "Invert the Rules",
    "Lethal Dose",
    "Machinations",
    "Overheat",
    "Overloaded Intensity",
    "Pacifism",
    "Pyre",
    "Scornful Herald",
    "Transfusion",
    "Unholy Trinity",
    "Vaal Sacrifice",
    "Vaal Temptation",
    "Void Shockwave",
    "Voidstorm",
)

_EXCEPTIONAL_GEM_FALLBACK_BOUNDS: dict[str, dict[str, Any]] = {
    **{
        nm.lower(): {
            "name": nm,
            "natural_max_level": 3,
            "corrupted_max_level": 4,
            "is_exceptional": True,
            "legacy_removed_328": False,
        }
        for nm in _EXCEPTIONAL_LV3_NAMES
    },
    "awakened empower": {
        "name": "Awakened Empower",
        "natural_max_level": 4,
        "corrupted_max_level": 5,
        "is_exceptional": True,
        "legacy_removed_328": False,
    },
    "awakened enhance": {
        "name": "Awakened Enhance",
        "natural_max_level": 4,
        "corrupted_max_level": 5,
        "is_exceptional": True,
        "legacy_removed_328": False,
    },
    "awakened enlighten": {
        "name": "Awakened Enlighten",
        "natural_max_level": 4,
        "corrupted_max_level": 5,
        "is_exceptional": True,
        "legacy_removed_328": False,
    },
}

_REMOVED_LEGACY_GEM_NAMES_LOWER: frozenset[str] = frozenset(
    {
        "divine blessing",
        "divine blessing support",
        "ancestral protector",
        "ancestral warchief",
        "vaal ancestral warchief",
        "call to arms",
    }
)

_FOLD_CHAR_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u02bc": "'",
        "\u0060": "'",
        "\u00b4": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u3000": " ",
        "\u00a0": " ",
    }
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CJK_PARTICLE_RE = re.compile(r"[之的・·．\s]+")

# Constants from PoE1/Data/Misc.lua (GameConstants.dat) used by CalcTools.lua
SKILL_DAMAGE_BASE_EFFECTIVENESS = 3.885209
SKILL_DAMAGE_INCREMENTAL_EFFECTIVENESS = 0.360246

DEFAULT_PROJECT_ROOT = (
    Path(os.environ["POB_HOME"].strip())
    if os.environ.get("POB_HOME", "").strip()
    else (detect_source_checkout_root() or get_runtime_dir())
)

# Process-local memoization to avoid re-parsing manifest.xml or re-opening SQLite on warm queries
_MANIFEST_VER_CACHE: dict[tuple[str, int, int], str] = {}
_ZH_META_VER_CACHE: dict[tuple[str, int, int], str] = {}
_VALID_INDEX_CACHE: set[tuple[str, str, str]] = set()

LUA_SKIP_KEYS_MOD: frozenset[str] = frozenset(
    {"tradeHashes", "statOrder", "weightMultiplierKey", "weightMultiplierVal"}
)
LUA_SKIP_KEYS_SKILL: frozenset[str] = frozenset(
    {
        "skillTypes",
        "minionSkillTypes",
        "statMap",
        "baseMods",
        "qualityMods",
        "levelMods",
        "requireSkillTypes",
        "addSkillTypes",
        "excludeSkillTypes",
        "notMinionStat",
    }
)
SKILL_KEEP_LEVELS: frozenset[int] = frozenset({1, 3, 4, 5, 6, 7, 20, 21})

MOD_FILES: tuple[tuple[str, str], ...] = (
    ("ModExplicit.lua", "Explicit"),
    ("ModDelve.lua", "Delve"),
    ("ModVeiled.lua", "Veiled"),
    ("ModCorrupted.lua", "Corrupted"),
    ("ModMaster.lua", "Bench"),
    ("ModEldritch.lua", "Eldritch"),
    ("ModTincture.lua", "Tincture"),
    ("ModFoulborn.lua", "Foulborn"),
    ("ModJewel.lua", "Jewel"),
    ("ModJewelCluster.lua", "JewelCluster"),
)

SKILL_FILES: tuple[str, ...] = (
    "act_str.lua",
    "act_dex.lua",
    "act_int.lua",
    "sup_str.lua",
    "sup_dex.lua",
    "sup_int.lua",
    "other.lua",
    "glove.lua",
    "minion.lua",
)
ITEM_SKILL_FILES: frozenset[str] = frozenset({"other.lua", "glove.lua", "minion.lua"})

COLOR_NAMES: dict[int, str] = {
    1: "Red",
    2: "Green",
    3: "Blue",
    4: "White",
}

UNIQUE_META_KEYS: frozenset[str] = frozenset(
    {
        "Variant",
        "Version",
        "Has Alt Variant",
        "Has Alt Variant Two",
        "Has Alt Variant Three",
        "Selected Variant",
        "Selected Alt Variant",
        "Selected Alt Variant Two",
        "Selected Alt Variant Three",
        "League",
        "Source",
        "LevelReq",
        "Level",
        "Requirements",
        "Requires Level",
        "Sockets",
        "Item Level",
        "Radius",
        "Limited to",
        "Upgrade",
        "Talisman Tier",
        "Crafted",
        "Suffix",
    }
)

# Slot aliases -> (weightKeys, parent_zero_check_tags, ModMaster types)
SLOT_ALIASES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "body": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "body_armour": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "chest": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "helmet": (("helmet",), ("helmet", "armour", "default"), ("Helmet",)),
    "helm": (("helmet",), ("helmet", "armour", "default"), ("Helmet",)),
    "gloves": (("gloves",), ("gloves", "armour", "default"), ("Gloves",)),
    "boots": (("boots",), ("boots", "armour", "default"), ("Boots",)),
    "shield": (("shield",), ("shield", "armour", "default"), ("Shield",)),
    "quiver": (("quiver",), ("quiver", "default"), ("Quiver",)),
    "amulet": (("amulet",), ("amulet", "default"), ("Amulet",)),
    "ring": (("ring",), ("ring", "default"), ("Ring",)),
    "ring_1": (("ring",), ("ring", "default"), ("Ring",)),
    "ring_2": (("ring",), ("ring", "default"), ("Ring",)),
    "ring_3": (("ring",), ("ring", "default"), ("Ring",)),
    "belt": (("belt",), ("belt", "default"), ("Belt",)),
    "flask": (("flask",), ("flask", "default"), ("Flask",)),
    "tincture": (("tincture",), ("tincture", "default"), ("Tincture",)),
    "jewel": (("jewel", "abyss_jewel"), ("jewel", "default"), ()),
    "abyss_jewel": (("abyss_jewel",), ("abyss_jewel", "default"), ()),
    "cluster_jewel": (
        ("expansion_jewel_large", "expansion_jewel_medium", "expansion_jewel_small"),
        ("default",),
        (),
    ),
    "bow": (("bow",), ("bow", "weapon", "ranged", "default"), ("Bow",)),
    "wand": (("wand",), ("wand", "one_hand_weapon", "onehand", "weapon", "ranged", "default"), ("Wand",)),
    "claw": (("claw",), ("claw", "one_hand_weapon", "onehand", "weapon", "default"), ("Claw",)),
    "dagger": (("dagger", "attack_dagger"), ("dagger", "one_hand_weapon", "onehand", "weapon", "default"), ("Dagger",)),
    "sceptre": (("sceptre",), ("sceptre", "mace", "one_hand_weapon", "onehand", "weapon", "default"), ("Sceptre",)),
    "staff": (("staff", "warstaff", "attack_staff"), ("staff", "two_hand_weapon", "weapon", "default"), ("Staff",)),
    "sword": (
        ("sword", "2h_sword"),
        ("sword", "weapon", "default"),
        ("One Handed Sword", "Thrusting One Handed Sword", "Two Handed Sword"),
    ),
    "one_handed_sword": (
        ("sword",),
        ("sword", "one_hand_weapon", "onehand", "weapon", "default"),
        ("One Handed Sword", "Thrusting One Handed Sword"),
    ),
    "thrusting_one_handed_sword": (
        ("sword",),
        ("sword", "one_hand_weapon", "onehand", "weapon", "default"),
        ("Thrusting One Handed Sword",),
    ),
    "two_handed_sword": (("2h_sword", "sword"), ("2h_sword", "sword", "two_hand_weapon", "weapon", "default"), ("Two Handed Sword",)),
    "axe": (("axe", "2h_axe"), ("axe", "weapon", "default"), ("One Handed Axe", "Two Handed Axe")),
    "one_handed_axe": (("axe",), ("axe", "one_hand_weapon", "onehand", "weapon", "default"), ("One Handed Axe",)),
    "two_handed_axe": (("2h_axe", "axe"), ("2h_axe", "axe", "two_hand_weapon", "weapon", "default"), ("Two Handed Axe",)),
    "mace": (("mace", "2h_mace"), ("mace", "weapon", "default"), ("One Handed Mace", "Two Handed Mace")),
    "one_handed_mace": (("mace",), ("mace", "one_hand_weapon", "onehand", "weapon", "default"), ("One Handed Mace",)),
    "two_handed_mace": (("2h_mace", "mace"), ("2h_mace", "mace", "two_hand_weapon", "weapon", "default"), ("Two Handed Mace",)),
    "weapon": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "weapon_1": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "weapon_2": (
        ("weapon", "one_hand_weapon", "shield", "quiver", "sword", "axe", "mace", "dagger", "claw", "wand", "sceptre"),
        ("weapon", "shield", "quiver", "default"),
        ("One Handed Sword", "One Handed Axe", "One Handed Mace", "Dagger", "Claw", "Wand", "Sceptre", "Thrusting One Handed Sword", "Shield", "Quiver"),
    ),
    # Tolerant EN/ZH aliases (P0-2)
    "offhand": (
        ("weapon", "one_hand_weapon", "shield", "quiver", "sword", "axe", "mace", "dagger", "claw", "wand", "sceptre"),
        ("weapon", "shield", "quiver", "default"),
        ("One Handed Sword", "One Handed Axe", "One Handed Mace", "Dagger", "Claw", "Wand", "Sceptre", "Thrusting One Handed Sword", "Shield", "Quiver"),
    ),
    "mainhand": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "ring1": (("ring",), ("ring", "default"), ("Ring",)),
    "ring2": (("ring",), ("ring", "default"), ("Ring",)),
    "ring3": (("ring",), ("ring", "default"), ("Ring",)),
    "weapon1": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "weapon2": (
        ("weapon", "one_hand_weapon", "shield", "quiver", "sword", "axe", "mace", "dagger", "claw", "wand", "sceptre"),
        ("weapon", "shield", "quiver", "default"),
        ("One Handed Sword", "One Handed Axe", "One Handed Mace", "Dagger", "Claw", "Wand", "Sceptre", "Thrusting One Handed Sword", "Shield", "Quiver"),
    ),
    "衣服": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "胸甲": (("body_armour",), ("body_armour", "armour", "default"), ("Body Armour",)),
    "头盔": (("helmet",), ("helmet", "armour", "default"), ("Helmet",)),
    "手套": (("gloves",), ("gloves", "armour", "default"), ("Gloves",)),
    "鞋子": (("boots",), ("boots", "armour", "default"), ("Boots",)),
    "靴子": (("boots",), ("boots", "armour", "default"), ("Boots",)),
    "盾牌": (("shield",), ("shield", "armour", "default"), ("Shield",)),
    "箭袋": (("quiver",), ("quiver", "default"), ("Quiver",)),
    "项链": (("amulet",), ("amulet", "default"), ("Amulet",)),
    "护身符": (("amulet",), ("amulet", "default"), ("Amulet",)),
    "戒指": (("ring",), ("ring", "default"), ("Ring",)),
    "左戒指": (("ring",), ("ring", "default"), ("Ring",)),
    "右戒指": (("ring",), ("ring", "default"), ("Ring",)),
    "腰带": (("belt",), ("belt", "default"), ("Belt",)),
    "药剂": (("flask",), ("flask", "default"), ("Flask",)),
    "珠宝": (("jewel", "abyss_jewel"), ("jewel", "default"), ()),
    "主手": (
        ("weapon", "one_hand_weapon", "two_hand_weapon", "sword", "axe", "mace", "dagger", "claw", "wand", "bow", "staff", "sceptre"),
        ("weapon", "default"),
        ("One Handed Sword", "Two Handed Sword", "One Handed Axe", "Two Handed Axe", "One Handed Mace", "Two Handed Mace", "Dagger", "Claw", "Wand", "Bow", "Staff", "Sceptre", "Thrusting One Handed Sword"),
    ),
    "副手": (
        ("weapon", "one_hand_weapon", "shield", "quiver", "sword", "axe", "mace", "dagger", "claw", "wand", "sceptre"),
        ("weapon", "shield", "quiver", "default"),
        ("One Handed Sword", "One Handed Axe", "One Handed Mace", "Dagger", "Claw", "Wand", "Sceptre", "Thrusting One Handed Sword", "Shield", "Quiver"),
    ),
}

# UI/non-domain files to skip from general translation index
ZH_SKIP_TRANSLATION_FILES: frozenset[str] = frozenset(
    {
        "meta.json",
        "Test.json",
        "unused.json",
        "GUI.json",
        "Help.json",
        "ui.json",
        "Z-UI.json",
        "Main.json",
        "Build.json",
        "BuildListControl.json",
        "ExtBuildListControl.json",
        "ImportTab.json",
        "NotesTab.json",
        "PartyTab.json",
        "PoBArchivesProvider.json",
        "SharedItemListControl.json",
        "SharedItemSetListControl.json",
        # zh-rTW lookup/config files: not en -> zh name tables.
        "synonyms.json",
        "display_exclude.json",
        "exact_only.json",
        "item_metadata.json",
        "stat_wrappers.json",
        "ui_tooltips.json",
    }
)
# zh-rTW also ships tables keyed by numeric node ids or internal ids (`passives_by_id.json`
# maps "30940" -> 聖宗) and raw GGPK text dumps; indexing them makes Chinese names resolve
# to ids instead of English.
ZH_SKIP_TRANSLATION_FILE_RE = re.compile(r"(?:_by_id\.json|^ggpk_.*\.json)$")
# An `en` key that is all digits or a snake_case stat id (`life_regeneration_rate_+%`).
ZH_NON_NAME_KEY_RE = re.compile(r"^\d+$|^[a-z0-9%+\-]*_[a-z0-9_%+\-]*$")

ZH_FILE_CATEGORY_HINT: dict[str, tuple[str, int]] = {
    "gems.json": ("gem", 1),
    "Gems_data.txt.json": ("gem", 1),
    "Items_Gems.txt.json": ("gem", 2),
    "Z-Skills.json": ("gem", 1),
    "uniques.json": ("unique", 1),
    "Uniques.txt.json": ("unique", 1),
    "Z-S28-Foulborn.json": ("unique", 2),
    "items.json": ("base", 1),
    "Items_Accessories.txt.json": ("base", 1),
    "Items_Armour.txt.json": ("base", 1),
    "Items_Flasks.txt.json": ("base", 1),
    "Items_Jewels.txt.json": ("base", 1),
    "Items_Weapons.txt.json": ("base", 1),
    "Items_Oils.json": ("base", 2),
    "Z-Item.json": ("base", 2),
    "tree_dn.json": ("passive", 1),
    "TattooPassives.json": ("passive", 2),
    "passiveTree.json": ("passive", 3),
    "tree_sd.json": ("stat", 3),
    "tree_rt.json": ("stat", 4),
    "Query_Mod.json": ("mod", 2),
    "ModMap.json": ("mod", 3),
    "stats.json": ("stat", 4),
    "statDescriptions.json": ("stat", 4),
}

NUM_PLACEHOLDER_RE = re.compile(
    r"[+-]?\([+-]?\d+(?:\.\d+)?-[+-]?\d+(?:\.\d+)?\)|[+-]?\d+(?:\.\d+)?|\{\d+\}|#"
)
STAT_VALUE_RE = re.compile(
    r"[+-]?\([+-]?\d+(?:\.\d+)?-[+-]?\d+(?:\.\d+)?\)|[+-]?\d+(?:\.\d+)?"
)
MOD_TAG_PREFIX_RE = re.compile(r"^(?:\{[^}]*\})+")
VARIANT_TAG_RE = re.compile(r"\{variant:([^}]+)\}")
