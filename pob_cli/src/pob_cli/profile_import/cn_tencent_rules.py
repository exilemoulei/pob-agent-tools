"""Tencent CN localization rules for clusters, jump/timeless jewels, tattoos, and runegrafts (L1)."""

from __future__ import annotations

import re
from collections.abc import Callable

from ..game_data import fold_han
from ..game_data.tables_cn_tencent import (
    TENCENT_BASE_ALIASES,
    TENCENT_RUNEGRAFT_ALIASES,
    TENCENT_TATTOO_ALIASES,
    TENCENT_UNIQUE_ALIASES,
    passive_lookup_variants,
)
from .cn_tencent_tables import (
    CLUSTER_BASE_ENCHANT_MAP,
    CLUSTER_GRANT_PREFIX,
    CLUSTER_REVERSED_PAIRS,
)

__all__ = [
    "CLUSTER_BASE_ENCHANT_MAP", "TENCENT_BASE_ALIASES", "TENCENT_RUNEGRAFT_ALIASES",
    "TENCENT_TATTOO_ALIASES", "TENCENT_UNIQUE_ALIASES", "lookup_tattoo_fallback",
    "normalize_cluster_enchant_order", "passive_lookup_variants", "pre_handle_cn_item",
    "prepare_mod_lines", "translate_property_value", "translate_tencent_mod_line",
]

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

_RADIUS_VAL_MAP: dict[str, str] = {
    "小": "Small", "小型": "Small", "中": "Medium", "中型": "Medium", "大": "Large",
    "大型": "Large", "特大": "Very Large", "极大": "Very Large", "巨型": "Massive", "巨大": "Massive",
}

_RING_MAP = {"极小": "Small", "小": "Small", "中": "Medium", "大": "Large", "特大": "Very Large", "巨型": "Massive", "巨大": "Massive"}
_MARAKETH_MAP = {"安赛娜丝": "Asenath", "迪虚瑞特": "Deshret", "娜斯玛": "Nasima", "纳西玛": "Nasima", "巴巴拉": "Balbala", "贝尔巴拉": "Balbala"}
_KALGUUR_MAP = {"沃拉娜": "Vorana", "梅德维德": "Medved", "欧罗什": "Olroth", "乌崔德": "Uhtred"}
_KARUI_MAP = {"阿克雅": "Akoya", "阿寇亚": "Akoya", "拉凯尔塔": "Rakiata", "拉其塔": "Rakiata", "冈姆": "Kaom", "基洛瓦": "Kiloava", "基洛阿瓦": "Kiloava"}
_ABYSSAL_MAP = {"特克罗": "Tecrod", "乌拉曼": "Ulaman", "古加尔": "Kurgal", "阿曼娜姆": "Amanamu"}

_CLUSTER_GRANT_RE = re.compile(r"^(?:增加|附加)的小型?天赋(?:获得|给予)[：:]\s*(.+)$")
_CLUSTER_ALSO_RE = re.compile(r"^(?:增加|附加)的小型?天赋还(?:获得|给予)[：:]\s*(.+)$")
_CLUSTER_SOCKET_RE = re.compile(r"^其中\s*(\d+)\s*个增加的天赋为珠宝插?槽$")
_CLUSTER_NOTABLE_RE = re.compile(r"^其中\s*1\s*个增加的天赋为[：:\s]*(.+)$")
_CLUSTER_ADDS_RE = re.compile(r"^(?:增加|附加)\s*(\d+)\s*个天赋(?:技能)?$")
_CLUSTER_SOCK_OVERRIDE_RE = re.compile(r"^(?:增加|附加)\s*(\d+)\s*个珠宝插槽天赋$")
_CLUSTER_NOTHING_RE = re.compile(r"^(?:增加|附加)\s*(\d+)\s*个无特殊效果的小天赋$")
_CLUSTER_INC_EFF_RE = re.compile(r"^增加的小天赋效果提高\s*(\d+)%$")
_THREAD_RING_RE = re.compile(r"^只影响(极小|小|中|大|特大|巨型|巨大)(?:圈|环形)内的天赋$")
_IMPOSSIBLE_ESCAPE_RE = re.compile(
    r"^(.+?)范围内的(?:核心)?天赋(?:技能)?可以在\s*未连结至天赋树的情况下配置(?:\s+通途)?$"
)
_FORBIDDEN_RE = re.compile(r"^若?禁断之(火|肉)上有(?:符合|匹配)的词缀[，,]?\s*(?:则)?配置\s*(.+)$")
_ANOINT_RE = re.compile(r"^配置\s*(.+)$")
_ELE_RES_RE = re.compile(r"^所有元素抗性\s*([+-]?\d+)%$")
_MARAKETH_RE = re.compile(r"^在(安赛娜丝|迪虚瑞特|娜斯玛|纳西玛|巴巴拉|贝尔巴拉)的阿卡拉中指派\s*(\d+)\s*名德卡拉的服务$")
_KALGUUR_RE = re.compile(r"^纪念\s*(\d+)\s*件(沃拉娜|梅德维德|欧罗什|乌崔德)世系值得传唱的事迹$")
_KARUI_RE = re.compile(r"^获得(阿克雅|阿寇亚|拉凯尔塔|拉其塔|冈姆|基洛瓦|基洛阿瓦)麾下\s*(\d+)\s*名武士的领导权$")
_ABYSSAL_SUBJ_RE = re.compile(r"^征服\s*(\d+)\s*个受(特克罗|乌拉曼|古加尔|阿曼娜姆)奴役的灵魂$")
_ABYSSAL_BIND_RE = re.compile(r"^将\s*(\d+)\s*个灵魂绑定于护符匣以供养佐拉斯$")


