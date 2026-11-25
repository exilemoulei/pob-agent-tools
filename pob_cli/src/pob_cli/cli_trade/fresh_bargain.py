"""Fresh-listing (`--max-age`), chaos-rate conversion, and bargain (`--bargain`) helpers for `pob trade search`."""

from __future__ import annotations

import copy
import statistics
from typing import Any

from ..trade_client import MAX_FETCH_IDS, MAX_FETCH_MANY, NoResults, TradeError
from ..trade_eval import listing_age_hours
from ..trade_query import build_query
from .common import _price_of

# The trade site stops matching at this many listings and sorts only those, so the "cheapest"
# page of a search that hits it is not the real floor.
RESULT_CAP = 10000


def _chaos_rates(client: Any, league: str, proxy: str | None) -> dict[str, float]:
    """Trade currency id -> chaos value (poe.ninja); only chaos itself when a source is unreachable."""
    import pob_cli.cli_trade as _ct

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
    can be all too old. Returns (fresh rows, rows fetched); stops after 5 requests.
    """
    fresh: list[dict[str, Any]] = []
    fetched = 0
    for start in range(0, min(len(ids), 5 * MAX_FETCH_IDS), MAX_FETCH_IDS):
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


def _market_chaos(cheapest: list[dict[str, Any]], rates: dict[str, float]) -> float | None:
    """Going price in chaos: the median of the cheapest listings of the search, any age.

    Comparing with old listings only does not work: cheap items sell, so what stays listed
    is overpriced and every fresh listing looks like a bargain. None under 5 known prices.
    """
    prices = [price for row in cheapest if (price := _price_chaos(row, rates)) is not None]
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
        query_id, total, rows, fetched = _search_rows(
            client, league_name, query, want=want, max_age=None
        )
        stale_fallback = True

    dropped_old = fetched - len(rows)
    rows = rows[:want]  # the last page fetched can overshoot --fetch-limit

    search_url = client.search_url(league_name, query_id) if query_id else ""
    # Looked up only when it changes the result, so a plain search needs no extra service.
    needs_rates = bargain is not None or sort_by == "price"
    rates = _ct._chaos_rates(client, league_name, proxy) if needs_rates else {"chaos": 1.0}
    market: float | None = None
    market_note = ""
    if bargain is not None:
        market_total = total
        # Read the body's sort: an --auto-query search sorts by weighted sum unless told otherwise.
        if (max_age is None or stale_fallback) and query.get("sort") == {"price": "asc"} and want >= 10:
            cheapest = rows[:10]  # this search already is "cheapest first, any age"
        else:
            try:
                mkt_query = (
                    _clone_without_max_age(query, price_asc=True)
                    if has_auto_query
                    else build_query(**{**query_args, "search_sort": "asc"})
                )
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
        market = None if capped else _market_chaos(cheapest, rates)
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
        "market": market,
        "market_note": market_note,
    }
