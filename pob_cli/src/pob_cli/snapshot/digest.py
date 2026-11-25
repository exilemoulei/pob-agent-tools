"""Build digests: one pipelined stats/sidebar/info/header capture, direct-calc digests, and diffs."""

from __future__ import annotations

import math
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none, fmt_delta, fmt_num, strip_pob_escapes
from .checkpoint import supports_direct_calc
from .helpers import (
    _DEFENCE_SUMMARY_KEYS,
    _OFFENCE_SUMMARY_KEYS,
    CORE_STAT_KEYS,
    _call_batch,
    _extract_points_used,
    _json_safe_stat,
    _safe_min_ele_max_hit,
    _stat_float,
)


def digest_from_stats(
    stats: dict[str, Any],
    *,
    warnings: list[str] | None = None,
    build_info: dict[str, Any] | None = None,
    build_header: dict[str, Any] | None = None,
    skill_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a canonical digest dict from a stats map (shared by `capture_digest` and `direct_calc`)."""
    primary_dps_key = "CombinedDPS"
    full_dps = _stat_float(stats.get("FullDPS"))
    comb_dps = _stat_float(stats.get("CombinedDPS"))
    minion_dps = _stat_float(stats.get("MinionCombinedDPS"))
    if full_dps > 0:
        primary_dps_key = "FullDPS"
    elif minion_dps > comb_dps:
        primary_dps_key = "MinionCombinedDPS"

    out: dict[str, Any] = {
        "stats": stats,
        "warnings": list(warnings or []),
        "buildInfo": dict(build_info) if isinstance(build_info, dict) else {},
        "buildHeader": dict(build_header) if isinstance(build_header, dict) else {},
        "primaryDpsKey": primary_dps_key,
        "primaryDps": _json_safe_stat(stats.get(primary_dps_key)),
        "totalEhp": _json_safe_stat(stats.get("TotalEHP")),
        "life": _json_safe_stat(stats.get("Life")),
    }
    pts_used = _extract_points_used(build_info, stats)
    if pts_used is not None:
        out["pointsUsed"] = pts_used
    if isinstance(skill_flags, dict):
        out["skillFlags"] = skill_flags
    return out


def capture_direct_digest(
    worker: Any,
    *,
    skip_ehp: bool = False,
    base_digest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture a build digest via single-pass `direct_calc` when supported, else `capture_digest`.

    Note: when `skip_ehp=True` (offence-only fast mode), Lua omits `RefreshStatList()` so
    `warnings` is empty (`[]`); full `skip_ehp=False` calls populate `warnings` as usual.
    """
    if not supports_direct_calc(worker):
        return capture_digest(worker)
    with worker.lock:
        params: dict[str, Any] = {"skipEHP": True} if skip_ehp else {}
        res = worker.call("direct_calc", params)
    stats = res.get("stats", {}) if isinstance(res, dict) else {}
    skill_flags = res.get("skillFlags") if isinstance(res, dict) else None
    build_info = dict(base_digest.get("buildInfo") or {}) if isinstance(base_digest, dict) else {}
    if isinstance(res, dict) and isinstance(res.get("points"), dict):
        build_info["points"] = res["points"]
    build_header = dict(base_digest.get("buildHeader") or {}) if isinstance(base_digest, dict) else {}
    warns: list[str] = []
    for w in (res.get("warnings") or []) if isinstance(res, dict) else []:
        txt = strip_pob_escapes(w if isinstance(w, str) else (w.get("raw") or w.get("text") or "") if isinstance(w, dict) else "")
        if txt:
            warns.append(txt)
    return digest_from_stats(
        stats,
        warnings=warns,
        build_info=build_info,
        build_header=build_header,
        skill_flags=skill_flags if isinstance(skill_flags, dict) else None,
    )


def capture_digest(worker: BridgeWorker) -> dict[str, Any]:
    """Capture a complete offence/defence/warning digest of the current build in one pipelined batch."""
    with worker.lock:
        history = getattr(worker, "slot_history", None)
        if history is not None and history.active is not None:
            history.active.fresh()
        stats_res, sidebar_res, info_res, header_res = _call_batch(
            worker,
            [("get_stats", {}), ("get_sidebar", {}), ("get_build_info", {}), ("get_build_header", {})],
        )

    stats = stats_res.get("stats", {}) if isinstance(stats_res, dict) else {}
    info_dict = dict(info_res) if isinstance(info_res, dict) else {}
    if "points" not in info_dict and isinstance(stats_res, dict) and isinstance(stats_res.get("points"), dict):
        info_dict["points"] = stats_res["points"]
    sidebar_warnings = sidebar_res.get("warnings", []) if isinstance(sidebar_res, dict) else []
    warnings = [
        strip_pob_escapes(w.get("raw") or w.get("text") or "")
        for w in sidebar_warnings
        if isinstance(w, dict) and (w.get("raw") or w.get("text"))
    ]
    return digest_from_stats(
        stats,
        warnings=warnings,
        build_info=info_dict,
        build_header=header_res if isinstance(header_res, dict) else {},
    )


def compact_digest_summary(digest: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, strictly JSON-safe headline dict reused across summary, matrix, audit, batch, and sweep."""
    stats = digest.get("stats") or {}
    info = digest.get("buildInfo") or {}
    pts = info.get("points") or {}
    primary_dps_key = digest.get("primaryDpsKey") or "CombinedDPS"

    offence = {k: _json_safe_stat(stats.get(k)) for k in _OFFENCE_SUMMARY_KEYS if stats.get(k) is not None}
    defence = {k: _json_safe_stat(stats.get(k)) for k in _DEFENCE_SUMMARY_KEYS if stats.get(k) is not None}
    ele_min = _safe_min_ele_max_hit(stats)
    if ele_min is not None:
        defence["EleMaxHit"] = ele_min

    core_stats = {
        key: _json_safe_stat(stats.get(key))
        for key, _, _ in CORE_STAT_KEYS
        if stats.get(key) is not None
    }

    primary_dps_raw = digest.get("primaryDps") if digest.get("primaryDps") is not None else stats.get(primary_dps_key)
    total_ehp_raw = digest.get("totalEhp") if digest.get("totalEhp") is not None else stats.get("TotalEHP")
    life_raw = digest.get("life") if digest.get("life") is not None else stats.get("Life")

    return {
        "build": {
            "level": info.get("level"),
            "className": info.get("className"),
            "classNameZh": info.get("classNameZh"),
            "ascendClassName": info.get("ascendClassName"),
            "ascendClassNameZh": info.get("ascendClassNameZh"),
            "treeVersion": info.get("treeVersion"),
            "points": (
                {
                    "used": pts.get("used"),
                    "usedMax": pts.get("usedMax"),
                    "ascUsed": pts.get("ascUsed"),
                    "ascMax": pts.get("ascMax"),
                }
                if isinstance(pts, dict) and pts
                else {}
            ),
        },
        "primaryDpsKey": primary_dps_key,
        "primaryDps": _json_safe_stat(primary_dps_raw),
        "totalEhp": _json_safe_stat(total_ehp_raw),
        "life": _json_safe_stat(life_raw),
        "offence": offence,
        "defence": defence,
        "coreStats": core_stats,
        "warnings": list(digest.get("warnings") or []),
    }


def diff_digest(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare two build digests and return structured metric deltas and warning diffs."""
    b_stats = before.get("stats", {})
    a_stats = after.get("stats", {})

    rows: list[dict[str, Any]] = []
    for key, label, unit in CORE_STAT_KEYS:
        b_val = b_stats.get(key)
        a_val = a_stats.get(key)
        if b_val is None and a_val is None:
            continue

        bf = _parse_numeric_or_none(b_val)
        af = _parse_numeric_or_none(a_val)
        if bf is None or af is None:
            b_disp = fmt_num(b_val)
            a_disp = fmt_num(a_val)
            if b_disp == a_disp:
                continue
            rows.append(
                {
                    "stat": key,
                    "label": label,
                    "unit": unit,
                    "before": b_disp,
                    "after": a_disp,
                    "delta": None,
                    "pct": None,
                    "beforeStr": f"{b_disp}{unit}",
                    "afterStr": f"{a_disp}{unit}",
                    "deltaStr": fmt_delta(b_val, a_val, unit),
                }
            )
            continue

        if (math.isnan(bf) and math.isnan(af)) or bf == af:
            continue
        both_finite = math.isfinite(bf) and math.isfinite(af)
        if both_finite and abs(af - bf) < 1e-4:
            continue

        delta_val: float | None = (af - bf) if both_finite else None
        pct = ((af - bf) / abs(bf) * 100.0) if (both_finite and abs(bf) > 1e-6) else None
        rows.append(
            {
                "stat": key,
                "label": label,
                "unit": unit,
                "before": bf if math.isfinite(bf) else fmt_num(bf),
                "after": af if math.isfinite(af) else fmt_num(af),
                "delta": delta_val,
                "pct": pct,
                "beforeStr": f"{fmt_num(bf)}{unit}",
                "afterStr": f"{fmt_num(af)}{unit}",
                "deltaStr": fmt_delta(bf, af, unit),
            }
        )

    b_warn = set(before.get("warnings", []))
    a_warn = set(after.get("warnings", []))
    new_warnings = sorted(a_warn - b_warn)
    resolved_warnings = sorted(b_warn - a_warn)

    b_pts = before.get("buildInfo", {}).get("points", {})
    a_pts = after.get("buildInfo", {}).get("points", {})

    return {
        "deltas": rows,
        "newWarnings": new_warnings,
        "resolvedWarnings": resolved_warnings,
        "allWarningsAfter": after.get("warnings", []),
        "pointsBefore": b_pts,
        "pointsAfter": a_pts,
    }
