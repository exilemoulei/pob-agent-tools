"""Inspect a rare item's affix occupancy and compatible crafting-bench options.

The matcher follows pob-mcp's strict-then-wording approach, using this CLI's
versioned PoB mod index and item parser. Unknown/ambiguous lines never imply a
free affix slot.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import bench, game_data
from .trade_item import Item, Mod, normalize_template, parse_item_text

_NUM = re.compile(r"([+-]?)\((-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\)|([+-]?\d+(?:\.\d+)?)")
_CAPACITY_MOD = re.compile(r"([+-]\d+)\s+(Prefix|Suffix) Modifiers? allowed", re.IGNORECASE)
_MAGNITUDE_MOD = re.compile(r"\d+% increased (Prefix|Suffix) Modifier magnitudes", re.IGNORECASE)
_DEFAULT_WANTS = ("maximum Life", "Fire Resistance", "Cold Resistance", "Lightning Resistance", "Chaos Resistance")


def _spans(line: str) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    for match in _NUM.finditer(line):
        if match.group(4) is not None:
            value = float(match.group(4))
            spans.append((value, value))
        else:
            sign = -1 if match.group(1) == "-" else 1
            a, b = sign * float(match.group(2)), sign * float(match.group(3))
            spans.append((min(a, b), max(a, b)))
    return spans


def _fits(rolled: str, data_line: str, *, strict: bool) -> bool:
    if normalize_template(rolled) != normalize_template(data_line):
        return False
    if not strict:
        return True
    actual, tier = _spans(rolled), _spans(data_line)
    return len(actual) == len(tier) and all(
        lo >= t_lo - 1e-6 and hi <= t_hi + 1e-6
        for (lo, hi), (t_lo, t_hi) in zip(actual, tier, strict=False)
    )


def _item_class(raw: str) -> str:
    for line in raw.splitlines()[:3]:
        if line.lower().startswith("item class:"):
            return line.split(":", 1)[1].strip()
    return ""


def _infer_slot(item_class: str) -> str | None:
    value = item_class.strip().lower()
    names = {
        "rings": "Ring 1", "amulets": "Amulet", "belts": "Belt",
        "body armours": "Body Armour", "helmets": "Helmet",
        "gloves": "Gloves", "boots": "Boots", "shields": "Shield",
        "quivers": "Quiver", "bows": "Bow", "wands": "Wand",
    }
    return names.get(value)


def _affix_type(entry: dict[str, Any]) -> str | None:
    raw = str(entry.get("generation_type") or "").lower()
    if "prefix" in raw:
        return "prefix"
    if "suffix" in raw:
        return "suffix"
    return None


def _base_affix_capacity(
    rarity: str, base_tags: frozenset[str], item_class: str = "", slot: str | None = None
) -> dict[str, int]:
    if rarity == "MAGIC":
        return {"prefix": 1, "suffix": 1}
    slot_low = (slot or "").strip().lower()
    if (
        "jewel" in base_tags or "abyss_jewel" in base_tags
        or "jewel" in item_class.lower() or "jewel" in slot_low or slot_low.startswith("#")
    ):
        return {"prefix": 2, "suffix": 2}
    return {"prefix": 3, "suffix": 3}


def _base_key(base_type: str) -> str:
    """The index's own fold: a pasted "Maelström Staff" must find the base stored as "maelstrom staff"."""
    return game_data._fold_search_text(base_type)


@lru_cache(maxsize=8)
def _cached_mod_rows(db_path_str: str) -> tuple[tuple[str, str, str, str, dict[str, Any]], ...]:
    conn = sqlite3.connect(game_data._sqlite_ro_uri(Path(db_path_str)), uri=True)
    try:
        rows = conn.execute(
            "SELECT source, weights_json, zero_weights_json, types_json, data_json "
            "FROM mods WHERE source IN ('Explicit', 'Bench')"
        ).fetchall()
    finally:
        conn.close()
    return tuple((s, w, z, t, json.loads(p)) for s, w, z, t, p in rows)


def classify_mod_affix_types(
    lines: list[str], *, pob_dir: Path | None = None, runtime_dir: Path | None = None
) -> dict[str, set[str]]:
    """Global lookup mapping each mod line to its possible affix side(s) (`prefix`/`suffix`)."""
    wanted = {normalize_template(ln) for ln in lines if ln.strip()}
    if not wanted:
        return {}
    db_path = game_data.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    by_tmpl: dict[str, set[str]] = defaultdict(set)
    for _, _, _, _, entry in _cached_mod_rows(str(db_path)):
        kind = _affix_type(entry)
        if not kind:
            continue
        for stat in entry.get("stats") or []:
            tmpl = normalize_template(str(stat))
            if tmpl in wanted:
                by_tmpl[tmpl].add(kind)
    return {ln: set(by_tmpl.get(normalize_template(ln), ())) for ln in lines}


