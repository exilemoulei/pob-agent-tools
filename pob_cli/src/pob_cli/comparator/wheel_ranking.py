"""Wheel-level batch CoW ROI ranking (`pob tree rank-wheels`), hard vetoes, and `[Proxy Benchmark]` isolation."""

from __future__ import annotations

import math
import time
from typing import Any

from ..analyzer.tree_sockets import enrich_tree_sockets
from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..game_data.tree_graph import PassiveGraph, WheelInfo, build_passive_graph
from ..snapshot import _call_batch
from .wheel_packages import build_wheel_packages, extract_used_mastery_effect_ids
from .wheel_proxy import prepare_proxy_socket_candidates
from .wheel_tags import BuildProfile, extract_build_profile

_EVAL_BATCH_SIZE = 64


def _num(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    try:
        f = float(str(val).strip())
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def check_wheel_veto_and_notes(
    base_stats: dict[str, Any],
    cand_stats: dict[str, Any],
    pkg: dict[str, Any],
    graph: PassiveGraph,
    profile: BuildProfile,
    dps_pct: float,
    sort_by: str,
) -> tuple[str | None, list[str]]:
    """Apply hard vetoes (`CoC overspeed / negative DPS`, `ManaUnreserved < 0`, `Agnostic ES > 0`) and threshold notes."""
    notes: list[str] = []

    base_mu = _num(base_stats.get("ManaUnreserved"))
    new_mu = _num(cand_stats.get("ManaUnreserved"))
    if new_mu is not None and new_mu < 0.0 and (base_mu is None or base_mu >= 0.0 or new_mu < base_mu - 0.5):
        return f"[VETO: ManaUnreserved < 0 ({new_mu:.0f})]", notes

    base_tr = _num(base_stats.get("SkillTriggerRate"))
    new_tr = _num(cand_stats.get("SkillTriggerRate"))
    new_src = _num(cand_stats.get("EffectiveSourceRate"))
    new_cap = _num(cand_stats.get("TriggerRateCap"))
    base_src = _num(base_stats.get("EffectiveSourceRate"))
    base_cap = _num(base_stats.get("TriggerRateCap"))
    if (base_tr is not None and new_tr is not None and base_tr > 0 and new_tr < base_tr * 0.90) or (
        new_src is not None
        and new_cap is not None
        and new_cap > 0
        and new_src > new_cap * 1.01
        and (base_src is None or base_cap is None or base_src <= base_cap * 1.01)
    ):
        return "[VETO: CoC overspeed / negative DPS]", notes

    if profile.is_agnostic:
        stat_blob = " ".join(
            " ".join(str(s) for s in ((graph.nodes.get(n) or {}).get("stats") or []))
            for n in pkg["nodeIds"]
        ).lower()
        if "energy shield" in stat_blob and "maximum life" not in stat_blob and "mana" not in stat_blob and dps_pct <= 0.1:
            return "[VETO: Agnostic ES > 0]", notes

    if dps_pct < -0.5 and sort_by == "dps":
        return f"[VETO: CoC overspeed / negative DPS ({dps_pct:+.2f}%)]", notes

    base_supp = _num(base_stats.get("SpellSuppressionChance")) or 0.0
    new_supp = _num(cand_stats.get("SpellSuppressionChance")) or 0.0
    if new_supp > base_supp + 0.5 and base_supp < 100.0:
        cap_tag = " -> 100% CAP!" if new_supp >= 100.0 else f" ({new_supp:.0f}%)"
        notes.append(f"+{new_supp - base_supp:.0f}% Supp{cap_tag}")
    for elem in ("Fire", "Cold", "Lightning"):
        br = _num(base_stats.get(f"{elem}Resist"))
        nr = _num(cand_stats.get(f"{elem}Resist"))
        if br is not None and nr is not None and br < 75.0 and nr > br + 0.5:
            notes.append(f"+{nr - br:.0f}% {elem}Res")
    if new_mu is not None and base_mu is not None and new_mu > base_mu + 15.0:
        notes.append(f"+{new_mu - base_mu:.0f} Unres Mana")

    return None, notes


def _eval_batched(
    worker: BridgeWorker,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], float]:
    base_stats: dict[str, Any] = {}
    results_by_key: dict[str, dict[str, Any]] = {}
    total_lua_ms = 0.0
    for i in range(0, len(candidates), _EVAL_BATCH_SIZE):
        chunk = candidates[i : i + _EVAL_BATCH_SIZE]
        res = worker.call("eval_node_paths", {"candidates": chunk}, timeout=60.0)
        if not base_stats and isinstance(res.get("baseStats"), dict):
            base_stats = res["baseStats"]
        total_lua_ms += float(res.get("elapsedMs") or 0.0)
        for row in res.get("results") or []:
            if isinstance(row, dict) and row.get("key"):
                results_by_key[str(row["key"])] = row
    return base_stats, results_by_key, total_lua_ms


