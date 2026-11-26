"""Passive tree branch pruning analyzer (`pob tree prune`) using Tarjan graph + batch CoW `eval_node_paths`."""

from __future__ import annotations

import math
import time
from typing import Any

from ..bridge_client import BridgeWorker
from ..game_data.tree_graph import PrunableBranch, build_passive_graph, find_prunable_branches
from ..snapshot import _call_batch
from .tree_sockets import enrich_tree_sockets

_DEFAULT_MIN_EVAL_CANDIDATES = 36


def _prioritize_prune_branches(branches: list[PrunableBranch], limit: int) -> list[PrunableBranch]:
    """Prioritize empty sockets, leaf masteries, distinct dead-end wheels, and short 1-3pt twigs within `limit`."""
    if len(branches) <= limit:
        return branches

    empty_socks: list[PrunableBranch] = []
    masteries: list[PrunableBranch] = []
    wheel_cands: list[PrunableBranch] = []
    multi_pt_twigs: list[PrunableBranch] = []
    one_pt_leaves: list[PrunableBranch] = []
    rest: list[PrunableBranch] = []

    for br in branches:
        if br.kind == "empty_socket" or br.includes_empty_socket_ids:
            empty_socks.append(br)
        elif br.kind == "mastery":
            masteries.append(br)
        elif br.kind in ("wheel", "wheel_spur"):
            wheel_cands.append(br)
        elif br.kind == "twig" and 2 <= br.points <= 3:
            multi_pt_twigs.append(br)
        elif br.kind in ("twig", "leaf") and br.points <= 3:
            one_pt_leaves.append(br)
        else:
            rest.append(br)

    # Pick one representative (fewest points) per distinct dead-end wheel before secondary sub-branches.
    wheel_cands.sort(key=lambda b: (0 if b.kind == "wheel" else 1, b.points, b.node_ids))
    distinct_wheels: list[PrunableBranch] = []
    extra_wheels: list[PrunableBranch] = []
    seen_wheels: set[str] = set()
    for br in wheel_cands:
        wkey = br.wheel_name or br.key
        if wkey not in seen_wheels:
            seen_wheels.add(wkey)
            distinct_wheels.append(br)
        else:
            extra_wheels.append(br)

    ordered = empty_socks + masteries + distinct_wheels + multi_pt_twigs + one_pt_leaves + extra_wheels + rest
    return ordered[:limit]


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