def translate_property_value(prop_name_en: str, raw_val: str) -> str:
    """Translate Tencent CN Jewel `Radius` / `Limited to` property values into exact PoB tokens."""
    s = str(raw_val or "").strip()
    if not s or not _CJK_RE.search(s):
        return s
    folded = fold_han(s).strip()
    if prop_name_en == "Radius":
        if folded.startswith("可变"):
            return "Variable"
        return _RADIUS_VAL_MAP.get(folded, s)
    if prop_name_en == "Limited to":
        out = re.sub(r"\s*(?:史实|威宏史观)", " Historic", folded)
        return re.sub(r"\s+", " ", out).strip()
    return s


def lookup_tattoo_fallback(text: str) -> str | None:
    """Resolve tattoos and all 32 Runegrafts missing from clean zh-1.0.0 / PobTools-zh zh-rCN."""
    for cand in passive_lookup_variants(text):
        hit = TENCENT_TATTOO_ALIASES.get(cand) or TENCENT_RUNEGRAFT_ALIASES.get(cand)
        if hit:
            return hit
    return None


def translate_tencent_mod_line(
    line: str,
    *,
    translate_passive: Callable[[str], str],
    translate_mod: Callable[[str], tuple[str, bool]],
) -> tuple[str, bool] | None:
    """Apply Tencent CN specific mod rules before generic dictionary lookup."""
    folded = fold_han(line).replace("\u00a0", " ").strip()
    if not folded:
        return None
    if folded in ("通途", "Passage"):
        return "", True
    if folded in (
        "范围内的天赋技能无需连接到你的天赋树即可配置",
        "未连结至天赋树的技能，仍然可以在范围内配置",
    ):
        return "Passive Skills in Radius can be Allocated without being connected to your tree", True
    if folded == "增加的小天赋无效果":
        return "Added Small Passive Skills grant Nothing", True
    if folded == "受影响的天赋被深渊抑制":
        return "Passives affected are Conquered by the Abyssal", True
    if folded == "范围内的天赋被卡古兰抑制":
        return "Passives in radius are Conquered by the Kalguur", True

    if m := _CLUSTER_GRANT_RE.match(folded):
        inner_compact = re.sub(r"\s+", "", m.group(1)).lower()
        if inner_compact in CLUSTER_BASE_ENCHANT_MAP:
            return CLUSTER_BASE_ENCHANT_MAP[inner_compact], True
        inner_en, ok = translate_mod(m.group(1).strip())
        if ok and not _CJK_RE.search(inner_en):
            return f"{CLUSTER_GRANT_PREFIX}{inner_en}", True
        return None
    if m := _CLUSTER_ALSO_RE.match(folded):
        inner_en, ok = translate_mod(m.group(1).strip())
        if ok and not _CJK_RE.search(inner_en):
            return f"Added Small Passive Skills also grant: {inner_en}", True
        return None
    if m := _CLUSTER_SOCKET_RE.match(folded):
        n = int(m.group(1))
        return ("1 Added Passive Skill is a Jewel Socket" if n == 1 else f"{n} Added Passive Skills are Jewel Sockets"), True
    if m := _CLUSTER_NOTABLE_RE.match(folded):
        node_en = translate_passive(m.group(1).strip())
        if not _CJK_RE.search(node_en):
            return f"1 Added Passive Skill is {node_en}", True
        return None
    if m := _CLUSTER_ADDS_RE.match(folded):
        return f"Adds {m.group(1)} Passive Skills", True
    if m := _CLUSTER_SOCK_OVERRIDE_RE.match(folded):
        return f"Adds {m.group(1)} Jewel Socket Passive Skills", True
    if m := _CLUSTER_NOTHING_RE.match(folded):
        n = int(m.group(1))
        return (
            "Adds 1 Small Passive Skill which grants nothing"
            if n == 1
            else f"Adds {n} Small Passive Skills which grant nothing"
        ), True
    if m := _CLUSTER_INC_EFF_RE.match(folded):
        return f"Added Small Passive Skills have {m.group(1)}% increased Effect", True
    if m := _THREAD_RING_RE.match(folded):
        return f"Only affects Passives in {_RING_MAP[m.group(1)]} Ring", True
    if m := _IMPOSSIBLE_ESCAPE_RE.match(folded):
        ks_en = translate_passive(m.group(1).strip())
        if not _CJK_RE.search(ks_en):
            return f"Passives in Radius of {ks_en} can be Allocated without being connected to your tree", True
        return None
    if m := _FORBIDDEN_RE.match(folded):
        node_en = translate_passive(m.group(2).strip())
        if not _CJK_RE.search(node_en):
            jewel = "Forbidden Flame" if m.group(1) == "火" else "Forbidden Flesh"
            return f"Allocates {node_en} if you have the matching modifier on {jewel}", True
        return None
    if m := _ANOINT_RE.match(folded):
        node_en = translate_passive(m.group(1).strip())
        if not _CJK_RE.search(node_en):
            return f"Allocates {node_en}", True
        return None
    if m := _ELE_RES_RE.match(folded):
        raw_v = m.group(1)
        signed = f"+{raw_v}" if int(raw_v) > 0 and not raw_v.startswith(("+", "-")) else raw_v
        return f"{signed}% to all Elemental Resistances", True
    if m := _MARAKETH_RE.match(folded):
        return f"Denoted service of {m.group(2)} dekhara in the akhara of {_MARAKETH_MAP[m.group(1)]}", True
    if m := _KALGUUR_RE.match(folded):
        return f"Remembrancing {m.group(1)} songworthy deeds by the line of {_KALGUUR_MAP[m.group(2)]}", True
    if m := _KARUI_RE.match(folded):
        return f"Commanded leadership over {m.group(2)} warriors under {_KARUI_MAP[m.group(1)]}", True
    if m := _ABYSSAL_SUBJ_RE.match(folded):
        return f"Subjugating {m.group(1)} souls in the thrall of {_ABYSSAL_MAP[m.group(2)]}", True
    if m := _ABYSSAL_BIND_RE.match(folded):
        return f"Binding {m.group(1)} souls to phylacteries to sustain Zorath", True
    return None


