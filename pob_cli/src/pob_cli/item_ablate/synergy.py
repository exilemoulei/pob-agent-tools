"""Pairwise synergy ablation (`--ablate --synergy`): joint toggle-off interaction & requirement-collapse detector."""

from __future__ import annotations

import math
from itertools import combinations
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none
from ..snapshot import BuildTransaction, _finite_float, capture_direct_digest
from .deltas import (
    _EPS,
    _detect_requirement_bricks,
    _extract_resists,
    _line_switched_off,
    _main_skill_moved,
)

SYNERGY_THRESHOLD_PCT = 0.5
_ELEM_RES_CAP_KEYS: tuple[tuple[str, str], ...] = (
    ("fire", "FireResistMax"),
    ("cold", "ColdResistMax"),
    ("lightning", "LightningResistMax"),
)


def _synergy_rank_key(d: dict[str, Any]) -> tuple[Any, ...]:
    bricks = 0 if d.get("bricksRequirements") else 1
    dps_abs = abs(float(d.get("deltaDpsPct") or 0.0))
    ehp_abs = abs(float(d.get("deltaEhpPct") or 0.0))
    attr_abs = sum(abs(float(v or 0.0)) for v in (d.get("deltaAttributes") or {}).values())
    res_abs = sum(abs(float(v or 0.0)) for v in (d.get("deltaResists") or {}).values())
    ov_abs = sum(abs(float(v or 0.0)) for v in (d.get("deltaResistOverCaps") or {}).values())
    # Rank primarily by load-bearing status + |ΔDPS%| + |ΔEHP%|, with attribute/resist weight as tie-breaker
    # so dual-attribute / dual-resist lines still enter Top-K when neither alone breaks cap/req.
    impact = dps_abs + ehp_abs
    has_cap_stat = 0 if (attr_abs > 0.0 or res_abs > 0.0 or ov_abs > 0.0) else 1
    return (bricks, -round(impact, 4), has_cap_stat, -(attr_abs + res_abs + ov_abs), int(d.get("lineIndex") or 0))


