"""PoB Lua parsers for item bases, uniques (including Special/*.lua), and mods."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .constants import (
    LUA_SKIP_KEYS_MOD,
    MOD_FILES,
    MOD_TAG_PREFIX_RE,
    UNIQUE_META_KEYS,
)
from .lua_parser import (
    MOD_TRADEHASH_STRIP_RE,
    _as_seq,
    _parse_lua_assignments,
    _parse_lua_return,
)
from .parsers_special import _extract_special_lua_blocks
from .parsers_vestigial import (
    VESTIGIAL_ITEM_TYPES,
    _load_vestigial_mods,
    _pob_title_key,
    _vestigial_donor_map_from_mods,
)
from .zh_dict import _ZhDictionary

# Lines PoB's unique data may put between the title and the base type.
_UNIQUE_HEADER_BEFORE_BASE: tuple[str, ...] = ("Source:", "League:", "Variant:")
_UNIQUE_INFLUENCE_LINES: frozenset[str] = frozenset({"Shaper Item", "Elder Item", "Fractured Item", "Synthesised Item"})


def _parse_bases(poe1_dir: Path, zh: _ZhDictionary) -> list[dict[str, Any]]:
    bases_dir = poe1_dir / "Data" / "Bases"
    records: list[dict[str, Any]] = []
    if not bases_dir.is_dir():
        return records

    for p in sorted(bases_dir.glob("*.lua")):
        raw_bases = _parse_lua_assignments(p, "itemBases")
        for name, b in raw_bases.items():
            if not isinstance(b, dict):
                continue
            tags_raw = b.get("tags") if isinstance(b.get("tags"), dict) else {}
            tags = sorted(str(k) for k, v in tags_raw.items() if v)
            influence_raw = b.get("influenceTags") if isinstance(b.get("influenceTags"), dict) else {}
            influence_tags = sorted({str(v) for v in influence_raw.values() if isinstance(v, str) and v})
            req_raw = b.get("req") if isinstance(b.get("req"), dict) else {}
            implicit_raw = b.get("implicit")
            implicits = [s.strip() for s in str(implicit_raw).splitlines() if s.strip()] if implicit_raw else []
            implicits_zh = [zh.translate_stat_line(line) for line in implicits] if zh.available else []

            props: dict[str, Any] = {}
            for prop_key in ("weapon", "armour", "flask", "tincture"):
                if isinstance(b.get(prop_key), dict):
                    props[prop_key] = dict(b[prop_key])

            records.append(
                {
                    "name": name,
                    "zh_name": zh.translate_base(name),
                    "type": str(b.get("type") or ""),
                    "sub_type": str(b.get("subType") or "") or None,
                    "socket_limit": b.get("socketLimit"),
                    "hidden": bool(b.get("hidden")),
                    "tags": tags,
                    "influence_tags": influence_tags,
                    "req": {
                        "level": int(req_raw.get("level") or 0),
                        "str": int(req_raw.get("str") or 0),
                        "dex": int(req_raw.get("dex") or 0),
                        "int": int(req_raw.get("int") or 0),
                    },
                    "implicits": implicits,
                    "implicits_zh": implicits_zh,
                    "enchant": b.get("enchant"),
                    "properties": props,
                }
            )
    return records


def _parse_uniques(
    poe1_dir: Path,
    bases_by_name: dict[str, dict[str, Any]],
    foulborn_mods_by_id: dict[str, dict[str, Any]],
    zh: _ZhDictionary,
    *,
    vestigial_by_donor: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    data_dir = poe1_dir / "Data"
    uniques_dir = data_dir / "Uniques"
    if not uniques_dir.is_dir():
        return []
    if vestigial_by_donor is None:
        vestigial_by_donor = _vestigial_donor_map_from_mods(_load_vestigial_mods(data_dir, zh))

    foulborn_lua_map: dict[str, list[str]] = {}
    fb_lua_path = data_dir / "ModFoulbornMap.lua"
    if fb_lua_path.is_file():
        parsed = _parse_lua_return(fb_lua_path)
        if isinstance(parsed, dict):
            for u_name, lines in parsed.items():
                foulborn_lua_map[str(u_name)] = [str(x) for x in _as_seq(lines) if x]

    foulborn_json_map: dict[str, dict[str, str]] = {}
    fb_json_path = data_dir / "ModFoulbornMap.jsonc"
    if fb_json_path.is_file():
        txt = fb_json_path.read_text(encoding="utf-8", errors="ignore")
        clean = re.sub(r"^\s*//.*$", "", txt, flags=re.M)
        try:
            raw_map = json.loads(clean)
            if isinstance(raw_map, dict):
                for u_name, mod_pair in raw_map.items():
                    if isinstance(mod_pair, dict):
                        foulborn_json_map[str(u_name)] = {str(k): str(v) for k, v in mod_pair.items()}
        except json.JSONDecodeError:
            pass

    raw_blocks: list[tuple[str, str]] = []
    for p in sorted(uniques_dir.glob("*.lua")):
        blocks = _parse_lua_return(p)
        for block in _as_seq(blocks):
            if isinstance(block, str):
                raw_blocks.append((p.stem, block))

    raw_blocks.extend(_extract_special_lua_blocks(uniques_dir / "Special", data_dir))

    records: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for fallback_slot, block in raw_blocks:
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        while lines and lines[0].startswith(("Item Class:", "Rarity:")):
            lines = lines[1:]
        if len(lines) < 2:
            continue
        name = lines[0]
        if name in seen_names:
            continue
        seen_names.add(name)
        # The base type is the first line after the title that is not a header (Gorgon's Gaze has
        # its Source line there); the headers move behind it, where the loop below reads them.
        head = 1
        while head < len(lines) - 1 and (
            lines[head].startswith(_UNIQUE_HEADER_BEFORE_BASE) or lines[head] in _UNIQUE_INFLUENCE_LINES
        ):
            head += 1
        lines = [name, lines[head], *lines[1:head], *lines[head + 1 :]]
        base_type = MOD_TAG_PREFIX_RE.sub("", lines[1]).strip() or lines[1]

        variants: list[str] = []
        league: str | None = None
        source: str | None = None
        req_level: int | None = None
        implicits_count = 0
        idx = 2

        while idx < len(lines):
            ln = lines[idx]
            if ln.startswith("Implicits:"):
                try:
                    implicits_count = int(ln.split(":", 1)[1].strip())
                except ValueError:
                    implicits_count = 0
                idx += 1
                break
            if ln.startswith("Variant:"):
                variants.append(ln.split(":", 1)[1].strip())
                idx += 1
                continue
            if ln.startswith("League:"):
                league = ln.split(":", 1)[1].strip()
                idx += 1
                continue
            if ln.startswith("Source:"):
                source = ln.split(":", 1)[1].strip()
                idx += 1
                continue
            if ln.startswith("Requirements:"):
                idx += 1
                continue
            if ln.startswith(("Requires Level ", "LevelReq:", "Level:")):
                m_lvl = re.search(r"(\d+)", ln)
                if m_lvl:
                    req_level = int(m_lvl.group(1))
                idx += 1
                continue
            if ln.startswith("{variant:") and idx < 10 and MOD_TAG_PREFIX_RE.sub("", ln).strip() in bases_by_name:
                idx += 1
                continue
            if ln in _UNIQUE_INFLUENCE_LINES:
                idx += 1
                continue
            if ":" in ln and not ln.startswith("{") and ln.split(":", 1)[0].strip() in UNIQUE_META_KEYS:
                idx += 1
                continue
            break

        implicits = lines[idx : idx + implicits_count] if implicits_count > 0 else []
        explicits = lines[idx + implicits_count :]

        implicits_zh = [zh.translate_stat_line(ln) for ln in implicits] if zh.available else []
        explicits_zh = [zh.translate_stat_line(ln) for ln in explicits] if zh.available else []

        base_info = bases_by_name.get(base_type)
        item_class = base_info["type"] if base_info else fallback_slot
        base_tags = base_info["tags"] if base_info else [fallback_slot]

        foulborn_mods: list[dict[str, Any]] = []
        seen_fb_lines: set[str] = set()
        for orig_id, fb_id in foulborn_json_map.get(name, {}).items():
            fb_mod = foulborn_mods_by_id.get(fb_id)
            if fb_mod:
                for line in fb_mod["stats"]:
                    seen_fb_lines.add(line)
                foulborn_mods.append(
                    {
                        "mod_id": fb_id,
                        "replaces_mod_id": orig_id,
                        "stats": fb_mod["stats"],
                        "stats_zh": fb_mod["stats_zh"],
                    }
                )
        for fb_line in foulborn_lua_map.get(name, []):
            if fb_line not in seen_fb_lines:
                foulborn_mods.append(
                    {
                        "mod_id": None,
                        "replaces_mod_id": None,
                        "stats": [fb_line],
                        "stats_zh": [zh.translate_stat_line(fb_line)] if zh.available else [],
                    }
                )

        zh_name = zh.translate_unique(name)
        foulborn_en = f"Foulborn {name}"
        zh_foulborn = zh.translate_unique(foulborn_en)
        donor_mods = list(vestigial_by_donor.get(_pob_title_key(name)) or ())
        donor_mods_zh = [zh.translate_stat_line(ln) for ln in donor_mods] if (zh.available and donor_mods) else []

        records.append(
            {
                "name": name,
                "zh_name": zh_name,
                "foulborn_name": foulborn_en if (foulborn_mods or zh_foulborn) else None,
                "zh_foulborn_name": zh_foulborn,
                "base_type": base_type,
                "base_type_zh": zh.translate_base(base_type),
                "item_class": item_class,
                "tags": base_tags,
                "req_level": req_level,
                "league": league,
                "source": source,
                "variants": variants,
                "implicits": implicits,
                "implicits_zh": implicits_zh,
                "explicits": explicits,
                "explicits_zh": explicits_zh,
                "foulborn_mods": foulborn_mods,
                "vestigial_donor_mods": donor_mods,
                "vestigial_donor_mods_zh": donor_mods_zh,
                # PoB offers the Vestigial source only to a core-pool unique (no `Source:` line) of
                # these item types; Stasis Prison is its one named exception.
                "takes_vestigial": str(item_class).lower() in VESTIGIAL_ITEM_TYPES
                and (not source or name == "Stasis Prison"),
            }
        )
    return records


def _incursion_stem(affix: str) -> str:
    return re.sub(r"(?:^of\s+|'s$)", "", affix.strip().lower()).strip()


def _load_drop_and_essence_slot_maps(
    data_dir: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    drop_slots: dict[str, list[str]] = {}
    essence_slots_set: dict[str, set[str]] = {}
    origins: dict[str, str] = {}

    for fname, origin_lbl in (("DelveDropOnly.lua", "Delve"), ("IncursionDropOnly.lua", "Incursion")):
        fpath = data_dir / fname
        if not fpath.is_file():
            continue
        parsed = _parse_lua_return(fpath)
        if not isinstance(parsed, dict):
            continue
        for mid, entry in parsed.items():
            if not isinstance(mid, str) or not isinstance(entry, dict):
                continue
            cats = sorted({str(c).strip() for c in _as_seq(entry.get("categories")) if isinstance(c, str) and c.strip()})
            if cats:
                drop_slots[mid] = cats
            origins[mid] = origin_lbl

    ess_path = data_dir / "Essence.lua"
    if ess_path.is_file():
        parsed_ess = _parse_lua_return(ess_path)
        if isinstance(parsed_ess, dict):
            for entry in parsed_ess.values():
                if not isinstance(entry, dict):
                    continue
                mods_map = entry.get("mods")
                if not isinstance(mods_map, dict):
                    continue
                for slot_cat, mid in mods_map.items():
                    if isinstance(slot_cat, str) and isinstance(mid, str) and slot_cat.strip() and mid.strip():
                        essence_slots_set.setdefault(mid.strip(), set()).add(slot_cat.strip())

    essence_slots = {mid: sorted(cats) for mid, cats in essence_slots_set.items()}
    return drop_slots, essence_slots, origins


def _infer_drop_origin(
    mod_id: str,
    source_name: str,
    affix: str,
    mod_tags: list[str],
    origins: dict[str, str],
    incursion_stems: set[str],
    in_essence: bool,
) -> str | None:
    if mod_id in origins:
        return origins[mod_id]
    mid_low = mod_id.lower()
    aff_low = affix.lower()
    stem = _incursion_stem(affix)
    if source_name == "Delve" or "delve" in mid_low or aff_low in ("subterranean", "of the underground"):
        return "Delve"
    if "enhancedmod" in mid_low or "enhancedlevel" in mid_low or (stem and stem in incursion_stems):
        return "Incursion"
    if in_essence or "essence" in mid_low or aff_low in ("essences", "of the essence"):
        return "Essence"
    if source_name == "Veiled" or "unveiled_mod" in mod_tags:
        return "Veiled"
    return None


def _parse_mods(poe1_dir: Path, zh: _ZhDictionary) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    data_dir = poe1_dir / "Data"
    records: list[dict[str, Any]] = []
    foulborn_by_id: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    drop_slots, essence_slots, drop_origins = _load_drop_and_essence_slot_maps(data_dir)
    parsed_files: list[tuple[str, list[tuple[str, dict[Any, Any]]]]] = []
    incursion_stems: set[str] = set()

    for filename, source_name in MOD_FILES:
        path = data_dir / filename
        if not path.is_file():
            continue
        raw_table = _parse_lua_return(
            path,
            skip_keys=LUA_SKIP_KEYS_MOD,
            prestrip_re=MOD_TRADEHASH_STRIP_RE,
            prestrip_repl=" }",
        )

        items: list[tuple[str, dict[Any, Any]]] = []
        if isinstance(raw_table, list):
            for idx, entry in enumerate(raw_table, 1):
                if isinstance(entry, dict):
                    grp = str(entry.get("group") or "Craft")
                    gen = str(entry.get("type") or "Craft")
                    lvl = int(entry.get("level") or 0)
                    items.append((f"Master_{grp}_{gen}_{lvl}_{idx}", entry))
        elif isinstance(raw_table, dict):
            for mod_id, entry in raw_table.items():
                if isinstance(entry, dict):
                    mid_s = str(mod_id)
                    items.append((mid_s, entry))
                    mid_l = mid_s.lower()
                    if drop_origins.get(mid_s) == "Incursion" or "enhancedmod" in mid_l or "enhancedlevel" in mid_l:
                        stem = _incursion_stem(str(entry.get("affix") or ""))
                        if len(stem) >= 4:
                            incursion_stems.add(stem)
        parsed_files.append((source_name, items))

    for source_name, items in parsed_files:
        for mod_id, entry in items:
            if mod_id in seen_ids:
                continue
            stat_lines = [str(s) for s in _as_seq(entry) if isinstance(s, str) and s.strip()]
            if not stat_lines:
                continue
            seen_ids.add(mod_id)
            stats_zh = [zh.translate_stat_line(ln) for ln in stat_lines] if zh.available else []

            raw_type = str(entry.get("type") or "")
            if source_name == "Bench":
                gen_type = f"Bench {raw_type}".strip()
            elif source_name == "Foulborn" and not raw_type:
                gen_type = "Foulborn"
            else:
                gen_type = raw_type or source_name

            wk_list = _as_seq(entry.get("weightKey"))
            wv_list = _as_seq(entry.get("weightVal"))
            positive_weights: dict[str, int] = {}
            zero_weights: list[str] = []
            weight_keys: list[str] = []  # PoB's order: the first key an item carries decides
            for k, v in zip(wk_list, wv_list, strict=False):
                if not isinstance(k, str) or not isinstance(v, (int, float)):
                    continue
                weight_keys.append(k)
                iv = int(v)
                if iv > 0:
                    positive_weights[k] = iv
                else:
                    zero_weights.append(k)

            types_raw = entry.get("types")
            applicable_types = sorted(str(k) for k, v in types_raw.items() if v) if isinstance(types_raw, dict) else []
            mod_tags = [str(t) for t in _as_seq(entry.get("modTags")) if isinstance(t, str)]
            drop_origin: str | None = None
            if not positive_weights and source_name != "Bench":
                drop_origin = _infer_drop_origin(
                    mod_id,
                    source_name,
                    str(entry.get("affix") or ""),
                    mod_tags,
                    drop_origins,
                    incursion_stems,
                    mod_id in essence_slots,
                )
                if not applicable_types:
                    applicable_types = list(drop_slots.get(mod_id) or essence_slots.get(mod_id) or ())

            bench_info: dict[str, Any] | None = None
            if source_name == "Bench":
                bench_info = {
                    "source": "Crafting Bench",
                    "types": applicable_types,
                    "cost": entry.get("cost"),
                }

            rec = {
                "id": mod_id,
                "source": source_name,
                "generation_type": gen_type,
                "affix": str(entry.get("affix") or ""),
                "group": str(entry.get("group") or ""),
                "level": int(entry.get("level") or 0),
                "stats": stat_lines,
                "stats_zh": stats_zh,
                "mod_tags": mod_tags,
                "weights": positive_weights,
                "zero_weights": zero_weights,
                "weight_keys": weight_keys,
                "applicable_types": applicable_types,
                "bench_info": bench_info,
            }
            if drop_origin:
                rec["drop_origin"] = drop_origin
            records.append(rec)
            if source_name == "Foulborn":
                foulborn_by_id[mod_id] = rec

    for vrec in _load_vestigial_mods(data_dir, zh):
        if vrec["id"] not in seen_ids:
            seen_ids.add(vrec["id"])
            records.append(vrec)

    return records, foulborn_by_id
