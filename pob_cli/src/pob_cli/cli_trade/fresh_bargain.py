"""Fresh-listing (`--max-age`), chaos-rate conversion, and bargain (`--bargain`) helpers for `pob trade search`."""

from __future__ import annotations

import copy
import math
import os
import statistics
from typing import Any

from ..trade_client import MAX_FETCH_IDS, MAX_FETCH_MANY, NoResults, TradeError, localize_query_for_tw
from ..trade_eval import listing_age_hours
from ..trade_query import build_query
from .common import _is_securable_row, _price_of

# The trade site stops matching at this many listings and sorts only those, so the "cheapest"
# page of a search that hits it is not the real floor.
RESULT_CAP = 10000

# Safe fallback chaos ratios when `realm == "tw"` (Global poe.ninja does not index TW leagues).
DEFAULT_TW_CURRENCY_RATES: dict[str, float] = {
    "chaos": 1.0,
    "divine": 150.0,
    "exalted": 15.0,
    "vaal": 1.0,
    "regal": 0.5,
    "alch": 0.25,
    "fuse": 0.25,
    "mirror": 60000.0,
}


def _tw_currency_rates_with_source() -> tuple[dict[str, float], str]:
    """Return TW fallback currency rates and source (`tw_env_override` or `tw_static_fallback`)."""
    rates = dict(DEFAULT_TW_CURRENCY_RATES)
    raw_div = os.environ.get("POB_TW_DIVINE_CHAOS", "").strip()
    if raw_div:
        try:
            div_val = float(raw_div)
            if math.isfinite(div_val) and div_val > 0:
                rates["divine"] = div_val
                return rates, "tw_env_override"
        except ValueError:
            pass
    return rates, "tw_static_fallback"


def _chaos_rates(client: Any, league: str, proxy: str | None) -> dict[str, float]:
    """Trade currency id -> chaos value (poe.ninja on global; safe TW Chaos/Divine fallback on tw)."""
    import pob_cli.cli_trade as _ct

    if getattr(client, "realm", "global") == "tw":
        return _tw_currency_rates_with_source()[0]

    rates = {"chaos": 1.0}
    try:
        ids = {
            str(entry.get("text")): str(entry.get("id"))
            for group in client.static()
            for entry in group.get("entries") or []
        }
        for name, chaos in _ct.fetch_currency_chaos_rates(league, proxy=proxy).items():
            if name in ids and chaos:
                rates.setdefault(ids[name], float(chaos))
    except Exception:
        pass
    return rates


def _price_chaos(row: dict[str, Any], rates: dict[str, float]) -> float | None:
    amount, currency = _price_of(row)
    rate = rates.get(currency or "")
    return amount * rate if (amount is not None and rate) else None


def _fetch_fresh(
    client: Any, ids: list[str], query_id: str, *, want: int, max_age: float
) -> tuple[list[dict[str, Any]], int]:
    """Fetch price-ordered listings until ``want`` are within ``max_age`` hours.

    The server's "listed" window is coarser than ``max_age``, so the cheapest page alone
    can be all too old. Returns (fresh rows, rows fetched); stops after 5 requests, or after
    as many as ``want`` listings take (10 at most).
    """
    fresh: list[dict[str, Any]] = []
    fetched = 0
    scan_cap = max(5 * MAX_FETCH_IDS, min(MAX_FETCH_MANY, want))
    for start in range(0, min(len(ids), scan_cap), MAX_FETCH_IDS):
        if len(fresh) >= want:
            break
        chunk = client.fetch(ids[start : start + MAX_FETCH_IDS], query_id)
        fetched += len(chunk)
        # No readable time is not proof of freshness: the server window is wider than max_age.
        fresh.extend(
            row for row in chunk if (age := listing_age_hours(row)) is not None and age <= max_age
        )
    return fresh, fetched


def _search_rows(
    client: Any, league: str, body: dict[str, Any], *, want: int, max_age: float | None
) -> tuple[str, int, list[dict[str, Any]], int]:
    """Search, then fetch up to ``want`` listings (only fresh ones when ``max_age`` is set).

    Returns (query id, total matches, rows, rows fetched).
    """
    found = client.search(league, body)
    query_id = str(found.get("id") or "")
    ids = [str(item) for item in found.get("result") or []]
    total = int(found.get("total") or len(ids))
    if max_age is None:
        rows = client.fetch_many(ids, query_id, limit=want)
        return query_id, total, rows, len(rows)
    rows, fetched = _fetch_fresh(client, ids, query_id, want=want, max_age=max_age)
    return query_id, total, rows, fetched


def _seller_account_key(row: dict[str, Any]) -> str:
    listing = row.get("listing") if isinstance(row, dict) else None
    account = listing.get("account") if isinstance(listing, dict) else None
    name = account.get("name") if isinstance(account, dict) else None
    return str(name).strip().lower() if name else ""


def _deduped_seller_prices(rows: list[dict[str, Any]], rates: dict[str, float]) -> list[float]:
    """Enforce 1 Seller Account = 1 Vote (lowest chaos price per account) before taking cheapest 10."""
    per_seller: dict[str, float] = {}
    anon_prices: list[float] = []
    for row in rows:
        price = _price_chaos(row, rates)
        if price is None:
            continue
        seller = _seller_account_key(row)
        if seller:
            prev = per_seller.get(seller)
            if prev is None or price < prev:
                per_seller[seller] = price
        else:
            anon_prices.append(price)
    return sorted([*per_seller.values(), *anon_prices])[:10]


