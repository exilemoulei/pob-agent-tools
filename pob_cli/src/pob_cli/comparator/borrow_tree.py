"""Passive-tree and cluster sub-socket (`nodeId >= 65536`) transplant helpers for `comparator.borrow`."""

from __future__ import annotations

from typing import Any

from ..analyzer.inspectors import resolve_slot_from_items
from ..bridge_client import BridgeWorker

_UNSUPPORTED_RPC_TOKENS: tuple[str, ...] = (
    "unknown",
    "unsupported",
    "unrecognized",
    "not implemented",
    "unhandled",
)


def _is_unsupported_rpc_error(exc: Exception) -> bool:
    if isinstance(exc, NotImplementedError):
        return True
    if not isinstance(exc, (RuntimeError, ValueError, KeyError, AssertionError)):
        return False
    msg = str(exc).lower()
    return any(tok in msg for tok in _UNSUPPORTED_RPC_TOKENS)


def _transplant_tree_in_place(
    main_worker: BridgeWorker,
    ref_bundle: dict[str, Any],
    *,
    cache: Any | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Transplant `ref`'s passive tree (class/ascendancy/secondary/nodes/masteries) onto `main_worker`."""
    notices: list[str] = []
    ref_tree = ref_bundle.get("treeState") or {}
    ref_url = ref_bundle.get("treeUrl")
    ref_alloc = [int(n) for n in ref_tree.get("allocatedNodes") or []]
    ref_mastery_map = ref_tree.get("masteryEffects") if isinstance(ref_tree.get("masteryEffects"), dict) else {}

    if not ref_url and not ref_alloc and ref_tree.get("classId") is None:
        raise RuntimeError("Failed to capture reference passive tree state; aborting tree transplant.")

    used_url = False
    if ref_url:
        main_worker.call("import_tree_url", {"url": ref_url, "title": "Borrowed Ref Tree"})
        used_url = True
    else:
        main_tree = cache.get_tree() if cache is not None else main_worker.call("get_tree_state", {})
        ref_cid = ref_tree.get("classId")
        ref_aid = ref_tree.get("ascendClassId")
        if ref_cid is not None and main_tree.get("classId") != ref_cid:
            main_worker.call("set_class", {"classId": int(ref_cid), "confirm": "reset"})
        if ref_aid is not None and main_tree.get("ascendClassId") != ref_aid:
            main_worker.call("set_ascendancy", {"ascendClassId": int(ref_aid)})
        reset_res = main_worker.call("reset_tree", {})
        cur_alloc = {int(n) for n in (reset_res.get("allocatedNodes") or [])} if isinstance(reset_res, dict) else set()
        for nid in ref_alloc:
            if nid in cur_alloc:
                continue
            params: dict[str, Any] = {"id": nid}
            eff = ref_mastery_map.get(str(nid)) if str(nid) in ref_mastery_map else ref_mastery_map.get(nid)
            if eff is not None:
                params["effect"] = int(eff)
            click_res = main_worker.call("tree_click", params)
            if isinstance(click_res, dict) and "allocatedNodes" in click_res:
                cur_alloc = {int(n) for n in click_res.get("allocatedNodes") or []}
            else:
                cur_alloc.add(nid)

    ref_sec_id = ref_bundle.get("secondaryAscendClassId")
    if ref_sec_id is None:
        ref_sec_id = ref_tree.get("secondaryAscendClassId")
    if ref_sec_id is not None and int(ref_sec_id) > 0:
        try:
            main_worker.call("set_secondary_ascendancy", {"ascendClassId": int(ref_sec_id)})
        except Exception as exc:
            if _is_unsupported_rpc_error(exc):
                notices.append("Skipped secondary ascendancy sync (unsupported by this PoB runtime).")
            else:
                raise

    if cache is not None:
        cache.invalidate("tree", "classes", "specs", "items", "skills")

    overrides = ref_tree.get("overrides") if isinstance(ref_tree.get("overrides"), dict) else {}
    tattoo_count = sum(1 for v in overrides.values() if isinstance(v, dict) and v.get("why") == "tattoo")
    if tattoo_count:
        notices.append(
            f"Reference tree has {tattoo_count} tattoo override(s); tree transplant preserves base nodes/masteries."
        )

    return {
        "method": "import_tree_url" if used_url else "reset_and_click",
        "className": ref_tree.get("className"),
        "ascendClassName": ref_tree.get("ascendClassName"),
        "allocatedCount": len(ref_alloc),
        "targetCount": len(ref_alloc),
    }, notices


def _allocate_cluster_nodes_pass(main_worker: BridgeWorker, cluster_nodes: list[int]) -> bool:
    if not cluster_nodes:
        return False
    cur_tree = main_worker.call("get_tree_state", {})
    cur_alloc = {int(n) for n in cur_tree.get("allocatedNodes") or []}
    progressed = False
    for cid in cluster_nodes:
        if cid in cur_alloc:
            continue
        try:
            c_res = main_worker.call("tree_click", {"id": cid})
            if isinstance(c_res, dict) and "allocatedNodes" in c_res:
                cur_alloc = {int(n) for n in c_res.get("allocatedNodes") or []}
            else:
                cur_alloc.add(cid)
            progressed = True
        except Exception:
            pass
    return progressed


def _apply_cluster_passes(
    main_worker: BridgeWorker,
    cluster_nodes: list[int],
    cluster_plans: list[dict[str, Any]],
    equipped_items: list[dict[str, Any]],
    notices: list[str],
) -> bool:
    """Allocate cluster subgraph nodes and equip cluster sub-socket jewels in up to 2 rounds + final node pass."""
    if not cluster_nodes and not cluster_plans:
        return False
    pending = list(cluster_plans)
    cur_items: dict[str, Any] | None = None
    touched_tree = False
    equipped_in_last_round = False
    for _ in range(2):
        equipped_in_last_round = False
        node_progressed = _allocate_cluster_nodes_pass(main_worker, cluster_nodes)
        if node_progressed:
            touched_tree = True
        if pending:
            cur_items = main_worker.call("list_items", {})
            nid_to_slot = {
                int(s["nodeId"]): s["name"]
                for s in cur_items.get("slots", [])
                if s.get("nodeId") is not None and not s.get("inactive")
            }
            still_pending: list[dict[str, Any]] = []
            for plan in pending:
                nid = int(plan["nodeId"])
                target_slot = nid_to_slot.get(nid)
                if target_slot:
                    add_res = main_worker.call("add_item", {"raw": plan["raw"], "slotName": target_slot, "equip": True})
                    equipped_items.append({"slotName": target_slot, "item": add_res.get("item")})
                    touched_tree = True
                    equipped_in_last_round = True
                else:
                    still_pending.append(plan)
            pending = still_pending
        if not pending and not node_progressed and not equipped_in_last_round:
            break
    if equipped_in_last_round and cluster_nodes:
        if _allocate_cluster_nodes_pass(main_worker, cluster_nodes):
            touched_tree = True
    if pending:
        cur_items = cur_items or main_worker.call("list_items", {})
        for plan in pending:
            if plan.get("autoBorrowedForTree"):
                notices.append(f"Skipped cluster sub-socket jewel at node:{plan['nodeId']} (socket not materialized).")
            else:
                resolve_slot_from_items(cur_items, f"node:{plan['nodeId']}")
    return touched_tree


def reconcile_transplanted_tree(
    main_worker: BridgeWorker,
    ref_bundle: dict[str, Any],
    borrowed_tree_info: dict[str, Any],
    notices: list[str],
    *,
    cache: Any | None = None,
) -> None:
    """Prune auto-pathed extra leaf nodes without cascading ref nodes, then record allocated/missing counts."""
    final_tree = main_worker.call("get_tree_state", {})
    actual_alloc = {int(n) for n in final_tree.get("allocatedNodes") or []}
    ref_alloc_set = {int(n) for n in (ref_bundle.get("treeState") or {}).get("allocatedNodes") or []}

    if ref_alloc_set:
        extra = sorted(actual_alloc - ref_alloc_set, reverse=True)
        if extra:
            cur_items = main_worker.call("list_items", {})
            equipped_nids = {
                int(s["nodeId"])
                for s in cur_items.get("slots", [])
                if s.get("nodeId") is not None and s.get("selItemId")
            }
            for eid in extra:
                if eid in equipped_nids or eid not in actual_alloc:
                    continue
                try:
                    t_res = main_worker.call("tree_click", {"id": eid})
                except Exception:
                    continue
                if isinstance(t_res, dict) and "allocatedNodes" in t_res:
                    new_alloc = {int(n) for n in t_res.get("allocatedNodes") or []}
                else:
                    new_alloc = actual_alloc - {eid}
                lost = sorted((ref_alloc_set & actual_alloc) - new_alloc)
                if lost:
                    cur_repair_alloc = set(new_alloc)
                    for lid in lost:
                        if lid in cur_repair_alloc:
                            continue
                        try:
                            r_res = main_worker.call("tree_click", {"id": lid})
                            if isinstance(r_res, dict) and "allocatedNodes" in r_res:
                                cur_repair_alloc = {int(n) for n in r_res.get("allocatedNodes") or []}
                            else:
                                cur_repair_alloc.add(lid)
                        except Exception:
                            pass
                    final_tree = main_worker.call("get_tree_state", {})
                    actual_alloc = {int(n) for n in final_tree.get("allocatedNodes") or []}
                else:
                    actual_alloc = new_alloc
                    if isinstance(t_res, dict) and "allocatedNodes" in t_res:
                        final_tree = t_res

    if cache is not None:
        cache.tree = final_tree
    borrowed_tree_info["allocatedCount"] = len(actual_alloc)
    borrowed_tree_info["targetCount"] = len(ref_alloc_set)
    if ref_alloc_set:
        missing = sorted(ref_alloc_set - actual_alloc)
        if missing:
            borrowed_tree_info["missingNodes"] = missing
            matched_count = len(actual_alloc & ref_alloc_set)
            preview = ", ".join(str(x) for x in missing[:8])
            ellipsis = "..." if len(missing) > 8 else ""
            notices.append(
                f"Tree transplant allocated {matched_count}/{len(ref_alloc_set)} reference nodes; "
                f"{len(missing)} node(s) could not be allocated (missing: {preview}{ellipsis})."
            )


def _paste_borrowed_skill_group(
    main_worker: BridgeWorker,
    ref_group_obj: dict[str, Any],
    copied_group_texts: list[tuple[str, str | None]],
    *,
    borrow_main_flag: bool,
    notices: list[str],
    cache: Any | None = None,
) -> dict[str, Any]:
    new_idx: int | None = None
    new_grp: dict[str, Any] = {}
    pasted_indices: list[int] = []
    for cp_text, target_slot_name in copied_group_texts:
        paste_res = main_worker.call("paste_group", {"text": cp_text})
        p_idx = paste_res.get("index")
        if p_idx is not None:
            p_idx = int(p_idx)
            pasted_indices.append(p_idx)
            new_grp = paste_res.get("group") or {}
            if target_slot_name:
                set_res = main_worker.call("set_group", {"index": p_idx, "slot": target_slot_name})
                new_grp = set_res.get("group") or new_grp

    if ref_group_obj.get("source"):
        src_slot = ref_group_obj.get("slot")
        skills_after = main_worker.call("list_skills", {})
        for g in skills_after.get("groups", []):
            if g.get("source") and (not src_slot or g.get("slot") == src_slot):
                new_idx = int(g["index"])
                new_grp = g
                break
    elif pasted_indices:
        new_idx = pasted_indices[-1]

    switched_main = False
    if borrow_main_flag and new_idx is not None:
        main_worker.call("set_build_field", {"field": "mainSocketGroup", "value": int(new_idx)})
        switched_main = True
        notices.append(f"Switched mainSocketGroup to borrowed skill group #{new_idx}.")
    if cache is not None:
        cache.invalidate("skills")

    return {
        "newGroupIndex": new_idx,
        "label": (
            new_grp.get("displayLabelZh")
            or new_grp.get("displayLabel")
            or new_grp.get("label")
            or ref_group_obj.get("displayLabelZh")
            or ref_group_obj.get("displayLabel")
            or ref_group_obj.get("label")
        ),
        "switchedMain": switched_main,
    }
