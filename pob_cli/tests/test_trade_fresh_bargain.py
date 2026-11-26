"""Tests for fresh-listing (`--max-age`), bargain (`--bargain`), chaos rates, and per-seller links."""

from __future__ import annotations

import base64
import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli_trade, external_data, trade_craft
from pob_cli import trade_query as tq
from pob_cli.cli import main
from pob_cli.cli_trade import _chaos_rates as _real_chaos_rates  # conftest stubs the module attribute
from pob_cli.trade_client import NoResults, TradeClient, seller_url
from pob_cli.trade_eval import Candidate, format_report, listing_age_hours, report_json
from pob_cli.trade_item import parse_item_text

MOCK_STATS: list[dict[str, Any]] = [
    {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
    {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
    {"id": "explicit.stat_3372524247", "text": "+#% to Fire Resistance", "type": "explicit"},
    {"id": "explicit.stat_1671376347", "text": "+#% to Lightning Resistance", "type": "explicit"},
]

SAMPLE_GLOVES_ITEM: dict[str, Any] = {
    "name": "Gale Hold",
    "typeLine": "Slink Gloves",
    "baseType": "Slink Gloves",
    "frameType": 2,
    "ilvl": 85,
    "identified": True,
    "explicitMods": [
        "+82 to maximum Life",
        "+42% to Cold Resistance",
    ],
}


def _mock_trade_stats(*_a: Any, **_kw: Any) -> tuple[list[dict[str, Any]], str]:
    return MOCK_STATS, "mock"


def _make_row(row_id: str, hours: float, amount: float, currency: str, seller: str) -> dict[str, Any]:
    indexed = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": row_id,
        "item": {**SAMPLE_GLOVES_ITEM, "name": row_id},
        "listing": {
            "indexed": indexed,
            "account": {"name": seller},
            "price": {"amount": amount, "currency": currency},
        },
    }


def _decode_seller_blob(url: str) -> dict[str, Any]:
    blob = url.rsplit("/", 1)[1]
    raw = gzip.decompress(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))
    return json.loads(raw)


def test_build_query_max_age_windows() -> None:
    plan = tq.plan_stats([tq.StatRequest("to Cold Resistance", 30)], MOCK_STATS, slot="gloves")

    def window(hours: float) -> dict[str, Any] | None:
        body = tq.build_query(plan=plan, category="armour.gloves", rarity="rare", budget=5, max_age_hours=hours)
        filters = body["query"]["filters"]["trade_filters"]["filters"]
        assert filters["price"] == {"max": 5}
        return filters.get("indexed")

    assert window(15) == {"option": "1day"}
    assert window(12) == {"option": "12hours"}
    assert window(0.5) == {"option": "1hour"}
    assert window(5000) is None


def test_trade_client_static_7day_cache(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"result": [{"id": "Currency", "entries": [{"id": "divine", "text": "Divine Orb"}]}]},
        )

    transport = httpx.MockTransport(handler)
    with TradeClient(cache_dir=tmp_path, transport=transport, now=lambda: 1000.0) as client:
        first = client.static()
        second = client.static()
    assert first == second
    assert calls == ["/api/trade/data/static"]
    assert (tmp_path / "trade_static.json").is_file()


def test_seller_url_encodes_narrowed_account_query() -> None:
    original = {"status": {"option": "securable"}, "filters": {"type_filters": {"filters": {"rarity": {"option": "rare"}}}}}
    url = seller_url("Covens", original, "Seller#42")
    assert url.startswith("https://www.pathofexile.com/trade/search/Covens/")
    assert "trade_filters" not in original["filters"]
    decoded = _decode_seller_blob(url)
    assert decoded["status"] == {"option": "securable"}
    assert decoded["filters"]["trade_filters"]["filters"]["account"] == {"input": "Seller#42"}


def test_chaos_rates_maps_trade_currency_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Static:
        def static(self) -> list[dict[str, Any]]:
            return [
                {
                    "id": "Currency",
                    "entries": [
                        {"id": "divine", "text": "Divine Orb"},
                        {"id": "alch", "text": "Orb of Alchemy"},
                    ],
                }
            ]

    ninja = {
        "Currency": [
            {"name": "Divine Orb", "chaos": 200},
            {"name": "Orb of Alchemy", "chaos": 0.07},
            {"name": "Not On Trade", "chaos": 5},
        ]
    }
    monkeypatch.setattr(external_data, "_fetch_dense_overviews", lambda *_a, **_kw: ninja)
    assert external_data.fetch_currency_chaos_rates("Covens") == {
        "Divine Orb": 200.0,
        "Orb of Alchemy": 0.07,
        "Not On Trade": 5.0,
    }
    assert _real_chaos_rates(_Static(), "Covens", None) == {"chaos": 1.0, "divine": 200.0, "alch": 0.07}
    # Either source failing leaves only chaos, so prices in other currencies stay unconverted.
    assert _real_chaos_rates(object(), "Covens", None) == {"chaos": 1.0}


