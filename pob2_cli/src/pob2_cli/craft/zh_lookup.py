# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 0.5.5 1,768-base whitelist, PoE1 base blocklist, and CN/TW base & stat reverse-lookup (L2)."""

from __future__ import annotations

import difflib
import gzip
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..errors import CrossGameHallucinationError
from ..han_fold import fold_han

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_HAS_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_RICH_LINK_RE = re.compile(r"\[[^\]|]+\|([^\]]+)\]|\[([^\]]+)\]")
_NUM_OR_RANGE_RE = re.compile(
    r"[+-]?\(\s*[+-]?\d+(?:\.\d+)?\s*-\s*[+-]?\d+(?:\.\d+)?\s*\)|[+-]?\d+(?:\.\d+)?"
)
_SLOT_RE = re.compile(r"\{(\d+)\}")

# Quality / tier prefixes that may decorate valid PoE2 base names in clipboard text.
_EN_BASE_PREFIXES: tuple[str, ...] = (
    "Superior ",
    "Exceptional ",
    "Runeforged ",
    "Runemastered ",
    "Expert ",
    "Advanced ",
    "Synthesised ",
)
_ZH_BASE_PREFIXES: tuple[str, ...] = (
    "精良的",
    "卓越的",
    "非凡的",
    "符文师匠",
    "符文锻造",
    "符文",
    "进阶",
    "专家",
    "高阶",
)

