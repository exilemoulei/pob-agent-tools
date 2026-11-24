"""Validation, API execution, candidate building, and PoB verification for `pob trade search`."""

from __future__ import annotations

import re
from typing import Any

import click

from .. import bench as bench_mod
from ..cli_common import _emit_json
from ..errors import BootstrapMissingError, CrossGameHallucinationError
from ..guardrails import GuardrailScope, check_input, validate_url
from ..trade_client import TradeError, seller_url
from ..trade_eval import Candidate, compare_items, score_deltas, verify_candidates_batch
from ..trade_item import build_item_text, convert_item_text, parse_item_text, sanitize_text, trade_mod_tiers
from ..trade_query import (
    INDEXED_WINDOWS,
    OPEN_AFFIX_ID,
    OPEN_PREFIX_ID,
    OPEN_SUFFIX_ID,
    build_query,
    plan_stats,
)
from ..updater import format_stderr_update_alert
from .common import (
    _conversion_payload,
    _err_utf8,
    _is_known_slot,
    _price_of,
    _trade_error,
    _weighted_sum_of,
)
from .fresh_bargain import _fetch_with_fresh_and_market, _price_chaos


def _validate_search_inputs(
    ctx: click.Context,
    *,
    slot: str | None,
    category: str | None,
    no_category: bool,
    stats: tuple[str, ...],
    sum_groups: tuple[str, ...],
    wants: tuple[str, ...],
    bench_crafts: tuple[str, ...],
    pob_mode: str,
    pob_limit: int,
    no_compare: bool,
    current_item_file: str | None,
    sort_by: str,
) -> str | None:
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    if category:
        check_input(category, scope=GuardrailScope.CATEGORY, expected_game="poe1")
    for s_val in stats:
        validate_url(s_val, expected_game="poe1", cli_subcommand="trade search")
        check_input(s_val, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    for sg_val in sum_groups:
        validate_url(sg_val, expected_game="poe1", cli_subcommand="trade search")
        sg_body = re.split(r"(?:>=|<=|=)", sg_val, maxsplit=1)[0]
        for part in sg_body.split("|"):
            if part.strip():
                check_input(part.strip(), scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    for w_val in wants:
        validate_url(w_val, expected_game="poe1", cli_subcommand="trade search")
        check_input(w_val, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    for bc_val in bench_crafts:
        validate_url(bc_val, expected_game="poe1", cli_subcommand="trade search")
        check_input(bc_val, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    if bench_crafts and (pob_mode == "off" or not slot or no_compare or current_item_file):
        raise click.UsageError("--bench-craft requires --slot, PoB verification and the loaded build comparison.")
    if bench_crafts and pob_limit <= 0:
        raise click.UsageError("--bench-craft requires --pob-limit greater than zero.")
    if bench_crafts and ctx.get_parameter_source("sort_by") == click.core.ParameterSource.DEFAULT:
        sort_by = "pob"
    if not slot and not category and not no_category:
        _err_utf8("Provide --slot (or --category / --no-category).")
        ctx.exit(1)
        return None
    if slot and not _is_known_slot(slot) and not category and not no_category:
        _err_utf8(f"Unknown slot {slot!r}; pass a known --slot, --category, or --no-category.")
        ctx.exit(1)
        return None
    return sort_by


def _build_candidates_from_rows(
    rows: list[dict[str, Any]],
    *,
    plan: Any,
    current: Any,
    weight_map: dict[str, float],
    preferred: set[str],
    search_url: str,
    league_name: str,
    query: dict[str, Any],
    rates: dict[str, float],
    market: float | None,
    bargain: float | None,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for row in rows:
        raw_item = row.get("item") or {}
        try:
            text = build_item_text(raw_item)
        except ValueError:
            continue

        conversions = []
        conversion_warnings: list[str] = []
        if plan.conversions:
            tiers = trade_mod_tiers(raw_item)
            for conv_plan in plan.conversions:
                converted = convert_item_text(text, conv_plan.target, group=conv_plan.group, tiers=tiers)
                text = converted.text
                conversions.extend(converted.conversions)
                conversion_warnings.extend(converted.warnings)

        try:
            item = parse_item_text(text)
        except ValueError:
            continue

        deltas = compare_items(current, item, weights=weight_map, preferred=preferred)
        amount, price_currency = _price_of(row)
        candidate = Candidate(
            raw=row,
            item=item,
            item_text=text,
            price_amount=amount,
            price_currency=price_currency,
            score=score_deltas(deltas),
            deltas=deltas,
            conversions=conversions,
            conversion_warnings=conversion_warnings,
            url=search_url,
            listing_id=row.get("id"),
            weighted_sum=_weighted_sum_of(raw_item),
            price_chaos=_price_chaos(row, rates),
        )
        if candidate.seller:
            # One seller rarely has two matching items, so this link lands on the item itself.
            candidate.url = seller_url(league_name, query["query"], candidate.seller)
        if market and candidate.price_chaos is not None and bargain is not None:
            candidate.vs_market_pct = (candidate.price_chaos / market - 1) * 100
            candidate.bargain = candidate.vs_market_pct <= -bargain + 1e-9  # float slack at the threshold
        candidates.append(candidate)
    return candidates


def _verify_candidates_with_pob(
    ctx: click.Context,
    candidates: list[Candidate],
    *,
    pob_mode: str,
    pob_slot: str | None,
    slot: str | None,
    target: str,
    verification_limit: int,
) -> bool:
    import pob_cli.cli_trade as _ct

    if pob_mode == "off" or not candidates:
        return True
    if not pob_slot:
        if pob_mode == "on":
            _err_utf8(f"PoB verification needs a supported --slot, got {slot!r}.")
            ctx.exit(6)
            return False
        _err_utf8("PoB verification skipped: no PoB slot mapping for this search.")
        return True

    try:
        verify_candidates_batch(
            candidates,
            slot=pob_slot,
            target=target,
            limit=verification_limit,
            daemon_call=_ct._daemon_call_safe,
        )
    except CrossGameHallucinationError:
        raise
    except BootstrapMissingError as exc:
        alert = format_stderr_update_alert(exc.update_status) or str(exc)
        _err_utf8(alert)
        if pob_mode == "on":
            ctx.exit(6)
            return False
    except Exception as exc:
        err_text = sanitize_text(exc, 300)
        for c in candidates[:verification_limit]:
            if c.pob is None and not c.pob_error:
                c.pob_error = err_text
        if pob_mode == "on":
            _err_utf8(f"PoB verification failed: {err_text}")
            ctx.exit(6)
            return False

    checked = candidates[:verification_limit]
    if pob_mode == "on" and checked:
        failed = [c for c in checked if c.pob is None]
        if failed:
            _err_utf8(f"PoB verification failed: {failed[0].pob_error or 'unknown error'}")
            ctx.exit(6)
            return False
    elif pob_mode == "auto" and checked and all(c.pob is None for c in checked):
        _err_utf8(
            "PoB verification unavailable (daemon/PoB not usable); "
            "report only shows modifier deltas, not DPS."
        )
    return True


def _collect_bench_options(
    *,
    bench_hint: bool,
    slot: str | None,
    open_prefix: bool,
    open_suffix: bool,
    requests: list[Any],
    wants: tuple[str, ...],
) -> list[dict[str, Any]]:
    bench_options: list[dict[str, Any]] = []
    if not (bench_hint and slot):
        return bench_options
    seen_ids: set[str] = set()
    affix_filter = (
        "prefix" if open_prefix and not open_suffix else ("suffix" if open_suffix and not open_prefix else None)
    )
    for text in [req.text for req in requests] + list(wants):
        for result in bench_mod.search(slot, text, limit=2, affix=affix_filter):
            recipe = result.recipe
            if recipe.id not in seen_ids:
                seen_ids.add(recipe.id)
                bench_options.append(recipe.as_dict())
    return bench_options


def _apply_cli_filters_to_auto_query(
    parsed: dict[str, Any],
    *,
    status: str = "available",
    budget: float | None = None,
    currency: str | None = "chaos_equiv",
    max_age: float | None = None,
    ilvl_min: int = 0,
    open_prefix: bool = False,
    open_suffix: bool = False,
    open_affix: bool = False,
    category: str | None = None,
    search_sort: str | None = None,
    allow_corrupted: bool | None = None,
    extra_stat_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Overlay CLI price/status/category/sort/stat/open-affix constraints onto a PoB-generated query."""
    inner = parsed.setdefault("query", {}) if "query" in parsed else parsed
    if status:
        inner["status"] = {"option": status.strip().lower()}
    filters = inner.setdefault("filters", {})
    if category:
        filters.setdefault("type_filters", {}).setdefault("filters", {})["category"] = {"option": category}
    if ilvl_min > 0:
        filters.setdefault("type_filters", {}).setdefault("filters", {})["ilvl"] = {"min": ilvl_min}
    if allow_corrupted is False:
        mf = filters.setdefault("misc_filters", {}).setdefault("filters", {})
        mf.setdefault("corrupted", {"option": "false"})
    trade_f: dict[str, Any] = {}
    if budget is not None and budget > 0:
        curr_norm = (currency or "").strip().lower()
        trade_f["price"] = {"max": budget} if curr_norm in ("", "chaos_equiv", "equiv") else {"option": curr_norm, "max": budget}
    if max_age is not None and max_age > 0:
        window = next((opt for hours, opt in INDEXED_WINDOWS if max_age <= hours), None)
        if window:
            trade_f["indexed"] = {"option": window}
    if trade_f:
        filters.setdefault("trade_filters", {}).setdefault("filters", {}).update(trade_f)
    if extra_stat_groups:
        inner.setdefault("stats", []).extend(extra_stat_groups)
    pseudo: list[dict[str, Any]] = []
    if open_prefix:
        pseudo.append({"id": OPEN_PREFIX_ID, "value": {"min": 1}, "disabled": False})
    if open_suffix:
        pseudo.append({"id": OPEN_SUFFIX_ID, "value": {"min": 1}, "disabled": False})
    if open_affix and not (open_prefix or open_suffix):
        pseudo.append({"id": OPEN_AFFIX_ID, "value": {"min": 1}, "disabled": False})
    if pseudo:
        inner.setdefault("stats", []).append({"type": "and", "filters": pseudo})
    if search_sort and "query" in parsed:
        s_norm = search_sort.strip().lower()
        if s_norm in ("asc", "desc"):
            parsed["sort"] = {"price": s_norm}
        elif s_norm in ("sum", "weight"):
            parsed["sort"] = {"statgroup.0": "desc"}
    return parsed


def _execute_trade_api_query(
    ctx: click.Context,
    *,
    proxy: str | None,
    verbose: bool,
    league: str | None,
    dump_query: bool,
    slot: str | None,
    resolved_category: str | None,
    requests: list[Any],
    weighted_groups: list[Any],
    wants: tuple[str, ...],
    allow_conversion: bool,
    convert_to: str | None,
    rarity: str,
    ilvl_min: int,
    status: str,
    currency: str,
    budget: float | None,
    max_age: float | None,
    bargain: float | None,
    search_sort: str,
    sort_by: str,
    open_prefix: bool,
    open_suffix: bool,
    open_affix: bool,
    allow_corrupted: bool,
    bench_hint: bool,
    fetch_limit: int,
    compact: bool,
    out_path: str | None,
    auto_query_body: dict[str, Any] | None = None,
    max_fetch_cap: int = 20,
) -> dict[str, Any] | None:
    import pob_cli.cli_trade as _ct

    with _ct.TradeClient(proxy=proxy, logger=(lambda line: _err_utf8(line)) if verbose else None) as client:
        league_name = _ct._resolve_league(league, offline_only=dump_query, proxy=proxy, client=client)
        try:
            entries, stats_source = _ct.load_trade_stats(remote_fetcher=None if dump_query else client.stats)
        except TradeError as exc:
            _trade_error(ctx, exc)
            return None
        except Exception as exc:
            if dump_query:
                try:
                    entries, stats_source = _ct.load_trade_stats(remote_fetcher=client.stats)
                except TradeError as trade_exc:
                    _trade_error(ctx, trade_exc)
                    return None
                except Exception as inner_exc:
                    _err_utf8(str(inner_exc))
                    ctx.exit(5)
                    return None
            else:
                _err_utf8(str(exc))
                ctx.exit(5)
                return None

        pseudo_filters: list[dict[str, Any]] = []
        if open_prefix:
            pseudo_filters.append({"id": OPEN_PREFIX_ID, "value": {"min": 1}, "disabled": False})
        if open_suffix:
            pseudo_filters.append({"id": OPEN_SUFFIX_ID, "value": {"min": 1}, "disabled": False})
        if open_affix and not (open_prefix or open_suffix):
            pseudo_filters.append({"id": OPEN_AFFIX_ID, "value": {"min": 1}, "disabled": False})
        open_flags = {
            "prefix": bool(open_prefix),
            "suffix": bool(open_suffix),
            "any": bool(open_affix and not (open_prefix or open_suffix)),
        }
        bench_options = _collect_bench_options(
            bench_hint=bench_hint,
            slot=slot,
            open_prefix=open_prefix,
            open_suffix=open_suffix,
            requests=requests,
            wants=wants,
        )

        try:
            plan = plan_stats(
                requests,
                entries,
                slot=slot,
                allow_conversion=allow_conversion,
                convert_to=convert_to,
                rarity=rarity,
                weighted_groups=weighted_groups,
            )
            query_args: dict[str, Any] = {
                "plan": plan,
                "category": resolved_category,
                "rarity": rarity,
                "item_level_min": ilvl_min,
                "status": status,
                "currency": currency,
                "budget": budget,
                "search_sort": search_sort,
                "pseudo_filters": pseudo_filters or None,
                "allow_corrupted": allow_corrupted,
            }
            is_auto = isinstance(auto_query_body, dict)
            manual_args = (
                {**query_args, "search_sort": "asc"}
                if (is_auto and str(search_sort).startswith("sum"))
                else query_args
            )
            built_manual = build_query(**manual_args, max_age_hours=max_age)
            if is_auto:
                extra_groups = list((built_manual.get("query") or {}).get("stats") or [])
                if pseudo_filters and extra_groups and extra_groups[-1].get("filters") == list(pseudo_filters):
                    extra_groups.pop()
                query = _apply_cli_filters_to_auto_query(
                    dict(auto_query_body),
                    status=status,
                    budget=budget,
                    currency=currency,
                    max_age=max_age,
                    ilvl_min=ilvl_min,
                    open_prefix=open_prefix,
                    open_suffix=open_suffix,
                    open_affix=open_affix,
                    category=resolved_category,
                    search_sort=search_sort if search_sort != "asc" else None,
                    allow_corrupted=False if (plan.conversions and not allow_corrupted) else None,
                    extra_stat_groups=extra_groups or None,
                )
                if str(search_sort).startswith("sum"):
                    merged_stats = (query.get("query") or {}).get("stats") or []
                    if not any(isinstance(g, dict) and g.get("type") == "weight" for g in merged_stats):
                        raise ValueError("--search-sort sum requires at least one weighted stat group.")
            else:
                query = built_manual
        except ValueError as exc:
            _err_utf8(str(exc))
            ctx.exit(1)
            return None

        if dump_query:
            payload = {
                "league": league_name,
                "slot": slot,
                "category": resolved_category,
                "statsSource": stats_source,
                "conversion": _conversion_payload(plan),
                "openAffixes": open_flags,
                "benchOptions": bench_options,
                "query": query,
            }
            _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
            return None

        try:
            market_ctx = _fetch_with_fresh_and_market(
                client=client,
                league_name=league_name,
                query=query,
                query_args=query_args,
                fetch_limit=fetch_limit,
                max_age=max_age,
                bargain=bargain,
                search_sort=search_sort,
                sort_by=sort_by,
                proxy=proxy,
                max_fetch_cap=max_fetch_cap,
                has_auto_query=isinstance(auto_query_body, dict),
            )
        except TradeError as exc:
            _trade_error(ctx, exc)
            return None

    return {
        "league_name": league_name,
        "stats_source": stats_source,
        "plan": plan,
        "open_flags": open_flags,
        "bench_options": bench_options,
        **market_ctx,
    }
