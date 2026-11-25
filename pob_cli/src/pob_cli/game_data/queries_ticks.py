"""Server-tick cooldown breakpoints for `pob data tick`."""

from __future__ import annotations

from typing import Any

from ..ticks import build_server_tick_table

_TICK_BASE_CD_PRESETS: dict[str, tuple[float, str]] = {
    "coc": (0.15, "Cast On Critical Strike (0.15s)"),
    "cospri": (0.25, "Cospri's Malice (0.25s)"),
    "mjolner": (0.25, "Mjölner (0.25s)"),
    "poet": (0.25, "The Poet's Pen (0.25s)"),
    "cwdt": (0.25, "Cast when Damage Taken (0.25s)"),
    "soa": (0.50, "The Sword of Ages / Temporal Mirage (0.50s)"),
    "mirage": (0.50, "Temporal Mirage (0.50s)"),
    "hidden": (0.50, "The Hidden Blade (0.50s)"),
    "spellslinger": (0.60, "Spellslinger (0.60s)"),
    "kitava": (0.10, "Kitava's Thirst (0.10s)"),
}


def query_tick_breakpoints(
    base_cd_spec: str | float,
    *,
    cdr_pct: float | None = None,
    aps: float | None = None,
    triggered_spells: int = 1,
) -> dict[str, Any]:
    """Daemon-free 0ms PoE1 0.033s (33ms) server-tick cooldown breakpoint calculator."""
    raw = str(base_cd_spec or "").strip()
    low = raw.lower()
    preset_label: str | None = None
    if low in _TICK_BASE_CD_PRESETS:
        base_cd, preset_label = _TICK_BASE_CD_PRESETS[low]
    elif low.endswith("ms"):
        try:
            base_cd = float(low[:-2].strip()) / 1000.0
        except ValueError as exc:
            raise ValueError(
                f"Invalid base cooldown {base_cd_spec!r}; pass seconds (e.g. 0.15), ms (150ms), or preset ({', '.join(_TICK_BASE_CD_PRESETS)})."
            ) from exc
    else:
        try:
            base_cd = float(low.rstrip("s").strip())
        except ValueError as exc:
            raise ValueError(
                f"Invalid base cooldown {base_cd_spec!r}; pass seconds (e.g. 0.15), ms (150ms), or preset ({', '.join(_TICK_BASE_CD_PRESETS)})."
            ) from exc
    if base_cd <= 0:
        raise ValueError(f"Base cooldown must be > 0 (got {base_cd}).")

    tbl = build_server_tick_table(
        base_cd,
        cdr_pct=cdr_pct,
        aps=aps,
        triggered_spells=triggered_spells,
    )
    tbl["kind"] = "tick"
    if preset_label:
        tbl["preset"] = preset_label
    return tbl
