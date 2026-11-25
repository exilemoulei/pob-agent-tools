"""Passive tree allocation/deallocation with R12 check and cascade deallocation guard."""

from __future__ import annotations

from typing import Any

from ..bridge_client import BridgeWorker
from ..snapshot import run_mutation
from .common import (
    _CASCADE_WARN_THRESHOLD,
    _call_batch,
    _check_agnostic_dealloc_caution,
    _fold_no_apos,
    _fold_search_text,
    _is_batch_error,
)


def _resolve_node_token(
    worker: BridgeWorker,
    token: str,
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
) -> tuple[int, int | None]:
    """Resolve a node token (`12345`, `12345:67890` mastery, or node name) into `(node_id, effect_id)`."""
    t = token.strip()
    effect_id: int | None = None
    if ":" in t:
        left, right = t.split(":", 1)
        t = left.strip()
        if right.strip().isdigit():
            effect_id = int(right.strip())
        else:
            effect_query = right.strip().lower()
            effect_fold = _fold_search_text(right)
            nid, _ = _resolve_node_token(worker, t, nodes_db, dynamic_map)
            node_meta = nodes_db.get(str(nid)) or dynamic_map.get(str(nid)) or {}
            for eff in node_meta.get("masteryEffects", []):
                text = " ".join(str(s) for s in (eff.get("stats") or []) + (eff.get("statsZh") or []))
                if effect_query in text.lower() or (effect_fold and effect_fold in _fold_search_text(text)):
                    return nid, int(eff["effect"])
            raise ValueError(f"Mastery effect matching '{right}' not found on node {nid}.")

    if t.isdigit():
        nid = int(t)
        if str(nid) not in nodes_db and str(nid) not in dynamic_map:
            raise ValueError(f"Passive tree node ID {nid} does not exist.")
        return nid, effect_id

    t_low = t.lower()
    t_fold = _fold_no_apos(t)
    exact_matches: list[int] = []
    sub_matches: list[int] = []
    for sid, meta in {**nodes_db, **dynamic_map}.items():
        name_en = str(meta.get("name") or "").lower()
        name_zh = str(meta.get("nameZh") or "").lower()
        fold_en = _fold_no_apos(meta.get("name"))
        if t_low in (name_en, name_zh) or (t_fold and t_fold == fold_en):
            exact_matches.append(int(sid))
        elif t_low in name_en or t_low in name_zh or (t_fold and t_fold in fold_en):
            sub_matches.append(int(sid))

    candidates = exact_matches or sub_matches
    if not candidates:
        raise ValueError(f"No passive node found matching '{t}'.")
    if len(candidates) == 1:
        return candidates[0], effect_id

    sample_ids = candidates[:15]
    hovers = _call_batch(
        worker,
        [("node_hover", {"id": cid}) for cid in sample_ids],
        raise_on_error=False,
    )
    best_id = sample_ids[0]
    best_dist = 999999
    for cid, hover in zip(sample_ids, hovers, strict=False):
        if _is_batch_error(hover):
            continue
        cost = hover.get("cost")
        dist = 0 if hover.get("allocated") else (int(cost) if cost is not None else 9999)
        if dist < best_dist:
            best_dist = dist
            best_id = cid
    return best_id, effect_id


