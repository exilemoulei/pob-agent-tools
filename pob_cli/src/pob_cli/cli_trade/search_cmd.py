"""`pob trade search` Click command entry point and result rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from ..cli_common import _emit_json, _emit_text, _output_options
from ..trade_eval import (
    compare_items,
    format_report,
    parse_weights,
    preferred_templates,
    report_json,
    score_deltas,
    sort_candidates,
)
from ..trade_item import parse_item_text, sanitize_text
from ..trade_query import (
    VALID_STATUSES,
    category_for_slot,
    infer_weapon_category_from_base,
    load_local_trade_stats,
    parse_stat_argument,
    parse_sum_group_argument,
    pob_slot_for_slot,
    translate_stat_query,
)
from ..trade_query.auto_query import apply_cli_filters_to_auto_query, finalize_auto_query, wrap_auto_query
from .common import (
    _TARGET_SLOT_TYPE,
    SLOT_HINT,
    _conversion_payload,
    _default_target_slot,
    _err_utf8,
    _prepend_warning,
    _read_current_item,
)
from .other_cmds import trade_group
from .search_exec import (
    _build_candidates_from_rows,
    _execute_trade_api_query,
    _validate_search_inputs,
    _verify_candidates_with_pob,
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
) -> dict[str, Any] | None:
    """Invoke PoB's `trade_generate_query` kernel via daemon when `--auto-query` is requested."""
    import pob_cli.cli_trade as _ct

    slot_name = pob_slot or slot
    if not slot_name:
        return None
    req_params: dict[str, Any] = {"target": target, "slotName": slot_name, "stat": stat}
    if rarity and rarity != "nonunique":
        req_params["rarity"] = rarity
    if allow_corrupted:
        req_params["includeCorrupted"] = True
    if league:
        req_params["league"] = league
    try:
        res = _ct._daemon_call_safe("trade_generate_query", req_params)
    except Exception:
        return None
    if not isinstance(res, dict) or res.get("error"):
        return None
    q_json = res.get("queryJson")
    if isinstance(q_json, str) and q_json.strip():
        try:
            parsed = json.loads(q_json)
            if isinstance(parsed, dict):
                return _enrich_auto_query_result(res, wrap_auto_query(parsed), slot_name=slot_name, rarity=rarity)
        except json.JSONDecodeError:
            return None
    return None


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
        if resolved_category is None and slot and current is not None and current.base_type:
            resolved_category = infer_weapon_category_from_base(current.base_type)

    return current, current_error, update_warning, build_label, resolved_category


