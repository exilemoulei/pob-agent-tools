"""Cross-build equipment, skill-group, and passive-tree transplant (`borrow_from_ref`, `capture_ref_bundle`)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..analyzer.inspectors import resolve_skill_group_index, resolve_slot_from_items
from ..bridge_client import BridgeWorker
from ..snapshot import (
    BuildScope,
    BuildTransaction,
    _call_batch,
    capture_digest,
    diff_digest,
    run_mutation,
)
from .borrow_tree import (
    _apply_cluster_passes,
    _paste_borrowed_skill_group,
    _transplant_tree_in_place,
    reconcile_transplanted_tree,
)

TWO_HANDED_TYPES = frozenset(
    {
        "Bow",
        "Staff",
        "Warstaff",
        "Two Handed Sword",
        "Two Handed Axe",
        "Two Handed Mace",
        "Fishing Rod",
    }
)


def _ok_rpc(res: Any) -> bool:
    return isinstance(res, dict) and not res.get("_error") and not res.get("error") and res.get("ok") is not False


def capture_ref_bundle(ref_worker: BridgeWorker) -> dict[str, Any]:
    """Capture `ref_worker`'s items, raw item texts, skills, group copies, and tree state ONCE."""
    with ref_worker.lock:
        base_res = _call_batch(
            ref_worker,
            [
                ("list_items", {}),
                ("list_skills", {}),
                ("get_tree_state", {}),
                ("export_tree_url", {}),
                ("list_classes", {}),
            ],
            raise_on_error=False,
        )
        items_res = base_res[0] if len(base_res) > 0 and _ok_rpc(base_res[0]) else ref_worker.call("list_items", {})
        skills_res = base_res[1] if len(base_res) > 1 and _ok_rpc(base_res[1]) else ref_worker.call("list_skills", {})
        if len(base_res) > 2 and _ok_rpc(base_res[2]):
            tree_state = base_res[2]
        else:
            try:
                tree_state = ref_worker.call("get_tree_state", {})
            except Exception:
                tree_state = {}
        tree_ok = isinstance(tree_state, dict) and bool(tree_state)
        tree_url = (
            str(base_res[3].get("url") or "").strip()
            if len(base_res) > 3 and _ok_rpc(base_res[3]) and base_res[3].get("url")
            else None
        )
        classes_res = base_res[4] if len(base_res) > 4 and _ok_rpc(base_res[4]) else {}
        sec_asc_id = (classes_res.get("classPick") or {}).get("secondaryAscendClassId")
        if sec_asc_id is None:
            sec_asc_id = tree_state.get("secondaryAscendClassId")

        items_by_id = {int(it["id"]): it for it in items_res.get("items", []) if it.get("id") is not None}
        slots_by_name = {s["name"]: s for s in items_res.get("slots", []) if s.get("name")}

        raw_ids: list[int] = []
        seen_ids: set[int] = set()
        for s in items_res.get("slots", []):
            iid = s.get("selItemId")
            if iid:
                iid_int = int(iid)
                if iid_int not in seen_ids:
                    seen_ids.add(iid_int)
                    raw_ids.append(iid_int)

        copy_indices = [
            int(g["index"])
            for g in skills_res.get("groups", [])
            if not g.get("source") and g.get("index") is not None
        ]

        detail_calls: list[tuple[str, dict[str, Any]]] = [
            ("item_raw", {"id": iid}) for iid in raw_ids
        ] + [
            ("copy_group", {"index": gidx}) for gidx in copy_indices
        ]
        detail_res = _call_batch(ref_worker, detail_calls, raise_on_error=False) if detail_calls else []

        item_raws: dict[int, str] = {}
        for iid, resp in zip(raw_ids, detail_res[: len(raw_ids)], strict=False):
            if _ok_rpc(resp) and resp.get("raw"):
                item_raws[iid] = str(resp["raw"])

        group_texts: dict[int, str] = {}
        for gidx, resp in zip(copy_indices, detail_res[len(raw_ids) :], strict=False):
            if _ok_rpc(resp) and resp.get("text"):
                group_texts[gidx] = str(resp["text"])

        return {
            "itemsRes": items_res,
            "itemsById": items_by_id,
            "slotsByName": slots_by_name,
            "itemRaws": item_raws,
            "skillsRes": skills_res,
            "groupTexts": group_texts,
            "treeState": tree_state,
            "treeOk": tree_ok,
            "treeUrl": tree_url,
            "secondaryAscendClassId": sec_asc_id,
        }


