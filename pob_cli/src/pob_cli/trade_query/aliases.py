"""Slot mappings, weapon base category inference, and bilingual EN/ZH stat aliases."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .apt_supplements import (
    get_stat_polarity as get_stat_polarity,
)
from .apt_supplements import (
    get_stat_polarity_info as get_stat_polarity_info,
)
from .apt_supplements import (
    lookup_apt_zh_stat as lookup_apt_zh_stat,
)
from .zh_aliases import (
    _BRACE_NUM_RE as _BRACE_NUM_RE,
)
from .zh_aliases import (
    _CJK_RE as _CJK_RE,
)
from .zh_aliases import (
    ZH_STAT_ALIASES as ZH_STAT_ALIASES,
)
from .zh_aliases import (
    translate_stat_query as translate_stat_query,
)

# ---------------------------------------------------------------------------
# Slot -> PoE1 trade category & PoB equipment slot mappings
# ---------------------------------------------------------------------------

CATEGORY_BY_SLOT: dict[str, str | None] = {
    # Armour
    "helmet": "armour.helmet",
    "helm": "armour.helmet",
    "body_armour": "armour.chest",
    "body": "armour.chest",
    "chest": "armour.chest",
    "gloves": "armour.gloves",
    "boots": "armour.boots",
    "shield": "armour.shield",
    "quiver": "armour.quiver",
    # Accessory
    "amulet": "accessory.amulet",
    "belt": "accessory.belt",
    "ring": "accessory.ring",
    "ring1": "accessory.ring",
    "ring2": "accessory.ring",
    "ring3": "accessory.ring",
    # Weapon generic
    "weapon": "weapon",
    "weapon1": None,
    "weapon2": None,
    # Weapon subtypes
    "bow": "weapon.bow",
    "wand": "weapon.wand",
    "claw": "weapon.claw",
    "dagger": "weapon.dagger",
    "sceptre": "weapon.sceptre",
    "staff": "weapon.staff",
    "sword": "weapon.onesword",
    "one_handed_sword": "weapon.onesword",
    "two_handed_sword": "weapon.twosword",
    "axe": "weapon.oneaxe",
    "one_handed_axe": "weapon.oneaxe",
    "two_handed_axe": "weapon.twoaxe",
    "mace": "weapon.onemace",
    "one_handed_mace": "weapon.onemace",
    "two_handed_mace": "weapon.twomace",
    "one_hand": "weapon.one",
    "two_hand": "weapon.two",
    # Flask / Jewel
    "flask": "flask",
    "flask1": "flask",
    "flask2": "flask",
    "flask3": "flask",
    "flask4": "flask",
    "flask5": "flask",
    "jewel": "jewel",
    "abyss_jewel": "jewel.abyss",
    "cluster_jewel": "jewel.cluster",
    # 3.30 Viscera (Wickerman) & 3.27 Graft — GGG Trade has no item category filter
    "viscera": None,
    "viscera_head": None,
    "viscera_body": None,
    "viscera_arm": None,
    "viscera_leg": None,
    "wickerman": None,
    "graft": None,
    "graft1": None,
    "graft2": None,
}

# PoB slot names used by `pob items` / `pob item try`.
POB_SLOT_BY_SLOT: dict[str, str] = {
    "helmet": "Helmet",
    "helm": "Helmet",
    "body_armour": "Body Armour",
    "body": "Body Armour",
    "chest": "Body Armour",
    "gloves": "Gloves",
    "boots": "Boots",
    "shield": "Weapon 2",
    "quiver": "Weapon 2",
    "amulet": "Amulet",
    "belt": "Belt",
    "ring": "Ring 1",
    "ring1": "Ring 1",
    "ring2": "Ring 2",
    "ring3": "Ring 3",
    "weapon": "Weapon 1",
    "weapon1": "Weapon 1",
    "weapon2": "Weapon 2",
    "bow": "Weapon 1",
    "wand": "Weapon 1",
    "claw": "Weapon 1",
    "dagger": "Weapon 1",
    "sceptre": "Weapon 1",
    "staff": "Weapon 1",
    "sword": "Weapon 1",
    "one_handed_sword": "Weapon 1",
    "two_handed_sword": "Weapon 1",
    "axe": "Weapon 1",
    "one_handed_axe": "Weapon 1",
    "two_handed_axe": "Weapon 1",
    "mace": "Weapon 1",
    "one_handed_mace": "Weapon 1",
    "two_handed_mace": "Weapon 1",
    "one_hand": "Weapon 1",
    "two_hand": "Weapon 1",
    "flask": "Flask 1",
    "flask1": "Flask 1",
    "flask2": "Flask 2",
    "flask3": "Flask 3",
    "flask4": "Flask 4",
    "flask5": "Flask 5",
    "jewel": "Jewel 1",
    "abyss_jewel": "Jewel 1",
    "cluster_jewel": "Jewel 1",
    "viscera_head": "Head",
    "viscera_body": "Body",
    "viscera_arm": "Arm",
    "viscera_leg": "Leg",
    "graft": "Graft 1",
    "graft1": "Graft 1",
    "graft2": "Graft 2",
}

_SLOT_COMPACT_ALIASES: dict[str, str] = {
    "ring_1": "ring1",
    "ring_2": "ring2",
    "ring_3": "ring3",
    "left_ring": "ring1",
    "leftring": "ring1",
    "right_ring": "ring2",
    "rightring": "ring2",
    "左戒指": "ring1",
    "左戒": "ring1",
    "戒指1": "ring1",
    "右戒指": "ring2",
    "右戒": "ring2",
    "戒指2": "ring2",
    "戒指3": "ring3",
    "戒指": "ring",
    "衣服": "body_armour",
    "胸甲": "body_armour",
    "身甲": "body_armour",
    "body_armor": "body_armour",
    "bodyarmour": "body_armour",
    "头盔": "helmet",
    "头部": "helmet",
    "head": "helmet",
    "手套": "gloves",
    "glove": "gloves",
    "hands": "gloves",
    "鞋子": "boots",
    "靴子": "boots",
    "boot": "boots",
    "feet": "boots",
    "shoes": "boots",
    "项链": "amulet",
    "护身符": "amulet",
    "neck": "amulet",
    "腰带": "belt",
    "waist": "belt",
    "盾牌": "shield",
    "箭袋": "quiver",
    "weapon_1": "weapon1",
    "weapon_2": "weapon2",
    "mainhand": "weapon1",
    "main_hand": "weapon1",
    "主手": "weapon1",
    "主手武器": "weapon1",
    "武器1": "weapon1",
    "offhand": "weapon2",
    "off_hand": "weapon2",
    "副手": "weapon2",
    "副手武器": "weapon2",
    "武器2": "weapon2",
    "flask_1": "flask1",
    "flask_2": "flask2",
    "flask_3": "flask3",
    "flask_4": "flask4",
    "flask_5": "flask5",
    "药剂": "flask",
    "药剂1": "flask1",
    "药剂2": "flask2",
    "药剂3": "flask3",
    "药剂4": "flask4",
    "药剂5": "flask5",
    "药水1": "flask1",
    "药水2": "flask2",
    "药水3": "flask3",
    "药水4": "flask4",
    "药水5": "flask5",
    "珠宝": "jewel",
    "graft_1": "graft1",
    "graft_2": "graft2",
    "graft_head": "viscera_head",
    "graft_body": "viscera_body",
    "graft_arm": "viscera_arm",
    "graft_leg": "viscera_leg",
    "grafthead": "viscera_head",
    "graftbody": "viscera_body",
    "graftarm": "viscera_arm",
    "graftleg": "viscera_leg",
    "viscerahead": "viscera_head",
    "viscerabody": "viscera_body",
    "visceraarm": "viscera_arm",
    "visceraleg": "viscera_leg",
    "脏器": "viscera",
    "假肢": "graft",
    "肢体": "viscera",
    "肢体头部": "viscera_head",
    "肢体躯干": "viscera_body",
    "肢体手臂": "viscera_arm",
    "肢体腿部": "viscera_leg",
}

_UNCATEGORIZED_TRADE_SLOTS: frozenset[str] = frozenset(
    {
        "viscera",
        "viscera_head",
        "viscera_body",
        "viscera_arm",
        "viscera_leg",
        "wickerman",
        "graft",
        "graft1",
        "graft2",
    }
)

_WEAPON_SLOTS: frozenset[str] = frozenset(
    {
        "weapon",
        "weapon1",
        "weapon2",
        "bow",
        "wand",
        "claw",
        "dagger",
        "sceptre",
        "staff",
        "sword",
        "one_handed_sword",
        "two_handed_sword",
        "axe",
        "one_handed_axe",
        "two_handed_axe",
        "mace",
        "one_handed_mace",
        "two_handed_mace",
        "one_hand",
        "two_hand",
    }
)

_ARMOUR_SLOTS: frozenset[str] = frozenset(
    {
        "helmet",
        "helm",
        "body_armour",
        "body",
        "chest",
        "gloves",
        "boots",
        "shield",
    }
)

_SHIELD_SLOTS: frozenset[str] = frozenset({"shield"})

_BASE_ITEM_TYPE_TO_CATEGORY: dict[str, str] = {
    "Bow": "weapon.bow",
    "Wand": "weapon.wand",
    "Claw": "weapon.claw",
    "Dagger": "weapon.dagger",
    "Sceptre": "weapon.sceptre",
    "Staff": "weapon.staff",
    "One Handed Sword": "weapon.onesword",
    "Two Handed Sword": "weapon.twosword",
    "One Handed Axe": "weapon.oneaxe",
    "Two Handed Axe": "weapon.twoaxe",
    "One Handed Mace": "weapon.onemace",
    "Two Handed Mace": "weapon.twomace",
    "Fishing Rod": "weapon.rod",
    "Shield": "armour.shield",
    "Quiver": "armour.quiver",
}

_WEAPON_BASE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("armour.quiver", ("quiver",)),
    ("armour.shield", ("shield", "buckler")),
    ("weapon.bow", ("bow",)),
    ("weapon.sceptre", ("sceptre", "sekhem", "fetish")),
    ("weapon.wand", ("wand", "demon's horn", "imbued wand", "profane wand", "convoking wand")),
    (
        "weapon.claw",
        ("claw", "ripper", "gouger", "awl", "blinder", "stabber", "lasher", "hellion's paw", "eyegouger"),
    ),
    (
        "weapon.dagger",
        (
            "dagger",
            "kris",
            "skean",
            "sai",
            "stiletto",
            "ambusher",
            "poignard",
            "trisula",
            "knife",
            "shank",
            "boot blade",
        ),
    ),
    ("weapon.staff", ("staff", "quarterstaff", "lathi", "maelström", "maelstrom", "gnarled branch")),
    (
        "weapon.twosword",
        (
            "greatsword",
            "two-handed sword",
            "two handed sword",
            "bastard sword",
            "longsword",
            "exquisite blade",
            "highland blade",
            "tiger sword",
            "wraith sword",
            "lion sword",
            "cursed blade",
            "reaver sword",
            "banishing blade",
            "vaal greatsword",
        ),
    ),
    ("weapon.onesword", ("sword", "foil", "rapier", "sabre", "cutlass", "gladius", "estoc", "blade")),
    (
        "weapon.twoaxe",
        (
            "greataxe",
            "two-handed axe",
            "two handed axe",
            "chopper",
            "splitter",
            "labrys",
            "poleaxe",
            "halberd",
            "fleshripper",
            "despot axe",
            "vaal axe",
            "ezomyte axe",
            "sundering axe",
            "headsman axe",
            "talon axe",
            "shadow axe",
            "timber axe",
            "noble axe",
            "abyssal axe",
            "dagger axe",
        ),
    ),
    ("weapon.oneaxe", ("axe", "hatchet", "cleaver", "tomahawk")),
    (
        "weapon.twomace",
        (
            "maul",
            "two-handed mace",
            "two handed mace",
            "great mallet",
            "sledgehammer",
            "piledriver",
            "meatgrinder",
            "steelhead",
            "mallet",
        ),
    ),
    ("weapon.onemace", ("mace", "club", "hammer", "gavel", "breaker", "bludgeon", "morning star", "tenderizer")),
)

# ---------------------------------------------------------------------------
# Pseudo stat IDs, valid trade statuses, stat priority
# ---------------------------------------------------------------------------

OPEN_PREFIX_ID = "pseudo.pseudo_number_of_empty_prefix_mods"
OPEN_SUFFIX_ID = "pseudo.pseudo_number_of_empty_suffix_mods"
OPEN_AFFIX_ID = "pseudo.pseudo_number_of_empty_affix_mods"

VALID_STATUSES: tuple[str, ...] = ("available", "securable", "online", "onlineleague", "any")

# `trade_filters.indexed` options of /api/trade/data/filters, as (hours covered, option id).
INDEXED_WINDOWS: tuple[tuple[int, str], ...] = (
    (1, "1hour"),
    (3, "3hours"),
    (12, "12hours"),
    (24, "1day"),
    (72, "3days"),
    (168, "1week"),
    (336, "2weeks"),
    (720, "1month"),
    (1440, "2months"),
)

STAT_TYPE_ORDER: dict[str, int] = {
    "explicit": 0,
    "crafted": 1,
    "fractured": 1,
    "enchant": 1,
    "implicit": 2,
    "pseudo": 3,
}

# ---------------------------------------------------------------------------
# Slot normalization & weapon base inference
# ---------------------------------------------------------------------------

_DYN_JEWEL_RE = re.compile(r"^(?:jewel[\s_\-]*(\d+)(?:\s*\(#\d+\))?|#(\d+))$", re.IGNORECASE)
_ABYSS_SOCKET_RE = re.compile(r"^(?:(.+?)[\s_\-]+)?abyssal[\s_\-]+socket[\s_\-]+(\d+)(?:\s*\(#\d+\))?$", re.IGNORECASE)

# The item slots PoB gives abyssal socket slots to (`ItemsTab.lua`), six each; weapons also on the swap set.
_ABYSSAL_SOCKET_PARENTS = frozenset({"Weapon 1", "Weapon 2", "Helmet", "Gloves", "Body Armour", "Boots", "Belt"})


def _normalize_base_slot(slot: str) -> str:
    from ..game_data import fold_han  # the Chinese slot aliases are Simplified (头盔, not 頭盔)

    cleaned = re.sub(r"[\s\-]+", "_", fold_han(slot.strip().lower())).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    return _SLOT_COMPACT_ALIASES.get(cleaned, cleaned)


def _parse_dynamic_jewel_slot(slot: str | None) -> tuple[str, str] | None:
    raw = (slot or "").strip()
    if m := _DYN_JEWEL_RE.match(raw):
        return ("jewel", f"Jewel {m.group(1)}" if m.group(1) else f"#{m.group(2)}")
    if (m := _ABYSS_SOCKET_RE.match(raw)) and m.group(1) and 1 <= int(m.group(2)) <= 6:
        # PoB names these slots after the item that holds the socket; there is no bare one.
        parent_raw = m.group(1)
        swap = re.search(r"[\s_\-]+swap$", parent_raw, re.IGNORECASE)
        parent = POB_SLOT_BY_SLOT.get(_normalize_base_slot(parent_raw[: swap.start()] if swap else parent_raw))
        if parent in _ABYSSAL_SOCKET_PARENTS and (not swap or parent.startswith("Weapon")):
            return ("abyss_jewel", f"{parent}{' Swap' if swap else ''} Abyssal Socket {int(m.group(2))}")
    return None


def _normalize_slot(slot: str | None) -> str:
    if not slot:
        return ""
    return dyn[0] if (dyn := _parse_dynamic_jewel_slot(slot)) else _normalize_base_slot(slot)


def category_for_slot(slot: str) -> str | None:
    """Map a PoE1 slot or weapon subtype name to its trade API ``type_filters.category`` option."""
    return CATEGORY_BY_SLOT.get(_normalize_slot(slot))


def is_uncategorized_trade_slot(slot: str | None) -> bool:
    """Return True for valid 3.30 Viscera / Graft slots that have no GGG Trade category filter."""
    return _normalize_slot(slot) in _UNCATEGORIZED_TRADE_SLOTS


def pob_slot_for_slot(slot: str) -> str | None:
    """Map a slot or weapon subtype name to its PoB equipment slot name."""
    if dyn := _parse_dynamic_jewel_slot(slot):
        return dyn[1]
    return POB_SLOT_BY_SLOT.get(_normalize_base_slot(slot)) if slot else None


def infer_weapon_category_from_base(base_type: str, pob_root: Path | None = None) -> str | None:
    """Infer trade category (e.g. ``weapon.bow``, ``weapon.wand``) from an item base type.

    Uses the local PoB SQLite ``bases`` table when available, falling back to
    offline keyword matching so CI/no-engine environments still work cleanly.
    """
    import pob_cli.trade_query as _tq

    cleaned = (base_type or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()

    # Fast path: query local SQLite bases table if engine data exists.
    try:
        resolved_root = Path(pob_root) if pob_root is not None else _tq.resolve_engine_root()
        if (resolved_root / "PoE1" / "Data").is_dir():
            from .. import game_data

            db_path = game_data.ensure_gamedata_index(pob_dir=resolved_root)
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                folded = game_data._fold_search_text(cleaned)  # the index's own fold
                row = conn.execute(
                    "SELECT item_type, sub_type FROM bases WHERE name_lower = ? OR zh_lower = ? LIMIT 1",
                    (folded, folded),
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                mapped = _BASE_ITEM_TYPE_TO_CATEGORY.get(str(row[0]))
                if mapped:
                    return mapped
    except Exception:
        pass

    # Lightweight keyword fallback.
    for category, keywords in _WEAPON_BASE_KEYWORDS:
        for kw in keywords:
            if kw in lowered:
                return category
    return None