def _emit_search_results(
    *,
    as_json: bool,
    ordered: list[Any],
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
    if as_json:
        payload: dict[str, Any] = {
            "league": fetched_ctx["league_name"],
            "slot": slot,
            "category": resolved_category,
            "status": status,
            "currency": currency,
            "statsSource": fetched_ctx["stats_source"],
            "query": fetched_ctx["query"],
            "searchUrl": fetched_ctx["search_url"],
            "total": fetched_ctx["total"],
            "fetched": fetched,
            "maxAgeHours": max_age,
            "droppedOld": dropped_old,
            "staleFallback": stale_fallback,
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
    )
    _emit_text(_prepend_warning(body, update_warning), out_path=out_path)
    if current_error:
        _err_utf8(f"Equipped item could not be read: {current_error}")


@trade_group.command("search")
@click.option("--slot", help=f"Equipment slot ({SLOT_HINT}).")
@click.option("--stat", "stats", multiple=True, help='Required stat, e.g. "maximum Life=80" (repeatable).')
@click.option("--sum", "sum_groups", multiple=True, help='Weighted sum group: "a[*w]|b[*w]>=min" (repeatable).')
@click.option("--want", "wants", multiple=True, help="Desired stat highlighted in comparison (repeatable).")
@click.option("--weight", "weights", multiple=True, help="Score weight key=value (repeatable).")
@click.option("--budget", type=float, help="Maximum listing price.")
@click.option("--currency", type=click.Choice(["chaos_equiv", "chaos", "divine"]), default="chaos_equiv", show_default=True)
@click.option("--status", type=click.Choice(sorted(VALID_STATUSES)), default="available", show_default=True)
@click.option(
    "--max-age",
    type=click.FloatRange(min=0, min_open=True),
    help="Only listings indexed within this many hours (stale listings are usually overpriced), e.g. 15.",
)
@click.option(
    "--bargain",
    type=click.FloatRange(min=0),
    help=(
        "Flag listings at least this many percent below the going price (median of the 10 "
        "cheapest listings of the same search, any age) and list them first; costs one more "
        "search. Pair with --max-age to catch underpriced fresh listings, e.g. 30."
    ),
)
@click.option("--league", help="PoE1 trade league (defaults to current challenge league).")
@click.option("--rarity", type=click.Choice(["nonunique", "rare", "unique", "any"]), default="nonunique", show_default=True)
@click.option("--ilvl-min", type=int, default=0, show_default=True)
@click.option("--category", help="Override official trade category (e.g. weapon.claw).")
@click.option("--no-category", is_flag=True, help="Do not set a category filter.")
@click.option("--allow-conversion", is_flag=True, help="Broaden single-element resist/damage filters for Harvest swap.")
@click.option("--convert-to", type=click.Choice(["fire", "cold", "lightning"]))
@click.option("--allow-corrupted", is_flag=True, help="Do not auto-exclude corrupted/mirrored items.")
@click.option("--search-sort", type=click.Choice(["asc", "desc", "sum"]), default="asc", show_default=True)
@click.option("--sort", "sort_by", type=click.Choice(["score", "price", "pob", "sum"]), default="score", show_default=True)
@click.option("--max-results", type=int, default=8, show_default=True, help="Rows in report.")
@click.option(
    "--fetch-limit", type=int, default=10, show_default=True,
    help="Listings to report (max 20); --max-age may page through up to 50 to find fresh ones.",
)
@click.option("--open-prefix", is_flag=True, help="Require at least one open prefix.")
@click.option("--open-suffix", is_flag=True, help="Require at least one open suffix.")
@click.option("--open-affix", is_flag=True, help="Require at least one open prefix or suffix.")
@click.option("--bench-craft", "bench_crafts", multiple=True, help="Try adding/replacing a bench craft (requires PoB).")
@click.option("--bench-hint/--no-bench-hint", default=True, show_default=True)
@click.option("--pob", "pob_mode", type=click.Choice(["auto", "on", "off"]), default="auto", show_default=True)
@click.option("--pob-limit", type=int, default=5, show_default=True, help="Candidates to PoB-verify.")
@click.option("--auto-query", is_flag=True, help="Generate weighted trade query via PoB TradeQueryGenerator.")
@click.option("--target", type=_TARGET_SLOT_TYPE, default=_default_target_slot)
@click.option("--current-item-file", type=click.Path(exists=True, dir_okay=False))
@click.option("--no-compare", is_flag=True, help="Skip reading the currently equipped item.")
@click.option("--dump-query", is_flag=True, help="Print official trade query JSON offline and exit.")
@click.option("--proxy", help="HTTP/SOCKS5 proxy URL (overrides POB_PROXY).")
@click.option("--json", "as_json", is_flag=True, help="Output RFC-8259 JSON.")
@_output_options
@click.option("--verbose", is_flag=True, help="Log HTTP request paths to stderr (never cookies).")
def cmd_trade_search(
    slot: str | None,
    stats: tuple[str, ...],
    sum_groups: tuple[str, ...],
    wants: tuple[str, ...],
    weights: tuple[str, ...],
    budget: float | None,
    currency: str,
    status: str,
    max_age: float | None,
    bargain: float | None,
    league: str | None,
    rarity: str,
    ilvl_min: int,
    category: str | None,
    no_category: bool,
    allow_conversion: bool,
    convert_to: str | None,
    allow_corrupted: bool,
    search_sort: str,
    sort_by: str,
    max_results: int,
    fetch_limit: int,
    open_prefix: bool,
    open_suffix: bool,
    open_affix: bool,
    bench_crafts: tuple[str, ...],
    bench_hint: bool,
    pob_mode: str,
    pob_limit: int,
    auto_query: bool,
    target: str,
    current_item_file: str | None,
    no_compare: bool,
    dump_query: bool,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
    verbose: bool,
) -> None:
    """Search official PoE1 trade listings, compare against equipped gear, and verify in PoB."""
    ctx = click.get_current_context()
    resolved_sort = _validate_search_inputs(
        ctx, slot=slot, category=category, no_category=no_category, stats=stats,
        sum_groups=sum_groups, wants=wants, bench_crafts=bench_crafts, pob_mode=pob_mode,
        pob_limit=pob_limit, no_compare=no_compare, current_item_file=current_item_file, sort_by=sort_by,
    )
    if resolved_sort is None:
        return
    sort_by = resolved_sort

    try:
        weight_map = parse_weights(weights)
        preferred = preferred_templates([translate_stat_query(w) for w in wants])
        requests = [parse_stat_argument(value) for value in stats]
        weighted_groups = [parse_sum_group_argument(value) for value in sum_groups]
    except ValueError as exc:
        _err_utf8(str(exc))
        ctx.exit(1)
        return

    pob_slot = pob_slot_for_slot(slot or "") if slot else None
    current, current_error, update_warning, build_label, resolved_category = _resolve_current_and_category(
        dump_query=dump_query, current_item_file=current_item_file, no_compare=no_compare,
        slot=slot, pob_slot=pob_slot, target=target, no_category=no_category, category=category,
    )
    auto_res: dict[str, Any] | None = None
    if auto_query:
        auto_res = _try_auto_trade_query(
            pob_slot=pob_slot, slot=slot, target=target, rarity=rarity,
            allow_corrupted=allow_corrupted, league=league,
        )
        if auto_res is not None:
            apply_cli_filters_to_auto_query(
                auto_res["query"], status=status, budget=budget, currency=currency,
                max_age=max_age, ilvl_min=ilvl_min, open_prefix=open_prefix,
                open_suffix=open_suffix, open_affix=open_affix,
                category=resolved_category,
                search_sort=search_sort if ctx.get_parameter_source("search_sort") != click.core.ParameterSource.DEFAULT else None,
            )
            finalize_auto_query(auto_res)
            if dump_query and not stats and not sum_groups:
                _emit_json({"slot": slot, "autoQuery": True, **auto_res}, compact=compact, out_path=out_path, sanitize=True)
                return

    fetched_ctx = _execute_trade_api_query(
        ctx, proxy=proxy, verbose=verbose, league=league, dump_query=dump_query, slot=slot,
        resolved_category=resolved_category, requests=requests, weighted_groups=weighted_groups,
        wants=wants, allow_conversion=allow_conversion, convert_to=convert_to, rarity=rarity,
        ilvl_min=ilvl_min, status=status, currency=currency, budget=budget, max_age=max_age,
        bargain=bargain, search_sort=search_sort, sort_by=sort_by, open_prefix=open_prefix,
        open_suffix=open_suffix, open_affix=open_affix, allow_corrupted=allow_corrupted,
        bench_hint=bench_hint, fetch_limit=fetch_limit, compact=compact, out_path=out_path,
        auto_query_body=auto_res["query"] if isinstance(auto_res, dict) else None,
    )
    if fetched_ctx is None:
        return

    plan, rows = fetched_ctx["plan"], fetched_ctx["rows"]
    candidates = _build_candidates_from_rows(
        rows, plan=plan, current=current, weight_map=weight_map, preferred=preferred,
        search_url=fetched_ctx["search_url"], league_name=fetched_ctx["league_name"],
        query=fetched_ctx["query"], rates=fetched_ctx["rates"], market=fetched_ctx["market"], bargain=bargain,
    )

    pre_sort = "sum" if (sort_by == "sum" or (search_sort == "sum" and sort_by != "price")) else "score"
    candidates = sort_candidates(candidates, pre_sort)
    candidates.sort(key=lambda c: not c.bargain)  # stable: bargains get PoB-verified first

    verification_limit = max(0, pob_limit)
    if bench_crafts and not dump_query:
        from ..trade_craft import expand_bench_candidates

        selected = expand_bench_candidates(candidates[:verification_limit], slot=slot, wants=bench_crafts)
        for candidate in selected:
            if candidate.bench_craft:
                candidate.deltas = compare_items(current, candidate.item, weights=weight_map, preferred=preferred)
                candidate.score = score_deltas(candidate.deltas)
        candidates = selected + candidates[verification_limit:]
        verification_limit = len(selected)

    if not _verify_candidates_with_pob(
        ctx, candidates, pob_mode=pob_mode, pob_slot=pob_slot, slot=slot,
        target=target, verification_limit=verification_limit,
    ):
        return

    ordered = sort_candidates(candidates, sort_by)
    ordered.sort(key=lambda c: not c.bargain)  # stable: bargains first, order otherwise kept
    ordered = ordered[: max(0, max_results)]
    _emit_search_results(
        as_json=as_json, ordered=ordered, fetched_ctx=fetched_ctx, plan=plan, current=current,
        current_error=current_error, update_warning=update_warning, build_label=build_label,
        slot=slot, resolved_category=resolved_category, status=status, budget=budget,
        currency=currency, max_age=max_age, bargain=bargain, compact=compact, out_path=out_path,
    )