def _apply_tree_changes_in_place(
    w: BridgeWorker,
    resolved_alloc: list[tuple[int, int | None]],
    resolved_dealloc: list[tuple[int, int | None]],
    *,
    initial_tree_state: dict[str, Any] | None = None,
    allow_cascade: bool = True,
    noop_warnings_out: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[int], int, str | None]:
    """Apply tree allocations/deallocations reusing `tree_click`'s returned `get_tree_state` payload."""
    actions_taken: list[dict[str, Any]] = []
    noop_warnings: list[str] = []
    cur_state = (
        initial_tree_state
        if (initial_tree_state and "allocatedNodes" in initial_tree_state)
        else w.call("get_tree_state", {})
    )

    initial_alloc = {int(x) for x in cur_state.get("allocatedNodes", [])}
    initial_used = int((cur_state.get("points") or {}).get("used") or 0)

    verified_alloc_ids: list[int] = []
    for nid, eff_id in resolved_alloc:
        alloc_set = {int(x) for x in cur_state.get("allocatedNodes", [])}
        if nid in alloc_set:
            if eff_id is not None:
                res = w.call("select_mastery", {"id": nid, "effect": eff_id})
                cur_state = res if (isinstance(res, dict) and "allocatedNodes" in res) else w.call("get_tree_state", {})
                actions_taken.append({"action": "select_mastery", "id": nid, "effect": eff_id})
            else:
                actions_taken.append({"action": "skip_already_allocated", "id": nid})
                noop_warnings.append(f"Node {nid} is already allocated (skipped allocate).")
            continue

        click_params: dict[str, Any] = {"id": nid}
        if eff_id is not None:
            click_params["effect"] = eff_id
        res = w.call("tree_click", click_params)
        if res.get("needsMastery"):
            effects = res.get("effects", [])
            opts_str = "; ".join(
                f"{e['effect']}: {' / '.join(e.get('statsZh') or e.get('stats') or [])}"
                for e in effects[:6]
            )
            raise ValueError(
                f"Node {nid} is a Mastery node and requires `<nodeId>:<effectId>`. Available effects: {opts_str}"
            )
        if res.get("needsConfirm"):
            raise ValueError(f"Node {nid} requires confirmation ({res.get('needsConfirm')}).")
        if isinstance(res, dict) and "allocatedNodes" in res:
            cur_state = res
            verified_alloc_ids.append(nid)
        else:
            cur_state = w.call("get_tree_state", {})
        actions_taken.append({"action": "allocated", "id": nid, "effect": eff_id})

    for nid, _ in resolved_dealloc:
        alloc_set = {int(x) for x in cur_state.get("allocatedNodes", [])}
        if nid not in alloc_set:
            actions_taken.append({"action": "skip_already_unallocated", "id": nid})
            noop_warnings.append(f"Node {nid} is not currently allocated (skipped deallocate).")
            continue
        res = w.call("tree_click", {"id": nid})
        if res.get("needsConfirm"):
            raise ValueError(f"Node {nid} deallocation requires confirmation ({res.get('needsConfirm')}).")
        cur_state = res if (isinstance(res, dict) and "allocatedNodes" in res) else w.call("get_tree_state", {})
        actions_taken.append({"action": "deallocated", "id": nid})

    final_alloc = {int(x) for x in cur_state.get("allocatedNodes", [])}
    requested_dealloc_ids = {nid for nid, _ in resolved_dealloc}
    for alloc_nid in verified_alloc_ids:
        if alloc_nid not in final_alloc and alloc_nid not in requested_dealloc_ids:
            raise ValueError(
                f"Passive node {alloc_nid} could not be allocated (disconnected or unreachable from current tree)."
            )
    orphaned_nodes = sorted((initial_alloc - final_alloc) - requested_dealloc_ids)

    final_points = cur_state.get("points") or {}
    if "used" in final_points:
        points_delta = int(final_points.get("used") or 0) - initial_used
    else:
        points_delta = len(final_alloc) - len(initial_alloc)

    cascade_warning: str | None = None
    if len(orphaned_nodes) > _CASCADE_WARN_THRESHOLD:
        cascade_warning = (
            f"Cascade deallocation dropped {len(orphaned_nodes)} additional orphaned passive nodes "
            f"beyond the requested deallocate set (pointsDelta={points_delta})."
        )
        if not allow_cascade:
            sample = orphaned_nodes[:10]
            raise ValueError(
                f"Cascade deallocation blocked: {len(orphaned_nodes)} orphaned passive nodes would be lost "
                f"(exceeds threshold {_CASCADE_WARN_THRESHOLD}; sample IDs: {sample}). "
                "Re-run with allow_cascade=True / --allow-cascade if intentional."
            )

    if noop_warnings_out is not None:
        noop_warnings_out.extend(noop_warnings)
    return actions_taken, cur_state, orphaned_nodes, points_delta, cascade_warning


def change_tree(
    worker: BridgeWorker,
    *,
    allocate: list[str] | None = None,
    deallocate: list[str] | None = None,
    apply: bool = False,
    allow_cascade: bool = True,
) -> dict[str, Any]:
    """Allocate and/or deallocate passive tree nodes (R12 + cascade guard)."""
    alloc_tokens = [x for x in (allocate or []) if str(x).strip()]
    dealloc_tokens = [x for x in (deallocate or []) if str(x).strip()]
    if not alloc_tokens and not dealloc_tokens:
        raise ValueError("Specify at least one node in --allocate or --deallocate.")

    with worker.lock:
        tree_state = worker.call("get_tree_state", {})
        nodes_db = worker.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
        dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}

        resolved_alloc = [_resolve_node_token(worker, str(tok), nodes_db, dynamic_map) for tok in alloc_tokens]
        resolved_dealloc = [_resolve_node_token(worker, str(tok), nodes_db, dynamic_map) for tok in dealloc_tokens]
        dealloc_ids = {nid for nid, _ in resolved_dealloc}
        cautions = _check_agnostic_dealloc_caution(worker, dealloc_ids)

        single_preview = None
        if len(resolved_alloc) + len(resolved_dealloc) == 1:
            single_nid = (resolved_alloc or resolved_dealloc)[0][0]
            try:
                n_info, n_hover = _call_batch(
                    worker,
                    [
                        ("node_info", {"id": single_nid, "diff": True}),
                        ("node_hover", {"id": single_nid}),
                    ],
                )
                single_preview = {"nodeInfo": n_info, "nodeHover": n_hover}
            except Exception:
                pass

        def _do_tree_changes(w: BridgeWorker) -> dict[str, Any]:
            noop_warnings: list[str] = []
            actions_taken, final_state, orphaned_nodes, points_delta, cascade_warning = _apply_tree_changes_in_place(
                w,
                resolved_alloc,
                resolved_dealloc,
                initial_tree_state=tree_state,
                allow_cascade=allow_cascade,
                noop_warnings_out=noop_warnings,
            )
            detail: dict[str, Any] = {
                "actions": actions_taken,
                "points": final_state.get("points"),
                "pointsDelta": points_delta,
                "orphanedNodes": orphaned_nodes,
                "singlePreview": single_preview,
            }
            if noop_warnings:
                detail["noopWarnings"] = noop_warnings
            if cascade_warning:
                detail["cascadeWarning"] = cascade_warning
            if cautions:
                detail["cautions"] = list(cautions)
            return detail

        res = run_mutation(worker, _do_tree_changes, apply=apply)
        detail = res.get("detail") or {}
        res["orphanedNodes"] = detail.get("orphanedNodes", [])
        res["pointsDelta"] = detail.get("pointsDelta", 0)
        if detail.get("noopWarnings"):
            res["noopWarnings"] = list(detail["noopWarnings"])
        if detail.get("cascadeWarning"):
            res["cascadeWarning"] = detail["cascadeWarning"]
        if detail.get("cautions"):
            res["cautions"] = list(detail["cautions"])
        return res


modify_tree = change_tree
