"""Validation, API execution, candidate building, and PoB verification for `pob trade search`."""

from __future__ import annotations

import copy
import re
from typing import Any

import click

from .. import bench as bench_mod
from ..cli_common import _emit_json
from ..errors import BootstrapMissingError, CrossGameHallucinationError
from ..guardrails import GuardrailScope, check_input, validate_url
from ..locale_cfg import current_realm
from ..trade_client import TradeError, localize_query_for_tw, seller_url, unmapped_tw_ascii_fields
from ..trade_eval import Candidate, compare_items, score_deltas, verify_candidates_batch
from ..trade_item import build_item_text, convert_item_text, parse_item_text, sanitize_text, trade_mod_tiers
from ..trade_query import (
    OPEN_AFFIX_ID,
    OPEN_PREFIX_ID,
    OPEN_SUFFIX_ID,
    build_query,
    plan_stats,
)
from ..trade_query.auto_query import apply_cli_filters_to_auto_query as _apply_cli_filters_to_auto_query
from ..trade_query.auto_query import fit_complexity, wrap_auto_query
from ..updater import format_stderr_update_alert
from .common import (
    _conversion_payload,
    _err_utf8,
    _is_known_slot,
    _listing_trade_meta,
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
    status: str = "available",
    securable: bool = False,
    max_per_seller: int = 2,
    item_name: str | None = None,
    item_type: str | None = None,
) -> tuple[str, str] | None:
    if securable:
        status_src = ctx.get_parameter_source("status")
        if status_src != click.core.ParameterSource.DEFAULT and status != "securable":
            _err_utf8(f"--securable conflicts with --status {status!r}; omit --status or pass --status securable.")
            ctx.exit(1)
            return None
        status = "securable"
    if max_per_seller < 0:
        _err_utf8("--max-per-seller must be 0 or greater.")
        ctx.exit(1)
        return None
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
    if not slot and not category and not no_category and not item_name and not item_type:
        _err_utf8("Provide --slot (or --category / --no-category / --name / --type).")
        ctx.exit(1)
        return None
    if slot and not _is_known_slot(slot) and not category and not no_category:
        _err_utf8(f"Unknown slot {slot!r}; pass a known --slot, --category, or --no-category.")
        ctx.exit(1)
        return None
    return sort_by, status


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
    realm: str = "global",
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
        trade_mode, gold_fee, price_type = _listing_trade_meta(row)
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
            trade_mode=trade_mode,
            gold_fee=gold_fee,
            price_type=price_type,
        )
        if candidate.seller:
            # One seller rarely has two matching items, so this link lands on the item itself.
            candidate.url = seller_url(league_name, query["query"], candidate.seller, realm=realm)
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
    prefilter_k: int | None = None,
    normalize_quality: bool = False,
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
            prefilter_k=prefilter_k,
            normalize_quality=normalize_quality,
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
        err_slice = candidates if prefilter_k is not None else candidates[:verification_limit]
        for c in err_slice:
            if c.pob is None and not c.pob_error:
                c.pob_error = err_text
        if pob_mode == "on":
            _err_utf8(f"PoB verification failed: {err_text}")
            ctx.exit(6)
            return False

    if prefilter_k is not None:
        checked = [c for c in candidates if c.pob is not None or c.pob_error is not None] or candidates[:verification_limit]
    else:
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


