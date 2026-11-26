"""Mutation RPC handlers (items, skills, tree, config, batch, sweep) for `DaemonManager`."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeWorker
from ..mutators.strict_args import (
    opt_bool,
    parse_bool,
    parse_gem_list,
    parse_int,
    parse_replace_mods,
    parse_str_list,
    parse_token_list,
)

if TYPE_CHECKING:
    from .slots import DaemonManager

_SlotHandler = Callable[["DaemonManager", dict[str, Any], BridgeWorker], dict[str, Any]]


def norm_keep(params: dict[str, Any]) -> list[str] | None:
    raw_keep = params.get("keep")
    if isinstance(raw_keep, str):
        return [raw_keep]
    if raw_keep is not None:
        return list(raw_keep)
    return None


def _h_switch_main(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.switch_main(
        w,
        spec_index=params.get("spec_index"),
        skill_group=params.get("skill_group"),
        main_skill_index=params.get("main_skill_index"),
        skill_part_index=params.get("skill_part_index"),
        item_set_id=params.get("item_set_id"),
        skill_set_id=params.get("skill_set_id"),
        config_set_id=params.get("config_set_id"),
        loadout_index=params.get("loadout_index"),
        apply=opt_bool(params, "apply", default=True),
    )


def _h_new_build(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.create_new_build(
        w,
        class_name=params.get("class_name") or params.get("className"),
        ascendancy=params.get("ascendancy") or params.get("ascendancy_name") or params.get("ascendancyName"),
        secondary_ascendancy=(
            params.get("secondary_ascendancy")
            or params.get("secondary_ascendancy_name")
            or params.get("secondaryAscendancy")
        ),
        level=parse_int(params["level"], "level") if params.get("level") is not None else None,
        apply=opt_bool(params, "apply", default=True),
    )


def _h_item_try(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.try_item(
        w,
        raw_text=str(params.get("raw") or ""),
        slot_name=params.get("slot"),
        tooltip_only=opt_bool(params, "tooltip_only", default=False),
        apply=opt_bool(params, "apply", default=False),
    )


def _h_item_edit(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    rep_key = "replace_mods" if params.get("replace_mods") is not None else "replaceMods"
    rep_tuples = parse_replace_mods(params.get(rep_key), rep_key)
    return mutators.edit_item_mods(
        w,
        slot_name=str(params.get("slot") or params.get("slotName") or ""),
        replace_mods=rep_tuples or None,
        add_mods=parse_str_list(params.get("add_mods") or params.get("addMods"), "add_mods"),
        remove_mods=parse_str_list(params.get("remove_mods") or params.get("removeMods"), "remove_mods"),
        allow_illegal_affixes=opt_bool(params, "allow_illegal_affixes", "allowIllegalAffixes", default=False),
        apply=opt_bool(params, "apply", default=False),
    )


def _h_item_unequip(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    slot_val = str(params.get("slot") or params.get("slotName") or "")
    return mutators.unequip_item(w, slot=slot_val, apply=opt_bool(params, "apply", default=False))


def _h_item_db(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    db_type = str(params.get("db_type") or params.get("dbType") or "UNIQUE")
    if opt_bool(params, "list_sorts", "listSorts", default=False):
        return mutators.list_item_db_sorts(w, db_type=db_type)
    idx_val = params.get("index") if params.get("index") is not None else params.get("item_index")
    return mutators.search_and_try_item_db(
        w,
        query=str(params.get("query") or ""),
        db_type=db_type,
        slot_name=params.get("slot") or params.get("slotName"),
        item_type=params.get("item_type") or params.get("itemType"),
        league=params.get("league"),
        requirement=params.get("requirement"),
        obtainable=params.get("obtainable"),
        search_mode=params.get("search_mode") or params.get("searchMode"),
        sort_mode=params.get("sort") or params.get("sort_mode") or params.get("sortMode"),
        item_index=parse_int(idx_val, "index") if idx_val is not None else None,
        equip=opt_bool(params, "equip", default=False),
        apply=opt_bool(params, "apply", default=False),
        limit=parse_int(params["limit"], "limit") if params.get("limit") is not None else 15,
        page=parse_int(params["page"], "page") if params.get("page") is not None else 1,
        with_tooltips=opt_bool(params, "with_tooltips", "withTooltips", default=False),
        with_raw=opt_bool(params, "with_raw", "withRaw", default=False),
    )


def _h_item_flask(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    flask_idx = params.get("flask_index")
    if flask_idx is None:
        raise ValueError("Missing required parameter 'flask_index'.")
    if "active" not in params or params["active"] is None:
        raise ValueError("Missing required parameter 'active'.")
    return mutators.set_flask_active(
        w,
        flask_index=flask_idx,
        active=parse_bool(params["active"], "active"),
        apply=opt_bool(params, "apply", default=False),
    )


def _h_item_anoint(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.anoint_item(
        w,
        slot_name=str(params.get("slot") or "Amulet"),
        notable_query=str(params.get("notable") or ""),
        anoint_index=parse_int(params["index"], "index") if params.get("index") is not None else 1,
        apply=opt_bool(params, "apply", default=False),
    )


def _h_skill_set(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.modify_skill(
        w,
        group_selector=str(params.get("group") or "main"),
        gem_selector=params.get("gem"),
        new_gem=params.get("new_gem"),
        level=params.get("level"),
        quality=params.get("quality"),
        enabled=params.get("enabled"),
        count=params.get("count"),
        include_in_full_dps=params.get("include_in_full_dps"),
        group_enabled=params.get("group_enabled"),
        allow_legacy=opt_bool(params, "allow_legacy", "allowLegacy", default=False),
        allow_illegal_level=opt_bool(params, "allow_illegal_level", "allowIllegalLevel", default=False),
        apply=opt_bool(params, "apply", default=False),
    )


def _h_skill_add(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.add_skill_gem(
        w,
        group_selector=str(params.get("group") or "main"),
        gem_name=str(params.get("gem") or ""),
        level=parse_int(params["level"], "level") if params.get("level") is not None else None,
        quality=parse_int(params["quality"], "quality") if params.get("quality") is not None else 20,
        enabled=opt_bool(params, "enabled", default=True),
        allow_legacy=opt_bool(params, "allow_legacy", "allowLegacy", default=False),
        allow_illegal_level=opt_bool(params, "allow_illegal_level", "allowIllegalLevel", default=False),
        apply=opt_bool(params, "apply", default=False),
    )


def _h_skill_rm(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    gem_sel = params.get("gem")
    if gem_sel is None or (isinstance(gem_sel, str) and not gem_sel.strip()):
        raise ValueError("Missing required parameter 'gem'.")
    return mutators.remove_skill_gem(
        w,
        group_selector=str(params.get("group") or "main"),
        gem_selector=gem_sel,
        apply=opt_bool(params, "apply", default=False),
    )


def _h_skill_new_group(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.add_skill_group(
        w,
        gems=parse_gem_list(params.get("gems")),
        label=params.get("label"),
        slot=params.get("slot"),
        enabled=opt_bool(params, "enabled", default=True),
        include_in_full_dps=(
            params.get("include_in_full_dps")
            if params.get("include_in_full_dps") is not None
            else params.get("includeInFullDps")
        ),
        make_main=opt_bool(params, "make_main", "makeMain", default=False),
        allow_legacy=opt_bool(params, "allow_legacy", "allowLegacy", default=False),
        allow_illegal_level=opt_bool(params, "allow_illegal_level", "allowIllegalLevel", default=False),
        apply=opt_bool(params, "apply", default=False),
    )


def _h_skill_replace_group(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    grp_val = params.get("group") if params.get("group") is not None else "main"
    return mutators.replace_skill_group(
        w,
        group=grp_val,
        gems=parse_gem_list(params.get("gems")) or [],
        label=params.get("label"),
        slot=params.get("slot"),
        enabled=params.get("enabled"),
        include_in_full_dps=(
            params.get("include_in_full_dps")
            if params.get("include_in_full_dps") is not None
            else params.get("includeInFullDps")
        ),
        allow_legacy=opt_bool(params, "allow_legacy", "allowLegacy", default=False),
        allow_illegal_level=opt_bool(params, "allow_illegal_level", "allowIllegalLevel", default=False),
        apply=opt_bool(params, "apply", default=False),
    )


def _h_tree_change(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.change_tree(
        w,
        allocate=parse_token_list(params.get("allocate"), "allocate"),
        deallocate=parse_token_list(params.get("deallocate"), "deallocate"),
        apply=opt_bool(params, "apply", default=False),
        allow_cascade=opt_bool(params, "allow_cascade", "allowCascade", default=True),
    )


def _h_tree_set_class(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.set_class_ascendancy(
        w,
        class_name=params.get("class_name") or params.get("className"),
        ascendancy_name=params.get("ascendancy_name") or params.get("ascendancyName"),
        secondary_ascendancy_name=(
            params.get("secondary_ascendancy_name") or params.get("secondaryAscendancyName")
        ),
        apply=opt_bool(params, "apply", default=False),
    )


def _h_tree_tattoo(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    node_val = params.get("node") if params.get("node") is not None else params.get("nodeId", params.get("id"))
    if node_val is None or not str(node_val).strip():
        raise ValueError("Missing required parameter 'node'.")
    return mutators.apply_tree_tattoo(
        w,
        node=node_val,
        tattoo=params.get("tattoo"),
        reset=opt_bool(params, "reset", "clear", default=False),
        show_legacy=opt_bool(params, "show_legacy", "showLegacy", default=False),
        strict_req=opt_bool(params, "strict_req", "strictReq", default=False),
        apply=opt_bool(params, "apply", default=False),
    )


def _h_config_set(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    append_val = (
        params.get("appendCustomMods")
        if params.get("appendCustomMods") is not None
        else params.get("append_custom_mods")
    )
    return mutators.modify_config(
        w,
        assignments=params.get("assignments") or [],
        custom_mods=params.get("custom_mods"),
        append_custom_mods=append_val,
        apply=opt_bool(params, "apply", default=False),
    )


def _h_batch(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.run_batch(
        w,
        ops=params.get("ops") or [],
        apply=opt_bool(params, "apply", default=False),
        no_undo=opt_bool(params, "no_undo", "noUndo", default=False),
        return_sections=params.get("return_sections") or params.get("returnSections"),
        keep=norm_keep(params),
    )


def _h_sweep(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.run_sweep(
        w,
        variants=params.get("variants") or [],
        base_ops=params.get("base_ops") or params.get("baseOps"),
        calc_keywords=params.get("calc_keywords") or params.get("calcKeywords"),
        scenario=params.get("scenario"),
        keep=norm_keep(params),
        auto_keep=not opt_bool(params, "no_auto_keep", "noAutoKeep", default=False),
    )


def _h_rebalance(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from ..rebalance import solve_rebalance

    ap_raw = params.get("apply_plan") if params.get("apply_plan") is not None else params.get("applyPlan")
    ms_raw = params.get("max_steps") if params.get("max_steps") is not None else params.get("maxSteps", 3)
    me_raw = params.get("max_evals") if params.get("max_evals") is not None else params.get("maxEvals", 24)
    return solve_rebalance(
        w,
        try_item_raw=params.get("try_item_raw") or params.get("tryItemRaw") or params.get("raw"),
        slot_name=params.get("slot") or params.get("slotName") or params.get("slot_name"),
        max_steps=parse_int(ms_raw, "max_steps"),
        max_evals=parse_int(me_raw, "max_evals"),
        include_chaos=opt_bool(params, "include_chaos", "includeChaos", default=False),
        include_suppression=opt_bool(params, "include_suppression", "includeSuppression", default=False),
        apply_plan=parse_int(ap_raw, "apply_plan") if ap_raw is not None else None,
    )


MUTATE_ACTION_HANDLERS: dict[str, _SlotHandler] = {
    "switch_main": _h_switch_main,
    "new_build": _h_new_build,
    "item_try": _h_item_try,
    "item_edit": _h_item_edit,
    "item_unequip": _h_item_unequip,
    "item_db": _h_item_db,
    "item_flask": _h_item_flask,
    "item_anoint": _h_item_anoint,
    "skill_set": _h_skill_set,
    "skill_add": _h_skill_add,
    "skill_rm": _h_skill_rm,
    "skill_new_group": _h_skill_new_group,
    "skill_replace_group": _h_skill_replace_group,
    "tree_change": _h_tree_change,
    "tree_set_class": _h_tree_set_class,
    "tree_tattoo": _h_tree_tattoo,
    "config_set": _h_config_set,
    "batch": _h_batch,
    "sweep": _h_sweep,
    "rebalance": _h_rebalance,
}
