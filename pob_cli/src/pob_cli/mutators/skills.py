"""Skill group and gem mutation operations (`add_skill_group`, `replace_skill_group`, `modify_skill`, etc.)."""

from __future__ import annotations

from typing import Any

from ..analyzer import resolve_skill_group_index
from ..bridge_client import BridgeTimeoutError, BridgeWorker
from ..guardrails import GuardrailScope, check_input
from ..snapshot import run_mutation
from .common import (
    _BatchCache,
    _call_batch,
    _merge_cautions,
    _resolve_slot_from_items,
    _validate_gem_level,
)
from .skills_resolve import (
    _add_gem_with_default_skill_flags,
    _apply_vaal_defaults_batch,
    _build_add_gem_params,
    _build_explicit_gem_state,
    _build_replaced_group_state_params,
    _build_specs_gem_states,
    _extract_explicit_gem_level,
    _normalize_gem_spec,
    _parse_positive_int,
    _resolve_and_validate_specs,
    _resolve_gem_candidate,
    _resolve_gem_in_group,
)


def _legacy_warn_cautions(warns: list[Any]) -> list[str]:
    return [f"[POE1-PATCH-WARN] {w.summary}" for w in warns if getattr(w, "category", "") == "legacy_patch"]


_UNKNOWN_RPC_TOKENS = ("unknown", "unsupported", "unexpected", "unrecognized", "not implemented", "unhandled")
_FATAL_RPC_TOKENS = ("lua_error", "crashed", "not running", "pipe", "timed out")