def _build_effective_trade_query(
    *,
    entries: list[dict[str, Any]],
    requests: list[Any],
    weighted_groups: list[Any],
    slot: str | None,
    resolved_category: str | None,
    allow_conversion: bool,
    convert_to: str | None,
    rarity: str,
    ilvl_min: int,
    status: str,
    currency: str,
    budget: float | None,
    max_age: float | None,
    search_sort: str,
    open_prefix: bool,
    open_suffix: bool,
    open_affix: bool,
    allow_corrupted: bool,
    pseudo_filters: list[dict[str, Any]],
    auto_query_body: dict[str, Any] | None,
    eff_realm: str,
    item_name: str | None,
    item_type: str | None,
    corrupted: str | None,
) -> tuple[Any, dict[str, Any], dict[str, Any], int | None]:
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
        "name": item_name,
        "item_type": item_type,
        "corrupted": corrupted,
    }
    is_auto = isinstance(auto_query_body, dict)
    auto_complexity: int | None = None
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
        cor_norm = (corrupted or "").strip().lower()
        auto_allow_corrupted = (
            False
            if (plan.conversions and not allow_corrupted and cor_norm not in ("yes", "true", "1", "any"))
            else None
        )
        query = _apply_cli_filters_to_auto_query(
            wrap_auto_query(copy.deepcopy(auto_query_body)),
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
            allow_corrupted=auto_allow_corrupted,
            extra_stat_groups=extra_groups or None,
            name=item_name,
            item_type=item_type,
            corrupted=corrupted,
        )
        if str(search_sort).startswith("sum"):
            merged_stats = (query.get("query") or {}).get("stats") or []
            if not any(isinstance(g, dict) and g.get("type") == "weight" for g in merged_stats):
                raise ValueError("--search-sort sum requires at least one weighted stat group.")
        auto_complexity, _dropped = fit_complexity(query)
    else:
        query = built_manual
    if eff_realm == "tw":
        query = localize_query_for_tw(query)
        for fld, val in unmapped_tw_ascii_fields(query):
            _err_utf8(
                f"Warning: pathofexile.tw requires Traditional Chinese item/base names; "
                f"could not map {fld}={val!r} to zh-rTW."
            )
    return plan, query_args, query, auto_complexity


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
    realm: str | None = None,
    item_name: str | None = None,
    item_type: str | None = None,
    corrupted: str | None = None,
) -> dict[str, Any] | None:
    import pob_cli.cli_trade as _ct

    eff_realm = current_realm(cli_realm=realm)
    client_kw: dict[str, Any] = {
        "proxy": proxy,
        "logger": (lambda line: _err_utf8(line)) if verbose else None,
        "realm": eff_realm,
    }
    with _ct.TradeClient(**client_kw) as client:
        resolve_kw: dict[str, Any] = {
            "offline_only": dump_query,
            "proxy": proxy,
            "client": client,
            "realm": eff_realm,
        }
        league_name = _ct._resolve_league(league, **resolve_kw)
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
            plan, query_args, query, auto_complexity = _build_effective_trade_query(
                entries=entries,
                requests=requests,
                weighted_groups=weighted_groups,
                slot=slot,
                resolved_category=resolved_category,
                allow_conversion=allow_conversion,
                convert_to=convert_to,
                rarity=rarity,
                ilvl_min=ilvl_min,
                status=status,
                currency=currency,
                budget=budget,
                max_age=max_age,
                search_sort=search_sort,
                open_prefix=open_prefix,
                open_suffix=open_suffix,
                open_affix=open_affix,
                allow_corrupted=allow_corrupted,
                pseudo_filters=pseudo_filters,
                auto_query_body=auto_query_body,
                eff_realm=eff_realm,
                item_name=item_name,
                item_type=item_type,
                corrupted=corrupted,
            )
            is_auto = isinstance(auto_query_body, dict)
        except ValueError as exc:
            _err_utf8(str(exc))
            ctx.exit(1)
            return None

        if dump_query:
            payload = {
                "realm": eff_realm,
                "league": league_name,
                "slot": slot,
                "category": resolved_category,
                "statsSource": stats_source,
                "conversion": _conversion_payload(plan),
                "openAffixes": open_flags,
                "benchOptions": bench_options,
                "query": query,
            }
            if is_auto:
                payload["autoQuery"] = True
                payload["complexity"] = auto_complexity
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
                has_auto_query=isinstance(auto_query_body, dict),
            )
        except TradeError as exc:
            _trade_error(ctx, exc)
            return None

    return {
        "realm": eff_realm,
        "league_name": league_name,
        "stats_source": stats_source,
        "plan": plan,
        "open_flags": open_flags,
        "bench_options": bench_options,
        **market_ctx,
    }
