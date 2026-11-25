"""`pob trade search --auto-query`: the exact request body, the complexity budget and the reuse
of the generated query by the --max-age fallback and the --bargain market search."""

from __future__ import annotations

import base64
import copy
import gzip
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import cli_trade
from pob_cli.cli import main
from pob_cli.trade_client import NoResults, TradeClient
from pob_cli.trade_query.auto_query import fit_complexity, query_complexity, wrap_auto_query

MOCK_STATS: list[dict[str, Any]] = [
    {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
    {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
]


def _generated_query(n_filters: int = 3) -> dict[str, Any]:
    """The table PoB's TradeQueryGenerator hands its requester (weights descending)."""
    return {
        "filters": {
            "type_filters": {"filters": {"category": {"option": "armour.gloves"}, "rarity": {"option": "nonunique"}}},
            "misc_filters": {"disabled": False, "filters": {"mirrored": False}},
        },
        "status": {"option": "available"},
        "stats": [
            {
                "type": "weight",
                "value": {"min": 100.0},
                "filters": [
                    {"id": f"explicit.stat_{1000 + i}", "value": {"weight": float(n_filters - i)}}
                    for i in range(n_filters)
                ],
            },
            {"type": "and", "filters": []},
            {"type": "not", "filters": []},
        ],
    }


def _kernel_result(query: dict[str, Any], *, wrapped: bool = True) -> dict[str, Any]:
    body = {"query": query, "sort": {"statgroup.0": "desc"}} if wrapped else query
    weight_filters = query["stats"][0]["filters"]
    return {
        "slotName": "Gloves",
        "stat": "CombinedDPS",
        "queryJson": json.dumps(body),
        "filters": len(weight_filters),
        "complexity": 0,
        "statLabels": {f["id"]: f"label {f['id']}" for f in weight_filters},
        "topFilters": [
            {"id": f["id"], "label": f"label {f['id']}", "weight": f["value"]["weight"]} for f in weight_filters[:15]
        ],
    }


def _row(row_id: str, *, hours: float = 1.0, amount: float = 10.0) -> dict[str, Any]:
    indexed = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": row_id,
        "item": {
            "name": row_id,
            "typeLine": "Slink Gloves",
            "baseType": "Slink Gloves",
            "frameType": 2,
            "ilvl": 85,
            "identified": True,
            "explicitMods": ["+82 to maximum Life"],
        },
        "listing": {
            "indexed": indexed,
            "account": {"name": f"Seller{row_id}#1"},
            "price": {"amount": amount, "currency": "chaos"},
        },
    }


@pytest.fixture
def searches(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(cli_trade, "load_trade_stats", lambda *_a, **_kw: (MOCK_STATS, "mock"))
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")
    return []


def _use_kernel(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    def _daemon(action: str, _params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        assert action == "trade_generate_query"
        return copy.deepcopy(result)

    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _daemon)


@pytest.mark.parametrize("wrapped", [True, False])
def test_auto_query_sends_wrapped_body_with_cli_filters(
    monkeypatch: pytest.MonkeyPatch, searches: list[dict[str, Any]], wrapped: bool
) -> None:
    _use_kernel(monkeypatch, _kernel_result(_generated_query(), wrapped=wrapped))
    rows = {"r1": _row("r1")}

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            searches.append(copy.deepcopy(query))
            return {"id": "q1", "total": 1, "result": list(rows)}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [rows[i] for i in ids[:limit]]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)
    args = ["trade", "search", "--slot", "gloves", "--auto-query", "--budget", "5", "--pob", "off", "--no-compare", "--json"]
    res = CliRunner().invoke(main, args)
    assert res.exit_code == 0, res.output

    expected_query = _generated_query()
    expected_query["filters"]["trade_filters"] = {"filters": {"price": {"max": 5.0}}}
    assert searches == [{"query": expected_query, "sort": {"statgroup.0": "desc"}}]

    # The per-seller link is built from the same query (this raised KeyError on a bare body).
    data = json.loads(res.output)
    blob = data["candidates"][0]["url"].rsplit("/", 1)[1]
    linked = json.loads(gzip.decompress(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))))
    assert linked["stats"] == expected_query["stats"]
    assert linked["filters"]["trade_filters"]["filters"]["account"] == {"input": "Sellerr1#1"}


def test_auto_query_explicit_search_sort_overrides_weighted_default(
    monkeypatch: pytest.MonkeyPatch, searches: list[dict[str, Any]]
) -> None:
    _use_kernel(monkeypatch, _kernel_result(_generated_query()))
    args = ["trade", "search", "--slot", "gloves", "--auto-query", "--search-sort", "asc", "--dump-query"]
    res = CliRunner().invoke(main, args)
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["query"]["sort"] == {"price": "asc"}