def _format_alloc_spec(pkg: dict[str, Any]) -> str:
    m_id = pkg.get("masteryId")
    m_eff = pkg.get("masteryEffect") or {}
    m_eid = m_eff.get("effectId")
    return ",".join(
        f"{nid}:{m_eid}" if (m_id is not None and nid == m_id and m_eid is not None) else str(nid)
        for nid in pkg["nodeIds"]
    )


def _assemble_proxy_benchmarks(
    proxy_meta: list[dict[str, Any]],
    results_by_key: dict[str, dict[str, Any]],
    *,
    dps_key: str,
    base_dps: float,
    base_ehp: float,
    sort_norm: str,
    target_flag: str = "",
) -> tuple[list[dict[str, Any]], int]:
    proxy_benchmarks: list[dict[str, Any]] = []
    proxy_err_count = 0
    for pm in proxy_meta:
        ev = results_by_key.get(pm["key"])
        if not ev:
            continue
        if ev.get("error"):
            proxy_err_count += 1
            continue
        out_st = ev.get("outputStats") or {}
        cand_dps = _num(out_st.get(dps_key))
        if cand_dps is None:
            cand_dps = _num(out_st.get("CombinedDPS")) or _num(out_st.get("TotalDPS")) or base_dps
        cand_ehp = _num(out_st.get("TotalEHP"))
        if cand_ehp is None:
            cand_ehp = base_ehp
        pts = max(1, int(pm["assumedPoints"]))
        dps_pct = ((cand_dps - base_dps) / base_dps * 100.0) if base_dps > 0.0 else 0.0
        ehp_pct = ((cand_ehp - base_ehp) / base_ehp * 100.0) if base_ehp > 0.0 else 0.0
        path_csv = ",".join(str(x) for x in (pm["pathIds"] or [pm["nodeId"]]))
        proxy_benchmarks.append(
            {
                "proxyBenchmark": True,
                "nodeId": pm["nodeId"],
                "kind": pm["kind"],
                "sector": pm.get("sector"),
                "travelCost": pm["travelCost"],
                "assumedPoints": pts,
                "pointsBreakdown": pm["pointsBreakdown"],
                "pathIds": pm["pathIds"],
                "pathCommand": f"pob tree change{target_flag} --allocate {path_csv}",
                "templateSummary": pm["templateSummary"],
                "dpsPct": round(dps_pct, 3),
                "dpsPctPerPt": round(dps_pct / pts, 3),
                "ehpPct": round(ehp_pct, 3),
                "ehpPctPerPt": round(ehp_pct / pts, 3),
            }
        )
    proxy_benchmarks.sort(
        key=lambda p: (p["dpsPctPerPt"], p["ehpPctPerPt"]) if sort_norm == "dps" else (p["ehpPctPerPt"], p["dpsPctPerPt"]),
        reverse=True,
    )
    return proxy_benchmarks, proxy_err_count


