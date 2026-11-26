# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 CoW transactional what-if mutator & build diff engine (L2)."""

from __future__ import annotations

from typing import Any

from ..analyzer.version_stamp import attach_version_stamp
from ..bridge_protocol import encode_xml_share_code
from ..errors import DaemonRequestError
from ..game_profile import GAME_ID
from ..guardrails import GuardrailScope, check_input, check_input_item_raw
from ..importers.share_urls import resolve_poe2_build_source
from ..snapshot import BuildScope, BuildTransaction, capture_digest, diff_digest, run_mutation


def _apply_mutation_ops(worker: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute one or more atomic PoE2 mutations on ``worker`` inside a BuildTransaction."""
    applied_ops: list[dict[str, Any]] = []

    if spec.get("level") is not None:
        lv = int(spec["level"])
        worker.call("set_build_field", {"field": "level", "value": lv})
        applied_ops.append({"op": "level", "value": lv})

    if spec.get("weapon_swap") is not None:
        on = bool(spec["weapon_swap"])
        worker.call("set_weapon_swap", {"on": on})
        applied_ops.append({"op": "weapon_swap", "on": on})

    if spec.get("alloc_mode") is not None:
        mode = int(spec["alloc_mode"])
        worker.call("set_alloc_mode", {"mode": mode})
        applied_ops.append({"op": "alloc_mode", "mode": mode})

    if spec.get("tree_node") is not None:
        nid = int(spec["tree_node"])
        worker.call("tree_click", {"id": nid})
        applied_ops.append({"op": "tree_click", "nodeId": nid})

    if spec.get("attribute_node") is not None:
        nid = int(spec["attribute_node"])
        attr_raw = spec.get("attribute")
        attr_map = {"str": 1, "strength": 1, "1": 1, "dex": 2, "dexterity": 2, "2": 2, "int": 3, "intelligence": 3, "3": 3}
        attr_idx = attr_map.get(str(attr_raw or "").strip().lower())
        if attr_idx is None:
            raise DaemonRequestError(f"Invalid attribute {attr_raw!r}; expected str/dex/int or 1/2/3.")
        worker.call("tree_attribute", {"id": nid, "attribute": attr_idx})
        applied_ops.append({"op": "tree_attribute", "nodeId": nid, "attribute": attr_idx})

    if spec.get("item_text"):
        raw_item = str(spec["item_text"]).strip()
        check_input_item_raw(raw_item, expected_game="poe2", emit_stderr=False)
        slot_name = str(spec.get("slot") or "").strip()
        add_res = worker.call("add_item", {"raw": raw_item})
        item_id = add_res.get("id")
        if slot_name and item_id:
            worker.call("equip_item", {"id": item_id, "slot": slot_name})
        applied_ops.append({"op": "equip_item", "slot": slot_name, "itemId": item_id})
    elif spec.get("unequip_slot"):
        slot_name = str(spec["unequip_slot"]).strip()
        worker.call("unequip_slot", {"slot": slot_name})
        applied_ops.append({"op": "unequip_slot", "slot": slot_name})

    if spec.get("gem_group") is not None or spec.get("set1") is not None or spec.get("set2") is not None:
        grp_idx = int(spec.get("gem_group") or 1)
        if spec.get("gem_index") is not None and any(
            spec.get(k) is not None for k in ("gem_level", "gem_quality", "gem_enabled")
        ):
            gp: dict[str, Any] = {"group": grp_idx, "gem": int(spec["gem_index"])}
            if spec.get("gem_level") is not None:
                gp["level"] = int(spec["gem_level"])
            if spec.get("gem_quality") is not None:
                gp["quality"] = int(spec["gem_quality"])
            if spec.get("gem_enabled") is not None:
                gp["enabled"] = bool(spec["gem_enabled"])
            worker.call("set_gem", gp)
            applied_ops.append({"op": "set_gem", **gp})
        if spec.get("set1") is not None or spec.get("set2") is not None:
            wsp: dict[str, Any] = {"index": grp_idx}
            if spec.get("set1") is not None:
                wsp["set1"] = bool(spec["set1"])
            if spec.get("set2") is not None:
                wsp["set2"] = bool(spec["set2"])
            worker.call("set_skill_weapon_set", wsp)
            applied_ops.append({"op": "set_skill_weapon_set", **wsp})

    if spec.get("config_key"):
        ckey = str(spec["config_key"]).strip()
        check_input(ckey, GuardrailScope.CONFIG_KEY, expected_game="poe2", emit_stderr=False)
        cval = spec.get("config_value")
        worker.call("set_config", {"var": ckey, "value": cval})
        applied_ops.append({"op": "set_config", "var": ckey, "value": cval})

    if spec.get("custom_mods") is not None:
        cmods = str(spec["custom_mods"])
        worker.call("set_custom_mods", {"text": cmods})
        applied_ops.append({"op": "set_custom_mods", "length": len(cmods)})

    if not applied_ops:
        raise DaemonRequestError(
            "No mutation specified. Pass one or more of: --level, --slot/--item-text, --unequip, "
            "--tree-node, --alloc-mode, --attribute-node/--attribute, --gem-group/--gem-index/--gem-level/--gem-quality, "
            "--set1/--set2, --config-key/--config-value, or --custom-mods."
        )
    return applied_ops


def run_what_if(
    worker: Any,
    mutation_spec: dict[str, Any],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Run a CoW what-if mutation (or persist when ``apply=True``) with 100% rollback verification."""
    res = run_mutation(
        worker,
        lambda w: _apply_mutation_ops(w, mutation_spec),
        apply=apply,
        scope=BuildScope.ACTIVE_SETS,
    )
    info = worker.call("get_build_info", {}) or {}
    return attach_version_stamp(
        res,
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


def diff_against_source(
    worker: Any,
    *,
    source: str | None = None,
    xml_code: str | None = None,
) -> dict[str, Any]:
    """Diff the live slot build against another PoE2 build source without mutating the live slot."""
    resolved = resolve_poe2_build_source(source=source, xml_code=xml_code, data_dir=getattr(worker, "data_dir", None))
    target_code = resolved.code or encode_xml_share_code(resolved.xml)
    with BuildTransaction(worker, scope=BuildScope.REPLACES_BUILD, will_commit=False) as tx:
        before_digest = capture_digest(worker)
        worker.call("import_code", {"code": target_code, "mode": "replace"})
        after_digest = capture_digest(worker)
        diff = diff_digest(before_digest, after_digest)
        tx.rollback()
    info = before_digest.get("buildInfo") or {}
    return attach_version_stamp(
        {
            "game": GAME_ID,
            "targetSource": resolved.source_label,
            "targetSourceKind": resolved.source_kind,
            "before": {
                "primaryDpsKey": before_digest["primaryDpsKey"],
                "primaryDps": before_digest["primaryDps"],
                "totalEhp": before_digest["totalEhp"],
                "life": before_digest["life"],
                "spirit": before_digest.get("spirit", 0.0),
            },
            "after": {
                "primaryDpsKey": after_digest["primaryDpsKey"],
                "primaryDps": after_digest["primaryDps"],
                "totalEhp": after_digest["totalEhp"],
                "life": after_digest["life"],
                "spirit": after_digest.get("spirit", 0.0),
            },
            "diff": diff,
        },
        pob2_version=getattr(worker, "pob_version", None),
        tree_version=info.get("treeVersion"),
    )


__all__ = ["diff_against_source", "run_what_if"]