def test_listing_seller_and_age_in_report() -> None:
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    row = {"listing": {"indexed": "2026-11-23T09:30:00Z", "account": {"name": "Seller#1234"}}}
    assert listing_age_hours(row, now) == pytest.approx(2.5)
    assert listing_age_hours({"listing": {"indexed": "garbage"}}, now) is None
    assert listing_age_hours({}, now) is None

    text = "Rarity: Rare\nTest Ring\nCoral Ring\n--------\n+70 to maximum Life"
    plain = Candidate(raw={}, item=parse_item_text(text), item_text=text, price_amount=10, price_currency="chaos")
    assert plain.seller is None and plain.age_hours is None
    assert "Seller" not in format_report(
        build_label="b", slot="ring1", current=None, candidates=[plain], league="Standard"
    )

    recent = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    listed = Candidate(
        raw={"listing": {"indexed": recent, "account": {"name": "Seller#1234"}}},
        item=parse_item_text(text),
        item_text=text,
        price_amount=10,
        price_currency="chaos",
        price_chaos=10.0,
    )
    report = format_report(
        build_label="b", slot="ring1", current=None, candidates=[listed], league="Standard"
    )
    assert "| Seller" in report and "Seller: Seller#1234 | Listed: 3.0h ago" in report
    payload = report_json([listed])[0]
    assert payload["seller"] == "Seller#1234"
    assert payload["age_hours"] == pytest.approx(3.0, abs=0.01)
    assert payload["price"]["chaos"] == 10.0