def _score_and_filter_packages(
    packages: list[dict[str, Any]],
    results_by_key: dict[str, dict[str, Any]],
    *,
    base_stats: dict[str, Any],
    graph: PassiveGraph,
    profile: BuildProfile,
    dps_key: str,
    base_dps: float,
    base_ehp: float,
    sort_norm: str,
    target_flag: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    best_per_wheel: dict[str, dict[str, Any]] = {}
    vetoed_list: list[dict[str, Any]] = []
    eval_err_count = 0

    for pkg in packages:
        ev = results_by_key.get(pkg["key"])
        if not ev:
            continue
        if ev.get("error"):
            eval_err_count += 1
            continue
        out_st = ev.get("outputStats") or {}
        cand_dps = _num(out_st.get(dps_key))
        if cand_dps is None:
            cand_dps = _num(out_st.get("CombinedDPS")) or _num(out_st.get("TotalDPS")) or base_dps
        cand_ehp = _num(out_st.get("TotalEHP"))
        if cand_ehp is None:
            cand_ehp = base_ehp

        dps_delta, ehp_delta = cand_dps - base_dps, cand_ehp - base_ehp
        dps_pct = (dps_delta / base_dps * 100.0) if base_dps > 0.0 else 0.0
        ehp_pct = (ehp_delta / base_ehp * 100.0) if base_ehp > 0.0 else 0.0
        pts = max(1, int(pkg["totalPoints"]))
        dps_pt, ehp_pt = dps_pct / pts, ehp_pct / pts

        veto_reason, notes = check_wheel_veto_and_notes(base_stats, out_st, pkg, graph, profile, dps_pct, sort_norm)
        winfo: WheelInfo = pkg["wheel"]
        m_id, m_eff = pkg.get("masteryId"), pkg.get("masteryEffect")
        alloc_csv = _format_alloc_spec(pkg)
        rec: dict[str, Any] = {
            "wheelId": pkg["wheelId"],
            "wheelName": winfo.name,
            "wheelNameZh": winfo.name_zh,
            "travelCost": pkg["travelCost"],
            "wheelCost": pkg["wheelCost"],
            "totalPoints": pts,
            "nodeIds": list(pkg["nodeIds"]),
            "travelIds": list(pkg["travelIds"]),
            "mastery": (
                {
                    "nodeId": m_id,
                    "effectId": m_eff["effectId"],
                    "allocSpec": f"{m_id}:{m_eff['effectId']}",
                    "stats": m_eff.get("stats") or [],
                    "statsZh": m_eff.get("statsZh"),
                }
                if (m_id is not None and isinstance(m_eff, dict))
                else None
            ),
            "dpsDelta": round(dps_delta, 2),
            "dpsPct": round(dps_pct, 3),
            "dpsPctPerPt": round(dps_pt, 3),
            "ehpDelta": round(ehp_delta, 2),
            "ehpPct": round(ehp_pct, 3),
            "ehpPctPerPt": round(ehp_pt, 3),
            "notes": notes,
            "vetoReason": veto_reason,
            "allocateSpec": alloc_csv,
            "allocateCommand": f"pob tree change{target_flag} --allocate {alloc_csv}",
        }
        if veto_reason:
            vetoed_list.append(rec)
            continue

        wid = pkg["wheelId"]
        primary, secondary = (dps_pt, ehp_pt) if sort_norm == "dps" else (ehp_pt, dps_pt)
        prev = best_per_wheel.get(wid)
        if prev is None or (primary, secondary, -pts) > (
            prev["dpsPctPerPt"] if sort_norm == "dps" else prev["ehpPctPerPt"],
            prev["ehpPctPerPt"] if sort_norm == "dps" else prev["dpsPctPerPt"],
            -prev["totalPoints"],
        ):
            best_per_wheel[wid] = rec

    ranked = sorted(
        best_per_wheel.values(),
        key=lambda r: (
            (r["dpsPctPerPt"], r["ehpPctPerPt"], -r["totalPoints"])
            if sort_norm == "dps"
            else (r["ehpPctPerPt"], r["dpsPctPerPt"], -r["totalPoints"])
        ),
        reverse=True,
    )
    return ranked, vetoed_list, eval_err_count


def rank_wheels(
    worker: BridgeWorker,
    *,
    top: int = 12,
    max_dist: int = 12,
    max_pts: int = 10,
    sort_by: str = "dps",
    all_tags: bool = False,
    include_proxy_sockets: bool = True,
    target_slot: str = "main",
) -> dict[str, Any]:
    """Aggregate reachable passive wheels, evaluate via batch CoW `eval_node_paths`, and rank by per-point ROI (<2s)."""
    t0 = time.perf_counter()
    sort_norm = (sort_by or "dps").strip().lower()
    if sort_norm not in ("dps", "ehp"):
        raise ValueError(f"Invalid sort mode {sort_by!r}; expected 'dps' or 'ehp'.")
    top_n = max(1, int(top))
    eff_max_dist = max(1, min(25, int(max_dist)))
    eff_max_pts = max(1, min(20, int(max_pts)))

    with worker.lock:
        tree_state, items_res, skills_res, stats_res = _call_batch(
            worker,
            [("get_tree_state", {}), ("list_items", {}), ("list_skills", {}), ("get_stats", {})],
        )
        nodes_db = worker.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
        dynamic_nodes = tree_state.get("dynamicNodes") or []
        dynamic_map = {str(n["id"]): n for n in dynamic_nodes if isinstance(n, dict) and "id" in n}
        overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
        alloc_set = {int(x) for x in tree_state.get("allocatedNodes", [])}

        used_mastery_effects = extract_used_mastery_effect_ids(worker, tree_state, nodes_db)
        graph = build_passive_graph(nodes_db, dynamic_nodes, overrides)
        alloc_ks_names = {
            strip_pob_escapes(str((graph.nodes.get(n) or {}).get("name") or ""))
            for n in alloc_set
            if graph.node_type(n) == "Keystone"
        }
        profile = extract_build_profile(
            (stats_res or {}).get("stats") or {},
            skills_res=skills_res,
            items_res=items_res,
            allocated_keystone_names=alloc_ks_names,
        )

        t_agg0 = time.perf_counter()
        packages, total_wheels, filtered_wheels = build_wheel_packages(
            graph,
            alloc_set,
            used_mastery_effects,
            profile,
            max_dist=eff_max_dist,
            max_pts=eff_max_pts,
            all_tags=all_tags,
        )
        agg_ms = round((time.perf_counter() - t_agg0) * 1000.0, 2)

        eval_cands: list[dict[str, Any]] = [
            {
                "key": p["key"],
                "mode": "add",
                "nodeIds": p["nodeIds"],
                **({"masteryEffects": p["masteryEffects"]} if p.get("masteryEffects") else {}),
            }
            for p in packages
        ]
        proxy_meta: list[dict[str, Any]] = []
        if include_proxy_sockets:
            _, _, nearby_sockets = enrich_tree_sockets(tree_state, items_res, nodes_db, dynamic_map, alloc_set)
            p_eval, proxy_meta = prepare_proxy_socket_candidates(nearby_sockets, profile)
            eval_cands.extend(p_eval)

        base_stats, results_by_key, lua_ms = (
            _eval_batched(worker, eval_cands) if eval_cands else ((stats_res or {}).get("stats") or {}, {}, 0.0)
        )

    use_full = (_num(base_stats.get("FullDPS")) or 0.0) > 0.0
    dps_key = "FullDPS" if use_full else "CombinedDPS"
    base_dps = _num(base_stats.get(dps_key)) or _num(base_stats.get("CombinedDPS")) or _num(base_stats.get("TotalDPS")) or 0.0
    base_ehp = _num(base_stats.get("TotalEHP")) or 0.0

    slot_norm = str(target_slot or "main").strip().lower() or "main"
    target_flag = f" --target {slot_norm}" if slot_norm != "main" else ""
    ranked, vetoed_list, eval_err_count = _score_and_filter_packages(
        packages,
        results_by_key,
        base_stats=base_stats,
        graph=graph,
        profile=profile,
        dps_key=dps_key,
        base_dps=base_dps,
        base_ehp=base_ehp,
        sort_norm=sort_norm,
        target_flag=target_flag,
    )
    positive_ranked = [r for r in ranked if (r["dpsPctPerPt"] > 0.0 if sort_norm == "dps" else r["ehpPctPerPt"] > 0.0)]
    shown_wheels = (positive_ranked or ranked)[:top_n]

    out: dict[str, Any] = {
        "dpsKey": dps_key,
        "baseDps": round(base_dps, 2),
        "baseEhp": round(base_ehp, 2),
        "sort": sort_norm,
        "maxDist": eff_max_dist,
        "maxPts": eff_max_pts,
        "allTags": bool(all_tags),
        "buildTags": sorted(profile.tags),
        "totalWheelsScanned": total_wheels,
        "tagFilteredWheels": filtered_wheels,
        "evaluatedPackages": len(packages),
        "wheels": shown_wheels,
        "vetoedWheels": vetoed_list,
        "aggregationMs": agg_ms,
        "luaEvalMs": round(lua_ms, 2),
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 2),
    }
    if include_proxy_sockets:
        proxies, proxy_errs = _assemble_proxy_benchmarks(
            proxy_meta,
            results_by_key,
            dps_key=dps_key,
            base_dps=base_dps,
            base_ehp=base_ehp,
            sort_norm=sort_norm,
            target_flag=target_flag,
        )
        out["proxySocketBenchmarks"] = proxies
        eval_err_count += proxy_errs
    if eval_err_count:
        out["evalErrorCount"] = eval_err_count
    return out
