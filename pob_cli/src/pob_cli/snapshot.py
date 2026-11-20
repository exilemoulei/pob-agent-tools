"""In-memory snapshot, undo/redo stack, and transactional what-if mutation runner."""

from __future__ import annotations

import math
from typing import Any, Callable

from .bridge_client import BridgeWorker, MAX_UNDO_STEPS
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


def _stat_float(val: Any) -> float:
    parsed = _parse_numeric_or_none(val)
    return parsed if parsed is not None else 0.0


def _json_safe_stat(val: Any) -> float | str:
    parsed = _parse_numeric_or_none(val)
    if parsed is None:
        return 0.0
    if math.isfinite(parsed):
        return parsed
    return fmt_num(parsed)


def capture_digest(worker: BridgeWorker) -> dict[str, Any]:
    """Capture a complete offence/defence/warning digest of the current build."""
    with worker.lock:
        stats_res = worker.call("get_stats", {})
        sidebar_res = worker.call("get_sidebar", {})
        info_res = worker.call("get_build_info", {})
        header_res = worker.call("get_build_header", {})

    stats = stats_res.get("stats", {})
    warnings = [
        strip_pob_escapes(w.get("text") or w.get("raw") or "")
        for w in sidebar_res.get("warnings", [])
        if (w.get("text") or w.get("raw"))
    ]

    # Pick primary headline DPS metric
    primary_dps_key = "CombinedDPS"
    if _stat_float(stats.get("FullDPS")) > 0:
        primary_dps_key = "FullDPS"
    elif _stat_float(stats.get("MinionCombinedDPS")) > _stat_float(stats.get("CombinedDPS")):
        primary_dps_key = "MinionCombinedDPS"

    return {
        "stats": stats,
        "warnings": warnings,
        "buildInfo": info_res,
        "buildHeader": header_res,
        "primaryDpsKey": primary_dps_key,
        "primaryDps": _json_safe_stat(stats.get(primary_dps_key)),
        "totalEhp": _json_safe_stat(stats.get("TotalEHP")),
        "life": _json_safe_stat(stats.get("Life")),
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
        snap_before = worker.call("export_code", {})["code"]
        digest_before = capture_digest(worker)

        try:
            action_detail = mutate_fn(worker)
            digest_after = capture_digest(worker)
            diff = diff_digest(digest_before, digest_after)
        except Exception:
            # Always restore snapshot on error
            worker.call("import_code", {"code": snap_before, "mode": "replace"})
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
