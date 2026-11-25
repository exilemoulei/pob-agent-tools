"""Simplified Chinese dictionary loader and stat-template translator."""

from __future__ import annotations

import json
from pathlib import Path

from .constants import (
    MOD_TAG_PREFIX_RE,
    NUM_PLACEHOLDER_RE,
    STAT_VALUE_RE,
    ZH_FILE_CATEGORY_HINT,
    ZH_NON_NAME_KEY_RE,
    ZH_SKIP_TRANSLATION_FILE_RE,
    ZH_SKIP_TRANSLATION_FILES,
)

_ZH_UI_OR_LORE_FILES: frozenset[str] = frozenset(
    {
        "ConfigOptions.json",
        "ConfigTab.json",
        "CalcBreakdown.json",
        "CalcDefence.json",
        "CalcOffence.json",
        "CalcSections.json",
        "CalcSetup.json",
        "CalcsTab.json",
        "CompareBuySimilar.json",
        "ComparePowerReportListControl.json",
        "CompareTab.json",
        "BuildDisplayStats.json",
        "Data.json",
        "Flask_tag.json",
        "Gems_tag.json",
        "GemSelectControl.json",
        "ItemDBControl.json",
        "ItemListControl.json",
        "ItemSetListControl.json",
        "ItemsTab.json",
        "MinionListControl.json",
        "Minions_tag.json",
        "PassiveTreeView.json",
        "PowerReportListControl.json",
        "SkillsTab.json",
        "TimelessJewelListControl.json",
        "TradeQuery.json",
        "TradeQueryGenerator.json",
        "Tree.json",
        "TreeTab.json",
        "monsters.json",
        "ui.json",
        "Unsorted.json",
        "A-Skills1.json",
        "A-Skills2.json",
        "A-Skillsfrom1.json",
        "A-Skillsfrom2.json",
        "Z-Uniques_Proverb.json",
        "Z-Keystone_Proverb.json",
        "Z-Ascendancy_Proverb.json",
        "Z-Exceptional_Gems_Lore.json",
        "Z-Replica_Dragonfang's_Flight.json",
        "Z-Skills_Gems_Overview.json",
        "Z-Support_Gems_Overview.json",
    }
)


def _norm_template(text: str) -> str:
    return NUM_PLACEHOLDER_RE.sub("{#}", text).lower().strip()


def _build_template_variants(text: str) -> tuple[str, ...]:
    n1 = _norm_template(text)
    n2 = n1.replace("+{#}", "{#}")
    if n2 != n1:
        return (n1, n2)
    return (n1,)


def _apply_stat_values(en_line: str, zh_tpl: str) -> str:
    vals = STAT_VALUE_RE.findall(en_line)
    if not vals:
        return zh_tpl
    out = zh_tpl
    for idx, val in enumerate(vals):
        token = f"{{{idx}}}"
        if token in out:
            out = out.replace(token, val)
    if "#" in out:
        for val in vals:
            out = out.replace("#", val, 1)
    return out


def _compute_superseded_zh_files(zh_dir: Path) -> set[str]:
    """Identify ZH JSON files superseded by merged dictionaries."""
    superseded: set[str] = set()
    if (zh_dir / "stats.json").is_file():
        superseded.update({"statDescriptions.json", "A-1a.json", "A-1b.json"})
    if (zh_dir / "tree_dn.json").is_file() and (zh_dir / "tree_sd.json").is_file():
        superseded.update({"passives.json", "tree_rt.json"})
    if (zh_dir / "gems.json").is_file():
        superseded.update({"Gems_data.txt.json", "Gems_data.json", "Items_Gems.txt.json", "Items_Gems.json"})
    if (zh_dir / "uniques.json").is_file():
        superseded.add("Uniques.txt.json")
    if (zh_dir / "items.json").is_file():
        superseded.update(
            {
                "Items_Accessories.txt.json",
                "Items_Armour.txt.json",
                "Items_Flasks.txt.json",
                "Items_Jewels.txt.json",
                "Items_Weapons.txt.json",
                "Items_Minions.json",
            }
        )
    return superseded


def _load_a1a_aliases(zh_dir: Path, obj: _ZhDictionary) -> None:
    """Index short entity-name community/TW aliases from A-1a.json without superseded stat templates."""
    alias_path = zh_dir / "A-1a.json"
    if not alias_path.is_file():
        return
    try:
        alias_payload = json.loads(alias_path.read_text(encoding="utf-8"))
        alias_entries = alias_payload.get("entries") if isinstance(alias_payload, dict) else None
        if not isinstance(alias_entries, dict):
            return
        for raw_en, raw_zh in alias_entries.items():
            if not isinstance(raw_en, str) or not isinstance(raw_zh, str):
                continue
            en = raw_en.strip()
            zh = raw_zh.strip()
            if (
                not en
                or not zh
                or len(en) > 60
                or len(zh) > 60
                or "{" in en
                or "#" in en
                or "\n" in en
                or "\n" in zh
            ):
                continue
            bucket = obj.aliases.setdefault(en.lower(), [])
            if zh not in bucket:
                bucket.append(zh)
    except Exception:
        pass