def _try_call_ext(w: BridgeWorker, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Invoke an optional `skill_calcs_ext` RPC, returning `None` if a fake/older worker lacks `method`."""
    try:
        res = w.call(method, params)
    except BridgeTimeoutError:
        raise
    except NotImplementedError:
        return None
    except (AssertionError, KeyError, ValueError, RuntimeError) as exc:
        raw_msg = str(exc).strip(" '\"")
        msg_low = raw_msg.lower()
        if any(tok in msg_low for tok in _FATAL_RPC_TOKENS):
            raise
        if raw_msg == method or any(tok in msg_low for tok in _UNKNOWN_RPC_TOKENS):
            return None
        raise
    if not isinstance(res, dict) or res.get("_error"):
        return None
    return res


def _precheck_gem_name(
    gem_name: str, level: int | None, *, allow_legacy: bool, allow_illegal_level: bool
) -> list[str]:
    warns = check_input(
        gem_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=not allow_legacy
    )
    _, lv_c = _validate_gem_level(gem_name.strip(), level, allow_illegal_level=allow_illegal_level)
    return _merge_cautions(_legacy_warn_cautions(warns), lv_c)


def _with_cautions(res: dict[str, Any]) -> dict[str, Any]:
    cautions = (res.get("detail") or {}).get("cautions")
    if cautions:
        res["cautions"] = list(cautions)
    return res


def _apply_add_group_in_place(
    w: BridgeWorker,
    *,
    gems: list[str | dict[str, Any]] | None = None,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool = True,
    include_in_full_dps: bool | None = None,
    make_main: bool = False,
    allow_legacy: bool = False,
    allow_illegal_level: bool = False,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    resolved_slot: str | None = None
    if slot is not None and str(slot).strip():
        check_input(str(slot), scope=GuardrailScope.SLOT, expected_game="poe1")
        items_res = cache.get_items() if cache is not None else w.call("list_items", {})
        resolved_slot = _resolve_slot_from_items(items_res, str(slot))
    cautions: list[str] = []
    for g_entry in gems or []:
        g_name = (
            g_entry
            if isinstance(g_entry, str)
            else str(g_entry.get("name") or g_entry.get("nameSpec") or g_entry.get("gem") or "")
        )
        if g_name:
            warns = check_input(
                g_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=not allow_legacy
            )
            cautions = _merge_cautions(cautions, _legacy_warn_cautions(warns))

    add_params: dict[str, Any] = {}
    if label is not None:
        add_params["label"] = str(label)
    if resolved_slot:
        add_params["slot"] = resolved_slot
    g_idx = int(w.call("add_group", add_params)["index"])

    group_updates: dict[str, Any] = {"index": g_idx}
    if enabled is False:
        group_updates["enabled"] = False
    if include_in_full_dps is not None:
        group_updates["includeInFullDPS"] = bool(include_in_full_dps)
    if len(group_updates) > 1:
        w.call("set_group", group_updates)

    added_gems: list[dict[str, Any]] = []
    if gems:
        explicit_levels = [_extract_explicit_gem_level(g) for g in gems]
        norm_specs = [_normalize_gem_spec(g, allow_illegal_level=allow_illegal_level) for g in gems]
        resolved_specs, spec_c = _resolve_and_validate_specs(
            w, g_idx, norm_specs, explicit_levels, allow_illegal_level=allow_illegal_level
        )
        cautions = _merge_cautions(cautions, spec_c)
        gem_results = _call_batch(w, [("add_gem", _build_add_gem_params(g_idx, sp)) for sp in resolved_specs])
        added_gems = _apply_vaal_defaults_batch(w, g_idx, resolved_specs, gem_results)
        spec_states = _build_specs_gem_states(resolved_specs, added_gems)
        if spec_states:
            _try_call_ext(w, "apply_group_gem_states", {"group": g_idx, "gemStates": spec_states})

    if make_main:
        w.call("set_build_field", {"field": "mainSocketGroup", "value": g_idx})
    if cache is not None:
        cache.invalidate("skills")

    detail: dict[str, Any] = {
        "groupIndex": g_idx,
        "label": str(label) if label is not None else "",
        "slot": resolved_slot,
        "enabled": bool(enabled),
        "includeInFullDPS": include_in_full_dps,
        "makeMain": bool(make_main),
        "gemsAdded": added_gems,
    }
    if cautions:
        detail["cautions"] = cautions
    return detail


_apply_new_group_in_place = _apply_add_group_in_place


def add_skill_group(
    worker: BridgeWorker,
    *,
    gems: list[str | dict[str, Any]] | None = None,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool = True,
    include_in_full_dps: bool | None = None,
    make_main: bool = False,
    allow_legacy: bool = False,
    allow_illegal_level: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Create a new skill socket group with optional gems, slot, label, enabled, and FullDPS flag."""
    for g in gems or []:
        _normalize_gem_spec(g, allow_illegal_level=allow_illegal_level)
    return _with_cautions(
        run_mutation(
            worker,
            lambda w: _apply_add_group_in_place(
                w,
                gems=gems,
                label=label,
                slot=slot,
                enabled=enabled,
                include_in_full_dps=include_in_full_dps,
                make_main=make_main,
                allow_legacy=allow_legacy,
                allow_illegal_level=allow_illegal_level,
            ),
            apply=apply,
        )
    )


def _guard_source_skill_group(grp: dict[str, Any], *, action: str) -> None:
    source = grp.get("source")
    if not source:
        return
    slot = grp.get("slot") or ""
    slot_hint = f"assigned to slot='{slot}'" if slot else "assigned to the granting item's slot"
    label = grp.get("displayLabel") or grp.get("label") or f"group #{grp.get('index')}"
    raise ValueError(
        f"Cannot {action} on granted skill group '{label}' (source='{source}'). "
        "PoB deletes and recreates granted groups when gem #1 is removed, shifting group indices. "
        f"Place support gems in a regular skill group {slot_hint} instead."
    )


def _apply_replace_group_in_place(
    w: BridgeWorker,
    group_selector: str | int,
    gems: list[str | dict[str, Any]],
    *,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool | None = None,
    include_in_full_dps: bool | None = None,
    allow_legacy: bool = False,
    allow_illegal_level: bool = False,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    skills_res = cache.get_skills() if cache is not None else w.call("list_skills", {})
    g_idx = resolve_skill_group_index(skills_res, group_selector)
    grp = next((g for g in skills_res.get("groups", []) if g["index"] == g_idx), None)
    if grp is None:
        raise ValueError(f"Skill group #{g_idx} not found in build.")
    _guard_source_skill_group(grp, action="replace gems")

    if slot is not None and str(slot).strip():
        check_input(str(slot), scope=GuardrailScope.SLOT, expected_game="poe1")
    raw_gems = list(gems or [])
    explicit_levels = [_extract_explicit_gem_level(g) for g in raw_gems]
    norm_specs = [_normalize_gem_spec(g, allow_illegal_level=allow_illegal_level) for g in raw_gems]
    cautions: list[str] = []
    for sp in norm_specs:
        if sp.get("name"):
            warns = check_input(
                str(sp["name"]), scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=not allow_legacy
            )
            cautions = _merge_cautions(cautions, _legacy_warn_cautions(warns))
    resolved_specs, spec_c = _resolve_and_validate_specs(
        w, g_idx, norm_specs, explicit_levels, allow_illegal_level=allow_illegal_level
    )
    cautions = _merge_cautions(cautions, spec_c)

    old_gems = list(grp.get("gems", []))
    prior_state: dict[str, Any] | None = None
    if old_gems:
        snap = _try_call_ext(w, "get_group_gem_states", {"group": g_idx})
        if isinstance(snap, dict) and isinstance(snap.get("gems"), list):
            prior_state = snap
        del_calls = [
            ("delete_gem", {"group": g_idx, "index": int(gm["index"])})
            for gm in sorted(old_gems, key=lambda x: int(x["index"]), reverse=True)
        ]
        _call_batch(w, del_calls)

    group_updates: dict[str, Any] = {"index": g_idx}
    if label is not None:
        group_updates["label"] = str(label)
    if slot is not None:
        s_clean = str(slot).strip()
        if s_clean:
            items_res = cache.get_items() if cache is not None else w.call("list_items", {})
            group_updates["slot"] = _resolve_slot_from_items(items_res, s_clean)
        else:
            group_updates["slot"] = ""
    if enabled is not None:
        group_updates["enabled"] = bool(enabled)
    if include_in_full_dps is not None:
        group_updates["includeInFullDPS"] = bool(include_in_full_dps)
    if len(group_updates) > 1:
        w.call("set_group", group_updates)

    added_gems: list[dict[str, Any]] = []
    if resolved_specs:
        gem_results = _call_batch(w, [("add_gem", _build_add_gem_params(g_idx, sp)) for sp in resolved_specs])
        added_gems = _apply_vaal_defaults_batch(w, g_idx, resolved_specs, gem_results)
        state_params = _build_replaced_group_state_params(g_idx, prior_state, resolved_specs, added_gems)
        if state_params is not None:
            _try_call_ext(w, "apply_group_gem_states", state_params)

    if cache is not None:
        cache.invalidate("skills")

    detail: dict[str, Any] = {
        "groupIndex": g_idx,
        "removedCount": len(old_gems),
        "groupUpdates": group_updates,
        "gemsAdded": added_gems,
    }
    if cautions:
        detail["cautions"] = cautions
    return detail


def replace_skill_group(
    worker: BridgeWorker,
    group: str | int = "main",
    gems: list[str | dict[str, Any]] | None = None,
    *,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool | None = None,
    include_in_full_dps: bool | None = None,
    allow_legacy: bool = False,
    allow_illegal_level: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Atomically clear all existing gems in `group` and replace them with `gems` in one call."""
    gem_list = list(gems or [])
    for g in gem_list:
        _normalize_gem_spec(g, allow_illegal_level=allow_illegal_level)
    return _with_cautions(
        run_mutation(
            worker,
            lambda w: _apply_replace_group_in_place(
                w,
                group,
                gem_list,
                label=label,
                slot=slot,
                enabled=enabled,
                include_in_full_dps=include_in_full_dps,
                allow_legacy=allow_legacy,
                allow_illegal_level=allow_illegal_level,
            ),
            apply=apply,
        )
    )


def _apply_set_gem_in_place(
    w: BridgeWorker,
    *,
    g_idx: int,
    group: dict[str, Any],
    gem_selector: str | int,
    new_gem: str | None = None,
    level: int | None = None,
    quality: int | None = None,
    enabled: bool | None = None,
    count: int | None = None,
    skill_part: int | None = None,
    stage_count: int | None = None,
    allow_legacy: bool = False,
    allow_illegal_level: bool = False,
    pre_cautions: list[str] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    sp_val = _parse_positive_int(skill_part, "skillPart")
    sc_val = _parse_positive_int(stage_count, "stageCount")
    target_gem = _resolve_gem_in_group(group, gem_selector)
    gem_idx = int(target_gem["index"])
    gem_params: dict[str, Any] = {"group": g_idx, "index": gem_idx}
    cautions: list[str] = list(pre_cautions or [])

    if new_gem:
        if pre_cautions is None:
            cautions = _precheck_gem_name(
                new_gem, level, allow_legacy=allow_legacy, allow_illegal_level=allow_illegal_level
            )
        cand = _resolve_gem_candidate(w, g_idx, new_gem)
        resolved_name = (cand.get("name") if cand else None) or new_gem.strip()
        eff_level, res_c = _validate_gem_level(resolved_name, level, allow_illegal_level=allow_illegal_level)
        cautions = _merge_cautions(cautions, res_c)
        if cand and cand.get("gemId"):
            gem_params["gemId"] = cand["gemId"]
            gem_params["nameSpec"] = resolved_name
        else:
            gem_params["nameSpec"] = new_gem.strip()
        if level is not None or (eff_level is not None and eff_level < 20):
            gem_params["level"] = eff_level
    elif level is not None:
        existing_name = str(target_gem.get("name") or target_gem.get("nameSpec") or "").strip()
        eff_level, res_c = _validate_gem_level(existing_name, level, allow_illegal_level=allow_illegal_level)
        cautions = _merge_cautions(cautions, res_c)
        gem_params["level"] = eff_level

    for k, val in (("quality", quality), ("enabled", enabled), ("count", count)):
        if val is not None:
            gem_params[k] = val

    gem_result = w.call("set_gem", gem_params) if (len(gem_params) > 2 or (sp_val is None and sc_val is None)) else None
    state_entry = _build_explicit_gem_state(gem_idx, skill_part=sp_val, stage_count=sc_val)
    if state_entry is not None:
        ext_res = _try_call_ext(w, "apply_group_gem_states", {"group": g_idx, "gemStates": [state_entry]})
        if gem_result is None:
            gem_result = ext_res
    return gem_result, cautions


def modify_skill(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_selector: str | int | None = None,
    new_gem: str | None = None,
    level: int | None = None,
    quality: int | None = None,
    enabled: bool | None = None,
    count: int | None = None,
    skill_part: int | None = None,
    stage_count: int | None = None,
    include_in_full_dps: bool | None = None,
    group_enabled: bool | None = None,
    allow_legacy: bool = False,
    allow_illegal_level: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Modify a skill group or a specific gem within a skill group."""
    sp_val = _parse_positive_int(skill_part, "skillPart")
    sc_val = _parse_positive_int(stage_count, "stageCount")
    pre_cautions = (
        _precheck_gem_name(new_gem, level, allow_legacy=allow_legacy, allow_illegal_level=allow_illegal_level)
        if new_gem
        else []
    )

    def _do_skill_set(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next((g for g in skills_res.get("groups", []) if g["index"] == g_idx), None)
        if group is None:
            raise ValueError(f"Skill group #{g_idx} not found in build.")

        group_updates: dict[str, Any] = {"index": g_idx}
        if include_in_full_dps is not None:
            group_updates["includeInFullDPS"] = include_in_full_dps
        if group_enabled is not None:
            group_updates["enabled"] = group_enabled
        if gem_selector is None and enabled is not None:
            group_updates["enabled"] = enabled
        if len(group_updates) > 1:
            w.call("set_group", group_updates)

        gem_result = None
        cautions: list[str] = list(pre_cautions)
        if gem_selector is not None:
            gem_result, cautions = _apply_set_gem_in_place(
                w,
                g_idx=g_idx,
                group=group,
                gem_selector=gem_selector,
                new_gem=new_gem,
                level=level,
                quality=quality,
                enabled=enabled,
                count=count,
                skill_part=sp_val,
                stage_count=sc_val,
                allow_legacy=allow_legacy,
                allow_illegal_level=allow_illegal_level,
                pre_cautions=pre_cautions,
            )

        detail: dict[str, Any] = {"groupIndex": g_idx, "groupUpdates": group_updates, "gemResult": gem_result}
        if cautions:
            detail["cautions"] = cautions
        return detail

    return _with_cautions(run_mutation(worker, _do_skill_set, apply=apply))


def _apply_add_gem_in_place(
    w: BridgeWorker,
    *,
    group_selector: str | int,
    gem_name: str,
    group_index: int | None = None,
    level: int | None = None,
    quality: int = 20,
    enabled: bool = True,
    count: int | None = None,
    skill_part: int | None = None,
    stage_count: int | None = None,
    allow_legacy: bool = False,
    allow_illegal_level: bool = False,
    pre_cautions: list[str] | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    sp_val = _parse_positive_int(skill_part, "skillPart")
    sc_val = _parse_positive_int(stage_count, "stageCount")
    base_cautions = (
        _precheck_gem_name(gem_name, level, allow_legacy=allow_legacy, allow_illegal_level=allow_illegal_level)
        if pre_cautions is None
        else list(pre_cautions)
    )
    g_idx = (
        int(group_index)
        if group_index is not None
        else resolve_skill_group_index(cache.get_skills() if cache is not None else w.call("list_skills", {}), group_selector)
    )
    chosen = _resolve_gem_candidate(w, g_idx, gem_name)
    resolved_name = (chosen.get("name") if chosen else None) or gem_name.strip()
    eff_level, res_c = _validate_gem_level(resolved_name, level, allow_illegal_level=allow_illegal_level)
    cautions = _merge_cautions(base_cautions, res_c)
    final_level = eff_level if eff_level is not None else 20

    add_params: dict[str, Any] = {"group": g_idx, "level": final_level, "quality": quality, "enabled": enabled}
    if count is not None:
        add_params["count"] = count
    if chosen and chosen.get("gemId"):
        add_params["gemId"] = chosen["gemId"]
        add_params["nameSpec"] = resolved_name
    else:
        add_params["nameSpec"] = gem_name.strip()

    res_add = _add_gem_with_default_skill_flags(w, add_params)
    state_entry = _build_explicit_gem_state(
        int((res_add.get("gem") or {}).get("index") or 1) if isinstance(res_add, dict) else 1,
        skill_part=sp_val,
        stage_count=sc_val,
    )
    if state_entry is not None:
        _try_call_ext(w, "apply_group_gem_states", {"group": g_idx, "gemStates": [state_entry]})

    detail: dict[str, Any] = {
        "groupIndex": g_idx,
        "addedGem": res_add,
        "resolvedCandidate": chosen,
        "effectiveLevel": final_level,
    }
    if cautions:
        detail["cautions"] = cautions
    return detail


def add_skill_gem(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_name: str,
    level: int | None = None,
    quality: int = 20,
    enabled: bool = True,
    skill_part: int | None = None,
    stage_count: int | None = None,
    allow_legacy: bool = False,
    allow_illegal_level: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Add a gem (resolved via `gem_search` if needed) to a skill group."""
    sp_val = _parse_positive_int(skill_part, "skillPart")
    sc_val = _parse_positive_int(stage_count, "stageCount")
    pre_cautions = _precheck_gem_name(
        gem_name, level, allow_legacy=allow_legacy, allow_illegal_level=allow_illegal_level
    )
    return _with_cautions(
        run_mutation(
            worker,
            lambda w: _apply_add_gem_in_place(
                w,
                group_selector=group_selector,
                gem_name=gem_name,
                level=level,
                quality=quality,
                enabled=enabled,
                skill_part=sp_val,
                stage_count=sc_val,
                allow_legacy=allow_legacy,
                allow_illegal_level=allow_illegal_level,
                pre_cautions=pre_cautions,
            ),
            apply=apply,
        )
    )


def remove_skill_gem(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_selector: str | int,
    apply: bool = False,
) -> dict[str, Any]:
    """Remove a gem from a skill group by 1-based index or gem name."""

    def _do_rm(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next((g for g in skills_res.get("groups", []) if g["index"] == g_idx), None)
        if group is None:
            raise ValueError(f"Skill group #{g_idx} not found in build.")
        target_gem = _resolve_gem_in_group(group, gem_selector)
        if group.get("source") and int(target_gem.get("index", 0)) == 1:
            _guard_source_skill_group(group, action="remove gem #1")
        res = w.call("delete_gem", {"group": g_idx, "index": target_gem["index"]})
        return {"groupIndex": g_idx, "removedGem": target_gem, "result": res}

    return run_mutation(worker, _do_rm, apply=apply)
