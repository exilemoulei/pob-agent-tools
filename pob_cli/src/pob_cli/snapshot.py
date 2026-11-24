"""In-memory snapshot, undo/redo stack, and transactional what-if mutation runner."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from .bridge_client import MAX_UNDO_STEPS, BridgeWorker
from .errors import DaemonRequestError
from .formatters import _parse_numeric_or_none, fmt_delta, fmt_num, strip_pob_escapes

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


def _call_batch(
    worker: Any,
    calls: list[tuple[str, dict[str, Any] | None]],
    *,
    timeout: float = 30.0,
    raise_on_error: bool = True,
) -> list[dict[str, Any]]:
    """Execute multiple RPCs in one pipelined stdin flush when supported, else fall back to worker.call()."""
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


def capture_digest(worker: BridgeWorker) -> dict[str, Any]:
    """Capture a complete offence/defence/warning digest of the current build in one pipelined batch."""
    with worker.lock:
        stats_res, sidebar_res, info_res, header_res = _call_batch(
            worker,
            [
                ("get_stats", {}),
                ("get_sidebar", {}),
                ("get_build_info", {}),
                ("get_build_header", {}),
            ],
        )

    stats = stats_res.get("stats", {}) if isinstance(stats_res, dict) else {}
    sidebar_warnings = sidebar_res.get("warnings", []) if isinstance(sidebar_res, dict) else []
    warnings = [
        strip_pob_escapes(w.get("text") or w.get("raw") or "")
        for w in sidebar_warnings
        if isinstance(w, dict) and (w.get("text") or w.get("raw"))
    ]

    # Pick primary headline DPS metric (NaN/Inf-safe)
    primary_dps_key = "CombinedDPS"
    full_dps = _stat_float(stats.get("FullDPS"))
    comb_dps = _stat_float(stats.get("CombinedDPS"))
    minion_dps = _stat_float(stats.get("MinionCombinedDPS"))
    if full_dps > 0:
        primary_dps_key = "FullDPS"
    elif minion_dps > comb_dps:
        primary_dps_key = "MinionCombinedDPS"

    return {
        "stats": stats,
        "warnings": warnings,
        "buildInfo": info_res if isinstance(info_res, dict) else {},
        "buildHeader": header_res if isinstance(header_res, dict) else {},
        "primaryDpsKey": primary_dps_key,
        "primaryDps": _json_safe_stat(stats.get(primary_dps_key)),
        "totalEhp": _json_safe_stat(stats.get("TotalEHP")),
        "life": _json_safe_stat(stats.get("Life")),
    }


def compact_digest_summary(digest: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, strictly JSON-safe headline dict reused across summary, matrix, audit, batch, and sweep."""
    stats = digest.get("stats") or {}
    info = digest.get("buildInfo") or {}
    pts = info.get("points") or {}
    primary_dps_key = digest.get("primaryDpsKey") or "CombinedDPS"

    def _pick(keys: tuple[str, ...]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in keys:
            if stats.get(k) is not None:
                out[k] = _json_safe_stat(stats.get(k))
        return out

    offence = _pick(
        (
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
    )
    defence = _pick(
        (
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
    )
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

    Guarantees atomic restore even if `mutate_fn` raises an exception.
    """
    with worker.lock:
        prev_last_good = getattr(worker, "last_good_code", None)
        snap_before = worker.call("export_code", {})["code"]
        digest_before = capture_digest(worker)

        try:
            action_detail = mutate_fn(worker)
            digest_after = capture_digest(worker)
            diff = diff_digest(digest_before, digest_after)
        except Exception:
            # Always restore snapshot on error
            worker.call("import_code", {"code": snap_before, "mode": "replace"})
            if hasattr(worker, "last_good_code"):
                worker.last_good_code = prev_last_good
            raise

        if apply:
            snap_after = worker.call("export_code", {})["code"]
            worker.undo_stack.append(snap_before)
            if len(worker.undo_stack) > MAX_UNDO_STEPS:
                worker.undo_stack.pop(0)
            worker.redo_stack.clear()
            worker.last_good_code = snap_after
            worker.save_slot_state()
            mode_str = "APPLIED"
        else:
            worker.call("import_code", {"code": snap_before, "mode": "replace"})
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


def undo_build(worker: BridgeWorker) -> dict[str, Any]:
    """Pop one snapshot from `worker.undo_stack` and restore it."""
    with worker.lock:
        if not worker.undo_stack:
            raise DaemonRequestError("Undo stack is empty.")
        current_code = worker.call("export_code", {})["code"]
        digest_before = capture_digest(worker)
        target_code = worker.undo_stack.pop()
        worker.redo_stack.append(current_code)
        if len(worker.redo_stack) > MAX_UNDO_STEPS:
            worker.redo_stack.pop(0)
        worker.call("import_code", {"code": target_code, "mode": "replace"})
        worker.last_good_code = target_code
        worker.save_slot_state()
        digest_after = capture_digest(worker)
        return {
            "action": "undo",
            "diff": diff_digest(digest_before, digest_after),
            "undoDepth": len(worker.undo_stack),
            "redoDepth": len(worker.redo_stack),
        }


def redo_build(worker: BridgeWorker) -> dict[str, Any]:
    """Pop one snapshot from `worker.redo_stack` and restore it."""
    with worker.lock:
        if not worker.redo_stack:
            raise DaemonRequestError("Redo stack is empty.")
        current_code = worker.call("export_code", {})["code"]
        digest_before = capture_digest(worker)
        target_code = worker.redo_stack.pop()
        worker.undo_stack.append(current_code)
        if len(worker.undo_stack) > MAX_UNDO_STEPS:
            worker.undo_stack.pop(0)
        worker.call("import_code", {"code": target_code, "mode": "replace"})
        worker.last_good_code = target_code
        worker.save_slot_state()
        digest_after = capture_digest(worker)
        return {
            "action": "redo",
            "diff": diff_digest(digest_before, digest_after),
            "undoDepth": len(worker.undo_stack),
            "redoDepth": len(worker.redo_stack),
        }
