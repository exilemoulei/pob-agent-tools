"""Item equipping, unequipping, flask toggles, anoints, and Unique/Rare DB search."""

from __future__ import annotations

import re
from typing import Any

from .. import game_data as _game_data
from ..analyzer import resolve_slot_name
from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from ..formatters import strip_pob_escapes
from ..guardrails import GuardrailScope, check_input, check_input_item_raw
from ..snapshot import run_mutation
from .common import (
    _BatchCache,
    _call_batch,
    _fold_no_apos,
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


def _search_item_db_items(
    worker: BridgeWorker,
    *,
    query: str,
    db_type: str = "UNIQUE",
    limit: int = 15,
) -> tuple[str, list[dict[str, Any]], int]:
    """Search PoB's Unique/Rare DB with NFKD diacritic/apostrophe folding fallback and exact-title ranking."""
    q = (query or "").strip()
    if not q:
        raise ValueError("Item DB search query cannot be empty.")
    check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    kind = "rare" if str(db_type or "UNIQUE").strip().lower() == "rare" else "unique"
    limit = max(1, min(200, int(limit)))

    res = worker.call(
        "item_db",
        {"kind": kind, "query": q, "size": min(200, max(limit, 50)), "page": 1},
        timeout=30.0,
    )
    items = list(res.get("items", []))
    total_hits = int(res.get("total", len(items)))

    # Simplified and Traditional translations often name the same item differently (法师之血 vs
    # 魔血), so a Chinese query the engine's display locale misses retries by its English name.
    q_en = _game_data.english_for_zh(q, pob_dir=getattr(worker, "pob_root", None)) if not items else None
    if q_en:
        res = worker.call(
            "item_db",
            {"kind": kind, "query": q_en, "size": min(200, max(limit, 50)), "page": 1},
            timeout=30.0,
        )
        items = list(res.get("items", []))
        total_hits = int(res.get("total", len(items)))
        if items:
            q = q_en

    if not items:
        q_low = q.lower()
        q_fold = _fold_search_text(q)
        q_no_apos = _fold_no_apos(q)
        fallback_hits: list[dict[str, Any]] = []
        page = 1
        while page <= 12:
            page_res = worker.call("item_db", {"kind": kind, "query": "", "size": 200, "page": page}, timeout=30.0)
            page_items = page_res.get("items", [])
            if not page_items:
                break
            for it in page_items:
                hay_raw = " ".join(
                    str(it.get(k) or "")
                    for k in ("title", "titleZh", "name", "nameZh", "baseName", "baseNameZh", "type", "raw")
                )
                if q_low in hay_raw.lower():
                    fallback_hits.append(it)
                    continue
                hay_fold = _fold_search_text(hay_raw)
                if (q_fold and q_fold in hay_fold) or (q_no_apos and q_no_apos in hay_fold.replace("'", "")):
                    fallback_hits.append(it)
            if len(page_items) < 200 or len(fallback_hits) >= max(limit, 50):
                break
            page += 1
        items = fallback_hits
        total_hits = len(fallback_hits)

    q_low = q.lower()
    q_fold = _fold_no_apos(q)

    def _rank_db_item(it: dict[str, Any]) -> tuple[int, str]:
        titles = [
            strip_pob_escapes(str(it.get(k) or "")).strip().lower()
            for k in ("title", "titleZh", "name", "nameZh")
            if it.get(k)
        ]
        folded_titles = [_fold_no_apos(t) for t in titles]
        if any(t == q_low for t in titles) or (q_fold and any(ft == q_fold for ft in folded_titles)):
            return (0, titles[0] if titles else "")
        if any(t.startswith(q_low) for t in titles) or (q_fold and any(ft.startswith(q_fold) for ft in folded_titles)):
            return (1, titles[0] if titles else "")
        if any(q_low in t for t in titles) or (q_fold and any(q_fold in ft for ft in folded_titles)):
            return (2, titles[0] if titles else "")
        return (3, titles[0] if titles else "")

    items.sort(key=_rank_db_item)
    return kind, items, total_hits


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
    with_tooltips: bool = False,
    with_raw: bool = False,
) -> dict[str, Any]:
    """Search PoB's built-in Unique/Rare item DB (EN or ZH) and optionally try/equip a match."""
    limit = max(1, min(200, int(limit)))
    if slot_name:
        check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    if query:
        check_input(query, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")

    with worker.lock:
        kind, items, total_hits = _search_item_db_items(worker, query=query, db_type=db_type, limit=limit)
        raw_by_idx: dict[int, str] = {}
        indexed_items: list[dict[str, Any]] = []
        for idx, it in enumerate(items[:limit], start=1):
            raw_by_idx[idx] = str(it.get("raw") or "")
            entry = {k: v for k, v in it.items() if (with_raw or k != "raw")}
            entry["index"] = idx
            indexed_items.append(entry)

        if not equip and not apply:
            if with_tooltips:
                tip_targets = [
                    (entry, raw_by_idx.get(entry["index"], ""))
                    for entry in indexed_items[: min(limit, 10)]
                    if raw_by_idx.get(entry["index"], "")
                ]
                if tip_targets:
                    tip_calls = [("item_tooltip", {"raw": raw_txt, "compare": False}) for _, raw_txt in tip_targets]
                    tip_results = _call_batch(worker, tip_calls, raise_on_error=False)
                    for (entry, _), tip in zip(tip_targets, tip_results, strict=False):
                        if not _is_batch_error(tip):
                            entry["tooltipLines"] = tip.get("lines", [])
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
            "selectedItem": chosen if with_raw else {k: v for k, v in chosen.items() if k != "raw"},
            "tooltip": native_tip,
            "mutation": mutation_res,
        }


query_item_db = search_and_try_item_db
