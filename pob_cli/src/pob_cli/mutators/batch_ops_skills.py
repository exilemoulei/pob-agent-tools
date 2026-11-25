"""Skill-group and gem batch operation handlers for `mutators.batch_ops`."""

from __future__ import annotations

from typing import Any

from ..analyzer import resolve_skill_group_index
from ..bridge_client import BridgeWorker
from ..guardrails import GuardrailScope, check_input
from .common import (
    _BatchCache,
    _merge_cautions,
    _resolve_slot_from_items,
    _validate_gem_level,
)
from .skills import (
    _apply_add_group_in_place,
    _apply_replace_group_in_place,
    _guard_source_skill_group,
    _legacy_warn_cautions,
)
from .skills_resolve import (
    _add_gem_with_default_skill_flags,
    _resolve_gem_candidate,
    _resolve_gem_in_group,
)


def _op_add_group(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    full_dps_val = (
        op.get("includeInFullDPS")
        if "includeInFullDPS" in op
        else op.get("include_in_full_dps", op.get("fullDps"))
    )
    ag_res = _apply_add_group_in_place(
        w,
        gems=op.get("gems"),
        label=op.get("label"),
        slot=op.get("slot"),
        enabled=bool(op.get("enabled", True)),
        include_in_full_dps=bool(full_dps_val) if full_dps_val is not None else None,
        make_main=bool(op.get("makeMain", op.get("make_main", False))),
        allow_legacy=bool(op.get("allowLegacy", op.get("allow_legacy", False))),
        allow_illegal_level=bool(op.get("allowIllegalLevel", op.get("allow_illegal_level", False))),
        cache=cache,
    )
    ag_step: dict[str, Any] = {
        "step": idx,
        "op": "add_group",
        "groupIndex": ag_res["groupIndex"],
        "gemsCount": len(ag_res["gemsAdded"]),
    }
    if ag_res.get("cautions"):
        ag_step["cautions"] = ag_res["cautions"]
    return ag_step


def _op_delete_group(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    g_sel = op.get("group") if op.get("group") is not None else op.get("index")
    if g_sel is None:
        raw_kind = str(op.get("op") or op.get("type") or "delete_group")
        raise ValueError(f"Batch op '{raw_kind}' at step {idx} requires 'group' or 'index'.")
    g_idx = resolve_skill_group_index(cache.get_skills(), g_sel)
    w.call("delete_group", {"index": g_idx})
    cache.invalidate("skills")
    return {"step": idx, "op": "delete_group", "groupIndex": g_idx}


def _op_set_group(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    g_sel = op.get("group") if op.get("group") is not None else op.get("index", "main")
    g_idx = resolve_skill_group_index(cache.get_skills(), g_sel)
    sg_params: dict[str, Any] = {"index": g_idx}
    if "label" in op and op["label"] is not None:
        sg_params["label"] = str(op["label"])
    if "enabled" in op and op["enabled"] is not None:
        sg_params["enabled"] = bool(op["enabled"])
    full_dps_val = (
        op.get("includeInFullDPS")
        if "includeInFullDPS" in op
        else op.get("include_in_full_dps", op.get("fullDps"))
    )
    if full_dps_val is not None:
        sg_params["includeInFullDPS"] = bool(full_dps_val)
    if "slot" in op and op["slot"] is not None:
        s_clean = str(op["slot"]).strip()
        if s_clean:
            check_input(s_clean, scope=GuardrailScope.SLOT, expected_game="poe1")
        sg_params["slot"] = _resolve_slot_from_items(cache.get_items(), s_clean) if s_clean else ""
    if "mainActiveSkill" in op or "main_active_skill" in op:
        mas = op.get("mainActiveSkill", op.get("main_active_skill"))
        if mas is not None:
            sg_params["mainActiveSkill"] = int(mas)
    w.call("set_group", sg_params)
    cache.invalidate("skills")
    return {"step": idx, "op": "set_group", "groupIndex": g_idx, "updates": sg_params}


def _op_replace_group(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    g_sel = op.get("group") if op.get("group") is not None else op.get("index", "main")
    full_dps_val = (
        op.get("includeInFullDPS")
        if "includeInFullDPS" in op
        else op.get("include_in_full_dps", op.get("fullDps"))
    )
    rg_res = _apply_replace_group_in_place(
        w,
        g_sel,
        list(op.get("gems") or []),
        label=op.get("label"),
        slot=op.get("slot"),
        enabled=bool(op["enabled"]) if "enabled" in op and op["enabled"] is not None else None,
        include_in_full_dps=bool(full_dps_val) if full_dps_val is not None else None,
        allow_legacy=bool(op.get("allowLegacy", op.get("allow_legacy", False))),
        allow_illegal_level=bool(op.get("allowIllegalLevel", op.get("allow_illegal_level", False))),
        cache=cache,
    )
    rg_step: dict[str, Any] = {
        "step": idx,
        "op": "replace_group",
        "groupIndex": rg_res["groupIndex"],
        "removedCount": rg_res["removedCount"],
        "gemsCount": len(rg_res["gemsAdded"]),
    }
    if rg_res.get("cautions"):
        rg_step["cautions"] = rg_res["cautions"]
    return rg_step


def _op_skill(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    skills_res = cache.get_skills()
    g_idx = resolve_skill_group_index(skills_res, op.get("group", "main"))
    group = next((g for g in skills_res.get("groups", []) if g["index"] == g_idx), None)
    if group is None:
        raise ValueError(f"Skill group #{g_idx} not found in build.")

    grp_updates: dict[str, Any] = {"index": g_idx}
    full_dps_val = op.get("includeInFullDPS") if "includeInFullDPS" in op else op.get("include_in_full_dps")
    if full_dps_val is not None:
        grp_updates["includeInFullDPS"] = bool(full_dps_val)
    grp_en = op.get("groupEnabled") if "groupEnabled" in op else op.get("group_enabled")
    if grp_en is not None:
        grp_updates["enabled"] = bool(grp_en)
    if len(grp_updates) > 1:
        w.call("set_group", grp_updates)

    skill_cautions: list[str] = []
    allow_legacy = bool(op.get("allowLegacy", op.get("allow_legacy", False)))
    allow_illegal_level = bool(op.get("allowIllegalLevel", op.get("allow_illegal_level", False)))
    add_gem_name = op.get("addGem") or op.get("add_gem")
    remove_gem_sel = op.get("removeGem") if "removeGem" in op else op.get("remove_gem")
    if add_gem_name:
        gem_name = str(add_gem_name).strip()
        warns = check_input(
            gem_name,
            scope=GuardrailScope.GEM_NAME,
            expected_game="poe1",
            block_legacy_gems=not allow_legacy,
        )
        raw_level = op.get("level")
        req_level = int(raw_level) if raw_level is not None else None
        _, pre_c = _validate_gem_level(gem_name, req_level, allow_illegal_level=allow_illegal_level)
        cand = _resolve_gem_candidate(w, g_idx, gem_name)
        resolved_name = (cand.get("name") if cand else None) or gem_name
        eff_lv, res_c = _validate_gem_level(resolved_name, req_level, allow_illegal_level=allow_illegal_level)
        skill_cautions = _merge_cautions(_legacy_warn_cautions(warns), pre_c, res_c)
        add_params: dict[str, Any] = {
            "group": g_idx,
            "level": eff_lv if eff_lv is not None else 20,
            "quality": int(op["quality"]) if op.get("quality") is not None else 20,
            "enabled": bool(op["enabled"]) if op.get("enabled") is not None else True,
            "count": int(op["count"]) if op.get("count") is not None else 1,
        }
        if cand and cand.get("gemId"):
            add_params["gemId"] = cand["gemId"]
            add_params["nameSpec"] = resolved_name
        else:
            add_params["nameSpec"] = gem_name
        _add_gem_with_default_skill_flags(w, add_params)
    elif remove_gem_sel is not None:
        t_gem = _resolve_gem_in_group(group, remove_gem_sel)
        if group.get("source") and int(t_gem.get("index", 0)) == 1:
            _guard_source_skill_group(group, action="remove gem #1")
        w.call("delete_gem", {"group": g_idx, "index": t_gem["index"]})
    elif op.get("gem") is not None:
        t_gem = _resolve_gem_in_group(group, op["gem"])
        gem_params: dict[str, Any] = {"group": g_idx, "index": t_gem["index"]}
        new_gem_name = op.get("newGem") or op.get("new_gem")
        req_level = int(op["level"]) if ("level" in op and op["level"] is not None) else None
        if new_gem_name:
            warns = check_input(
                str(new_gem_name),
                scope=GuardrailScope.GEM_NAME,
                expected_game="poe1",
                block_legacy_gems=not allow_legacy,
            )
            _, pre_c = _validate_gem_level(str(new_gem_name).strip(), req_level, allow_illegal_level=allow_illegal_level)
            cand = _resolve_gem_candidate(w, g_idx, str(new_gem_name))
            resolved_name = (cand.get("name") if cand else None) or str(new_gem_name).strip()
            eff_lv, res_c = _validate_gem_level(resolved_name, req_level, allow_illegal_level=allow_illegal_level)
            skill_cautions = _merge_cautions(_legacy_warn_cautions(warns), pre_c, res_c)
            if cand and cand.get("gemId"):
                gem_params["gemId"] = cand["gemId"]
                gem_params["nameSpec"] = resolved_name
            else:
                gem_params["nameSpec"] = str(new_gem_name).strip()
            if req_level is not None or (eff_lv is not None and eff_lv < 20):
                gem_params["level"] = eff_lv
        elif req_level is not None:
            existing_name = str(t_gem.get("name") or t_gem.get("nameSpec") or "").strip()
            eff_lv, res_c = _validate_gem_level(existing_name, req_level, allow_illegal_level=allow_illegal_level)
            skill_cautions = _merge_cautions(skill_cautions, res_c)
            gem_params["level"] = eff_lv
        for k in ("quality", "enabled", "count"):
            if k in op:
                gem_params[k] = op[k]
        w.call("set_gem", gem_params)

    cache.invalidate("skills")
    sk_step: dict[str, Any] = {"step": idx, "op": "skill", "group": g_idx}
    if skill_cautions:
        sk_step["cautions"] = skill_cautions
    return sk_step
