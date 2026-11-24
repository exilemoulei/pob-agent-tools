"""In-memory snapshot, undo/redo stack, and transactional what-if mutation runner."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from ..bridge_client import MAX_UNDO_STEPS, BridgeWorker
from ..errors import DaemonRequestError
from ..formatters import _parse_numeric_or_none, fmt_delta, fmt_num, strip_pob_escapes
from .helpers import (
    _DEFENCE_SUMMARY_KEYS,
    _OFFENCE_SUMMARY_KEYS,
    CORE_STAT_KEYS,
    _call_batch,
    _extract_points_used,
    _finite_float,
    _json_safe_stat,
    _safe_min_ele_max_hit,
    _stat_float,
)

__all__ = [
    "CORE_STAT_KEYS",
    "BuildSnapshot",
    "_call_batch",
    "_extract_points_used",
    "_finite_float",
    "_json_safe_stat",
    "_safe_min_ele_max_hit",
    "_stat_float",
    "capture_digest",
    "capture_direct_digest",
    "compact_digest_summary",
    "diff_digest",
    "digest_from_stats",
    "drop_snapshot",
    "redo_build",
    "restore_snapshot",
    "run_mutation",
    "supports_cow_snapshot",
    "supports_direct_calc",
    "take_snapshot",
    "undo_build",
    "verify_cow_restored",
    "worker_caps",
]


class BuildSnapshot:
    """Opaque build checkpoint backed by Lua CoW (`cow_snapshot`) or lossless `export_code`."""

    __slots__ = ("fallback_code", "kind", "proc_tag", "token")

    def __init__(
        self,
        kind: str,
        token: str,
        *,
        fallback_code: str | None = None,
        proc_tag: Any = None,
    ) -> None:
        self.kind = kind
        self.token = token
        self.fallback_code = fallback_code
        self.proc_tag = proc_tag


def worker_caps(worker: Any) -> dict[str, Any]:
    """Safely return a worker's capability dict (empty when a test FakeWorker lacks `.caps`)."""
    caps = getattr(worker, "caps", None)
    return caps if isinstance(caps, dict) else {}


def supports_cow_snapshot(worker: Any) -> bool:
    """Return True when the worker advertises `cowSnapshot` (C2c Lua CoW rollback)."""
    return bool(worker_caps(worker).get("cowSnapshot"))


def supports_direct_calc(worker: Any) -> bool:
    """Return True when the worker advertises `directCalc` (C1 single-pass direct calc)."""
    return bool(worker_caps(worker).get("directCalc"))


def take_snapshot(worker: Any, *, prefer_cow: bool = True) -> BuildSnapshot:
    """Take a Lua CoW snapshot when advertised, otherwise fall back to `export_code`."""
    if prefer_cow and supports_cow_snapshot(worker):
        proc_tag = getattr(worker, "_proc", None)
        res = worker.call("cow_snapshot", {})
        snap_id = res.get("snapId") if isinstance(res, dict) else None
        if isinstance(snap_id, str) and snap_id:
            fb_code = getattr(worker, "last_good_code", None)
            if not fb_code:
                try:
                    fb_code = str((worker.call("export_code", {}) or {}).get("code") or "") or None
                except Exception:
                    fb_code = None
            return BuildSnapshot(
                "cow",
                snap_id,
                fallback_code=fb_code,
                proc_tag=proc_tag,
            )
    return BuildSnapshot("code", str(worker.call("export_code", {})["code"]))


def restore_snapshot(
    worker: Any,
    snap: BuildSnapshot,
    *,
    drop: bool = False,
    rebuild: bool = True,
) -> None:
    """Restore a `BuildSnapshot` (CoW or `import_code`), falling back cleanly if CoW or subprocess fails."""
    if snap.kind == "cow":
        cur_proc = getattr(worker, "_proc", None)
        if cur_proc is snap.proc_tag:
            params: dict[str, Any] = {"snapId": snap.token}
            if drop:
                params["drop"] = True
            if not rebuild:
                params["rebuild"] = False
            try:
                worker.call("cow_restore", params)
                return
            except Exception:
                if not snap.fallback_code:
                    raise
        if snap.fallback_code:
            worker.call("import_code", {"code": snap.fallback_code, "mode": "replace"})
            return
    worker.call("import_code", {"code": snap.token, "mode": "replace"})


