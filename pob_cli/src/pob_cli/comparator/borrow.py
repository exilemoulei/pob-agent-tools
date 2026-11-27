"""Cross-build equipment and skill-group transplant (`borrow_from_ref`)."""

from __future__ import annotations

from typing import Any

from ..analyzer import resolve_skill_group_index, resolve_slot_name
from ..bridge_client import BridgeWorker
from ..snapshot import run_mutation

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


def borrow_from_ref(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    item_slots: list[str] | None = None,
    skill_group: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Borrow one or more equipment slots and/or a skill group from `ref` into `main`."""
    slots_to_borrow = [s.strip() for s in (item_slots or []) if s.strip()]
    if not slots_to_borrow and not skill_group:
        raise ValueError("Specify at least one of --item-slot or --skill-group to borrow.")

    with main_worker.lock, ref_worker.lock:
        ref_items_res = ref_worker.call("list_items", {})
        ref_items_by_id = {it["id"]: it for it in ref_items_res.get("items", [])}
        ref_slots_by_name = {s["name"]: s for s in ref_items_res.get("slots", [])}
        ref_skills_res = ref_worker.call("list_skills", {})

        borrowed_items_plan: list[dict[str, Any]] = []
        for s_query in slots_to_borrow:
            resolved = resolve_slot_name(ref_worker, s_query, items_res=ref_items_res)
            r_slot = ref_slots_by_name.get(resolved)
            if not r_slot or not r_slot.get("selItemId"):
                raise ValueError(f"Reference build has no equipped item in slot '{resolved}'.")
            item_id = int(r_slot["selItemId"])
            raw_text = ref_worker.call("item_raw", {"id": item_id}).get("raw")
            borrowed_items_plan.append(
                {
                    "slotQuery": s_query,
                    "slotName": resolved,
                    "nodeId": r_slot.get("nodeId"),
                    "refItem": ref_items_by_id.get(item_id),
                    "raw": raw_text,
                }
            )

        ref_group_obj: dict[str, Any] | None = None
        copied_group_texts: list[tuple[str, str | None]] = []
        borrow_main_flag = False
        if skill_group:
            g_idx = resolve_skill_group_index(ref_skills_res, skill_group)
            ref_group_obj = next((g for g in ref_skills_res.get("groups", []) if g.get("index") == g_idx), None)
            if ref_group_obj is None:
                raise ValueError(f"Reference skill group #{g_idx} not found.")
            borrow_main_flag = str(skill_group).strip().lower() == "main" or bool(ref_group_obj.get("isMain"))
            src_slot = ref_group_obj.get("slot")
            if ref_group_obj.get("source"):
                if src_slot and not any(p["slotName"] == src_slot for p in borrowed_items_plan):
                    r_slot = ref_slots_by_name.get(src_slot)
                    if r_slot and r_slot.get("selItemId"):
                        item_id = int(r_slot["selItemId"])
                        raw_text = ref_worker.call("item_raw", {"id": item_id}).get("raw")
                        borrowed_items_plan.append(
                            {
                                "slotQuery": src_slot,
                                "slotName": src_slot,
                                "nodeId": r_slot.get("nodeId"),
                                "refItem": ref_items_by_id.get(item_id),
                                "raw": raw_text,
                                "autoBorrowedForSkillSource": True,
                            }
                        )
                if src_slot:
                    for sg in ref_skills_res.get("groups", []):
                        if not sg.get("source") and sg.get("slot") == src_slot and sg.get("enabled", True):
                            cp = ref_worker.call("copy_group", {"index": sg["index"]}).get("text")
                            if cp:
                                copied_group_texts.append((cp, src_slot))
            else:
                cp = ref_worker.call("copy_group", {"index": g_idx}).get("text")
                if cp:
                    copied_group_texts.append((cp, src_slot))

        def _do_borrow(w: BridgeWorker) -> dict[str, Any]:
            notices: list[str] = []
            equipped_items: list[dict[str, Any]] = []
            main_items_before = w.call("list_items", {})
            main_items_by_id = {it["id"]: it for it in main_items_before.get("items", [])}
            main_slots_by_name = {s["name"]: s for s in main_items_before.get("slots", [])}
            borrowed_slot_names: set[str] = set()
            resolved_plans: list[tuple[str, dict[str, Any]]] = []
            for plan in borrowed_items_plan:
                target_query = f"node:{plan['nodeId']}" if plan.get("nodeId") is not None else plan["slotName"]
                target_slot = (
                    resolve_slot_name(w, target_query, items_res=main_items_before)
                    if plan.get("nodeId") is not None
                    else plan["slotName"]
                )
                borrowed_slot_names.add(target_slot)
                resolved_plans.append((target_slot, plan))

            for target_slot, plan in resolved_plans:
                ref_item = plan.get("refItem") or {}
                ref_type = str(ref_item.get("type") or "")

                if target_slot == "Weapon 1" and ref_type in TWO_HANDED_TYPES and "Weapon 2" not in borrowed_slot_names:
                    w2_slot = main_slots_by_name.get("Weapon 2")
                    if w2_slot and w2_slot.get("selItemId"):
                        w.call("unequip_slot", {"slotName": "Weapon 2"})
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

                add_res = w.call("add_item", {"raw": plan["raw"], "slotName": target_slot, "equip": True})
                equipped_items.append({"slotName": target_slot, "item": add_res.get("item")})

            borrowed_skill_info = None
            if ref_group_obj:
                new_idx: int | None = None
                new_grp: dict[str, Any] = {}
                pasted_indices: list[int] = []
                for cp_text, target_slot_name in copied_group_texts:
                    paste_res = w.call("paste_group", {"text": cp_text})
                    p_idx = paste_res.get("index")
                    if p_idx is not None:
                        p_idx = int(p_idx)
                        pasted_indices.append(p_idx)
                        new_grp = paste_res.get("group") or {}
                        if target_slot_name:
                            set_res = w.call("set_group", {"index": p_idx, "slot": target_slot_name})
                            new_grp = set_res.get("group") or new_grp

                if ref_group_obj.get("source"):
                    src_slot = ref_group_obj.get("slot")
                    skills_after = w.call("list_skills", {})
                    for g in skills_after.get("groups", []):
                        if g.get("source") and (not src_slot or g.get("slot") == src_slot):
                            new_idx = int(g["index"])
                            new_grp = g
                            break
                elif pasted_indices:
                    new_idx = pasted_indices[-1]

                switched_main = False
                if borrow_main_flag and new_idx is not None:
                    w.call("set_build_field", {"field": "mainSocketGroup", "value": int(new_idx)})
                    switched_main = True
                    notices.append(f"Switched mainSocketGroup to borrowed skill group #{new_idx}.")

                borrowed_skill_info = {
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

            return {
                "borrowedItems": equipped_items,
                "borrowedSkillGroup": borrowed_skill_info,
                "notices": notices,
            }

        return run_mutation(main_worker, _do_borrow, apply=apply)
