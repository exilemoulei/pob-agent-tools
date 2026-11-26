"""Item equipping, unequipping, flask toggles, and anoint operations."""

from __future__ import annotations

import re
from typing import Any

from ..analyzer import resolve_slot_name
from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from ..formatters import strip_pob_escapes
from ..guardrails import GuardrailScope, check_input, check_input_item_raw
from ..snapshot import run_mutation
from .common import (
    _BatchCache,
    _call_batch,
    _fold_search_text,
    _is_batch_error,
    _resolve_slot_from_items,
)


def try_item(
    worker: BridgeWorker,
    *,
    raw_text: str,
    slot_name: str | None = None,
    tooltip_only: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Evaluate or equip a pasted raw PoE item text."""
    raw_clean = raw_text.strip()
    if not raw_clean:
        raise ValueError("Item raw text cannot be empty.")
    if slot_name:
        check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input_item_raw(raw_clean, expected_game="poe1")

    with worker.lock:
        resolved_slot = resolve_slot_name(worker, slot_name) if slot_name else None
        tip_params: dict[str, Any] = {"raw": raw_clean, "compare": True}
        if resolved_slot:
            tip_params["slotName"] = resolved_slot
        native_tip = worker.call("item_tooltip", tip_params)

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
            items_res, tree_state = _call_batch(worker, [("list_items", {}), ("get_tree_state", {})])
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

        native_slot_key = resolved_slot or native_tip.get("slotName")
        extra_slots = [c for c in candidate_slots if c != native_slot_key]
        extra_tips_by_slot: dict[str, dict[str, Any]] = {}
        if extra_slots:
            extra_calls = [
                ("item_tooltip", {"raw": raw_clean, "slotName": c_slot, "compare": True})
                for c_slot in extra_slots
            ]
            extra_results = _call_batch(worker, extra_calls, raise_on_error=False)
            for c_slot, c_res in zip(extra_slots, extra_results, strict=False):
                if not _is_batch_error(c_res):
                    extra_tips_by_slot[c_slot] = c_res

        slot_tooltips: list[dict[str, Any]] = []
        for c_slot in candidate_slots:
            c_tip = native_tip if c_slot == native_slot_key else extra_tips_by_slot.get(c_slot)
            if c_tip is None:
                continue
            slot_tooltips.append(
                {"slotName": c_slot, "lines": c_tip.get("lines", []), "summary": c_tip.get("summary")}
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


def unequip_item(
    worker: BridgeWorker,
    slot: str | None = None,
    *,
    slot_name: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Unequip whatever item is currently in `slot` (0-residue what-if or `--apply`)."""
    target_slot = (slot if slot is not None else slot_name or "").strip()
    if not target_slot:
        raise ValueError("Specify a slot name to unequip.")
    check_input(target_slot, scope=GuardrailScope.SLOT, expected_game="poe1")

    def _do_unequip(w: BridgeWorker) -> dict[str, Any]:
        items_res = w.call("list_items", {})
        resolved_slot = _resolve_slot_from_items(items_res, target_slot)
        w.call("unequip_slot", {"slotName": resolved_slot})
        return {"slotName": resolved_slot, "unequipped": True}

    return run_mutation(worker, _do_unequip, apply=apply)


def _parse_flask_slots(flask_index: int | str) -> list[str]:
    idx_str = str(flask_index).strip().lower()
    if idx_str == "all":
        return [f"Flask {i}" for i in range(1, 6)]
    m_flask = re.match(r"^(?:flask\s*)?(\d+)$", idx_str)
    if m_flask:
        idx = int(m_flask.group(1))
        if 1 <= idx <= 5:
            return [f"Flask {idx}"]
    raise ValueError("Flask selector must be 1..5, 'Flask 1'..'Flask 5', or 'all'.")


def set_flask_active(
    worker: BridgeWorker,
    *,
    flask_index: int | str,
    active: bool,
    apply: bool = False,
) -> dict[str, Any]:
    """Toggle a flask slot (`Flask 1`..`Flask 5` or `all`) active or inactive."""
    slot_names = _parse_flask_slots(flask_index)

    def _do_flask(w: BridgeWorker) -> dict[str, Any]:
        calls = [("set_slot_active", {"slotName": s_name, "active": active}) for s_name in slot_names]
        results = _call_batch(w, calls)
        toggled = [
            {"slotName": s_name, "active": res.get("active", active)}
            for s_name, res in zip(slot_names, results, strict=False)
        ]
        label = "Flask 1..5 (all)" if len(slot_names) > 1 else slot_names[0]
        return {"slotName": label, "active": active, "slots": toggled}

    return run_mutation(worker, _do_flask, apply=apply)


def _apply_anoint_in_place(
    w: BridgeWorker,
    *,
    slot_name: str = "Amulet",
    notable_query: str,
    anoint_index: int = 1,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    if anoint_index < 1 or anoint_index > 4:
        raise ValueError("anoint_index must be between 1 and 4.")
    check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(notable_query, scope=GuardrailScope.NOTABLE_OR_OIL, expected_game="poe1")
    action_key = "anoint" if anoint_index == 1 else f"anoint{anoint_index}"

    items_res = cache.get_items() if cache is not None else w.call("list_items", {})
    resolved_slot = _resolve_slot_from_items(items_res, slot_name)
    slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
    if not slot_obj or not slot_obj.get("selItemId"):
        raise DaemonRequestError(f"Slot '{resolved_slot}' has no equipped item to anoint.")

    item_id = int(slot_obj["selItemId"])
    edit_state = w.call("item_edit_begin", {"id": item_id})
    try:
        actions = edit_state.get("actions", {})
        if not actions.get(action_key):
            raise DaemonRequestError(
                f"Item '{edit_state.get('name')}' in slot '{resolved_slot}' does not support '{action_key}' "
                "(may be Corrupted, Mirrored, or not an anointable base)."
            )

        pop = w.call("item_edit_popup", {"kind": "anoint", "action": "open", "slot": anoint_index})
        notable_ctl = next((c for c in pop.get("controls", []) if c.get("name") == "notableDB"), None)
        if not notable_ctl:
            raise RuntimeError("Anoint popup did not expose 'notableDB' control.")

        options = notable_ctl.get("options", [])
        q = notable_query.strip().lower()
        q_stripped = q.removeprefix("allocate ").removeprefix("配置 ").strip()
        q_folded = _fold_search_text(q_stripped)

        matched_opt: dict[str, Any] | None = None
        for opt in options:
            lbl = strip_pob_escapes(opt.get("label") or "").lower().strip()
            lbl_zh = strip_pob_escapes(opt.get("labelZh") or "").lower().strip()
            lbl_s = lbl.removeprefix("allocate ").strip()
            lbl_zh_s = lbl_zh.removeprefix("配置 ").strip()
            if (
                q in (lbl, lbl_zh, lbl_s, lbl_zh_s)
                or q_stripped in (lbl, lbl_zh, lbl_s, lbl_zh_s)
                or (q_folded and q_folded in (_fold_search_text(lbl_s), _fold_search_text(lbl_zh_s)))
            ):
                matched_opt = opt
                break

        if not matched_opt:
            sub_matches = [
                opt
                for opt in options
                if q_stripped in strip_pob_escapes(opt.get("label") or "").lower()
                or q_stripped in strip_pob_escapes(opt.get("labelZh") or "").lower()
                or (q_folded and q_folded in _fold_search_text(opt.get("label") or ""))
            ]
            if len(sub_matches) == 1:
                matched_opt = sub_matches[0]
            elif len(sub_matches) > 1:
                samples = ", ".join(
                    f"{strip_pob_escapes(o.get('labelZh') or o.get('label'))}" for o in sub_matches[:5]
                )
                raise ValueError(
                    f"Ambiguous notable query '{notable_query}' matched {len(sub_matches)} notables: {samples}"
                )

        if not matched_opt:
            raise ValueError(f"Notable '{notable_query}' not found in anointable notableDB options.")

        _call_batch(
            w,
            [
                ("item_edit_popup", {"kind": "anoint", "action": "pick", "name": "notableDB", "value": matched_opt["id"]}),
                ("item_edit_popup", {"kind": "anoint", "action": "apply", "button": "save"}),
            ],
        )
        commit_res = w.call("item_edit_commit", {"equip": True})
        if cache is not None:
            cache.invalidate("items", "skills")
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
    check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(notable_query, scope=GuardrailScope.NOTABLE_OR_OIL, expected_game="poe1")

    def _do_anoint(w: BridgeWorker) -> dict[str, Any]:
        return _apply_anoint_in_place(
            w, slot_name=slot_name, notable_query=notable_query, anoint_index=anoint_index
        )

    return run_mutation(worker, _do_anoint, apply=apply)
