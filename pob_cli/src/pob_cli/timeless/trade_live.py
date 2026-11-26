"""Live official PoE1 Trade API batch executor with Early-Stop and post-ranking seller deduplication."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from typing import Any

from ..external_data import fetch_currency_chaos_rates
from ..trade_client import MAX_FETCH_IDS, NoResults, TradeClient, TradeError, seller_url
from ..trade_client.league_meta import extract_listing_trade_meta, resolve_trade_league
from .trade_data import canonical_conqueror_key, parse_timeless_mod_line
from .trade_sharder import build_single_seed_query, lookup_seed_outcome

_DEFAULT_RATES: dict[str, float] = {"chaos": 1.0, "divine": 150.0, "exalted": 15.0}


def _iter_item_mod_lines(raw_item: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in ("explicitMods", "pseudoMods", "fracturedMods", "implicitMods"):
        vals = raw_item.get(key)
        if not isinstance(vals, list):
            continue
        for entry in vals:
            txt = entry.get("description") if isinstance(entry, dict) else entry
            if isinstance(txt, str) and txt.strip():
                lines.append(" ".join(txt.splitlines()).strip())
    return lines


def extract_seed_and_conqueror(raw_item: dict[str, Any]) -> tuple[int | None, str | None]:
    """Extract `(seed, canonical_pob_conqueror)` from an English or zh-TW/zh-CN Trade API item."""
    if not isinstance(raw_item, dict):
        return None, None
    for line in _iter_item_mod_lines(raw_item):
        seed, conq = parse_timeless_mod_line(line)
        if seed is not None:
            return seed, conq
    return None, None


def extract_listing_meta(row: dict[str, Any]) -> dict[str, Any]:
    """Delegate to shared `trade_client.league_meta.extract_listing_trade_meta` (`securable`/`in_person`)."""
    return extract_listing_trade_meta(row)


def resolve_timeless_trade_league(
    explicit: str | None,
    *,
    realm: str | None = None,
    offline_only: bool = False,
    proxy: str | None = None,
    client: TradeClient | None = None,
) -> tuple[str, str | None]:
    """Resolve active PoE1 trade league and optional warning, preserving RateLimited/CookieRequired."""
    return resolve_trade_league(explicit, realm=realm, offline_only=offline_only, proxy=proxy, client=client)


def _resolve_currency_rates(
    client: Any, league: str, *, realm: str, proxy: str | None
) -> dict[str, float]:
    rates = dict(_DEFAULT_RATES)
    if realm == "tw":
        raw_div = os.environ.get("POB_TW_DIVINE_CHAOS", "").strip()
        if raw_div:
            try:
                val = float(raw_div)
                if math.isfinite(val) and val > 0:
                    rates["divine"] = val
            except ValueError:
                pass
        return rates
    try:
        ids = {
            str(e.get("text")): str(e.get("id"))
            for grp in client.static()
            for e in (grp.get("entries") or [])
        }
        for name, chaos in fetch_currency_chaos_rates(league, proxy=proxy).items():
            if name in ids and chaos:
                rates[ids[name]] = float(chaos)
    except Exception:
        pass
    return rates


def _within_budget(
    amount: float | None,
    price_currency: str | None,
    price_chaos: float | None,
    *,
    budget: float | None,
    budget_currency: str,
    rates: dict[str, float],
) -> bool:
    if budget is None or budget <= 0:
        return True
    if amount is None:
        return False
    p_cur = (price_currency or "").strip().lower()
    b_cur = (budget_currency or "divine").strip().lower()
    if p_cur and p_cur == b_cur:
        return amount <= budget + 1e-9
    b_rate = rates.get(b_cur)
    if b_rate and b_rate > 0:
        # Never allow unknown high-tier currencies (no known chaos rate) to silently bypass `--budget`.
        if price_chaos is None:
            return False
        return price_chaos <= budget * b_rate + 1e-9
    return False


def _match_sort_key(item: dict[str, Any]) -> tuple[float, float, int, float, float, int]:
    d_stat = item.get("deltaStat")
    d_dps = item.get("deltaDps")
    rank = int(item.get("pobRank") or 999999)
    p_chaos = item.get("priceChaos")
    p_amt = item.get("priceAmount")
    return (
        -(float(d_stat) if isinstance(d_stat, (int, float)) and math.isfinite(d_stat) else -1e18),
        -(float(d_dps) if isinstance(d_dps, (int, float)) and math.isfinite(d_dps) else -1e18),
        rank,
        float(p_chaos) if isinstance(p_chaos, (int, float)) and math.isfinite(p_chaos) else 1e18,
        float(p_amt) if isinstance(p_amt, (int, float)) and math.isfinite(p_amt) else 1e18,
        int(item.get("seed") or 0),
    )


def _rank_and_cap_by_seller(
    distinct_matches: list[dict[str, Any]],
    *,
    max_per_seller: int,
) -> tuple[list[dict[str, Any]], int]:
    """Sort by PoB gain descending (price ascending tie-break), then apply `max_per_seller`."""
    ranked = sorted(distinct_matches, key=_match_sort_key)
    if max_per_seller <= 0:
        return ranked, 0
    capped: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    dropped = 0
    for item in ranked:
        seller_key = str(item.get("seller") or "").strip().lower()
        if not seller_key:
            capped.append(item)
            continue
        cnt = counts.get(seller_key, 0)
        if cnt >= max_per_seller:
            dropped += 1
            continue
        counts[seller_key] = cnt + 1
        capped.append(item)
    return capped, dropped


def _build_trade_report(
    plan: dict[str, Any],
    *,
    realm: str,
    league: str,
    eff_budget: float | None,
    eff_currency: str,
    max_per_seller: int,
    max_trade_batches: int,
    trade_limit: int,
    mode: str,
    batch_summaries: list[dict[str, Any]],
    batches_executed: int,
    early_stopped: bool,
    early_stop_reason: str | None,
    seller_capped_dropped: int,
    ranked_matches: list[dict[str, Any]],
    offline_fallback: bool = False,
    league_warning: str | None = None,
    error_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    total_before_limit = len(ranked_matches)
    sliced = ranked_matches[:trade_limit]
    report: dict[str, Any] = {
        "realm": realm, "league": league, "jewel": plan.get("jewel"), "conqueror": plan.get("conqueror"),
        "status": plan.get("status"), "budget": eff_budget, "currency": eff_currency,
        "maxPerSeller": max_per_seller, "maxTradeBatches": max_trade_batches, "tradeLimit": trade_limit,
        "mode": mode, "offlineFallback": offline_fallback, "totalShards": len(batch_summaries),
        "batchesExecuted": batches_executed,
        "remainingShardCount": max(0, len(batch_summaries) - batches_executed),
        "truncatedShardCandidates": int(plan.get("truncatedShardCandidates") or 0),
        "unmatchedDevotionMods": list(plan.get("unmatchedDevotionMods") or []),
        "unmappedTwFields": list(plan.get("unmappedTwFields") or []),
        "earlyStopped": early_stopped, "earlyStopReason": early_stop_reason,
        "sellerCappedDropped": seller_capped_dropped,
        "totalMatchedBeforeLimit": total_before_limit, "matchedCount": len(sliced),
        "matches": sliced, "batches": batch_summaries,
    }
    if league_warning:
        report["leagueWarning"] = league_warning
    if error_info:
        report["error"] = error_info
    return report


def build_urls_only_trade_report(
    plan: dict[str, Any],
    *,
    max_trade_batches: int = 0,
    trade_limit: int = 5,
    max_per_seller: int = 2,
    budget: float | None = None,
    currency: str | None = None,
    offline_fallback: bool = False,
    league_warning: str | None = None,
    reason: str = "urls_only",
) -> dict[str, Any]:
    """Build a deterministic 0-HTTP `urls_only` trade report from a pre-sharded plan."""
    realm = str(plan.get("realm") or "global")
    league = str(plan.get("league") or ("標準模式" if realm == "tw" else "Standard"))
    eff_budget = budget if budget is not None else plan.get("budget")
    eff_currency = str(currency or plan.get("currency") or "divine")
    batch_summaries: list[dict[str, Any]] = [
        {
            "batch": int(s["batch"]), "seedCount": int(s["seedCount"]), "filterCount": int(s["filterCount"]),
            "complexity": int(s["complexity"]), "seeds": list(s["seeds"]), "url": str(s["url"]),
            "executed": False, "queryId": None, "searchUrl": None, "total": 0, "fetched": 0,
            "unfetchedCount": 0, "unparsedCount": 0, "notInPlanCount": 0, "matched": 0,
        }
        for s in (plan.get("shards") or [])
    ]
    return _build_trade_report(
        plan, realm=realm, league=league, eff_budget=eff_budget, eff_currency=eff_currency,
        max_per_seller=max_per_seller, max_trade_batches=max_trade_batches, trade_limit=trade_limit,
        mode="urls_only", batch_summaries=batch_summaries, batches_executed=0,
        early_stopped=False, early_stop_reason=reason, seller_capped_dropped=0, ranked_matches=[],
        offline_fallback=offline_fallback, league_warning=league_warning,
    )


def execute_timeless_trade_plan(
    plan: dict[str, Any],
    *,
    client: Any | None = None,
    max_trade_batches: int = 3,
    trade_limit: int = 5,
    max_per_seller: int = 2,
    budget: float | None = None,
    currency: str | None = None,
    rates: dict[str, float] | None = None,
    proxy: str | None = None,
    progress: Callable[[str], None] | None = None,
    offline_fallback: bool = False,
    league_warning: str | None = None,
) -> dict[str, Any]:
    """Execute pre-sharded timeless trade queries with Early-Stop and post-ranking seller cap."""
    if max_trade_batches < 0:
        raise ValueError("--max-trade-batches must be >= 0.")
    if trade_limit < 1:
        raise ValueError("--trade-limit must be >= 1.")
    if max_per_seller < 0:
        raise ValueError("--max-per-seller must be >= 0.")

    realm = str(plan.get("realm") or "global")
    league = str(plan.get("league") or ("標準模式" if realm == "tw" else "Standard"))
    eff_budget = budget if budget is not None else plan.get("budget")
    eff_currency = str(currency or plan.get("currency") or "divine")
    shards = list(plan.get("shards") or [])
    say = progress or (lambda _msg: None)

    batch_summaries: list[dict[str, Any]] = [
        {
            "batch": int(s["batch"]), "seedCount": int(s["seedCount"]), "filterCount": int(s["filterCount"]),
            "complexity": int(s["complexity"]), "seeds": list(s["seeds"]), "url": str(s["url"]),
            "executed": False, "queryId": None, "searchUrl": None, "total": 0, "fetched": 0,
            "unfetchedCount": 0, "unparsedCount": 0, "notInPlanCount": 0, "matched": 0,
        }
        for s in shards
    ]

    if max_trade_batches == 0 or not shards:
        reason = "offline_fallback" if offline_fallback else ("max_trade_batches=0" if max_trade_batches == 0 else "no_shards")
        return _build_trade_report(
            plan, realm=realm, league=league, eff_budget=eff_budget, eff_currency=eff_currency,
            max_per_seller=max_per_seller, max_trade_batches=max_trade_batches, trade_limit=trade_limit,
            mode="urls_only", batch_summaries=batch_summaries, batches_executed=0,
            early_stopped=False, early_stop_reason=reason, seller_capped_dropped=0, ranked_matches=[],
            offline_fallback=offline_fallback, league_warning=league_warning,
        )

    own_client = client is None
    active_client = TradeClient(proxy=proxy, realm=realm) if own_client else client
    try:
        eff_rates = dict(rates) if rates is not None else _resolve_currency_rates(
            active_client, league, realm=realm, proxy=proxy
        )
        div_rate = eff_rates.get("divine") or 150.0
        distinct_matches: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_clones: dict[tuple[Any, ...], dict[str, Any]] = {}
        batches_executed = 0
        early_stopped = False
        early_stop_reason: str | None = None
        fetch_cap = min(50, max(20, MAX_FETCH_IDS * 2, trade_limit * 2))

        for idx, shard in enumerate(shards[:max_trade_batches]):
            bmeta = batch_summaries[idx]
            bmeta["executed"] = True
            batches_executed += 1
            say(f"trade batch {bmeta['batch']}/{min(max_trade_batches, len(shards))} ({bmeta['seedCount']} seeds)")
            try:
                found = active_client.search(league, shard["payload"])
            except NoResults:
                continue
            except TradeError as exc:
                capped_partial, dropped_partial = _rank_and_cap_by_seller(distinct_matches, max_per_seller=max_per_seller)
                exc.partial_trade_result = _build_trade_report(  # type: ignore[attr-defined]
                    plan, realm=realm, league=league, eff_budget=eff_budget, eff_currency=eff_currency,
                    max_per_seller=max_per_seller, max_trade_batches=max_trade_batches, trade_limit=trade_limit,
                    mode="live", batch_summaries=batch_summaries, batches_executed=batches_executed,
                    early_stopped=False, early_stop_reason=None, seller_capped_dropped=dropped_partial,
                    ranked_matches=capped_partial, offline_fallback=False, league_warning=league_warning,
                )
                raise

            query_id = str(found.get("id") or "")
            ids = [str(x) for x in (found.get("result") or [])]
            bmeta["queryId"] = query_id or None
            bmeta["total"] = int(found.get("total") or len(ids))
            bmeta["searchUrl"] = active_client.search_url(league, query_id) if query_id else None
            try:
                rows = active_client.fetch_many(ids, query_id, limit=fetch_cap) if ids else []
            except TradeError as exc:
                capped_partial, dropped_partial = _rank_and_cap_by_seller(distinct_matches, max_per_seller=max_per_seller)
                exc.partial_trade_result = _build_trade_report(  # type: ignore[attr-defined]
                    plan, realm=realm, league=league, eff_budget=eff_budget, eff_currency=eff_currency,
                    max_per_seller=max_per_seller, max_trade_batches=max_trade_batches, trade_limit=trade_limit,
                    mode="live", batch_summaries=batch_summaries, batches_executed=batches_executed,
                    early_stopped=False, early_stop_reason=None, seller_capped_dropped=dropped_partial,
                    ranked_matches=capped_partial, offline_fallback=False, league_warning=league_warning,
                )
                raise

            bmeta["fetched"] = len(rows)
            bmeta["unfetchedCount"] = max(0, bmeta["total"] - len(rows))
            batch_matched = 0
            unparsed = 0
            not_in_plan = 0

            for row in rows:
                meta = extract_listing_meta(row)
                lid = meta["listingId"]
                if lid and lid in seen_ids:
                    continue
                seed, conq = extract_seed_and_conqueror(row.get("item") or {})
                if seed is None:
                    unparsed += 1
                    continue
                outcome = lookup_seed_outcome(plan, seed, conq)
                if outcome is None:
                    not_in_plan += 1
                    continue
                p_amt, p_cur = meta["priceAmount"], meta["priceCurrency"]
                p_rate = eff_rates.get(p_cur or "")
                p_chaos = round(p_amt * p_rate, 2) if (p_amt is not None and p_rate) else None
                if not _within_budget(p_amt, p_cur, p_chaos, budget=eff_budget, budget_currency=eff_currency, rates=eff_rates):
                    continue
                p_div = (
                    round(p_amt, 4) if (p_cur == "divine" and p_amt is not None)
                    else (round(p_chaos / div_rate, 4) if (p_chaos is not None and div_rate > 0) else None)
                )

                seller_key = meta["seller"].lower()
                clone_key = (seller_key, seed, canonical_conqueror_key(conq or ""), p_chaos if p_chaos is not None else p_amt, p_cur) if seller_key else None
                if clone_key is not None and clone_key in seen_clones:
                    seen_clones[clone_key]["sellerDuplicatesCollapsed"] += 1
                    if lid:
                        seen_ids.add(lid)
                    continue

                d_dps = outcome.get("deltaDps")
                dps_per_div = round(float(d_dps) / p_div, 1) if (isinstance(d_dps, (int, float)) and d_dps > 0 and p_div and p_div > 0) else None
                direct_url = (
                    seller_url(league, build_single_seed_query(plan, seed, conq), meta["seller"], realm=realm)
                    if meta["seller"]
                    else (bmeta["searchUrl"] or bmeta["url"])
                )
                entry: dict[str, Any] = {
                    "batch": bmeta["batch"], "listingId": lid, "seed": seed,
                    "conqueror": conq or outcome.get("conqueror") or (outcome.get("conquerors") or [""])[0],
                    "socket": outcome.get("socket"), "socketLabel": outcome.get("socketLabel"),
                    "pobRank": outcome.get("rank"), "stat": outcome.get("stat"),
                    "statValue": outcome.get("statValue"), "deltaStat": outcome.get("deltaStat"),
                    "deltaStatPct": outcome.get("deltaStatPct"), "dps": outcome.get("dps"),
                    "deltaDps": d_dps, "deltaDpsPct": outcome.get("deltaDpsPct"),
                    "ehp": outcome.get("ehp"), "deltaEhp": outcome.get("deltaEhp"),
                    "deltaEhpPct": outcome.get("deltaEhpPct"), "keyNotables": list(outcome.get("keyNotables") or []),
                    "priceAmount": p_amt, "priceCurrency": p_cur, "priceChaos": p_chaos, "priceDivine": p_div,
                    "dpsPerDivine": dps_per_div, "tradeMode": meta["tradeMode"], "goldFee": meta["goldFee"],
                    "priceType": meta["priceType"], "seller": meta["seller"],
                    "sellerDuplicatesCollapsed": 0, "indexed": meta["indexed"], "url": direct_url,
                }
                if lid:
                    seen_ids.add(lid)
                if clone_key is not None:
                    seen_clones[clone_key] = entry
                distinct_matches.append(entry)
                batch_matched += 1

            bmeta["unparsedCount"] = unparsed
            bmeta["notInPlanCount"] = not_in_plan
            bmeta["matched"] = batch_matched
            capped_now, _ = _rank_and_cap_by_seller(distinct_matches, max_per_seller=max_per_seller)
            if len(capped_now) >= trade_limit:
                early_stopped = True
                early_stop_reason = f"collected {len(capped_now)} >= trade_limit ({trade_limit}) at batch {bmeta['batch']}"
                break

        capped_final, seller_capped_dropped = _rank_and_cap_by_seller(distinct_matches, max_per_seller=max_per_seller)
        return _build_trade_report(
            plan, realm=realm, league=league, eff_budget=eff_budget, eff_currency=eff_currency,
            max_per_seller=max_per_seller, max_trade_batches=max_trade_batches, trade_limit=trade_limit,
            mode="live", batch_summaries=batch_summaries, batches_executed=batches_executed,
            early_stopped=early_stopped, early_stop_reason=early_stop_reason,
            seller_capped_dropped=seller_capped_dropped, ranked_matches=capped_final,
            offline_fallback=False, league_warning=league_warning,
        )
    finally:
        if own_client:
            active_client.close()