def _build_item_plans(
    ref_bundle: dict[str, Any],
    slots_to_borrow: list[str],
    *,
    borrow_tree: bool,
    borrow_tree_jewels: bool,
    ref_group_obj: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[int]]:
    ref_items_res = ref_bundle["itemsRes"]
    ref_items_by_id = ref_bundle["itemsById"]
    ref_slots_by_name = ref_bundle["slotsByName"]
    ref_raws = ref_bundle["itemRaws"]
    plans: list[dict[str, Any]] = []
    seen_ref_slots: set[str] = set()
    skipped_tree_jewels: list[int] = []

    def _add_plan(resolved: str, *, auto_source: bool = False, auto_tree_jewel: bool = False) -> None:
        if resolved in seen_ref_slots:
            return
        r_slot = ref_slots_by_name.get(resolved)
        if not r_slot or not r_slot.get("selItemId"):
            raise ValueError(f"Reference build has no equipped item in slot '{resolved}'.")
        iid = int(r_slot["selItemId"])
        raw_text = ref_raws.get(iid)
        if not raw_text:
            raise ValueError(f"Reference build has no raw text for equipped item in slot '{resolved}'.")
        seen_ref_slots.add(resolved)
        plans.append(
            {
                "slotName": resolved,
                "nodeId": r_slot.get("nodeId"),
                "refItem": ref_items_by_id.get(iid),
                "raw": raw_text,
                "autoBorrowedForSkillSource": auto_source,
                "autoBorrowedForTree": auto_tree_jewel,
            }
        )

    for s_query in slots_to_borrow:
        resolved = resolve_slot_from_items(ref_items_res, s_query)
        _add_plan(resolved)

    if borrow_tree and borrow_tree_jewels:
        ref_alloc = {int(n) for n in (ref_bundle.get("treeState") or {}).get("allocatedNodes") or []}
        for s in ref_items_res.get("slots", []):
            nid = s.get("nodeId")
            if nid is None or s.get("inactive") or not s.get("selItemId"):
                continue
            if ref_alloc and int(nid) not in ref_alloc:
                continue
            if s["name"] in seen_ref_slots:
                continue
            if not ref_raws.get(int(s["selItemId"])):
                skipped_tree_jewels.append(int(nid))
                continue
            _add_plan(s["name"], auto_tree_jewel=True)

    if ref_group_obj and ref_group_obj.get("source"):
        src_slot = ref_group_obj.get("slot")
        if src_slot and src_slot not in seen_ref_slots:
            r_slot = ref_slots_by_name.get(src_slot)
            if not r_slot or not r_slot.get("selItemId") or int(r_slot["selItemId"]) not in ref_raws:
                raise ValueError(
                    f"Cannot borrow item-granted skill group '{ref_group_obj.get('displayLabel')}': "
                    f"reference slot '{src_slot}' has no raw item text."
                )
            _add_plan(src_slot, auto_source=True)
        elif not src_slot:
            raise ValueError(
                f"Cannot borrow item-granted skill group '{ref_group_obj.get('displayLabel')}': "
                "reference slot is missing."
            )

    return plans, skipped_tree_jewels


