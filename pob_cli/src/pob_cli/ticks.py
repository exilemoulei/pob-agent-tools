"""PoE1 server-tick (0.033s / 33ms) cooldown math and breakpoint tables (L0 pure module)."""

from __future__ import annotations

import math
from typing import Any

POE1_SERVER_TICK_SEC = 0.033


def compute_server_tick_cooldown(
    base_cd: float,
    cdr_pct: float,
    *,
    aps: float | None = None,
    triggered_spells: int = 1,
) -> dict[str, Any]:
    """Compute exact PoE1 0.033s (33ms, NOT 1/30=33.33ms) server-tick effective cooldown & next breakpoint."""
    base = max(0.001, float(base_cd))
    cdr = max(0.0, float(cdr_pct))
    spells = max(1, int(triggered_spells or 1))
    raw_cd = base / (1.0 + cdr / 100.0)
    ticks = max(1, math.ceil(raw_cd / POE1_SERVER_TICK_SEC - 1e-9))
    eff_cd = round(ticks * POE1_SERVER_TICK_SEC, 3)
    eff_ms = ticks * 33
    max_rate = round(1.0 / eff_cd, 2) if eff_cd > 0 else 0.0
    eff_cap = round(max_rate * spells, 2)

    next_ticks = ticks - 1
    next_info: dict[str, Any] | None = None
    if next_ticks >= 1:
        next_cdr_pct = math.ceil((base / (next_ticks * POE1_SERVER_TICK_SEC) - 1.0) * 100.0 - 1e-9)
        cdr_delta = max(0, int(next_cdr_pct - math.floor(cdr + 1e-9)))
        next_rate = round(1.0 / (next_ticks * POE1_SERVER_TICK_SEC), 2)
        next_info = {
            "targetTicks": next_ticks,
            "requiredCdrPct": int(next_cdr_pct),
            "cdrNeededPct": int(cdr_delta),
            "effectiveCdSec": round(next_ticks * POE1_SERVER_TICK_SEC, 3),
            "effectiveCdMs": next_ticks * 33,
            "maxTriggersPerSec": next_rate,
            "effectiveCapPerSec": round(next_rate * spells, 2),
        }

    out: dict[str, Any] = {
        "baseCdSec": base,
        "serverTickSec": POE1_SERVER_TICK_SEC,
        "serverTickMs": 33,
        "globalCdrPct": round(cdr, 2),
        "rawCdSec": round(raw_cd, 4),
        "ticks": ticks,
        "effectiveCdSec": eff_cd,
        "effectiveCdMs": eff_ms,
        "maxTriggersPerSec": max_rate,
        "triggeredSpells": spells,
        "effectiveCapPerSec": eff_cap,
        "nextBreakpoint": next_info,
    }
    if aps is not None and math.isfinite(float(aps)) and float(aps) > 0:
        aps_f = round(float(aps), 2)
        overspeed = aps_f > (eff_cap * 1.01)
        out["aps"] = aps_f
        out["overspeed"] = overspeed
        out["apsRatioToCap"] = round(aps_f / eff_cap, 3) if eff_cap > 0 else None
        if overspeed:
            out["overspeedNote"] = (
                f"APS ({aps_f:.2f}/s) exceeds trigger cap ({eff_cap:.2f}/s at {cdr:g}% CDR, "
                f"{ticks}t={eff_ms}ms): "
                "attacks land during cooldown and HALVE effective trigger rate!"
            )
    return out


def build_server_tick_table(
    base_cd: float,
    *,
    cdr_pct: float | None = None,
    aps: float | None = None,
    triggered_spells: int = 1,
    max_ticks: int = 16,
) -> dict[str, Any]:
    """Build exact PoE1 0.033s (33ms) server-tick CDR breakpoint table for any cooldown skill/trigger."""
    base = max(0.001, float(base_cd))
    spells = max(1, int(triggered_spells or 1))
    base_ticks = max(1, math.ceil(base / POE1_SERVER_TICK_SEC - 1e-9))
    start_ticks = min(max(1, int(max_ticks)), base_ticks)

    rows: list[dict[str, Any]] = []
    seen_cdrs: set[int] = set()
    for t in range(start_ticks, 0, -1):
        req_cdr = max(0, math.ceil((base / (t * POE1_SERVER_TICK_SEC) - 1.0) * 100.0 - 1e-9))
        if req_cdr in seen_cdrs:
            continue
        seen_cdrs.add(req_cdr)
        eff_cd = round(t * POE1_SERVER_TICK_SEC, 3)
        rate = round(1.0 / eff_cd, 2)
        rows.append(
            {
                "ticks": t,
                "minCdrPct": int(req_cdr),
                "effectiveCdSec": eff_cd,
                "effectiveCdMs": t * 33,
                "maxTriggersPerSec": rate,
                "maxApsCap": round(rate * spells, 2),
            }
        )

    current = (
        compute_server_tick_cooldown(base, float(cdr_pct), aps=aps, triggered_spells=spells)
        if cdr_pct is not None
        else (
            compute_server_tick_cooldown(base, 0.0, aps=aps, triggered_spells=spells)
            if aps is not None
            else None
        )
    )
    return {
        "baseCdSec": base,
        "serverTickSec": POE1_SERVER_TICK_SEC,
        "serverTickMs": 33,
        "triggeredSpells": spells,
        "breakpoints": rows,
        "current": current,
        "cwcNote": (
            "Note: Cast while Channelling (CwC) uses a FIXED channelling interval "
            "(320ms at Lv20, 300ms Awakened Lv5), NOT a Cooldown — CDR has ZERO effect on CwC."
        ),
    }
