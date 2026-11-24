"""3-baseline dual-build comparison (`compare_builds`) and cross-build transplant (`borrow_from_ref`)."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..analyzer import (
    dewater_build_in_place,
    inspect_tree,
    resolve_skill_group_index,
    resolve_slot_name,
)
from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none, fmt_delta, fmt_num, strip_pob_escapes
from ..importers import load_build_into_worker
from ..snapshot import (
    CORE_STAT_KEYS,
    _call_batch,
    _json_safe_stat,
    capture_digest,
    run_mutation,
)

EXTERNAL_ENV_WHITELIST = (
    "enemyIsBoss",
    "enemyLevel",
    "deliriousPercentage",
    "presetBossSkills",
    "enemyPhysicalReduction",
    "enemyLightningResist",
    "enemyColdResist",
    "enemyFireResist",
    "enemyChaosResist",
    "enemyMaxResist",
    "EHPUnluckyWorstOf",
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

_COMPARE_SLOT_NAMES = (
    "Weapon 1",
    "Weapon 2",
    "Helmet",
    "Body Armour",
    "Gloves",
    "Boots",
    "Amulet",
    "Ring 1",
    "Ring 2",
    "Belt",
    "Flask 1",
    "Flask 2",
    "Flask 3",
    "Flask 4",
    "Flask 5",
)


def _extract_env_config_from_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sec in cfg.get("sections", []):
        for item in sec.get("items", []):
            var = item.get("var")
            if var in EXTERNAL_ENV_WHITELIST:
                out[var] = item.get("value")
    return out


def _extract_env_config(worker: BridgeWorker) -> dict[str, Any]:
    cfg = worker.call("list_config", {})
    return _extract_env_config_from_cfg(cfg)


def _apply_env_config(worker: BridgeWorker, env_map: dict[str, Any]) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    for var in EXTERNAL_ENV_WHITELIST:
        val = env_map.get(var)
        if val is None or val is False or val == "":
            calls.append(("reset_config", {"var": var}))
        else:
            calls.append(("set_config", {"var": var, "value": val}))
    if env_map.get("presetBossSkills"):
        calls.append(("set_config", {"var": "presetBossSkills", "value": env_map["presetBossSkills"]}))
    if calls:
        _call_batch(worker, calls)


def _build_pair_table(main_dig: dict[str, Any], ref_dig: dict[str, Any]) -> list[dict[str, Any]]:
    m_stats = main_dig.get("stats", {})
    r_stats = ref_dig.get("stats", {})
    rows: list[dict[str, Any]] = []
    for key, label, unit in CORE_STAT_KEYS:
        m_raw = m_stats.get(key)
        r_raw = r_stats.get(key)
        mf = _parse_numeric_or_none(m_raw)
        rf = _parse_numeric_or_none(r_raw)
        if mf is not None and rf is not None:
            if math.isfinite(mf) and math.isfinite(rf) and abs(mf) < 1e-4 and abs(rf) < 1e-4:
                continue
        elif m_raw is None and r_raw is None:
            continue
        rows.append(
            {
                "stat": key,
                "label": label,
                "main": _json_safe_stat(m_raw),
                "ref": _json_safe_stat(r_raw),
                "mainStr": f"{fmt_num(m_raw if m_raw is not None else 0.0)}{unit}",
                "refStr": f"{fmt_num(r_raw if r_raw is not None else 0.0)}{unit}",
                "diffRefVsMainStr": fmt_delta(
                    m_raw if m_raw is not None else 0.0,
                    r_raw if r_raw is not None else 0.0,
                    unit,
                ),
            }
        )
    return rows


def _run_worker_pair(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    fn_main: Any,
    fn_ref: Any,
) -> tuple[Any, Any]:
    """Run independent main/ref evaluation closures concurrently unless both share a worker or lock."""
    same_lock = (main_worker is ref_worker) or (
        getattr(main_worker, "lock", None) is getattr(ref_worker, "lock", None)
    )
    if same_lock:
        return fn_main(), fn_ref()
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_main = pool.submit(fn_main)
        fut_ref = pool.submit(fn_ref)
        return fut_main.result(), fut_ref.result()


def _diff_node_lists(m_list: list[dict[str, Any]], r_list: list[dict[str, Any]]) -> dict[str, Any]:
    m_map = {
        int(n["id"]): strip_pob_escapes(n.get("nameZh") or n.get("name") or str(n["id"]))
        for n in m_list
    }
    r_map = {
        int(n["id"]): strip_pob_escapes(n.get("nameZh") or n.get("name") or str(n["id"]))
        for n in r_list
    }
    m_ids = set(m_map.keys())
    r_ids = set(r_map.keys())
    return {
        "onlyInMain": [{"id": i, "name": m_map[i]} for i in sorted(m_ids - r_ids)],
        "onlyInRef": [{"id": i, "name": r_map[i]} for i in sorted(r_ids - m_ids)],
        "sharedCount": len(m_ids & r_ids),
    }


def _active_skill_names(skills_res: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for g in skills_res.get("groups", []):
        if not g.get("enabled", True):
            continue
        for gm in g.get("gems", []):
            if gm.get("enabled", True) and not gm.get("support"):
                nm = strip_pob_escapes(gm.get("nameZh") or gm.get("name") or gm.get("nameSpec") or "")
                if nm:
                    names.add(nm)
    return names


def _build_compare_structural_diffs(
    main_tree: dict[str, Any],
    ref_tree: dict[str, Any],
    main_skills: dict[str, Any],
    ref_skills: dict[str, Any],
    main_items_res: dict[str, Any],
    ref_items_res: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    tree_diff = {
        "mainPoints": main_tree.get("points"),
        "refPoints": ref_tree.get("points"),
        "keystones": _diff_node_lists(main_tree.get("keystones", []), ref_tree.get("keystones", [])),
        "ascendancy": _diff_node_lists(main_tree.get("ascendancyNodes", []), ref_tree.get("ascendancyNodes", [])),
        "notables": _diff_node_lists(main_tree.get("notables", []), ref_tree.get("notables", [])),
        "masteries": _diff_node_lists(main_tree.get("masteries", []), ref_tree.get("masteries", [])),
    }

    m_act = _active_skill_names(main_skills)
    r_act = _active_skill_names(ref_skills)
    m_main_idx = main_skills.get("mainSocketGroup") or 1
    r_main_idx = ref_skills.get("mainSocketGroup") or 1
    m_main_grp = next((g for g in main_skills.get("groups", []) if g["index"] == m_main_idx), None)
    r_main_grp = next((g for g in ref_skills.get("groups", []) if g["index"] == r_main_idx), None)
    skill_diff = {
        "mainGroupMain": m_main_grp,
        "refGroupMain": r_main_grp,
        "activeSkillsOnlyInMain": sorted(m_act - r_act),
        "activeSkillsOnlyInRef": sorted(r_act - m_act),
        "sharedActiveSkills": sorted(m_act & r_act),
    }

    m_items_by_id = {it["id"]: it for it in main_items_res.get("items", [])}
    r_items_by_id = {it["id"]: it for it in ref_items_res.get("items", [])}
    m_slots = {s["name"]: s for s in main_items_res.get("slots", [])}
    r_slots = {s["name"]: s for s in ref_items_res.get("slots", [])}

    slot_comparison: list[dict[str, Any]] = []
    for sname in _COMPARE_SLOT_NAMES:
        ms = m_slots.get(sname, {})
        rs = r_slots.get(sname, {})
        m_it = m_items_by_id.get(ms.get("selItemId") or 0)
        r_it = r_items_by_id.get(rs.get("selItemId") or 0)
        slot_comparison.append(
            {
                "slot": sname,
                "mainItem": strip_pob_escapes(
                    (m_it.get("titleZh") or m_it.get("title") or m_it.get("nameZh") or m_it.get("name"))
                    if m_it
                    else "-"
                ),
                "mainRarity": m_it.get("rarity") if m_it else "-",
                "refItem": strip_pob_escapes(
                    (r_it.get("titleZh") or r_it.get("title") or r_it.get("nameZh") or r_it.get("name"))
                    if r_it
                    else "-"
                ),
                "refRarity": r_it.get("rarity") if r_it else "-",
            }
        )

    return tree_diff, skill_diff, slot_comparison


def compare_builds(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    ref_source: str | None = None,
    ref_xml_code: str | None = None,
) -> dict[str, Any]:
    """Compare `main` build vs `ref` build across 3 baselines + tree, skill, and item diffs."""
    if ref_source or ref_xml_code:
        load_build_into_worker(ref_worker, source=ref_source, xml_code=ref_xml_code)

    def _capture_main_phase1() -> dict[str, Any]:
        with main_worker.lock:
            prev_last_good = getattr(main_worker, "last_good_code", None)
            orig = capture_digest(main_worker)
            tree = inspect_tree(main_worker)
            skills, items, cfg, exp = _call_batch(
                main_worker,
                [("list_skills", {}), ("list_items", {}), ("list_config", {}), ("export_code", {})],
            )
            if hasattr(main_worker, "last_good_code"):
                main_worker.last_good_code = prev_last_good
            return {
                "orig": orig,
                "tree": tree,
                "skills": skills,
                "items": items,
                "env": _extract_env_config_from_cfg(cfg),
                "snap": exp["code"],
                "prev_last_good": prev_last_good,
            }

    def _capture_ref_phase1() -> dict[str, Any]:
        with ref_worker.lock:
            prev_last_good = getattr(ref_worker, "last_good_code", None)
            orig = capture_digest(ref_worker)
            tree = inspect_tree(ref_worker)
            skills, items, exp = _call_batch(
                ref_worker,
                [("list_skills", {}), ("list_items", {}), ("export_code", {})],
            )
            if hasattr(ref_worker, "last_good_code"):
                ref_worker.last_good_code = prev_last_good
            return {
                "orig": orig,
                "tree": tree,
                "skills": skills,
                "items": items,
                "snap": exp["code"],
                "prev_last_good": prev_last_good,
            }

    main_p1, ref_p1 = _run_worker_pair(main_worker, ref_worker, _capture_main_phase1, _capture_ref_phase1)
    main_orig, ref_orig = main_p1["orig"], ref_p1["orig"]
    main_env, main_snap, ref_snap = main_p1["env"], main_p1["snap"], ref_p1["snap"]
    baseline_1 = _build_pair_table(main_orig, ref_orig)

    tree_diff, skill_diff, slot_comparison = _build_compare_structural_diffs(
        main_p1["tree"],
        ref_p1["tree"],
        main_p1["skills"],
        ref_p1["skills"],
        main_p1["items"],
        ref_p1["items"],
    )

    main_prev_last_good = main_p1["prev_last_good"]
    ref_prev_last_good = ref_p1["prev_last_good"]

    def _eval_main_phase2() -> tuple[dict[str, list[str]], dict[str, Any]]:
        with main_worker.lock:
            try:
                stripped = dewater_build_in_place(main_worker)
                dewatered = capture_digest(main_worker)
                return stripped, dewatered
            finally:
                main_worker.call("import_code", {"code": main_snap, "mode": "replace"})
                if hasattr(main_worker, "last_good_code"):
                    main_worker.last_good_code = main_prev_last_good

    def _eval_ref_phase2() -> tuple[dict[str, Any], dict[str, list[str]], dict[str, Any]]:
        with ref_worker.lock:
            try:
                _apply_env_config(ref_worker, main_env)
                env_aligned = capture_digest(ref_worker)
            finally:
                ref_worker.call("import_code", {"code": ref_snap, "mode": "replace"})
                if hasattr(ref_worker, "last_good_code"):
                    ref_worker.last_good_code = ref_prev_last_good

            try:
                stripped = dewater_build_in_place(ref_worker)
                dewatered = capture_digest(ref_worker)
                return env_aligned, stripped, dewatered
            finally:
                ref_worker.call("import_code", {"code": ref_snap, "mode": "replace"})
                if hasattr(ref_worker, "last_good_code"):
                    ref_worker.last_good_code = ref_prev_last_good

    (main_stripped, main_dewatered), (ref_env_aligned, ref_stripped, ref_dewatered) = _run_worker_pair(
        main_worker,
        ref_worker,
        _eval_main_phase2,
        _eval_ref_phase2,
    )

    return {
        "mainBuildInfo": main_orig["buildInfo"],
        "refBuildInfo": ref_orig["buildInfo"],
        "baselines": {
            "original": baseline_1,
            "envAlignedToMain": {"envConfig": main_env, "rows": _build_pair_table(main_orig, ref_env_aligned)},
            "pinnacleDewatered": {
                "mainStripped": main_stripped,
                "refStripped": ref_stripped,
                "rows": _build_pair_table(main_dewatered, ref_dewatered),
            },
        },
        "treeDiff": tree_diff,
        "skillDiff": skill_diff,
        "itemSlotDiff": slot_comparison,
    }


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
            resolved = resolve_slot_name(ref_worker, s_query)
            r_slot = ref_slots_by_name.get(resolved)
            if not r_slot or not r_slot.get("selItemId"):
                raise ValueError(f"Reference build has no equipped item in slot '{resolved}'.")
            item_id = int(r_slot["selItemId"])
            raw_text = ref_worker.call("item_raw", {"id": item_id}).get("raw")
            borrowed_items_plan.append(
                {"slotName": resolved, "refItem": ref_items_by_id.get(item_id), "raw": raw_text}
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
                                "slotName": src_slot,
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
            borrowed_slot_names = {p["slotName"] for p in borrowed_items_plan}

            for plan in borrowed_items_plan:
                target_slot = plan["slotName"]
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
