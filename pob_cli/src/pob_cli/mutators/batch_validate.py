"""Strict schema and semantic validation for `pob batch` step dicts (L2)."""

from __future__ import annotations

import json
from typing import Any

from .batch_types import normalize_batch_op_types

# The op itself plus the names people give a remark; a key starting with `_` is a remark too.
_COMMON_OP_KEYS: frozenset[str] = frozenset(
    {"op", "type", "action", "comment", "note", "why", "desc", "description", "reason"}
)
_SKILL_SUB_ACTIONS: frozenset[str] = frozenset({"set", "add", "remove", "rm"})

_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "set",
        "reset",
        "assignments",
        "customMods",
        "custom_mods",
        "appendCustomMods",
        "append_custom_mods",
    }
)

_FLASK_KEYS: frozenset[str] = frozenset({"index", "flask_index", "flaskIndex", "slot", "flask", "active", "state", "enabled"})
_ITEM_KEYS: frozenset[str] = frozenset({"raw", "slot", "slotName", "slot_name"})
_ITEM_DB_KEYS: frozenset[str] = frozenset(
    {
        "query",
        "name",
        "dbType",
        "db_type",
        "kind",
        "index",
        "itemIndex",
        "item_index",
        "slot",
        "slotName",
        "slot_name",
    }
)
_ITEM_EDIT_KEYS: frozenset[str] = frozenset(
    {
        "slot",
        "slotName",
        "slot_name",
        "replaceMods",
        "replace_mods",
        "addMods",
        "add_mods",
        "removeMods",
        "remove_mods",
        "allowIllegalAffixes",
        "allow_illegal_affixes",
    }
)
_UNEQUIP_KEYS: frozenset[str] = frozenset({"slot", "slotName", "slot_name"})
_ANOINT_KEYS: frozenset[str] = frozenset({"notable", "slot", "slotName", "slot_name", "index"})
_TREE_KEYS: frozenset[str] = frozenset({"allocate", "deallocate", "allowCascade", "allow_cascade"})
_TATTOO_KEYS: frozenset[str] = frozenset(
    {"node", "nodeId", "node_id", "id", "tattoo", "name", "reset", "clear", "showLegacy", "show_legacy", "strictReq", "strict_req"}
)
_TREE_RESET_KEYS: frozenset[str] = frozenset({"tattoos"})
_IMPORT_TREE_URL_KEYS: frozenset[str] = frozenset({"url", "title", "name"})
_NEW_BUILD_KEYS: frozenset[str] = frozenset(
    {
        "class",
        "className",
        "class_name",
        "ascendancy",
        "asc",
        "ascendancyName",
        "ascendancy_name",
        "secondaryAscendancy",
        "secondary_ascendancy",
        "secondaryAsc",
        "level",
        "name",
    }
)
_SET_LEVEL_KEYS: frozenset[str] = frozenset({"level", "value", "auto"})
_SET_CLASS_KEYS: frozenset[str] = frozenset(
    {
        "class",
        "className",
        "class_name",
        "ascendancy",
        "asc",
        "ascendancyName",
        "ascendancy_name",
        "secondaryAscendancy",
        "secondary_ascendancy",
        "secondaryAsc",
        "secondaryAscendancyName",
    }
)
_SWITCH_MAIN_KEYS: frozenset[str] = frozenset(
    {
        "spec",
        "specIndex",
        "spec_index",
        "skillGroup",
        "skill_group",
        "group",
        "mainSkill",
        "mainSkillIndex",
        "main_skill_index",
        "skillPart",
        "skill_part",
        "skillPartIndex",
        "skill_part_index",
        "itemSet",
        "itemSetId",
        "item_set_id",
        "skillSet",
        "skillSetId",
        "skill_set_id",
        "configSet",
        "configSetId",
        "config_set_id",
        "loadout",
        "loadoutIndex",
        "loadout_index",
    }
)
_ADD_GROUP_KEYS: frozenset[str] = frozenset(
    {
        "gems",
        "label",
        "slot",
        "enabled",
        "includeInFullDPS",
        "include_in_full_dps",
        "fullDps",
        "makeMain",
        "make_main",
        "allowLegacy",
        "allow_legacy",
        "allowIllegalLevel",
        "allow_illegal_level",
    }
)
_DELETE_GROUP_KEYS: frozenset[str] = frozenset({"group", "index"})
_SET_GROUP_KEYS: frozenset[str] = frozenset(
    {
        "group",
        "index",
        "label",
        "enabled",
        "includeInFullDPS",
        "include_in_full_dps",
        "fullDps",
        "slot",
        "mainActiveSkill",
        "main_active_skill",
    }
)
_REPLACE_GROUP_KEYS: frozenset[str] = frozenset(
    {
        "group",
        "index",
        "gems",
        "label",
        "slot",
        "enabled",
        "includeInFullDPS",
        "include_in_full_dps",
        "fullDps",
        "allowLegacy",
        "allow_legacy",
        "allowIllegalLevel",
        "allow_illegal_level",
    }
)
_SKILL_COMMON_GROUP_KEYS: frozenset[str] = frozenset(
    {
        "group",
        "includeInFullDPS",
        "include_in_full_dps",
        "fullDps",
        "groupEnabled",
        "group_enabled",
    }
)
_SKILL_GEM_ATTR_KEYS: frozenset[str] = frozenset(
    {
        "level",
        "quality",
        "enabled",
        "count",
        "skillPart",
        "skill_part",
        "stageCount",
        "stage_count",
        "skillStageCount",
        "allowLegacy",
        "allow_legacy",
        "allowIllegalLevel",
        "allow_illegal_level",
    }
)
# What a removal never reads: another gem to edit and every gem attribute.
_REMOVAL_IGNORED_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "newGem",
        "new_gem",
        "level",
        "quality",
        "enabled",
        "count",
        "skillPart",
        "skill_part",
        "stageCount",
        "stage_count",
        "skillStageCount",
    }
)
_SKILL_KEYS: frozenset[str] = (
    _SKILL_COMMON_GROUP_KEYS
    | _SKILL_GEM_ATTR_KEYS
    | frozenset(
        {
            "gem",
            "index",
            "name",
            "newGem",
            "new_gem",
            "addGem",
            "add_gem",
            "removeGem",
            "remove_gem",
        }
    )
)
_SKILL_ADD_KEYS: frozenset[str] = (
    _SKILL_COMMON_GROUP_KEYS
    | _SKILL_GEM_ATTR_KEYS
    | frozenset({"gem", "name", "addGem", "add_gem"})
)
_SKILL_RM_KEYS: frozenset[str] = _SKILL_COMMON_GROUP_KEYS | frozenset(
    {"gem", "index", "removeGem", "remove_gem"}
)

