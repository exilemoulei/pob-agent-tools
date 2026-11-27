"""3-baseline dual-build comparison (`compare_builds`)."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..analyzer import dewater_smart_in_place, inspect_tree
from ..bridge_client import BridgeWorker
from ..formatters import _parse_numeric_or_none, fmt_delta, fmt_num, strip_pob_escapes
from ..importers import share_code_to_xml
from ..snapshot import (
    CORE_STAT_KEYS,
    BuildTransaction,
    _call_batch,
    _json_safe_stat,
    capture_digest,
    load_build,
)
from .borrow import TWO_HANDED_TYPES, borrow_from_ref

__all__ = [
    "COMPARE_BUDGET_S",
    "EXTERNAL_ENV_WHITELIST",
    "TWO_HANDED_TYPES",
    "_apply_env_config",
    "_build_pair_table",
    "_extract_env_config",
    "_extract_env_config_from_cfg",
    "_run_worker_pair",
    "borrow_from_ref",
    "compare_builds",
]

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

_VISCERA_SLOT_NAMES = ("Head", "Body", "Arm", "Leg")


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


def _fmt_item_label(item: dict[str, Any] | None) -> str:
    if not item:
        return "-"
    return strip_pob_escapes(item.get("titleZh") or item.get("title") or item.get("nameZh") or item.get("name") or "-")


def _fmt_item_base(item: dict[str, Any] | None) -> str:
    if not item:
        return "-"
    return strip_pob_escapes(item.get("baseNameZh") or item.get("baseName") or item.get("base") or item.get("type") or "-")


def _allocated_socket_node_ids(tree_summary: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for sock in tree_summary.get("jewelSockets") or []:
        if isinstance(sock, dict) and sock.get("nodeId") is not None and sock.get("allocated"):
            out.add(int(sock["nodeId"]))
    return out


def _build_jewel_socket_diff(
    main_tree: dict[str, Any],
    ref_tree: dict[str, Any],
    main_items_res: dict[str, Any],
    ref_items_res: dict[str, Any],
    m_items_by_id: dict[int, dict[str, Any]],
    r_items_by_id: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    m_alloc = _allocated_socket_node_ids(main_tree)
    r_alloc = _allocated_socket_node_ids(ref_tree)
    m_by_nid = {
        int(s["nodeId"]): s
        for s in main_items_res.get("slots", [])
        if s.get("nodeId") is not None and not s.get("inactive")
    }
    r_by_nid = {
        int(s["nodeId"]): s
        for s in ref_items_res.get("slots", [])
        if s.get("nodeId") is not None and not s.get("inactive")
    }
    all_nids = sorted(set(m_by_nid) | set(r_by_nid) | m_alloc | r_alloc)
    diff: list[dict[str, Any]] = []
    for nid in all_nids:
        ms = m_by_nid.get(nid, {})
        rs = r_by_nid.get(nid, {})
        m_it = m_items_by_id.get(ms.get("selItemId") or 0)
        r_it = r_items_by_id.get(rs.get("selItemId") or 0)
        main_allocated = (nid in m_alloc) or (m_it is not None)
        ref_allocated = (nid in r_alloc) or (r_it is not None)
        if not main_allocated and not ref_allocated:
            continue
        m_lbl, r_lbl = _fmt_item_label(m_it), _fmt_item_label(r_it)
        m_base, r_base = _fmt_item_base(m_it), _fmt_item_base(r_it)
        if m_it is not None and r_it is None:
            status = "only_in_main"
        elif r_it is not None and m_it is None:
            status = "only_in_ref"
        elif m_it is None and r_it is None:
            status = "same" if main_allocated == ref_allocated else ("only_in_main" if main_allocated else "only_in_ref")
        else:
            status = "same" if (m_lbl == r_lbl and m_base == r_base) else "changed"
        diff.append(
            {
                "nodeId": nid,
                "mainAllocated": main_allocated,
                "refAllocated": ref_allocated,
                "mainItem": m_lbl,
                "mainBase": m_base,
                "mainRarity": m_it.get("rarity") if m_it else "-",
                "refItem": r_lbl,
                "refBase": r_base,
                "refRarity": r_it.get("rarity") if r_it else "-",
                "status": status,
            }
        )
    return diff


def _build_compare_structural_diffs(
    main_tree: dict[str, Any],
    ref_tree: dict[str, Any],
    main_skills: dict[str, Any],
    ref_skills: dict[str, Any],
    main_items_res: dict[str, Any],
    ref_items_res: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
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

    slot_names = list(_COMPARE_SLOT_NAMES)
    for vname in _VISCERA_SLOT_NAMES:
        if (m_slots.get(vname) or {}).get("selItemId") or (r_slots.get(vname) or {}).get("selItemId"):
            slot_names.append(vname)

    slot_comparison: list[dict[str, Any]] = []
    for sname in slot_names:
        ms = m_slots.get(sname, {})
        rs = r_slots.get(sname, {})
        m_it = m_items_by_id.get(ms.get("selItemId") or 0)
        r_it = r_items_by_id.get(rs.get("selItemId") or 0)
        slot_comparison.append(
            {
                "slot": sname,
                "mainItem": _fmt_item_label(m_it),
                "mainRarity": m_it.get("rarity") if m_it else "-",
                "refItem": _fmt_item_label(r_it),
                "refRarity": r_it.get("rarity") if r_it else "-",
            }
        )

    jewel_socket_diff = _build_jewel_socket_diff(
        main_tree, ref_tree, main_items_res, ref_items_res, m_items_by_id, r_items_by_id
    )
    return tree_diff, skill_diff, slot_comparison, jewel_socket_diff


# The CLI waits 150 s for `pob compare`; the margin covers the reply.
COMPARE_BUDGET_S = 140.0
# With less left, attribution would likely time out, and a timeout restarts the worker.
_MIN_ATTRIBUTION_S = 15.0


def _power_attribution(
    main_worker: BridgeWorker, ref_snap: str, stat: str, remaining_s: float
) -> tuple[dict[str, Any] | None, str | None]:
    """`power_diff_attribution` within the time left: (result, error)."""
    if remaining_s < _MIN_ATTRIBUTION_S:
        return None, f"skipped: only {max(0.0, remaining_s):.0f}s of the compare budget left"
    try:
        ref_xml = ref_snap if ref_snap.lstrip().startswith("<") else share_code_to_xml(ref_snap)
    except Exception:
        ref_xml = ref_snap
    try:
        with main_worker.lock:
            res = main_worker.call(
                "power_diff_attribution",
                {"compareXml": ref_xml, "stat": stat},
                timeout=min(60.0, remaining_s),
            )
    except Exception as exc:
        return None, f"failed: {str(exc)[:200]}"
    if isinstance(res, dict) and not res.get("_error"):
        return res, None
    return None, f"failed: {str(res.get('_error') if isinstance(res, dict) else res)[:200]}"


def compare_builds(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    ref_source: str | None = None,
    ref_xml_code: str | None = None,
    ref_stage: str | None = None,
    ref_spec: str | None = None,
    ref_item_set: str | None = None,
    ref_skill_set: str | None = None,
    keep: Iterable[str] | str | None = None,
    auto_keep: bool = True,
    budget_s: float = COMPARE_BUDGET_S,
) -> dict[str, Any]:
    """Compare `main` build vs `ref` build across 3 baselines + tree, skill, and item diffs."""
    started = time.monotonic()
    if ref_source or ref_xml_code:
        load_build(
            ref_worker,
            source=ref_source,
            xml_code=ref_xml_code,
            stage=ref_stage,
            spec=ref_spec,
            item_set=ref_item_set,
            skill_set=ref_skill_set,
        )

    def _capture_main_phase1() -> dict[str, Any]:
        with main_worker.lock:
            orig = capture_digest(main_worker)
            tree = inspect_tree(main_worker)
            skills, items, cfg = _call_batch(
                main_worker,
                [("list_skills", {}), ("list_items", {}), ("list_config", {})],
            )
            return {
                "orig": orig,
                "tree": tree,
                "skills": skills,
                "items": items,
                "env": _extract_env_config_from_cfg(cfg),
            }

    def _capture_ref_phase1() -> dict[str, Any]:
        with ref_worker.lock:
            orig = capture_digest(ref_worker)
            tree = inspect_tree(ref_worker)
            skills, items, exp = _call_batch(
                ref_worker,
                [("list_skills", {}), ("list_items", {}), ("export_code", {})],
            )
            return {
                "orig": orig,
                "tree": tree,
                "skills": skills,
                "items": items,
                "snap": exp["code"],
            }

    main_p1, ref_p1 = _run_worker_pair(main_worker, ref_worker, _capture_main_phase1, _capture_ref_phase1)
    main_orig, ref_orig = main_p1["orig"], ref_p1["orig"]
    main_env, ref_snap = main_p1["env"], ref_p1["snap"]
    baseline_1 = _build_pair_table(main_orig, ref_orig)

    tree_diff, skill_diff, slot_comparison, jewel_socket_diff = _build_compare_structural_diffs(
        main_p1["tree"],
        ref_p1["tree"],
        main_p1["skills"],
        ref_p1["skills"],
        main_p1["items"],
        ref_p1["items"],
    )

    def _eval_main_phase2() -> tuple[dict[str, list[str]], dict[str, Any]]:
        with BuildTransaction(main_worker) as tx:
            stripped = dewater_smart_in_place(
                main_worker,
                keep=keep,
                auto_keep=auto_keep,
                stats=main_orig.get("stats"),
                skills_res=main_p1["skills"],
                items_res=main_p1["items"],
                build_info=main_orig.get("buildInfo"),
                tree_summary=main_p1["tree"],
            )
            dewatered = capture_digest(main_worker)
            tx.rollback_verified(main_orig)
            return stripped, dewatered

    def _eval_ref_phase2() -> tuple[dict[str, Any], dict[str, list[str]], dict[str, Any]]:
        with BuildTransaction(ref_worker) as tx:
            _apply_env_config(ref_worker, main_env)
            env_aligned = capture_digest(ref_worker)
            tx.restore()
            stripped = dewater_smart_in_place(
                ref_worker,
                keep=keep,
                auto_keep=auto_keep,
                stats=ref_orig.get("stats"),
                skills_res=ref_p1["skills"],
                items_res=ref_p1["items"],
                build_info=ref_orig.get("buildInfo"),
                tree_summary=ref_p1["tree"],
            )
            dewatered = capture_digest(ref_worker)
            tx.rollback_verified(ref_orig)
            return env_aligned, stripped, dewatered

    (main_stripped, main_dewatered), (ref_env_aligned, ref_stripped, ref_dewatered) = _run_worker_pair(
        main_worker,
        ref_worker,
        _eval_main_phase2,
        _eval_ref_phase2,
    )

    power_attribution: dict[str, Any] | None = None
    attribution_error: str | None = None
    if (getattr(main_worker, "caps", None) or {}).get("powerAttribution") and ref_snap:
        power_attribution, attribution_error = _power_attribution(
            main_worker,
            ref_snap,
            main_orig.get("primaryDpsKey") or "CombinedDPS",
            budget_s - (time.monotonic() - started),
        )

    out_cmp: dict[str, Any] = {
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
        "jewelSocketDiff": jewel_socket_diff,
    }
    if power_attribution is not None:
        out_cmp["powerAttribution"] = power_attribution
    if attribution_error:
        out_cmp["powerAttributionError"] = attribution_error
    return out_cmp