def apply_borrow_in_place(
    main_worker: BridgeWorker,
    ref_bundle: dict[str, Any],
    *,
    item_slots: list[str] | None = None,
    skill_group: str | int | None = None,
    borrow_tree: bool = False,
    borrow_tree_jewels: bool = True,
    cache: Any | None = None,
) -> dict[str, Any]:
    """Apply pre-fetched `ref_bundle` transplants onto `main_worker` in place (0 RPCs to `ref_worker`)."""
    slots_to_borrow = [s.strip() for s in (item_slots or []) if s.strip()]
    if not slots_to_borrow and skill_group is None and not borrow_tree:
        raise ValueError("Specify at least one of --item-slot, --skill-group, or --tree to borrow.")

    ref_skills_res = ref_bundle["skillsRes"]
    ref_group_texts = ref_bundle["groupTexts"]
    ref_group_obj: dict[str, Any] | None = None
    copied_group_texts: list[tuple[str, str | None]] = []
    borrow_main_flag = False
    if skill_group is not None and str(skill_group).strip():
        g_idx = resolve_skill_group_index(ref_skills_res, skill_group)
        ref_group_obj = next((g for g in ref_skills_res.get("groups", []) if g.get("index") == g_idx), None)
        if ref_group_obj is None:
            raise ValueError(f"Reference skill group #{g_idx} not found.")
        borrow_main_flag = str(skill_group).strip().lower() == "main" or bool(ref_group_obj.get("isMain"))
        src_slot = ref_group_obj.get("slot")
        if ref_group_obj.get("source"):
            if src_slot:
                for sg in ref_skills_res.get("groups", []):
                    if not sg.get("source") and sg.get("slot") == src_slot and sg.get("enabled", True):
                        cp = ref_group_texts.get(int(sg["index"]))
                        if cp:
                            copied_group_texts.append((cp, src_slot))
        else:
            cp = ref_group_texts.get(int(g_idx))
            if not cp:
                raise RuntimeError(
                    f"Failed to copy reference skill group #{g_idx} "
                    f"('{ref_group_obj.get('displayLabel') or g_idx}')."
                )
            copied_group_texts.append((cp, src_slot))

    borrowed_items_plan, skipped_tree_jewels = _build_item_plans(
        ref_bundle,
        slots_to_borrow,
        borrow_tree=borrow_tree,
        borrow_tree_jewels=borrow_tree_jewels,
        ref_group_obj=ref_group_obj,
    )

    notices: list[str] = [
        f"Skipped tree jewel at node:{nid} (raw text unavailable from the reference slot)."
        for nid in skipped_tree_jewels
    ]
    borrowed_tree_info: dict[str, Any] | None = None
    if borrow_tree:
        borrowed_tree_info, tree_notices = _transplant_tree_in_place(main_worker, ref_bundle, cache=cache)
        notices.extend(tree_notices)

    main_items_before = cache.get_items() if cache is not None else main_worker.call("list_items", {})
    active_main_nids = {
        int(s["nodeId"])
        for s in main_items_before.get("slots", [])
        if s.get("nodeId") is not None and not s.get("inactive")
    }
    base_plans = [
        p
        for p in borrowed_items_plan
        if p.get("nodeId") is None or (int(p["nodeId"]) < 65536 and int(p["nodeId"]) in active_main_nids)
    ]
    cluster_plans = sorted(
        [
            p
            for p in borrowed_items_plan
            if p.get("nodeId") is not None and (int(p["nodeId"]) >= 65536 or int(p["nodeId"]) not in active_main_nids)
        ],
        key=lambda p: int(p["nodeId"]),
    )

    equipped_items: list[dict[str, Any]] = []
    touched_jewel = False
    if base_plans:
        main_items_by_id = {it["id"]: it for it in main_items_before.get("items", [])}
        main_slots_by_name = {s["name"]: s for s in main_items_before.get("slots", [])}
        borrowed_slot_names: set[str] = set()
        resolved_plans: list[tuple[str, dict[str, Any]]] = []
        for plan in base_plans:
            if plan.get("nodeId") is not None:
                target_slot = resolve_slot_from_items(main_items_before, f"node:{plan['nodeId']}")
                touched_jewel = True
            else:
                target_slot = plan["slotName"]
            borrowed_slot_names.add(target_slot)
            resolved_plans.append((target_slot, plan))

        for target_slot, plan in resolved_plans:
            ref_item = plan.get("refItem") or {}
            ref_type = str(ref_item.get("type") or "")
            if target_slot == "Weapon 1" and ref_type in TWO_HANDED_TYPES and "Weapon 2" not in borrowed_slot_names:
                w2_slot = main_slots_by_name.get("Weapon 2")
                if w2_slot and w2_slot.get("selItemId"):
                    main_worker.call("unequip_slot", {"slotName": "Weapon 2"})
                    notices.append(
                        f"Unequipped 'Weapon 2' in main build because borrowed 'Weapon 1' is Two-Handed ({ref_type})."
                    )
            elif target_slot == "Weapon 2" and "Weapon 1" not in borrowed_slot_names:
                w1_slot = main_slots_by_name.get("Weapon 1")
                w1_item = main_items_by_id.get(w1_slot.get("selItemId") or 0) if w1_slot else None
                if w1_item and str(w1_item.get("type") or "") in TWO_HANDED_TYPES:
                    notices.append(
                        f"Warning: Borrowing 'Weapon 2' while main 'Weapon 1' has a Two-Handed weapon ({w1_item.get('type')})."
                    )
            if plan.get("autoBorrowedForSkillSource") and ref_group_obj is not None:
                notices.append(
                    f"Auto-borrowed source item in slot '{target_slot}' because skill group "
                    f"'{ref_group_obj.get('displayLabel')}' is granted by that item."
                )
            add_res = main_worker.call("add_item", {"raw": plan["raw"], "slotName": target_slot, "equip": True})
            equipped_items.append({"slotName": target_slot, "item": add_res.get("item")})

        if cache is not None:
            cache.invalidate("items", "skills")
            if touched_jewel:
                cache.invalidate("tree")

    # After outer cluster jewels are equipped, re-click any still-unallocated ref nodes (both >=65536
    # subgraph passives and <65536 inner cluster socket proxies) on both import_tree_url and reset_and_click.
    cluster_nodes = (
        sorted(int(n) for n in (ref_bundle.get("treeState") or {}).get("allocatedNodes") or [])
        if borrow_tree
        else []
    )
    if _apply_cluster_passes(main_worker, cluster_nodes, cluster_plans, equipped_items, notices) and cache is not None:
        cache.invalidate("items", "skills", "tree")

    if borrow_tree and borrowed_tree_info is not None:
        reconcile_transplanted_tree(main_worker, ref_bundle, borrowed_tree_info, notices, cache=cache)

    borrowed_skill_info = (
        _paste_borrowed_skill_group(
            main_worker,
            ref_group_obj,
            copied_group_texts,
            borrow_main_flag=borrow_main_flag,
            notices=notices,
            cache=cache,
        )
        if ref_group_obj
        else None
    )

    out: dict[str, Any] = {
        "borrowedItems": equipped_items,
        "borrowedSkillGroup": borrowed_skill_info,
        "notices": notices,
    }
    if borrowed_tree_info is not None:
        out["borrowedTree"] = borrowed_tree_info
    if skipped_tree_jewels:
        out["skippedTreeJewels"] = skipped_tree_jewels
    return out