_BORROW_KEYS: frozenset[str] = frozenset(
    {
        "items",
        "item_slots",
        "itemSlots",
        "slots",
        "slot",
        "item_slot",
        "skill_group",
        "skillGroup",
        "tree",
        "borrow_tree",
        "borrowTree",
        "tree_jewels",
        "treeJewels",
    }
)
_OP_VALID_KEYS: dict[str, frozenset[str]] = {
    "borrow": _BORROW_KEYS,
    "transplant": _BORROW_KEYS,
    "config": _CONFIG_KEYS,
    "config_set": _CONFIG_KEYS,
    "flask": _FLASK_KEYS,
    "item_flask": _FLASK_KEYS,
    "item": _ITEM_KEYS,
    "item_try": _ITEM_KEYS,
    "item_db": _ITEM_DB_KEYS,
    "item_edit": _ITEM_EDIT_KEYS,
    "unequip": _UNEQUIP_KEYS,
    "item_unequip": _UNEQUIP_KEYS,
    "unequip_slot": _UNEQUIP_KEYS,
    "anoint": _ANOINT_KEYS,
    "item_anoint": _ANOINT_KEYS,
    "tree": _TREE_KEYS,
    "tree_change": _TREE_KEYS,
    "tattoo": _TATTOO_KEYS,
    "tree_tattoo": _TATTOO_KEYS,
    "tree_reset": _TREE_RESET_KEYS,
    "reset_tree": _TREE_RESET_KEYS,
    "import_tree_url": _IMPORT_TREE_URL_KEYS,
    "new_build": _NEW_BUILD_KEYS,
    "set_level": _SET_LEVEL_KEYS,
    "set_class": _SET_CLASS_KEYS,
    "tree_set_class": _SET_CLASS_KEYS,
    "switch_main": _SWITCH_MAIN_KEYS,
    "add_group": _ADD_GROUP_KEYS,
    "new_group": _ADD_GROUP_KEYS,
    "skill_new_group": _ADD_GROUP_KEYS,
    "delete_group": _DELETE_GROUP_KEYS,
    "rm_group": _DELETE_GROUP_KEYS,
    "remove_group": _DELETE_GROUP_KEYS,
    "set_group": _SET_GROUP_KEYS,
    "replace_group": _REPLACE_GROUP_KEYS,
    "skill_replace_group": _REPLACE_GROUP_KEYS,
    "skill": _SKILL_KEYS,
    "skill_set": _SKILL_KEYS,
    "skill_add": _SKILL_ADD_KEYS,
    "skill_rm": _SKILL_RM_KEYS,
}