def _select_top_candidates(deltas: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    k = max(2, min(8, int(top_k)))
    eligible = [
        d
        for d in deltas
        if int(d.get("lineIndex") or 0) > 0
        and not d.get("unmeasuredReason")
        and not d.get("unsupported")
    ]
    ranked = sorted(eligible, key=_synergy_rank_key)
    return ranked[:k]


def _detect_joint_resist_cap_collapse(
    base_stats: dict[str, Any],
    cand_stats: dict[str, Any],
    delta_a: dict[str, Any],
    delta_b: dict[str, Any],
) -> list[str]:
    """Detect elemental resistances that stayed capped under each single removal, but dropped below cap jointly."""
    b_res = _extract_resists(base_stats)
    c_res = _extract_resists(cand_stats)
    da_res = delta_a.get("deltaResists") or {}
    db_res = delta_b.get("deltaResists") or {}
    reasons: list[str] = []
    for elem, max_key in _ELEM_RES_CAP_KEYS:
        cap = _finite_float(base_stats.get(max_key), default=75.0)
        if cap <= 0.0:
            cap = 75.0
        if b_res[elem] < cap - 0.5:
            continue
        single_a_ok = (b_res[elem] + float(da_res.get(elem) or 0.0)) >= cap - 0.5
        single_b_ok = (b_res[elem] + float(db_res.get(elem) or 0.0)) >= cap - 0.5
        cand_cap = _finite_float(cand_stats.get(max_key), default=cap)
        if cand_cap <= 0.0:
            cand_cap = cap
        if single_a_ok and single_b_ok and c_res[elem] < cand_cap - 0.5:
            reasons.append(f"{elem.title()}Res {round(c_res[elem])}% < {round(cand_cap)}% cap")
    return reasons


def evaluate_pairwise_synergies(
    worker: BridgeWorker,
    tx: BuildTransaction,
    *,
    item_id: int,
    slot_active: bool,
    base_digest: dict[str, Any],
    eval_base_digest: dict[str, Any],
    deltas: list[dict[str, Any]],
    synergy_top: int = 5,
) -> list[dict[str, Any]]:
    """Toggle off all C(K, 2) pairs of the top-K mod lines inside the active transaction and measure interaction."""
    if not slot_active:
        return []
    candidates = _select_top_candidates(deltas, synergy_top)
    if len(candidates) < 2:
        return []

    b_stats = eval_base_digest.get("stats") or {}
    base_warns = set(eval_base_digest.get("warnings") or [])
    base_header = base_digest.get("buildHeader")
    primary_key = str(eval_base_digest.get("primaryDpsKey") or "CombinedDPS")
    base_dps = _finite_float(b_stats.get(primary_key))
    base_ehp_num = _parse_numeric_or_none(b_stats.get("TotalEHP"))
    base_ehp_finite = base_ehp_num is not None and math.isfinite(base_ehp_num)

    pairs_out: list[dict[str, Any]] = []
    for da, db in combinations(candidates, 2):
        if int(da["lineIndex"]) > int(db["lineIndex"]):
            da, db = db, da
        idx_a = int(da["lineIndex"])
        idx_b = int(db["lineIndex"])
        edit_open = False
        cand_digest: dict[str, Any] | None = None
        skipped_reason: str | None = None
        try:
            worker.call("item_edit_begin", {"id": item_id})
            edit_open = True
            worker.call("item_edit_set", {"modLine": {"index": idx_a, "enabled": False}})
            set_b = worker.call("item_edit_set", {"modLine": {"index": idx_b, "enabled": False}})
            if not (_line_switched_off(set_b, idx_a) and _line_switched_off(set_b, idx_b)):
                skipped_reason = "toggleIgnored"
            else:
                worker.call("item_edit_commit", {"equip": True})
                edit_open = False
                if _main_skill_moved(base_header, worker.call("get_build_header", {})):
                    skipped_reason = "mainSkillShifted"
                else:
                    cand_digest = capture_direct_digest(worker, base_digest=base_digest)
        finally:
            if edit_open:
                try:
                    worker.call("item_edit_cancel", {})
                except Exception:
                    pass
            tx.restore(rebuild=False)

        if skipped_reason or cand_digest is None:
            continue

        c_stats = cand_digest.get("stats") or {}
        cand_dps = _finite_float(c_stats.get(primary_key))
        joint_dps = round(cand_dps - base_dps, 2)
        joint_dps_pct = round(((cand_dps - base_dps) / base_dps * 100.0) if abs(base_dps) > _EPS else 0.0, 3)
        add_dps_pct = round(float(da.get("deltaDpsPct") or 0.0) + float(db.get("deltaDpsPct") or 0.0), 3)
        syn_dps_pct = round(joint_dps_pct - add_dps_pct, 3)

        cand_ehp_num = _parse_numeric_or_none(c_stats.get("TotalEHP"))
        both_ehp_finite = (
            base_ehp_finite
            and cand_ehp_num is not None
            and math.isfinite(cand_ehp_num)
            and da.get("deltaEhpPct") is not None
            and db.get("deltaEhpPct") is not None
        )
        joint_ehp: float | None = None
        joint_ehp_pct: float | None = None
        add_ehp_pct: float | None = None
        syn_ehp_pct: float | None = None
        if both_ehp_finite and base_ehp_num is not None and cand_ehp_num is not None:
            raw_j = cand_ehp_num - base_ehp_num
            joint_ehp = round(raw_j, 2)
            joint_ehp_pct = round((raw_j / base_ehp_num * 100.0) if abs(base_ehp_num) > _EPS else 0.0, 3)
            add_ehp_pct = round(float(da["deltaEhpPct"]) + float(db["deltaEhpPct"]), 3)
            syn_ehp_pct = round(joint_ehp_pct - add_ehp_pct, 3)

        joint_bricks, brick_reasons = _detect_requirement_bricks(
            b_stats, c_stats, base_warns, set(cand_digest.get("warnings") or [])
        )
        res_collapse_reasons = _detect_joint_resist_cap_collapse(b_stats, c_stats, da, db)
        neither_single_bricks = (not da.get("bricksRequirements")) and (not db.get("bricksRequirements"))
        collapse_reasons: list[str] = []
        if joint_bricks and neither_single_bricks:
            collapse_reasons.extend(brick_reasons)
        collapse_reasons.extend(res_collapse_reasons)
        joint_req_collapse = bool(collapse_reasons)

        non_trivial = abs(syn_dps_pct) >= SYNERGY_THRESHOLD_PCT or (
            syn_ehp_pct is not None and abs(syn_ehp_pct) >= SYNERGY_THRESHOLD_PCT
        )
        entry: dict[str, Any] = {
            "lineIndexA": idx_a,
            "lineIndexB": idx_b,
            "modTextA": da.get("modText") or "",
            "modTextB": db.get("modText") or "",
            "jointDpsDelta": joint_dps,
            "jointDpsDeltaPct": joint_dps_pct,
            "additiveDpsDeltaPct": add_dps_pct,
            "synergyDpsPct": syn_dps_pct,
            "jointEhpDelta": joint_ehp,
            "jointEhpDeltaPct": joint_ehp_pct,
            "additiveEhpDeltaPct": add_ehp_pct,
            "synergyEhpPct": syn_ehp_pct,
            "nonTrivialSynergy": non_trivial,
            "jointReqCollapse": joint_req_collapse,
        }
        if collapse_reasons:
            entry["jointCollapseReasons"] = collapse_reasons
        pairs_out.append(entry)

    pairs_out.sort(
        key=lambda r: (
            0 if r["jointReqCollapse"] else 1,
            0 if r["nonTrivialSynergy"] else 1,
            -round(abs(float(r["synergyDpsPct"] or 0.0)) + abs(float(r["synergyEhpPct"] or 0.0)), 4),
            int(r["lineIndexA"]),
            int(r["lineIndexB"]),
        )
    )
    return pairs_out
