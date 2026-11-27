"""Read-only build inspectors for ConfigTab, equipped items, and skill groups."""

from __future__ import annotations

import re
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..game_data import fold_han
from ..snapshot import _call_batch
from .constants import _ALWAYS_SHOW_VISIBLE_CONFIG_VARS, _KNOWN_CONFIG_DEFAULTS

_VISCERA_SLOT_NAMES: tuple[str, ...] = ("Head", "Body", "Arm", "Leg")
_VISCERA_PREFIX_RE = re.compile(r"^(?:viscera|graft)\s*[:_\-\s]\s*(head|body|arm|leg)$", re.IGNORECASE)
_NODE_SOCKET_RE = re.compile(r"^(?:node|socket)\s*:\s*(\d+)$|^socket\s*#\s*(\d+)$", re.IGNORECASE)


def resolve_slot_from_items(items_res: dict[str, Any], slot_query: str) -> str:
    """Resolve user slot query against an already-fetched `list_items` response.

    Supports:
    - 3.30 Wickerman Viscera slots (`Head`, `Body`, `Arm`, `Leg`, or explicit `viscera:*` / `graft:*`),
      including empty slots (`selItemId == 0`) while keeping lowercase `body`/`head` aliases mapped to
      `Body Armour`/`Helmet`.
    - Cross-build invariant passive-tree jewel sockets via `node:<nodeId>`, `socket:<nodeId>`, or `Socket #<nodeId>`.
    - `#1`, `#2` jewel socket index shorthand, EN/ZH aliases, and `Flask 1..5`.
    """
    from ..guardrails import GuardrailScope, check_input, normalize_equipment_slot

    raw_q = slot_query.strip()
    check_input(raw_q, scope=GuardrailScope.SLOT, expected_game="poe1")
    slots = items_res.get("slots", [])

    m_viscera = _VISCERA_PREFIX_RE.match(raw_q)
    if m_viscera:
        target_v = m_viscera.group(1).capitalize()
        for s in slots:
            if s.get("name") == target_v and bool(s.get("shown") or s.get("selItemId")):
                return s["name"]
        raise ValueError(f"3.30 Wickerman Viscera slot '{target_v}' is not available on this build.")

    if raw_q in _VISCERA_SLOT_NAMES or raw_q.lower() in ("arm", "leg"):
        target_v = raw_q.capitalize()
        for s in slots:
            if s.get("name") == target_v and bool(s.get("shown") or s.get("selItemId")):
                return s["name"]
        if raw_q.lower() in ("arm", "leg"):
            raise ValueError(f"Equipment slot '{slot_query}' (3.30 Wickerman Viscera) is not available on this build.")

    m_node = _NODE_SOCKET_RE.match(raw_q)
    if m_node:
        target_nid = int(m_node.group(1) or m_node.group(2))
        for s in slots:
            nid = s.get("nodeId")
            if nid is not None and int(nid) == target_nid and not s.get("inactive"):
                return s["name"]
        for s in slots:
            nid = s.get("nodeId")
            if nid is not None and int(nid) == target_nid:
                raise ValueError(
                    f"Jewel socket at nodeId={target_nid} is not allocated on the target passive tree "
                    f"(allocate node {target_nid} first, or pass --tree)."
                )
        avail = sorted({int(s["nodeId"]) for s in slots if s.get("nodeId") is not None and not s.get("inactive")})
        avail_str = ", ".join(str(x) for x in avail) if avail else "none"
        raise ValueError(
            f"Jewel socket with nodeId={target_nid} not found in build slots "
            f"(available allocated socket nodeIds: {avail_str})."
        )

    # Fold both sides: a Traditional query must hit the Simplified aliases, and the engine's labels
    # are Traditional by default.
    q = normalize_equipment_slot(fold_han(raw_q)) or raw_q

    m_jewel = re.match(r"^#?(\d+)$", q)
    if m_jewel:
        idx = int(m_jewel.group(1))
        for s in slots:
            if s.get("socketIndex") == idx:
                return s["name"]
        raise ValueError(f"Jewel socket #{idx} not found in build slots.")

    q_lower = q.lower()
    for s in slots:
        if (
            s.get("name", "").lower() == q_lower
            or fold_han(s.get("label") or "").lower() == q_lower
            or fold_han(s.get("labelZh") or "").lower() == q_lower
        ):
            return s["name"]

    for s in slots:
        if q_lower in s.get("name", "").lower() or q_lower in fold_han(s.get("labelZh") or "").lower():
            return s["name"]

    raise ValueError(f"Equipment slot '{slot_query}' not found.")