def drop_snapshot(worker: Any, snap: BuildSnapshot | None) -> None:
    """Best-effort release of a Lua CoW snapshot (no-op for `export_code` or restarted subprocess)."""
    if snap is None or snap.kind != "cow":
        return
    if getattr(worker, "_proc", None) is not snap.proc_tag:
        return
    try:
        worker.call("cow_drop", {"snapId": snap.token})
    except Exception:
        pass


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
        txt = strip_pob_escapes(w if isinstance(w, str) else (w.get("text") or w.get("raw") or "") if isinstance(w, dict) else "")
        if txt:
            warns.append(txt)
    return digest_from_stats(
        stats,
        warnings=warns,
        build_info=build_info,
        build_header=build_header,
        skill_flags=skill_flags if isinstance(skill_flags, dict) else None,
    )


def verify_cow_restored(worker: Any, before_digest: dict[str, Any]) -> dict[str, Any]:
    """Verify a CoW-restored build recalculates identical stats and passive points."""
    use_direct = isinstance(before_digest, dict) and "skillFlags" in before_digest and supports_direct_calc(worker)
    res = (worker.call("direct_calc" if use_direct else "get_stats", {}) or {})
    after = res.get("stats") or {}
    after_pts = _extract_points_used(stats=after, pts=res.get("points"))
    before = (before_digest.get("stats") or {}) if isinstance(before_digest, dict) else {}
    core_keys = {k for k, _, _ in CORE_STAT_KEYS}
    check_keys = (set(before.keys()) & set(after.keys())) | (core_keys & (set(before.keys()) | set(after.keys())))
    differing: list[str] = []
    for k in check_keys:
        rb, ra = before.get(k), after.get(k)
        if isinstance(rb, bool) or isinstance(ra, bool):
            continue
        b, a = _parse_numeric_or_none(rb), _parse_numeric_or_none(ra)
        if b is None and a is None:
            continue
        if b is None or a is None:
            differing.append(k)
        elif math.isnan(b) or math.isnan(a):
            if not (math.isnan(b) and math.isnan(a)):
                differing.append(k)
        elif math.isinf(b) or math.isinf(a):
            if a != b:
                differing.append(k)
        elif abs(a - b) > 1e-6 * max(1.0, abs(b)):
            differing.append(k)
    before_pts = None
    if isinstance(before_digest, dict):
        bp_raw = before_digest.get("pointsUsed")
        bp_num = _parse_numeric_or_none(bp_raw) if (bp_raw is not None and not isinstance(bp_raw, bool)) else None
        before_pts = (
            int(bp_num)
            if (bp_num is not None and math.isfinite(bp_num))
            else _extract_points_used(before_digest.get("buildInfo"), before)
        )
    if before_pts is not None:
        if after_pts is None:
            for rpc_name in ("get_build_info", "get_tree_state"):
                try:
                    aux = worker.call(rpc_name, {}) or {}
                except Exception:
                    continue
                after_pts = _extract_points_used(aux, pts=aux.get("points") if isinstance(aux, dict) else None)
                if after_pts is not None:
                    break
        if after_pts is None or after_pts != before_pts:
            differing.append("pointsUsed")
    if differing:
        differing.sort()
        return {
            "verified": False,
            "detail": f"{len(differing)} stat(s) differ after CoW restore (e.g. {', '.join(differing[:5])})",
            "differing": differing,
        }
    return {"verified": True, "detail": "CoW snapshot restored with identical stats"}


