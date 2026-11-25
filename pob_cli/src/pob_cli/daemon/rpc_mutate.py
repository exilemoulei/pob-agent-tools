"""Mutation RPC handlers (items, skills, tree, config, batch, sweep) for `DaemonManager`."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..bridge_client import BridgeWorker

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
        apply=bool(params.get("apply", True)),
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
        level=int(params["level"]) if params.get("level") is not None else None,
        apply=bool(params.get("apply", True)),
    )


def _h_item_try(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.try_item(
        w,
        raw_text=str(params.get("raw") or ""),
        slot_name=params.get("slot"),
        tooltip_only=bool(params.get("tooltip_only", False)),
        apply=bool(params.get("apply", False)),
    )


def _h_item_edit(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    raw_reps = params.get("replace_mods") or params.get("replaceMods") or []
    rep_tuples = [(str(p[0]), str(p[1])) for p in raw_reps if isinstance(p, (list, tuple)) and len(p) == 2]
    return mutators.edit_item_mods(
        w,
        slot_name=str(params.get("slot") or params.get("slotName") or ""),
        replace_mods=rep_tuples or None,
        add_mods=params.get("add_mods") or params.get("addMods"),
        remove_mods=params.get("remove_mods") or params.get("removeMods"),
        allow_illegal_affixes=bool(params.get("allow_illegal_affixes", params.get("allowIllegalAffixes", False))),
        apply=bool(params.get("apply", False)),
    )


def _h_item_unequip(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    slot_val = str(params.get("slot") or params.get("slotName") or "")
    return mutators.unequip_item(w, slot=slot_val, apply=bool(params.get("apply", False)))


def _h_item_db(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    idx_val = params.get("index") if params.get("index") is not None else params.get("item_index")
    return mutators.search_and_try_item_db(
        w,
        query=str(params.get("query") or ""),
        db_type=str(params.get("db_type") or params.get("dbType") or "UNIQUE"),
        slot_name=params.get("slot") or params.get("slotName"),
        item_index=int(idx_val) if idx_val is not None else None,
        equip=bool(params.get("equip", False)),
        apply=bool(params.get("apply", False)),
        limit=int(params["limit"]) if params.get("limit") is not None else 15,
        with_tooltips=bool(params.get("with_tooltips", params.get("withTooltips", False))),
        with_raw=bool(params.get("with_raw", params.get("withRaw", False))),
    )


def _h_item_flask(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    flask_idx = params.get("flask_index")
    if flask_idx is None:
        raise ValueError("Missing required parameter 'flask_index'.")
    if "active" not in params or params["active"] is None:
        raise ValueError("Missing required parameter 'active'.")
    return mutators.set_flask_active(
        w, flask_index=flask_idx, active=bool(params["active"]), apply=bool(params.get("apply", False))
    )


def _h_item_anoint(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.anoint_item(
        w,
        slot_name=str(params.get("slot") or "Amulet"),
        notable_query=str(params.get("notable") or ""),
        anoint_index=int(params["index"]) if params.get("index") is not None else 1,
        apply=bool(params.get("apply", False)),
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
        allow_legacy=bool(params.get("allow_legacy", params.get("allowLegacy", False))),
        allow_illegal_level=bool(params.get("allow_illegal_level", params.get("allowIllegalLevel", False))),
        apply=bool(params.get("apply", False)),
    )


def _h_skill_add(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.add_skill_gem(
        w,
        group_selector=str(params.get("group") or "main"),
        gem_name=str(params.get("gem") or ""),
        level=int(params["level"]) if params.get("level") is not None else None,
        quality=int(params["quality"]) if params.get("quality") is not None else 20,
        enabled=bool(params.get("enabled", True)),
        allow_legacy=bool(params.get("allow_legacy", params.get("allowLegacy", False))),
        allow_illegal_level=bool(params.get("allow_illegal_level", params.get("allowIllegalLevel", False))),
        apply=bool(params.get("apply", False)),
    )


def _h_skill_rm(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    gem_sel = params.get("gem")
    if gem_sel is None or (isinstance(gem_sel, str) and not gem_sel.strip()):
        raise ValueError("Missing required parameter 'gem'.")
    return mutators.remove_skill_gem(
        w, group_selector=str(params.get("group") or "main"), gem_selector=gem_sel, apply=bool(params.get("apply", False))
    )


def _h_skill_new_group(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.add_skill_group(
        w,
        gems=params.get("gems"),
        label=params.get("label"),
        slot=params.get("slot"),
        enabled=bool(params.get("enabled", True)),
        include_in_full_dps=(
            params.get("include_in_full_dps")
            if params.get("include_in_full_dps") is not None
            else params.get("includeInFullDps")
        ),
        make_main=bool(params.get("make_main", params.get("makeMain", False))),
        allow_legacy=bool(params.get("allow_legacy", params.get("allowLegacy", False))),
        allow_illegal_level=bool(params.get("allow_illegal_level", params.get("allowIllegalLevel", False))),
        apply=bool(params.get("apply", False)),
    )


def _h_skill_replace_group(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    grp_val = params.get("group") if params.get("group") is not None else "main"
    return mutators.replace_skill_group(
        w,
        group=grp_val,
        gems=params.get("gems") or [],
        label=params.get("label"),
        slot=params.get("slot"),
        enabled=params.get("enabled"),
        include_in_full_dps=(
            params.get("include_in_full_dps")
            if params.get("include_in_full_dps") is not None
            else params.get("includeInFullDps")
        ),
        allow_legacy=bool(params.get("allow_legacy", params.get("allowLegacy", False))),
        allow_illegal_level=bool(params.get("allow_illegal_level", params.get("allowIllegalLevel", False))),
        apply=bool(params.get("apply", False)),
    )


def _h_tree_change(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.change_tree(
        w,
        allocate=params.get("allocate"),
        deallocate=params.get("deallocate"),
        apply=bool(params.get("apply", False)),
        allow_cascade=bool(params.get("allow_cascade", params.get("allowCascade", True))),
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
        apply=bool(params.get("apply", False)),
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
        apply=bool(params.get("apply", False)),
    )


def _h_batch(_m: DaemonManager, params: dict[str, Any], w: BridgeWorker) -> dict[str, Any]:
    from .. import mutators

    return mutators.run_batch(
        w,
        ops=params.get("ops") or [],
        apply=bool(params.get("apply", False)),
        no_undo=bool(params.get("no_undo", params.get("noUndo", False))),
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
    "config_set": _h_config_set,
    "batch": _h_batch,
    "sweep": _h_sweep,
}
