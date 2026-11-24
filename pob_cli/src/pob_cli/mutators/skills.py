"""Skill group and gem mutation operations (`add_skill_group`, `replace_skill_group`, `modify_skill`, etc.)."""

from __future__ import annotations

from typing import Any

from ..analyzer import resolve_skill_group_index
from ..bridge_client import BridgeWorker
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
    _extract_explicit_gem_level,
    _normalize_gem_spec,
    _resolve_gem_candidate,
    _resolve_gem_in_group,
    _resolve_gem_specs_batch,
)


def _apply_add_group_in_place(
    w: BridgeWorker,
    *,
    gems: list[str | dict[str, Any]] | None = None,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool = True,
    include_in_full_dps: bool | None = None,
    make_main: bool = False,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    resolved_slot: str | None = None
    if slot is not None and str(slot).strip():
        check_input(str(slot), scope=GuardrailScope.SLOT, expected_game="poe1")
        items_res = cache.get_items() if cache is not None else w.call("list_items", {})
        resolved_slot = _resolve_slot_from_items(items_res, str(slot))
    if gems:
        for g_entry in gems:
            g_name = (
                g_entry
                if isinstance(g_entry, str)
                else str(g_entry.get("name") or g_entry.get("nameSpec") or g_entry.get("gem") or "")
            )
            if g_name:
                check_input(g_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)

    add_params: dict[str, Any] = {}
    if label is not None:
        add_params["label"] = str(label)
    if resolved_slot:
        add_params["slot"] = resolved_slot

    add_res = w.call("add_group", add_params)
    g_idx = int(add_res["index"])

    group_updates: dict[str, Any] = {"index": g_idx}
    if enabled is False:
        group_updates["enabled"] = False
    if include_in_full_dps is not None:
        group_updates["includeInFullDPS"] = bool(include_in_full_dps)
    if len(group_updates) > 1:
        w.call("set_group", group_updates)

    added_gems: list[dict[str, Any]] = []
    cautions: list[str] = []
    if gems:
        explicit_levels = [_extract_explicit_gem_level(g) for g in gems]
        norm_specs = [_normalize_gem_spec(g) for g in gems]
        for sp in norm_specs:
            cautions = _merge_cautions(cautions, sp.get("cautions"))
        resolved_specs = _resolve_gem_specs_batch(w, g_idx, norm_specs)
        for sp, exp_lv in zip(resolved_specs, explicit_levels, strict=False):
            r_name = str(sp.get("name") or "").strip()
            if r_name:
                eff_lv, lv_cautions = _validate_gem_level(r_name, exp_lv)
                if eff_lv is not None:
                    sp["level"] = eff_lv
                cautions = _merge_cautions(cautions, lv_cautions)
        add_calls = [("add_gem", _build_add_gem_params(g_idx, sp)) for sp in resolved_specs]
        gem_results = _call_batch(w, add_calls)
        added_gems = _apply_vaal_defaults_batch(w, g_idx, resolved_specs, gem_results)

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


def add_skill_group(
    worker: BridgeWorker,
    *,
    gems: list[str | dict[str, Any]] | None = None,
    label: str | None = None,
    slot: str | None = None,
    enabled: bool = True,
    include_in_full_dps: bool | None = None,
    make_main: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Create a new skill socket group with optional gems, slot, label, enabled, and FullDPS flag."""
    if gems:
        for g in gems:
            _normalize_gem_spec(g)

    def _do_add_group(w: BridgeWorker) -> dict[str, Any]:
        return _apply_add_group_in_place(
            w,
            gems=gems,
            label=label,
            slot=slot,
            enabled=enabled,
            include_in_full_dps=include_in_full_dps,
            make_main=make_main,
        )

    res = run_mutation(worker, _do_add_group, apply=apply)
    cautions = (res.get("detail") or {}).get("cautions")
    if cautions:
        res["cautions"] = list(cautions)
    return res


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
    norm_specs = [_normalize_gem_spec(g) for g in raw_gems]
    cautions: list[str] = []
    for sp in norm_specs:
        cautions = _merge_cautions(cautions, sp.get("cautions"))
        if sp.get("name"):
            check_input(str(sp["name"]), scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
    resolved_specs = _resolve_gem_specs_batch(w, g_idx, norm_specs)
    for sp, exp_lv in zip(resolved_specs, explicit_levels, strict=False):
        r_name = str(sp.get("name") or "").strip()
        if r_name:
            eff_lv, lv_cautions = _validate_gem_level(r_name, exp_lv)
            if eff_lv is not None:
                sp["level"] = eff_lv
            cautions = _merge_cautions(cautions, lv_cautions)

    old_gems = list(grp.get("gems", []))
    if old_gems:
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
        add_calls = [("add_gem", _build_add_gem_params(g_idx, sp)) for sp in resolved_specs]
        gem_results = _call_batch(w, add_calls)
        added_gems = _apply_vaal_defaults_batch(w, g_idx, resolved_specs, gem_results)

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
    apply: bool = False,
) -> dict[str, Any]:
    """Atomically clear all existing gems in `group` and replace them with `gems` in one call."""
    gem_list = list(gems or [])
    for g in gem_list:
        _normalize_gem_spec(g)

    def _do_replace(w: BridgeWorker) -> dict[str, Any]:
        return _apply_replace_group_in_place(
            w,
            group,
            gem_list,
            label=label,
            slot=slot,
            enabled=enabled,
            include_in_full_dps=include_in_full_dps,
        )

    res = run_mutation(worker, _do_replace, apply=apply)
    cautions = (res.get("detail") or {}).get("cautions")
    if cautions:
        res["cautions"] = list(cautions)
    return res


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
    include_in_full_dps: bool | None = None,
    group_enabled: bool | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Modify a skill group or a specific gem within a skill group."""
    pre_cautions: list[str] = []
    if new_gem:
        check_input(new_gem, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
        _, pre_cautions = _validate_gem_level(new_gem.strip(), level)

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
            target_gem = _resolve_gem_in_group(group, gem_selector)
            gem_params: dict[str, Any] = {"group": g_idx, "index": target_gem["index"]}
            if new_gem:
                cand = _resolve_gem_candidate(w, g_idx, new_gem)
                resolved_name = (cand.get("name") if cand else None) or new_gem.strip()
                eff_level, res_cautions = _validate_gem_level(resolved_name, level)
                cautions = _merge_cautions(cautions, res_cautions)
                if cand and cand.get("gemId"):
                    gem_params["gemId"] = cand["gemId"]
                    gem_params["nameSpec"] = resolved_name
                else:
                    gem_params["nameSpec"] = new_gem.strip()
                if level is not None:
                    gem_params["level"] = eff_level
                elif eff_level is not None and eff_level < 20:
                    gem_params["level"] = eff_level
            else:
                existing_name = str(target_gem.get("name") or target_gem.get("nameSpec") or "").strip()
                if level is not None:
                    eff_level, res_cautions = _validate_gem_level(existing_name, level)
                    cautions = _merge_cautions(cautions, res_cautions)
                    gem_params["level"] = eff_level
            if quality is not None:
                gem_params["quality"] = quality
            if enabled is not None:
                gem_params["enabled"] = enabled
            if count is not None:
                gem_params["count"] = count
            gem_result = w.call("set_gem", gem_params)

        detail: dict[str, Any] = {"groupIndex": g_idx, "groupUpdates": group_updates, "gemResult": gem_result}
        if cautions:
            detail["cautions"] = cautions
        return detail

    res = run_mutation(worker, _do_skill_set, apply=apply)
    cautions = (res.get("detail") or {}).get("cautions")
    if cautions:
        res["cautions"] = list(cautions)
    return res


def add_skill_gem(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_name: str,
    level: int | None = None,
    quality: int = 20,
    enabled: bool = True,
    apply: bool = False,
) -> dict[str, Any]:
    """Add a gem (resolved via `gem_search` if needed) to a skill group."""
    check_input(gem_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
    _, pre_cautions = _validate_gem_level(gem_name.strip(), level)

    def _do_add(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        chosen = _resolve_gem_candidate(w, g_idx, gem_name)
        resolved_name = (chosen.get("name") if chosen else None) or gem_name.strip()
        eff_level, res_cautions = _validate_gem_level(resolved_name, level)
        cautions = _merge_cautions(pre_cautions, res_cautions)
        final_level = eff_level if eff_level is not None else 20

        add_params: dict[str, Any] = {
            "group": g_idx,
            "level": final_level,
            "quality": quality,
            "enabled": enabled,
        }
        if chosen and chosen.get("gemId"):
            add_params["gemId"] = chosen["gemId"]
            add_params["nameSpec"] = resolved_name
        else:
            add_params["nameSpec"] = gem_name.strip()

        res_add = _add_gem_with_default_skill_flags(w, add_params)
        detail: dict[str, Any] = {
            "groupIndex": g_idx,
            "addedGem": res_add,
            "resolvedCandidate": chosen,
            "effectiveLevel": final_level,
        }
        if cautions:
            detail["cautions"] = cautions
        return detail

    res = run_mutation(worker, _do_add, apply=apply)
    cautions = (res.get("detail") or {}).get("cautions")
    if cautions:
        res["cautions"] = list(cautions)
    return res


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
