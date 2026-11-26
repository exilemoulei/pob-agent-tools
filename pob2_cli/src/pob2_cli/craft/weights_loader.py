# SPDX-License-Identifier: GPL-3.0-only
# Adapted in part from lifan-builds/poe2brain (GPL-3.0-only; see pob2_cli/ATTRIBUTION.md).
"""Zero-SQLite loader for bundled PoE2 0.5.5 ModsView weights, overrides, and base resolution."""

from __future__ import annotations

import gzip
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .base_catalog import (
    COMMON_BASE_SUFFIX_TO_SLUG,
    ELEMENT_LOCK_MAP,
    SPECIAL_BASE_SPECS,
    ZH_BASE_ALIASES,
    build_static_levers,
)
from .models import AffixSide, BaseItemEntry, LeverEntry, ModEntry, WeightConfidence

EXPECTED_GAME_PATCH = "0.5.5"

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_GEN_TYPE_TO_SIDE: dict[int, AffixSide] = {
    1: AffixSide.PREFIX,
    2: AffixSide.SUFFIX,
    3: AffixSide.IMPLICIT,
    5: AffixSide.NONE,
}
_CASTER_WEAPON_SLUGS: frozenset[str] = frozenset({"Wands", "Staves"})


def norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def strip_numbers_to_pattern(text: str) -> str:
    s = re.sub(r"[+-]?\([^)]*\)", "#", text)
    s = re.sub(r"[+-]?\d+(?:\.\d+)?", "#", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _infer_adds_tags(
    slug: str, mod_id: str, tags: tuple[str, ...], stat_text: str
) -> tuple[str, ...]:
    """Infer PoE2 caster-weapon single-element lock tags (`no_*_spell_mods`)."""
    if slug not in _CASTER_WEAPON_SLUGS:
        return ()
    if "caster" not in tags and "Spell" not in stat_text:
        return ()
    low_id = mod_id.lower()
    low_txt = stat_text.lower()
    if not (
        "spell" in low_txt
        or "damageprefixonweapon" in low_id
        or "damageprefixontwohandweapon" in low_id
        or "spellgemslevel" in low_id
    ):
        return ()
    present = [el for el in ELEMENT_LOCK_MAP if el in tags]
    if len(present) == 1:
        return ELEMENT_LOCK_MAP[present[0]]
    return ()


def _slot_limits_for_slug(slug: str) -> tuple[int, int]:
    if "Flask" in slug or slug == "Charms":
        return 1, 1
    if any(k in slug for k in ("Tablet", "Ruby", "Emerald", "Sapphire", "Diamond", "Relic")):
        return 2, 2
    return 3, 3


@dataclass(slots=True)
class CraftDataBundle:
    patch: str
    patch_mismatch_warning: str | None
    overrides: dict[str, Any]
    mods_by_slug: dict[str, tuple[ModEntry, ...]]
    measured_slugs: frozenset[str]
    levers: tuple[LeverEntry, ...]
    levers_by_key: dict[str, LeverEntry]
    removed_levers: frozenset[str]
    dormant_poe1_orbs: frozenset[str]
    base_name_to_slug: dict[str, str]


def _load_optional_pob2_bases(base_map: dict[str, str]) -> None:
    """Optionally enrich base_name -> slug from PoB2 `Data/Bases/*.lua` via env var."""
    env_dir = os.environ.get("POB2_ENGINE_DIR") or os.environ.get("POB2_DATA_DIR")
    if not env_dir:
        return
    bases_dir = Path(env_dir) / "Data" / "Bases"
    if not bases_dir.is_dir():
        bases_dir = Path(env_dir) / "PoE2" / "Data" / "Bases"
    if not bases_dir.is_dir():
        return
    file_to_slug = {
        "wand.lua": "Wands",
        "sceptre.lua": "Sceptres",
        "staff.lua": "Staves",
        "bow.lua": "Bows",
        "crossbow.lua": "Crossbows",
        "quiver.lua": "Quivers",
        "amulet.lua": "Amulets",
        "ring.lua": "Rings",
        "belt.lua": "Belts",
        "spear.lua": "Spears",
        "flail.lua": "Flails",
        "dagger.lua": "Daggers",
        "claw.lua": "Claws",
    }
    name_re = re.compile(r'itemBases\["([^"]+)"\]\s*=')
    for fname, slug in file_to_slug.items():
        fp = bases_dir / fname
        if not fp.is_file():
            continue
        try:
            txt = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in name_re.finditer(txt):
            base_map.setdefault(m.group(1).lower(), slug)


def _compile_slug_mods(
    slug: str,
    info: dict[str, Any],
    overrides: dict[str, Any],
    forced_by_lever: dict[str, tuple[str, dict[str, str]]],
) -> tuple[ModEntry, ...]:
    has_measured = bool(info.get("has_measured_normal_weights", False))
    defaults = overrides.get("defaults", {})
    desec_default_w = int(defaults.get("desecrated_unomen_weight", 2500))
    unmeasured_default_w = int(defaults.get("unmeasured_normal_pool_weight", 1000))
    mod_overrides: dict[str, Any] = overrides.get("mod_weight_overrides", {})
    pools = info.get("pools", {})

    ladders: dict[tuple[str, AffixSide, str, str], list[tuple[int, dict[str, Any]]]] = {}
    for pname in ("normal", "desecrated", "essence", "perfect_essence", "liquid"):
        for idx, item in enumerate(pools.get(pname, [])):
            side = _GEN_TYPE_TO_SIDE.get(int(item.get("gen_type_id") or 0), AffixSide.NONE)
            if side not in (AffixSide.PREFIX, AffixSide.SUFFIX):
                continue
            fams = tuple(str(f) for f in (item.get("families") or []) if f) or ("Unknown",)
            pat = strip_numbers_to_pattern(str(item.get("stat_text") or ""))
            eff_pool = "alloy" if (pname == "perfect_essence" and item.get("is_alloy")) else pname
            ladders.setdefault((eff_pool, side, fams[0], pat), []).append((idx, item))

    entries: list[ModEntry] = []
    for (eff_pool, side, fam0, _pat), members in ladders.items():
        members.sort(key=lambda x: (int(x[1].get("level") or 1), str(x[1].get("mod_id") or ""), x[0]))
        total_t = len(members)
        for rank0, (idx, item) in enumerate(members):
            level = int(item.get("level") or 1)
            raw_id = item.get("mod_id")
            mod_id = str(raw_id) if raw_id else f"{slug}_{eff_pool}_{fam0}_{side.value}_L{level}_{idx}"
            fams = tuple(str(f) for f in (item.get("families") or []) if f) or (fam0,)
            tags = tuple(str(t) for t in (item.get("tags") or []) if t)
            stat_text = str(item.get("stat_text") or "")
            raw_w = int(item.get("drop_chance") or 0)
            boss_tag = str(item["boss_tag"]) if item.get("boss_tag") else None
            name_field = str(item.get("name") or "")
            is_alloy = bool(item.get("is_alloy")) or eff_pool == "alloy"
            removes_mod = bool(item.get("removes_mod")) or eff_pool in (
                "perfect_essence",
                "alloy",
                "liquid",
            )
            currency_name: str | None = None

            if eff_pool in ("essence", "perfect_essence", "alloy", "liquid"):
                currency_name = name_field
                weight = 0
                confidence = WeightConfidence.EXACT_DETERMINISTIC
                cat = (
                    "alloy"
                    if is_alloy
                    else (
                        "liquid_emotion"
                        if eff_pool == "liquid"
                        else (
                            "perfect_or_corrupted_essence"
                            if eff_pool == "perfect_essence"
                            else "essence"
                        )
                    )
                )
                if currency_name:
                    _, fdict = forced_by_lever.setdefault(currency_name, (cat, {}))
                    fdict.setdefault(slug, mod_id)
            elif eff_pool == "desecrated":
                weight = raw_w if raw_w > 1 else desec_default_w
                confidence = WeightConfidence.EMPIRICAL_ESTIMATE
            else:
                ov = mod_overrides.get(f"{slug}:{mod_id}")
                if ov and isinstance(ov, dict):
                    weight = int(ov.get("weight", unmeasured_default_w))
                    confidence = WeightConfidence.EMPIRICAL_ESTIMATE
                elif has_measured:
                    weight = raw_w
                    confidence = WeightConfidence.VERIFIED_COMMUNITY
                else:
                    weight = raw_w if raw_w > 1 else unmeasured_default_w
                    confidence = WeightConfidence.UNMEASURED

            mod_type = re.sub(r"\d+_?$", "", mod_id) or fam0
            entries.append(
                ModEntry(
                    mod_id=mod_id,
                    mod_type=mod_type,
                    family_id=fam0,
                    families=fams,
                    base_group_slug=slug,
                    source_pool=eff_pool,
                    affix_side=side,
                    affix_name="" if currency_name else name_field,
                    internal_rank_asc=rank0 + 1,
                    display_tier=total_t - rank0,
                    required_level=level,
                    weight=weight,
                    weight_confidence=confidence,
                    boss_tag=boss_tag,
                    currency_name=currency_name,
                    is_alloy=is_alloy,
                    removes_mod=removes_mod,
                    adds_tags=_infer_adds_tags(slug, mod_id, tags, stat_text),
                    mod_tags=tags,
                    stat_text=stat_text,
                )
            )
    return tuple(entries)


@lru_cache(maxsize=2)
def load_craft_data(data_dir: Path | None = None) -> CraftDataBundle:
    """Load bundled ModsView weights & overrides once per process."""
    d = data_dir or _DATA_DIR
    with gzip.open(d / "modsview_weights.json.gz", "rt", encoding="utf-8") as fh:
        mv = json.load(fh)
    ov_path = d / "weights_overrides.json"
    overrides = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.is_file() else {}

    patch = str(mv.get("patch") or overrides.get("patch") or "unknown")
    mismatch_warn: str | None = None
    if patch != EXPECTED_GAME_PATCH:
        mismatch_warn = (
            f"[待复核] 词缀权重包补丁版本 ({patch}) 与预期 {EXPECTED_GAME_PATCH} 不一致，需复核。"
        )

    mods_by_slug: dict[str, tuple[ModEntry, ...]] = {}
    measured_slugs: set[str] = set()
    forced_by_lever: dict[str, tuple[str, dict[str, str]]] = {}

    for slug, info in mv.get("classes", {}).items():
        if info.get("has_measured_normal_weights"):
            measured_slugs.add(slug)
        mods_by_slug[slug] = _compile_slug_mods(slug, info, overrides, forced_by_lever)

    levers = build_static_levers()
    seen_ids = {lv.lever_id for lv in levers}
    for lname, (cat, forced_map) in sorted(forced_by_lever.items()):
        lid = norm_key(lname)
        if lid in seen_ids:
            continue
        seen_ids.add(lid)
        applies = (
            "magic->rare"
            if cat == "essence"
            else (
                "rare_jewel->remove_1_add_1_forced"
                if cat == "liquid_emotion"
                else "rare->remove_1_add_1_forced"
            )
        )
        levers.append(
            LeverEntry(
                lever_id=lid,
                name=lname,
                category=cat,
                is_active_drop_in_patch=True,
                applies_to_rarities=applies,
                forced_mods=forced_map,
            )
        )

    levers_by_key: dict[str, LeverEntry] = {}
    for lv in levers:
        levers_by_key[lv.lever_id] = lv
        levers_by_key[norm_key(lv.name)] = lv

    base_map: dict[str, str] = {k: v[1] for k, v in SPECIAL_BASE_SPECS.items()}
    _load_optional_pob2_bases(base_map)

    return CraftDataBundle(
        patch=patch,
        patch_mismatch_warning=mismatch_warn,
        overrides=overrides,
        mods_by_slug=mods_by_slug,
        measured_slugs=frozenset(measured_slugs),
        levers=tuple(levers),
        levers_by_key=levers_by_key,
        removed_levers=frozenset(norm_key(x) for x in overrides.get("removed_levers_in_patch", [])),
        dormant_poe1_orbs=frozenset(
            norm_key(x) for x in overrides.get("dormant_poe1_currencies_blacklist", [])
        ),
        base_name_to_slug=base_map,
    )


def resolve_base_entry(bundle: CraftDataBundle, base_name_or_slug: str) -> BaseItemEntry:
    """Resolve an English/Chinese base item name or poe2db slug to `BaseItemEntry`."""
    raw = base_name_or_slug.strip()
    if raw in ZH_BASE_ALIASES:
        raw = ZH_BASE_ALIASES[raw]
    for pref in ("Superior ", "Exceptional ", "Runeforged ", "Runemastered ", "Expert ", "Advanced "):
        if raw.lower().startswith(pref.lower()):
            raw = raw[len(pref) :].strip()

    low = raw.lower()
    if low in SPECIAL_BASE_SPECS:
        name, slug, dlvl, tags = SPECIAL_BASE_SPECS[low]
        max_p, max_s = _slot_limits_for_slug(slug)
        return BaseItemEntry(
            id=f"Base/{slug}/{norm_key(name)}",
            name=name,
            item_class=slug,
            poe2db_slug=slug,
            drop_level=dlvl,
            max_prefixes=max_p,
            max_suffixes=max_s,
            tags=tags,
            has_measured_weights=slug in bundle.measured_slugs,
        )

    slug_probe = raw.replace(" ", "_")
    for slug in bundle.mods_by_slug:
        if slug.lower() == slug_probe.lower():
            max_p, max_s = _slot_limits_for_slug(slug)
            return BaseItemEntry(
                id=f"Slug/{slug}",
                name=raw,
                item_class=slug,
                poe2db_slug=slug,
                drop_level=1,
                max_prefixes=max_p,
                max_suffixes=max_s,
                tags=("default",),
                has_measured_weights=slug in bundle.measured_slugs,
            )

    if low in bundle.base_name_to_slug:
        slug = bundle.base_name_to_slug[low]
        max_p, max_s = _slot_limits_for_slug(slug)
        return BaseItemEntry(
            id=f"Base/{slug}/{norm_key(raw)}",
            name=raw,
            item_class=slug,
            poe2db_slug=slug,
            drop_level=1,
            max_prefixes=max_p,
            max_suffixes=max_s,
            tags=("default",),
            has_measured_weights=slug in bundle.measured_slugs,
        )

    for suffix, slug in COMMON_BASE_SUFFIX_TO_SLUG:
        if low == suffix or low.endswith(f" {suffix}") or suffix in low:
            max_p, max_s = _slot_limits_for_slug(slug)
            return BaseItemEntry(
                id=f"Base/{slug}/{norm_key(raw)}",
                name=raw,
                item_class=slug,
                poe2db_slug=slug,
                drop_level=1,
                max_prefixes=max_p,
                max_suffixes=max_s,
                tags=("default",),
                has_measured_weights=slug in bundle.measured_slugs,
            )

    raise ValueError(f"Could not resolve PoE2 0.5.5 base item or class slug: {base_name_or_slug!r}")
