"""`pob trade search` Click command entry point."""

from __future__ import annotations

from typing import Any

import click

from ..cli_common import _emit_json, _output_options
from ..trade_eval import (
    compare_items,
    dedupe_candidates_with_stats,
    parse_weights,
    preferred_templates,
    score_deltas,
    sort_candidates,
)
from ..trade_query import (
    VALID_STATUSES,
    parse_stat_argument,
    parse_sum_group_argument,
    pob_slot_for_slot,
    translate_stat_query,
)
from ..trade_query.auto_query import apply_cli_filters_to_auto_query, finalize_auto_query
from .common import (
    _TARGET_SLOT_TYPE,
    SLOT_HINT,
    _default_target_slot,
    _err_utf8,
)
from .other_cmds import trade_group
from .search_exec import (
    _build_candidates_from_rows,
    _execute_trade_api_query,
    _validate_search_inputs,
    _verify_candidates_with_pob,
)
from .search_render import (
    _emit_search_results,
    _enrich_auto_query_result,
    _resolve_current_and_category,
    _try_auto_trade_query,
)

__all__ = [
    "_emit_search_results",
    "_enrich_auto_query_result",
    "_resolve_current_and_category",
    "_try_auto_trade_query",
    "cmd_trade_search",
]


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
    "--securable",
    is_flag=True,
    help="Only Faustus Merchant's Tab instant Gold buyout listings (shortcut for --status securable).",
)
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
    "--max-per-seller",
    type=int,
    default=2,
    show_default=True,
    help="Max listings retained per seller account before PoB verification (0 = unlimited).",
)
@click.option(
    "--fetch-limit", type=int, default=10, show_default=True,
    help="Listings to report (max 100); --max-age may page through up to 100 to find fresh ones.",
)
@click.option("--open-prefix", is_flag=True, help="Require at least one open prefix.")
@click.option("--open-suffix", is_flag=True, help="Require at least one open suffix.")
@click.option("--open-affix", is_flag=True, help="Require at least one open prefix or suffix.")
@click.option("--bench-craft", "bench_crafts", multiple=True, help="Try adding/replacing a bench craft (requires PoB).")
@click.option("--bench-hint/--no-bench-hint", default=True, show_default=True)
@click.option("--pob", "pob_mode", type=click.Choice(["auto", "on", "off"]), default="auto", show_default=True)
@click.option("--pob-limit", type=int, default=5, show_default=True, help="Candidates to PoB-verify.")
@click.option(
    "--surrogate-k",
    "surrogate_k",
    type=click.IntRange(min=0),
    default=None,
    help="Rank the fetched listings with a linear surrogate (0 disables) and PoB-eval the top --pob-limit; unless --fetch-limit is given, fetches max(50, K), at most 100.",
)
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
    securable: bool,
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
    max_per_seller: int,
    fetch_limit: int,
    open_prefix: bool,
    open_suffix: bool,
    open_affix: bool,
    bench_crafts: tuple[str, ...],
    bench_hint: bool,
    pob_mode: str,
    pob_limit: int,
    surrogate_k: int | None,
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
    validated = _validate_search_inputs(
        ctx, slot=slot, category=category, no_category=no_category, stats=stats,
        sum_groups=sum_groups, wants=wants, bench_crafts=bench_crafts, pob_mode=pob_mode,
        pob_limit=pob_limit, no_compare=no_compare, current_item_file=current_item_file,
        sort_by=sort_by, status=status, securable=securable, max_per_seller=max_per_seller,
    )
    if validated is None:
        return
    sort_by, status = validated

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
        auto_problems: list[str] = []
        auto_res = _try_auto_trade_query(
            pob_slot=pob_slot, slot=slot, target=target, rarity=rarity,
            allow_corrupted=allow_corrupted, league=league, problems=auto_problems,
        )
        if auto_res is None:
            # The search still runs on the filters given; saying nothing would pass it off as PoB's query.
            _err_utf8(f"Warning: --auto-query gave no query ({'; '.join(auto_problems) or 'unknown reason'}); searching without it.")
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

    eff_prefilter_k = surrogate_k if (surrogate_k is not None and surrogate_k > 0) else None
    eff_fetch_limit = fetch_limit
    if eff_prefilter_k is not None and ctx.get_parameter_source("fetch_limit") == click.core.ParameterSource.DEFAULT:
        eff_fetch_limit = max(fetch_limit, 50, eff_prefilter_k)

    fetched_ctx = _execute_trade_api_query(
        ctx, proxy=proxy, verbose=verbose, league=league, dump_query=dump_query, slot=slot,
        resolved_category=resolved_category, requests=requests, weighted_groups=weighted_groups,
        wants=wants, allow_conversion=allow_conversion, convert_to=convert_to, rarity=rarity,
        ilvl_min=ilvl_min, status=status, currency=currency, budget=budget, max_age=max_age,
        bargain=bargain, search_sort=search_sort, sort_by=sort_by, open_prefix=open_prefix,
        open_suffix=open_suffix, open_affix=open_affix, allow_corrupted=allow_corrupted,
        bench_hint=bench_hint, fetch_limit=eff_fetch_limit, compact=compact, out_path=out_path,
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
    candidates, seller_capped_dropped = dedupe_candidates_with_stats(candidates, max_per_seller=max_per_seller)

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
        target=target, verification_limit=verification_limit, prefilter_k=eff_prefilter_k,
    ):
        return

    eff_sort_by = sort_by
    if (
        eff_prefilter_k is not None
        and ctx.get_parameter_source("sort_by") == click.core.ParameterSource.DEFAULT
        and any(c.pob is not None for c in candidates)
    ):
        eff_sort_by = "pob"

    ordered = sort_candidates(candidates, eff_sort_by)
    ordered.sort(key=lambda c: not c.bargain)  # stable: bargains first, order otherwise kept
    ordered = ordered[: max(0, max_results)]
    _emit_search_results(
        as_json=as_json, ordered=ordered, all_candidates=candidates, fetched_ctx=fetched_ctx,
        plan=plan, current=current, current_error=current_error, update_warning=update_warning,
        build_label=build_label, slot=slot, resolved_category=resolved_category, status=status,
        budget=budget, currency=currency, max_age=max_age, bargain=bargain,
        seller_capped_dropped=seller_capped_dropped, max_per_seller=max_per_seller,
        compact=compact, out_path=out_path,
    )