# Unambiguous PoE1-only item bases / uniques that MUST trigger CrossGameHallucinationError.
POE1_ONLY_BASES: dict[str, str] = {
    "viridian jewel": "PoE1 jewel base; PoE2 uses Ruby, Emerald, Sapphire, Diamond, and Time-Lost Jewels.",
    "crimson jewel": "PoE1 jewel base; PoE2 uses Ruby, Emerald, Sapphire, Diamond, and Time-Lost Jewels.",
    "cobalt jewel": "PoE1 jewel base; PoE2 uses Ruby, Emerald, Sapphire, Diamond, and Time-Lost Jewels.",
    "prismatic jewel": "PoE1 jewel base; PoE2 uses Ruby, Emerald, Sapphire, Diamond, and Time-Lost Jewels.",
    "murderous eye jewel": "PoE1 Abyss jewel base; Abyss jewels do not exist in PoE2.",
    "searching eye jewel": "PoE1 Abyss jewel base; Abyss jewels do not exist in PoE2.",
    "hypnotic eye jewel": "PoE1 Abyss jewel base; Abyss jewels do not exist in PoE2.",
    "ghastly eye jewel": "PoE1 Abyss jewel base; Abyss jewels do not exist in PoE2.",
    "cluster jewel": "PoE1 Cluster Jewels do not exist in PoE2.",
    "small cluster jewel": "PoE1 Cluster Jewels do not exist in PoE2.",
    "medium cluster jewel": "PoE1 Cluster Jewels do not exist in PoE2.",
    "large cluster jewel": "PoE1 Cluster Jewels do not exist in PoE2.",
    "vaal regalia": "PoE1 ES Body Armour base; PoE2 uses Vile Robe / Ancestral Robe / Altar Robe (`Body_Armours_int`).",
    "astral plate": "PoE1 Armour Body base; PoE2 uses Conquered Plate / Ornate Plate (`Body_Armours_str`).",
    "stygian vise": "PoE1 Abyssal Socket belt; PoE2 belts have Charm slots (`Utility Belt`, `Heavy Belt`).",
    "mageblood": "PoE1 unique Utility Flask belt; PoE2 has no Utility Flasks.",
    "hubris circlet": "PoE1 ES Helmet base; PoE2 uses Kamasan Tiara / Ancestral Tiara (`Helmets_int`).",
    "sorcerer boots": "PoE1 ES Boots base; PoE2 uses Sekhema Sandals / Silken Slippers (`Boots_int`).",
    "sorcerer gloves": "PoE1 ES Gloves base; PoE2 uses Sirenscale Gloves / Sombre Gloves (`Gloves_int`).",
    "spiked gloves": "PoE1 Atlas gloves base; not in PoE2.",
    "fingerless silk gloves": "PoE1 Atlas gloves base; not in PoE2.",
    "gripped gloves": "PoE1 Atlas gloves base; not in PoE2.",
    "two-toned boots": "PoE1 Atlas boots base; not in PoE2.",
    "opal ring": "PoE1 Atlas ring base; PoE2 uses Breach Ring / Amethyst Ring / Prismatic Ring.",
    "steel ring": "PoE1 Atlas ring base; not in PoE2.",
    "vermillion ring": "PoE1 Atlas ring base; not in PoE2.",
    "crystal belt": "PoE1 Atlas belt base; not in PoE2.",
    "marble amulet": "PoE1 Atlas amulet base; PoE2 uses Solar Amulet / Stellar Amulet.",
    "blue pearl amulet": "PoE1 Atlas amulet base; not in PoE2.",
    "bone helmet": "PoE1 Atlas helmet base; not in PoE2.",
    "titanium spirit shield": "PoE1 Spirit Shield base; PoE2 uses Foci (`Foci`) instead of Spirit Shields.",
    "archon kite shield": "PoE1 Kite Shield base; PoE2 uses Crest Shields (`Shields_str_int`).",
    "imbued wand": "PoE1 wand base; PoE2 uses Attuned Wand / Siphoning Wand / Volatile Wand.",
    "profane wand": "PoE1 wand base; PoE2 uses Attuned Wand / Withered Wand.",
    "opal wand": "PoE1 wand base; PoE2 uses Attuned Wand / Galvanic Wand.",
    "thicket bow": "PoE1 bow base; PoE2 uses Dualstring Bow / Gemini Bow / Obliterator Bow.",
    "imperial bow": "PoE1 bow base; PoE2 uses Dualstring Bow / Gemini Bow.",
    "spine bow": "PoE1 bow base; PoE2 uses Dualstring Bow / Gemini Bow.",
    "jeweled foil": "PoE1 thrusting sword base; not in PoE2.",
    "jewelled foil": "PoE1 thrusting sword base; not in PoE2.",
    "quicksilver flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "granite flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "jade flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "quartz flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "diamond flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "silver flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "amethyst flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "bismuth flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "basalt flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "sulphur flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "stibnite flask": "PoE1 Utility Flask; PoE2 only has Life/Mana Flasks and Belt Charms.",
    "progenesis": "PoE1 unique Amethyst Flask; not in PoE2.",
    "瓦尔法衣": "PoE1 ES 胸甲底材（Vaal Regalia），PoE2 对应先祖长袍 / 邪恶长袍（Vile Robe）。",
    "星芒战铠": "PoE1 护甲胸甲底材（Astral Plate），PoE2 不存在。",
    "冥河腰带": "PoE1 深渊插槽腰带（Stygian Vise），PoE2 不存在。",
    "魔血": "PoE1 传奇功能药剂腰带（Mageblood），PoE2 无功能药剂。",
    "星团珠宝": "PoE1 星团珠宝（Cluster Jewel），PoE2 不存在。",
    "水银药剂": "PoE1 功能药剂（Quicksilver Flask），PoE2 仅保留血/蓝药与腰带咒符。",
    "坚岩药剂": "PoE1 功能药剂（Granite Flask），PoE2 仅保留血/蓝药与腰带咒符。",
}


