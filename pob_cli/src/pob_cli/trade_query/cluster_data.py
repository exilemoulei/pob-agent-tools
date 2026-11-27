"""Cluster Jewel Lua data loader (`ClusterJewels.lua` + `ModJewelCluster.lua`) and offline fallback."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

_CLUSTER_CACHE: dict[tuple[str, int, int, int, int], ClusterData] = {}
_CLUSTER_LOCK = threading.Lock()

# Matches TradeSiteStats.lua enchant.stat_3948993189|<option_id> for all 17 Large Cluster enchants.
LARGE_ENCHANT_OPTION_IDS: dict[str, int] = {
    "affliction_axe_and_sword_damage": 1,
    "affliction_mace_and_staff_damage": 2,
    "affliction_dagger_and_claw_damage": 3,
    "affliction_bow_damage": 4,
    "affliction_wand_damage": 5,
    "affliction_damage_with_two_handed_melee_weapons": 6,
    "affliction_attack_damage_while_dual_wielding_": 7,
    "affliction_attack_damage_while_holding_a_shield": 8,
    "affliction_attack_damage_": 9,
    "affliction_spell_damage": 10,
    "affliction_elemental_damage": 11,
    "affliction_physical_damage": 12,
    "affliction_fire_damage": 13,
    "affliction_lightning_damage": 14,
    "affliction_cold_damage": 15,
    "affliction_chaos_damage": 16,
    "affliction_minion_damage": 17,
}

# Curated Simplified Chinese aliases (folded) for Large Cluster enchants.
ZH_LARGE_ENCHANT_ALIASES: dict[str, str] = {
    "斧和剑伤害": "affliction_axe_and_sword_damage",
    "斧剑伤害": "affliction_axe_and_sword_damage",
    "锤和长杖伤害": "affliction_mace_and_staff_damage",
    "匕首和爪伤害": "affliction_dagger_and_claw_damage",
    "爪匕伤害": "affliction_dagger_and_claw_damage",
    "弓类伤害": "affliction_bow_damage",
    "弓伤害": "affliction_bow_damage",
    "法杖伤害": "affliction_wand_damage",
    "双手武器伤害": "affliction_damage_with_two_handed_melee_weapons",
    "双手近战伤害": "affliction_damage_with_two_handed_melee_weapons",
    "双持攻击伤害": "affliction_attack_damage_while_dual_wielding_",
    "持盾攻击伤害": "affliction_attack_damage_while_holding_a_shield",
    "持盾伤害": "affliction_attack_damage_while_holding_a_shield",
    "攻击伤害": "affliction_attack_damage_",
    "法术伤害": "affliction_spell_damage",
    "法伤": "affliction_spell_damage",
    "元素伤害": "affliction_elemental_damage",
    "物理伤害": "affliction_physical_damage",
    "火焰伤害": "affliction_fire_damage",
    "火伤": "affliction_fire_damage",
    "闪电伤害": "affliction_lightning_damage",
    "电伤": "affliction_lightning_damage",
    "冰霜伤害": "affliction_cold_damage",
    "冰伤": "affliction_cold_damage",
    "混沌伤害": "affliction_chaos_damage",
    "混伤": "affliction_chaos_damage",
    "召唤生物伤害": "affliction_minion_damage",
    "召唤伤害": "affliction_minion_damage",
}

# Compact offline Chinese notable aliases & community variants (folded Simplified).
ZH_NOTABLE_OFFLINE_FALLBACK: dict[str, str] = {
    "暴风雪": "Snowstorm",
    "风暴痛饮": "Storm Drinker",
    "风暴狂饮": "Storm Drinker",
    "暴风饮者": "Storm Drinker",
    "多里亚尼之训": "Doryani's Lesson",
    "多利亚尼之训": "Doryani's Lesson",
    "多里亚尼的教程": "Doryani's Lesson",
    "多利亚尼的教程": "Doryani's Lesson",
    "毁天灭地": "Widespread Destruction",
    "毁灭扩散": "Widespread Destruction",
    "虹彩之心": "Prismatic Heart",
    "棱光之心": "Prismatic Heart",
    "施虐者": "Sadist",
    "腐蚀元素": "Corrosive Elements",
    "迷茫展现": "Disorienting Display",
    "心神错乱": "Disorienting Display",
    "瘫痪": "Paralysis",
    "麻痹": "Paralysis",
    "雷霆万钧": "Thunderstruck",
    "风暴骑士": "Stormrider",
    "风暴骑手": "Stormrider",
    "超载感电": "Overshock",
    "闪烁灵光": "Scintillating Idea",
    "残酷之触": "Cruel Preparation",
    "老练战吼者": "Veteran Defender",
    "武艺精湛": "Martial Prowess",
    "怒火盛宴": "Feed the Fury",
    "豢养狂怒": "Feed the Fury",
    "恶毒穿刺": "Feed the Fury",
    "燃料之怒": "Fuel the Fight",
    "横扫千军": "Heavy Hitter",
    "打击弱点": "Smite the Weak",
    "恢宏壁垒": "Prodigious Defence",
    "先遣卫士": "Advance Guard",
    "钢铁破壁": "Iron Breaker",
    "久经沙场": "Battle-Hardened",
    "力量倍增": "Force Multiplier",
    "狂暴突袭": "Furious Assault",
    "冷酷誓言": "Grim Oath",
    "焕然一新": "Renewal",
    "屠戮召唤": "Call to the Slaughter",
    "原始羁绊": "Primordial Bond",
    "饕餮群魔": "Feasting Fiends",
    "腐化之爪": "Rotten Claws",
    "凶残撕咬": "Vicious Bite",
    "白骨萎缩": "Skeletal Atrophy",
    "资深法师": "Practiced Caster",
    "魔源涌动": "Essence Rush",
    "法师猎手": "Mage Hunter",
    "蚀魔": "Thaumophage",
    "精神榨取": "Sap Psyche",
}


@dataclass(frozen=True)
class ClusterGeometry:
    size: str
    size_index: int
    min_nodes: int
    max_nodes: int
    small_indices: tuple[int, ...]
    notable_indices: tuple[int, ...]
    socket_indices: tuple[int, ...]
    total_indices: int


LARGE_CLUSTER_GEOMETRY = ClusterGeometry(
    size="Large",
    size_index=2,
    min_nodes=8,
    max_nodes=12,
    small_indices=(0, 4, 6, 8, 10, 2, 7, 5, 9, 3, 11, 1),
    notable_indices=(6, 4, 8, 10, 2),
    socket_indices=(4, 8, 6),
    total_indices=12,
)


@dataclass(frozen=True)
class ClusterEnchant:
    tag: str
    name: str
    stats: tuple[str, ...]
    enchant_lines: tuple[str, ...]
    trade_option_id: int | None = None


@dataclass(frozen=True)
class ClusterNotable:
    name: str
    sort_order: int
    affix_type: str  # "Prefix" | "Suffix"
    level: int
    group: str
    weights: Mapping[str, int]
    blocks_second_suffix: bool = False
    trade_hash: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.weights, MappingProxyType):
            object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))

    @property
    def stat_id_hint(self) -> str | None:
        return f"explicit.stat_{self.trade_hash}" if self.trade_hash is not None else None


@dataclass(frozen=True)
class ClusterData:
    source: str  # "local_engine" | "offline_fallback"
    large_geometry: ClusterGeometry
    large_enchants: Mapping[str, ClusterEnchant]
    notable_sort_order: Mapping[str, int]
    notables_by_name: Mapping[str, ClusterNotable]

    def __post_init__(self) -> None:
        if not isinstance(self.large_enchants, MappingProxyType):
            object.__setattr__(self, "large_enchants", MappingProxyType(dict(self.large_enchants)))
        if not isinstance(self.notable_sort_order, MappingProxyType):
            object.__setattr__(self, "notable_sort_order", MappingProxyType(dict(self.notable_sort_order)))
        if not isinstance(self.notables_by_name, MappingProxyType):
            object.__setattr__(self, "notables_by_name", MappingProxyType(dict(self.notables_by_name)))


_INT_LIST_RE = re.compile(r"(\d+)")
_STR_LIST_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_SORT_ENTRY_RE = re.compile(r'\["((?:[^"\\]|\\.)*)"\]\s*=\s*(\d+)')
_MOD_TYPE_RE = re.compile(r'type\s*=\s*"([^"]+)"')
_MOD_NOTABLE_RE = re.compile(r'"1 Added Passive Skill is ([^"]+)"')
_MOD_LEVEL_RE = re.compile(r"\blevel\s*=\s*(\d+)")
_MOD_GROUP_RE = re.compile(r'group\s*=\s*"([^"]+)"')
_MOD_WKEY_RE = re.compile(r"weightKey\s*=\s*\{([^}]*)\}")
_MOD_WVAL_RE = re.compile(r"weightVal\s*=\s*\{([^}]*)\}")
_MOD_TAGS_RE = re.compile(r"\btags\s*=\s*\{([^}]*)\}")
_MOD_HASH_RE = re.compile(r"tradeHashes\s*=\s*\{\s*\[(\d+)\]")


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _extract_lua_int_tuple(section: str, key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    m = re.search(rf"\b{key}\s*=\s*\{{([^}}]*)\}}", section)
    if not m:
        return default
    vals = tuple(int(x) for x in _INT_LIST_RE.findall(m.group(1)))
    return vals or default


def _parse_cluster_jewels_lua(text: str) -> tuple[ClusterGeometry, dict[str, ClusterEnchant], dict[str, int]]:
    large_geom = LARGE_CLUSTER_GEOMETRY
    large_enchants: dict[str, ClusterEnchant] = {}
    if '["Large Cluster Jewel"] = {' in text:
        large_sec = text.split('["Large Cluster Jewel"] = {', 1)[1]
        if "notableSortOrder = {" in large_sec:
            large_sec = large_sec.split("notableSortOrder = {", 1)[0]
        header = large_sec.split("skills = {", 1)[0] if "skills = {" in large_sec else large_sec
        m_min = re.search(r"\bminNodes\s*=\s*(\d+)", header)
        m_max = re.search(r"\bmaxNodes\s*=\s*(\d+)", header)
        m_tot = re.search(r"\btotalIndicies\s*=\s*(\d+)", header)
        large_geom = ClusterGeometry(
            size="Large",
            size_index=2,
            min_nodes=int(m_min.group(1)) if m_min else 8,
            max_nodes=int(m_max.group(1)) if m_max else 12,
            small_indices=_extract_lua_int_tuple(header, "smallIndicies", LARGE_CLUSTER_GEOMETRY.small_indices),
            notable_indices=_extract_lua_int_tuple(header, "notableIndicies", LARGE_CLUSTER_GEOMETRY.notable_indices),
            socket_indices=_extract_lua_int_tuple(header, "socketIndicies", LARGE_CLUSTER_GEOMETRY.socket_indices),
            total_indices=int(m_tot.group(1)) if m_tot else 12,
        )
        skills_sec = large_sec.split("skills = {", 1)[1] if "skills = {" in large_sec else ""
        for block in re.split(r'\n\t\t\t\t\["[^"]+"\]\s*=\s*\{', skills_sec)[1:]:
            m_name = re.search(r'\bname\s*=\s*"([^"]+)"', block)
            m_tag = re.search(r'\btag\s*=\s*"([^"]+)"', block)
            m_stats = re.search(r"\bstats\s*=\s*\{([^}]*)\}", block)
            m_ench = re.search(r"\benchant\s*=\s*\{([^}]*)\}", block, re.DOTALL)
            if not (m_name and m_tag):
                continue
            tag = _unescape(m_tag.group(1))
            stats = tuple(_unescape(s) for s in _STR_LIST_RE.findall(m_stats.group(1))) if m_stats else ()
            ench_lines = tuple(_unescape(s) for s in _STR_LIST_RE.findall(m_ench.group(1))) if m_ench else ()
            large_enchants[tag] = ClusterEnchant(
                tag=tag,
                name=_unescape(m_name.group(1)),
                stats=stats,
                enchant_lines=ench_lines,
                trade_option_id=LARGE_ENCHANT_OPTION_IDS.get(tag),
            )

    sort_order: dict[str, int] = {}
    if "notableSortOrder = {" in text:
        nso_sec = text.split("notableSortOrder = {", 1)[1].split("\n\t},", 1)[0]
        for m in _SORT_ENTRY_RE.finditer(nso_sec):
            sort_order[_unescape(m.group(1))] = int(m.group(2))
    return large_geom, large_enchants, sort_order


def _parse_mod_jewel_cluster_lua(text: str, sort_order: dict[str, int]) -> dict[str, ClusterNotable]:
    notables: dict[str, ClusterNotable] = {}
    for line in text.splitlines():
        if "1 Added Passive Skill is " not in line:
            continue
        m_name = _MOD_NOTABLE_RE.search(line)
        m_type = _MOD_TYPE_RE.search(line)
        m_wk = _MOD_WKEY_RE.search(line)
        m_wv = _MOD_WVAL_RE.search(line)
        if not (m_name and m_type and m_wk and m_wv):
            continue
        name = _unescape(m_name.group(1))
        affix_type = m_type.group(1)
        if affix_type not in ("Prefix", "Suffix"):
            continue
        keys = [_unescape(k) for k in _STR_LIST_RE.findall(m_wk.group(1))]
        vals = [int(v) for v in _INT_LIST_RE.findall(m_wv.group(1))]
        weights = {k: v for k, v in zip(keys, vals, strict=False) if v > 0}
        m_lvl = _MOD_LEVEL_RE.search(line)
        m_grp = _MOD_GROUP_RE.search(line)
        m_tags = _MOD_TAGS_RE.search(line)
        m_hash = _MOD_HASH_RE.search(line)
        tags_str = m_tags.group(1) if m_tags else ""
        notables[name] = ClusterNotable(
            name=name,
            sort_order=sort_order.get(name, 999999),
            affix_type=affix_type,
            level=int(m_lvl.group(1)) if m_lvl else 1,
            group=_unescape(m_grp.group(1)) if m_grp else name,
            weights=weights,
            blocks_second_suffix="has_affliction_notable2" in tags_str,
            trade_hash=int(m_hash.group(1)) if m_hash else None,
        )
    return notables


def _build_offline_fallback_data() -> ClusterData:
    """Compact verified PoE1 3.30 Large Cluster dataset used only when local PoB engine is absent."""
    enchants = {
        "affliction_lightning_damage": ClusterEnchant(
            tag="affliction_lightning_damage",
            name="Lightning Damage",
            stats=("12% increased Lightning Damage",),
            enchant_lines=("Added Small Passive Skills grant: 12% increased Lightning Damage",),
            trade_option_id=14,
        ),
        "affliction_elemental_damage": ClusterEnchant(
            tag="affliction_elemental_damage",
            name="Elemental Damage",
            stats=("10% increased Elemental Damage",),
            enchant_lines=("Added Small Passive Skills grant: 10% increased Elemental Damage",),
            trade_option_id=11,
        ),
        "affliction_minion_damage": ClusterEnchant(
            tag="affliction_minion_damage",
            name="Minion Damage",
            stats=("Minions deal 10% increased Damage",),
            enchant_lines=("Added Small Passive Skills grant: Minions deal 10% increased Damage",),
            trade_option_id=17,
        ),
        "affliction_spell_damage": ClusterEnchant(
            tag="affliction_spell_damage",
            name="Spell Damage",
            stats=("10% increased Spell Damage",),
            enchant_lines=("Added Small Passive Skills grant: 10% increased Spell Damage",),
            trade_option_id=10,
        ),
    }
    raw_rows: tuple[tuple[str, int, str, int, dict[str, int], int], ...] = (
        ("Sadist", 11386, "Prefix", 68, {"affliction_lightning_damage": 136, "affliction_elemental_damage": 281}, 3638731729),
        ("Corrosive Elements", 11387, "Prefix", 75, {"affliction_lightning_damage": 45, "affliction_elemental_damage": 94}, 1777139212),
        ("Doryani's Lesson", 11388, "Suffix", 68, {"affliction_lightning_damage": 136, "affliction_elemental_damage": 281}, 228455793),
        ("Disorienting Display", 11389, "Suffix", 50, {"affliction_lightning_damage": 364, "affliction_elemental_damage": 750}, 3206911230),
        ("Prismatic Heart", 11390, "Prefix", 1, {"affliction_lightning_damage": 727, "affliction_elemental_damage": 1500}, 2342448236),
        ("Widespread Destruction", 11391, "Suffix", 1, {"affliction_lightning_damage": 727, "affliction_elemental_damage": 1500}, 1678643716),
        ("Snowstorm", 11395, "Prefix", 50, {"affliction_lightning_damage": 364}, 1595367309),
        ("Storm Drinker", 11396, "Prefix", 1, {"affliction_lightning_damage": 727}, 2087561637),
        ("Paralysis", 11397, "Prefix", 50, {"affliction_lightning_damage": 364}, 4272503233),
        ("Inspired Oppression", 11439, "Prefix", 75, {"affliction_lightning_damage": 45, "affliction_elemental_damage": 94}, 3872380586),
        ("Thunderstruck", 11443, "Prefix", 50, {"affliction_lightning_damage": 364}, 1741700339),
        ("Stormrider", 11444, "Prefix", 68, {"affliction_lightning_damage": 136}, 889728548),
        ("Overshock", 11445, "Prefix", 50, {"affliction_lightning_damage": 364}, 3777170562),
        ("Scintillating Idea", 11501, "Prefix", 50, {"affliction_lightning_damage": 364}, 2589589781),
        ("Renewal", 11408, "Prefix", 50, {"affliction_minion_damage": 500}, 3607300552),
        ("Raze and Pillage", 11409, "Prefix", 68, {"affliction_minion_damage": 188}, 1038897629),
        ("Rotten Claws", 11410, "Prefix", 50, {"affliction_minion_damage": 500}, 2289610642),
        ("Call to the Slaughter", 11411, "Suffix", 1, {"affliction_minion_damage": 1000}, 3317068522),
        ("Vicious Bite", 11413, "Suffix", 75, {"affliction_minion_damage": 63}, 882876854),
        ("Primordial Bond", 11414, "Suffix", 68, {"affliction_minion_damage": 188}, 622362787),
        ("Feasting Fiends", 11467, "Prefix", 1, {"affliction_minion_damage": 1000}, 383245807),
        ("Skeletal Atrophy", 11704, "Prefix", 68, {"affliction_minion_damage": 188}, 1290215329),
        ("Practiced Caster", 11381, "Prefix", 1, {"affliction_spell_damage": 1500}, 3435403756),
        ("Burden Projection", 11382, "Prefix", 50, {"affliction_spell_damage": 750}, 2008682345),
        ("Thaumophage", 11383, "Suffix", 50, {"affliction_spell_damage": 750}, 177215332),
        ("Essence Rush", 11384, "Suffix", 50, {"affliction_spell_damage": 750}, 1096136223),
        ("Sap Psyche", 11385, "Suffix", 68, {"affliction_spell_damage": 281}, 715786975),
        ("Mage Hunter", 11527, "Prefix", 68, {"affliction_spell_damage": 281}, 2118664144),
    )
    sort_order: dict[str, int] = {}
    by_name: dict[str, ClusterNotable] = {}
    for nm, order, affix, lvl, wts, th in raw_rows:
        sort_order[nm] = order
        by_name[nm] = ClusterNotable(
            name=nm,
            sort_order=order,
            affix_type=affix,
            level=lvl,
            group=f"AfflictionNotable{re.sub(r'[^A-Za-z0-9]', '', nm)}",
            weights=wts,
            blocks_second_suffix=(affix == "Suffix"),
            trade_hash=th,
        )
    return ClusterData(
        source="offline_fallback",
        large_geometry=LARGE_CLUSTER_GEOMETRY,
        large_enchants=enchants,
        notable_sort_order=sort_order,
        notables_by_name=by_name,
    )


def load_cluster_data(pob_root: Path | None = None) -> ClusterData:
    """Load Cluster Jewel topology & notable affix tables from local PoB Lua data (with offline fallback)."""
    import pob_cli.trade_query as _tq

    try:
        root = Path(pob_root) if pob_root is not None else _tq.resolve_engine_root()
    except Exception:
        return _build_offline_fallback_data()
    cj_path = root / "PoE1" / "Data" / "ClusterJewels.lua"
    mjc_path = root / "PoE1" / "Data" / "ModJewelCluster.lua"
    if not (cj_path.is_file() and mjc_path.is_file()):
        return _build_offline_fallback_data()

    try:
        st_cj = cj_path.stat()
        st_mjc = mjc_path.stat()
    except OSError:
        return _build_offline_fallback_data()

    key = (str(root.resolve()), st_cj.st_mtime_ns, st_cj.st_size, st_mjc.st_mtime_ns, st_mjc.st_size)
    with _CLUSTER_LOCK:
        cached = _CLUSTER_CACHE.get(key)
        if cached is not None:
            return cached

    try:
        cj_text = cj_path.read_text(encoding="utf-8", errors="replace")
        mjc_text = mjc_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _build_offline_fallback_data()

    geom, enchants, sort_order = _parse_cluster_jewels_lua(cj_text)
    notables = _parse_mod_jewel_cluster_lua(mjc_text, sort_order)
    if not enchants or not notables:
        return _build_offline_fallback_data()

    data = ClusterData(
        source="local_engine",
        large_geometry=geom,
        large_enchants=enchants,
        notable_sort_order=sort_order,
        notables_by_name=notables,
    )
    with _CLUSTER_LOCK:
        if len(_CLUSTER_CACHE) >= 4:
            _CLUSTER_CACHE.clear()
        _CLUSTER_CACHE[key] = data
    return data