def borrow_from_ref(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    item_slots: list[str] | None = None,
    skill_group: str | None = None,
    borrow_tree: bool = False,
    borrow_tree_jewels: bool = True,
    scenario: str | dict[str, Any] | None = None,
    keep: Iterable[str] | str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Borrow items, a skill group, and/or the passive tree from `ref` into `main`."""
    slots_to_borrow = [s.strip() for s in (item_slots or []) if s.strip()]
    if not slots_to_borrow and not skill_group and not borrow_tree:
        raise ValueError("Specify at least one of --item-slot, --skill-group, or --tree to borrow.")

    has_scenario = scenario is not None and (not isinstance(scenario, str) or bool(scenario.strip()))
    if has_scenario and apply:
        raise ValueError(
            "--scenario measures what-if deltas under a temporary combat baseline and cannot be combined "
            "with --apply; omit --scenario when persisting changes."
        )

    scope = BuildScope.SETS if borrow_tree else BuildScope.ACTIVE_SETS
    with main_worker.lock, ref_worker.lock:
        ref_bundle = capture_ref_bundle(ref_worker)

        if has_scenario:
            from .matrix import _apply_matrix_scenario_in_place, _parse_matrix_scenario

            plan = _parse_matrix_scenario(scenario)  # type: ignore[arg-type]
            with BuildTransaction(main_worker, scope=scope) as tx:
                orig_digest = capture_digest(main_worker)
                tx.verify_digest = orig_digest
                scenario_applied = _apply_matrix_scenario_in_place(main_worker, plan, keep=keep)
                before = capture_digest(main_worker)
                detail = apply_borrow_in_place(
                    main_worker,
                    ref_bundle,
                    item_slots=slots_to_borrow,
                    skill_group=skill_group,
                    borrow_tree=borrow_tree,
                    borrow_tree_jewels=borrow_tree_jewels,
                )
                after = capture_digest(main_worker)
                restore_check = tx.rollback_verified(orig_digest)
            return {
                "applied": False,
                "mode": f"PREVIEW (what-if @ {plan['name']})",
                "scenario": scenario,
                "scenarioApplied": scenario_applied,
                "detail": detail,
                "before": {
                    "primaryDpsKey": before["primaryDpsKey"],
                    "primaryDps": before["primaryDps"],
                    "totalEhp": before["totalEhp"],
                    "life": before["life"],
                    "points": before.get("buildInfo", {}).get("points", {}),
                },
                "after": {
                    "primaryDpsKey": after["primaryDpsKey"],
                    "primaryDps": after["primaryDps"],
                    "totalEhp": after["totalEhp"],
                    "life": after["life"],
                    "points": after.get("buildInfo", {}).get("points", {}),
                },
                "diff": diff_digest(before, after),
                "restoreCheck": restore_check,
                **tx.history.depths(),
            }

        if not has_scenario and not apply and borrow_tree:
            with BuildTransaction(main_worker, scope=scope) as tx:
                orig_digest = capture_digest(main_worker)
                tx.verify_digest = orig_digest
                detail = apply_borrow_in_place(
                    main_worker,
                    ref_bundle,
                    item_slots=slots_to_borrow,
                    skill_group=skill_group,
                    borrow_tree=borrow_tree,
                    borrow_tree_jewels=borrow_tree_jewels,
                )
                after = capture_digest(main_worker)
                restore_check = tx.rollback_verified(orig_digest)
            return {
                "applied": False,
                "mode": "PREVIEW (what-if)",
                "detail": detail,
                "before": {
                    "primaryDpsKey": orig_digest["primaryDpsKey"],
                    "primaryDps": orig_digest["primaryDps"],
                    "totalEhp": orig_digest["totalEhp"],
                    "life": orig_digest["life"],
                    "points": orig_digest.get("buildInfo", {}).get("points", {}),
                },
                "after": {
                    "primaryDpsKey": after["primaryDpsKey"],
                    "primaryDps": after["primaryDps"],
                    "totalEhp": after["totalEhp"],
                    "life": after["life"],
                    "points": after.get("buildInfo", {}).get("points", {}),
                },
                "diff": diff_digest(orig_digest, after),
                "restoreCheck": restore_check,
                **tx.history.depths(),
            }

        def _apply_borrow(w: BridgeWorker) -> dict[str, Any]:
            detail = apply_borrow_in_place(
                w,
                ref_bundle,
                item_slots=slots_to_borrow,
                skill_group=skill_group,
                borrow_tree=borrow_tree,
                borrow_tree_jewels=borrow_tree_jewels,
            )
            if borrow_tree and apply and (detail.get("borrowedTree") or {}).get("method") == "import_tree_url":
                detail.setdefault("notices", []).append(
                    "Imported reference passive tree as active spec 'Borrowed Ref Tree'."
                )
            return detail

        return run_mutation(main_worker, _apply_borrow, apply=apply, scope=scope)
