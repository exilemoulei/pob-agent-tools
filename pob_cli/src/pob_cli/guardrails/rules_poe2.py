"""Table D PoE2-only reverse trap rules, timeless jewels, and PoE2 classes/ascendancies."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .build_gate import POE2_ONLY_CLASSES
from .warnings import GuardrailScope, S, normalize_zh_compact


@dataclass(frozen=True)
class TrapRule:
    rule_id: str
    scopes: frozenset[GuardrailScope]
    en_exact: tuple[str, ...] = ()
    zh_exact: tuple[str, ...] = ()
    en_words: tuple[str, ...] = ()
    zh_phrases: tuple[str, ...] = ()
    regex: re.Pattern[str] | None = None
    exclude_en_re: re.Pattern[str] | None = None
    exclude_zh_compounds: tuple[str, ...] = ()
    error: str = ""
    remediation: str = ""
    equivalent_command: str | None = None


POE2_ONLY_REVERSE_TRAPS_FOR_POB1: tuple[TrapRule, ...] = (
    TrapRule(
        rule_id="POE2_UNCUT_GEMS",
        scopes=frozenset({S.GEM_NAME, S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY}),
        en_words=("uncut skill gem", "uncut spirit gem", "uncut support gem"),
        zh_phrases=("未切割的技能宝石", "未切割的精魂宝石", "未切割的辅助宝石", "原胚技能石", "精魂宝石"),
        error="Uncut Skill/Spirit/Support Gems (`未切割的宝石`/`精魂宝石`) belong to Path of Exile 2.",
        remediation="Use `pob2` for PoE2 gems, or query specific PoE1 gems via `pob data gem <name>`.",
        equivalent_command='pob2 data gem "<query>"',
    ),
    TrapRule(
        rule_id="POE2_DISTILLED_EMOTIONS",
        scopes=frozenset({S.NOTABLE_OR_OIL, S.DATA_QUERY, S.ITEM_RAW}),
        en_words=(
            "distilled ire",
            "distilled guilt",
            "distilled greed",
            "distilled paranoia",
            "distilled envy",
            "distilled disgust",
            "distilled despair",
            "distilled fear",
            "distilled suffering",
            "distilled isolation",
            "diluted liquid ire",
            "liquid paranoia",
            "concentrated liquid fear",
            "concentrated liquid isolation",
            "instilled modifier",
        ),
        zh_phrases=(
            "涂灌",
            "精炼的愤怒",
            "精炼的罪孽",
            "精炼的贪婪",
            "精炼的偏执",
            "精炼的嫉妒",
            "精炼的厌恶",
            "精炼的绝望",
            "精炼的恐惧",
            "精炼的苦难",
            "精炼的孤立",
        ),
        exclude_en_re=re.compile(r"distilled\s+perfection", re.IGNORECASE),
        error="Distilled / Liquid Emotions (`涂灌` / `精炼情绪`) are PoE2 Delirium instilling currencies.",
        remediation=(
            "PoE1 amulets/rings/blighted maps use **Blight Oils (`圣油`/`涂油`)** via "
            "`pob item anoint --notable <name>`, or use `pob2` for PoE2."
        ),
        equivalent_command='pob item anoint --notable "<name>"',
    ),
    TrapRule(
        rule_id="POE2_WAYSTONE",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY, S.CATEGORY}),
        en_exact=("waystone", "waystones"),
        en_words=("waystone tier", "waystones dropped"),
        zh_phrases=("引路石", "界石"),
        exclude_en_re=re.compile(r"ancient\s+waystones", re.IGNORECASE),
        error="Waystones (`引路石`) are PoE2 Atlas endgame items (PoE1 uses Maps / `地图`).",
        remediation="Use `pob2` for PoE2 Waystones, or query `Map` in `pob`.",
        equivalent_command="pob price Map --category map",
    ),
    TrapRule(
        rule_id="POE2_SOUL_CORES_AND_RUNE_MODS",
        scopes=frozenset({S.DATA_QUERY, S.ITEM_RAW, S.STAT_QUERY}),
        en_words=("soul core", "soul cores", "socketed soul cores"),
        zh_phrases=("灵魂核心", "镶嵌的灵核"),
        regex=re.compile(r"(?m)^(?:\{enchant\})?\{rune\}"),
        error=(
            "Azmeri/Vaal Soul Cores (`灵魂核心`/`灵核`) and `{rune}` item socket lines "
            "belong to PoE2 (`ModRunes.lua`)."
        ),
        remediation=(
            "In PoE1, use Hideout Crafting Bench (`{crafted}`) via `pob bench suggest`, "
            "or use `pob2 rune` for PoE2."
        ),
        equivalent_command="pob2 rune list --slot helmet",
    ),
    TrapRule(
        rule_id="POE2_WEAPONS_AND_FOCUS_OFFHAND",
        scopes=frozenset({S.SLOT, S.CATEGORY, S.ITEM_RAW, S.DATA_QUERY}),
        en_exact=(
            "crossbow",
            "flail",
            "spear",
            "buckler",
            "weapon.quarterstaff",
            "weapon.crossbow",
            "weapon.flail",
            "weapon.spear",
            "armour.focus",
            "armour.buckler",
        ),
        en_words=(
            "quarterstaves",
            "wrapped quarterstaff",
            "long quarterstaff",
            "gothic quarterstaff",
            "crackling quarterstaff",
            "crescent quarterstaff",
            "steelpoint quarterstaff",
            "slicing quarterstaff",
            "barrier quarterstaff",
            "crossbow",
            "crossbows",
            "twig focus",
            "woven focus",
            "antler focus",
            "crystal focus",
            "tonal focus",
            "engraved focus",
        ),
        zh_phrases=("节杖", "十字弓", "战弩", "副手法器"),
        zh_exact=("法器",),
        error=(
            "Quarterstaff (`节杖`), Crossbow (`十字弓`/`战弩`), Spear (`长矛`), Flail (`连枷`), "
            "and off-hand Focus (`法器`, `armour.focus`) are PoE2 equipment classes."
        ),
        remediation=(
            "Use `pob2` for PoE2 equipment, or PoE1 weapon/shield/wand/spirit-shield "
            "slots in `pob`."
        ),
        equivalent_command="pob2 trade search --slot focus",
    ),
    TrapRule(
        rule_id="POE2_FOCUS_AND_QUARTERSTAFF_SLOT_ONLY",
        scopes=frozenset({S.SLOT, S.CATEGORY}),
        en_exact=("focus", "armour.focus", "quarterstaff", "weapon.quarterstaff"),
        zh_exact=("法器", "细柄长杖", "节杖"),
        error=(
            "`Focus` (`法器`) and `Quarterstaff` (`节杖`) as equipment slots/categories are PoE2-only "
            "(PoE1 uses `shield`/`quiver` and `staff`/`warstaff`)."
        ),
        remediation="Use `pob2` for PoE2 Focus/Quarterstaff slots, or `--slot shield` / `--slot staff` in `pob`.",
        equivalent_command="pob2 trade search --slot quarterstaff",
    ),
    TrapRule(
        rule_id="POE2_CHARM_BELT_SLOTS",
        scopes=frozenset({S.SLOT, S.CATEGORY, S.ITEM_RAW}),
        en_exact=(
            "charm 1",
            "charm 2",
            "charm 3",
            "charm1",
            "charm2",
            "charm3",
            "charm_1",
            "charm_2",
            "charm_3",
            "charm-1",
            "charm-2",
            "charm-3",
            "flask.charm",
        ),
        en_words=(
            "thawing charm",
            "staunching charm",
            "antidote charm",
            "dousing charm",
            "grounding charm",
            "has 1 charm slot",
            "has 2 charm slots",
            "has 3 charm slots",
        ),
        zh_exact=("护符 1", "护符 2", "护符 3", "咒符 1", "咒符 2", "咒符 3"),
        zh_phrases=("融冰咒符", "止血咒符", "抗毒咒符"),
        error="`Charm 1..3` belt slots (`护符 1..3`) are PoE2 mechanics; PoE1 uses `Flask 1..5`.",
        remediation="Use `pob item flask 1..5` for PoE1 flasks, or `pob2` for PoE2 Charms.",
        equivalent_command='pob2 items --slot "Charm 1"',
    ),
    TrapRule(
        rule_id="POE2_SPIRIT_RESERVATION",
        scopes=frozenset({S.CALC_QUERY, S.STAT_QUERY, S.CONFIG_VAR}),
        en_exact=("spirit", "totalspirit", "spiritunreserved"),
        zh_exact=("精魂", "最大精魂", "未保留精魂"),
        en_words=("maximum spirit", "spirit reservation", "increased spirit", "to spirit"),
        zh_phrases=("最大精魂", "精魂保留", "精魂提高"),
        exclude_en_re=re.compile(
            r"spirit\s+shield|spirit\s+offering|raging\s+spirit|spectral\s+spirit|spirit\s+burst|spirit\s+charge",
            re.IGNORECASE,
        ),
        exclude_zh_compounds=("精魂之盾",),
        error="`Spirit` (`精魂`) reservation pool does not exist in PoE1.",
        remediation=(
            "PoE1 skills/auras reserve **Mana** (or Life) scaled by `Mana Reservation Efficiency` "
            "(`魔力保留效能`). Use `pob2 calc Spirit` for PoE2 builds."
        ),
        equivalent_command='pob calc "Mana Reservation"',
    ),
)

POE2_ONLY_TIMELESS_JEWELS_EN: tuple[str, ...] = (
    "undying hate",
    "controlled metamorphosis",
    "against the darkness",
    "prism of belief",
    "from nothing",
)
# NOTE: `英雄的悲歌` is the PoE2 CN localization of `Heroic Tragedy`; do NOT block
# English `Heroic Tragedy` or PoE1 CN `英勇悲剧` / `英勇的悲剧` (valid PoE1 jewel).
POE2_ONLY_TIMELESS_JEWELS_ZH: tuple[str, ...] = (
    "不死之恨",
    "不朽之恨",
    "受控的蜕变",
    "可控变形",
    "力抗黑暗",
    "抗衡黑暗",
    "信仰棱镜",
    "信念的棱镜",
    "无中生有",
    "无根之源",
    "英雄的悲歌",
)

POE2_ONLY_CLASSES_EN: frozenset[str] = POE2_ONLY_CLASSES
POE2_ONLY_CLASSES_ZH: frozenset[str] = frozenset(
    {
        "战士",
        "武僧",
        "佣兵",
        "魔巫",
        "女术士",
        "女猎手",
        "德鲁伊",
    }
)

POE2_ONLY_ASCENDANCIES_EN: frozenset[str] = frozenset(
    {
        "titan",
        "warbringer",
        "smith of kitava",
        "invoker",
        "acolyte of chayula",
        "witchhunter",
        "gemling legionnaire",
        "tactician",
        "stormweaver",
        "chronomancer",
        "disciple of varashta",
        "blood mage",
        "infernalist",
        "lich",
        "abyssal lich",
        "amazon",
        "ritualist",
        "oracle",
        "shaman",
    }
)
POE2_ONLY_ASCENDANCIES_ZH: frozenset[str] = frozenset(
    {
        "泰坦",
        "战争使者",
        "奇塔弗铁匠",
        "奇塔弗的铁匠",
        "祈求者",
        "夏乌拉侍僧",
        "夏乌拉的侍僧",
        "猎巫人",
        "宝石军团",
        "古灵使徒",
        "战术家",
        "风暴编织者",
        "时空术士",
        "瓦拉什塔信徒",
        "瓦拉什塔的信徒",
        "血法师",
        "鲜血法师",
        "狱火师",
        "巫妖",
        "深渊巫妖",
        "亚马逊",
        "仪式师",
        "先知",
        "萨满",
    }
)
_POE2_CLASS_ASC_EN = POE2_ONLY_CLASSES_EN | POE2_ONLY_ASCENDANCIES_EN
_POE2_CLASS_ASC_ZH = frozenset(
    normalize_zh_compact(x) for x in (POE2_ONLY_CLASSES_ZH | POE2_ONLY_ASCENDANCIES_ZH)
)
