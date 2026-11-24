"""Slot mappings, weapon base category inference, and bilingual EN/ZH stat aliases."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

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
}

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
# Pseudo stat IDs, valid trade statuses, stat priority, Chinese aliases
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

ZH_STAT_ALIASES: dict[str, str] = {
    # Life & Mana
    "生命": "+# to maximum Life",
    "最大生命": "+# to maximum Life",
    "血量": "+# to maximum Life",
    "最大生命百分比": "#% increased maximum Life",
    "生命百分比": "#% increased maximum Life",
    "生命回复": "# Life Regenerated per Second",
    "每秒生命回复": "# Life Regenerated per Second",
    "魔力": "+# to maximum Mana",
    "最大魔力": "+# to maximum Mana",
    "魔力回复": "#% increased Mana Regeneration Rate",
    "魔力回复速度": "#% increased Mana Regeneration Rate",
    # Resistances
    "火抗": "+#% to Fire Resistance",
    "火焰抗性": "+#% to Fire Resistance",
    "冰抗": "+#% to Cold Resistance",
    "冰霜抗性": "+#% to Cold Resistance",
    "电抗": "+#% to Lightning Resistance",
    "闪电抗性": "+#% to Lightning Resistance",
    "混沌抗性": "+#% to Chaos Resistance",
    "混抗": "+#% to Chaos Resistance",
    "全抗": "+#% to all Elemental Resistances",
    "全元素抗性": "+#% to all Elemental Resistances",
    "所有元素抗性": "+#% to all Elemental Resistances",
    "元素抗性": "+#% total Elemental Resistance",
    "总元素抗性": "+#% total Elemental Resistance",
    "总抗性": "+#% total Resistance",
    "总抗": "+#% total Resistance",
    # Speed
    "攻速": "#% increased Attack Speed",
    "攻击速度": "#% increased Attack Speed",
    "施法速度": "#% increased Cast Speed",
    "施速": "#% increased Cast Speed",
    "移速": "#% increased Movement Speed",
    "移动速度": "#% increased Movement Speed",
    # Defences & Suppression & Block
    "法压": "+#% chance to Suppress Spell Damage",
    "法术压制": "+#% chance to Suppress Spell Damage",
    "法术压制率": "+#% chance to Suppress Spell Damage",
    "压制法术伤害几率": "+#% chance to Suppress Spell Damage",
    "能量护盾": "+# to maximum Energy Shield",
    "护盾": "+# to maximum Energy Shield",
    "最大能量护盾": "+# to maximum Energy Shield",
    "能量护盾百分比": "#% increased Energy Shield",
    "护盾百分比": "#% increased Energy Shield",
    "护甲": "+# to Armour",
    "护甲百分比": "#% increased Armour",
    "闪避": "+# to Evasion Rating",
    "闪避值": "+# to Evasion Rating",
    "闪避百分比": "#% increased Evasion Rating",
    "结界": "+# to Ward",
    "格挡": "+#% Chance to Block",
    "格挡率": "+#% Chance to Block",
    "格挡几率": "+#% Chance to Block",
    "攻击格挡": "#% Chance to Block Attack Damage",
    "攻击格挡率": "#% Chance to Block Attack Damage",
    "法术格挡": "#% Chance to Block Spell Damage",
    "法术格挡率": "#% Chance to Block Spell Damage",
    "法格": "#% Chance to Block Spell Damage",
    # Crit & Accuracy
    "暴击率": "#% increased Critical Strike Chance",
    "暴击几率": "#% increased Critical Strike Chance",
    "全局暴击率": "#% increased Global Critical Strike Chance",
    "暴击伤害": "+#% to Global Critical Strike Multiplier",
    "暴伤": "+#% to Global Critical Strike Multiplier",
    "全局暴击伤害加成": "+#% to Global Critical Strike Multiplier",
    "命中": "+# to Accuracy Rating",
    "命中值": "+# to Accuracy Rating",
    # Attributes
    "力量": "+# to Strength",
    "敏捷": "+# to Dexterity",
    "智慧": "+# to Intelligence",
    "智力": "+# to Intelligence",
    "全属性": "+# to all Attributes",
    "所有属性": "+# to all Attributes",
    # Damage & Flat Added Damage
    "物理伤害": "#% increased Physical Damage",
    "法术伤害": "#% increased Spell Damage",
    "法伤": "#% increased Spell Damage",
    "元素伤害": "#% increased Elemental Damage",
    "火焰伤害": "#% increased Fire Damage",
    "火伤": "#% increased Fire Damage",
    "冰霜伤害": "#% increased Cold Damage",
    "冰伤": "#% increased Cold Damage",
    "闪电伤害": "#% increased Lightning Damage",
    "电伤": "#% increased Lightning Damage",
    "混沌伤害": "#% increased Chaos Damage",
    "混伤": "#% increased Chaos Damage",
    "火焰点伤": "Adds # to # Fire Damage",
    "附加火焰伤害": "Adds # to # Fire Damage",
    "冰霜点伤": "Adds # to # Cold Damage",
    "附加冰霜伤害": "Adds # to # Cold Damage",
    "闪电点伤": "Adds # to # Lightning Damage",
    "附加闪电伤害": "Adds # to # Lightning Damage",
    "物理点伤": "Adds # to # Physical Damage",
    "附加物理伤害": "Adds # to # Physical Damage",
    "混沌点伤": "Adds # to # Chaos Damage",
    "附加混沌伤害": "Adds # to # Chaos Damage",
    "攻击火焰点伤": "Adds # to # Fire Damage to Attacks",
    "攻击附加火焰伤害": "Adds # to # Fire Damage to Attacks",
    "攻击冰霜点伤": "Adds # to # Cold Damage to Attacks",
    "攻击附加冰霜伤害": "Adds # to # Cold Damage to Attacks",
    "攻击闪电点伤": "Adds # to # Lightning Damage to Attacks",
    "攻击附加闪电伤害": "Adds # to # Lightning Damage to Attacks",
    "攻击物理点伤": "Adds # to # Physical Damage to Attacks",
    "攻击附加物理伤害": "Adds # to # Physical Damage to Attacks",
    "法术火焰点伤": "Adds # to # Fire Damage to Spells",
    "法术附加火焰伤害": "Adds # to # Fire Damage to Spells",
    "法术冰霜点伤": "Adds # to # Cold Damage to Spells",
    "法术附加冰霜伤害": "Adds # to # Cold Damage to Spells",
    "法术闪电点伤": "Adds # to # Lightning Damage to Spells",
    "法术附加闪电伤害": "Adds # to # Lightning Damage to Spells",
    "法术物理点伤": "Adds # to # Physical Damage to Spells",
    "法术附加物理伤害": "Adds # to # Physical Damage to Spells",
    "物品稀有度": "#% increased Rarity of Items found",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_BRACE_NUM_RE = re.compile(r"\{\d+\}")


# ---------------------------------------------------------------------------
# Slot normalization, weapon base inference & bilingual translation
# ---------------------------------------------------------------------------


def _normalize_slot(slot: str | None) -> str:
    if not slot:
        return ""
    from ..game_data import fold_han  # the Chinese slot aliases are Simplified (头盔, not 頭盔)

    cleaned = re.sub(r"[\s\-]+", "_", fold_han(slot.strip().lower())).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    return _SLOT_COMPACT_ALIASES.get(cleaned, cleaned)


def category_for_slot(slot: str) -> str | None:
    """Map a PoE1 slot or weapon subtype name to its trade API ``type_filters.category`` option."""
    return CATEGORY_BY_SLOT.get(_normalize_slot(slot))


def pob_slot_for_slot(slot: str) -> str | None:
    """Map a slot or weapon subtype name to its PoB equipment slot name."""
    return POB_SLOT_BY_SLOT.get(_normalize_slot(slot))


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


def translate_stat_query(text: str) -> str:
    """Translate a Chinese PoE1 stat query into English stat text.

    Non-CJK text is returned unchanged. Uses the curated ``ZH_STAT_ALIASES`` table
    first, then falls back to local SQLite ``game_data`` translations if available.
    """
    import pob_cli.trade_query as _tq

    original = (text or "").strip()
    if not original or not _CJK_RE.search(original):
        return original
    from ..game_data import fold_han

    # The alias table is Simplified, while pob-zh shows Traditional names (能量護盾). Unfolded, such
    # a query skipped the table and the fuzzy fallback picked another stat ("Has no Energy Shield").
    raw = fold_han(original)

    # Direct exact lookup
    direct = ZH_STAT_ALIASES.get(raw)
    if direct:
        return direct

    # Strip common numeric/symbol prefixes/suffixes (e.g., "+#% 火焰抗性", "最大生命 +")
    cleaned = re.sub(r"[+#%\d\s()（）\-–—]+", "", raw)
    if cleaned:
        direct_clean = ZH_STAT_ALIASES.get(cleaned)
        if direct_clean:
            return direct_clean
        # Strip leading verb hints like "提高"/"增加" when exact alias follows
        for prefix in ("提高", "增加"):
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
                sub = cleaned[len(prefix) :]
                if sub in ZH_STAT_ALIASES:
                    return ZH_STAT_ALIASES[sub]

    # Fallback: query local PoB SQLite translations table when available
    try:
        resolved_root = _tq.resolve_engine_root()
        if (resolved_root / "PoE1" / "Data").is_dir():
            from .. import game_data

            query_term = cleaned or raw
            res = game_data.query_local_data("translate", query_term, limit=12)
            rows = [
                r
                for r in (res.get("results") or [])
                if str(r.get("match_type") or "") != "partial"
            ]
            if rows:
                cat_rank = {"mod": 0, "stat": 1, "passive": 2, "base": 3, "gem": 4, "unique": 5}
                match_rank = {"exact": 0, "prefix": 1, "substring": 2}
                best_row = min(
                    rows,
                    key=lambda r: (
                        cat_rank.get(str(r.get("category") or ""), 9),
                        match_rank.get(str(r.get("match_type") or ""), 9),
                        len(str(r.get("en") or "")),
                    ),
                )
                en_text = str(best_row.get("en") or "").strip()
                if en_text:
                    return _BRACE_NUM_RE.sub("#", en_text)
    except Exception:
        pass

    return original