def resolve_slot_name(
    worker: BridgeWorker,
    slot_query: str,
    *,
    items_res: dict[str, Any] | None = None,
) -> str:
    """Resolve user slot query (including '#1', 'node:<nodeId>', 3.30 Viscera Head/Body/Arm/Leg, EN/ZH aliases, or Flask 1..5) to exact PoB slotName."""
    if items_res is None:
        from ..guardrails import GuardrailScope, check_input

        check_input(slot_query.strip(), scope=GuardrailScope.SLOT, expected_game="poe1")
        items_res = worker.call("list_items", {})
    return resolve_slot_from_items(items_res, slot_query)


def _infer_config_default(item: dict[str, Any]) -> Any:
    var = str(item.get("var") or "")
    if var in _KNOWN_CONFIG_DEFAULTS:
        return _KNOWN_CONFIG_DEFAULTS[var]
    itype = str(item.get("type") or "")
    if itype == "check":
        return False
    if itype == "list":
        opts = item.get("list") or []
        if opts and isinstance(opts[0], dict):
            return opts[0].get("val")
        return "None"
    return None


def inspect_config(
    worker: BridgeWorker,
    show_all: bool = False,
    *,
    include_defaults: bool = False,
    search: str | None = None,
    with_options: bool = False,
) -> dict[str, Any]:
    """Inspect active PoB ConfigTab set, active/non-default/searched config variables, and raw customMods."""
    effective_all = bool(show_all or include_defaults)
    search_clean = (search or "").strip()
    if search_clean:
        from ..guardrails import GuardrailScope, check_input

        check_input(search_clean, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
    search_low = search_clean.lower()

    with worker.lock:
        cfg_res = worker.call("list_config", {})

    active_id = cfg_res.get("activeConfigSetId")
    config_sets = cfg_res.get("configSets", [])
    active_set = next((s for s in config_sets if s.get("id") == active_id), None)

    variables: list[dict[str, Any]] = []
    total_count = 0
    active_count = 0

    for sec in cfg_res.get("sections", []):
        sec_name = sec.get("name") or ""
        sec_name_zh = sec.get("nameZh") or sec_name
        for item in sec.get("items", []):
            var = item.get("var")
            if not var:
                continue
            total_count += 1
            val = item.get("value")
            default_val = _infer_config_default(item)
            visible = bool(item.get("visible", True))

            if item.get("type") == "check":
                if default_val is True:
                    is_default = val is True
                else:
                    is_default = (val in (None, False)) and (default_val is False)
            elif item.get("type") == "list":
                is_default = (val is None) or (val == default_val)
            else:
                is_default = val in (None, "") or (val == default_val)

            is_active = visible and (not is_default or var in _ALWAYS_SHOW_VISIBLE_CONFIG_VARS)
            if is_active:
                active_count += 1

            label_clean = strip_pob_escapes(item.get("label") or "")
            label_zh_clean = strip_pob_escapes(item.get("labelZh") or item.get("label") or "")
            raw_list = item.get("list") or []

            matched_search = False
            if search_low:
                hay_parts = [str(var), label_clean, label_zh_clean, sec_name, sec_name_zh]
                for o in raw_list:
                    if isinstance(o, dict):
                        hay_parts.append(str(o.get("val") or ""))
                        hay_parts.append(strip_pob_escapes(o.get("label") or ""))
                        hay_parts.append(strip_pob_escapes(o.get("labelZh") or ""))
                haystack = " ".join(hay_parts).lower()
                matched_search = search_low in haystack
                if not matched_search:
                    continue
            elif not (effective_all or is_active):
                continue

            entry: dict[str, Any] = {
                "section": sec_name,
                "sectionZh": sec_name_zh,
                "var": var,
                "label": label_clean,
                "labelZh": label_zh_clean,
                "type": item.get("type"),
                "value": val,
                "defaultValue": default_val,
                "placeholder": item.get("placeholder"),
                "visible": visible,
                "isDefault": is_default,
            }
            if raw_list and (with_options or matched_search):
                entry["options"] = [
                    {
                        "val": o.get("val"),
                        "label": strip_pob_escapes(o.get("label") or ""),
                        "labelZh": strip_pob_escapes(o.get("labelZh") or o.get("label") or ""),
                    }
                    for o in raw_list
                    if isinstance(o, dict)
                ]
            variables.append(entry)

    custom_mods = cfg_res.get("customMods", [])
    enabled_texts: list[str] = []
    active_custom_lines: list[str] = []
    for blk in custom_mods:
        if blk.get("enabled", True):
            txt = str(blk.get("text") or "")
            if txt.strip():
                enabled_texts.append(txt.strip())
                for line in txt.splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        active_custom_lines.append(s)

    return {
        "activeConfigSetId": active_id,
        "activeConfigSet": active_set,
        "configSets": config_sets,
        "variables": variables,
        "activeCount": active_count,
        "totalCount": total_count,
        "includeDefaults": effective_all,
        "search": search_clean or None,
        "withOptions": bool(with_options),
        "customMods": custom_mods,
        "customModsText": "\n".join(enabled_texts),
        "activeCustomModLines": active_custom_lines,
        "rev": cfg_res.get("rev"),
    }


def inspect_items(
    worker: BridgeWorker,
    slot_selector: str | None = None,
    *,
    slot_name: str | None = None,
    raw: bool = False,
    include_raw: bool = False,
    all_raw: bool = False,
) -> dict[str, Any]:
    """List equipped items (pipelining all `item_raw` RPCs in ONE batch) or inspect one slot."""
    effective_slot = slot_selector if slot_selector is not None else slot_name
    if effective_slot:
        from ..guardrails import GuardrailScope, check_input

        check_input(effective_slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    want_raw = bool(raw or include_raw or all_raw)

    with worker.lock:
        if effective_slot:
            items_res, build_info = _call_batch(
                worker,
                [("list_items", {}), ("get_build_info", {})],
            )
            items_by_id = {it["id"]: it for it in items_res.get("items", [])}
            resolved_slot = resolve_slot_name(worker, effective_slot, items_res=items_res)
            slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
            item_id = slot_obj.get("selItemId") if slot_obj else None
            if not item_id:
                return {
                    "slot": slot_obj or {"name": resolved_slot},
                    "item": None,
                    "tooltip": None,
                    "raw": None,
                    "buildInfo": build_info,
                }
            if want_raw:
                tip, raw_res = _call_batch(
                    worker,
                    [
                        ("item_tooltip", {"id": item_id, "slotName": resolved_slot, "compare": False}),
                        ("item_raw", {"id": item_id}),
                    ],
                )
                raw_text = raw_res.get("raw") if isinstance(raw_res, dict) else None
            else:
                tip = worker.call("item_tooltip", {"id": item_id, "slotName": resolved_slot, "compare": False})
                raw_text = None
            return {
                "slot": slot_obj,
                "item": items_by_id.get(item_id),
                "tooltip": tip,
                "raw": raw_text,
                "buildInfo": build_info,
            }

        allocated_nodes: set[int] = set()
        if all_raw:
            items_res, tree_state, build_info = _call_batch(
                worker,
                [("list_items", {}), ("get_tree_state", {}), ("get_build_info", {})],
            )
            allocated_nodes = {int(x) for x in tree_state.get("allocatedNodes", [])}
        else:
            items_res, build_info = _call_batch(
                worker,
                [("list_items", {}), ("get_build_info", {})],
            )

        items_by_id = {it["id"]: it for it in items_res.get("items", [])}
        equipped_slots: list[dict[str, Any]] = []
        raw_ids_ordered: list[int] = []
        raw_ids_seen: set[int] = set()

        for s in items_res.get("slots", []):
            item_id = s.get("selItemId")
            node_id = s.get("nodeId")
            is_jewel_socket = node_id is not None
            is_allocated_socket = is_jewel_socket and (int(node_id) in allocated_nodes)

            if all_raw:
                if s.get("inactive"):
                    continue
                if not item_id and not is_allocated_socket:
                    continue
            else:
                if not item_id and not s.get("shown"):
                    continue

            item = items_by_id.get(item_id) if item_id else None
            entry: dict[str, Any] = {
                "slotName": s["name"],
                "label": s.get("labelZh") or s.get("label") or s["name"],
                "socketIndex": s.get("socketIndex"),
                "nodeId": node_id,
                "weaponSet": s.get("weaponSet"),
                "isFlask": bool(s.get("isFlask")),
                "active": bool(s.get("active")),
                "inactive": bool(s.get("inactive")),
                "itemId": item_id,
                "item": item,
            }
            if all_raw and is_jewel_socket:
                entry["allocated"] = is_allocated_socket
            if want_raw and item_id:
                iid = int(item_id)
                if iid not in raw_ids_seen:
                    raw_ids_seen.add(iid)
                    raw_ids_ordered.append(iid)
            equipped_slots.append(entry)

        if want_raw and raw_ids_ordered:
            raw_responses = _call_batch(
                worker,
                [("item_raw", {"id": iid}) for iid in raw_ids_ordered],
                raise_on_error=False,
            )
            raw_cache: dict[int, str | None] = {}
            for iid, res_obj in zip(raw_ids_ordered, raw_responses, strict=False):
                if isinstance(res_obj, dict) and not res_obj.get("_error") and res_obj.get("ok") is not False:
                    raw_cache[iid] = res_obj.get("raw")
                else:
                    raw_cache[iid] = None
            for entry in equipped_slots:
                if entry.get("itemId"):
                    entry["raw"] = raw_cache.get(int(entry["itemId"]))

        return {
            "buildInfo": build_info,
            "activeItemSetId": items_res.get("activeItemSetId"),
            "useSecondWeaponSet": bool(items_res.get("useSecondWeaponSet")),
            "itemSets": items_res.get("itemSets", []),
            "equippedSlots": equipped_slots,
            "equippedCount": sum(1 for e in equipped_slots if e.get("itemId")),
            "allRaw": all_raw,
            "totalItemsInBuild": len(items_res.get("items", [])),
        }


def resolve_skill_group_index(skills_res: dict[str, Any], group_selector: str | int | None) -> int:
    """Resolve 'main', 1-based group index, or group label substring into 1-based group index."""
    groups = skills_res.get("groups", [])
    if not groups:
        raise ValueError("Build has no skill groups.")
    if group_selector is None or str(group_selector).strip().lower() in ("", "main"):
        raw_main = skills_res.get("mainSocketGroup")
        try:
            main_idx = int(raw_main) if raw_main is not None else 1
        except (TypeError, ValueError):
            main_idx = 1
        if any(g.get("index") == main_idx for g in groups):
            return main_idx
        return int(groups[0]["index"])
    s = str(group_selector).strip()
    if s.isdigit():
        idx = int(s)
        if any(g.get("index") == idx for g in groups):
            return idx
        raise ValueError(f"Skill group #{idx} does not exist (1..{len(groups)}).")
    s_lower = s.lower()
    for g in groups:
        for key in ("label", "displayLabel", "displayLabelZh"):
            if s_lower in str(g.get(key) or "").lower():
                return int(g["index"])
    raise ValueError(f"Could not find skill group matching '{group_selector}'.")


def inspect_skills(
    worker: BridgeWorker,
    *,
    group_selector: str | None = None,
) -> dict[str, Any]:
    """List all skill groups or inspect a specific group with pipelined per-gem DPS tooltips."""
    with worker.lock:
        skills_res = worker.call("list_skills", {})
        if group_selector is not None:
            idx = resolve_skill_group_index(skills_res, group_selector)
            group = next((g for g in skills_res.get("groups", []) if g.get("index") == idx), None)
            if group is None:
                raise ValueError(f"Skill group #{idx} not found.")
            gems = group.get("gems", [])
            tip_calls = [("gem_tooltip", {"group": idx, "index": gem["index"]}) for gem in gems]
            tips = _call_batch(worker, tip_calls, raise_on_error=False) if tip_calls else []
            gems_with_tips = []
            for gem, tip in zip(gems, tips, strict=False):
                dps_val = (
                    tip.get("dps")
                    if isinstance(tip, dict) and not tip.get("_error") and tip.get("ok") is not False
                    else None
                )
                gems_with_tips.append({**gem, "tooltipDps": dps_val})
            return {
                "group": {**group, "gems": gems_with_tips},
                "mainSocketGroup": skills_res.get("mainSocketGroup"),
            }
        return skills_res
