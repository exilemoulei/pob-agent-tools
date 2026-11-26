"""PoB Unique/Rare item database search and what-if equip helper."""

from __future__ import annotations

from typing import Any

from .. import game_data as _game_data
from ..analyzer import resolve_slot_name
from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..guardrails import GuardrailScope, check_input
from ..snapshot import run_mutation
from .common import _call_batch, _fold_no_apos, _fold_search_text, _is_batch_error


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

__all__ = [
    "_search_item_db_items",
    "query_item_db",
    "search_and_try_item_db",
]
