"""Per-item PoB water attribution with atomic coupled-ailment grouping (L2)."""

from __future__ import annotations

import math
from typing import Any

from ..bridge_client import BridgeWorker
from ..snapshot import _call_batch
from .dewater import (
    _digest_metrics,
    _extract_probe_state,
    _probe_rpc_method,
    _rpc_error_text,
)
from .dewater_steps import _same_state

# Non-damaging ailment config pairs (ConfigOptions.lua) where the effect value is gated by the
# boolean flag's Condition:*Config. Measuring either half alone yields 0 delta on the real engine.
COUPLED_CONFIG_PAIRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("conditionEnemyShocked", ("conditionShockEffect",)),
    ("conditionEnemyChilled", ("conditionEnemyChilledEffect",)),
    ("conditionEnemyScorched", ("conditionScorchedEffect",)),
    ("conditionEnemyBrittle", ("conditionBrittleEffect",)),
    ("conditionEnemySapped", ("conditionSapEffect",)),
    ("buffElusive", ("overrideBuffElusive",)),
    ("buffFortification", ("overrideFortification",)),
)


def group_coupled_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge stripped coupled (flag, effect) config pairs into atomic attribution units.

    A partial clamp (e.g. only `conditionShockEffect` stripped/clamped while `conditionEnemyShocked`
    stayed True) has only one member in `records` and passes through unchanged.
    """
    if len(records) < 2:
        return list(records)

    cfg_index_by_low: dict[str, int] = {}
    for idx, rec in enumerate(records):
        if rec.get("dimension") == "config":
            key = str(rec.get("key") or "")
            if key:
                cfg_index_by_low[key.lower()] = idx

    merged_at: dict[int, dict[str, Any]] = {}
    consumed: set[int] = set()
    for flag_var, eff_vars in COUPLED_CONFIG_PAIRS:
        f_idx = cfg_index_by_low.get(flag_var.lower())
        if f_idx is None or f_idx in consumed:
            continue
        e_idx: int | None = None
        for ev in eff_vars:
            cand = cfg_index_by_low.get(ev.lower())
            if cand is not None and cand != f_idx and cand not in consumed:
                e_idx = cand
                break
        if e_idx is None:
            continue

        f_rec = records[f_idx]
        e_rec = records[e_idx]
        f_key = str(f_rec["key"])
        e_key = str(e_rec["key"])
        first_idx = min(f_idx, e_idx)
        consumed.update((f_idx, e_idx))
        merged_at[first_idx] = {
            "dimension": "config",
            "key": f"{f_key}+{e_key}",
            "label": f"{f_rec['label']} + {e_rec['label']}",
            "coupledKeys": [f_key, e_key],
            "restore": list(f_rec.get("restore") or []) + list(e_rec.get("restore") or []),
            "revert": list(e_rec.get("revert") or []) + list(f_rec.get("revert") or []),
            "probe": {"kind": "config_multi", "vars": [f_key, e_key]},
            "original": {f_key: f_rec.get("original"), e_key: e_rec.get("original")},
        }

    if not merged_at:
        return list(records)

    out: list[dict[str, Any]] = []
    for idx, rec in enumerate(records):
        if idx in merged_at:
            out.append(merged_at[idx])
        elif idx not in consumed:
            out.append(rec)
    return out


def _prepare_attribution_probes(
    worker: BridgeWorker,
    records: list[dict[str, Any]],
) -> tuple[list[str | None], list[str | None], dict[str, dict[str, Any]], str | None]:
    needed_probe_rpcs: list[str] = []
    rec_probe_rpcs: list[str | None] = []
    rec_probe_errors: list[str | None] = []
    for rec in records:
        probe = rec.get("probe")
        if not probe:
            rec_probe_rpcs.append(None)
            rec_probe_errors.append(None)
            continue
        try:
            rpc_name = _probe_rpc_method(probe)
        except Exception as exc:
            rec_probe_rpcs.append(None)
            rec_probe_errors.append(f"cannot read state before restore: {exc}")
            continue
        rec_probe_rpcs.append(rpc_name)
        rec_probe_errors.append(None)
        if rpc_name not in needed_probe_rpcs:
            needed_probe_rpcs.append(rpc_name)

    pre_probe_results: dict[str, dict[str, Any]] = {}
    pre_probe_batch_err: str | None = None
    if needed_probe_rpcs:
        try:
            raw_pre = _call_batch(
                worker,
                [(m, {}) for m in needed_probe_rpcs],
                raise_on_error=False,
            )
            for m, r in zip(needed_probe_rpcs, raw_pre, strict=False):
                pre_probe_results[m] = r
        except Exception as exc:
            pre_probe_batch_err = str(exc)

    return rec_probe_rpcs, rec_probe_errors, pre_probe_results, pre_probe_batch_err


def _measure_single_water_record(
    worker: BridgeWorker,
    rec: dict[str, Any],
    probe: dict[str, Any] | None,
    probe_rpc: str | None,
    before: Any,
    base: dict[str, float | str | None],
    dps_key: str,
) -> tuple[dict[str, Any], str | None]:
    row: dict[str, Any] = {
        "dimension": rec["dimension"],
        "key": rec["key"],
        "label": rec["label"],
    }
    if rec.get("coupledKeys"):
        row["coupledKeys"] = list(rec["coupledKeys"])
    restore_calls = list(rec.get("restore") or [])
    revert_calls = list(rec.get("revert") or [])

    batch_calls: list[tuple[str, dict[str, Any]]] = []
    restore_start = len(batch_calls)
    batch_calls.extend(restore_calls)
    restore_end = len(batch_calls)

    probe_after_idx: int | None = None
    if probe and probe_rpc is not None:
        probe_after_idx = len(batch_calls)
        batch_calls.append((probe_rpc, {}))

    stats_idx = len(batch_calls)
    batch_calls.append(("get_stats", {}))

    revert_start = len(batch_calls)
    batch_calls.extend(revert_calls)
    revert_end = len(batch_calls)

    probe_back_idx: int | None = None
    if probe and probe_rpc is not None:
        probe_back_idx = len(batch_calls)
        batch_calls.append((probe_rpc, {}))

    try:
        batch_res = _call_batch(worker, batch_calls, raise_on_error=False)
    except Exception as exc:
        row["error"] = str(exc)
        return row, rec["key"]

    if len(batch_res) < len(batch_calls):
        row["error"] = f"incomplete batch response ({len(batch_res)}/{len(batch_calls)} RPCs)"
        return row, rec["key"]

    restore_err: str | None = None
    for r_i in range(restore_start, restore_end):
        err_txt = _rpc_error_text(batch_res[r_i])
        if err_txt is not None:
            restore_err = err_txt
            break

    if restore_err is None and probe and probe_after_idx is not None:
        p_err = _rpc_error_text(batch_res[probe_after_idx])
        if p_err is not None:
            restore_err = p_err
        else:
            try:
                got = _extract_probe_state(probe, batch_res[probe_after_idx])
                if not _same_state(got, rec.get("original")):
                    restore_err = (
                        f"restore not applied: expected {rec.get('original')!r}, PoB reads {got!r}"
                    )
            except Exception as exc:
                restore_err = str(exc)

    if restore_err is None:
        stats_res = batch_res[stats_idx]
        s_err = _rpc_error_text(stats_res)
        if s_err is not None:
            restore_err = s_err
        else:
            m = _digest_metrics({"stats": stats_res.get("stats", {})}, dps_key)
            for k in ("dps", "totalEhp", "physMaxHit", "eleMaxHit"):
                mv = m[k]
                bv = base[k]
                row[k] = mv
                if (
                    isinstance(mv, (int, float))
                    and isinstance(bv, (int, float))
                    and math.isfinite(mv)
                    and math.isfinite(bv)
                ):
                    delta = mv - bv
                    row[f"{k}Delta"] = delta
                    row[f"{k}Pct"] = (delta / abs(bv) * 100.0) if abs(bv) > 0.0 else None
                else:
                    row[f"{k}Delta"] = None
                    row[f"{k}Pct"] = None
    else:
        row["error"] = restore_err

    revert_err: str | None = None
    for r_i in range(revert_start, revert_end):
        err_txt = _rpc_error_text(batch_res[r_i])
        if err_txt is not None:
            revert_err = err_txt
            break

    if revert_err is None and probe and probe_back_idx is not None:
        pb_err = _rpc_error_text(batch_res[probe_back_idx])
        if pb_err is not None:
            revert_err = pb_err
        else:
            try:
                back = _extract_probe_state(probe, batch_res[probe_back_idx])
                if not _same_state(back, before):
                    revert_err = f"revert not applied: expected {before!r}, PoB reads {back!r}"
            except Exception as exc:
                revert_err = str(exc)

    broken_key: str | None = None
    if revert_err is not None:
        broken_key = rec["key"]
        prev = row.get("error")
        row["error"] = f"{prev}; {revert_err}" if prev else revert_err

    return row, broken_key


def attribute_water(
    worker: BridgeWorker,
    records: list[dict[str, Any]],
    dewatered_digest: dict[str, Any],
    dps_key: str,
) -> list[dict[str, Any]]:
    """Measure each stripped item's real PoB contribution, one at a time (pipelined per item)."""
    grouped = group_coupled_records(records)
    if not grouped:
        return []

    base = _digest_metrics(dewatered_digest, dps_key)
    rec_probe_rpcs, rec_probe_errors, pre_probe_results, pre_probe_batch_err = (
        _prepare_attribution_probes(worker, grouped)
    )

    rows: list[dict[str, Any]] = []
    broken_after: str | None = None

    for idx, rec in enumerate(grouped):
        if broken_after is not None:
            rows.append(
                {
                    "dimension": rec["dimension"],
                    "key": rec["key"],
                    "label": rec["label"],
                    "error": f"skipped: baseline not restored after {broken_after}",
                }
            )
            continue

        probe = rec.get("probe")
        probe_rpc = rec_probe_rpcs[idx]
        if rec_probe_errors[idx] is not None:
            rows.append(
                {
                    "dimension": rec["dimension"],
                    "key": rec["key"],
                    "label": rec["label"],
                    "error": rec_probe_errors[idx],
                }
            )
            continue

        before: Any = None
        if probe and probe_rpc is not None:
            if pre_probe_batch_err is not None:
                rows.append(
                    {
                        "dimension": rec["dimension"],
                        "key": rec["key"],
                        "label": rec["label"],
                        "error": f"cannot read state before restore: {pre_probe_batch_err}",
                    }
                )
                continue
            pre_res = pre_probe_results.get(probe_rpc)
            pre_err = _rpc_error_text(pre_res)
            if pre_err is not None:
                rows.append(
                    {
                        "dimension": rec["dimension"],
                        "key": rec["key"],
                        "label": rec["label"],
                        "error": f"cannot read state before restore: {pre_err}",
                    }
                )
                continue
            try:
                before = _extract_probe_state(probe, pre_res or {})
            except Exception as exc:
                rows.append(
                    {
                        "dimension": rec["dimension"],
                        "key": rec["key"],
                        "label": rec["label"],
                        "error": f"cannot read state before restore: {exc}",
                    }
                )
                continue
            if probe.get("kind") in ("config", "config_multi") and _same_state(before, rec.get("original")):
                continue

        row, broken_key = _measure_single_water_record(
            worker, rec, probe, probe_rpc, before, base, dps_key
        )
        if broken_key is not None:
            broken_after = broken_key
        rows.append(row)

    return rows