def check_poe1_base_hallucination(raw_base: str) -> None:
    """Raise `CrossGameHallucinationError` if *raw_base* names a PoE1-only base or unique."""
    s = (raw_base or "").strip()
    if not s:
        return
    low = s.lower()
    folded = fold_han(s)
    hit_key: str | None = None
    if low in POE1_ONLY_BASES:
        hit_key = low
    elif folded in POE1_ONLY_BASES:
        hit_key = folded
    else:
        for k in POE1_ONLY_BASES:
            if _HAS_CJK_RE.search(k):
                if k in folded:
                    hit_key = k
                    break
            elif re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", low):
                hit_key = k
                break
    if hit_key is not None:
        raise CrossGameHallucinationError(
            game="poe2",
            detected_game="poe1",
            trigger=s,
            error=f"`{s}` is a PoE1-only base/item and does not exist in PoE2 0.5.5. {POE1_ONLY_BASES[hit_key]}",
            remediation="Use a valid PoE2 0.5.5 base item (e.g. `Vile Robe`, `Attuned Wand`, `Emerald`, `Breach Ring`, `Solar Amulet`) or run `pob` for PoE1.",
            equivalent_command="pob2 craft odds --base \"Vile Robe\" --target \"T1 Energy Shield\"",
            rule_id="CRAFT_POE1_BASE",
        )


def _discover_l10n_dir() -> Path | None:
    for env_key in ("POB2_DATA_DIR", "POB2_L10N_DIR"):
        raw = os.environ.get(env_key, "").strip()
        if raw:
            p = Path(raw)
            if (p / "zh-rCN").is_dir():
                return p
            if (p / "Data" / "poe2" / "zh-rCN").is_dir():
                return p / "Data" / "poe2"
    eng = os.environ.get("POB2_ENGINE_DIR", "").strip()
    if eng:
        cand = Path(eng).parent / "Data" / "poe2"
        if (cand / "zh-rCN").is_dir():
            return cand
    return None


def _zh_stat_pattern(text: str) -> tuple[str, list[str]]:
    """Normalize a Chinese stat line or template into `(compact_pattern, extracted_values)`."""
    cleaned = _RICH_LINK_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)
    cleaned = fold_han(cleaned).strip()
    # Replace template placeholders {0}, {1} first if present
    vals: list[str] = []
    if "{" in cleaned and _SLOT_RE.search(cleaned):
        pat = _SLOT_RE.sub("#", cleaned)
    else:
        def _cap(m: re.Match[str]) -> str:
            vals.append(m.group(0))
            return "#"
        pat = _NUM_OR_RANGE_RE.sub(_cap, cleaned)
    pat = re.sub(r"[+\s]+", "", pat).lower()
    # Normalize punctuation variants
    pat = pat.replace("，", ",").replace("：", ":").replace("（", "(").replace("）", ")").replace("％", "%")
    return pat, vals


