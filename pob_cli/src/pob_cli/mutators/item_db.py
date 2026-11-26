"""PoB Unique/Rare item database search, slot/type filtering, and PoB-engine stat sorting."""

from __future__ import annotations

from typing import Any

from .. import game_data as _game_data
from ..analyzer import resolve_slot_name
from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..guardrails import GuardrailScope, check_input
from ..snapshot import run_mutation
from .common import _call_batch, _fold_no_apos, _fold_search_text, _is_batch_error
from .item_db_resolve import (
    _GENERIC_MULTI_SLOT_TOKENS,
    DB_STAT_SORT_MAX,
    ITEM_DB_TEMPLATE_NOTE,
    _compact_active_variants_raw,
    _compact_key,
    _compute_power_delta,
    _extract_baseline_for_sort,
    _fetch_item_db_options,
    _json_safe_metric,
    _resolve_db_rpc_plan,
    _stat_sort_too_many_error,
)

_FLASK_SLOT_NOTE = (
    "注：药剂在 PoB 中按激活该药剂（toggleFlask）试算；穿戴替换具体药剂槽请结合 --equip 复核"
)


def _resolve_stat_sort_target_slot(
    worker: BridgeWorker,
    *,
    slot_name: str | None,
    filters: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Pick exact `targetSlot` for PoB stat-sorting so multi-slot categories evaluate one concrete slot."""
    raw_ck = _compact_key(slot_name) if slot_name else ""
    db_slot = str(filters.get("slot") or "")

    if slot_name and raw_ck not in _GENERIC_MULTI_SLOT_TOKENS:
        try:
            resolved = resolve_slot_name(worker, slot_name)
        except Exception:
            resolved = None
        if resolved:
            filters["targetSlot"] = resolved
            note = _FLASK_SLOT_NOTE if resolved.startswith("Flask ") else None
            return resolved, note

    if db_slot == "Jewel":
        try:
            items_res, tree_state = _call_batch(worker, [("list_items", {}), ("get_tree_state", {})])
            slots = items_res.get("slots", []) if isinstance(items_res, dict) else []
            alloc_set = {int(x) for x in (tree_state.get("allocatedNodes", []) if isinstance(tree_state, dict) else [])}
            alloc_jewels = [s for s in slots if s.get("nodeId") and int(s["nodeId"]) in alloc_set]
            target = (
                next((str(s["name"]) for s in alloc_jewels if not s.get("selItemId")), None)
                or (str(alloc_jewels[0]["name"]) if alloc_jewels else None)
                or next((str(s["name"]) for s in slots if str(s.get("name") or "").startswith("Jewel ") and s.get("shown", True)), None)
            )
            if target:
                filters["targetSlot"] = target
                note = f'注：珠宝默认按首个已点亮珠宝孔 `{target}` 试算以避免全树多孔超时；指定孔位请传 `--slot "#2"`'
                return target, note
        except Exception:
            pass

    if db_slot == "Flask":
        try:
            items_res = worker.call("list_items", {})
            slots = items_res.get("slots", []) if isinstance(items_res, dict) else []
            names = [str(s.get("name") or "") for s in slots if s.get("shown", True)]
            target = "Flask 1" if "Flask 1" in names else next((n for n in names if n.startswith("Flask ")), None)
            if target:
                filters["targetSlot"] = target
                return target, _FLASK_SLOT_NOTE
        except Exception:
            pass

    return None, None


def list_item_db_sorts(worker: BridgeWorker, *, db_type: str = "UNIQUE") -> dict[str, Any]:
    """List all PoB `ItemDBControl` sort modes and filter options."""
    kind = "rare" if str(db_type or "UNIQUE").strip().lower() == "rare" else "unique"
    if kind != "unique":
        raise ValueError("--list-sorts is only supported for UNIQUE database (`--type UNIQUE`).")
    with worker.lock:
        opts = _fetch_item_db_options(worker, "unique")
    if not opts:
        raise ValueError("This PoB engine does not expose `item_db_options`.")
    sorts = [
        {
            "sortMode": str(e.get("sortMode") or ""),
            "stat": e.get("stat"),
            "label": strip_pob_escapes(str(e.get("label") or "")),
            "labelZh": strip_pob_escapes(str(e.get("labelZh") or "")),
        }
        for e in (opts.get("sort") or [])
    ]
    return {
        "dbType": "UNIQUE",
        "listSorts": True,
        "sorts": sorts,
        "slots": [strip_pob_escapes(str(e.get("label") or "")) for e in (opts.get("slot") or [])],
        "obtainable": [strip_pob_escapes(str(e.get("label") or "")) for e in (opts.get("obtainable") or [])],
        "requirement": [strip_pob_escapes(str(e.get("label") or "")) for e in (opts.get("requirement") or [])],
        "searchMode": [strip_pob_escapes(str(e.get("label") or "")) for e in (opts.get("searchMode") or [])],
    }


def _query_db_core(
    worker: BridgeWorker,
    *,
    query: str = "",
    db_type: str = "UNIQUE",
    limit: int = 15,
    page: int = 1,
    min_fetch: int = 0,
    slot_name: str | None = None,
    item_type: str | None = None,
    league: str | None = None,
    requirement: str | None = None,
    obtainable: str | None = None,
    search_mode: str | None = None,
    sort_mode: str | None = None,
) -> tuple[str, list[dict[str, Any]], int, dict[str, Any], str, str, str | None]:
    """Execute stateless PoB `item_db` query with filters, targetSlot, stat-sort guard, and ZH/NFKD fallbacks."""
    q = (query or "").strip()
    if q:
        check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    if slot_name:
        check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    if item_type:
        check_input(item_type, scope=GuardrailScope.CATEGORY, expected_game="poe1")

    kind = "rare" if str(db_type or "UNIQUE").strip().lower() == "rare" else "unique"
    limit = max(1, min(200, int(limit)))
    page = max(1, int(page))
    min_fetch = max(0, int(min_fetch))

    base_rpc, filters, resolved_sort, sort_label = _resolve_db_rpc_plan(
        worker,
        kind=kind,
        slot_name=slot_name,
        item_type=item_type,
        league=league,
        requirement=requirement,
        obtainable=obtainable,
        search_mode=search_mode,
        sort_mode=sort_mode,
    )
    stat_sort = resolved_sort.upper() != "NAME"
    if not q and not slot_name and not item_type and not league:
        if stat_sort:
            raise _stat_sort_too_many_error(resolved_sort)
        raise ValueError("Item DB search requires a QUERY, --slot, --item-type, or --league filter.")
    if stat_sort and not q and not slot_name and not item_type:
        raise _stat_sort_too_many_error(resolved_sort)

    slot_note: str | None = None
    if stat_sort:
        target_slot, slot_note = _resolve_stat_sort_target_slot(worker, slot_name=slot_name, filters=filters)
        if target_slot:
            base_rpc["targetSlot"] = target_slot

    rpc_timeout = 90.0 if stat_sort else 30.0
    page_size = min(200, max(limit, min_fetch, 50)) if page == 1 else min(200, limit)
    res = worker.call("item_db", {**base_rpc, "query": q, "size": page_size, "page": page}, timeout=rpc_timeout)
    if res.get("tooMany"):
        raise _stat_sort_too_many_error(
            resolved_sort,
            total=int(res.get("total", 0)),
            max_n=int(res.get("max", DB_STAT_SORT_MAX)),
        )
    items = list(res.get("items", []))
    total_hits = int(res.get("total", len(items)))

    if q and not items:
        q_en = _game_data.english_for_zh(q, pob_dir=getattr(worker, "pob_root", None))
        if q_en:
            res = worker.call(
                "item_db",
                {**base_rpc, "query": q_en, "size": page_size, "page": page},
                timeout=rpc_timeout,
            )
            if res.get("tooMany"):
                raise _stat_sort_too_many_error(
                    resolved_sort,
                    total=int(res.get("total", 0)),
                    max_n=int(res.get("max", DB_STAT_SORT_MAX)),
                )
            items = list(res.get("items", []))
            total_hits = int(res.get("total", len(items)))
            if items:
                q = q_en

    if q and not items and not stat_sort and page == 1:
        q_low = q.lower()
        q_fold = _fold_search_text(q)
        q_no_apos = _fold_no_apos(q)
        fallback_hits: list[dict[str, Any]] = []
        scan_page = 1
        while scan_page <= 12:
            page_res = worker.call("item_db", {**base_rpc, "query": "", "size": 200, "page": scan_page}, timeout=30.0)
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
            if len(page_items) < 200 or len(fallback_hits) >= max(limit, min_fetch, 50):
                break
            scan_page += 1
        items = fallback_hits
        total_hits = len(fallback_hits)

    # Preserve PoB's descending `measuredPower` order whenever stat sorting is active.
    if not stat_sort and q:
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

    return kind, items, total_hits, filters, resolved_sort, sort_label, slot_note


def _search_item_db_items(
    worker: BridgeWorker,
    *,
    query: str = "",
    db_type: str = "UNIQUE",
    limit: int = 15,
    page: int = 1,
    min_fetch: int = 0,
    slot_name: str | None = None,
    item_type: str | None = None,
    league: str | None = None,
    requirement: str | None = None,
    obtainable: str | None = None,
    search_mode: str | None = None,
    sort_mode: str | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    """Backward-compatible wrapper returning `(kind, items, total_hits)`."""
    kind, items, total_hits, _, _, _, _ = _query_db_core(
        worker,
        query=query,
        db_type=db_type,
        limit=limit,
        page=page,
        min_fetch=min_fetch,
        slot_name=slot_name,
        item_type=item_type,
        league=league,
        requirement=requirement,
        obtainable=obtainable,
        search_mode=search_mode,
        sort_mode=sort_mode,
    )
    return kind, items, total_hits


def search_and_try_item_db(
    worker: BridgeWorker,
    *,
    query: str = "",
    db_type: str = "UNIQUE",
    slot_name: str | None = None,
    item_type: str | None = None,
    league: str | None = None,
    requirement: str | None = None,
    obtainable: str | None = None,
    search_mode: str | None = None,
    sort_mode: str | None = None,
    item_index: int | None = None,
    equip: bool = False,
    apply: bool = False,
    limit: int = 15,
    page: int = 1,
    with_tooltips: bool = False,
    with_raw: bool = False,
) -> dict[str, Any]:
    """Search/filter/stat-sort PoB's built-in Unique/Rare item DB and optionally try/equip a match."""
    limit = max(1, min(200, int(limit)))
    page = max(1, int(page))
    if slot_name:
        check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    if query:
        check_input(query, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    if item_type:
        check_input(item_type, scope=GuardrailScope.CATEGORY, expected_game="poe1")

    with worker.lock:
        # Backward compat for legacy 2.67.2 / minimal fakes lacking `item_db_options`:
        # `pob item db "<query>" --slot S --equip` treats `--slot` as equip-target only when no DB options exist.
        db_slot_name = slot_name
        if (
            slot_name
            and (equip or apply)
            and query.strip()
            and not any((item_type, league, requirement, obtainable, search_mode, sort_mode))
            and _fetch_item_db_options(worker, "rare" if str(db_type or "UNIQUE").strip().lower() == "rare" else "unique") is None
        ):
            db_slot_name = None

        kind, items, total_hits, filters, resolved_sort, sort_label, slot_note = _query_db_core(
            worker,
            query=query,
            db_type=db_type,
            limit=limit,
            page=page,
            min_fetch=item_index or 0,
            slot_name=db_slot_name,
            item_type=item_type,
            league=league,
            requirement=requirement,
            obtainable=obtainable,
            search_mode=search_mode,
            sort_mode=sort_mode,
        )
        stat_sort = resolved_sort.upper() != "NAME"
        baseline_val: float | None = None
        if stat_sort:
            try:
                stats_res = worker.call("get_stats", {})
                baseline_val = _extract_baseline_for_sort(stats_res.get("stats") or {}, resolved_sort)
            except Exception:
                baseline_val = None

        raw_by_idx: dict[int, str] = {}
        indexed_items: list[dict[str, Any]] = []
        for idx, it in enumerate(items[:limit], start=1):
            raw_by_idx[idx] = str(it.get("raw") or "")
            entry = {k: v for k, v in it.items() if (with_raw or k != "raw")}
            entry["index"] = idx
            if stat_sort:
                mp_safe, delta, delta_pct = _compute_power_delta(it.get("measuredPower"), baseline_val)
                entry["measuredPower"] = mp_safe
                entry["delta"] = delta
                entry["deltaPct"] = delta_pct
            indexed_items.append(entry)

        hint: str | None = None
        if query.strip() and total_hits == 0 and kind == "unique" and filters.get("obtainable") == "Obtainable":
            hint = (
                "No obtainable uniques matched. Retry with `--obtainable any` to include retired/legacy uniques "
                "（未找到可获取暗金，可加 `--obtainable any` 检索绝版/历史暗金）."
            )

        base_payload: dict[str, Any] = {
            "query": query,
            "dbType": kind.upper(),
            "total": total_hits,
            "page": page,
            "sortMode": resolved_sort,
            "sortLabel": sort_label,
            "statSort": stat_sort,
            "baselineValue": _json_safe_metric(baseline_val) if stat_sort else None,
            "filters": filters,
            "templateNote": ITEM_DB_TEMPLATE_NOTE if stat_sort else None,
            "matches": indexed_items,
        }
        if slot_note:
            base_payload["slotNote"] = slot_note
        if hint:
            base_payload["hint"] = hint

        if not equip and not apply:
            if with_tooltips:
                tip_targets = [
                    (entry, _compact_active_variants_raw(raw_by_idx.get(entry["index"], "")))
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
                **base_payload,
                "equipped": False,
                "applied": False,
                "selectedItem": None,
                "tooltip": None,
                "mutation": None,
            }

        if not items:
            label = f"query '{query}'" if query.strip() else f"filters {filters}"
            raise ValueError(f"No {kind} database items matched {label}.")

        if item_index is not None:
            if item_index < 1 or item_index > len(items):
                raise ValueError(f"item_index {item_index} out of range (1..{len(items)}).")
            chosen = {**items[item_index - 1], "index": item_index}
        else:
            chosen = {**items[0], "index": 1}

        if stat_sort:
            mp_safe, delta, delta_pct = _compute_power_delta(chosen.get("measuredPower"), baseline_val)
            chosen["measuredPower"] = mp_safe
            chosen["delta"] = delta
            chosen["deltaPct"] = delta_pct

        chosen_raw = _compact_active_variants_raw(str(chosen.get("raw") or "").strip())
        if not chosen_raw:
            raise RuntimeError(f"Selected DB item '{chosen.get('title') or chosen.get('name')}' has no raw text.")

        is_generic_slot = bool(slot_name and _compact_key(slot_name) in _GENERIC_MULTI_SLOT_TOKENS)
        target_slot_val = str(filters.get("targetSlot") or "")
        if target_slot_val and not (is_generic_slot and target_slot_val.startswith("Flask ")):
            resolved_slot: str | None = target_slot_val
        elif slot_name and not is_generic_slot:
            resolved_slot = resolve_slot_name(worker, slot_name)
        else:
            resolved_slot = None
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
            **base_payload,
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
    "DB_STAT_SORT_MAX",
    "ITEM_DB_TEMPLATE_NOTE",
    "_search_item_db_items",
    "list_item_db_sorts",
    "query_item_db",
    "search_and_try_item_db",
]
