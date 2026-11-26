"""Auto-query enrichment, current-item resolution, and result rendering for `pob trade search`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..cli_common import _emit_json, _emit_text
from ..trade_eval import compute_price_floor_note, format_report, report_json
from ..trade_item import parse_item_text, sanitize_text
from ..trade_query import (
    category_for_slot,
    infer_weapon_category_from_base,
    load_local_trade_stats,
)
from ..trade_query.auto_query import wrap_auto_query
from .common import (
    _conversion_payload,
    _err_utf8,
    _prepend_warning,
    _read_current_item,
)


def _enrich_auto_query_result(
    res: dict[str, Any],
    parsed: dict[str, Any],
    *,
    slot_name: str,
    rarity: str,
) -> dict[str, Any]:
    inner_q = parsed["query"]
    if rarity and rarity != "nonunique" and isinstance(inner_q.get("filters"), dict):
        tf = inner_q["filters"].get("type_filters")
        if isinstance(tf, dict) and isinstance(tf.get("filters"), dict):
            if rarity == "any":
                tf["filters"].pop("rarity", None)
            else:
                tf["filters"]["rarity"] = {"option": rarity}

    raw_labels = res.get("statLabels")
    stat_labels: dict[str, str] = dict(raw_labels) if isinstance(raw_labels, dict) else {}
    all_ids: list[str] = []
    for grp in inner_q.get("stats") or []:
        if isinstance(grp, dict):
            for f in grp.get("filters") or []:
                if isinstance(f, dict) and isinstance(f.get("id"), str):
                    all_ids.append(f["id"])

    if any(fid not in stat_labels for fid in all_ids):
        local_map: dict[str, str] = {}
        try:
            for e in load_local_trade_stats() or []:
                if isinstance(e, dict) and e.get("id") and e.get("text"):
                    local_map[str(e["id"])] = str(e["text"])
        except Exception:
            pass
        for fid in all_ids:
            if fid not in stat_labels:
                stat_labels[fid] = local_map.get(fid, fid)

    raw_top = res.get("topFilters")
    top_filters: list[dict[str, Any]] = []
    if isinstance(raw_top, list) and raw_top:
        for entry in raw_top:
            if isinstance(entry, dict) and entry.get("id"):
                fid = str(entry["id"])
                top_filters.append(
                    {
                        **entry,
                        "label": str(entry.get("label") or stat_labels.get(fid, fid)),
                    }
                )
    else:
        first_grp = (inner_q.get("stats") or [None])[0]
        w_filters = list(first_grp.get("filters") or []) if isinstance(first_grp, dict) else []
        w_filters.sort(
            key=lambda f: abs(float((f.get("value") or {}).get("weight") or 0.0)) if isinstance(f, dict) else 0.0,
            reverse=True,
        )
        for f in w_filters[:15]:
            if isinstance(f, dict) and f.get("id"):
                fid = str(f["id"])
                wt = float((f.get("value") or {}).get("weight") or 0.0)
                top_filters.append({"id": fid, "label": stat_labels.get(fid, fid), "weight": wt})

    raw_sc = res.get("slotConstraints")
    if isinstance(raw_sc, dict):
        slot_constraints: dict[str, Any] = {
            **raw_sc,
            "loadBearingAttrs": dict(raw_sc.get("loadBearingAttrs") or {"Str": 0, "Dex": 0, "Int": 0}),
            "attrDeficits": dict(raw_sc.get("attrDeficits") or {}),
            "resistDeficits": dict(raw_sc.get("resistDeficits") or {}),
        }
    else:
        slot_constraints = {
            "slot": slot_name,
            "isLoadBearing": False,
            "loadBearingAttrs": {"Str": 0, "Dex": 0, "Int": 0},
            "attrDeficits": {},
            "resistDeficits": {},
            "brickedCount": 0,
            "bricked": [],
        }

    out: dict[str, Any] = {
        "query": parsed,
        "filters": int(res.get("filters") or 0),
        "complexity": int(res.get("complexity") or 0),
        "stat": res.get("stat"),
        "statLabels": stat_labels,
        "topFilters": top_filters,
        "slotConstraints": slot_constraints,
    }
    if res.get("minWeight") is not None:
        out["minWeight"] = res["minWeight"]
    return out


def _try_auto_trade_query(
    *,
    pob_slot: str | None,
    slot: str | None,
    target: str,
    stat: str | None = None,
    rarity: str = "nonunique",
    allow_corrupted: bool = False,
    league: str | None = None,
    problems: list[str] | None = None,
) -> dict[str, Any] | None:
    """Invoke PoB's `trade_generate_query` kernel via daemon when `--auto-query` is requested.

    A reason goes into `problems` when no query comes back, so the search that runs without it says why.
    """
    import pob_cli.cli_trade as _ct

    def _no(reason: str) -> None:
        if problems is not None:
            problems.append(reason)

    slot_name = pob_slot or slot
    if not slot_name:
        return _no("it needs --slot")
    req_params: dict[str, Any] = {"target": target, "slotName": slot_name, "stat": stat}
    if rarity and rarity != "nonunique":
        req_params["rarity"] = rarity
    if allow_corrupted:
        req_params["includeCorrupted"] = True
    if league:
        req_params["league"] = league
    try:
        res = _ct._daemon_call_safe("trade_generate_query", req_params)
    except Exception as exc:
        return _no(str(exc).strip() or type(exc).__name__)
    if not isinstance(res, dict):
        return _no("PoB returned no query")
    if res.get("error"):
        return _no(str(res.get("error")))
    q_json = res.get("queryJson")
    if isinstance(q_json, str) and q_json.strip():
        try:
            parsed = json.loads(q_json)
            if isinstance(parsed, dict):
                return _enrich_auto_query_result(res, wrap_auto_query(parsed), slot_name=slot_name, rarity=rarity)
        except json.JSONDecodeError:
            pass
    return _no("PoB returned no query")


def _resolve_current_and_category(
    *,
    dump_query: bool,
    current_item_file: str | None,
    no_compare: bool,
    slot: str | None,
    pob_slot: str | None,
    target: str,
    no_category: bool,
    category: str | None,
) -> tuple[Any, str | None, str | None, str, str | None]:
    current = None
    current_error: str | None = None
    update_warning: str | None = None
    build_label = f"slot:{target}"

    if not dump_query:
        if current_item_file:
            try:
                current = parse_item_text(Path(current_item_file).read_text(encoding="utf-8", errors="replace"))
            except ValueError as exc:
                current_error = sanitize_text(exc, 300)
        elif not no_compare:
            if pob_slot:
                current, current_error, update_warning, item_label = _read_current_item(pob_slot, target)
                if item_label:
                    build_label = item_label
            elif slot:
                current_error = f"no PoB slot mapping for {slot!r}"

    resolved_category: str | None = None
    if not no_category:
        resolved_category = category or (category_for_slot(slot) if slot else None)
        if slot and current is not None and current.base_type:
            if resolved_category is None:
                resolved_category = infer_weapon_category_from_base(current.base_type)
            elif category is None and resolved_category == "jewel" and "cluster jewel" in current.base_type.lower():
                resolved_category = "jewel.cluster"

    return current, current_error, update_warning, build_label, resolved_category


def _emit_search_results(
    *,
    as_json: bool,
    ordered: list[Any],
    all_candidates: list[Any] | None = None,
    fetched_ctx: dict[str, Any],
    plan: Any,
    current: Any,
    current_error: str | None,
    update_warning: str | None,
    build_label: str,
    slot: str | None,
    resolved_category: str | None,
    status: str,
    budget: float | None,
    currency: str,
    max_age: float | None,
    bargain: float | None,
    seller_capped_dropped: int = 0,
    max_per_seller: int | None = None,
    compact: bool,
    out_path: str | None,
) -> None:
    conversion_note = "; ".join(
        f"harvest {'/'.join(cp.elements)} -> {cp.target} "
        f"(cost: {cp.cost}; {cp.patch} rules; tier kept, roll re-rolled)"
        for cp in plan.conversions
    )
    fetched = fetched_ctx["fetched"]
    dropped_old = fetched_ctx["dropped_old"]
    stale_fallback = fetched_ctx["stale_fallback"]
    market = fetched_ctx["market"]
    floor_pool = all_candidates if all_candidates is not None else ordered
    price_floor_note = compute_price_floor_note(floor_pool, status=status)
    eff_realm = fetched_ctx.get("realm") or "global"
    rates_source = fetched_ctx.get("rates_source") or (
        "tw_static_fallback" if eff_realm == "tw" else "poe_ninja"
    )
    if as_json:
        payload: dict[str, Any] = {
            "realm": eff_realm,
            "league": fetched_ctx["league_name"],
            "slot": slot,
            "category": resolved_category,
            "status": status,
            "currency": currency,
            "statsSource": fetched_ctx["stats_source"],
            "ratesSource": rates_source,
            "query": fetched_ctx["query"],
            "searchUrl": fetched_ctx["search_url"],
            "total": fetched_ctx["total"],
            "fetched": fetched,
            "maxAgeHours": max_age,
            "droppedOld": dropped_old,
            "staleFallback": stale_fallback,
            "sellerCappedDropped": seller_capped_dropped,
            "bargainPct": bargain,
            "marketChaos": market,
            "conversion": _conversion_payload(plan),
            "openAffixes": fetched_ctx["open_flags"],
            "benchOptions": fetched_ctx["bench_options"],
            "current": {
                "found": current is not None,
                "error": current_error,
                "name": current.display_name if current else None,
            },
            "candidates": report_json(ordered),
        }
        if price_floor_note:
            payload["priceFloorNote"] = price_floor_note
        if update_warning:
            payload["updateWarning"] = update_warning
        _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
        return

    body = format_report(
        build_label=build_label,
        slot=slot or resolved_category or "",
        current=current,
        candidates=ordered,
        league=fetched_ctx["league_name"],
        status=status,
        listed_note=(
            ""
            if max_age is None
            else f"NOTHING within {max_age:g}h - showing older listings instead"
            if stale_fallback
            else f"within {max_age:g}h ({dropped_old} older or undated dropped)"
        ),
        budget=budget,
        currency=currency,
        search_url=fetched_ctx["search_url"],
        total=fetched_ctx["total"],
        fetched=fetched,
        conversion_note=conversion_note,
        open_affixes=fetched_ctx["open_flags"],
        bench_options=fetched_ctx["bench_options"],
        market_note=fetched_ctx["market_note"],
        price_floor_note=price_floor_note or "",
        seller_capped_dropped=seller_capped_dropped,
        max_per_seller=max_per_seller,
    )
    if eff_realm == "tw":
        div_rate = (fetched_ctx.get("rates") or {}).get("divine", 150.0)
        if rates_source == "tw_env_override":
            tw_note = (
                f"Note: TW chaos-equivalent uses POB_TW_DIVINE_CHAOS override (1d={div_rate:g}c) "
                "since poe.ninja does not index pathofexile.tw."
            )
        else:
            tw_note = (
                f"Note: TW chaos-equivalent uses static fallback rates (1d={div_rate:g}c; "
                "override via POB_TW_DIVINE_CHAOS) since poe.ninja does not index pathofexile.tw."
            )
        body = f"{body.rstrip()}\n\n{tw_note}"
    _emit_text(_prepend_warning(body, update_warning), out_path=out_path)
    if current_error:
        _err_utf8(f"Equipped item could not be read: {current_error}")