def _check_removal(idx: int, kind: str, op: dict[str, Any]) -> None:
    """A removal reads one gem selector and nothing else about a gem."""
    stray = sorted(k for k in op if k in _REMOVAL_IGNORED_KEYS and op[k] is not None)
    if stray:
        raise ValueError(
            f"Batch step #{idx} ({kind}): {stray} would be ignored by a removal; "
            "it takes only the gem to remove (edit or add another gem in its own step)."
        )
    named = {k: op[k] for k in ("removeGem", "remove_gem", "gem", "index") if op.get(k) is not None}
    if len({str(v).strip().lower() for v in named.values()}) > 1:
        raise ValueError(
            f"Batch step #{idx} ({kind}): names more than one gem to remove ({named}); one step removes one gem."
        )


def validate_batch_op(idx: int, kind: str, op: dict[str, Any]) -> dict[str, Any]:
    """Reject unknown keys, wrong types and silent no-op / ambiguous forms on batch step `idx`.

    Returns the step with canonical bools/ints (see `normalize_batch_op_types`) for its handler.
    """
    valid = _OP_VALID_KEYS.get(kind)
    if valid is None:
        return op

    unknown = sorted(k for k in op if k not in valid and k not in _COMMON_OP_KEYS and not str(k).startswith("_"))
    if unknown:
        if kind in ("config", "config_set"):
            wrap_obj = {k: op[k] for k in unknown}
            named = next((op[k] for k in ("var", "name", "key", "field") if isinstance(op.get(k), str)), None)
            if named and "value" in op:
                # `{"var": "enemyIsBoss", "value": "Pinnacle"}` names one variable; it is not two of them.
                wrap_obj = {named: op["value"]}
            try:
                wrap_json = json.dumps(wrap_obj)
            except TypeError:
                wrap_json = json.dumps({k: str(v) for k, v in wrap_obj.items()})
            raise ValueError(
                f"Batch step #{idx} ({kind}): unknown top-level key(s) {unknown}; "
                f'wrap PoB config variables inside "set": {wrap_json} '
                "(valid top-level keys: set, reset, assignments, customMods, appendCustomMods)."
            )
        raise ValueError(
            f"Batch step #{idx} ({kind}): unknown key(s) {unknown} "
            f"(valid keys: {', '.join(sorted(valid))})."
        )

    norm = normalize_batch_op_types(idx, kind, op)
    if kind in ("borrow", "transplant"):
        for group_label, alias_keys in (
            ("tree", ("tree", "borrow_tree", "borrowTree")),
            ("tree_jewels", ("tree_jewels", "treeJewels")),
        ):
            vals = [(k, bool(norm[k])) for k in alias_keys if norm.get(k) is not None]
            if len({v for _, v in vals}) > 1:
                raise ValueError(
                    f"Batch step #{idx} ({kind}): conflicting {group_label} aliases {dict(vals)}; "
                    "pass only one or matching booleans."
                )
        sg_vals = [
            (k, str(norm[k]).strip())
            for k in ("skill_group", "skillGroup")
            if norm.get(k) is not None and str(norm[k]).strip() != ""
        ]
        if len({v for _, v in sg_vals}) > 1:
            raise ValueError(
                f"Batch step #{idx} ({kind}): conflicting skill_group aliases {dict(sg_vals)}; pass only one."
            )
        has_items = any(
            bool(norm.get(k))
            for k in ("items", "item_slots", "itemSlots", "slots", "slot", "item_slot")
        )
        has_sg = bool(sg_vals)
        has_tree = any(bool(norm.get(k)) for k in ("tree", "borrow_tree", "borrowTree"))
        if not (has_items or has_sg or has_tree):
            raise ValueError(
                f"Batch step #{idx} ({kind}): borrows nothing; provide at least one of "
                "'items' (or 'slot'), 'skill_group', or 'tree': true."
            )
    if kind == "switch_main" and not any(op.get(k) is not None for k in _SWITCH_MAIN_KEYS):
        raise ValueError(
            f"Batch step #{idx} ({kind}): switches nothing; give at least one of spec, skillGroup, "
            "mainSkill, skillPart, itemSet, skillSet, configSet or loadout."
        )
    if kind == "skill_rm":
        _check_removal(idx, kind, op)
    if kind in ("skill", "skill_set"):
        raw_act = str(op.get("action") or "").strip().lower()
        sub_action = raw_act if raw_act in _SKILL_SUB_ACTIONS else ("set" if kind == "skill_set" else None)
        has_add = bool(op.get("addGem") or op.get("add_gem"))
        has_rm = op.get("removeGem") is not None or op.get("remove_gem") is not None
        has_gem = op.get("gem") is not None or (
            sub_action == "set"
            and op.get("index") is not None
            and not (has_add or "removeGem" in op or "remove_gem" in op)
        )
        if sub_action != "add" and op.get("name") is not None and not has_gem and not has_add:
            raise ValueError(
                f"Batch step #{idx} ({kind}): 'name' without 'gem' is ambiguous; "
                'use {"op": "skill_add", "group": ..., "name": "..."} (or "addGem") to add a gem, '
                'or pass both "gem" and "newGem" (or "name") to replace an existing gem.'
            )
        # The dispatcher does one of add / remove / edit and reads nothing of the others.
        removing = has_rm or sub_action in ("remove", "rm")
        if (has_add or sub_action == "add") and removing:
            raise ValueError(f"Batch step #{idx} ({kind}): adds and removes a gem in one step; use two steps.")
        if removing:
            _check_removal(idx, kind, op)
        if sub_action not in ("add", "remove", "rm"):
            has_full_dps = any(
                op.get(k) is not None for k in ("includeInFullDPS", "include_in_full_dps", "fullDps")
            )
            has_grp_en = any(op.get(k) is not None for k in ("groupEnabled", "group_enabled"))
            if not (has_full_dps or has_grp_en or has_add or has_rm or has_gem):
                raise ValueError(
                    f"Batch step #{idx} ({kind}): skill op performs zero mutations; "
                    'provide at least one of "gem", "addGem", "removeGem", "includeInFullDPS", or "groupEnabled".'
                )
    return norm
