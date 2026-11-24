"""Multi-objective `rank-nodes --score/--floor`: one engine `node_power` pass per stat, merged by node id."""

from __future__ import annotations

from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none, strip_pob_escapes
from ..scoring import Floor, floor_failures, pct_change, stat_keys, weighted_score
from .ranking import _detect_equipped_weapon_types, _node_weapon_mastery_kind, _safe_dist, _safe_float


def _baseline(stats: dict[str, Any], key: str) -> float | None:
    low = key.lower()
    raw = stats.get(key, next((v for k, v in stats.items() if str(k).lower() == low), None))
    val = _parse_numeric_or_none(raw)
    return float(val) if val is not None else None


def _node_power(worker: BridgeWorker, stat: str, max_dist: int) -> list[dict[str, Any]]:
    res = worker.call("node_power", {"stat": stat, "maxDepth": max_dist}, timeout=120.0)
    if not isinstance(res, dict) or res.get("error") or res.get("ok") is False:
        err = res.get("error") if isinstance(res, dict) else res
        # No silent fallback to another stat here: a mislabelled term would corrupt the score.
        raise ValueError(f"node_power cannot rank stat `{stat}`: {err or 'no report'}")
    return list(res.get("report") or [])


def rank_nodes_scored(
    worker: BridgeWorker,
    *,
    score: dict[str, float],
    floors: list[Floor],
    max_dist: int = 5,
    top: int = 15,
) -> dict[str, Any]:
    """Rank unallocated nodes by the weighted % gain over several stats, dropping nodes that break a floor."""
    keys = stat_keys(score, floors)
    with worker.lock:
        st = worker.call("get_stats", {})
        stats = (st.get("stats") if isinstance(st, dict) else None) or {}
        base = {k: _baseline(stats, k) for k in keys}
        missing = [k for k in keys if base[k] is None or (score.get(k) and base[k] == 0)]
        if missing:
            raise ValueError(f"Baseline of {', '.join(missing)} is missing or 0; it cannot be scored by percent.")
        reports = {k: {r.get("id"): r for r in _node_power(worker, k, max_dist)} for k in keys}
        equipped_weapons = _detect_equipped_weapon_types(worker)

    ids = list(dict.fromkeys(i for rep in reports.values() for i in rep))
    candidates: list[dict[str, Any]] = []
    filtered_masteries: list[str] = []
    floor_excluded = 0
    for node_id in ids:
        meta = next(rep[node_id] for rep in reports.values() if node_id in rep)
        if meta.get("allocated") or _safe_dist(meta.get("pathDist")) > max_dist:
            continue
        if equipped_weapons is not None:
            wm_kind = _node_weapon_mastery_kind(meta)
            if wm_kind is not None and wm_kind not in equipped_weapons:
                filtered_masteries.append(strip_pob_escapes(str(meta.get("name") or meta.get("nameZh") or wm_kind)))
                continue
        # A node absent from one stat's report had no effect on that stat, so its delta is 0.
        power = {k: _safe_float((reports[k].get(node_id) or {}).get("power")) for k in keys}
        path_power = {k: _safe_float((reports[k].get(node_id) or {}).get("pathPower")) for k in keys}
        pcts = {k: pct_change(base[k], power[k]) for k in keys}
        node_score = weighted_score(pcts, score)
        per_point = weighted_score({k: pct_change(base[k], path_power[k]) for k in keys}, score)
        if node_score is None or per_point is None or not (node_score > 0 or per_point > 0):
            continue
        if floor_failures({k: base[k] + power[k] for k in keys if base[k] is not None}, floors):
            floor_excluded += 1
            continue
        candidates.append(
            {
                "id": node_id,
                "name": meta.get("name"),
                "nameZh": meta.get("nameZh"),
                "type": meta.get("type"),
                "pathDist": meta.get("pathDist"),
                "score": node_score,
                "scorePerPoint": per_point,
                "pcts": {k: pcts[k] for k in score},
            }
        )

    out: dict[str, Any] = {
        "stat": "score",
        "score": score,
        "floors": [f"{k}{op}{lim:g}" for k, op, lim in floors],
        "baseline": base,
        "maxDist": max_dist,
        "totalCandidates": len(candidates),
        "floorExcluded": floor_excluded,
        "byPerPoint": sorted(candidates, key=lambda r: (r["scorePerPoint"], r["score"]), reverse=True)[:top],
        "byTotalGain": sorted(candidates, key=lambda r: (r["score"], r["scorePerPoint"]), reverse=True)[:top],
    }
    if filtered_masteries:
        out["filteredIncompatibleMasteries"] = filtered_masteries
    return out