def test_auto_query_complexity_counts_cli_filters_and_trims_lowest_weights(
    monkeypatch: pytest.MonkeyPatch, searches: list[dict[str, Any]]
) -> None:
    # 22 generated filters fit on their own (62 + 3 + 88 = 153) but not with the CLI's additions.
    _use_kernel(monkeypatch, _kernel_result(_generated_query(22)))
    base = ["trade", "search", "--slot", "gloves", "--auto-query", "--budget", "5", "--max-age", "15", "--open-prefix"]

    dumped = json.loads(CliRunner().invoke(main, [*base, "--dump-query"]).output)
    body = dumped["query"]
    assert dumped["complexity"] == query_complexity(body) <= 160
    assert dumped["complexityWithinLimit"] is True
    weights = [f["value"]["weight"] for f in body["query"]["stats"][0]["filters"]]
    assert dumped["filters"] == len(weights) < 22
    # The dropped ones are exactly the lowest weights, and the metadata no longer lists them.
    assert sorted(weights, reverse=True) == weights and min(weights) == 22 - len(weights) + 1
    assert len(dumped["droppedFilters"]) == 22 - len(weights)
    assert not set(dumped["droppedFilters"]) & {f["id"] for f in dumped["topFilters"]}
    assert not set(dumped["droppedFilters"]) & set(dumped["statLabels"])
    assert dumped["minWeight"] == body["query"]["stats"][0]["value"]["min"] < 100.0

    # Manual --stat groups are added later and count as well.
    with_stat = CliRunner().invoke(main, [*base, "--stat", "maximum Life=80", "--dump-query"])
    assert with_stat.exit_code == 0, with_stat.output
    payload = json.loads(with_stat.output)
    assert payload["complexity"] == query_complexity(payload["query"]) <= 160
    stats = payload["query"]["query"]["stats"]
    assert len(stats[0]["filters"]) < len(weights)
    assert {"id": "explicit.stat_3299347043", "value": {"min": 80.0}, "disabled": False} in [
        f for g in stats[1:] for f in g["filters"]
    ]


def test_fit_complexity_unit() -> None:
    body = wrap_auto_query(_generated_query(30))
    before = query_complexity(body)
    complexity, dropped = fit_complexity(body, limit=120)
    kept = body["query"]["stats"][0]["filters"]
    assert before > 120 >= complexity == query_complexity(body)
    assert dropped == [f"explicit.stat_{1000 + i}" for i in range(29, 29 - len(dropped), -1)]
    assert len(kept) + len(dropped) == 30
    assert 25.0 <= body["query"]["stats"][0]["value"]["min"] < 100.0
    # Nothing to do when it already fits.
    assert fit_complexity(body, limit=120) == (complexity, [])


def test_auto_query_max_age_fallback_and_bargain_reuse_generated_query(
    monkeypatch: pytest.MonkeyPatch, searches: list[dict[str, Any]]
) -> None:
    _use_kernel(monkeypatch, _kernel_result(_generated_query()))
    rows = {f"r{i}": _row(f"r{i}", hours=40, amount=10.0 + i) for i in range(6)}

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            searches.append(copy.deepcopy(query))
            if "indexed" in json.dumps(query):
                raise NoResults()
            return {"id": "q1", "total": len(rows), "result": list(rows)}

        def fetch(self, ids: list[str], query_id: str) -> list[dict[str, Any]]:
            return [rows[i] for i in ids]

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return [rows[i] for i in ids[:limit]]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)
    args = [
        "trade", "search", "--slot", "gloves", "--auto-query", "--max-age", "15", "--bargain", "30",
        "--pob", "off", "--no-compare", "--json",
    ]
    res = CliRunner().invoke(main, args)
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["staleFallback"] is True

    generated = _generated_query()["stats"]
    fresh, fallback, market = searches
    assert fresh["query"]["filters"]["trade_filters"]["filters"]["indexed"] == {"option": "1day"}
    # Both follow-up searches keep the generated weights and only drop the listed window.
    for body in (fallback, market):
        assert body["query"]["stats"] == generated
        assert "indexed" not in json.dumps(body)
    assert fallback["sort"] == {"statgroup.0": "desc"}
    # A weighted-sum page is not "cheapest first", so the going price needs its own search.
    assert market["sort"] == {"price": "asc"}


def test_surrogate_k_flag_accepted_and_validates_non_negative(searches: list[dict[str, Any]]) -> None:
    res = CliRunner().invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "maximum Life=80", "--surrogate-k", "60", "--dump-query"],
    )
    assert res.exit_code == 0, res.output
    bad = CliRunner().invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "maximum Life=80", "--surrogate-k", "-1", "--dump-query"],
    )
    assert bad.exit_code == 2