def evaluate_prune_guardrails(
    base_stats: dict[str, Any],
    cand_stats: dict[str, Any],
) -> dict[str, Any]:
    """Check attribute requirements, elemental resist caps, mana/life reservation, and hit/trigger cliffs."""
    violations: list[str] = []
    details: dict[str, Any] = {}

    # 1. Attributes: newStr >= ReqStr, newDex >= ReqDex, newInt >= ReqInt.
    attr_breaks: dict[str, dict[str, int]] = {}
    for attr in ("Str", "Dex", "Int"):
        cur = _num(base_stats.get(attr))
        new = _num(cand_stats.get(attr))
        req = _num(cand_stats.get(f"Req{attr}"))
        if req is None:
            req = _num(base_stats.get(f"Req{attr}"))
        if new is not None and req is not None and req > 0 and new < req:
            # Only blame this branch if it creates or worsens a deficit.
            cur_def = (cur - req) if cur is not None else 0.0
            new_def = new - req
            if cur is None or cur >= req or new_def < cur_def - 0.5:
                deficit_i = round(new_def)
                attr_breaks[attr] = {
                    "after": round(new),
                    "required": round(req),
                    "deficit": deficit_i,
                }
                violations.append(f"{attr} {deficit_i} deficit")
    if attr_breaks:
        details["attributes"] = attr_breaks

    # 2. Elemental resists remain capped (do not drop below min(75%, currentRes) or lose cap).
    res_breaks: dict[str, dict[str, float]] = {}
    for elem in ("Fire", "Cold", "Lightning"):
        cur_r = _num(base_stats.get(f"{elem}Resist"))
        new_r = _num(cand_stats.get(f"{elem}Resist"))
        cur_max = _num(base_stats.get(f"{elem}ResistMax")) or 75.0
        new_max = _num(cand_stats.get(f"{elem}ResistMax")) or cur_max
        if cur_r is not None and new_r is not None:
            floor_r = min(75.0, cur_r)
            dropped_below_75 = new_r < floor_r - 0.5
            dropped_below_cap = (cur_r >= cur_max - 0.5) and (new_r < new_max - 0.5)
            if dropped_below_75 or dropped_below_cap:
                target_cap = new_max if dropped_below_cap else floor_r
                res_breaks[elem] = {"before": round(cur_r, 1), "after": round(new_r, 1), "cap": round(target_cap, 1)}
                violations.append(f"{elem}Res {new_r:.0f}%<{target_cap:.0f}%")
    if res_breaks:
        details["resists"] = res_breaks

    # 3. Reservation: ManaUnreserved >= 0 (and >= ManaCost if previously satisfied) & LifeUnreserved > 0.
    base_mu = _num(base_stats.get("ManaUnreserved"))
    new_mu = _num(cand_stats.get("ManaUnreserved"))
    base_cost = _num(base_stats.get("ManaCost"))
    raw_cand_cost = cand_stats.get("ManaCost")
    cand_cost = _num(raw_cand_cost) if raw_cand_cost is not None else base_cost
    if new_mu is not None:
        if new_mu < 0.0 and (base_mu is None or base_mu >= 0.0 or new_mu < base_mu - 0.5):
            details["manaUnreserved"] = {"before": base_mu, "after": round(new_mu, 1)}
            violations.append(f"ManaUnreserved {new_mu:.0f}<0")
        elif (
            cand_cost is not None
            and cand_cost > 0.0
            and base_mu is not None
            and base_mu >= (base_cost or 0.0)
            and new_mu < cand_cost
        ):
            details["manaUnreserved"] = {"before": base_mu, "after": round(new_mu, 1), "manaCost": cand_cost}
            violations.append(f"ManaUnreserved {new_mu:.0f}<Cost {cand_cost:.0f}")

    base_lu = _num(base_stats.get("LifeUnreserved"))
    new_lu = _num(cand_stats.get("LifeUnreserved"))
    if new_lu is not None and new_lu <= 0.0 and (base_lu is None or base_lu > 0.0):
        details["lifeUnreserved"] = {"before": base_lu, "after": round(new_lu, 1)}
        violations.append(f"LifeUnreserved {new_lu:.0f}<=0")

    # 4. HitChance & Trigger cliff check.
    base_hit = _num(base_stats.get("HitChance"))
    new_hit = _num(cand_stats.get("HitChance"))
    if base_hit is not None and new_hit is not None and base_hit >= 99.5 and new_hit < 99.5 and (base_hit - new_hit) >= 0.5:
        details["hitChance"] = {"before": round(base_hit, 2), "after": round(new_hit, 2)}
        violations.append(f"HitChance {base_hit:.1f}%->{new_hit:.1f}%")

    base_tr = _num(base_stats.get("SkillTriggerRate"))
    new_tr = _num(cand_stats.get("SkillTriggerRate"))
    if base_tr is not None and new_tr is not None and base_tr > 0 and new_tr < base_tr * 0.85:
        details["triggerRate"] = {"before": round(base_tr, 2), "after": round(new_tr, 2)}
        violations.append(f"TriggerRate cliff {base_tr:.2f}->{new_tr:.2f}/s")

    return {
        "safe": len(violations) == 0,
        "violations": violations,
        "details": details,
    }


