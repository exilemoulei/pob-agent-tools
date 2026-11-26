"""Small static build checks used by the audit: resistance cap gaps and slot-emptying keystones."""

from __future__ import annotations

import math
from typing import Any

from ..formatters import _parse_numeric_or_none
from ..game_data import english_for_zh


def _has_hollow_palm_keystone(tree_summary: dict[str, Any] | None) -> bool:
    if not isinstance(tree_summary, dict):
        return False
    for ks in tree_summary.get("keystones") or []:
        names = [ks.get(k) for k in ("name", "nameEn", "nameZh")] if isinstance(ks, dict) else [ks]
        for nm in names:
            txt = str(nm or "")
            if not txt:
                continue
            if "hollow palm" in txt.lower():
                return True
            try:
                eng = english_for_zh(txt)
            except Exception:
                eng = None
            if eng and "hollow palm" in eng.lower():
                return True
    return False


def _resist_cap_and_gap(stats: dict[str, Any], elem: str, val: float) -> tuple[float, float]:
    """Return (max resist, gap to it) from the engine's outputs; 75% only when the max is unknown.

    PoB's CalcDefence sets `Missing{elem}Resist = max(0, max - final)` and clamps
    `{elem}ResistOverCap` at 0, so the missing figure is what says a raised cap is not reached.
    """

    def _num(key: str) -> float | None:
        raw = stats.get(key)
        if raw is None:  # _parse_numeric_or_none maps None to 0.0; an absent stat is unknown here
            return None
        parsed = _parse_numeric_or_none(raw)
        return parsed if parsed is not None and math.isfinite(parsed) else None

    max_raw = _num(f"{elem}ResistMax")
    if max_raw is not None:
        return max_raw, max(0.0, max_raw - val)
    missing_raw = _num(f"Missing{elem}Resist")
    if missing_raw is not None:
        missing = max(0.0, missing_raw)
        return val + missing, missing
    over_raw = _num(f"{elem}ResistOverCap")
    if over_raw is not None and over_raw > 0:
        return val, 0.0  # total exceeds the (possibly lowered) max, so the shown value is the cap
    return 75.0, max(0.0, 75.0 - val)