def prepare_mod_lines(raw_text: str) -> list[str]:
    """Split a raw CN mod entry into clean lines, rejoining Impossible Escape and propagating cluster prefixes."""
    parts = [p.strip() for p in str(raw_text or "").replace("\u00a0", " ").split("\n") if p.strip()]
    if not parts:
        return []
    if (
        len(parts) >= 2
        and fold_han(parts[0]).endswith(("范围内的核心天赋技能可以在", "范围内的天赋可以在", "范围内的天赋技能可以在"))
        and fold_han(parts[1]) == "未连结至天赋树的情况下配置"
    ):
        parts = [f"{parts[0]}{parts[1]}", *parts[2:]]
    if len(parts) >= 2 and _CLUSTER_GRANT_RE.match(fold_han(parts[0])):
        fixed = [parts[0]]
        for p in parts[1:]:
            pf = fold_han(p)
            if not _CLUSTER_GRANT_RE.match(pf) and re.sub(r"\s+", "", pf).lower() in CLUSTER_BASE_ENCHANT_MAP:
                fixed.append(f"增加的小天赋获得：{p}")
            else:
                fixed.append(p)
        parts = fixed
    return [p for p in parts if fold_han(p) != "通途"]


def normalize_cluster_enchant_order(mods: list[object]) -> list[object]:
    """Ensure 2-line cluster base enchants appear in PoB's canonical ClusterJewels.lua order."""
    out = list(mods)
    i = 0
    while i + 1 < len(out):
        a, b = out[i], out[i + 1]
        ta = a if isinstance(a, str) else (a.get("description") if isinstance(a, dict) else None)
        tb = b if isinstance(b, str) else (b.get("description") if isinstance(b, dict) else None)
        if (
            isinstance(ta, str)
            and isinstance(tb, str)
            and ta.startswith(CLUSTER_GRANT_PREFIX)
            and tb.startswith(CLUSTER_GRANT_PREFIX)
            and (ta[len(CLUSTER_GRANT_PREFIX) :], tb[len(CLUSTER_GRANT_PREFIX) :]) in CLUSTER_REVERSED_PAIRS
        ):
            out[i], out[i + 1] = out[i + 1], out[i]
            i += 2
            continue
        i += 1
    return out


def _map_mod_entry(entry: object, fn: Callable[[str], str]) -> object:
    if isinstance(entry, str):
        return fn(entry)
    if isinstance(entry, dict) and isinstance(entry.get("description"), str):
        return {**entry, "description": fn(entry["description"])}
    return entry


def pre_handle_cn_item(item: dict[str, object], s26_weapon_re: re.Pattern[str]) -> None:
    """Apply CN localization disambiguation fixes (Scion Assassin suffix and S26 weapon enchants)."""
    reqs = item.get("requirements") or []
    is_scion = isinstance(reqs, list) and any(
        isinstance(r, dict)
        and str(r.get("name") or "") in ("职业：", "职业:")
        and any(isinstance(v, list) and v and "贵族" in str(v[0]) for v in (r.get("values") or []))
        for r in reqs
    )
    if is_scion and isinstance(item.get("explicitMods"), list):
        item["explicitMods"] = [
            _map_mod_entry(m, lambda txt: re.sub(r"暗影$", "暗影（贵族）", txt))
            for m in item["explicitMods"]
        ]
    if isinstance(item.get("enchantMods"), list):
        item["enchantMods"] = [
            _map_mod_entry(m, lambda txt: f"该武器的{txt}" if s26_weapon_re.match(txt.strip()) else txt)
            for m in item["enchantMods"]
        ]