def _branch_label(br: PrunableBranch, nodes: dict[int, dict[str, Any]]) -> tuple[str, str | None]:
    if br.kind == "empty_socket":
        return f"Empty Jewel Socket ({br.node_ids[0]})", f"空珠宝槽 ({br.node_ids[0]})"
    if br.wheel_name:
        if br.notable_names:
            n_en = ", ".join(br.notable_names[:2])
            n_zh = ", ".join(br.notable_names_zh[:2]) if br.notable_names_zh else None
            lbl_en = f"{br.wheel_name}: {n_en}" if n_en != br.wheel_name else br.wheel_name
            lbl_zh = f"{br.wheel_name_zh}: {n_zh}" if (br.wheel_name_zh and n_zh and n_zh != br.wheel_name_zh) else (br.wheel_name_zh or n_zh)
            return lbl_en, lbl_zh
        return f"{br.wheel_name} ({br.points}pt)", f"{br.wheel_name_zh} ({br.points}点)" if br.wheel_name_zh else None
    names_en: list[str] = []
    names_zh: list[str] = []
    for nid in br.node_ids[:3]:
        nd = nodes.get(nid) or {}
        names_en.append(str(nd.get("name") or nid))
        if nd.get("nameZh"):
            names_zh.append(str(nd["nameZh"]))
    return " + ".join(names_en), (" + ".join(names_zh) if names_zh else None)


def _sort_key_for_branch(entry: dict[str, Any], sort_by: str) -> tuple[Any, ...]:
    is_empty = 0 if entry.get("kind") == "empty_socket" else 1
    dps_pt = float(entry.get("dpsPctPerPt") or 0.0)
    ehp_pt = float(entry.get("ehpPctPerPt") or 0.0)
    pts = int(entry.get("points") or 1)
    # Least loss (highest signed Δ%/pt; negative loss = positive gain) sorts first!
    if sort_by == "ehp":
        return (-round(ehp_pt, 6), is_empty, -round(dps_pt, 6), -pts, entry.get("nodeIds") or [])
    if sort_by == "pts":
        return (-pts, is_empty, -round(dps_pt, 6), -round(ehp_pt, 6), entry.get("nodeIds") or [])
    return (-round(dps_pt, 6), is_empty, -round(ehp_pt, 6), -pts, entry.get("nodeIds") or [])


