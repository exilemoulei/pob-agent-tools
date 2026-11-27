"""Tier 5 single-slot trade fallback and PoB delta/ranking helpers for `pob rebalance` (L2)."""

from __future__ import annotations

import math
from typing import Any

from ..formatters import _parse_numeric_or_none
from ..trade_item import parse_item_text
from .deficits import BuildDeficits
from .steps_craft_harvest import parse_stat_contributions

_RARE_TRADE_PRIORITY: tuple[str, ...] = (
    "Ring 1",
    "Ring 2",
    "Belt",
    "Boots",
    "Gloves",
    "Helmet",
    "Amulet",
    "Body Armour",
)
_TRADE_STAT_LABELS: dict[str, str] = {
    "Str": "Strength",
    "Dex": "Dexterity",
    "Int": "Intelligence",
    "Fire": "Fire Resistance",
    "Cold": "Cold Resistance",
    "Lightning": "Lightning Resistance",
    "Chaos": "Chaos Resistance",
    "SpellSuppression": "Chance to Suppress Spell Damage",
}


def compute_stat_delta(
    before_stats: dict[str, Any],
    after_stats: dict[str, Any],
    key: str,
) -> tuple[float | None, float | None]:
    """Compute `(delta, delta_pct)` from raw stat dicts; returns `(None, None)` when unavailable or non-finite."""
    raw_b = before_stats.get(key)
    raw_a = after_stats.get(key)
    if raw_b is None or raw_a is None:
        return None, None
    bf = _parse_numeric_or_none(raw_b)
    af = _parse_numeric_or_none(raw_a)
    if bf is None or af is None or not (math.isfinite(bf) and math.isfinite(af)):
        return None, None
    diff = af - bf
    if abs(diff) < 1e-6:
        return 0.0, 0.0
    pct = ((diff / abs(bf)) * 100.0) if abs(bf) > 1e-6 else None
    return round(diff, 2), (round(pct, 2) if pct is not None else None)


def pct_rank_key(val: Any) -> float:
    """Safe descending rank key for optional percentage deltas (maps `None`/non-finite to `-inf`)."""
    if val is None or isinstance(val, bool):
        return -math.inf
    try:
        f = float(val)
        return f if math.isfinite(f) else -math.inf
    except (TypeError, ValueError):
        return -math.inf


def build_trade_fallback(
    equipped_raw: list[dict[str, Any]],
    deficits: BuildDeficits,
) -> dict[str, Any] | None:
    """Tier 5 fallback from the consistent base state: pick lowest-burden rare slot and emit `pob trade search`."""
    rem = {k: v for k, v in deficits.need_map().items() if v > 0}
    if not rem:
        return None

    rare_candidates: list[tuple[int, int, str, dict[str, float], str]] = []
    for entry in equipped_raw:
        slot = str(entry.get("slotName") or "")
        raw = str(entry.get("raw") or "")
        if slot not in _RARE_TRADE_PRIORITY or not raw.strip():
            continue
        item = parse_item_text(raw)
        if item.rarity != "RARE":
            continue
        contrib = parse_stat_contributions(raw, use_min_roll=False)
        special_mods = sum(
            1
            for m in item.mods
            if m.kind in ("explicit", "fractured")
            and m.category not in ("resist", "attributes", "life", "energy_shield", "armour", "evasion", "other")
        )
        prio_idx = _RARE_TRADE_PRIORITY.index(slot)
        rare_candidates.append((special_mods, prio_idx, slot, contrib, item.display_name))

    if not rare_candidates:
        return None
    rare_candidates.sort(key=lambda x: (x[0], x[1]))
    _, _, best_slot, slot_contrib, item_disp = rare_candidates[0]

    load_bearing: dict[str, float] = {}
    for k, val in slot_contrib.items():
        if val <= 0:
            continue
        if k in ("Fire", "Cold", "Lightning"):
            if k == "Lightning" and deficits.has_doryani:
                continue
            lb = max(0.0, val - deficits.resist_slack(k))
            if lb > 0:
                load_bearing[k] = math.ceil(lb)
        elif k in ("Str", "Dex", "Int"):
            lb = max(0.0, val - deficits.attr_surplus(k))
            if lb > 0:
                load_bearing[k] = math.ceil(lb)
        elif k == "Chaos" and deficits.include_chaos and not deficits.is_chaos_immune:
            cur_chaos = float((deficits.resistances.get("Chaos") or {}).get("current") or 0.0)
            headroom = max(0.0, cur_chaos - deficits.chaos_target)
            lb = max(0.0, val - headroom)
            if lb > 0:
                load_bearing["Chaos"] = math.ceil(lb)
        elif k == "SpellSuppression" and deficits.include_suppression:
            supp_info = deficits.optional.get("spellSuppression") or {}
            cur_supp = float(supp_info.get("currentPct") or 0.0)
            surplus = max(0.0, cur_supp - 100.0)
            lb = max(0.0, val - surplus)
            if lb > 0:
                load_bearing["SpellSuppression"] = math.ceil(lb)

    target_stats: dict[str, int] = {}
    for k in sorted(set(load_bearing) | set(rem)):
        if k not in _TRADE_STAT_LABELS:
            continue
        tot = math.ceil(load_bearing.get(k, 0.0) + rem.get(k, 0.0))
        if tot > 0:
            target_stats[k] = tot

    stat_flags = " ".join(f'--stat "{_TRADE_STAT_LABELS[k]}={v}"' for k, v in target_stats.items())
    cmd = f'pob trade search --slot "{best_slot}" {stat_flags}'.strip()
    return {
        "slot": best_slot,
        "currentItem": item_disp,
        "loadBearingStats": load_bearing,
        "remainingDeficits": {k: round(v, 1) for k, v in rem.items()},
        "targetStats": target_stats,
        "command": cmd,
    }
