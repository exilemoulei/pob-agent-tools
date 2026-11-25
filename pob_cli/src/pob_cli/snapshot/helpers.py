"""Stat parsing, core stat catalog, and batch RPC helpers for `pob_cli.snapshot`."""

from __future__ import annotations

import math
from typing import Any

from ..formatters import _parse_numeric_or_none, fmt_num

CORE_STAT_KEYS: list[tuple[str, str, str]] = [
    ("FullDPS", "Full DPS", ""),
    ("CombinedDPS", "Combined DPS", ""),
    ("TotalDPS", "Hit DPS", ""),
    ("TotalDotDPS", "DoT DPS", ""),
    ("MinionCombinedDPS", "Minion Combined DPS", ""),
    ("MinionTotalDPS", "Minion Hit DPS", ""),
    ("AverageDamage", "Average Hit", ""),
    ("Speed", "Attack/Cast Rate", "/s"),
    ("CritChance", "Crit Chance", "%"),
    ("CritMultiplier", "Crit Multiplier", "x"),
    ("HitChance", "Hit Chance", "%"),
    ("ManaCost", "Mana Cost", ""),
    ("TotalEHP", "Effective HP (TotalEHP)", ""),
    ("Life", "Maximum Life", ""),
    ("LifeUnreserved", "Unreserved Life", ""),
    ("Mana", "Maximum Mana", ""),
    ("ManaUnreserved", "Unreserved Mana", ""),
    ("EnergyShield", "Energy Shield", ""),
    ("Ward", "Ward", ""),
    ("Armour", "Armour", ""),
    ("Evasion", "Evasion", ""),
    ("PhysicalMaximumHitTaken", "Phys Max Hit", ""),
    ("FireMaximumHitTaken", "Fire Max Hit", ""),
    ("ColdMaximumHitTaken", "Cold Max Hit", ""),
    ("LightningMaximumHitTaken", "Lightning Max Hit", ""),
    ("ChaosMaximumHitTaken", "Chaos Max Hit", ""),
    ("FireResist", "Fire Resist", "%"),
    ("ColdResist", "Cold Resist", "%"),
    ("LightningResist", "Lightning Resist", "%"),
    ("ChaosResist", "Chaos Resist", "%"),
    ("EffectiveSpellSuppressionChance", "Spell Suppression", "%"),
    ("EffectiveBlockChance", "Block Chance", "%"),
    ("EffectiveSpellBlockChance", "Spell Block", "%"),
    ("LifeRegenRecovery", "Net Life Regen", "/s"),
    ("ManaRegenRecovery", "Net Mana Regen", "/s"),
    ("Str", "Strength", ""),
    ("Dex", "Dexterity", ""),
    ("Int", "Intelligence", ""),
]

_OFFENCE_SUMMARY_KEYS = (
    "FullDPS",
    "CombinedDPS",
    "TotalDPS",
    "TotalDotDPS",
    "MinionCombinedDPS",
    "MinionTotalDPS",
    "AverageDamage",
    "Speed",
    "CritChance",
    "CritMultiplier",
    "HitChance",
    "ManaCost",
)
_DEFENCE_SUMMARY_KEYS = (
    "TotalEHP",
    "Life",
    "LifeUnreserved",
    "Mana",
    "ManaUnreserved",
    "EnergyShield",
    "Ward",
    "Armour",
    "Evasion",
    "PhysicalMaximumHitTaken",
    "FireMaximumHitTaken",
    "ColdMaximumHitTaken",
    "LightningMaximumHitTaken",
    "ChaosMaximumHitTaken",
    "FireResist",
    "ColdResist",
    "LightningResist",
    "ChaosResist",
    "EffectiveSpellSuppressionChance",
    "EffectiveBlockChance",
    "EffectiveSpellBlockChance",
    "LifeRegenRecovery",
    "ManaRegenRecovery",
)


def _call_batch(
    worker: Any,
    calls: list[tuple[str, dict[str, Any] | None]],
    *,
    timeout: float = 30.0,
    raise_on_error: bool = True,
) -> list[dict[str, Any]]:
    """Execute multiple RPCs in one pipelined stdin flush when supported, else fall back to `worker.call()`."""
    if not calls:
        return []
    batch_fn = getattr(worker, "call_batch", None)
    if callable(batch_fn):
        return batch_fn(calls, timeout=timeout, raise_on_error=raise_on_error)
    results: list[dict[str, Any]] = []
    for method, params in calls:
        if raise_on_error:
            results.append(worker.call(method, params or {}))
        else:
            try:
                results.append(worker.call(method, params or {}))
            except Exception as exc:
                results.append({"ok": False, "_error": str(exc), "error": str(exc)})
    return results


def _stat_float(val: Any) -> float:
    """Parse a stat to float for ordering/threshold checks; returns 0.0 for None/NaN/unparseable."""
    parsed = _parse_numeric_or_none(val)
    if parsed is None or math.isnan(parsed):
        return 0.0
    return parsed


def _finite_float(val: Any, default: float = 0.0) -> float:
    """Parse a stat to a strictly finite float (never NaN/Inf)."""
    parsed = _parse_numeric_or_none(val)
    if parsed is None or not math.isfinite(parsed):
        return default
    return float(parsed)


def _json_safe_stat(val: Any) -> float | str:
    """Convert any PoB stat into a finite float or RFC-8259 sentinel string ('inf', '-inf', 'nan')."""
    if val is None:
        return 0.0
    parsed = _parse_numeric_or_none(val)
    if parsed is None:
        return str(val) if isinstance(val, str) and val.strip() else 0.0
    if math.isfinite(parsed):
        return parsed
    return fmt_num(parsed)


def _safe_min_ele_max_hit(stats: dict[str, Any]) -> float | str | None:
    """Safely compute minimum elemental max hit across Fire/Cold/Lightning (inf/NaN-hardened)."""
    vals: list[float] = []
    for key in ("FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken"):
        raw = stats.get(key)
        if raw is None:
            continue
        parsed = _parse_numeric_or_none(raw)
        if parsed is None or math.isnan(parsed):
            continue
        vals.append(parsed)
    if not vals:
        return None
    m = min(vals)
    return m if math.isfinite(m) else fmt_num(m)


def _extract_points_used(info: Any = None, stats: Any = None, pts: Any = None) -> int | None:
    """Extract allocated passive point count from `points`, `buildInfo.points`, or `stats`."""
    raw: Any = pts.get("used") if isinstance(pts, dict) else None
    if raw is None and isinstance(info, dict) and isinstance(info.get("points"), dict):
        raw = info["points"].get("used")
    if raw is None and isinstance(stats, dict):
        raw = stats.get("pointsUsed")
    if raw is None or isinstance(raw, bool):
        return None
    parsed = _parse_numeric_or_none(raw)
    return int(parsed) if (parsed is not None and math.isfinite(parsed)) else None
