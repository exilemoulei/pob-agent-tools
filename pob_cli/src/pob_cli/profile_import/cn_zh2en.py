"""Pure-Python ZH -> EN translator for CN PoE1 character JSON using local 3.30 PobTools-zh dictionaries (L1)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from ..game_data import fold_han

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_PLACEHOLDER_RE = re.compile(r"\{(\d+)\}|#")
_SKELETON_STRIP_RE = re.compile(r"[{}\d.+\-\s\u3000]")
_S26_WEAPON_ENCHANT_RE = re.compile(r"^元素伤害(提高|降低)\s*\d+%$")

_CLASS_ZH_TO_EN: dict[str, str] = {
    "贵族": "Scion", "升华使徒": "Ascendant",
    "野蛮人": "Marauder", "勇士": "Juggernaut", "暴徒": "Berserker", "酋长": "Chieftain",
    "游侠": "Ranger", "锐眼": "Deadeye", "侠客": "Raider", "追猎者": "Pathfinder", "守望者": "Warden",
    "女巫": "Witch", "秘术家": "Occultist", "元素使": "Elementalist", "死灵师": "Necromancer",
    "决斗者": "Duelist", "处刑者": "Slayer", "卫士": "Gladiator", "冠军": "Champion",
    "圣堂武僧": "Templar", "判官": "Inquisitor", "圣宗": "Hierophant", "守护者": "Guardian",
    "暗影刺客": "Shadow", "刺客": "Assassin", "破坏者": "Saboteur", "欺诈师": "Trickster",
}

_PROP_ZH_TO_EN: dict[str, str] = {
    "品质": "Quality", "护甲": "Armour", "闪避值": "Evasion Rating", "能量护盾": "Energy Shield",
    "结界": "Ward", "范围": "Radius", "仅限": "Limited to", "等级": "Level",
    "格挡几率": "Chance to Block", "暴击几率": "Critical Strike Chance",
    "每秒攻击次数": "Attacks per Second", "物理伤害": "Physical Damage",
    "元素伤害": "Elemental Damage", "混沌伤害": "Chaos Damage",
}

_REQ_ZH_TO_EN: dict[str, str] = {
    "等级": "Level", "职业：": "Class:", "职业:": "Class:", "力量": "Str", "敏捷": "Dex", "智慧": "Int",
}

_INFLUENCE_WRAPPERS: tuple[tuple[str, str], ...] = (
    ("传奇怪物在场时，", "While a Unique Enemy is in your Presence, "),
    ("传奇怪物在场时：", "While a Unique Enemy is in your Presence, "),
    ("异界终极首领在场时，", "While a Pinnacle Atlas Boss is in your Presence, "),
    ("异界终极首领在场时：", "While a Pinnacle Atlas Boss is in your Presence, "),
)

_BUILTIN_SUPPORT_RE = re.compile(r"^(?:由\s*)?(?:等级\s*(\d+)\s*的?\s*)?(.+?)\s*辅助$|^被等级\s*(\d+)\s*的?\s*(.+?)\s*辅助$")
_FORBIDDEN_REF_RE = re.compile(r"^若禁断之(?:火|肉)上有符合的词缀[，,]配置\s*(.+)$")
_ANOINT_REF_RE = re.compile(r"^配置\s*(.+)$")

_BASE_FILES = ("items.json", "Items_Accessories.txt.json", "Items_Armour.txt.json", "Items_Flasks.txt.json", "Items_Jewels.txt.json", "Items_Weapons.txt.json", "Z-Item.json", "Z-Flask.json", "Z-Jewel.json", "Z-Addition.json", "Z-S28.json", "Z-S29.json", "Z-S30.json")
_UNIQUE_FILES = ("uniques.json", "Uniques.txt.json", "Z-S28-Foulborn.json", "Z-S28.json", "Z-S29.json", "Z-S30.json", "Z-Addition.json")
_GEM_FILES = ("gems.json", "Gems_data.txt.json", "Items_Gems.txt.json", "Z-Skills.json", "Z-Skills_Gems_Overview.json", "Z-Support_Gems_Overview.json", "Z-S28.json", "Z-S29.json", "Z-S30.json")
_PASSIVE_FILES = ("passives.json", "passiveTree.json", "tree_dn.json", "A-Forbidden.json", "Tatto.json", "TattooPassives.json", "Z-Phrecia.json", "Z-S30-Pearl_of_Tsoatha-Passive_Tree.json")
_STAT_FILES = ("stats.json", "statDescriptions.json", "ModMap.json", "A-Forbidden.json", "Z-S30.json", "Z-S30-Pearl_of_Tsoatha-Boots.json", "Z-S30-Pearl_of_Tsoatha-Gloves.json", "Z-S30-Pearl_of_Tsoatha-Helmet.json", "Z-S29.json", "Z-S28.json", "Z-S28-Foulborn.json", "Z-Addition.json", "Z-Other.json")


def _norm_key(text: str) -> str:
    return fold_han(str(text or "")).strip().lower()


def _text_skeleton(text: str) -> str:
    return _SKELETON_STRIP_RE.sub("", _norm_key(text))


def _compile_zh_template(zh_tmpl: str) -> tuple[re.Pattern[str], list[str]] | None:
    """Compile a ZH stat template with `{0}`/`{1}`/`#` placeholders into a regex + placeholder keys."""
    parts: list[str] = []
    keys: list[str] = []
    pos = 0
    for idx, m in enumerate(_PLACEHOLDER_RE.finditer(zh_tmpl)):
        parts.append(re.escape(zh_tmpl[pos : m.start()]))
        key = m.group(1) if m.group(1) is not None else str(idx)
        keys.append(key)
        parts.append(r"([+-]?\d+(?:\.\d+)?(?:\s*-\s*[+-]?\d+(?:\.\d+)?)?)")
        pos = m.end()
    if not keys:
        return None
    parts.append(re.escape(zh_tmpl[pos:]))
    # Allow optional whitespace differences around placeholders
    pat_str = "^" + re.sub(r"\\ ", r"\\s*", "".join(parts)) + "$"
    try:
        return re.compile(pat_str), keys
    except re.error:
        return None


def _render_en_template(en_tmpl: str, values: dict[str, str]) -> str:
    out = en_tmpl
    for key, val in values.items():
        out = out.replace(f"{{{key}}}", val)
    if "#" in out:
        idx = 0
        def _repl(_m: re.Match[str]) -> str:
            nonlocal idx
            v = values.get(str(idx), "#")
            idx += 1
            return v
        out = re.sub(r"#", _repl, out)
    return out


_DICT_CACHE: dict[tuple[str, float], Zh2EnDictionary] = {}


class Zh2EnDictionary:
    """Reverse ZH -> EN dictionary loaded from local PobTools-zh `Data/poe1/zh-rCN/*.json`."""

    def __init__(self) -> None:
        self.base_map: dict[str, str] = {}
        self.unique_map: dict[str, str] = {}
        self.gem_map: dict[str, str] = {}
        self.passive_map: dict[str, str] = {}
        self.stat_exact: dict[str, str] = {}
        self.stat_skeleton: dict[str, list[tuple[str, str]]] = {}
        self._compiled_cache: dict[str, tuple[re.Pattern[str], list[str]] | None] = {}

    @classmethod
    def from_entries(
        cls,
        *,
        bases: dict[str, str] | None = None,
        uniques: dict[str, str] | None = None,
        gems: dict[str, str] | None = None,
        passives: dict[str, str] | None = None,
        stats: dict[str, str] | None = None,
    ) -> Zh2EnDictionary:
        """Build a dictionary directly from `{en: zh}` dicts (used in unit tests & custom loaders)."""
        d = cls()
        d._ingest_exact(d.base_map, bases or {})
        d._ingest_exact(d.unique_map, uniques or {})
        d._ingest_exact(d.gem_map, gems or {})
        d._ingest_exact(d.passive_map, passives or {})
        d._ingest_stats(stats or {})
        return d

    @classmethod
    def from_engine_dir(cls, pob_root: Path | None) -> Zh2EnDictionary:
        """Load & cache 3.30 `Data/poe1/zh-rCN` dictionaries from a PoB engine root."""
        if pob_root is None:
            return cls()
        zh_dir = Path(pob_root) / "Data" / "poe1" / "zh-rCN"
        if not zh_dir.is_dir():
            return cls()
        meta = zh_dir / "meta.json"
        mtime = meta.stat().st_mtime if meta.is_file() else zh_dir.stat().st_mtime
        cache_key = (str(zh_dir.resolve()), mtime)
        if cache_key in _DICT_CACHE:
            return _DICT_CACHE[cache_key]

        d = cls()

        def _load_entries(filename: str) -> dict[str, str]:
            p = zh_dir / filename
            if not p.is_file():
                return {}
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                ent = raw.get("entries") if isinstance(raw, dict) else None
                return ent if isinstance(ent, dict) else {}
            except Exception:
                return {}

        for fn in _BASE_FILES:
            d._ingest_exact(d.base_map, _load_entries(fn))
        for fn in _UNIQUE_FILES:
            d._ingest_exact(d.unique_map, _load_entries(fn))
        for fn in _GEM_FILES:
            d._ingest_exact(d.gem_map, _load_entries(fn))
        for fn in _PASSIVE_FILES:
            d._ingest_exact(d.passive_map, _load_entries(fn))
        for fn in _STAT_FILES:
            d._ingest_stats(_load_entries(fn))

        _DICT_CACHE[cache_key] = d
        return d

    def _ingest_exact(self, target: dict[str, str], entries: dict[str, str]) -> None:
        for en, zh in entries.items():
            if not isinstance(en, str) or not isinstance(zh, str):
                continue
            k = _norm_key(zh)
            if k and k not in target:
                target[k] = en.strip()

    def _ingest_stats(self, entries: dict[str, str]) -> None:
        for en, zh in entries.items():
            if not isinstance(en, str) or not isinstance(zh, str):
                continue
            zh_s = zh.strip()
            en_s = en.strip()
            if not zh_s or not en_s:
                continue
            k = _norm_key(zh_s)
            if _PLACEHOLDER_RE.search(zh_s):
                skel = _text_skeleton(zh_s)
                if skel:
                    bucket = self.stat_skeleton.setdefault(skel, [])
                    if len(bucket) < 40:
                        bucket.append((zh_s, en_s))
            else:
                if k and k not in self.stat_exact:
                    self.stat_exact[k] = en_s

    def translate_class(self, text: str) -> str:
        s = str(text or "").strip()
        if not s or not _CJK_RE.search(s):
            return s
        return _CLASS_ZH_TO_EN.get(fold_han(s), self.passive_map.get(_norm_key(s), s))

    def translate_base(self, text: str) -> str:
        s = str(text or "").strip()
        if not s or not _CJK_RE.search(s):
            return s
        k = _norm_key(s)
        return self.base_map.get(k) or self.unique_map.get(k) or s

    def translate_unique(self, text: str) -> str:
        s = str(text or "").strip()
        if not s or not _CJK_RE.search(s):
            return s
        k = _norm_key(s)
        return self.unique_map.get(k) or self.base_map.get(k) or ""

    def translate_gem(self, text: str) -> str:
        s = str(text or "").strip()
        if not s or not _CJK_RE.search(s):
            return s
        k = _norm_key(s)
        if k in self.gem_map:
            return self.gem_map[k]
        # Handle CN `（辅）` / `辅助` suffix variants
        for suffix in ("（辅）", "(辅)", "辅助"):
            if s.endswith(suffix):
                stem = s[: -len(suffix)].strip()
                hit = self.gem_map.get(_norm_key(stem))
                if hit:
                    return hit if hit.endswith("Support") else f"{hit} Support"
        return s

    def translate_passive(self, text: str) -> str:
        s = str(text or "").strip()
        if not s or not _CJK_RE.search(s):
            return s
        k = _norm_key(s)
        return self.passive_map.get(k) or self.base_map.get(k) or _CLASS_ZH_TO_EN.get(fold_han(s), s)

    def translate_mod(self, mod: str) -> tuple[str, bool]:
        """Translate one CN mod line into English. Returns `(translated, matched)`."""
        s = str(mod or "").strip()
        if not s or not _CJK_RE.search(s):
            return s, True

        # 1. Influence condition prefix wrapper
        folded = fold_han(s)
        for zh_pref, en_pref in _INFLUENCE_WRAPPERS:
            if folded.startswith(zh_pref):
                inner, ok = self.translate_mod(s[len(zh_pref) :].strip())
                if ok:
                    return f"{en_pref}{inner}", True

        # 2. Exact non-numeric stat match first (preserves exact A-Forbidden / Z-S30 disambiguations)
        k = _norm_key(s)
        if k in self.stat_exact:
            return self.stat_exact[k], True

        # 3. Forbidden Flame/Flesh & Anoint reference lines (fallback when not in exact table)
        m_forb = _FORBIDDEN_REF_RE.match(folded)
        if m_forb:
            node_en = self.translate_passive(m_forb.group(1).strip())
            if not _CJK_RE.search(node_en):
                jewel = "Forbidden Flame" if "禁断之火" in folded else "Forbidden Flesh"
                return f"Allocates {node_en} if you have the matching modifier on {jewel}", True
        m_anoint = _ANOINT_REF_RE.match(folded)
        if m_anoint:
            node_en = self.translate_passive(m_anoint.group(1).strip())
            if not _CJK_RE.search(node_en):
                return f"Allocates {node_en}", True

        # 4. Skeleton-indexed numeric template match
        skel = _text_skeleton(s)
        norm_line = _norm_key(s)
        for zh_tmpl, en_tmpl in self.stat_skeleton.get(skel, ()):
            norm_tmpl = _norm_key(zh_tmpl)
            compiled = self._compiled_cache.get(norm_tmpl)
            if compiled is None and norm_tmpl not in self._compiled_cache:
                compiled = _compile_zh_template(norm_tmpl)
                self._compiled_cache[norm_tmpl] = compiled
            if compiled is None:
                continue
            pat, keys = compiled
            m = pat.match(norm_line)
            if m:
                vals = {keys[i]: m.group(i + 1).replace(" ", "") for i in range(len(keys))}
                rendered = _render_en_template(en_tmpl, vals)
                if not _CJK_RE.search(rendered):
                    return rendered, True

        return s, False


def _pre_handle_cn_item(item: dict[str, Any]) -> None:
    """Apply CN localization disambiguation fixes (from cn-poe-utils)."""
    # 1. Scion Assassin collision on Forbidden Flame / Forbidden Flesh
    reqs = item.get("requirements") or []
    is_scion = any(
        isinstance(r, dict)
        and str(r.get("name") or "") in ("职业：", "职业:")
        and any(isinstance(v, list) and v and "贵族" in str(v[0]) for v in (r.get("values") or []))
        for r in reqs
    )
    if is_scion and isinstance(item.get("explicitMods"), list):
        item["explicitMods"] = [
            re.sub(r"暗影$", "暗影（贵族）", str(m)) if isinstance(m, str) else m
            for m in item["explicitMods"]
        ]
    # 2. S26 weapon enchant local ele dmg collision
    if isinstance(item.get("enchantMods"), list):
        item["enchantMods"] = [
            f"该武器的{m}" if isinstance(m, str) and _S26_WEAPON_ENCHANT_RE.match(m.strip()) else m
            for m in item["enchantMods"]
        ]


def _translate_gem_in_place(gem: dict[str, Any], dictionary: Zh2EnDictionary) -> None:
    for field in ("baseType", "typeLine"):
        if isinstance(gem.get(field), str):
            gem[field] = dictionary.translate_gem(gem[field])
    if isinstance(gem.get("hybrid"), dict) and isinstance(gem["hybrid"].get("baseTypeName"), str):
        gem["hybrid"]["baseTypeName"] = dictionary.translate_gem(gem["hybrid"]["baseTypeName"])
    for prop in gem.get("properties") or []:
        if isinstance(prop, dict) and isinstance(prop.get("name"), str):
            pn = fold_han(prop["name"]).strip()
            if pn in _PROP_ZH_TO_EN:
                prop["name"] = _PROP_ZH_TO_EN[pn]
    bis = gem.get("builtInSupport")
    if isinstance(bis, str) and _CJK_RE.search(bis):
        m = _BUILTIN_SUPPORT_RE.match(fold_han(bis).strip())
        if m:
            lvl = m.group(1) or m.group(3) or "1"
            sup_zh = (m.group(2) or m.group(4) or "").strip()
            sup_en = dictionary.translate_gem(sup_zh).removesuffix(" Support")
            if not _CJK_RE.search(sup_en):
                gem["builtInSupport"] = f"Supported by Level {lvl} {sup_en}"


def _translate_item_in_place(
    item: dict[str, Any],
    dictionary: Zh2EnDictionary,
    warnings: list[str],
) -> None:
    _pre_handle_cn_item(item)
    slot_label = str(item.get("inventoryId") or item.get("baseType") or "Item")

    base_zh = str(item.get("baseType") or item.get("typeLine") or "").strip()
    base_en = dictionary.translate_base(base_zh)
    item["baseType"] = base_en
    name_zh = str(item.get("name") or "").strip()
    if name_zh:
        uniq_en = dictionary.translate_unique(name_zh)
        item["name"] = uniq_en or "Item"
    item["typeLine"] = base_en

    for req in item.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        rname = fold_han(str(req.get("name") or "")).strip()
        if rname in _REQ_ZH_TO_EN:
            req["name"] = _REQ_ZH_TO_EN[rname]
        for val_pair in req.get("values") or []:
            if isinstance(val_pair, list) and val_pair and isinstance(val_pair[0], str):
                val_pair[0] = dictionary.translate_class(val_pair[0])

    for prop in item.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        pname = fold_han(str(prop.get("name") or "")).strip()
        if pname in _PROP_ZH_TO_EN:
            prop["name"] = _PROP_ZH_TO_EN[pname]

    for mod_key in (
        "enchantMods", "implicitMods", "explicitMods", "craftedMods",
        "fracturedMods", "mutatedMods", "scourgeMods", "crucibleMods", "utilityMods",
    ):
        mods = item.get(mod_key)
        if not isinstance(mods, list):
            continue
        new_mods: list[str] = []
        for raw_mod in mods:
            if not isinstance(raw_mod, str):
                continue
            for sub_line in raw_mod.split("\n"):
                sub = sub_line.strip()
                if not sub:
                    continue
                translated, ok = dictionary.translate_mod(sub)
                new_mods.append(translated)
                if not ok and _CJK_RE.search(translated):
                    warnings.append(f"[CN-TRANSLATE-WARN] {slot_label} ({base_en}): {sub}")
        item[mod_key] = new_mods

    for si in item.get("socketedItems") or []:
        if not isinstance(si, dict):
            continue
        if si.get("abyssJewel"):
            _translate_item_in_place(si, dictionary, warnings)
        else:
            _translate_gem_in_place(si, dictionary)


def translate_cn_bundle(
    bundle: dict[str, Any],
    dictionary: Zh2EnDictionary,
) -> tuple[dict[str, Any], list[str]]:
    """Translate a CN character bundle (`char`, `items`, `passiveSkills`) into English JSON + warnings."""
    out = copy.deepcopy(bundle)
    warnings: list[str] = []

    if isinstance(out.get("char"), dict) and isinstance(out["char"].get("class"), str):
        out["char"]["class"] = dictionary.translate_class(out["char"]["class"])

    items_obj = out.get("items")
    if isinstance(items_obj, dict):
        if isinstance(items_obj.get("character"), dict) and isinstance(items_obj["character"].get("class"), str):
            items_obj["character"]["class"] = dictionary.translate_class(items_obj["character"]["class"])
        for it in items_obj.get("items") or []:
            if isinstance(it, dict):
                _translate_item_in_place(it, dictionary, warnings)

    tree_obj = out.get("passiveSkills")
    if isinstance(tree_obj, dict):
        for jewel in tree_obj.get("items") or []:
            if isinstance(jewel, dict):
                _translate_item_in_place(jewel, dictionary, warnings)
        overrides = tree_obj.get("skill_overrides")
        if isinstance(overrides, dict):
            for ov in overrides.values():
                if isinstance(ov, dict) and isinstance(ov.get("name"), str):
                    ov["name"] = dictionary.translate_passive(ov["name"])

    return out, warnings