def capture_digest(worker: BridgeWorker) -> dict[str, Any]:
    """Capture a complete offence/defence/warning digest of the current build in one pipelined batch."""
    with worker.lock:
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
        strip_pob_escapes(w.get("text") or w.get("raw") or "")
        for w in sidebar_warnings
        if isinstance(w, dict) and (w.get("text") or w.get("raw"))
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


def run_mutation(
    worker: BridgeWorker,
    mutate_fn: Callable[[BridgeWorker], Any],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Execute `mutate_fn(worker)` with full snapshot rollback (what-if) or undo-stack commit (`--apply`).

    Uses Lua CoW snapshots for `apply=False` when `worker.caps.get("cowSnapshot")` is true;
    `apply=True` always exports lossless XML for `undo_stack` and `slot_<slug>.json`.
    Guarantees atomic restore even if `mutate_fn` raises an exception.
    """
    with worker.lock:
        prev_last_good = getattr(worker, "last_good_code", None)
        snap = take_snapshot(worker, prefer_cow=not apply)
        try:
            digest_before = capture_digest(worker)
        except Exception:
            drop_snapshot(worker, snap)
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good
            raise

        try:
            action_detail = mutate_fn(worker)
            digest_after = capture_digest(worker)
            diff = diff_digest(digest_before, digest_after)
        except Exception:
            restore_snapshot(worker, snap, drop=True)
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good
            raise

        if apply:
            snap_after = worker.call("export_code", {})["code"]
            worker.undo_stack.append(snap.token)
            if len(worker.undo_stack) > MAX_UNDO_STEPS:
                worker.undo_stack.pop(0)
            worker.redo_stack.clear()
            worker.last_good_code = snap_after
            worker.save_slot_state()
            mode_str = "APPLIED"
        else:
            restore_snapshot(worker, snap, drop=True)
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good
            mode_str = "WHAT-IF (not applied; pass --apply to persist)"

        return {
            "applied": apply,
            "mode": mode_str,
            "detail": action_detail,
            "before": {
                "primaryDpsKey": digest_before["primaryDpsKey"],
                "primaryDps": digest_before["primaryDps"],
                "totalEhp": digest_before["totalEhp"],
                "life": digest_before["life"],
                "points": digest_before.get("buildInfo", {}).get("points", {}),
            },
            "after": {
                "primaryDpsKey": digest_after["primaryDpsKey"],
                "primaryDps": digest_after["primaryDps"],
                "totalEhp": digest_after["totalEhp"],
                "life": digest_after["life"],
                "points": digest_after.get("buildInfo", {}).get("points", {}),
            },
            "diff": diff,
            "undoDepth": len(worker.undo_stack),
            "redoDepth": len(worker.redo_stack),
        }


def _shift_history(worker: BridgeWorker, *, action: str) -> dict[str, Any]:
    with worker.lock:
        src_stack = worker.undo_stack if action == "undo" else worker.redo_stack
        dst_stack = worker.redo_stack if action == "undo" else worker.undo_stack
        if not src_stack:
            raise DaemonRequestError(f"{action.capitalize()} stack is empty.")
        current_code = worker.call("export_code", {})["code"]
        digest_before = capture_digest(worker)
        target_code = src_stack.pop()
        dst_stack.append(current_code)
        if len(dst_stack) > MAX_UNDO_STEPS:
            dst_stack.pop(0)
        worker.call("import_code", {"code": target_code, "mode": "replace"})
        worker.last_good_code = target_code
        worker.save_slot_state()
        return {
            "action": action,
            "diff": diff_digest(digest_before, capture_digest(worker)),
            "undoDepth": len(worker.undo_stack),
            "redoDepth": len(worker.redo_stack),
        }


def undo_build(worker: BridgeWorker) -> dict[str, Any]:
    """Pop one snapshot from `worker.undo_stack` and restore it."""
    return _shift_history(worker, action="undo")


def redo_build(worker: BridgeWorker) -> dict[str, Any]:
    """Pop one snapshot from `worker.redo_stack` and restore it."""
    return _shift_history(worker, action="redo")
