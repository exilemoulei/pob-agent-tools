"""Multi-objective scoring shared by ranking commands: `--score STAT=W` and `--floor STAT>=N`.

A score is the weighted sum of percent changes against the baseline, so stats of very
different size (DPS in millions, EHP in thousands) can be traded off directly:
`--score CombinedDPS=1 --score TotalEHP=0.5` values +1% EHP at half of +1% DPS.
A floor is a hard constraint on the value after the change.
"""

from __future__ import annotations

import math
import re
from typing import Any

_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_FLOOR_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*(>=|<=)\s*([-+]?[0-9][0-9_,]*(?:\.[0-9]+)?)\s*$")

Floor = tuple[str, str, float]


def parse_score(values: list[str] | tuple[str, ...] | None) -> dict[str, float]:
    """Parse repeatable (or comma-joined) `STAT=WEIGHT` terms; raises ValueError on bad input."""
    out: dict[str, float] = {}
    for value in values or []:
        for term in str(value).split(","):
            if not term.strip():
                continue
            if "=" not in term:
                raise ValueError(f"--score needs STAT=WEIGHT: {term.strip()}")
            key, raw = (p.strip() for p in term.split("=", 1))
            if not _KEY_RE.match(key):
                raise ValueError(f"--score stat must be a PoB stat name: {key!r}")
            try:
                weight = float(raw)
            except ValueError:
                raise ValueError(f"--score weight must be a number: {term.strip()}") from None
            if not math.isfinite(weight):
                raise ValueError(f"--score weight must be finite: {term.strip()}")
            out[key] = weight
    if out and not any(out.values()):
        raise ValueError("--score needs at least one non-zero weight")
    return out


def parse_floors(values: list[str] | tuple[str, ...] | None) -> list[Floor]:
    """Parse `STAT>=N` / `STAT<=N` constraints; raises ValueError on bad input."""
    out: list[Floor] = []
    for value in values or []:
        m = _FLOOR_RE.match(str(value))
        if not m:
            raise ValueError(f"--floor needs STAT>=N or STAT<=N: {value}")
        out.append((m.group(1), m.group(2), float(m.group(3).replace(",", "").replace("_", ""))))
    return out


def floor_to_json(floors: list[Floor]) -> list[list[Any]]:
    return [[k, op, lim] for k, op, lim in floors]


def floors_from_json(raw: Any) -> list[Floor]:
    return [(str(k), str(op), float(lim)) for k, op, lim in (raw or [])]


def stat_keys(weights: dict[str, float], floors: list[Floor]) -> list[str]:
    """Every stat the score or a floor reads, in first-seen order."""
    return list(dict.fromkeys([*weights, *(k for k, _, _ in floors)]))


def stat_value(stats: dict[str, Any], key: str) -> float | None:
    """Finite numeric value of `key` (case-insensitive, so `TotalEHP` finds `totalEhp`)."""
    val = stats.get(key)
    if val is None:
        low = key.lower()
        val = next((v for k, v in stats.items() if str(k).lower() == low), None)
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
        return None
    return float(val)


def pct_change(base: float | None, delta: float | None) -> float | None:
    """Percent change; None when unverified or the baseline is 0 (no meaningful percent)."""
    if base is None or delta is None or base == 0:
        return None
    return delta / abs(base) * 100.0


def weighted_score(pcts: dict[str, float | None], weights: dict[str, float]) -> float | None:
    """Sum of weight * percent; None if any weighted term is unverified (never scored as 0)."""
    total = 0.0
    for key, weight in weights.items():
        if not weight:
            continue
        pct = pcts.get(key)
        if pct is None:
            return None
        total += weight * pct
    return total


def floor_failures(after: dict[str, float | None], floors: list[Floor]) -> list[str]:
    """Floors the post-change values break; an unknown value counts as broken."""
    fails: list[str] = []
    for key, op, lim in floors:
        val = after.get(key)
        if val is None or (val < lim if op == ">=" else val > lim):
            fails.append(f"{key}{op}{lim:g}")
    return fails
