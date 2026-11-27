"""Tier 1 (Crafting Bench Recraft) and Tier 2 (Harvest Resistance Swap) candidate enumerators (L2)."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .. import bench, item_inspect
from ..trade_item import (
    ELEMENT_LABELS,
    NON_CONVERTIBLE_KINDS,
    match_harvest,
    normalize_template,
    parse_item_text,
)
from .deficits import BuildDeficits, RebalanceStep

CRAFTABLE_GEAR_SLOTS: frozenset[str] = frozenset(
    {"Weapon 1", "Weapon 2", "Body Armour", "Helmet", "Gloves", "Boots", "Amulet", "Ring 1", "Ring 2", "Belt"}
)

# PoE1 single-element explicit resistance tiers (T1..T8) for conservative worst-case Harvest re-roll floor.
_HARVEST_RES_TIERS: tuple[tuple[int, int], ...] = (
    (46, 48),
    (42, 45),
    (36, 41),
    (30, 35),
    (24, 29),
    (18, 23),
    (12, 17),
    (6, 11),
)

_SINGLE_ATTR_RE = re.compile(r"^\+?(\d+)\s+to\s+(Strength|Dexterity|Intelligence)\b", re.IGNORECASE)
_DUAL_ATTR_RE = re.compile(
    r"^\+?(\d+)\s+to\s+(Strength|Dexterity|Intelligence)\s+and\s+(Strength|Dexterity|Intelligence)\b",
    re.IGNORECASE,
)
_ALL_ATTR_RE = re.compile(r"^\+?(\d+)\s+to\s+all\s+Attributes\b", re.IGNORECASE)
_SINGLE_RES_RE = re.compile(r"^\+?(\d+)%\s+to\s+(Fire|Cold|Lightning|Chaos)\s+Resistance\b", re.IGNORECASE)
_DUAL_RES_RE = re.compile(
    r"^\+?(\d+)%\s+to\s+(Fire|Cold|Lightning|Chaos)\s+and\s+(Fire|Cold|Lightning|Chaos)\s+Resistances?\b",
    re.IGNORECASE,
)
_ALL_ELE_RES_RE = re.compile(r"^\+?(\d+)%\s+to\s+all\s+Elemental\s+Resistances\b", re.IGNORECASE)
_SUPPRESS_RE = re.compile(r"^\+?(\d+)%\s+chance\s+to\s+Suppress\s+Spell\s+Damage\b", re.IGNORECASE)
_MOD_TAG_STRIP_RE = re.compile(r"\{[^}]*\}")
_RANGE_TAG_RE = re.compile(r"\{range:([0-9]+(?:\.[0-9]+)?)\}", re.IGNORECASE)
_RANGE_VAL_RE = re.compile(r"\((\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\)")
_COST_NUM_RE = re.compile(r"(\d+)\s*(chaos|exalt|divine|alch|transmut|alterat|augment|regal|scour|vaals?)", re.IGNORECASE)

_ATTR_CANON = {"strength": "Str", "dexterity": "Dex", "intelligence": "Int"}
_RES_CANON = {"fire": "Fire", "cold": "Cold", "lightning": "Lightning", "chaos": "Chaos"}


def harvest_tier_min_roll(value: float) -> int:
    """Return conservative worst-case tier minimum roll for an explicit single-element resistance value."""
    v = math.floor(value)
    for lo, hi in _HARVEST_RES_TIERS:
        if lo <= v <= hi:
            return lo
    if v > 48:
        return 46
    return max(1, v)


def _collapse_ranges_to_min(text: str) -> str:
    return _RANGE_VAL_RE.sub(lambda m: m.group(1), text)


def _resolve_pob_range_line(raw_line: str, *, use_min_roll: bool) -> str:
    """Resolve PoB `{range:t}(lo-hi)` mod lines; defaults to `lo` when `use_min_roll=True` or `{range}` is absent."""
    range_m = _RANGE_TAG_RE.search(raw_line)
    clean = _MOD_TAG_STRIP_RE.sub("", raw_line).strip()
    if not clean:
        return ""
    if use_min_roll or range_m is None:
        return _collapse_ranges_to_min(clean)
    try:
        t = min(1.0, max(0.0, float(range_m.group(1))))
    except ValueError:
        return _collapse_ranges_to_min(clean)

    def _interp(m: re.Match[str]) -> str:
        lo, hi = float(m.group(1)), float(m.group(2))
        val = lo + math.floor((hi - lo) * t + 0.5)
        return str(int(val)) if abs(val - round(val)) < 1e-9 else f"{val:g}"

    return _RANGE_VAL_RE.sub(_interp, clean)


def parse_stat_contributions(text: str, *, use_min_roll: bool = True) -> dict[str, float]:
    """Extract conservative Str/Dex/Int/Fire/Cold/Lightning/Chaos/Suppression contributions from mod line(s)."""
    totals: dict[str, float] = {}
    for raw_ln in str(text or "").splitlines():
        clean = _resolve_pob_range_line(raw_ln, use_min_roll=use_min_roll)
        if not clean:
            continue
        m = _DUAL_ATTR_RE.match(clean)
        if m:
            val = float(m.group(1))
            for idx in (2, 3):
                k = _ATTR_CANON[m.group(idx).lower()]
                totals[k] = totals.get(k, 0.0) + val
            continue
        m = _SINGLE_ATTR_RE.match(clean)
        if m:
            k = _ATTR_CANON[m.group(2).lower()]
            totals[k] = totals.get(k, 0.0) + float(m.group(1))
            continue
        m = _ALL_ATTR_RE.match(clean)
        if m:
            val = float(m.group(1))
            for k in ("Str", "Dex", "Int"):
                totals[k] = totals.get(k, 0.0) + val
            continue
        m = _DUAL_RES_RE.match(clean)
        if m:
            val = float(m.group(1))
            for idx in (2, 3):
                k = _RES_CANON[m.group(idx).lower()]
                totals[k] = totals.get(k, 0.0) + val
            continue
        m = _SINGLE_RES_RE.match(clean)
        if m:
            k = _RES_CANON[m.group(2).lower()]
            totals[k] = totals.get(k, 0.0) + float(m.group(1))
            continue
        m = _ALL_ELE_RES_RE.match(clean)
        if m:
            val = float(m.group(1))
            for k in ("Fire", "Cold", "Lightning"):
                totals[k] = totals.get(k, 0.0) + val
            continue
        m = _SUPPRESS_RE.match(clean)
        if m:
            totals["SpellSuppression"] = totals.get("SpellSuppression", 0.0) + float(m.group(1))
    return totals


def _estimate_bench_cost_chaos(cost_text: str, *, replacing_crafted: bool) -> float:
    base = 0.5 if replacing_crafted else 0.0  # 1 Scouring Orb to clear old craft
    m = _COST_NUM_RE.search(str(cost_text or ""))
    if not m:
        return base + 1.0
    qty = float(m.group(1))
    cur = m.group(2).lower()
    if cur.startswith("divine"):
        return base + qty * 150.0
    if cur.startswith("exalt"):
        return base + qty * 15.0
    if cur.startswith("chaos"):
        return base + qty
    if cur.startswith(("alch", "regal", "scour", "vaal")):
        return base + max(0.5, qty * 0.25)
    return base + max(0.2, qty * 0.1)


def _materialized_bench_line(recipe: bench.BenchRecipe) -> str:
    """Materialize a conservative min-roll PoB `{crafted}...` line from a `BenchRecipe`."""
    raw_line = recipe.lines[0] if recipe.lines else recipe.text
    resolved = _collapse_ranges_to_min(raw_line)
    return resolved if resolved.lower().startswith("{crafted}") else f"{{crafted}}{resolved}"


def _remove_crafted_line(raw_text: str) -> str:
    """Return raw item text with its first `{crafted}` / `(crafted)` mod line removed."""
    out: list[str] = []
    removed = False
    for ln in str(raw_text or "").splitlines():
        low = ln.lower()
        if not removed and ("{crafted}" in low or "(crafted)" in low):
            removed = True
            continue
        out.append(ln)
    return "\n".join(out)


def _crafted_mod_side_from_recipes(crafted_template: str, recipes: list[bench.BenchRecipe]) -> str | None:
    """Identify `'prefix'` or `'suffix'` for an existing `{crafted}` mod template from bench recipes."""
    sides: set[str] = set()
    for rec in recipes:
        if rec.affix not in ("prefix", "suffix"):
            continue
        for ln in rec.lines or (rec.text,):
            if normalize_template(ln) == crafted_template:
                sides.add(rec.affix)
    return next(iter(sides)) if len(sides) == 1 else None


def _inspect_open_affix_sides(
    raw_text: str,
    slot_name: str,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> tuple[bool, bool, bool]:
    """Return `(has_open_prefix, has_open_suffix, inspected)` via `item_inspect`; fail closed `(False, False, False)` offline."""
    try:
        rep = item_inspect.inspect_item(
            raw_text,
            slot=slot_name,
            pob_dir=pob_dir,
            runtime_dir=runtime_dir,
            include_suggestions=False,
        )
        cnt = rep.get("counts") or {}
        cap = rep.get("capacity") or {"prefix": 3, "suffix": 3}
        unmatched_lines = list(rep.get("unmatched") or ())
        p_cur = int(cnt.get("prefix") or 0)
        s_cur = int(cnt.get("suffix") or 0)
        p_cap = int(cap.get("prefix") or 3)
        s_cap = int(cap.get("suffix") or 3)
        still_unknown = 0
        if unmatched_lines:
            try:
                gtypes = item_inspect.classify_mod_affix_types(
                    unmatched_lines, pob_dir=pob_dir, runtime_dir=runtime_dir
                )
            except Exception:
                gtypes = {}
            for ln in unmatched_lines:
                sides = gtypes.get(ln, set())
                if sides == {"prefix"}:
                    p_cur += 1
                elif sides == {"suffix"}:
                    s_cur += 1
                else:
                    still_unknown += 1
        if p_cur + s_cur + still_unknown >= p_cap + s_cap:
            return False, False, True
        return (p_cur + still_unknown) < p_cap, (s_cur + still_unknown) < s_cap, True
    except Exception:
        return False, False, False


def _craft_reduces_net_hole(
    net_covers: dict[str, float],
    deficits: BuildDeficits,
    needs: dict[str, float],
) -> tuple[bool, float]:
    """Verify a bench recraft actually helps active deficits without creating a worse hole on another stat."""
    helpful = 0.0
    for k, gain in net_covers.items():
        if gain > 0 and k in needs:
            helpful += min(gain, needs[k])
    if helpful <= 0.0:
        return False, 0.0

    new_hole = 0.0
    for k, delta in net_covers.items():
        if delta >= 0:
            continue
        loss = -delta
        if k in ("Fire", "Cold", "Lightning"):
            if k == "Lightning" and deficits.has_doryani:
                continue
            slack = deficits.resist_slack(k)
            if loss > slack:
                new_hole += loss - slack
        elif k == "Chaos" and deficits.include_chaos and not deficits.is_chaos_immune:
            cur_chaos = float((deficits.resistances.get("Chaos") or {}).get("current") or 0.0)
            headroom = max(0.0, cur_chaos - deficits.chaos_target)
            if loss > headroom:
                new_hole += loss - headroom
        elif k in ("Str", "Dex", "Int", "Omni"):
            surplus = deficits.attr_surplus(k)
            if loss > surplus:
                new_hole += loss - surplus
    if new_hole >= helpful:
        return False, 0.0
    return True, helpful - new_hole


def enumerate_bench_recraft_steps(
    equipped_items: list[dict[str, Any]],
    deficits: BuildDeficits,
    *,
    allow_engine: bool = True,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    max_per_slot: int = 4,
) -> list[RebalanceStep]:
    """Enumerate Tier 1 (~0-4c) Crafting Bench additions and `{crafted}` replacements addressing deficits."""
    needs = deficits.need_map()
    if not needs:
        return []

    steps: list[RebalanceStep] = []
    for entry in equipped_items:
        slot = str(entry.get("slotName") or entry.get("slot") or "")
        raw = str(entry.get("raw") or "")
        if slot not in CRAFTABLE_GEAR_SLOTS or not raw.strip():
            continue
        item = parse_item_text(raw)
        if item.corrupted or item.mirrored or item.unidentified or item.rarity not in ("RARE", "MAGIC"):
            continue

        crafted_mods = [m for m in item.mods if m.kind == "crafted"]
        non_crafted_templates = [m.template for m in item.mods if m.kind in ("explicit", "fractured")]
        has_crafted = bool(crafted_mods)
        if len(crafted_mods) > 1:
            # Skip multimodded items to avoid breaking complex metamod setups.
            continue

        recipes = bench.recipes_for_slot(slot, allow_engine=allow_engine)
        removed_stats: dict[str, float] = {}
        replace_target: str | None = None
        if has_crafted:
            replace_target = crafted_mods[0].raw
            removed_stats = parse_stat_contributions(replace_target, use_min_roll=False)
            raw_post_remove = _remove_crafted_line(raw)
            open_p, open_s, inspected = _inspect_open_affix_sides(
                raw_post_remove, slot, pob_dir=pob_dir, runtime_dir=runtime_dir
            )
            if not inspected:
                # Offline fallback: only the affix side freed by removing the existing `{crafted}` mod is guaranteed open.
                freed_side = _crafted_mod_side_from_recipes(crafted_mods[0].template, recipes)
                open_p = freed_side == "prefix"
                open_s = freed_side == "suffix"
        else:
            open_p, open_s, _ = _inspect_open_affix_sides(
                raw, slot, pob_dir=pob_dir, runtime_dir=runtime_dir
            )
        if not open_p and not open_s:
            continue

        best_by_sig: dict[tuple[str, ...], tuple[float, float, bench.BenchRecipe, dict[str, float]]] = {}
        for rec in recipes:
            if rec.affix == "prefix" and not open_p:
                continue
            if rec.affix == "suffix" and not open_s:
                continue
            if len(rec.lines) > 1:
                continue
            if item.item_level > 0 and getattr(rec, "level", 0) > item.item_level:
                continue
            cost_c = _estimate_bench_cost_chaos(rec.cost, replacing_crafted=has_crafted)
            if cost_c > 12.0:
                continue
            ok, _ = bench.can_craft_on_item(rec, non_crafted_templates, has_crafted_mod=has_crafted)
            if not ok:
                continue
            gains = parse_stat_contributions(rec.text, use_min_roll=True)
            if not gains:
                continue
            net = dict(gains)
            for rk, rv in removed_stats.items():
                net[rk] = net.get(rk, 0.0) - rv
            net = {k: v for k, v in net.items() if abs(v) > 1e-6}
            is_useful, score = _craft_reduces_net_hole(net, deficits, needs)
            if not is_useful:
                continue
            sig = tuple(sorted(k for k, v in gains.items() if v > 0))
            prev = best_by_sig.get(sig)
            if prev is None or (score, -cost_c) > (prev[0], prev[1]):
                best_by_sig[sig] = (score, -cost_c, rec, net)

        ranked = sorted(best_by_sig.values(), key=lambda x: (-x[0], -x[1]))[:max_per_slot]
        for _, neg_cost, rec, net in ranked:
            cost_c = -neg_cost
            crafted_line = _materialized_bench_line(rec)
            action_lbl = "Recraft" if has_crafted else "Bench Craft"
            apply_spec: dict[str, Any] = {
                "kind": "item_edit",
                "slot": slot,
                "replaceMods": [[replace_target, crafted_line]] if replace_target else [],
                "addMods": [] if replace_target else [crafted_line],
                "craftedLine": crafted_line,
            }
            steps.append(
                RebalanceStep(
                    tier=1,
                    kind="bench_recraft",
                    slot=slot,
                    target_key=f"item:{slot}:bench",
                    title=f"{action_lbl} {slot}: {crafted_line.replace('{crafted}', '')}",
                    est_cost_chaos=cost_c,
                    cost_label=f"~{cost_c:g}c ({rec.cost or 'Crafting Bench'})",
                    covers=net,
                    apply_spec=apply_spec,
                )
            )
    return steps


def enumerate_harvest_swap_steps(
    equipped_items: list[dict[str, Any]],
    deficits: BuildDeficits,
) -> list[RebalanceStep]:
    """Enumerate Tier 2 (~8c / 300 Lifeforce) Harvest single-element resistance swaps with min-roll floor guard."""
    res_needs = {k: v for k, v in deficits.hard_resist_deficits().items() if k in ("Fire", "Cold", "Lightning")}
    if not res_needs:
        return []

    steps: list[RebalanceStep] = []
    for entry in equipped_items:
        slot = str(entry.get("slotName") or entry.get("slot") or "")
        raw = str(entry.get("raw") or "")
        if slot not in CRAFTABLE_GEAR_SLOTS or not raw.strip():
            continue
        item = parse_item_text(raw)
        if item.corrupted or item.mirrored or item.unidentified or item.rarity not in ("RARE", "MAGIC"):
            continue

        locked_elements: set[str] = set()
        for mod in item.mods:
            hm = match_harvest(mod.template)
            if hm is not None and hm.group.key == "resistance" and mod.kind in ("explicit", "fractured", "crafted"):
                locked_elements.add(hm.element)

        for mod in item.mods:
            if mod.kind in NON_CONVERTIBLE_KINDS:
                continue
            hm = match_harvest(mod.template)
            if hm is None or hm.group.key != "resistance":
                continue
            src_canon = ELEMENT_LABELS[hm.element]
            cur_val = parse_stat_contributions(mod.raw, use_min_roll=False).get(src_canon, 0.0)
            if cur_val <= 0.0 and mod.values:
                cur_val = float(mod.values[0])
            if cur_val <= 0.0:
                continue
            src_slack = deficits.resist_slack(src_canon)
            # Require positive surplus on the source element so we don't trade one hole for a bigger hole.
            if src_slack < min(cur_val, 12.0):
                continue

            min_roll = harvest_tier_min_roll(cur_val)
            donor_gap_created = max(0.0, cur_val - src_slack)
            for dst_canon, dst_need in res_needs.items():
                dst_elem = dst_canon.lower()
                if dst_elem == hm.element or dst_elem in locked_elements or dst_need <= 0.0:
                    continue
                if min(dst_need, float(min_roll)) <= donor_gap_created + 1e-6:
                    continue
                new_line = f"+{min_roll}% to {dst_canon} Resistance"
                covers = {src_canon: -cur_val, dst_canon: float(min_roll)}
                warn = (
                    f"Harvest re-rolls inside the same tier; modeled at conservative tier floor +{min_roll}% "
                    f"(actual roll could be up to tier max)."
                )
                steps.append(
                    RebalanceStep(
                        tier=2,
                        kind="harvest_swap",
                        slot=slot,
                        target_key=f"item:{slot}:harvest:{mod.line_index}",
                        title=f"Harvest Swap {slot}: {src_canon} ({cur_val:g}%) -> {dst_canon} (floor +{min_roll}%)",
                        est_cost_chaos=8.0,
                        cost_label="~8c (300 Lifeforce)",
                        covers=covers,
                        apply_spec={
                            "kind": "item_edit",
                            "slot": slot,
                            "replaceMods": [[mod.raw, new_line]],
                            "addMods": [],
                            "harvestFrom": src_canon,
                            "harvestTo": dst_canon,
                            "tierFloor": min_roll,
                        },
                        warnings=(warn,),
                    )
                )
    return steps
