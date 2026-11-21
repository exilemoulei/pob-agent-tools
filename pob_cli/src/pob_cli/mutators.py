"""Build mutation operations (items, flasks, anoints, skills, passive tree, config, batch)."""

from __future__ import annotations

import re
from typing import Any

from .analyzer import resolve_skill_group_index, resolve_slot_name
from .bridge_client import BridgeWorker
from .formatters import strip_pob_escapes
from .snapshot import capture_digest, diff_digest, run_mutation


def try_item(
    worker: BridgeWorker,
    *,
    raw_text: str,
    slot_name: str | None = None,
    tooltip_only: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Evaluate or equip a pasted raw PoE item text.

    - Always runs PoB's native `item_tooltip{raw, slotName, compare=True}`.
    - Unless `tooltip_only=True`, also runs a snapshot `add_item{raw, equip=True}` to catch secondary
      effects (attribute shortages, uncapped resistances, lost item-granted skills) or persists when `apply=True`.
    - When `slot_name` is omitted and the item fits multiple slots (e.g. Ring 1 vs Ring 2, Weapon 1 vs Weapon 2),
      includes the comparison for all candidate slots in `tooltip`.
    """
    raw_clean = raw_text.strip()
    if not raw_clean:
        raise ValueError("Item raw text cannot be empty.")

    with worker.lock:
        resolved_slot = resolve_slot_name(worker, slot_name) if slot_name else None
        tip_params: dict[str, Any] = {"raw": raw_clean, "compare": True}
        if resolved_slot:
            tip_params["slotName"] = resolved_slot
        native_tip = worker.call("item_tooltip", tip_params)

        # Build multi-slot comparisons when slot_name is omitted and item fits multiple slots (P1-1)
        summary = native_tip.get("summary") or {}
        primary_slot = resolved_slot or native_tip.get("slotName") or summary.get("primarySlot") or ""
        item_type = str(summary.get("type") or "")

        if resolved_slot:
            candidate_slots = [resolved_slot]
        elif primary_slot in ("Ring 1", "Ring 2") or "Ring" in item_type:
            candidate_slots = ["Ring 1", "Ring 2"]
        elif primary_slot in ("Weapon 1", "Weapon 2"):
            candidate_slots = ["Weapon 1", "Weapon 2"]
        elif primary_slot.startswith("Flask") or "Flask" in item_type:
            candidate_slots = [f"Flask {i}" for i in range(1, 6)]
        elif "Jewel" in primary_slot or "Jewel" in item_type:
            items_res = worker.call("list_items", {})
            tree_state = worker.call("get_tree_state", {})
            alloc_set = {int(x) for x in tree_state.get("allocatedNodes", [])}
            candidate_slots = [
                s["name"]
                for s in items_res.get("slots", [])
                if s.get("nodeId") and int(s["nodeId"]) in alloc_set
            ][:8]
            if not candidate_slots and primary_slot:
                candidate_slots = [primary_slot]
        else:
            candidate_slots = [primary_slot] if primary_slot else []

        slot_tooltips: list[dict[str, Any]] = []
        for c_slot in candidate_slots:
            if c_slot == (resolved_slot or native_tip.get("slotName")):
                c_tip = native_tip
            else:
                try:
                    c_tip = worker.call("item_tooltip", {"raw": raw_clean, "slotName": c_slot, "compare": True})
                except Exception:
                    continue
            slot_tooltips.append(
                {
                    "slotName": c_slot,
                    "lines": c_tip.get("lines", []),
                    "summary": c_tip.get("summary"),
                }
            )

        if tooltip_only and not apply:
            return {
                "applied": False,
                "mode": "TOOLTIP-ONLY (fast preview)",
                "slotName": primary_slot or None,
                "tooltip": native_tip,
                "slotTooltips": slot_tooltips,
                "mutation": None,
            }

        def _do_equip(w: BridgeWorker) -> dict[str, Any]:
            add_params: dict[str, Any] = {"raw": raw_clean, "equip": True}
            if resolved_slot:
                add_params["slotName"] = resolved_slot
            res = w.call("add_item", add_params)
            return {
                "addedItem": res.get("item"),
                "slotName": resolved_slot or res.get("item", {}).get("primarySlot"),
            }

        mutation_res = run_mutation(worker, _do_equip, apply=apply)
        return {
            "applied": apply,
            "mode": mutation_res["mode"],
            "slotName": resolved_slot or mutation_res["detail"].get("slotName") or primary_slot,
            "tooltip": native_tip,
            "slotTooltips": slot_tooltips,
            "mutation": mutation_res,
        }


def set_flask_active(
    worker: BridgeWorker,
    *,
    flask_index: int | str,
    active: bool,
    apply: bool = False,
) -> dict[str, Any]:
    """Toggle a flask slot (`Flask 1`..`Flask 5` or `all`) active or inactive."""
    idx_str = str(flask_index).strip().lower()
    if idx_str == "all":
        slot_names = [f"Flask {i}" for i in range(1, 6)]
    elif idx_str.isdigit() and 1 <= int(idx_str) <= 5:
        slot_names = [f"Flask {int(idx_str)}"]
    else:
        raise ValueError("Flask selector must be 1..5 or 'all'.")

    def _do_flask(w: BridgeWorker) -> dict[str, Any]:
        toggled: list[dict[str, Any]] = []
        for s_name in slot_names:
            res = w.call("set_slot_active", {"slotName": s_name, "active": active})
            toggled.append({"slotName": s_name, "active": res.get("active", active)})
        label = "Flask 1..5 (all)" if len(slot_names) > 1 else slot_names[0]
        return {"slotName": label, "active": active, "slots": toggled}

    return run_mutation(worker, _do_flask, apply=apply)


def _apply_anoint_in_place(
    w: BridgeWorker,
    *,
    slot_name: str = "Amulet",
    notable_query: str,
    anoint_index: int = 1,
) -> dict[str, Any]:
    if anoint_index < 1 or anoint_index > 4:
        raise ValueError("anoint_index must be between 1 and 4.")
    action_key = "anoint" if anoint_index == 1 else f"anoint{anoint_index}"

    resolved_slot = resolve_slot_name(w, slot_name)
    items_res = w.call("list_items", {})
    slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
    if not slot_obj or not slot_obj.get("selItemId"):
        raise RuntimeError(f"Slot '{resolved_slot}' has no equipped item to anoint.")

    item_id = int(slot_obj["selItemId"])
    edit_state = w.call("item_edit_begin", {"id": item_id})
    try:
        actions = edit_state.get("actions", {})
        if not actions.get(action_key):
            raise RuntimeError(
                f"Item '{edit_state.get('name')}' in slot '{resolved_slot}' does not support '{action_key}' "
                f"(may be Corrupted, Mirrored, or not an anointable base)."
            )

        pop = w.call(
            "item_edit_popup",
            {"kind": "anoint", "action": "open", "slot": anoint_index},
        )
        notable_ctl = next(
            (c for c in pop.get("controls", []) if c.get("name") == "notableDB"),
            None,
        )
        if not notable_ctl:
            raise RuntimeError("Anoint popup did not expose 'notableDB' control.")

        options = notable_ctl.get("options", [])
        q = notable_query.strip().lower()
        q_stripped = q.removeprefix("allocate ").removeprefix("配置 ").strip()

        matched_opt: dict[str, Any] | None = None
        for opt in options:
            lbl = strip_pob_escapes(opt.get("label") or "").lower().strip()
            lbl_zh = strip_pob_escapes(opt.get("labelZh") or "").lower().strip()
            lbl_s = lbl.removeprefix("allocate ").strip()
            lbl_zh_s = lbl_zh.removeprefix("配置 ").strip()
            if q in (lbl, lbl_zh, lbl_s, lbl_zh_s) or q_stripped in (lbl, lbl_zh, lbl_s, lbl_zh_s):
                matched_opt = opt
                break

        if not matched_opt:
            sub_matches = [
                opt
                for opt in options
                if q_stripped in strip_pob_escapes(opt.get("label") or "").lower()
                or q_stripped in strip_pob_escapes(opt.get("labelZh") or "").lower()
            ]
            if len(sub_matches) == 1:
                matched_opt = sub_matches[0]
            elif len(sub_matches) > 1:
                samples = ", ".join(
                    f"{strip_pob_escapes(o.get('labelZh') or o.get('label'))}"
                    for o in sub_matches[:5]
                )
                raise ValueError(
                    f"Ambiguous notable query '{notable_query}' matched {len(sub_matches)} notables: {samples}"
                )

        if not matched_opt:
            raise ValueError(f"Notable '{notable_query}' not found in anointable notableDB options.")

        w.call(
            "item_edit_popup",
            {
                "kind": "anoint",
                "action": "pick",
                "name": "notableDB",
                "value": matched_opt["id"],
            },
        )
        w.call(
            "item_edit_popup",
            {"kind": "anoint", "action": "apply", "button": "save"},
        )
        commit_res = w.call("item_edit_commit", {"equip": True})
        return {
            "slotName": resolved_slot,
            "anointIndex": anoint_index,
            "notable": matched_opt,
            "item": commit_res.get("item"),
        }
    except Exception:
        try:
            w.call("item_edit_cancel", {})
        except Exception:
            pass
        raise


def anoint_item(
    worker: BridgeWorker,
    *,
    slot_name: str = "Amulet",
    notable_query: str,
    anoint_index: int = 1,
    apply: bool = False,
) -> dict[str, Any]:
    """Anoint an equipped item (default Amulet) using PoB's native `item_edit_*` popup workflow."""
    if anoint_index < 1 or anoint_index > 4:
        raise ValueError("anoint_index must be between 1 and 4.")

    def _do_anoint(w: BridgeWorker) -> dict[str, Any]:
        return _apply_anoint_in_place(
            w,
            slot_name=slot_name,
            notable_query=notable_query,
            anoint_index=anoint_index,
        )

    return run_mutation(worker, _do_anoint, apply=apply)


def _resolve_gem_in_group(group: dict[str, Any], gem_selector: str | int) -> dict[str, Any]:
    gems = group.get("gems", [])
    s = str(gem_selector).strip()
    target_gem = None
    if s.isdigit():
        idx = int(s)
        target_gem = next((gm for gm in gems if gm["index"] == idx), None)
    else:
        s_low = s.lower()
        for gm in gems:
            names = [
                str(gm.get("nameSpec") or ""),
                str(gm.get("name") or ""),
                str(gm.get("nameZh") or ""),
            ]
            if any(s_low in n.lower() for n in names):
                target_gem = gm
                break
    if not target_gem:
        raise ValueError(f"Gem '{gem_selector}' not found in skill group #{group.get('index')}.")
    return target_gem


def _resolve_gem_candidate(w: BridgeWorker, g_idx: int, gem_name: str) -> dict[str, Any] | None:
    search_res = w.call(
        "gem_search",
        {
            "group": g_idx,
            "query": gem_name.strip(),
            "byDps": False,
            "supportableOnly": False,
            "limit": 20,
        },
    )
    candidates = search_res.get("gems", [])
    q_low = gem_name.strip().lower()
    for c in candidates:
        if q_low in (str(c.get("name") or "").lower(), str(c.get("nameZh") or "").lower()):
            return c
    return candidates[0] if candidates else None


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
    def _do_skill_set(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        group = next(g for g in skills_res.get("groups", []) if g["index"] == g_idx)

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
        if gem_selector is not None:
            target_gem = _resolve_gem_in_group(group, gem_selector)
            gem_params: dict[str, Any] = {"group": g_idx, "index": target_gem["index"]}
            if new_gem:
                cand = _resolve_gem_candidate(w, g_idx, new_gem)
                if cand and cand.get("gemId"):
                    gem_params["gemId"] = cand["gemId"]
                    gem_params["nameSpec"] = cand.get("name") or new_gem.strip()
                else:
                    gem_params["nameSpec"] = new_gem.strip()
            if level is not None:
                gem_params["level"] = level
            if quality is not None:
                gem_params["quality"] = quality
            if enabled is not None:
                gem_params["enabled"] = enabled
            if count is not None:
                gem_params["count"] = count
            gem_result = w.call("set_gem", gem_params)

        return {"groupIndex": g_idx, "groupUpdates": group_updates, "gemResult": gem_result}

    return run_mutation(worker, _do_skill_set, apply=apply)


def _add_gem_with_default_skill_flags(w: BridgeWorker, params: dict[str, Any]) -> dict[str, Any]:
    """Match pob-mcp's added-gem defaults: base skill on, Vaal skill off."""
    res = w.call("add_gem", params)
    gem_index = (res.get("gem") or {}).get("index")
    if gem_index is None:
        raise RuntimeError("add_gem did not return the added gem index")
    updated = w.call(
        "set_gem",
        {"group": params["group"], "index": gem_index, "enableGlobal1": True, "enableGlobal2": False},
    )
    res["gem"] = updated.get("gem", res["gem"])
    return res


def add_skill_gem(
    worker: BridgeWorker,
    *,
    group_selector: str,
    gem_name: str,
    level: int = 20,
    quality: int = 20,
    enabled: bool = True,
    apply: bool = False,
) -> dict[str, Any]:
    """Add a gem (resolved via `gem_search` if needed) to a skill group."""
    def _do_add(w: BridgeWorker) -> dict[str, Any]:
        skills_res = w.call("list_skills", {})
        g_idx = resolve_skill_group_index(skills_res, group_selector)
        chosen = _resolve_gem_candidate(w, g_idx, gem_name)

        add_params: dict[str, Any] = {
            "group": g_idx,
            "level": level,
            "quality": quality,
            "enabled": enabled,
        }
        if chosen and chosen.get("gemId"):
            add_params["gemId"] = chosen["gemId"]
            add_params["nameSpec"] = chosen.get("name") or gem_name.strip()
        else:
            add_params["nameSpec"] = gem_name.strip()

        res = _add_gem_with_default_skill_flags(w, add_params)
        return {"groupIndex": g_idx, "addedGem": res, "resolvedCandidate": chosen}

    return run_mutation(worker, _do_add, apply=apply)


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
        group = next(g for g in skills_res.get("groups", []) if g["index"] == g_idx)
        target_gem = _resolve_gem_in_group(group, gem_selector)
        res = w.call("delete_gem", {"group": g_idx, "index": target_gem["index"]})
        return {"groupIndex": g_idx, "removedGem": target_gem, "result": res}

    return run_mutation(worker, _do_rm, apply=apply)


def _resolve_node_token(
    worker: BridgeWorker,
    token: str,
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
) -> tuple[int, int | None]:
    """Resolve a node token (`12345`, `12345:67890` mastery, or node name) into `(node_id, effect_id)`."""
    t = token.strip()
    effect_id: int | None = None
    if ":" in t:
        left, right = t.split(":", 1)
        t = left.strip()
        if right.strip().isdigit():
            effect_id = int(right.strip())
        else:
            # Resolve mastery effect by substring later once node_id is known
            effect_query = right.strip().lower()
            nid, _ = _resolve_node_token(worker, t, nodes_db, dynamic_map)
            node_meta = nodes_db.get(str(nid)) or dynamic_map.get(str(nid)) or {}
            for eff in node_meta.get("masteryEffects", []):
                text = " ".join(str(s) for s in (eff.get("stats") or []) + (eff.get("statsZh") or [])).lower()
                if effect_query in text:
                    return nid, int(eff["effect"])
            raise ValueError(f"Mastery effect matching '{right}' not found on node {nid}.")

    if t.isdigit():
        nid = int(t)
        if str(nid) not in nodes_db and str(nid) not in dynamic_map:
            raise ValueError(f"Passive tree node ID {nid} does not exist.")
        return nid, effect_id

    t_low = t.lower()
    exact_matches: list[int] = []
    sub_matches: list[int] = []
    for sid, meta in {**nodes_db, **dynamic_map}.items():
        name_en = str(meta.get("name") or "").lower()
        name_zh = str(meta.get("nameZh") or "").lower()
        if t_low in (name_en, name_zh):
            exact_matches.append(int(sid))
        elif t_low in name_en or t_low in name_zh:
            sub_matches.append(int(sid))

    candidates = exact_matches or sub_matches
    if not candidates:
        raise ValueError(f"No passive node found matching '{t}'.")
    if len(candidates) == 1:
        return candidates[0], effect_id

    # Prefer allocated or shortest path distance
    best_id = candidates[0]
    best_dist = 999999
    for cid in candidates[:15]:
        try:
            hover = worker.call("node_hover", {"id": cid})
            cost = hover.get("cost")
            dist = 0 if hover.get("allocated") else (int(cost) if cost is not None else 9999)
            if dist < best_dist:
                best_dist = dist
                best_id = cid
        except Exception:
            pass
    return best_id, effect_id


def _apply_tree_changes_in_place(
    w: BridgeWorker,
    resolved_alloc: list[tuple[int, int | None]],
    resolved_dealloc: list[tuple[int, int | None]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    actions_taken: list[dict[str, Any]] = []

    for nid, eff_id in resolved_alloc:
        cur_state = w.call("get_tree_state", {})
        alloc_set = {int(x) for x in cur_state.get("allocatedNodes", [])}
        if nid in alloc_set:
            if eff_id is not None:
                w.call("select_mastery", {"id": nid, "effect": eff_id})
                actions_taken.append({"action": "select_mastery", "id": nid, "effect": eff_id})
            else:
                actions_taken.append({"action": "skip_already_allocated", "id": nid})
            continue

        click_params: dict[str, Any] = {"id": nid}
        if eff_id is not None:
            click_params["effect"] = eff_id
        res = w.call("tree_click", click_params)
        if res.get("needsMastery"):
            effects = res.get("effects", [])
            opts_str = "; ".join(
                f"{e['effect']}: {' / '.join(e.get('statsZh') or e.get('stats') or [])}"
                for e in effects[:6]
            )
            raise ValueError(
                f"Node {nid} is a Mastery node and requires `<nodeId>:<effectId>`. Available effects: {opts_str}"
            )
        if res.get("needsConfirm"):
            raise ValueError(f"Node {nid} requires confirmation ({res.get('needsConfirm')}).")
        actions_taken.append({"action": "allocated", "id": nid, "effect": eff_id})

    for nid, _ in resolved_dealloc:
        cur_state = w.call("get_tree_state", {})
        alloc_set = {int(x) for x in cur_state.get("allocatedNodes", [])}
        if nid not in alloc_set:
            actions_taken.append({"action": "skip_already_unallocated", "id": nid})
            continue
        res = w.call("tree_click", {"id": nid})
        if res.get("needsConfirm"):
            raise ValueError(f"Node {nid} deallocation requires confirmation ({res.get('needsConfirm')}).")
        actions_taken.append({"action": "deallocated", "id": nid})

    final_state = w.call("get_tree_state", {})
    return actions_taken, final_state.get("points")


def change_tree(
    worker: BridgeWorker,
    *,
    allocate: list[str] | None = None,
    deallocate: list[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Allocate and/or deallocate passive tree nodes.

    Follows R12:
    - Single-node what-if (`apply=False`, 1 node total, no mastery effect mutation) uses fast `node_info{diff=true}` + `node_hover` preview PLUS full snapshot diff for accuracy.
    - Multi-node or `--apply` re-queries `get_tree_state()["allocatedNodes"]` before every `tree_click` so auto-pathing or cascading dependency removal never inverts a target node's state.
    """
    alloc_tokens = [x for x in (allocate or []) if x.strip()]
    dealloc_tokens = [x for x in (deallocate or []) if x.strip()]
    if not alloc_tokens and not dealloc_tokens:
        raise ValueError("Specify at least one node in --allocate or --deallocate.")

    with worker.lock:
        tree_state = worker.call("get_tree_state", {})
        nodes_db = worker.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
        dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}

        resolved_alloc = [_resolve_node_token(worker, tok, nodes_db, dynamic_map) for tok in alloc_tokens]
        resolved_dealloc = [_resolve_node_token(worker, tok, nodes_db, dynamic_map) for tok in dealloc_tokens]

        # Capture fast node_info + node_hover preview when operating on a single node
        single_preview = None
        if len(resolved_alloc) + len(resolved_dealloc) == 1:
            single_nid = (resolved_alloc or resolved_dealloc)[0][0]
            try:
                n_info = worker.call("node_info", {"id": single_nid, "diff": True})
                n_hover = worker.call("node_hover", {"id": single_nid})
                single_preview = {"nodeInfo": n_info, "nodeHover": n_hover}
            except Exception:
                pass

        def _do_tree_changes(w: BridgeWorker) -> dict[str, Any]:
            actions_taken, points = _apply_tree_changes_in_place(w, resolved_alloc, resolved_dealloc)
            return {
                "actions": actions_taken,
                "points": points,
                "singlePreview": single_preview,
            }

        return run_mutation(worker, _do_tree_changes, apply=apply)


_MOD_TAG_RE = re.compile(r"\{[^}]*\}")
_IMPLICITS_HDR_RE = re.compile(r"^Implicits:\s*(\d+)\s*$", re.IGNORECASE)
_ITEM_TRAILER_RE = re.compile(
    r"^(?:Split|Mirrored|Fractured Item|Corrupted|Unidentified|Synthesised Item|Foil Unique(?:\s*\(.*\))?)$",
    re.IGNORECASE,
)
_ITEM_HEADER_KEY_RE = re.compile(
    r"^(?:Rarity|Armour|Evasion|Energy Shield|Ward|ArmourBasePercentile|EvasionBasePercentile|"
    r"EnergyShieldBasePercentile|WardBasePercentile|Intangibility|Unique ID|League|Unreleased|"
    r"Crafted|Prefix|Suffix|Catalyst|CatalystQuality|Cluster Jewel Skill|Cluster Jewel Node Count|"
    r"Talisman Tier|Item Level|Memory Strands|Version|Selected Version|Variant|Selected Variant|"
    r"Selected Variant Group|Has Alt Variant.*|Selected Alt Variant.*|Allow Duplicate Variants|"
    r"Quality|Sockets|LevelReq|Radius|Limited to|Requires Class|Shaper Item|Elder Item|"
    r"Crusader Item|Redeemer Item|Hunter Item|Warlord Item|Searing Exarch Item|Eater of Worlds Item|"
    r"Synthesised Item)\b",
    re.IGNORECASE,
)


def _mod_line_matches(line: str, pattern: str) -> bool:
    pat = pattern.strip()
    if not pat:
        return False
    clean = strip_pob_escapes(_MOD_TAG_RE.sub("", line)).strip()
    pat_low = pat.lower()
    if pat_low in clean.lower() or pat_low in line.lower():
        return True
    try:
        if re.search(pat, clean, re.IGNORECASE) or re.search(pat, line, re.IGNORECASE):
            return True
    except re.error:
        return False
    return False


def _edit_raw_item_mods(
    raw_text: str,
    *,
    replace_mods: list[tuple[str, str]] | None = None,
    add_mods: list[str] | None = None,
    remove_mods: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Safely edit only the mod lines of a PoB item raw string without mangling headers or trailers."""
    rep_list = list(replace_mods or [])
    add_list = [m.strip() for m in (add_mods or []) if str(m).strip()]
    rem_list = [p.strip() for p in (remove_mods or []) if str(p).strip()]
    if not rep_list and not add_list and not rem_list:
        raise ValueError("Specify at least one of replace_mods, add_mods, or remove_mods.")

    lines = raw_text.splitlines()
    has_implicits_hdr = False
    header_lines: list[str] = []
    implicit_lines: list[str] = []
    after_implicit: list[str] = []

    imp_idx: int | None = None
    imp_count = 0
    for idx, line in enumerate(lines):
        m = _IMPLICITS_HDR_RE.match(line.strip())
        if m:
            imp_idx = idx
            imp_count = int(m.group(1))
            break

    if imp_idx is not None:
        has_implicits_hdr = True
        header_lines = lines[:imp_idx]
        implicit_lines = lines[imp_idx + 1 : imp_idx + 1 + imp_count]
        after_implicit = lines[imp_idx + 1 + imp_count :]
    else:
        sep_indices = [i for i, l in enumerate(lines) if l.strip() == "--------"]
        if sep_indices:
            last_sep = sep_indices[-1]
            header_lines = lines[: last_sep + 1]
            after_implicit = lines[last_sep + 1 :]
        else:
            # Minimal raw text without Implicits: or --------
            # Keep Rarity + item name/base + leading metadata lines untouched
            split_at = 0
            if lines and lines[0].strip().lower().startswith("rarity:"):
                rarity_val = lines[0].split(":", 1)[1].strip().upper()
                split_at = 3 if rarity_val in ("RARE", "UNIQUE") else 2
            while split_at < len(lines) and _ITEM_HEADER_KEY_RE.match(lines[split_at].strip()):
                split_at += 1
            header_lines = lines[:split_at]
            after_implicit = lines[split_at:]

    # Separate trailing item status lines (Corrupted, Split, Mirrored, etc.)
    trailer_rev: list[str] = []
    explicit_lines = list(after_implicit)
    while explicit_lines:
        tail = explicit_lines[-1].strip()
        if not tail or _ITEM_TRAILER_RE.match(tail):
            trailer_rev.append(explicit_lines.pop())
        else:
            break
    trailer_lines = list(reversed(trailer_rev))

    changes: list[dict[str, Any]] = []

    for pattern, new_line in rep_list:
        pat = str(pattern).strip()
        repl = str(new_line).strip()
        if not pat:
            raise ValueError("replace_mods pattern cannot be empty.")
        matched = False
        for i, line in enumerate(explicit_lines):
            if _mod_line_matches(line, pat):
                changes.append({"action": "replace", "section": "explicit", "before": line, "after": repl})
                explicit_lines[i] = repl
                matched = True
        if not matched:
            for i, line in enumerate(implicit_lines):
                if _mod_line_matches(line, pat):
                    changes.append({"action": "replace", "section": "implicit", "before": line, "after": repl})
                    implicit_lines[i] = repl
                    matched = True
        if not matched:
            raise ValueError(f"Pattern '{pat}' did not match any mod line on the item.")

    for pat in rem_list:
        kept_explicit: list[str] = []
        removed_any = False
        for line in explicit_lines:
            if _mod_line_matches(line, pat):
                changes.append({"action": "remove", "section": "explicit", "before": line})
                removed_any = True
            else:
                kept_explicit.append(line)
        explicit_lines = kept_explicit

        if not removed_any:
            kept_implicit: list[str] = []
            for line in implicit_lines:
                if _mod_line_matches(line, pat):
                    changes.append({"action": "remove", "section": "implicit", "before": line})
                    removed_any = True
                else:
                    kept_implicit.append(line)
            implicit_lines = kept_implicit

        if not removed_any:
            raise ValueError(f"Pattern '{pat}' did not match any mod line to remove.")

    for new_mod in add_list:
        for sub_line in new_mod.splitlines():
            s = sub_line.strip()
            if s:
                explicit_lines.append(s)
                changes.append({"action": "add", "section": "explicit", "after": s})

    out_lines: list[str] = list(header_lines)
    if has_implicits_hdr:
        out_lines.append(f"Implicits: {len(implicit_lines)}")
    out_lines.extend(implicit_lines)
    out_lines.extend(explicit_lines)
    out_lines.extend(trailer_lines)

    return "\n".join(out_lines), changes


def edit_item_mods(
    worker: BridgeWorker,
    *,
    slot_name: str,
    replace_mods: list[tuple[str, str]] | None = None,
    add_mods: list[str] | None = None,
    remove_mods: list[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Edit mod lines on an equipped item in place (replace/add/remove) with 0-residue what-if or --apply."""
    with worker.lock:
        resolved_slot = resolve_slot_name(worker, slot_name)
        items_res = worker.call("list_items", {})
        slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
        if not slot_obj or not slot_obj.get("selItemId"):
            raise RuntimeError(f"Slot '{resolved_slot}' has no equipped item to edit.")

        item_id = int(slot_obj["selItemId"])
        before_raw = worker.call("item_raw", {"id": item_id}).get("raw") or ""
        after_raw, changes = _edit_raw_item_mods(
            before_raw,
            replace_mods=replace_mods,
            add_mods=add_mods,
            remove_mods=remove_mods,
        )

        def _do_edit(w: BridgeWorker) -> dict[str, Any]:
            res = w.call("add_item", {"raw": after_raw, "slotName": resolved_slot, "equip": True})
            return {
                "slotName": resolved_slot,
                "item": res.get("item"),
                "changes": changes,
            }

        mutation_res = run_mutation(worker, _do_edit, apply=apply)
        return {
            "applied": apply,
            "mode": mutation_res["mode"],
            "slotName": resolved_slot,
            "beforeRaw": before_raw,
            "afterRaw": after_raw,
            "changes": changes,
            "mutation": mutation_res,
        }


def search_and_try_item_db(
    worker: BridgeWorker,
    *,
    query: str,
    db_type: str = "UNIQUE",
    slot_name: str | None = None,
    item_index: int | None = None,
    equip: bool = False,
    apply: bool = False,
    limit: int = 15,
) -> dict[str, Any]:
    """Search PoB's built-in Unique/Rare item DB (EN or ZH) and optionally try/equip a match."""
    q = (query or "").strip()
    if not q:
        raise ValueError("Item DB search query cannot be empty.")
    kind = "rare" if str(db_type or "UNIQUE").strip().lower() == "rare" else "unique"
    limit = max(1, min(200, int(limit)))

    with worker.lock:
        res = worker.call(
            "item_db",
            {"kind": kind, "query": q, "size": min(200, max(limit, 50)), "page": 1},
            timeout=30.0,
        )
        items = list(res.get("items", []))
        total_hits = int(res.get("total", len(items)))

        # Fallback client-side scan when query (e.g. ZH mod text or alias) got 0 hits from Lua filter
        if not items:
            q_low = q.lower()
            fallback_hits: list[dict[str, Any]] = []
            page = 1
            while page <= 12:
                page_res = worker.call("item_db", {"kind": kind, "query": "", "size": 200, "page": page}, timeout=30.0)
                page_items = page_res.get("items", [])
                if not page_items:
                    break
                for it in page_items:
                    hay = " ".join(
                        str(it.get(k) or "")
                        for k in ("title", "titleZh", "name", "nameZh", "baseName", "baseNameZh", "type", "raw")
                    ).lower()
                    if q_low in hay:
                        fallback_hits.append(it)
                if len(page_items) < 200 or len(fallback_hits) >= max(limit, 50):
                    break
                page += 1
            items = fallback_hits
            total_hits = len(fallback_hits)

        # Rank exact EN/ZH title/name matches first
        q_low = q.lower()

        def _rank_db_item(it: dict[str, Any]) -> tuple[int, str]:
            titles = [
                strip_pob_escapes(str(it.get(k) or "")).strip().lower()
                for k in ("title", "titleZh", "name", "nameZh")
                if it.get(k)
            ]
            if any(t == q_low for t in titles):
                return (0, titles[0] if titles else "")
            if any(t.startswith(q_low) for t in titles):
                return (1, titles[0] if titles else "")
            if any(q_low in t for t in titles):
                return (2, titles[0] if titles else "")
            return (3, titles[0] if titles else "")

        items.sort(key=_rank_db_item)

        indexed_items: list[dict[str, Any]] = []
        for idx, it in enumerate(items[:limit], start=1):
            entry = {**it, "index": idx}
            indexed_items.append(entry)

        if not equip and not apply:
            for entry in indexed_items[: min(limit, 10)]:
                raw_txt = entry.get("raw")
                if raw_txt:
                    try:
                        tip = worker.call("item_tooltip", {"raw": raw_txt, "compare": False})
                        entry["tooltipLines"] = tip.get("lines", [])
                    except Exception:
                        pass
            return {
                "query": query,
                "dbType": kind.upper(),
                "total": total_hits,
                "matches": indexed_items,
                "equipped": False,
                "applied": False,
                "selectedItem": None,
                "tooltip": None,
                "mutation": None,
            }

        if not items:
            raise ValueError(f"No {kind} database items matched query '{query}'.")

        if item_index is not None:
            if item_index < 1 or item_index > len(items):
                raise ValueError(f"item_index {item_index} out of range (1..{len(items)}).")
            chosen = {**items[item_index - 1], "index": item_index}
        else:
            chosen = {**items[0], "index": 1}

        chosen_raw = str(chosen.get("raw") or "").strip()
        if not chosen_raw:
            raise RuntimeError(f"Selected DB item '{chosen.get('title') or chosen.get('name')}' has no raw text.")

        resolved_slot = resolve_slot_name(worker, slot_name) if slot_name else None
        tip_params: dict[str, Any] = {"raw": chosen_raw, "compare": True}
        if resolved_slot:
            tip_params["slotName"] = resolved_slot
        native_tip = worker.call("item_tooltip", tip_params)

        def _do_equip_db(w: BridgeWorker) -> dict[str, Any]:
            add_params: dict[str, Any] = {"raw": chosen_raw, "equip": True}
            if resolved_slot:
                add_params["slotName"] = resolved_slot
            add_res = w.call("add_item", add_params)
            return {
                "addedItem": add_res.get("item"),
                "slotName": resolved_slot or (add_res.get("item") or {}).get("primarySlot") or chosen.get("primarySlot"),
            }

        mutation_res = run_mutation(worker, _do_equip_db, apply=apply)
        return {
            "query": query,
            "dbType": kind.upper(),
            "total": total_hits,
            "matches": indexed_items,
            "equipped": True,
            "applied": apply,
            "mode": mutation_res["mode"],
            "slotName": resolved_slot or mutation_res["detail"].get("slotName") or chosen.get("primarySlot"),
            "selectedItem": chosen,
            "tooltip": native_tip,
            "mutation": mutation_res,
        }


def _match_named_option(
    options: list[dict[str, Any]],
    query: str,
    entity_label: str,
) -> dict[str, Any]:
    q = query.strip()
    q_low = q.lower()
    if q_low in ("none", "无", "clear", "0"):
        for opt in options:
            if opt.get("id") == 0 or str(opt.get("name") or "").lower() == "none":
                return opt

    for opt in options:
        n_en = strip_pob_escapes(str(opt.get("name") or "")).strip().lower()
        n_zh = strip_pob_escapes(str(opt.get("nameZh") or "")).strip().lower()
        if q_low in (n_en, n_zh) or (q.isdigit() and int(q) == opt.get("id")):
            return opt

    sub_matches = [
        opt
        for opt in options
        if q_low in strip_pob_escapes(str(opt.get("name") or "")).strip().lower()
        or q_low in strip_pob_escapes(str(opt.get("nameZh") or "")).strip().lower()
    ]
    if len(sub_matches) == 1:
        return sub_matches[0]
    if len(sub_matches) > 1:
        samples = ", ".join(f"{o.get('name')} ({o.get('nameZh')})" for o in sub_matches[:6])
        raise ValueError(f"Ambiguous {entity_label} '{query}' matched multiple options: {samples}")

    available = ", ".join(f"{o.get('name')}/{o.get('nameZh')}" for o in options[:12])
    raise ValueError(f"Unknown {entity_label} '{query}'. Available: {available}")


def set_class_ascendancy(
    worker: BridgeWorker,
    *,
    class_name: str | None = None,
    ascendancy_name: str | None = None,
    secondary_ascendancy_name: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Change character class, primary ascendancy, and/or secondary ascendancy (Bloodline) in a snapshot transaction."""
    if not any(x is not None and str(x).strip() != "" for x in (class_name, ascendancy_name, secondary_ascendancy_name)):
        raise ValueError("Specify at least one of class_name, ascendancy_name, or secondary_ascendancy_name.")

    def _do_set_class(w: BridgeWorker) -> dict[str, Any]:
        lc = w.call("list_classes", {})
        classes = lc.get("classes", [])
        secondary = lc.get("secondary", [])
        before = dict(lc.get("current") or {})

        target_class_obj: dict[str, Any] | None = None
        target_asc_obj: dict[str, Any] | None = None
        target_sec_obj: dict[str, Any] | None = None
        changes: list[str] = []
        class_change_mode: str | None = None

        if class_name is not None and str(class_name).strip():
            target_class_obj = _match_named_option(classes, str(class_name), "class")
        else:
            cur_cid = before.get("classId")
            target_class_obj = next((c for c in classes if c.get("id") == cur_cid), None)

        if ascendancy_name is not None and str(ascendancy_name).strip():
            asc_q = str(ascendancy_name).strip()
            if class_name is not None and str(class_name).strip():
                assert target_class_obj is not None
                target_asc_obj = _match_named_option(
                    target_class_obj.get("ascendancies", []),
                    asc_q,
                    f"ascendancy for {target_class_obj.get('name')}",
                )
            else:
                # First try current class; if not found, infer owning class automatically (e.g. Reliquarian -> Scion)
                try:
                    assert target_class_obj is not None
                    target_asc_obj = _match_named_option(
                        target_class_obj.get("ascendancies", []),
                        asc_q,
                        f"ascendancy for {target_class_obj.get('name')}",
                    )
                except ValueError:
                    found_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
                    q_low = asc_q.lower()
                    for cls in classes:
                        for asc in cls.get("ascendancies", []):
                            if asc.get("id") == 0:
                                continue
                            n_en = strip_pob_escapes(str(asc.get("name") or "")).strip().lower()
                            n_zh = strip_pob_escapes(str(asc.get("nameZh") or "")).strip().lower()
                            if q_low in (n_en, n_zh) or q_low in n_en or q_low in n_zh:
                                found_pairs.append((cls, asc))
                    exact_pairs = [
                        p
                        for p in found_pairs
                        if q_low
                        in (
                            strip_pob_escapes(str(p[1].get("name") or "")).strip().lower(),
                            strip_pob_escapes(str(p[1].get("nameZh") or "")).strip().lower(),
                        )
                    ]
                    chosen_pairs = exact_pairs or found_pairs
                    if len(chosen_pairs) == 1:
                        target_class_obj, target_asc_obj = chosen_pairs[0]
                    elif len(chosen_pairs) > 1:
                        samples = ", ".join(f"{c['name']}/{a['name']}" for c, a in chosen_pairs[:6])
                        raise ValueError(f"Ambiguous ascendancy '{asc_q}': {samples}")
                    else:
                        raise

        if secondary_ascendancy_name is not None and str(secondary_ascendancy_name).strip():
            target_sec_obj = _match_named_option(secondary, str(secondary_ascendancy_name), "secondary ascendancy")

        # 1. Apply class change if needed
        if target_class_obj is not None and target_class_obj.get("id") != before.get("classId"):
            cid = int(target_class_obj["id"])
            res = w.call("set_class", {"classId": cid})
            if res.get("needsConfirm") == "class_change":
                res = w.call("set_class", {"classId": cid, "confirm": "connect"})
                if res.get("connectFailed") or res.get("needsConfirm") == "class_change":
                    res = w.call("set_class", {"classId": cid, "confirm": "reset"})
                    class_change_mode = "reset"
                else:
                    class_change_mode = "connect"
            else:
                class_change_mode = "direct"
            changes.append(
                f"class: {before.get('className')} -> {target_class_obj.get('name')} ({class_change_mode})"
            )

        # 2. Apply primary ascendancy
        if target_asc_obj is not None:
            aid = int(target_asc_obj["id"])
            w.call("set_ascendancy", {"ascendClassId": aid})
            changes.append(f"ascendancy: {before.get('ascendClassName')} -> {target_asc_obj.get('name')}")

        # 3. Apply secondary ascendancy (Bloodline)
        if target_sec_obj is not None:
            sid = int(target_sec_obj["id"])
            w.call("set_secondary_ascendancy", {"ascendClassId": sid})
            changes.append(f"secondaryAscendancy: {target_sec_obj.get('name')} (id={sid})")

        after = dict(w.call("list_classes", {}).get("current") or {})
        return {
            "before": before,
            "after": after,
            "changes": changes,
            "classChangeMode": class_change_mode,
        }

    return run_mutation(worker, _do_set_class, apply=apply)


def _coerce_config_value(raw_val: Any) -> Any:
    if isinstance(raw_val, (bool, int, float)):
        return raw_val
    s = str(raw_val).strip()
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _split_custom_mod_lines(raw_text: str) -> list[str]:
    """Split multiline or semicolon-separated custom mod text into clean non-empty lines."""
    lines: list[str] = []
    for raw_line in raw_text.splitlines():
        for part in raw_line.split(";"):
            s = part.strip()
            if s:
                lines.append(s)
    return lines


def _apply_config_in_place(
    w: BridgeWorker,
    assignments: list[tuple[str, Any]],
    custom_mods: str | None = None,
    append_custom_mods: str | None = None,
) -> dict[str, Any]:
    applied_vars: list[dict[str, Any]] = []
    touched_boss = False

    # Build case-insensitive config var lookup so e.g. MultiplierWitheredStackCount resolves to multiplierWitheredStackCount
    known_vars: dict[str, dict[str, Any]] = {}
    if assignments:
        cfg_meta = w.call("list_config", {})
        for sec in cfg_meta.get("sections", []):
            for item in sec.get("items", []):
                vname = item.get("var")
                if vname:
                    known_vars[str(vname).lower()] = item

    for raw_var, raw_val in assignments:
        item = known_vars.get(str(raw_var).strip().lower(), {})
        var = str(item.get("var") or str(raw_var).strip())
        v_str = str(raw_val).strip()
        list_options = item.get("list") or []
        none_is_option = item.get("type") == "list" and any(
            str(option.get("val")).lower() == "none" for option in list_options
        )
        if raw_val is None or v_str.lower() in ("reset", "null", "default", "__unset__") or (
            v_str.lower() == "none" and not none_is_option
        ):
            res = w.call("reset_config", {"var": var})
            applied_vars.append({"var": var, "action": "reset", "result": res})
        else:
            coerced = _coerce_config_value(raw_val)
            if var == "enemyIsBoss" and isinstance(coerced, str):
                coerced = {
                    "shaper": "Pinnacle", "sirus": "Pinnacle",
                    "uber atziri": "Boss", "atziri": "Boss",
                }.get(coerced.lower(), coerced)
            if item.get("type") == "list" and isinstance(coerced, str):
                for option in list_options:
                    if coerced.lower() in (
                        str(option.get("val")).lower(),
                        str(option.get("label") or "").lower(),
                    ):
                        coerced = option["val"]
                        break
            res = w.call("set_config", {"var": var, "value": coerced})
            applied_vars.append({"var": var, "action": "set", "value": coerced, "result": res})
        if var in ("enemyIsBoss", "presetBossSkills"):
            touched_boss = True

    if touched_boss:
        cfg = w.call("list_config", {})
        for sec in cfg.get("sections", []):
            for item in sec.get("items", []):
                if item.get("var") == "presetBossSkills" and item.get("value"):
                    w.call("set_config", {"var": "presetBossSkills", "value": item["value"]})
                    break

    if custom_mods is not None:
        cm_clean = custom_mods.strip()
        if cm_clean.lower() in ("clear", "reset", "none", "disable", "disabled", ""):
            w.call(
                "set_custom_mods",
                {"list": [{"title": "Default", "text": "# disabled", "enabled": True}]},
            )
            applied_vars.append({"var": "customMods", "action": "clear"})
        else:
            w.call(
                "set_custom_mods",
                {"list": [{"title": "CLI Custom Mods", "text": cm_clean, "enabled": True}]},
            )
            applied_vars.append({"var": "customMods", "action": "set", "value": cm_clean})

    if append_custom_mods is not None:
        new_lines = _split_custom_mod_lines(append_custom_mods)
        if not new_lines:
            raise ValueError("append_custom_mods cannot be empty.")

        cfg_cur = w.call("list_config", {})
        existing_blocks = list(cfg_cur.get("customMods") or [])
        updated_blocks: list[dict[str, Any]] = []
        target_idx: int | None = None

        for blk in existing_blocks:
            title = blk.get("title") or "Default"
            enabled = blk.get("enabled", True) is not False
            raw_blk_text = str(blk.get("text") or "")
            # Strip '# disabled' placeholder lines left by clear/dewater operations
            cleaned_lines = [
                ln
                for ln in raw_blk_text.splitlines()
                if ln.strip().lower() != "# disabled"
            ]
            cleaned_text = "\n".join(cleaned_lines).strip()
            updated_blocks.append({"title": title, "text": cleaned_text, "enabled": enabled})
            if enabled:
                target_idx = len(updated_blocks) - 1

        append_str = "\n".join(new_lines)
        if target_idx is None:
            updated_blocks.append({"title": "Default", "text": append_str, "enabled": True})
        else:
            prev_text = updated_blocks[target_idx]["text"]
            updated_blocks[target_idx]["text"] = f"{prev_text}\n{append_str}" if prev_text else append_str

        w.call("set_custom_mods", {"list": updated_blocks})
        combined_text = "\n".join(b["text"] for b in updated_blocks if b.get("enabled") and b.get("text"))
        applied_vars.append(
            {
                "var": "customMods",
                "action": "append",
                "appendedLines": new_lines,
                "customModsText": combined_text,
            }
        )

    return {"assignments": applied_vars}


def modify_config(
    worker: BridgeWorker,
    *,
    assignments: dict[str, Any] | list[tuple[str, Any]] | None = None,
    custom_mods: str | None = None,
    append_custom_mods: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Set/reset PoB ConfigTab variables and/or replace or append `customMods` (0-residue by default)."""
    pairs: list[tuple[str, Any]] = []
    if isinstance(assignments, dict):
        pairs = list(assignments.items())
    elif assignments:
        pairs = [(k, v) for k, v in assignments]

    if not pairs and custom_mods is None and append_custom_mods is None:
        raise ValueError("No config assignments, custom_mods, or append_custom_mods provided.")

    def _do_cfg(w: BridgeWorker) -> dict[str, Any]:
        return _apply_config_in_place(
            w,
            pairs,
            custom_mods=custom_mods,
            append_custom_mods=append_custom_mods,
        )

    return run_mutation(worker, _do_cfg, apply=apply)


LOADOUT_SENTINELS = frozenset({
    "loadouts:",
    "-----",
    "sync",
    "new loadout",
    "help >>",
})


def _resolve_id_or_title(
    items: list[dict[str, Any]],
    selector: int | str,
    *,
    id_key: str = "id",
    title_keys: tuple[str, ...] = ("title", "titleZh", "label", "labelZh"),
    entity_name: str = "item",
) -> int:
    s = str(selector).strip()
    if s.isdigit():
        val = int(s)
        if any(int(it.get(id_key, -1)) == val for it in items):
            return val
        if 1 <= val <= len(items):
            return int(items[val - 1][id_key])
    s_low = s.lower()
    for it in items:
        for k in title_keys:
            if s_low == strip_pob_escapes(str(it.get(k) or "")).strip().lower():
                return int(it[id_key])
    for it in items:
        for k in title_keys:
            if s_low in strip_pob_escapes(str(it.get(k) or "")).strip().lower():
                return int(it[id_key])
    raise ValueError(f"Could not find {entity_name} matching '{selector}'.")


def switch_main(
    worker: BridgeWorker,
    *,
    spec_index: int | str | None = None,
    skill_group: str | int | None = None,
    main_skill_index: int | None = None,
    skill_part_index: int | None = None,
    item_set_id: int | str | None = None,
    skill_set_id: int | str | None = None,
    config_set_id: int | str | None = None,
    loadout_index: int | str | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Switch active spec, main skill group, active skill/part, item/skill/config set, or synced loadout."""
    def _do_switch(w: BridgeWorker) -> dict[str, Any]:
        changes: list[str] = []
        if loadout_index is not None:
            lo_res = w.call("list_loadouts", {})
            raw_list = lo_res.get("list", [])
            real_loadouts = [
                it
                for it in raw_list
                if strip_pob_escapes(str(it.get("label") or "")).strip().lower() not in LOADOUT_SENTINELS
            ]
            if not real_loadouts:
                raise ValueError("Build has no synced loadouts available.")
            s_lo = str(loadout_index).strip()
            if s_lo.isdigit():
                k = int(s_lo)
                if 1 <= k <= len(real_loadouts):
                    target_raw_idx = int(real_loadouts[k - 1]["index"])
                elif any(int(it["index"]) == k for it in real_loadouts):
                    target_raw_idx = k
                else:
                    raise ValueError(f"Loadout #{k} out of range (1..{len(real_loadouts)}).")
            else:
                target_raw_idx = _resolve_id_or_title(
                    real_loadouts,
                    s_lo,
                    id_key="index",
                    title_keys=("label",),
                    entity_name="loadout",
                )
            w.call("select_loadout", {"index": target_raw_idx})
            changes.append(f"loadout={loadout_index} (rawIndex={target_raw_idx})")
        if spec_index is not None:
            specs_res = w.call("list_specs", {})
            resolved_spec = _resolve_id_or_title(
                specs_res.get("specs", []),
                spec_index,
                id_key="index",
                entity_name="tree spec",
            )
            w.call("set_active_spec", {"index": resolved_spec})
            changes.append(f"activeSpec={resolved_spec}")
        if item_set_id is not None:
            items_res = w.call("list_items", {})
            resolved_iset = _resolve_id_or_title(
                items_res.get("itemSets", []),
                item_set_id,
                id_key="id",
                entity_name="item set",
            )
            w.call("set_item_set", {"id": resolved_iset})
            changes.append(f"itemSetId={resolved_iset}")
        if skill_set_id is not None:
            skills_res = w.call("list_skills", {})
            resolved_sset = _resolve_id_or_title(
                skills_res.get("skillSets", []),
                skill_set_id,
                id_key="id",
                entity_name="skill set",
            )
            w.call("set_skill_set", {"id": resolved_sset})
            changes.append(f"skillSetId={resolved_sset}")
        if config_set_id is not None:
            cfg_res = w.call("list_config", {})
            resolved_cset = _resolve_id_or_title(
                cfg_res.get("configSets", []),
                config_set_id,
                id_key="id",
                entity_name="config set",
            )
            w.call("set_config_set", {"id": resolved_cset})
            changes.append(f"configSetId={resolved_cset}")
        if skill_group is not None:
            skills_res = w.call("list_skills", {})
            g_idx = resolve_skill_group_index(skills_res, skill_group)
            w.call("set_build_field", {"field": "mainSocketGroup", "value": g_idx})
            changes.append(f"mainSocketGroup={g_idx}")
        if main_skill_index is not None:
            w.call("set_build_field", {"field": "mainSkill", "value": int(main_skill_index)})
            changes.append(f"mainSkill={main_skill_index}")
        if skill_part_index is not None:
            w.call("set_build_field", {"field": "mainSkillPart", "value": int(skill_part_index)})
            changes.append(f"mainSkillPart={skill_part_index}")
        return {"changes": changes, "header": w.call("get_build_header", {})}

    return run_mutation(worker, _do_switch, apply=apply)


def run_batch(
    worker: BridgeWorker,
    *,
    ops: list[dict[str, Any]],
    apply: bool = False,
) -> dict[str, Any]:
    """Execute a composite list of mutations in a single snapshot transaction."""
    if not ops:
        raise ValueError("Batch operations list cannot be empty.")

    def _do_batch(w: BridgeWorker) -> dict[str, Any]:
        step_results: list[dict[str, Any]] = []
        for idx, op in enumerate(ops, start=1):
            kind = str(op.get("op") or op.get("type") or "").lower()
            if kind == "config":
                pairs = [(k, v) for k, v in (op.get("set") or {}).items()]
                for var in op.get("reset") or []:
                    pairs.append((var, "reset"))
                cm = op.get("customMods")
                acm = op.get("appendCustomMods")
                cfg_res = _apply_config_in_place(
                    w,
                    pairs,
                    custom_mods=str(cm) if cm is not None else None,
                    append_custom_mods=str(acm) if acm is not None else None,
                )
                step_results.append({"step": idx, "op": "config", "count": len(cfg_res["assignments"])})
            elif kind == "flask":
                f_sel = str(op.get("index", "all")).strip().lower()
                act = bool(op.get("active", True))
                slots = [f"Flask {i}" for i in range(1, 6)] if f_sel == "all" else [f"Flask {int(f_sel)}"]
                for s_name in slots:
                    w.call("set_slot_active", {"slotName": s_name, "active": act})
                step_results.append({"step": idx, "op": "flask", "slotName": ",".join(slots)})
            elif kind == "item":
                slot_name = resolve_slot_name(w, op["slot"]) if op.get("slot") else None
                params: dict[str, Any] = {"raw": str(op["raw"]).strip(), "equip": True}
                if slot_name:
                    params["slotName"] = slot_name
                res = w.call("add_item", params)
                step_results.append({"step": idx, "op": "item", "item": res.get("item")})
            elif kind == "anoint":
                an_res = _apply_anoint_in_place(
                    w,
                    slot_name=str(op.get("slot") or "Amulet"),
                    notable_query=str(op["notable"]),
                    anoint_index=int(op.get("index", 1)),
                )
                step_results.append({"step": idx, "op": "anoint", "notable": an_res.get("notable")})
            elif kind == "tree":
                tree_state = w.call("get_tree_state", {})
                nodes_db = w.get_cached_tree_data(tree_state.get("treeVersion")).get("nodes", {})
                dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}
                resolved_alloc = [
                    _resolve_node_token(w, str(tok), nodes_db, dynamic_map)
                    for tok in (op.get("allocate") or [])
                ]
                resolved_dealloc = [
                    _resolve_node_token(w, str(tok), nodes_db, dynamic_map)
                    for tok in (op.get("deallocate") or [])
                ]
                actions_taken, _ = _apply_tree_changes_in_place(w, resolved_alloc, resolved_dealloc)
                step_results.append({"step": idx, "op": "tree", "actions": actions_taken})
            elif kind == "skill":
                skills_res = w.call("list_skills", {})
                g_idx = resolve_skill_group_index(skills_res, op.get("group", "main"))
                group = next(g for g in skills_res.get("groups", []) if g["index"] == g_idx)
                if op.get("addGem"):
                    gem_name = str(op["addGem"]).strip()
                    cand = _resolve_gem_candidate(w, g_idx, gem_name)
                    add_params: dict[str, Any] = {
                        "group": g_idx,
                        "level": int(op.get("level", 20)),
                        "quality": int(op.get("quality", 20)),
                        "enabled": bool(op.get("enabled", True)),
                    }
                    if cand and cand.get("gemId"):
                        add_params["gemId"] = cand["gemId"]
                        add_params["nameSpec"] = cand.get("name") or gem_name
                    else:
                        add_params["nameSpec"] = gem_name
                    _add_gem_with_default_skill_flags(w, add_params)
                elif op.get("removeGem") is not None:
                    t_gem = _resolve_gem_in_group(group, op["removeGem"])
                    w.call("delete_gem", {"group": g_idx, "index": t_gem["index"]})
                elif op.get("gem") is not None:
                    t_gem = _resolve_gem_in_group(group, op["gem"])
                    gem_params: dict[str, Any] = {"group": g_idx, "index": t_gem["index"]}
                    if op.get("newGem"):
                        cand = _resolve_gem_candidate(w, g_idx, str(op["newGem"]))
                        if cand and cand.get("gemId"):
                            gem_params["gemId"] = cand["gemId"]
                            gem_params["nameSpec"] = cand.get("name") or str(op["newGem"]).strip()
                        else:
                            gem_params["nameSpec"] = str(op["newGem"]).strip()
                    for k in ("level", "quality", "enabled", "count"):
                        if k in op:
                            gem_params[k] = op[k]
                    w.call("set_gem", gem_params)
                step_results.append({"step": idx, "op": "skill", "group": g_idx})
            else:
                raise ValueError(f"Unsupported batch op type '{kind}' at step {idx}.")

        return {"steps": step_results}

    return run_mutation(worker, _do_batch, apply=apply)


def trade_eval_items(
    worker: BridgeWorker,
    *,
    slot_name: str | None,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Read-only batch what-if evaluation of candidate items against a single build snapshot.

    Captures `export_code` and `capture_digest` once under `worker.lock`, evaluates each
    candidate item isolated against `snap_before` (preventing 2H/shield or item-granted
    skill bleed between candidates), and always restores `snap_before` in `finally`.
    """
    if not items:
        return {"slotName": slot_name, "before": None, "results": []}

    with worker.lock:
        resolved_slot = resolve_slot_name(worker, slot_name) if slot_name else None
        snap_before = worker.call("export_code", {})["code"]
        digest_before = capture_digest(worker)
        before_summary = {
            "primaryDpsKey": digest_before["primaryDpsKey"],
            "primaryDps": digest_before["primaryDps"],
            "totalEhp": digest_before["totalEhp"],
            "life": digest_before["life"],
            "points": digest_before.get("buildInfo", {}).get("points", {}),
        }

        results: list[dict[str, Any]] = []
        dirty = False
        try:
            for entry in items:
                item_id = entry.get("id")
                raw_clean = str(entry.get("raw") or "").strip()
                if not raw_clean:
                    results.append({"id": item_id, "ok": False, "error": "Empty item text."})
                    continue

                if dirty:
                    worker.call("import_code", {"code": snap_before, "mode": "replace"})
                    dirty = False

                try:
                    add_params: dict[str, Any] = {"raw": raw_clean, "equip": True}
                    if resolved_slot:
                        add_params["slotName"] = resolved_slot
                    dirty = True
                    worker.call("add_item", add_params)
                    digest_after = capture_digest(worker)
                    diff = diff_digest(digest_before, digest_after)
                    results.append(
                        {
                            "id": item_id,
                            "ok": True,
                            "before": before_summary,
                            "after": {
                                "primaryDpsKey": digest_after["primaryDpsKey"],
                                "primaryDps": digest_after["primaryDps"],
                                "totalEhp": digest_after["totalEhp"],
                                "life": digest_after["life"],
                                "points": digest_after.get("buildInfo", {}).get("points", {}),
                            },
                            "diff": diff,
                        }
                    )
                except Exception as exc:
                    results.append({"id": item_id, "ok": False, "error": str(exc)})
        finally:
            if dirty:
                worker.call("import_code", {"code": snap_before, "mode": "replace"})

        return {
            "slotName": resolved_slot or slot_name,
            "before": before_summary,
            "results": results,
        }
