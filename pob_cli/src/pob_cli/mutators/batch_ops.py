"""Composite batch op handlers and dispatcher (`_execute_batch_ops`)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..bridge_client import BridgeWorker
from ..guardrails import GuardrailScope, check_input, check_input_item_raw, validate_url
from ..snapshot import BuildScope
from .batch_ops_skills import (
    _op_add_group,
    _op_delete_group,
    _op_replace_group,
    _op_set_group,
    _op_skill,
)
from .batch_types import resolve_flask_active_state
from .batch_validate import validate_batch_op
from .build_state import (
    _apply_class_ascendancy_in_place,
    _apply_new_build_in_place,
    _apply_switch_main_in_place,
)
from .common import (
    _BatchCache,
    _call_batch,
    _check_agnostic_dealloc_caution,
    _is_jewel_slot_name,
    _resolve_slot_from_items,
)
from .config import _apply_config_in_place
from .item_db import _search_item_db_items
from .item_edit import _apply_edit_item_mods_in_place
from .items import _apply_anoint_in_place, _parse_flask_slots
from .strict_args import opt_bool, parse_replace_mods, parse_token_list
from .tree import _apply_tree_changes_in_place, _resolve_node_token

_BatchOpHandler = Callable[[BridgeWorker, dict[str, Any], _BatchCache, int], dict[str, Any]]


def _op_config(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    set_map = op.get("set")
    if set_map is not None and not isinstance(set_map, dict):
        raise ValueError(f"Batch op 'config' at step {idx} expects 'set' to be a JSON object.")
    reset_list = op.get("reset")
    if reset_list is not None and not isinstance(reset_list, list):
        raise ValueError(f"Batch op 'config' at step {idx} expects 'reset' to be a JSON list.")
    pairs = list((set_map or {}).items())
    raw_assign = op.get("assignments")
    if isinstance(raw_assign, dict):
        pairs.extend(raw_assign.items())
    elif isinstance(raw_assign, list):
        for item in raw_assign:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                raise ValueError(
                    f"Batch op 'config' at step {idx} expects each 'assignments' item to be a [key, value] pair."
                )
            pairs.append((item[0], item[1]))
    elif raw_assign is not None:
        raise ValueError(f"Batch op 'config' at step {idx} expects 'assignments' to be a dict or list of [key, value] pairs.")
    for var in reset_list or []:
        pairs.append((var, "reset"))
    cm = op.get("customMods") if "customMods" in op else op.get("custom_mods")
    acm = op.get("appendCustomMods") if "appendCustomMods" in op else op.get("append_custom_mods")
    cfg_res = _apply_config_in_place(
        w,
        pairs,
        custom_mods=str(cm) if cm is not None else None,
        append_custom_mods=str(acm) if acm is not None else None,
        cache=cache,
    )
    return {"step": idx, "op": "config", "count": len(cfg_res["assignments"])}


def _op_flask(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    f_sel: Any = None
    for k in ("index", "flask_index", "flaskIndex", "slot", "flask"):
        if op.get(k) is not None:
            f_sel = op[k]
            break
    if f_sel is None:
        f_sel = "all"
    act = resolve_flask_active_state(op, default=True)
    slots = _parse_flask_slots(f_sel)
    _call_batch(w, [("set_slot_active", {"slotName": s_name, "active": act}) for s_name in slots])
    cache.invalidate("items")
    return {"step": idx, "op": "flask", "slotName": ",".join(slots), "active": act}


def _op_item(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    raw_val = op.get("raw")
    raw_text = str(raw_val).strip() if raw_val is not None else ""
    if not raw_text:
        raise ValueError(f"Batch op 'item' at step {idx} requires non-empty 'raw'.")
    slot_query = op.get("slot") or op.get("slotName") or op.get("slot_name")
    if slot_query:
        check_input(str(slot_query), scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input_item_raw(raw_text, expected_game="poe1")
    slot_name = _resolve_slot_from_items(cache.get_items(), str(slot_query)) if slot_query else None
    params: dict[str, Any] = {"raw": raw_text, "equip": True}
    if slot_name:
        params["slotName"] = slot_name
    res = w.call("add_item", params)
    cache.invalidate("items", "skills")
    if _is_jewel_slot_name(slot_name) or "jewel" in str((res.get("item") or {}).get("type") or "").lower():
        cache.invalidate("tree")
    return {"step": idx, "op": "item", "slotName": slot_name, "item": res.get("item")}


def _op_item_db(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    raw_query = op.get("query")
    if raw_query is None or not str(raw_query).strip():
        raw_query = op.get("name")
    query = str(raw_query or "").strip()
    db_type = str(op.get("dbType") or op.get("db_type") or op.get("kind") or "UNIQUE")
    item_index = op.get("index") if op.get("index") is not None else op.get("itemIndex", op.get("item_index"))
    slot_query = op.get("slot") or op.get("slotName") or op.get("slot_name")
    if slot_query:
        check_input(str(slot_query), scope=GuardrailScope.SLOT, expected_game="poe1")
    if query:
        check_input(query, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    resolved_slot = _resolve_slot_from_items(cache.get_items(), str(slot_query)) if slot_query else None
    db_kind, items, _ = _search_item_db_items(w, query=query, db_type=db_type, limit=max(15, int(item_index or 1)))
    if not items:
        raise ValueError(f"No {db_kind} database items matched query '{query}' at step {idx}.")
    pick_idx = int(item_index) if item_index is not None else 1
    if pick_idx < 1 or pick_idx > len(items):
        raise ValueError(f"item_db index {pick_idx} out of range (1..{len(items)}) at step {idx}.")
    chosen = items[pick_idx - 1]
    chosen_raw = str(chosen.get("raw") or "").strip()
    if not chosen_raw:
        raise RuntimeError(f"Selected DB item '{chosen.get('title') or chosen.get('name')}' has no raw text.")
    add_params: dict[str, Any] = {"raw": chosen_raw, "equip": True}
    if resolved_slot:
        add_params["slotName"] = resolved_slot
    res = w.call("add_item", add_params)
    cache.invalidate("items", "skills")
    if _is_jewel_slot_name(resolved_slot) or "jewel" in str(chosen.get("type") or "").lower():
        cache.invalidate("tree")
    return {
        "step": idx,
        "op": "item_db",
        "query": query,
        "slotName": resolved_slot or (res.get("item") or {}).get("primarySlot"),
        "item": res.get("item"),
    }


def _op_item_edit(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    slot_query = str(op.get("slot") or op.get("slotName") or op.get("slot_name") or "").strip()
    rep_key = "replaceMods" if op.get("replaceMods") is not None else "replace_mods"
    rep_tuples = parse_replace_mods(op.get(rep_key), rep_key)
    edit_res = _apply_edit_item_mods_in_place(
        w,
        slot_name=slot_query,
        replace_mods=rep_tuples or None,
        add_mods=op.get("addMods") or op.get("add_mods"),
        remove_mods=op.get("removeMods") or op.get("remove_mods"),
        allow_illegal_affixes=bool(op.get("allowIllegalAffixes", op.get("allow_illegal_affixes", False))),
        cache=cache,
    )
    return {"step": idx, "op": "item_edit", "slotName": edit_res["slotName"], "changes": edit_res["changes"]}


def _op_unequip(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    slot_query = str(op.get("slot") or op.get("slotName") or op.get("slot_name") or "").strip()
    if not slot_query:
        raw_kind = str(op.get("op") or op.get("type") or op.get("action") or "unequip")
        raise ValueError(f"Batch op '{raw_kind}' at step {idx} requires 'slot'.")
    check_input(slot_query, scope=GuardrailScope.SLOT, expected_game="poe1")
    resolved_slot = _resolve_slot_from_items(cache.get_items(), slot_query)
    w.call("unequip_slot", {"slotName": resolved_slot})
    cache.invalidate("items", "skills")
    if _is_jewel_slot_name(resolved_slot):
        cache.invalidate("tree")
    return {"step": idx, "op": "unequip", "slotName": resolved_slot}


def _op_anoint(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    notable_val = op.get("notable")
    notable_text = str(notable_val).strip() if notable_val is not None else ""
    if not notable_text:
        raise ValueError(f"Batch op 'anoint' at step {idx} requires non-empty 'notable'.")
    an_res = _apply_anoint_in_place(
        w,
        slot_name=str(op.get("slot") or op.get("slotName") or op.get("slot_name") or "Amulet"),
        notable_query=notable_text,
        anoint_index=int(op["index"]) if op.get("index") is not None else 1,
        cache=cache,
    )
    return {"step": idx, "op": "anoint", "notable": an_res.get("notable")}


def _op_tree(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    tree_state = cache.get_tree()
    nodes_db = w.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
    dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}
    alloc_tokens = parse_token_list(op.get("allocate"), "allocate")
    dealloc_tokens = parse_token_list(op.get("deallocate"), "deallocate")
    if not alloc_tokens and not dealloc_tokens:
        raise ValueError(f"Batch op 'tree' at step {idx} requires at least one node in 'allocate' or 'deallocate'.")
    resolved_alloc = [_resolve_node_token(w, tok, nodes_db, dynamic_map) for tok in alloc_tokens]
    resolved_dealloc = [_resolve_node_token(w, tok, nodes_db, dynamic_map) for tok in dealloc_tokens]
    allow_casc = opt_bool(op, "allowCascade", "allow_cascade", default=True)
    tree_cautions = _check_agnostic_dealloc_caution(w, {nid for nid, _ in resolved_dealloc}, cache=cache)
    noop_warnings: list[str] = []
    actions_taken, final_state, orphaned_nodes, points_delta, cascade_warning = _apply_tree_changes_in_place(
        w,
        resolved_alloc,
        resolved_dealloc,
        initial_tree_state=tree_state,
        allow_cascade=allow_casc,
        noop_warnings_out=noop_warnings,
    )
    cache.tree = final_state
    cache.invalidate("items", "skills")
    step_entry: dict[str, Any] = {
        "step": idx,
        "op": "tree",
        "actions": actions_taken,
        "pointsDelta": points_delta,
        "orphanedNodes": orphaned_nodes,
    }
    if noop_warnings:
        step_entry["noopWarnings"] = noop_warnings
    if cascade_warning:
        step_entry["cascadeWarning"] = cascade_warning
    if tree_cautions:
        step_entry["cautions"] = tree_cautions
    return step_entry


def _op_tree_reset(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    tattoos = bool(op.get("tattoos", False))
    res = w.call("reset_tree", {"tattoos": True} if tattoos else {})
    if isinstance(res, dict) and "allocatedNodes" in res:
        cache.tree = res
    else:
        cache.invalidate("tree")
    cache.invalidate("items", "skills")
    return {"step": idx, "op": "tree_reset", "tattoos": tattoos, "points": res.get("points")}


def _op_import_tree_url(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    url = str(op.get("url") or "").strip()
    title = str(op.get("title") or op.get("name") or "Imported Tree").strip()
    if not url:
        raise ValueError(f"Batch op 'import_tree_url' at step {idx} requires 'url'.")
    validate_url(url, expected_game="poe1", cli_subcommand="batch")
    specs_res = w.call("import_tree_url", {"url": url, "title": title})
    cache.invalidate("tree", "classes", "items", "skills")
    cache.specs = specs_res
    return {"step": idx, "op": "import_tree_url", "title": title, "activeSpec": specs_res.get("activeSpec")}


def _op_new_build(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    nb_res = _apply_new_build_in_place(
        w,
        class_name=op.get("class") or op.get("className") or op.get("class_name"),
        ascendancy=op.get("ascendancy") or op.get("asc") or op.get("ascendancyName") or op.get("ascendancy_name"),
        secondary_ascendancy=(
            op.get("secondaryAscendancy") or op.get("secondary_ascendancy") or op.get("secondaryAsc")
        ),
        level=int(op["level"]) if op.get("level") is not None else None,
        name=op.get("name"),
        cache=cache,
    )
    return {"step": idx, "op": "new_build", "changes": nb_res["changes"], "level": nb_res["level"]}


def _op_set_level(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    raw_lv = op.get("level") if op.get("level") is not None else op.get("value")
    if raw_lv is None and "auto" not in op:
        raise ValueError(f"Batch op 'set_level' at step {idx} requires 'level' or 'auto'.")
    lv_val: int | None = None
    if raw_lv is not None:
        lv_val = int(raw_lv)
        if lv_val < 1 or lv_val > 100:
            raise ValueError(f"Character level must be between 1 and 100 (got {lv_val}).")
        w.call("set_build_field", {"field": "level", "value": lv_val})
    if "auto" in op:
        w.call("set_build_field", {"field": "levelAuto", "value": bool(op["auto"])})
    cache.invalidate("tree", "config", "items", "skills")
    return {"step": idx, "op": "set_level", "level": lv_val, "auto": op.get("auto")}


def _op_set_class(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    cls_res = _apply_class_ascendancy_in_place(
        w,
        class_name=op.get("class") or op.get("className") or op.get("class_name"),
        ascendancy_name=(
            op.get("ascendancy") or op.get("asc") or op.get("ascendancyName") or op.get("ascendancy_name")
        ),
        secondary_ascendancy_name=(
            op.get("secondaryAscendancy")
            or op.get("secondary_ascendancy")
            or op.get("secondaryAsc")
            or op.get("secondaryAscendancyName")
        ),
        cache=cache,
    )
    return {"step": idx, "op": "set_class", "changes": cls_res["changes"], "after": cls_res["after"]}


def _op_switch_main(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    sw_res = _apply_switch_main_in_place(
        w,
        spec_index=op.get("spec") if "spec" in op else op.get("specIndex", op.get("spec_index")),
        skill_group=(
            op.get("skillGroup") if "skillGroup" in op else op.get("skill_group", op.get("group"))
        ),
        main_skill_index=(
            op.get("mainSkill") if "mainSkill" in op else op.get("mainSkillIndex", op.get("main_skill_index"))
        ),
        skill_part_index=(
            op.get("skillPart")
            if "skillPart" in op
            else op.get("skill_part", op.get("skillPartIndex", op.get("skill_part_index")))
        ),
        item_set_id=op.get("itemSet") if "itemSet" in op else op.get("itemSetId", op.get("item_set_id")),
        skill_set_id=op.get("skillSet") if "skillSet" in op else op.get("skillSetId", op.get("skill_set_id")),
        config_set_id=op.get("configSet") if "configSet" in op else op.get("configSetId", op.get("config_set_id")),
        loadout_index=op.get("loadout") if "loadout" in op else op.get("loadoutIndex", op.get("loadout_index")),
        cache=cache,
    )
    return {"step": idx, "op": "switch_main", "changes": sw_res["changes"]}


_SKILL_SUB_ACTIONS = frozenset({"set", "add", "remove", "rm"})
_BATCH_OP_EXAMPLE = (
    '[{"op":"config","set":{"enemyIsBoss":"Uber"}},'
    '{"op":"skill_add","group":"main","gem":"Empower","level":4},'
    '{"op":"tree","allocate":["12345"]}]'
)


def _dispatch_skill_op(
    w: BridgeWorker,
    op: dict[str, Any],
    cache: _BatchCache,
    idx: int,
    *,
    default_action: str | None = None,
) -> dict[str, Any]:
    norm = dict(op)
    raw_act = str(norm.get("action") or "").strip().lower()
    sub_action = raw_act if raw_act in _SKILL_SUB_ACTIONS else default_action
    if sub_action is not None:
        norm["action"] = "remove" if sub_action == "rm" else sub_action
    elif raw_act:
        norm.pop("action", None)

    if sub_action == "add":
        if not (norm.get("addGem") or norm.get("add_gem")):
            gem_name = norm.pop("gem", None) or norm.get("name")
            if gem_name is None or not str(gem_name).strip():
                raise ValueError(f"Batch op 'skill_add' at step {idx} requires 'gem' or 'addGem'.")
            norm["addGem"] = str(gem_name).strip()
    elif sub_action in ("remove", "rm"):
        if norm.get("removeGem") is None and norm.get("remove_gem") is None:
            gem_sel = norm.pop("gem", None)
            if gem_sel is None:
                gem_sel = norm.get("index")
            if gem_sel is None:
                raise ValueError(f"Batch op 'skill_rm' at step {idx} requires 'gem' or 'removeGem'.")
            norm["removeGem"] = gem_sel
    elif sub_action == "set":
        if norm.get("gem") is None and norm.get("index") is not None and not (
            norm.get("addGem") or norm.get("add_gem") or "removeGem" in norm or "remove_gem" in norm
        ):
            norm["gem"] = norm["index"]

    return _op_skill(w, norm, cache, idx)


def _op_skill_dispatch(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    return _dispatch_skill_op(w, op, cache, idx, default_action=None)


def _op_skill_set(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    return _dispatch_skill_op(w, op, cache, idx, default_action="set")


def _op_skill_add(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    return _dispatch_skill_op(w, op, cache, idx, default_action="add")


def _op_skill_rm(w: BridgeWorker, op: dict[str, Any], cache: _BatchCache, idx: int) -> dict[str, Any]:
    return _dispatch_skill_op(w, op, cache, idx, default_action="remove")


_BATCH_OP_HANDLERS: dict[str, _BatchOpHandler] = {
    "config": _op_config,
    "config_set": _op_config,
    "flask": _op_flask,
    "item_flask": _op_flask,
    "item": _op_item,
    "item_try": _op_item,
    "item_db": _op_item_db,
    "item_edit": _op_item_edit,
    "unequip": _op_unequip,
    "item_unequip": _op_unequip,
    "unequip_slot": _op_unequip,
    "anoint": _op_anoint,
    "item_anoint": _op_anoint,
    "tree": _op_tree,
    "tree_change": _op_tree,
    "tree_reset": _op_tree_reset,
    "reset_tree": _op_tree_reset,
    "import_tree_url": _op_import_tree_url,
    "new_build": _op_new_build,
    "set_level": _op_set_level,
    "set_class": _op_set_class,
    "tree_set_class": _op_set_class,
    "switch_main": _op_switch_main,
    "add_group": _op_add_group,
    "new_group": _op_add_group,
    "skill_new_group": _op_add_group,
    "delete_group": _op_delete_group,
    "rm_group": _op_delete_group,
    "remove_group": _op_delete_group,
    "set_group": _op_set_group,
    "replace_group": _op_replace_group,
    "skill_replace_group": _op_replace_group,
    "skill": _op_skill_dispatch,
    "skill_set": _op_skill_set,
    "skill_add": _op_skill_add,
    "skill_rm": _op_skill_rm,
}


# How far each op reaches beyond the active spec and sets of the loaded build (`BuildScope`); the
# transaction a batch or sweep runs in picks its checkpoint from the widest. Ops not listed stay
# inside the active sets.
_OP_SCOPES = {
    "new_build": BuildScope.REPLACES_BUILD,
    "import_tree_url": BuildScope.SETS,
}
_SET_SWITCH_KEYS = (
    "spec", "specIndex", "spec_index", "itemSet", "itemSetId", "item_set_id", "skillSet", "skillSetId",
    "skill_set_id", "configSet", "configSetId", "config_set_id", "loadout", "loadoutIndex", "loadout_index",
)


def op_scope(op: Any) -> BuildScope:
    """The `BuildScope` of one batch op (switch_main reaches other sets only when it names one)."""
    if not isinstance(op, dict):
        return BuildScope.ACTIVE_SETS
    kind = str(op.get("op") or op.get("type") or op.get("action") or "").strip().lower().replace("-", "_")
    if kind == "switch_main" and any(op.get(k) is not None for k in _SET_SWITCH_KEYS):
        return BuildScope.SETS
    return _OP_SCOPES.get(kind, BuildScope.ACTIVE_SETS)


def ops_scope(*op_lists: list[dict[str, Any]] | None) -> BuildScope:
    """The widest `BuildScope` over every op of `op_lists`."""
    return BuildScope.widest(op_scope(op) for ops in op_lists for op in ops or [])


def _execute_batch_ops(
    w: BridgeWorker,
    ops: list[dict[str, Any]],
    cache: _BatchCache,
) -> list[dict[str, Any]]:
    """Execute a list of batch mutation ops in place with smart per-tab caching.

    Every step is checked before the first one runs, so a malformed later step costs no edit to roll back.
    """
    planned: list[tuple[int, _BatchOpHandler, dict[str, Any]]] = []
    for idx, op in enumerate(ops, start=1):
        if not isinstance(op, dict):
            raise ValueError(f"Batch op at step {idx} must be a JSON object.")
        raw_kind = str(op.get("op") or op.get("type") or op.get("action") or "").strip().lower()
        kind = raw_kind.replace("-", "_")
        handler = _BATCH_OP_HANDLERS.get(kind)
        if handler is None:
            raise ValueError(
                f"Unsupported batch op type '{raw_kind}' at step {idx}. "
                f"Example valid ops: {_BATCH_OP_EXAMPLE}"
            )
        planned.append((idx, handler, validate_batch_op(idx, kind, op)))
    return [handler(w, norm, cache, idx) for idx, handler, norm in planned]