class CraftL10nCatalog:
    """Indexed 1,768 PoE2 base whitelist, CN/TW base reverse-map, and CN/TW stat templates."""

    def __init__(self, data_dir: Path | None = None) -> None:
        d = data_dir or _DATA_DIR
        self.base_to_slug: dict[str, str] = {}  # canonical EN name -> poe2db_slug
        self.base_low_to_canon: dict[str, str] = {}  # lower EN name -> canonical EN name
        self.zh_to_base: dict[str, str] = {}  # folded CN/TW name -> canonical EN name
        self.stat_pat_to_en: dict[str, tuple[str, tuple[int, ...]]] = {}
        self._en_bases_by_len: tuple[tuple[str, str], ...] = ()
        self._zh_bases_by_len: tuple[tuple[str, str], ...] = ()
        self._load_bundled(d)
        self._load_optional_l10n_dir()
        self._finalize_indexes()

    def _register_stat_template(self, zh_tmpl: str, en_tmpl: str, *, overwrite: bool = False) -> None:
        if not zh_tmpl or not en_tmpl:
            return
        slot_order = tuple(int(x) for x in _SLOT_RE.findall(zh_tmpl))
        pat, _ = _zh_stat_pattern(zh_tmpl)
        if not pat:
            return
        if overwrite or pat not in self.stat_pat_to_en:
            self.stat_pat_to_en[pat] = (en_tmpl, slot_order)

    def _load_bundled(self, data_dir: Path) -> None:
        fp = data_dir / "poe2_craft_l10n.json.gz"
        if not fp.is_file():
            return
        raw = gzip.decompress(fp.read_bytes()).decode("utf-8")
        obj: dict[str, Any] = json.loads(raw)
        for en_name, slug in (obj.get("base_to_slug") or {}).items():
            self.base_to_slug[str(en_name)] = str(slug)
            self.base_low_to_canon[str(en_name).lower()] = str(en_name)
        for zh_name, en_name in (obj.get("zh_to_base") or {}).items():
            if en_name in self.base_to_slug:
                self.zh_to_base[fold_han(str(zh_name)).strip()] = str(en_name)
        for zh_tmpl, en_tmpl in (obj.get("zh_stat_templates") or {}).items():
            self._register_stat_template(str(zh_tmpl), str(en_tmpl), overwrite=True)

    def _load_optional_l10n_dir(self) -> None:
        l10n = _discover_l10n_dir()
        if l10n is None:
            return
        for loc in ("zh-rTW", "zh-rCN"):
            loc_dir = l10n / loc
            if not loc_dir.is_dir():
                continue
            is_cn = loc == "zh-rCN"
            items_fp = loc_dir / "items.json"
            if items_fp.is_file():
                try:
                    entries = (json.loads(items_fp.read_text(encoding="utf-8")) or {}).get("entries") or {}
                    for en, zh in entries.items():
                        if en in self.base_to_slug and isinstance(zh, str) and zh:
                            k = fold_han(zh).strip()
                            if is_cn or k not in self.zh_to_base:
                                self.zh_to_base[k] = en
                except (OSError, json.JSONDecodeError):
                    pass
            stats_fp = loc_dir / "stats.json"
            if stats_fp.is_file():
                try:
                    entries = (json.loads(stats_fp.read_text(encoding="utf-8")) or {}).get("entries") or {}
                    for en, zh in entries.items():
                        if isinstance(en, str) and isinstance(zh, str) and zh and en:
                            self._register_stat_template(zh, en, overwrite=False)
                except (OSError, json.JSONDecodeError):
                    pass

    def _finalize_indexes(self) -> None:
        self._en_bases_by_len = tuple(
            sorted(((k, v) for k, v in self.base_low_to_canon.items() if len(k) >= 4), key=lambda x: len(x[0]), reverse=True)
        )
        self._zh_bases_by_len = tuple(
            sorted(((k, v) for k, v in self.zh_to_base.items() if len(k) >= 2), key=lambda x: len(x[0]), reverse=True)
        )

    def lookup_exact_base(self, raw: str) -> tuple[str, str] | None:
        """Return `(canonical_base_name, poe2db_slug)` if *raw* matches one of the 1,768 PoE2 bases."""
        s = (raw or "").strip()
        if not s:
            return None
        low = s.lower()
        if low in self.base_low_to_canon:
            canon = self.base_low_to_canon[low]
            return canon, self.base_to_slug[canon]
        folded = fold_han(s)
        if folded in self.zh_to_base:
            canon = self.zh_to_base[folded]
            return canon, self.base_to_slug[canon]
        # Strip quality/tier prefixes and retry exact whitelist
        for pref in _EN_BASE_PREFIXES:
            if low.startswith(pref.lower()):
                sub = low[len(pref) :].strip()
                if sub in self.base_low_to_canon:
                    canon = self.base_low_to_canon[sub]
                    return canon, self.base_to_slug[canon]
        for pref in _ZH_BASE_PREFIXES:
            if folded.startswith(pref):
                sub = folded[len(pref) :].strip()
                if sub in self.zh_to_base:
                    canon = self.zh_to_base[sub]
                    return canon, self.base_to_slug[canon]
        return None

    def extract_magic_base(self, raw: str) -> tuple[str, str] | None:
        """Strip Magic prefix/suffix affixes around a valid PoE2 EN/ZH base (e.g. `Analyst's Grounding Charm of the Foliage`)."""
        s = (raw or "").strip()
        if not s:
            return None
        if _HAS_CJK_RE.search(s):
            folded = fold_han(s)
            for zh_key, canon in self._zh_bases_by_len:
                if zh_key in folded:
                    return canon, self.base_to_slug[canon]
            return None
        low = s.lower()
        # Require at least 2 words so random single words never accidentally match
        if " " not in low:
            return None
        padded = f" {low} "
        for en_low, canon in self._en_bases_by_len:
            needle = f" {en_low} "
            if needle in padded:
                return canon, self.base_to_slug[canon]
        return None

    def suggest_bases(self, query: str, *, limit: int = 5) -> list[str]:
        """Return Top-*limit* closest valid PoE2 base names for an unknown base error."""
        q = (query or "").strip()
        if not q:
            return ["Vile Robe", "Attuned Wand", "Solar Amulet", "Breach Ring", "Grounding Charm"][:limit]
        if _HAS_CJK_RE.search(q):
            folded = fold_han(q)
            zh_hits = difflib.get_close_matches(folded, list(self.zh_to_base.keys()), n=limit, cutoff=0.25)
            out = [f"{z} ({self.zh_to_base[z]})" for z in zh_hits]
            if len(out) < limit:
                for fallback in ("Vile Robe", "Attuned Wand", "Solar Amulet", "Breach Ring", "Grounding Charm"):
                    if fallback not in out and len(out) < limit:
                        out.append(fallback)
            return out[:limit]
        q_low = q.lower()
        # Prefer token overlap + difflib ratio
        all_names = list(self.base_to_slug.keys())
        close = difflib.get_close_matches(q, all_names, n=limit, cutoff=0.35)
        if len(close) < limit:
            q_toks = [t for t in re.split(r"[^a-z0-9]+", q_low) if len(t) >= 3]
            if q_toks:
                tok_hits = [n for n in all_names if n not in close and any(t in n.lower() for t in q_toks)]
                close.extend(tok_hits[: limit - len(close)])
        if len(close) < limit:
            for fallback in ("Vile Robe", "Attuned Wand", "Solar Amulet", "Breach Ring", "Grounding Charm"):
                if fallback not in close and len(close) < limit:
                    close.append(fallback)
        return close[:limit]

    def translate_zh_stat_line(self, raw_line: str) -> tuple[str, bool]:
        """Translate a Simplified/Traditional Chinese stat line into PoE2 EN mod text."""
        if not raw_line or not _HAS_CJK_RE.search(raw_line):
            return raw_line, True
        pat, vals = _zh_stat_pattern(raw_line)
        hit = self.stat_pat_to_en.get(pat)
        if hit is None:
            # Also try without leading/trailing '+' or '%' variance
            return raw_line, False
        en_tmpl, slot_order = hit
        if not slot_order:
            return en_tmpl, True
        if len(vals) != len(slot_order):
            return raw_line, False
        val_by_slot: dict[int, str] = {}
        for slot_id, val in zip(slot_order, vals, strict=False):
            val_by_slot[slot_id] = val
        def _sub(m: re.Match[str]) -> str:
            idx = int(m.group(1))
            v = val_by_slot.get(idx, "0")
            return v
        rendered = _SLOT_RE.sub(_sub, en_tmpl)
        # Ensure canonical leading '+' for flat/resist `+X to ...` templates when raw CN omitted '+'
        if re.match(r"^\d+(?:\.\d+)?%?\s+to\s+", rendered, re.IGNORECASE):
            rendered = f"+{rendered}"
        return rendered, True


@lru_cache(maxsize=2)
def load_craft_l10n(data_dir: Path | None = None) -> CraftL10nCatalog:
    """Load and cache the bundled PoE2 0.5.5 craft l10n & 1,768-base whitelist catalog."""
    return CraftL10nCatalog(data_dir)


__all__ = [
    "POE1_ONLY_BASES",
    "CraftL10nCatalog",
    "check_poe1_base_hallucination",
    "load_craft_l10n",
]