def _bench_slot(
    slot: str | None, base_type: str, *, pob_dir: Path | None, runtime_dir: Path | None
) -> str | None:
    """Narrow equipment weapon slots to the base's actual crafting class."""
    if not slot or bench.slot_key(slot) not in ("weapon1", "weapon2"):
        return slot
    db_path = game_data.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    conn = sqlite3.connect(game_data._sqlite_ro_uri(db_path), uri=True)
    try:
        row = conn.execute(
            "SELECT item_type FROM bases WHERE name_lower=? OR zh_lower=? LIMIT 1",
            (_base_key(base_type), _base_key(base_type)),
        ).fetchone()
    finally:
        conn.close()
    slots = bench.POB_TYPE_TO_SLOTS.get(str(row[0]), ()) if row else ()
    # Unknown classes must not fall back to the union of all weapon recipes.
    return slots[0] if slots else None


def _load_entries(
    slot: str | None, base_type: str, *, pob_dir: Path | None, runtime_dir: Path | None
) -> tuple[list[dict[str, Any]], bool, frozenset[str]]:
    db_path = game_data.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    conn = sqlite3.connect(game_data._sqlite_ro_uri(db_path), uri=True)
    try:
        base_row = conn.execute(
            "SELECT data_json FROM bases WHERE name_lower=? OR zh_lower=? LIMIT 1",
            (_base_key(base_type), _base_key(base_type)),
        ).fetchone()
    finally:
        conn.close()
    base_tags = set(json.loads(base_row[0]).get("tags") or []) if base_row else set()
    slot_spec = game_data._normalize_slot_filter(slot) if slot else None
    out: list[dict[str, Any]] = []
    for source, weights, zeros, types, entry in _cached_mod_rows(str(db_path)):
        if source == "Bench":
            if slot_spec and not game_data._mod_matches_slot(source, weights, zeros, types, slot_spec):
                continue
        elif base_tags:
            positive = json.loads(weights)
            excluded = set(json.loads(zeros))
            specific = any(positive.get(tag, 0) > 0 for tag in base_tags if tag != "default")
            if not specific and not (positive.get("default", 0) > 0 and not (base_tags & excluded)):
                continue
        # Without a resolved base, keep all explicit candidates and report ambiguity.
        if _affix_type(entry):
            out.append(entry)
    return out, bool(base_row), frozenset(base_tags)


_DEFENCE_ATTRS = (("armour", "str"), ("evasion", "dex"), ("energy shield", "int"))
_ATTR_PARTS = {"str", "dex", "int"}


def _base_defence_attrs(base_tags: frozenset[str]) -> set[str]:
    """Attributes of an armour base's defence type, e.g. ``str_int_armour`` -> {str, int}."""
    attrs: set[str] = set()
    for tag in base_tags:
        parts = tag.split("_")
        if len(parts) > 1 and parts[-1] in ("armour", "shield") and set(parts[:-1]) <= _ATTR_PARTS:
            attrs.update(parts[:-1])
    return attrs


def _defence_craft_fits_base(recipe: Any, base_tags: frozenset[str]) -> bool:
    """Local defence crafts need the matching defence on the base (no Armour craft on ES bases).

    Bench data only restricts by slot, so derive the necessary condition from the
    base's ``*_armour``/``*_shield`` tags. Unknown bases are not filtered here;
    occupancy is already uncertain for them.
    """
    if not base_tags or not str(getattr(recipe, "id", "")).startswith("Master_Local"):
        return True
    needed = {
        attr for line in getattr(recipe, "lines", ()) or (getattr(recipe, "text", ""),)
        for word, attr in _DEFENCE_ATTRS if word in str(line).lower()
    }
    return needed <= _base_defence_attrs(base_tags)


def _find_attempt(
    entry: dict[str, Any], mods: list[Mod], claimed: set[int], strict: bool
) -> list[int] | None:
    used: list[int] = []
    for data_line in entry.get("stats") or []:
        source = "Bench" if entry.get("source") == "Bench" else "Explicit"
        found = next(
            (
                i for i, mod in enumerate(mods)
                if i not in claimed and i not in used
                and (mod.kind == "crafted") == (source == "Bench")
                and _fits(mod.raw, str(data_line), strict=strict)
            ),
            None,
        )
        if found is None:
            return None
        used.append(found)
    return used


