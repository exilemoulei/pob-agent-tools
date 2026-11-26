"""Offline tests for Layer 1: `--securable`, buyout/gold-fee tagging, price-floor divergence, and seller dedupe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from pob_cli import cli_trade
from pob_cli.cli import main
from pob_cli.cli_trade.fresh_bargain import _market_chaos
from pob_cli.trade_client import NoResults, TradeClient
from pob_cli.trade_client.league_meta import extract_listing_trade_meta
from pob_cli.trade_eval import (
    Candidate,
    compute_price_floor_note,
    dedupe_candidates_by_seller,
    format_report,
    report_json,
)
from pob_cli.trade_item import parse_item_text
from pob_cli.trade_query import ZH_STAT_ALIASES as TQ_ZH
from pob_cli.trade_query import translate_stat_query as tq_translate
from pob_cli.trade_query.aliases import ZH_STAT_ALIASES as ALIAS_ZH
from pob_cli.trade_query.aliases import translate_stat_query as alias_translate
from pob_cli.trade_query.zh_aliases import ZH_STAT_ALIASES as DIRECT_ZH
from pob_cli.trade_query.zh_aliases import translate_stat_query as direct_translate

MOCK_STATS: list[dict[str, Any]] = [
    {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
    {"id": "explicit.stat_4220027924", "text": "+#% to Cold Resistance", "type": "explicit"},
]


def _mock_trade_stats(*_a: Any, **_kw: Any) -> tuple[list[dict[str, Any]], str]:
    return MOCK_STATS, "mock"


def _row(
    row_id: str,
    *,
    amount: float,
    currency: str = "chaos",
    seller: str = "Seller#1",
    fee: Any = None,
    hideout_token: str | None = None,
    whisper: str | None = None,
    price_type: str = "~b/o",
    life: int = 80,
    cold_res: int = 40,
) -> dict[str, Any]:
    indexed = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    listing: dict[str, Any] = {
        "indexed": indexed,
        "account": {"name": seller} if seller else {},
        "price": {"type": price_type, "amount": amount, "currency": currency},
    }
    if fee is not None:
        listing["fee"] = fee
    if hideout_token is not None:
        listing["hideout_token"] = hideout_token
    if whisper is not None:
        listing["whisper"] = whisper
    return {
        "id": row_id,
        "item": {
            "name": row_id,
            "typeLine": "Slink Gloves",
            "baseType": "Slink Gloves",
            "frameType": 2,
            "ilvl": 85,
            "identified": True,
            "explicitMods": [f"+{life} to maximum Life", f"+{cold_res}% to Cold Resistance"],
        },
        "listing": listing,
    }


def test_zh_aliases_split_reexports_are_identical() -> None:
    assert TQ_ZH is ALIAS_ZH is DIRECT_ZH
    assert tq_translate is alias_translate is direct_translate
    assert direct_translate("法压") == "+#% chance to Suppress Spell Damage"


def test_securable_flag_and_status_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    runner = CliRunner()

    res = runner.invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "maximum Life=70", "--securable", "--dump-query"],
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["query"]["query"]["status"] == {"option": "securable"}

    same = runner.invoke(
        main,
        [
            "trade", "search", "--slot", "gloves", "--stat", "maximum Life=70",
            "--securable", "--status", "securable", "--dump-query",
        ],
    )
    assert same.exit_code == 0, same.output

    conflict = runner.invoke(
        main,
        [
            "trade", "search", "--slot", "gloves", "--stat", "maximum Life=70",
            "--securable", "--status", "available", "--dump-query",
        ],
    )
    assert conflict.exit_code == 1
    assert "--securable conflicts with --status" in conflict.output

    neg_cap = runner.invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "maximum Life=70", "--max-per-seller", "-1", "--dump-query"],
    )
    assert neg_cap.exit_code == 1
    assert "--max-per-seller" in neg_cap.output


def test_no_results_reflects_queried_status() -> None:
    assert "No matching securable listings" in str(NoResults("securable"))
    assert "No matching available listings" in str(NoResults("available"))
    assert "No matching listings" in str(NoResults())

    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={"id": "q0", "total": 0, "result": []}))
    with TradeClient(transport=transport) as client:
        with pytest.raises(NoResults, match="No matching securable listings"):
            client.search("Covens", {"query": {"status": {"option": "securable"}}})


def _meta_tuple(row: dict[str, Any]) -> tuple[str, int | None, str | None]:
    meta = extract_listing_trade_meta(row)
    return meta["tradeMode"], meta["goldFee"], meta["priceType"]


def test_cookie_invariant_buyout_detection() -> None:
    # Anonymous Merchant's Tab listing: fee present, no hideout_token
    assert _meta_tuple(_row("a", amount=2, fee=2280)) == ("securable", 2280, "~b/o")
    # Online Merchant's Tab listing with both fee and whisper
    assert _meta_tuple(_row("b", amount=2, fee=2280, whisper="@s hi")) == ("securable", 2280, "~b/o")
    # Guard: fee == 0 alongside whisper is treated as in_person (not instant buyout)
    assert _meta_tuple(_row("b0", amount=2, fee=0, whisper="@seller hi")) == ("in_person", None, "~b/o")
    # Guard: fee == 0 with hideout_token still counts as securable
    assert _meta_tuple(_row("b1", amount=2, fee=0, hideout_token="tok")) == ("securable", 0, "~b/o")
    # Cookie listing with hideout_token only
    assert _meta_tuple(_row("c", amount=5, hideout_token="tok_123", price_type="~price")) == (
        "securable",
        None,
        "~price",
    )
    # Traditional whisper listing (never carries fee)
    assert _meta_tuple(_row("d", amount=1, whisper="@s hi")) == ("in_person", None, "~b/o")
    # Malformed boolean fee is ignored
    assert _meta_tuple(_row("e", amount=1, fee=True)) == ("in_person", None, "~b/o")


@pytest.mark.parametrize(
    ("lang", "instant_fee", "instant_nofee", "whisper"),
    [
        ("zh-CN", "2c [一口价 · 2,280G]", "3c [一口价]", "1c [私聊]"),
        ("zh-TW", "2c [一口價 · 2,280G]", "3c [一口價]", "1c [私聊]"),
        ("en", "2c [Instant · 2,280G]", "3c [Instant]", "1c [Whisper]"),
    ],
)
def test_markdown_buyout_tags_and_json_fields_by_locale(
    monkeypatch: pytest.MonkeyPatch,
    lang: str,
    instant_fee: str,
    instant_nofee: str,
    whisper: str,
) -> None:
    monkeypatch.setenv("POB_LANG", lang)
    item = parse_item_text("Rarity: RARE\nDoom Hand\nSlink Gloves\nImplicits: 0\n+80 to maximum Life")
    c_fee = Candidate(
        raw=_row("a", amount=2, fee=2280, seller="Mer#1"),
        item=item, item_text="", price_amount=2, price_currency="chaos", price_chaos=2.0,
        trade_mode="securable", gold_fee=2280, price_type="~b/o", seller_duplicates_collapsed=2,
    )
    c_nofee = Candidate(
        raw=_row("b", amount=3, hideout_token="tok", seller="Mer#2"),
        item=item, item_text="", price_amount=3, price_currency="chaos", price_chaos=3.0,
        trade_mode="securable", gold_fee=None, price_type="~price",
    )
    c_whisp = Candidate(
        raw=_row("c", amount=1, seller="Whisp#1"),
        item=item, item_text="", price_amount=1, price_currency="chaos", price_chaos=1.0,
        trade_mode="in_person", gold_fee=None, price_type="~b/o",
    )
    md = format_report(
        build_label="test", slot="gloves", current=None,
        candidates=[c_fee, c_nofee, c_whisp], league="Covens", status="available",
    )
    assert instant_fee in md
    assert instant_nofee in md
    assert whisper in md
    assert "Mer#1 (+2 dup)" in md
    assert "- Price floor: Instant Buyout floor: 2c (+2,280G) vs Whisper-only floor: 1c" in md

    js = report_json([c_fee, c_whisp])
    assert js[0]["tradeMode"] == "securable"
    assert js[0]["goldFee"] == 2280
    assert js[0]["priceType"] == "~b/o"
    assert js[0]["sellerDuplicatesCollapsed"] == 2
    assert js[1]["tradeMode"] == "in_person"
    assert js[1]["goldFee"] is None


def test_price_floor_divergence_thresholds() -> None:
    item = parse_item_text("Rarity: RARE\nA\nSlink Gloves\nImplicits: 0\n+70 to maximum Life")
    sec = Candidate(raw={}, item=item, item_text="", price_amount=10, price_currency="chaos", price_chaos=10.0, trade_mode="securable", gold_fee=1500)
    inp_low = Candidate(raw={}, item=item, item_text="", price_amount=6, price_currency="chaos", price_chaos=6.0, trade_mode="in_person")
    inp_near = Candidate(raw={}, item=item, item_text="", price_amount=7, price_currency="chaos", price_chaos=7.0, trade_mode="in_person")

    assert compute_price_floor_note([sec, inp_low], status="available") == (
        "Instant Buyout floor: 10c (+1,500G) vs Whisper-only floor: 6c"
    )
    assert compute_price_floor_note([sec, inp_near], status="available") is None
    assert compute_price_floor_note([sec, inp_low], status="securable") is None
    assert compute_price_floor_note([sec], status="available") is None


def test_market_chaos_one_seller_one_vote_and_securable_denoising() -> None:
    rates = {"chaos": 1.0}
    # 1 spammer posts 8 x 1c whisper items; 5 legitimate distinct sellers post at 100c
    spammer_rows = [_row(f"spam{i}", amount=1, seller="PriceFixer#1") for i in range(8)]
    legit_rows = [_row(f"legit{i}", amount=100, seller=f"Legit#{i}") for i in range(5)]
    # 1-account-1-vote -> [1, 100, 100, 100, 100, 100] -> median 100.0
    assert _market_chaos(spammer_rows + legit_rows, rates) == 100.0

    # On `available`: 5 distinct whisper price-fixers at 1c + 5 distinct securable listings at 50c
    whisper_fixers = [_row(f"w{i}", amount=1, seller=f"Fixer#{i}") for i in range(5)]
    securable_real = [_row(f"s{i}", amount=50, seller=f"Shop#{i}", fee=2000) for i in range(5)]
    assert _market_chaos(whisper_fixers + securable_real, rates, status="available") == 50.0
    # Fewer than 5 distinct securable sellers falls back to all distinct sellers
    assert _market_chaos(whisper_fixers + securable_real[:4], rates, status="available") == 1.0


def test_candidate_dedupe_clones_and_max_per_seller_before_pob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_trade_stats)
    monkeypatch.setattr(cli_trade, "resolve_trade_league", lambda *_a, **_kw: ("Covens", None))
    monkeypatch.setattr(cli_trade, "_chaos_rates", lambda *_a, **_kw: {"chaos": 1.0})

    # Spammer has 3 identical clones + 3 more distinct items; Legit has 2 distinct items
    rows = [
        _row("clone1", amount=10, seller="Spam#1", fee=1000, life=95, cold_res=45),
        _row("clone2", amount=10, seller="Spam#1", fee=1000, life=95, cold_res=45),
        _row("clone3", amount=10, seller="Spam#1", fee=1000, life=95, cold_res=45),
        _row("spam_d2", amount=11, seller="Spam#1", fee=1000, life=94, cold_res=45),
        _row("spam_d3", amount=12, seller="Spam#1", fee=1000, life=93, cold_res=45),
        _row("spam_d4", amount=13, seller="Spam#1", fee=1000, life=92, cold_res=45),
        _row("legit1", amount=15, seller="Legit#1", fee=1200, life=90, cold_res=45),
        _row("whisper_low", amount=1, seller="Whisp#1", life=70, cold_res=35),
    ]

    class _FakeClient(TradeClient):
        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            return {"id": "q1", "total": len(rows), "result": [r["id"] for r in rows]}

        def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
            return rows[:limit]

    monkeypatch.setattr(cli_trade, "TradeClient", _FakeClient)

    seen_pob_count: list[int] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        if action == "trade_eval_items":
            items = (params or {}).get("items") or []
            seen_pob_count.append(len(items))
            return {
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1000.0, "totalEhp": 5000.0, "life": 4000.0},
                "results": [
                    {
                        "id": entry["id"],
                        "ok": True,
                        "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 1050.0, "totalEhp": 5100.0, "life": 4050.0},
                        "diff": {},
                    }
                    for entry in items
                ],
            }
        raise AssertionError(f"unexpected daemon action: {action}")

    monkeypatch.setattr(cli_trade, "_daemon_call_safe", _fake_daemon)

    res = CliRunner().invoke(
        main,
        [
            "trade", "search", "--slot", "gloves", "--stat", "maximum Life=70",
            "--no-compare", "--pob", "on", "--pob-limit", "3", "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    # Default --max-per-seller 2: Spam#1 gets 2 slots (clone1 with +2 dup, spam_d2); Legit#1 gets the 3rd PoB slot!
    assert seen_pob_count == [3]
    names = [c["name"] for c in data["candidates"]]
    assert "clone1 (Slink Gloves)" in names
    assert "spam_d2 (Slink Gloves)" in names
    assert "spam_d3 (Slink Gloves)" not in names
    assert "legit1 (Slink Gloves)" in names
    clone_cand = next(c for c in data["candidates"] if c["name"] == "clone1 (Slink Gloves)")
    assert clone_cand["sellerDuplicatesCollapsed"] == 2
    assert data["sellerCappedDropped"] == 2  # spam_d3 and spam_d4 dropped by default --max-per-seller 2
    assert data["priceFloorNote"] == "Instant Buyout floor: 10c (+1,000G) vs Whisper-only floor: 1c"

    # Markdown report surfaces the seller-capped count explicitly (never silent)
    res_md = CliRunner().invoke(
        main,
        ["trade", "search", "--slot", "gloves", "--stat", "maximum Life=70", "--no-compare", "--pob", "off"],
    )
    assert res_md.exit_code == 0, res_md.output
    assert "- Seller cap: 2 additional listing(s) hidden (cap: 2/seller; pass --max-per-seller 0 for unlimited)" in res_md.output

    # --max-per-seller 0 disables the per-seller cap (still collapses exact clones)
    res0 = CliRunner().invoke(
        main,
        [
            "trade", "search", "--slot", "gloves", "--stat", "maximum Life=70",
            "--no-compare", "--pob", "off", "--max-per-seller", "0", "--json",
        ],
    )
    assert res0.exit_code == 0, res0.output
    data0 = json.loads(res0.output)
    assert data0["sellerCappedDropped"] == 0
    names0 = [c["name"] for c in data0["candidates"]]
    assert "spam_d3 (Slink Gloves)" in names0 and "spam_d4 (Slink Gloves)" in names0
    assert "clone2 (Slink Gloves)" not in names0

    # Direct helper check: anonymous sellers are never capped
    item = parse_item_text("Rarity: RARE\nAnon\nSlink Gloves\nImplicits: 0\n+70 to maximum Life")
    anons = [Candidate(raw={}, item=item, item_text="", price_amount=i, price_currency="chaos") for i in range(4)]
    assert len(dedupe_candidates_by_seller(anons, max_per_seller=1)) == 4
