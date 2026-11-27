"""PoE1 Trade API ``armour_filters`` normalization, semantic routing, and local-flat hints."""

from __future__ import annotations

import math
import re
from typing import Any

from .aliases import _ARMOUR_SLOTS, _normalize_slot

ARMOUR_FILTER_KEYS: tuple[str, ...] = ("es", "ar", "ev", "ward")

_LEADING_MOD_PREFIX_RE = re.compile(r"^[+#\s]+")
_SEP_RE = re.compile(r"[\s_\-]+")

# Total / panel defence phrases on armour slots -> native `armour_filters.filters.<key>`.
# Never route these to `pseudo.pseudo_total_energy_shield` (ignores base * quality * %ES).
_PANEL_DEFENCE_PHRASES: dict[str, str] = {
    # Energy Shield
    "total energy shield": "es",
    "total maximum energy shield": "es",
    "total es": "es",
    "panel energy shield": "es",
    "panel es": "es",
    "item energy shield": "es",
    "item es": "es",
    "总能量护盾": "es",
    "总护盾": "es",
    "总最大能量护盾": "es",
    "面板能量护盾": "es",
    "面板护盾": "es",
    "装备能量护盾": "es",
    "装备护盾": "es",
    # Armour
    "total armour": "ar",
    "total armor": "ar",
    "total ar": "ar",
    "panel armour": "ar",
    "panel armor": "ar",
    "panel ar": "ar",
    "item armour": "ar",
    "item armor": "ar",
    "总护甲": "ar",
    "总护甲值": "ar",
    "面板护甲": "ar",
    "面板护甲值": "ar",
    "装备护甲": "ar",
    "装备护甲值": "ar",
    # Evasion
    "total evasion": "ev",
    "total evasion rating": "ev",
    "total ev": "ev",
    "panel evasion": "ev",
    "panel evasion rating": "ev",
    "panel ev": "ev",
    "item evasion": "ev",
    "item evasion rating": "ev",
    "总闪避": "ev",
    "总闪避值": "ev",
    "面板闪避": "ev",
    "面板闪避值": "ev",
    "装备闪避": "ev",
    "装备闪避值": "ev",
    # Ward
    "total ward": "ward",
    "panel ward": "ward",
    "item ward": "ward",
    "总结界": "ward",
    "总结界值": "ward",
    "面板结界": "ward",
    "面板结界值": "ward",
    "装备结界": "ward",
    "装备结界值": "ward",
}

# Local flat defence specs: (armour_key, cli_flag, panel_scale_threshold, label, known_stat_suffixes, template_key).
_LOCAL_FLAT_DEFENCE_SPECS: tuple[tuple[str, str, float, str, frozenset[str], str], ...] = (
    ("es", "--es-min", 150.0, "Energy Shield", frozenset({"stat_4052037485"}), "to maximum energy shield"),
    ("ar", "--ar-min", 600.0, "Armour", frozenset({"stat_3484657501"}), "to armour"),
    ("ev", "--ev-min", 600.0, "Evasion Rating", frozenset({"stat_53045048"}), "to evasion rating"),
    ("ward", "--ward-min", 200.0, "Ward", frozenset({"stat_3214536851"}), "to ward"),
)


def is_armour_slot(slot: str | None = None, category: str | None = None) -> bool:
    """Return True when the target slot or trade category is an armour piece (excluding quivers)."""
    if slot:
        return _normalize_slot(slot) in _ARMOUR_SLOTS
    if category:
        cat = category.strip().lower()
        return cat == "armour" or (cat.startswith("armour.") and cat != "armour.quiver")
    return False