def _classify(
    mods: list[Mod], entries: list[dict[str, Any]], caps: dict[str, int] | None = None
) -> dict[str, Any]:
    caps = caps or {"prefix": 3, "suffix": 3}
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        for data_line in entry.get("stats") or []:
            index[normalize_template(str(data_line))].append(entry)

    claimed: set[int] = set()
    affixes: list[dict[str, Any]] = []
    for strict in (True, False):
        for i, mod in enumerate(mods):
            if i in claimed:
                continue
            candidates = index.get(mod.template, [])
            attempts = [
                (entry, used)
                for entry in candidates
                if (used := _find_attempt(entry, mods, claimed, strict)) is not None
                and i in used
            ]
            if not attempts:
                continue
            # A hybrid uses one affix despite printing several lines.
            max_lines = max(len(used) for _, used in attempts)
            attempts = [(e, u) for e, u in attempts if len(u) == max_lines]
            best = min(attempts, key=lambda pair: (int(pair[0].get("level") or 0), str(pair[0].get("id"))))
            entry, used = best
            readings = {(str(e.get("group")), _affix_type(e)) for e, _ in attempts}
            same_group = [e for e, u in attempts if u == used and e.get("group") == entry.get("group")]
            tiers = sorted({int(e.get("level") or 0) for e in same_group})
            claimed.update(used)
            affixes.append({
                "type": _affix_type(entry), "group": entry.get("group"),
                "affix": entry.get("affix"), "source": entry.get("source"),
                "lines": [mods[n].raw for n in used],
                "matchedTier": strict, "tierLevels": tiers if strict else [],
                "ambiguous": len(readings) > 1,
                "alternatives": sorted(f"{kind}:{group}" for group, kind in readings if (group, kind) != (entry.get("group"), _affix_type(entry))),
            })

    unmatched = [mod.raw for i, mod in enumerate(mods) if i not in claimed]
    counts = {kind: sum(a["type"] == kind for a in affixes) for kind in ("prefix", "suffix")}
    certain = not unmatched and not any(a["ambiguous"] for a in affixes) and all(
        counts[kind] <= caps[kind] for kind in counts
    )
    return {
        "affixes": affixes, "unmatched": unmatched, "counts": counts,
        "open": {kind: caps[kind] - counts[kind] if certain else None for kind in counts},
        "capacity": caps,
        "certain": certain,
    }


