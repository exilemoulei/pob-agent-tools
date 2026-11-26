"""Strict type checks for `pob batch` step fields, run before a step's handler (L2).

`normalize_batch_op_types` returns a shallow copy whose booleans and integers are canonical, so the
handlers' `bool(...)` / `int(...)` never see `"false"`, `true` standing in for 1, or `[2]`.
"""

from __future__ import annotations

from typing import Any

from .strict_args import (
    check_int_or_str,
    parse_bool,
    parse_gem_list,
    parse_int,
    parse_replace_mods,
    parse_str_list,
    parse_token_list,
)

_BOOL_KEYS: frozenset[str] = frozenset(
    {
        "active",
        "allowCascade",
        "allow_cascade",
        "tattoos",
        "auto",
        "enabled",
        "includeInFullDPS",
        "include_in_full_dps",
        "fullDps",
        "groupEnabled",
        "group_enabled",
        "makeMain",
        "make_main",
        "allowLegacy",
        "allow_legacy",
        "allowIllegalLevel",
        "allow_illegal_level",
        "allowIllegalAffixes",
        "allow_illegal_affixes",
    }
)
_INT_KEYS: frozenset[str] = frozenset(
    {
        "level",
        "quality",
        "count",
        "stageCount",
        "stage_count",
        "skillStageCount",
        "skillPart",
        "skill_part",
        "skillPartIndex",
        "skill_part_index",
        "mainSkill",
        "mainSkillIndex",
        "main_skill_index",
        "mainActiveSkill",
        "main_active_skill",
        "itemIndex",
        "item_index",
    }
)
# Selectors that take an index or a name.
_INT_OR_STR_KEYS: frozenset[str] = frozenset(
    {
        "spec",
        "specIndex",
        "spec_index",
        "skillGroup",
        "skill_group",
        "group",
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
        "gem",
        "removeGem",
        "remove_gem",
        "flask_index",
        "flaskIndex",
    }
)
# Where `index` is a 1-based integer rather than a name-or-index selector.
_INT_INDEX_KINDS: frozenset[str] = frozenset({"anoint", "item_anoint", "item_db"})
_TREE_KINDS: frozenset[str] = frozenset({"tree", "tree_change"})


def normalize_batch_op_types(idx: int, kind: str, op: dict[str, Any]) -> dict[str, Any]:
    """Type-check step `idx` and return a copy with canonical bools/ints (raises `ValueError`)."""
    out = dict(op)
    prefix = f"Batch step #{idx} ({kind})"
    try:
        for key, val in op.items():
            if val is None:
                continue
            if key in _BOOL_KEYS:
                out[key] = parse_bool(val, key)
            elif key in _INT_KEYS or (key == "value" and kind == "set_level"):
                out[key] = parse_int(val, key)
            elif key == "index":
                if kind in _INT_INDEX_KINDS:
                    out[key] = parse_int(val, key)
                else:
                    check_int_or_str(val, key)
            elif key in _INT_OR_STR_KEYS:
                check_int_or_str(val, key)
        if kind in _TREE_KINDS:
            for key in ("allocate", "deallocate"):
                out[key] = parse_token_list(op.get(key), key)
            if not out["allocate"] and not out["deallocate"]:
                raise ValueError("specify at least one node in 'allocate' or 'deallocate'")
        if kind == "item_edit":
            for key in ("replaceMods", "replace_mods"):
                if op.get(key) is not None:
                    out[key] = [list(p) for p in parse_replace_mods(op[key], key)]
            for key in ("addMods", "add_mods", "removeMods", "remove_mods"):
                if op.get(key) is not None:
                    out[key] = parse_str_list(op[key], key)
        if op.get("gems") is not None:
            out["gems"] = parse_gem_list(op["gems"])
    except ValueError as exc:
        raise ValueError(f"{prefix}: {exc}") from None
    return out