def test_trade_search_max_age_bargain_and_seller_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")
    monkeypatch.setattr(cli_trade, "_chaos_rates", lambda *_a, **_kw: {"chaos": 1.0, "divine": 200.0})

    # Cheapest page is all 20h old (inside the server's 1-day window, outside --max-age 15);
    # its median price, 100 chaos, is the going price.
    rows = {f"old{i}": _make_row(f"old{i}", 20, 95 + i if i < 5 else 96 + i, "chaos", f"Old#{i}") for i in range(10)}
    rows["undated"] = _make_row("undated", 1, 1, "chaos", "Nobody#0")
    del rows["undated"]["listing"]["indexed"]  # no readable time: not proof of freshness
    rows["divine"] = _make_row("divine", 2, 1, "divine", "Rich#1")
    rows["cheap"] = _make_row("cheap", 3, 50, "chaos", "Seller#2")
    rows["edge"] = _make_row("edge", 4, 80, "chaos", "Edge#3")
    searches: list[dict[str, Any]] = []

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            searches.append(query)
            return {"id": "q1", "total": len(rows), "result": list(rows)}

        def fetch(self, ids: list[str], query_id: str) -> list[dict[str, Any]]:
            return [rows[item_id] for item_id in ids]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)

    args = [
        "trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30",
        "--status", "securable", "--max-age", "15",
        "--sort", "price", "--pob", "off", "--no-compare", "--json",
    ]
    res = CliRunner().invoke(main, [*args, "--bargain", "30"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)

    # The fresh search asks for the 1-day window; the market search drops it.
    assert searches[0]["query"]["filters"]["trade_filters"]["filters"]["indexed"] == {"option": "1day"}
    assert "trade_filters" not in searches[1]["query"].get("filters", {})

    # It kept fetching past the stale first page, and 1 divine does not sort before 50 chaos.
    assert (data["fetched"], data["droppedOld"], data["marketChaos"]) == (14, 11, 100)
    cheap, edge, divine = data["candidates"]
    assert (edge["name"], edge["bargain"]) == ("edge (Slink Gloves)", False)
    assert (cheap["name"], cheap["bargain"], round(cheap["vs_market_pct"])) == ("cheap (Slink Gloves)", True, -50)
    assert (divine["price"]["chaos"], divine["bargain"], round(divine["vs_market_pct"])) == (200, False, 100)

    # Each link is the same search narrowed to that listing's seller.
    linked = _decode_seller_blob(cheap["url"])
    assert linked["filters"]["trade_filters"]["filters"]["account"] == {"input": "Seller#2"}
    assert linked["status"] == {"option": "securable"}
    assert data["staleFallback"] is False

    # Exactly at the threshold counts as a bargain: 80 is 20% below 100.
    at_threshold = json.loads(CliRunner().invoke(main, [*args, "--bargain", "20"]).output)
    assert [c["bargain"] for c in at_threshold["candidates"]] == [True, True, False]

    # --fetch-limit caps the rows even though listings are fetched a page at a time.
    capped = json.loads(CliRunner().invoke(main, [*args, "--fetch-limit", "1"]).output)
    assert (len(capped["candidates"]), capped["droppedOld"]) == (1, 11)


def test_trade_search_plain_run_needs_no_currency_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")
    lookups: list[int] = []
    monkeypatch.setattr(cli_trade, "_chaos_rates", lambda *_a, **_kw: lookups.append(1) or {"chaos": 1.0})

    sample_rows = [
        _make_row("r1", 2, 10, "chaos", "S1#1"),
        _make_row("r2", 3, 20, "chaos", "S2#2"),
    ]

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q1", "total": 2, "result": ["r1", "r2"]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return sample_rows[:limit]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)
    args = ["trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30", "--pob", "off", "--no-compare"]
    assert CliRunner().invoke(main, args).exit_code == 0 and not lookups
    assert CliRunner().invoke(main, [*args, "--sort", "price"]).exit_code == 0 and lookups == [1]


@pytest.mark.parametrize("window_is_empty", [True, False])
def test_trade_search_max_age_falls_back_to_older_listings(
    monkeypatch: pytest.MonkeyPatch, window_is_empty: bool
) -> None:
    """Nothing fresh (window empty, or everything in it too old): list the older ones, clearly marked."""
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")

    old_row = _make_row("old_only", 20, 40, "chaos", "Old#1")
    searches: list[dict[str, Any]] = []

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            searches.append(query)
            windowed = "indexed" in json.dumps(query)
            if windowed and window_is_empty:
                raise NoResults()
            return {"id": "q1", "total": 1, "result": ["old_only"]}

        def fetch(self, ids: list[str], query_id: str) -> list[dict[str, Any]]:
            return [old_row]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)

    args = [
        "trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30",
        "--max-age", "15", "--bargain", "30", "--pob", "off", "--no-compare",
    ]
    res = CliRunner().invoke(main, [*args, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    # Two searches, not three: the fallback already is the "any age" search --bargain needs.
    assert data["staleFallback"] is True and len(searches) == 2
    assert [round(c["age_hours"]) for c in data["candidates"]] == [20]

    # The link must not carry the "listed" window, or it could not find the old listing.
    linked = _decode_seller_blob(data["candidates"][0]["url"])
    assert "indexed" not in json.dumps(linked)

    report = CliRunner().invoke(main, args)
    assert "- Listed: NOTHING within 15h - showing older listings instead" in report.output


def test_trade_search_bargain_reuses_any_age_asc_search_and_guards_under_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")
    monkeypatch.setattr(cli_trade, "_chaos_rates", lambda *_a, **_kw: {"chaos": 1.0})

    # 10 priced listings, any-age asc: reuses single search for market median (100c).
    ten_rows = [_make_row(f"it{i}", 5, 50 if i == 0 else 100, "chaos", f"S#{i}") for i in range(10)]
    calls: list[dict[str, Any]] = []

    class _FakeClient10(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            calls.append(query)
            return {"id": "q10", "total": 10, "result": [r["id"] for r in ten_rows]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return ten_rows[:limit]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient10)
    args = [
        "trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30",
        "--bargain", "30", "--pob", "off", "--no-compare", "--json", "--compact",
    ]
    res = CliRunner().invoke(main, args)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert len(calls) == 1
    assert data["marketChaos"] == 100.0
    assert data["candidates"][0]["name"] == "it0 (Slink Gloves)"
    assert data["candidates"][0]["bargain"] is True

    # Under 5 priced listings: no going price, nothing flagged.
    four_rows = ten_rows[:4]

    class _FakeClient4(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q4", "total": 4, "result": [r["id"] for r in four_rows]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return four_rows[:limit]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient4)
    res4 = CliRunner().invoke(main, args)
    assert res4.exit_code == 0, res4.output
    data4 = json.loads(res4.output)
    assert data4["marketChaos"] is None
    assert all(not c["bargain"] for c in data4["candidates"])

    # At the site's 10000-match cap the cheapest page is not the real floor: nothing flagged.
    class _FakeClientCapped(_FakeClient10):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {**super().search(league, query), "total": 10000}

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClientCapped)
    capped = json.loads(CliRunner().invoke(main, args).output)
    assert capped["marketChaos"] is None
    assert all(not c["bargain"] for c in capped["candidates"])
    report = CliRunner().invoke(main, [a for a in args if a != "--json"]).output
    assert "- Market: no going price: the search matches 10000+ listings" in report


def test_trade_search_bench_craft_preserves_bargain_seller_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit: --bench-craft variants cloned via replace() preserve price_chaos, bargain, seller, age_hours, seller_url."""
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "_resolve_league", lambda *_a, **_kw: "Covens")
    monkeypatch.setattr(cli_trade, "_chaos_rates", lambda *_a, **_kw: {"chaos": 1.0, "divine": 200.0})

    # 9 expensive listings (100c) + 1 bargain listing (50c) with lower raw mod score so bargain promotion is exercised.
    rows: list[dict[str, Any]] = []
    for i in range(9):
        r = _make_row(f"reg{i}", 4, 100, "chaos", f"Reg#{i}")
        r["item"]["explicitMods"] = ["+110 to maximum Life", "+45% to Cold Resistance"]
        rows.append(r)
    bargain_row = _make_row("steal", 2, 50, "chaos", "BargainSeller#7")
    bargain_row["item"]["explicitMods"] = ["+50 to maximum Life", "+32% to Cold Resistance"]
    rows.append(bargain_row)

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "qb", "total": len(rows), "result": [r["id"] for r in rows]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return rows[:limit]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        if action == "items":
            return {
                "raw": "Rarity: RARE\nOld Hand\nSlink Gloves\nItem Level: 85\nImplicits: 0\n+40 to maximum Life\n",
                "buildInfo": {"className": "Ranger", "ascendClassName": "Deadeye", "level": 95},
            }
        if action == "trade_eval_items":
            items = (params or {}).get("items") or []
            return {
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000.0, "totalEhp": 5000.0, "life": 4000.0},
                "results": [
                    {
                        "id": entry["id"],
                        "ok": True,
                        "after": {
                            "primaryDpsKey": "CombinedDPS",
                            "primaryDps": 1150.0 if "{crafted}" in entry["raw"] else 1050.0,
                            "totalEhp": 5100.0,
                            "life": 4050.0,
                        },
                        "diff": {},
                    }
                    for entry in items
                ],
            }
        raise AssertionError(f"unexpected daemon action: {action}")

    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _fake_daemon)

    inspect_report = {
        "certain": True,
        "craftEligible": True,
        "item": {"itemLevel": 85},
        "scaledAffixTypes": [],
        "affixes": [],
        "suggestions": [{"status": "open", "affix": "prefix", "want": "Area Damage", "id": "area"}],
    }
    monkeypatch.setattr(trade_craft.item_inspect, "inspect_item", lambda *a, **k: inspect_report)
    recipe = SimpleNamespace(
        id="area",
        cost="4x Exalted Orb",
        crafted_lines=lambda *, use_min: ["{crafted}16% increased Area Damage"],
        as_dict=lambda: {
            "id": "area",
            "affix": "prefix",
            "text": "(14-16)% increased Area Damage",
            "cost": "4x Exalted Orb",
        },
    )
    monkeypatch.setattr(trade_craft.bench, "search", lambda *a, **k: [SimpleNamespace(recipe=recipe)])

    args = [
        "trade", "search", "--slot", "gloves", "--stat", "to Cold Resistance=30",
        "--bargain", "30", "--bench-craft", "Area Damage", "--pob", "on", "--pob-limit", "1", "--json", "--compact",
    ]
    res = CliRunner().invoke(main, args)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["marketChaos"] == 100.0

    # With --pob-limit 1, ONLY the bargain listing ("steal") should have been expanded & PoB-verified!
    top_two = data["candidates"][:2]
    assert all(c["name"] == "steal (Slink Gloves)" for c in top_two)
    crafted = next(c for c in top_two if c["bench_craft"] is not None)
    original = next(c for c in top_two if c["bench_craft"] is None)

    assert crafted["bargain"] is True
    assert crafted["price"]["chaos"] == 50.0
    assert crafted["vs_market_pct"] == pytest.approx(-50.0)
    assert crafted["seller"] == "BargainSeller#7"
    assert crafted["age_hours"] == pytest.approx(2.0, abs=0.05)
    assert crafted["url"] == original["url"]
    linked = _decode_seller_blob(crafted["url"])
    assert linked["filters"]["trade_filters"]["filters"]["account"] == {"input": "BargainSeller#7"}
    assert crafted["pob"]["dps_after"] == 1150.0


def test_vs_market_text_avoids_negative_zero() -> None:
    from pob_cli.trade_eval.report import _vs_market_text

    item = parse_item_text("Rarity: Rare\nTest Ring\nCoral Ring\n--------\n+50 to maximum Life\n")
    tiny_neg = Candidate(raw={}, item=item, item_text="", vs_market_pct=-0.2, bargain=False)
    assert _vs_market_text(tiny_neg) == "+0%"
    steal = Candidate(raw={}, item=item, item_text="", vs_market_pct=-30.4, bargain=True)
    assert _vs_market_text(steal) == "-30% BARGAIN"