def inspect_item(
    raw: str, *, slot: str | None = None, wants: tuple[str, ...] = (), limit: int = 8,
    pob_dir: Path | None = None, runtime_dir: Path | None = None,
    include_suggestions: bool = True,
) -> dict[str, Any]:
    item: Item = parse_item_text(raw)
    item_class = _item_class(raw)
    slot = slot or _infer_slot(item_class)
    bench_slot = _bench_slot(slot, item.base_type, pob_dir=pob_dir, runtime_dir=runtime_dir)
    target_mods = [m for m in item.mods if m.kind in ("explicit", "crafted", "fractured", "veiled")]
    unsupported = [m for m in target_mods if m.kind == "veiled"]
    matchable = [m for m in target_mods if m.kind in ("explicit", "crafted", "fractured")]
    entries, base_resolved, base_tags = _load_entries(bench_slot, item.base_type, pob_dir=pob_dir, runtime_dir=runtime_dir)
    capacity = _base_affix_capacity(item.rarity, base_tags, item_class, slot)
    scaled_types: set[str] = set()
    for mod in item.mods:
        if mod.kind == "implicit":
            match = _CAPACITY_MOD.fullmatch(mod.raw)
            if match:
                kind = match.group(2).lower()
                capacity[kind] += int(match.group(1))
            magnitude = _MAGNITUDE_MOD.fullmatch(mod.raw)
            if magnitude:
                scaled_types.add(magnitude.group(1).lower())
    classified = _classify(matchable, entries, capacity)
    for affix in classified["affixes"]:
        if affix["type"] in scaled_types:
            affix["tierLevels"] = []
            affix["matchedTier"] = False
            affix["scaledRoll"] = True
    unmatched = classified["unmatched"] + [m.raw for m in unsupported]
    out_of_range = [
        affix for affix in classified["affixes"]
        if not affix["matchedTier"] and not affix.get("scaledRoll")
    ]
    tier_conflicts = [
        f"{affix['group']} matched only ilvl {min(affix['tierLevels'])}+ tiers above item level {item.item_level}"
        for affix in classified["affixes"]
        if item.item_level and affix["tierLevels"] and min(affix["tierLevels"]) > item.item_level
    ] + [
        f"{affix['group']} value fits no single tier ({' / '.join(affix['lines'])}); "
        "it may combine several affixes"
        for affix in out_of_range
    ]
    if unsupported or item.unidentified or item.rarity != "RARE" or not base_resolved or tier_conflicts:
        classified["certain"] = False
        classified["open"] = {"prefix": None, "suffix": None}
    classified["unmatched"] = unmatched

    has_crafted = any(m.kind == "crafted" for m in target_mods)
    natural_groups = {str(a["group"]) for a in classified["affixes"] if a["source"] != "Bench"}
    existing_templates = [m.template for m in target_mods if m.kind != "crafted"]
    suggestions: list[dict[str, Any]] = []
    if include_suggestions and bench_slot and item.rarity == "RARE" and not (item.corrupted or item.mirrored or item.unidentified):
        for want in wants or _DEFAULT_WANTS:
            candidates = bench.search(bench_slot, want, limit=15, allow_engine=True)
            best_score = candidates[0].score if candidates else 0
            for found in candidates:
                if found.score < max(0.85, best_score - 0.015):
                    break
                recipe = found.recipe
                if recipe.group in natural_groups:
                    continue
                if item.item_level and recipe.level > item.item_level:
                    continue
                if not _defence_craft_fits_base(recipe, base_tags):
                    continue
                allowed, _ = bench.can_craft_on_item(recipe, existing_templates, has_crafted_mod=has_crafted)
                if not allowed:
                    continue
                open_count = classified["open"].get(recipe.affix)
                removed_count = sum(
                    a["source"] == "Bench" and a["type"] == recipe.affix
                    for a in classified["affixes"]
                )
                if open_count is not None and open_count + removed_count <= 0:
                    continue
                if any(s["group"] == recipe.group for s in suggestions):
                    continue
                suggestions.append({
                    "want": want, "id": recipe.id, "text": recipe.text,
                    "textZh": recipe.text_zh, "group": recipe.group,
                    "affix": recipe.affix, "cost": recipe.cost,
                    "status": (
                        "verify_item" if not classified["certain"] or not item.item_level else
                        "replace_craft" if has_crafted else
                        "open" if open_count is not None and open_count > 0 else "verify_item"
                    ),
                })
                break
            if len(suggestions) >= limit:
                break

    return {
        "item": {"name": item.display_name, "rarity": item.rarity, "itemLevel": item.item_level or None,
                 "itemClass": item_class or None, "slot": slot, "corrupted": item.corrupted,
                 "mirrored": item.mirrored, "unidentified": item.unidentified},
        "affixes": classified["affixes"], "unmatched": unmatched,
        "counts": classified["counts"], "open": classified["open"],
        "capacity": capacity,
        "benchSlot": bench_slot,
        "scaledAffixTypes": sorted(scaled_types),
        "certain": classified["certain"], "baseResolved": base_resolved,
        "dataWarnings": tier_conflicts,
        "hasCraftedMod": has_crafted,
        "craftEligible": item.rarity == "RARE" and not (item.corrupted or item.mirrored or item.unidentified),
        "suggestions": suggestions[:limit],
    }


def format_inspection(report: dict[str, Any]) -> str:
    item = report["item"]
    lines = [f"### {item['name']}", f"Slot: {item['slot'] or 'unknown'} | Rarity: {item['rarity']} | Item level: {item['itemLevel'] or 'unknown'}", ""]
    for affix in report["affixes"]:
        tier = f"; ilvl tier(s): {', '.join(map(str, affix['tierLevels']))}" if affix["tierLevels"] else "; tier unknown"
        if affix.get("scaledRoll"):
            tier = "; tier unknown (implicit scales modifier values)"
        lines.append(f"- **{affix['type']}** {affix['group']} ({affix['affix']}){tier}: {' / '.join(affix['lines'])}")
        if affix["ambiguous"]:
            lines.append(f"  - Ambiguous: {', '.join(affix['alternatives'])}")
    if report["unmatched"]:
        lines.append("- **Unmatched:** " + "; ".join(report["unmatched"]))
    for warning in report["dataWarnings"]:
        lines.append(f"- **Data warning:** {warning}")
    lines.append("")
    if report["certain"]:
        lines.append(f"Open affixes: {report['open']['prefix']} prefix / {report['open']['suffix']} suffix (capacity {report['capacity']['prefix']}/{report['capacity']['suffix']})")
    else:
        lines.append(f"Matched: {report['counts']['prefix']} prefix / {report['counts']['suffix']} suffix; open affixes unknown (item/base data incomplete or ambiguous).")
    if report["suggestions"]:
        lines.append("\n### Bench options")
        for s in report["suggestions"]:
            status = {"open": "open slot", "replace_craft": "replace existing craft", "verify_item": "verify item/slot"}[s["status"]]
            lines.append(f"- [{s['affix']}; {status}] {s['textZh'] or s['text']} — {s['cost']} (target: {s['want']})")
    else:
        lines.append("\nNo compatible bench options found for these targets.")
    if not report["craftEligible"]:
        lines.append("\nThis item cannot be bench-crafted in its current state (rarity or item state).")
    return "\n".join(lines)