def _market_chaos(
    cheapest: list[dict[str, Any]],
    rates: dict[str, float],
    *,
    status: str | None = None,
) -> float | None:
    """Going price in chaos: 1-seller-1-vote median of the 10 cheapest listings, any age.

    When ``status == "available"`` and at least 5 distinct-seller Instant Buyout (``securable``)
    listings exist in the fetched batch, compute the median from those securable listings so
    fake low whisper price-fixers cannot depress the bargain anchor. None under 5 votes.
    """
    if status == "available":
        securable_prices = _deduped_seller_prices(
            [row for row in cheapest if _is_securable_row(row)],
            rates,
        )
        if len(securable_prices) >= 5:
            return statistics.median(securable_prices)
    prices = _deduped_seller_prices(cheapest, rates)
    return statistics.median(prices) if len(prices) >= 5 else None


def _clone_without_max_age(query: dict[str, Any], *, price_asc: bool = False) -> dict[str, Any]:
    cloned = copy.deepcopy(query)
    tf = ((cloned.get("query") or {}).get("filters") or {}).get("trade_filters")
    if isinstance(tf, dict) and isinstance(tf.get("filters"), dict):
        tf["filters"].pop("indexed", None)
    if price_asc:
        cloned["sort"] = {"price": "asc"}
    return cloned


def _fetch_with_fresh_and_market(
    *,
    client: Any,
    league_name: str,
    query: dict[str, Any],
    query_args: dict[str, Any],
    fetch_limit: int,
    max_age: float | None,
    bargain: float | None,
    search_sort: str,
    sort_by: str,
    proxy: str | None,
    has_auto_query: bool = False,
) -> dict[str, Any]:
    """Execute trade search + optional fresh filtering/fallback, chaos rates, and bargain baseline."""
    import pob_cli.cli_trade as _ct

    want = max(0, min(fetch_limit, MAX_FETCH_MANY))
    rows: list[dict[str, Any]] = []
    query_id = ""
    total = 0
    fetched = 0
    stale_fallback = False

    try:
        query_id, total, rows, fetched = _search_rows(
            client, league_name, query, want=want, max_age=max_age
        )
    except NoResults:
        if max_age is None or not want:
            raise
    if max_age is not None and not rows and want:
        # Nothing that fresh: show the older listings rather than an empty report.
        # `query` is replaced so the links and the JSON match what is listed.
        query = _clone_without_max_age(query) if has_auto_query else build_query(**query_args)
        if not has_auto_query and getattr(client, "realm", "global") == "tw":
            query = localize_query_for_tw(query, pob_dir=getattr(client, "pob_dir", None))
        query_id, total, rows, fetched = _search_rows(
            client, league_name, query, want=want, max_age=None
        )
        stale_fallback = True

    dropped_old = fetched - len(rows)
    rows = rows[:want]  # the last page fetched can overshoot --fetch-limit

    search_url = client.search_url(league_name, query_id) if query_id else ""
    # Looked up only when it changes the result, so a plain search needs no extra service.
    is_tw = getattr(client, "realm", "global") == "tw"
    needs_rates = is_tw or bargain is not None or sort_by == "price"
    rates_source = _tw_currency_rates_with_source()[1] if is_tw else "poe_ninja"
    rates = _ct._chaos_rates(client, league_name, proxy) if needs_rates else {"chaos": 1.0}
    market: float | None = None
    market_note = ""
    if bargain is not None:
        market_total = total
        # Read the body's sort: an --auto-query search sorts by weighted sum unless told otherwise.
        if (max_age is None or stale_fallback) and query.get("sort") == {"price": "asc"} and want >= 10:
            cheapest = rows  # this search already is "cheapest first, any age"; _market_chaos dedupes & slices [:10]
        else:
            try:
                mkt_query = (
                    _clone_without_max_age(query, price_asc=True)
                    if has_auto_query
                    else build_query(**{**query_args, "search_sort": "asc"})
                )
                if not has_auto_query and getattr(client, "realm", "global") == "tw":
                    mkt_query = localize_query_for_tw(mkt_query, pob_dir=getattr(client, "pob_dir", None))
                market_total, cheapest = _search_rows(
                    client,
                    league_name,
                    mkt_query,
                    want=10,
                    max_age=None,
                )[1:3]
            except TradeError:
                cheapest = []
        capped = market_total >= RESULT_CAP
        market = None if capped else _market_chaos(cheapest, rates, status=query_args.get("status"))
        market_note = (
            f"median of the {len(cheapest)} cheapest listings (any age) is {market:g} chaos; "
            f"BARGAIN = at least {bargain:g}% below that (price only - judge the item by the PoB columns)"
            if market
            else f"no going price: the search matches {RESULT_CAP}+ listings, the site's cap, so its "
            "cheapest page is not the real floor; narrow the search - nothing flagged"
            if capped
            else "no going price (under 5 priced listings, or the market search failed); nothing flagged"
        )

    return {
        "query": query,
        "rows": rows,
        "total": total,
        "fetched": fetched,
        "dropped_old": dropped_old,
        "stale_fallback": stale_fallback,
        "search_url": search_url,
        "rates": rates,
        "rates_source": rates_source,
        "market": market,
        "market_note": market_note,
    }