def _clean_bound(value: float, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number.")
    fval = float(value)
    if not math.isfinite(fval):
        raise ValueError(f"{label} must be a finite number.")
    if fval < 0:
        raise ValueError(f"{label} must be >= 0 (got {fval:g}).")
    return int(fval) if fval.is_integer() else fval


def normalize_armour_filters(
    raw: dict[str, dict[str, Any]] | None = None,
    *,
    es_min: float | None = None,
    es_max: float | None = None,
    ar_min: float | None = None,
    ar_max: float | None = None,
    ev_min: float | None = None,
    ev_max: float | None = None,
    ward_min: float | None = None,
    ward_max: float | None = None,
) -> dict[str, dict[str, int | float]]:
    """Validate and build ``filters.armour_filters.filters`` mapping."""
    out: dict[str, dict[str, int | float]] = {}
    if raw:
        for key, bounds in raw.items():
            if key not in ARMOUR_FILTER_KEYS:
                raise ValueError(
                    f"Unknown armour filter key {key!r}; expected one of: {', '.join(ARMOUR_FILTER_KEYS)}"
                )
            if not isinstance(bounds, dict):
                raise ValueError(f"Armour filter {key!r} must be a dict with 'min'/'max'.")
            entry: dict[str, int | float] = {}
            if bounds.get("min") is not None:
                entry["min"] = _clean_bound(bounds["min"], f"--{key}-min")
            if bounds.get("max") is not None:
                entry["max"] = _clean_bound(bounds["max"], f"--{key}-max")
            if "min" in entry and "max" in entry and entry["min"] > entry["max"]:
                raise ValueError(f"--{key}-min ({entry['min']}) cannot exceed --{key}-max ({entry['max']}).")
            if entry:
                out[key] = entry

    cli_pairs = (
        ("es", es_min, es_max),
        ("ar", ar_min, ar_max),
        ("ev", ev_min, ev_max),
        ("ward", ward_min, ward_max),
    )
    cli_map: dict[str, dict[str, int | float]] = {}
    for key, lo, hi in cli_pairs:
        entry = {}
        if lo is not None:
            entry["min"] = _clean_bound(lo, f"--{key}-min")
        if hi is not None:
            entry["max"] = _clean_bound(hi, f"--{key}-max")
        if "min" in entry and "max" in entry and entry["min"] > entry["max"]:
            raise ValueError(f"--{key}-min ({entry['min']}) cannot exceed --{key}-max ({entry['max']}).")
        if entry:
            cli_map[key] = entry

    return merge_armour_filters(out, cli_map) if (out and cli_map) else (out or cli_map)


def merge_armour_filters(
    *maps: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, int | float]]:
    """Merge multiple armour filter dicts, preserving tighter bounds and rejecting conflicts."""
    merged: dict[str, dict[str, int | float]] = {}
    for item in maps:
        if not item:
            continue
        cleaned = normalize_armour_filters(item)
        for key, bounds in cleaned.items():
            cur = merged.setdefault(key, {})
            if "min" in bounds:
                cur["min"] = max(cur["min"], bounds["min"]) if "min" in cur else bounds["min"]
            if "max" in bounds:
                cur["max"] = min(cur["max"], bounds["max"]) if "max" in cur else bounds["max"]
            if "min" in cur and "max" in cur and cur["min"] > cur["max"]:
                raise ValueError(
                    f"Conflicting {key} armour filter bounds: min ({cur['min']}) exceeds max ({cur['max']})."
                )
    return merged


def match_panel_defence_key(text: str) -> str | None:
    """Return ``es``/``ar``/``ev``/``ward`` when ``text`` asks for total/panel item defence."""
    raw = (text or "").strip()
    if not raw:
        return None
    from ..game_data import fold_han

    folded = fold_han(raw).strip().lower()
    folded = _LEADING_MOD_PREFIX_RE.sub("", folded).strip()
    normalized = _SEP_RE.sub(" ", folded)
    if normalized in _PANEL_DEFENCE_PHRASES:
        return _PANEL_DEFENCE_PHRASES[normalized]
    compact = normalized.replace(" ", "")
    return _PANEL_DEFENCE_PHRASES.get(compact)


def extract_armour_panel_filters(
    requests: list[Any],
    *,
    slot: str | None = None,
    category: str | None = None,
) -> tuple[list[Any], dict[str, dict[str, int | float]]]:
    """On armour slots, route total/panel defence ``--stat`` requests into ``armour_filters``."""
    if not is_armour_slot(slot, category):
        return list(requests), {}

    remaining: list[Any] = []
    routed: dict[str, dict[str, int | float]] = {}
    for req in requests:
        key = match_panel_defence_key(getattr(req, "text", ""))
        if key is None:
            remaining.append(req)
            continue
        bounds: dict[str, Any] = {}
        min_v = getattr(req, "min_value", None)
        max_v = getattr(req, "max_value", None)
        if min_v is not None:
            bounds["min"] = min_v
        if max_v is not None:
            bounds["max"] = max_v
        routed = merge_armour_filters(routed, {key: bounds})
    return remaining, routed


def detect_local_defence_hints(
    plain_requests: list[Any],
    *,
    slot: str | None = None,
    category: str | None = None,
) -> list[str]:
    """Warn (non-blocking) when an armour slot filters local flat defence at panel-scale values."""
    if not is_armour_slot(slot, category):
        return []

    hints: list[str] = []
    for req in plain_requests:
        match = getattr(req, "match", None)
        min_v = getattr(req, "min_value", None)
        if match is None or min_v is None:
            continue
        stat_suffix = str(match.id).split(".", 1)[-1]
        raw_lower = str(match.text).lower()
        tpl_lower = str(match.template).lower()
        for _key, flag, threshold, label, suffixes, tpl_key in _LOCAL_FLAT_DEFENCE_SPECS:
            is_local_flat = (
                stat_suffix in suffixes
                or ("(local)" in raw_lower and tpl_key in tpl_lower and "%" not in tpl_lower)
            )
            if is_local_flat and float(min_v) > threshold:
                val_str = str(int(min_v)) if float(min_v).is_integer() else f"{float(min_v):g}"
                hints.append(
                    f"--stat {req.text!r} mapped to local flat {label} ({match.id}) with min {val_str}; "
                    f"for total item panel {label}, use {flag} {val_str} instead."
                )
                break
    return hints