class _ZhDictionary:
    """Loaded Simplified Chinese lookup tables and mod stat template translator."""

    def __init__(self) -> None:
        self.available = False
        self.gems: dict[str, str] = {}
        self.uniques: dict[str, str] = {}
        self.bases: dict[str, str] = {}
        self.passives: dict[str, str] = {}
        self.stats_exact: dict[str, str] = {}
        self.stat_templates: dict[str, str] = {}
        self.aliases: dict[str, list[str]] = {}
        self.all_entries: list[tuple[str, str, str, int]] = []

    @classmethod
    def load(cls, zh_dir: Path | None) -> _ZhDictionary:
        obj = cls()
        if zh_dir is None or not zh_dir.is_dir():
            return obj

        obj.available = True
        load_order: list[str] = []
        meta_path = zh_dir / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                load_order = [str(f) for f in meta.get("load_order", [])]
            except Exception:
                load_order = []

        seen_files: set[str] = set()
        ordered_files: list[Path] = []
        for fn in load_order:
            p = zh_dir / fn
            if p.is_file() and fn not in seen_files:
                ordered_files.append(p)
                seen_files.add(fn)
        for p in sorted(zh_dir.glob("*.json")):
            if p.name not in seen_files:
                ordered_files.append(p)
                seen_files.add(p.name)

        superseded = _compute_superseded_zh_files(zh_dir)
        dedup_pairs: dict[tuple[str, str, str], tuple[str, int]] = {}

        for p in ordered_files:
            fn = p.name
            if (
                fn in ZH_SKIP_TRANSLATION_FILES
                or ZH_SKIP_TRANSLATION_FILE_RE.search(fn)
                or fn in superseded
                or fn in _ZH_UI_OR_LORE_FILES
            ):
                continue
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if not isinstance(entries, dict):
                continue

            cat_hint, rank_hint = ZH_FILE_CATEGORY_HINT.get(
                fn, ZH_FILE_CATEGORY_HINT.get(fn.lower(), ("other", 3))
            )
            is_season_patch = fn.startswith("Z-S") and fn.endswith(".json")
            is_gem_file = fn in ("gems.json", "Gems_data.txt.json", "Items_Gems.txt.json", "Z-Skills.json") or is_season_patch
            is_unique_file = fn in ("uniques.json", "Uniques.txt.json", "Z-S28-Foulborn.json", "Z-Flask.json", "Z-Jewel.json") or is_season_patch
            is_base_file = fn in ("items.json", "Items_Accessories.txt.json", "Items_Armour.txt.json", "Items_Flasks.txt.json", "Items_Jewels.txt.json", "Items_Weapons.txt.json", "Z-Item.json", "Z-Flask.json", "Z-Jewel.json", "Z-Addition.json", "Z.json") or is_season_patch
            # passives.json is a bootstrapped engine's merge of the pack's tree and tattoo files.
            is_passive_file = (
                fn in ("tree_dn.json", "TattooPassives.json", "Z-Phrecia.json", "passives.json")
                or fn.lower() in ("tatto.json", "tattoo.json")
                or is_season_patch
            )
            is_stat_file = fn in ("statDescriptions.json", "stats.json", "Query_Mod.json", "ModMap.json", "A-1a.json", "A-1b.json", "Z-Item.json", "tree_sd.json", "Z-Addition.json", "Z.json") or is_season_patch

            for raw_en, raw_zh in entries.items():
                if not isinstance(raw_en, str) or not isinstance(raw_zh, str):
                    continue
                en = raw_en.strip()
                zh = raw_zh.strip()
                if not en or not zh or ZH_NON_NAME_KEY_RE.match(en) or ZH_NON_NAME_KEY_RE.match(zh):
                    continue

                if is_gem_file:
                    if fn != "Items_Gems.txt.json" or en not in obj.gems:
                        obj.gems[en] = zh
                if is_unique_file:
                    obj.uniques[en] = zh
                if is_base_file:
                    obj.bases[en] = zh
                if is_passive_file:
                    obj.passives[en] = zh
                if is_stat_file:
                    obj.stats_exact[en.lower()] = zh
                    if "{" in en or "#" in en:
                        for norm_k in _build_template_variants(en):
                            obj.stat_templates[norm_k] = zh

                key = (en.lower(), zh, cat_hint)
                prev = dedup_pairs.get(key)
                if prev is None or rank_hint < prev[1]:
                    dedup_pairs[key] = (en, rank_hint)

        obj.all_entries = [(en, k[1], k[2], rank) for k, (en, rank) in dedup_pairs.items()]
        if "A-1a.json" in superseded:
            _load_a1a_aliases(zh_dir, obj)
        return obj

    def translate_gem(self, name: str) -> str | None:
        if not self.available:
            return None
        return (
            self.gems.get(name)
            or self.gems.get(f"{name} Support")
            or self.gems.get(name.removesuffix(" Support"))
        )

    def translate_unique(self, name: str) -> str | None:
        if not self.available:
            return None
        return self.uniques.get(name)

    def translate_base(self, name: str) -> str | None:
        if not self.available:
            return None
        return self.bases.get(name)

    def translate_stat_line(self, line: str) -> str | None:
        if not self.available or not line:
            return None
        clean = MOD_TAG_PREFIX_RE.sub("", line).strip()
        if not clean:
            return None
        exact = self.stats_exact.get(clean.lower())
        if exact is not None:
            return exact

        for norm_k in _build_template_variants(clean):
            tpl = self.stat_templates.get(norm_k)
            if tpl is not None:
                return _apply_stat_values(clean, tpl)
        return None