def prune_tree(
    worker: BridgeWorker,
    *,
    top: int = 15,
    max_branch_pts: int = 5,
    sort_by: str = "dps",
    include_unsafe: bool = False,
    include_occupied_sockets: bool = False,
    target_slot: str = "main",
) -> dict[str, Any]:
    """Identify and rank connectivity-preserving prunable passive branches with safety guardrails (<0.5s)."""
    t0 = time.perf_counter()
    sort_norm = (sort_by or "dps").strip().lower()
    if sort_norm not in ("dps", "ehp", "pts"):
        raise ValueError(f"Invalid sort mode {sort_by!r}; expected 'dps', 'ehp', or 'pts'.")
    max_pts = max(1, min(12, int(max_branch_pts)))
    top_n = max(1, int(top))

    with worker.lock:
        tree_state, items_res = _call_batch(worker, [("get_tree_state", {}), ("list_items", {})])
        tree_version = tree_state.get("treeVersion")
        nodes_db = worker.get_cached_tree_data(tree_version).get("nodes", {})
        dynamic_nodes = tree_state.get("dynamicNodes") or []
        dynamic_map = {str(n["id"]): n for n in dynamic_nodes if isinstance(n, dict) and "id" in n}
        overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
        alloc_ids = [int(x) for x in tree_state.get("allocatedNodes", [])]
        alloc_set = set(alloc_ids)

        jewel_sockets, socket_summary, _ = enrich_tree_sockets(
            tree_state, items_res, nodes_db, dynamic_map, alloc_set
        )
        occ_socket_ids = {
            int(s["nodeId"])
            for s in jewel_sockets
            if s.get("allocated") and s.get("occupied") and s.get("nodeId") is not None
        }
        empty_socket_ids = {int(x) for x in (socket_summary.get("allocatedEmptyIds") or [])}

        graph = build_passive_graph(nodes_db, dynamic_nodes, overrides)
        branches = find_prunable_branches(
            graph,
            alloc_set,
            occupied_socket_ids=occ_socket_ids,
            empty_socket_ids=empty_socket_ids,
            max_branch_pts=max_pts,
            include_occupied_sockets=include_occupied_sockets,
        )

        # Bound batch size to keep 123pt trees under <0.9s while scaling automatically with `--top`.
        eval_cap = max(_DEFAULT_MIN_EVAL_CANDIDATES, top_n * 2)
        eval_branches = _prioritize_prune_branches(branches, eval_cap)
        eval_payload = [
            {"key": br.key, "mode": "remove", "nodeIds": list(br.node_ids)}
            for br in eval_branches
        ]
        if eval_payload:
            eval_res = worker.call("eval_node_paths", {"candidates": eval_payload}, timeout=60.0)
        else:
            eval_res = {"baseStats": {}, "results": [], "useFullDPS": False}

    base_stats = eval_res.get("baseStats") or {}
    use_full = bool(eval_res.get("useFullDPS")) or ((_num(base_stats.get("FullDPS")) or 0.0) > 0.0)
    dps_key = "FullDPS" if use_full else "CombinedDPS"
    base_dps = _num(base_stats.get(dps_key)) or _num(base_stats.get("CombinedDPS")) or _num(base_stats.get("TotalDPS")) or 0.0
    base_ehp = _num(base_stats.get("TotalEHP")) or 0.0

    res_by_key = {str(r.get("key") or ""): r for r in (eval_res.get("results") or []) if isinstance(r, dict)}
    all_candidates: list[dict[str, Any]] = []
    eval_err_count = 0
    slot_norm = str(target_slot or "main").strip().lower() or "main"
    target_flag = f" --target {slot_norm}" if slot_norm != "main" else ""

    for br in eval_branches:
        ev = res_by_key.get(br.key) or {}
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

        dps_delta = cand_dps - base_dps
        ehp_delta = cand_ehp - base_ehp
        dps_pct = (dps_delta / base_dps * 100.0) if base_dps > 0.0 else 0.0
        ehp_pct = (ehp_delta / base_ehp * 100.0) if base_ehp > 0.0 else 0.0
        pts = max(1, br.points)

        guardrails = evaluate_prune_guardrails(base_stats, out_st)
        lbl_en, lbl_zh = _branch_label(br, graph.nodes)
        ids_csv = ",".join(str(x) for x in br.node_ids)

        all_candidates.append(
            {
                "key": br.key,
                "kind": br.kind,
                "nodeIds": list(br.node_ids),
                "points": pts,
                "label": lbl_en,
                "labelZh": lbl_zh,
                "wheel": br.wheel_name,
                "wheelZh": br.wheel_name_zh,
                "notables": list(br.notable_names),
                "notablesZh": list(br.notable_names_zh),
                "includesMasteryIds": list(br.includes_mastery_ids),
                "includesEmptySocketIds": list(br.includes_empty_socket_ids),
                "dpsDelta": round(dps_delta, 2),
                "dpsPct": round(dps_pct, 3),
                "dpsPctPerPt": round(dps_pct / pts, 3),
                "ehpDelta": round(ehp_delta, 2),
                "ehpPct": round(ehp_pct, 3),
                "ehpPctPerPt": round(ehp_pct / pts, 3),
                "safe": guardrails["safe"],
                "guardrails": guardrails,
                "deallocCommand": f"pob tree change{target_flag} --deallocate {ids_csv}",
            }
        )

    safe_list = sorted([c for c in all_candidates if c["safe"]], key=lambda c: _sort_key_for_branch(c, sort_norm))
    unsafe_list = sorted([c for c in all_candidates if not c["safe"]], key=lambda c: _sort_key_for_branch(c, sort_norm))

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    out: dict[str, Any] = {
        "dpsKey": dps_key,
        "baseDps": round(base_dps, 2),
        "baseEhp": round(base_ehp, 2),
        "sort": sort_norm,
        "maxBranchPts": max_pts,
        "includeUnsafe": bool(include_unsafe),
        "totalFound": len(branches),
        "evaluatedCount": len(all_candidates),
        "safeCount": len(safe_list),
        "unsafeCount": len(unsafe_list),
        "safeBranches": safe_list[:top_n],
        "unsafeBranches": unsafe_list[:top_n],
        "candidates": safe_list + unsafe_list,
        "socketSummary": socket_summary,
        "elapsedMs": elapsed_ms,
        "luaEvalMs": eval_res.get("elapsedMs"),
    }
    if eval_err_count:
        out["evalErrorCount"] = eval_err_count
    return out
