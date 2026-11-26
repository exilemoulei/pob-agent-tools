"""Unit & CLI tests for `pob timeless search --trade`, multi-conqueror PoB eval, and formatter (Layer 2)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import cli_timeless, cli_timeless_trade
from pob_cli.formatters.timeless_trade_md import format_timeless_trade_markdown
from pob_cli.timeless.multi_conqueror import search_multi_conqueror
from pob_cli.timeless.trade_live import execute_timeless_trade_plan, extract_seed_and_conqueror
from pob_cli.timeless.trade_sharder import build_timeless_trade_plan
from pob_cli.trade_client import CookieRequired, NoResults, RateLimited, TradeError


def _make_search_res(
    *,
    jewel: str = "Lethal Pride",
    conqueror: str = "Kaom",
    seed_count: int = 50,
    dps_base: float = 1_500_000.0,
) -> dict[str, Any]:
    outcomes = [
        {
            "stats": {
                "CombinedDPS": dps_base - i * 10_000.0,
                "TotalEHP": 90_000.0 + i * 250.0,
                "Life": 5000.0,
            },
            "seeds": [10000 + i],
        }
        for i in range(seed_count)
    ]
    return {
        "jewel": jewel, "conqueror": conqueror, "socket": 61419, "mods": [],
        "stat": "CombinedDPS", "seedCount": 8001, "groupCount": seed_count,
        "calculated": seed_count, "workers": 1, "seconds": 0.2,
        "baseline": {"CombinedDPS": 1_000_000.0, "TotalEHP": 80_000.0, "Life": 5000.0},
        "outcomes": outcomes,
        "radiusSummary": {"allocatedNotables": [{"id": 1, "name": "Heart of the Warrior"}], "unallocatedNotables": []},
    }


def _row(
    lid: str,
    mod_line: str,
    *,
    amount: float,
    currency: str = "divine",
    seller: str = "seller_a",
    fee: int | None = 1200,
    hideout_token: str | None = None,
    whisper: str | None = None,
) -> dict[str, Any]:
    listing: dict[str, Any] = {
        "price": {"amount": amount, "currency": currency, "type": "~b/o"},
        "account": {"name": seller},
        "indexed": "2026-11-26T00:00:00Z",
    }
    if fee is not None:
        listing["fee"] = fee
    if hideout_token:
        listing["hideout_token"] = hideout_token
    if whisper:
        listing["whisper"] = whisper
    return {"id": lid, "listing": listing, "item": {"explicitMods": [mod_line]}}


class _FakeTradeClient:
    def __init__(self, responses: list[tuple[str, list[dict[str, Any]]] | Exception], realm: str = "global") -> None:
        self.responses = list(responses)
        self.realm = realm
        self.search_calls: list[tuple[str, dict[str, Any]]] = []
        self.fetch_calls: list[tuple[list[str], str]] = []

    def search(self, league: str, payload: dict[str, Any]) -> dict[str, Any]:
        idx = len(self.search_calls)
        self.search_calls.append((league, payload))
        if idx >= len(self.responses):
            raise NoResults("securable")
        item = self.responses[idx]
        if isinstance(item, Exception):
            raise item
        qid, rows = item
        return {"id": qid, "total": len(rows), "result": [r["id"] for r in rows]}

    def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
        self.fetch_calls.append((list(ids[:limit]), query_id))
        for resp in self.responses:
            if not isinstance(resp, Exception) and resp[0] == query_id:
                by_id = {r["id"]: r for r in resp[1]}
                return [by_id[i] for i in ids[:limit] if i in by_id]
        return []

    def search_url(self, league: str, query_id: str) -> str:
        host = "https://pathofexile.tw" if self.realm == "tw" else "https://www.pathofexile.com"
        return f"{host}/trade/search/{league}/{query_id}"


def test_early_stop_halts_at_batch_2_and_never_calls_batch_3() -> None:
    plan = build_timeless_trade_plan(_make_search_res(seed_count=72), league="Covens", realm="global")
    assert len(plan["shards"]) >= 3
    s0, s1 = plan["shards"][0]["seeds"], plan["shards"][1]["seeds"]

    batch1_rows = [
        _row("id1", f"Commanded leadership over {s0[0]} warriors under Kaom", amount=2.0, seller="s1", fee=900),
        _row("id2", f"Commanded leadership over {s0[1]} warriors under Kaom", amount=1.5, seller="s2", fee=800),
    ]
    batch2_rows = [
        _row("id3", f"Commanded leadership over {s1[0]} warriors under Kaom", amount=1.0, seller="s3", fee=700),
        _row("id4", f"Commanded leadership over {s1[1]} warriors under Kaom", amount=3.0, seller="s4", fee=1100),
        _row("id5", f"Commanded leadership over {s1[2]} warriors under Kaom", amount=2.5, seller="s5", fee=950),
    ]
    fake = _FakeTradeClient([("q1", batch1_rows), ("q2", batch2_rows), ("q3", [])])

    res = execute_timeless_trade_plan(
        plan, client=fake, max_trade_batches=3, trade_limit=5, max_per_seller=2, rates={"chaos": 1.0, "divine": 150.0}
    )

    assert len(fake.search_calls) == 2
    assert res["batchesExecuted"] == 2
    assert res["earlyStopped"] is True
    assert res["totalMatchedBeforeLimit"] == 5
    assert res["matchedCount"] == 5
    assert [m["seed"] for m in res["matches"][:3]] == [s0[0], s0[1], s1[0]]
    assert res["matches"][0]["dpsPerDivine"] == pytest.approx(250_000.0)


def test_trade_limit_caps_sorted_matches_and_records_total_before_limit() -> None:
    plan = build_timeless_trade_plan(_make_search_res(seed_count=10), league="Covens", realm="global")
    seeds = plan["shards"][0]["seeds"]
    rows = [
        _row("r5", f"Commanded leadership over {seeds[4]} warriors under Kaom", amount=1.0, seller="s5", fee=500),
        _row("r1_hi", f"Commanded leadership over {seeds[0]} warriors under Kaom", amount=5.0, seller="s1b", fee=500),
        _row("r3", f"Commanded leadership over {seeds[2]} warriors under Kaom", amount=1.0, seller="s3", fee=500),
        _row("r1_lo", f"Commanded leadership over {seeds[0]} warriors under Kaom", amount=1.0, seller="s1a", fee=500),
        _row("r4", f"Commanded leadership over {seeds[3]} warriors under Kaom", amount=1.0, seller="s4", fee=500),
        _row("r2", f"Commanded leadership over {seeds[1]} warriors under Kaom", amount=2.0, seller="s2", fee=500),
    ]
    fake = _FakeTradeClient([("q1", rows)])
    res = execute_timeless_trade_plan(
        plan, client=fake, max_trade_batches=2, trade_limit=3, max_per_seller=2, rates={"chaos": 1.0, "divine": 150.0}
    )

    assert res["earlyStopped"] is True
    assert res["totalMatchedBeforeLimit"] == 6
    assert res["matchedCount"] == 3
    assert [m["listingId"] for m in res["matches"]] == ["r1_lo", "r1_hi", "r2"]


def test_post_ranking_seller_cap_retains_highest_gain_seed_and_in_person_vocab() -> None:
    plan = build_timeless_trade_plan(_make_search_res(seed_count=10), league="Covens", realm="global")
    seeds = plan["shards"][0]["seeds"]
    rows = [
        # Lower-gain seed arrives first from whale, higher-gain seed arrives later -> post-ranking cap must keep seeds[0] & seeds[1]!
        _row("lo_first", f"Commanded leadership over {seeds[4]} warriors under Kaom", amount=1.0, seller="whale", fee=1000),
        _row("mid", f"Commanded leadership over {seeds[1]} warriors under Kaom", amount=2.0, seller="whale", fee=None, hideout_token="tok_xyz"),
        _row("hi_later", f"Commanded leadership over {seeds[0]} warriors under Kaom", amount=3.0, seller="whale", fee=1400),
        _row("hi_clone", f"Commanded leadership over {seeds[0]} warriors under Kaom", amount=3.0, seller="whale", fee=1400),
        # In-person listing (no fee, no hideout_token) -> tradeMode == "in_person"
        _row("inp", f"Commanded leadership over {seeds[3]} warriors under Kaom", amount=50.0, currency="chaos", seller="other", fee=None, whisper="@other hi"),
        # Unknown high-tier currency rejected when budget is set
        _row("unk", f"Commanded leadership over {seeds[2]} warriors under Kaom", amount=1.0, currency="mirror", seller="rich", fee=1000),
    ]
    fake = _FakeTradeClient([("q1", rows)])
    res = execute_timeless_trade_plan(
        plan, client=fake, max_trade_batches=1, trade_limit=10, max_per_seller=2,
        budget=5.0, currency="divine", rates={"chaos": 1.0, "divine": 150.0},
    )

    assert res["sellerCappedDropped"] == 1
    assert res["matchedCount"] == 3
    by_id = {m["listingId"]: m for m in res["matches"]}
    assert set(by_id) == {"hi_later", "mid", "inp"}
    assert by_id["hi_later"]["tradeMode"] == "securable" and by_id["hi_later"]["goldFee"] == 1400
    assert by_id["hi_later"]["sellerDuplicatesCollapsed"] == 1
    assert by_id["mid"]["tradeMode"] == "securable"
    assert by_id["inp"]["tradeMode"] == "in_person" and by_id["inp"]["goldFee"] is None


def test_multi_conqueror_any_evaluates_all_3_and_attributes_exact_gains() -> None:
    seen_conqs: list[str | None] = []
    dps_by_conq = {"Doryani": 1_200_000.0, "Xibaqua": 1_100_000.0, "Mazcotl": 1_450_000.0}

    def _fake_search(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        c = kwargs["conqueror"]
        seen_conqs.append(c)
        return _make_search_res(jewel="Glorious Vanity", conqueror=c, seed_count=2, dps_base=dps_by_conq[c])

    merged = search_multi_conqueror(
        "CODE", "<PathOfBuilding/>", 61419, "Glorious Vanity", conqueror="any", top=6, search_fn=_fake_search
    )
    assert seen_conqs == ["Doryani", "Xibaqua", "Mazcotl"]
    assert merged["conqueror"] == "any"
    assert merged["conquerorsEvaluated"] == ["Doryani", "Xibaqua", "Mazcotl"]
    assert merged["outcomes"][0]["conqueror"] == "Mazcotl"

    plan = build_timeless_trade_plan(merged, league="Covens", realm="global", conqueror="any")
    rows = [
        _row("m1", "Bathed in the blood of 10000 sacrificed in the name of Ahuana", amount=2.0, seller="s1", fee=900),
        _row("d1", "Bathed in the blood of 10000 sacrificed in the name of Doryani", amount=1.0, seller="s2", fee=900),
    ]
    fake = _FakeTradeClient([("q1", rows)])
    res = execute_timeless_trade_plan(plan, client=fake, max_trade_batches=1, trade_limit=5, rates={"chaos": 1.0, "divine": 150.0})
    assert [(m["conqueror"], m["deltaDps"]) for m in res["matches"]] == [
        ("Mazcotl", pytest.approx(450_000.0)),
        ("Doryani", pytest.approx(200_000.0)),
    ]


def test_mazcotl_and_zh_tw_seed_extraction_and_matching() -> None:
    assert extract_seed_and_conqueror({"explicitMods": ["緬懷由 4321 位梅德偉(沃拉娜-梅德偉)血脈所傳誦的壯舉"]}) == (4321, "Medved")
    assert extract_seed_and_conqueror({"explicitMods": ["浸泡在以阿呼阿娜之名獻祭的 5555 條生命中"]}) == (5555, "Mazcotl")

    plan = build_timeless_trade_plan(
        _make_search_res(jewel="Glorious Vanity", conqueror="Mazcotl", seed_count=5),
        league="Standard", realm="tw",
    )
    target_seed = plan["shards"][0]["seeds"][0]
    rows = [
        _row("tw1", f"浸泡在以阿呼阿娜之名獻祭的 {target_seed} 條生命中", amount=1.0, seller="tw_seller", fee=2500),
        _row("en1", f"Bathed in the blood of {target_seed} sacrificed in the name of Ahuana", amount=2.0, seller="en_seller", fee=1500),
    ]
    fake = _FakeTradeClient([("qtw", rows)], realm="tw")
    res = execute_timeless_trade_plan(plan, client=fake, max_trade_batches=1, trade_limit=5, rates={"chaos": 1.0, "divine": 150.0})
    assert res["matchedCount"] == 2
    assert all(m["conqueror"] == "Mazcotl" and m["seed"] == target_seed for m in res["matches"])


def test_zero_http_urls_only_and_pob_offline_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = build_timeless_trade_plan(_make_search_res(seed_count=30), league="Covens", realm="global")

    class _BoomClient:
        def search(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("HTTP must not be called in urls_only / offline mode")

    res = execute_timeless_trade_plan(plan, client=_BoomClient(), max_trade_batches=0, trade_limit=5)
    assert res["mode"] == "urls_only" and res["batchesExecuted"] == 0 and len(res["batches"]) == len(plan["shards"])

    # POB_OFFLINE=1 degrades live --trade (default max_trade_batches=3) cleanly to urls_only with 0 HTTP
    monkeypatch.setenv("POB_OFFLINE", "1")
    monkeypatch.setattr(cli_timeless, "_daemon_call", lambda action, params: {"code": "FAKE_CODE", "xml": "<PathOfBuilding/>"})
    monkeypatch.setattr(cli_timeless, "search", lambda *args, **kwargs: _make_search_res(seed_count=5))
    runner = CliRunner()
    cli_res = runner.invoke(
        cli_timeless.timeless_group,
        ["search", "--jewel", "Lethal Pride", "--socket", "61419", "--trade", "--league", "Covens", "--json"],
    )
    assert cli_res.exit_code == 0, cli_res.output
    payload = json.loads(cli_res.stdout)
    assert payload["trade"]["mode"] == "urls_only"
    assert payload["trade"]["offlineFallback"] is True
    assert "POB_OFFLINE=1" in cli_res.stderr


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (CookieRequired(403, "cf"), 3),
        (RateLimited(30), 4),
        (TradeError("upstream 502"), 5),
    ],
)
def test_trade_errors_preserve_pob_table_and_fallback_urls_with_exact_exit_code(
    monkeypatch: pytest.MonkeyPatch, exc: TradeError, expected_code: int
) -> None:
    monkeypatch.delenv("POB_OFFLINE", raising=False)
    monkeypatch.setattr(cli_timeless, "_daemon_call", lambda action, params: {"code": "FAKE_CODE", "xml": "<PathOfBuilding/>"})
    monkeypatch.setattr(cli_timeless, "search", lambda *args, **kwargs: _make_search_res(seed_count=4))
    monkeypatch.setattr(cli_timeless_trade, "resolve_timeless_trade_league", lambda *a, **kw: ("Covens", None))

    def _boom_exec(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(cli_timeless_trade, "execute_timeless_trade_plan", _boom_exec)
    runner = CliRunner()
    res = runner.invoke(
        cli_timeless.timeless_group,
        ["search", "--jewel", "Lethal Pride", "--socket", "61419", "--trade", "--league", "Covens", "--json"],
    )
    assert res.exit_code == expected_code
    payload = json.loads(res.stdout)
    assert len(payload["outcomes"]) == 4
    assert payload["trade"]["error"]["exitCode"] == expected_code
    assert len(payload["trade"]["batches"]) >= 1


def test_trade_only_flags_without_trade_rejected_and_markdown_socket_column(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    bad = runner.invoke(
        cli_timeless.timeless_group,
        ["search", "--jewel", "Lethal Pride", "--socket", "61419", "--budget", "5"],
    )
    assert bad.exit_code != 0
    assert "require --trade" in bad.output

    monkeypatch.setenv("POB_LANG", "zh-CN")
    sample_trade = {
        "realm": "global", "league": "Covens", "status": "securable", "budget": 5.0, "currency": "divine",
        "mode": "live", "totalShards": 1, "batchesExecuted": 1, "earlyStopped": False, "sellerCappedDropped": 0,
        "matches": [{
            "seed": 10000, "conqueror": "Kaom", "socket": 26725, "socketLabel": "Marauder",
            "priceAmount": 2.0, "priceCurrency": "divine", "tradeMode": "securable", "goldFee": 1400,
            "deltaDps": 500_000.0, "deltaDpsPct": 50.0, "deltaEhp": 10_000.0, "deltaEhpPct": 12.5,
            "dpsPerDivine": 250_000.0, "keyNotables": ["Heart of the Warrior"], "seller": "seller_a",
            "sellerDuplicatesCollapsed": 0, "url": "https://www.pathofexile.com/trade/search/Covens/abc",
        }],
        "batches": [{"batch": 1, "seedCount": 5, "filterCount": 5, "complexity": 82, "seeds": [10000], "url": "https://example.com", "executed": True, "total": 1, "matched": 1, "unfetchedCount": 0}],
    }
    md = format_timeless_trade_markdown(sample_trade)
    assert "[一口价 · 1,400G]" in md
    assert "| Socket" in md and "#26725 (Marauder)" in md


def test_abyss_timeless_jewel_en_and_zh_emit_progress_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB_OFFLINE", "1")
    monkeypatch.setattr(cli_timeless, "_daemon_call", lambda action, params: {"code": "FAKE_CODE", "xml": "<PathOfBuilding/>"})
    monkeypatch.setattr(
        cli_timeless, "search",
        lambda *args, **kwargs: _make_search_res(jewel="Festering Vengeance", conqueror="Tecrod", seed_count=2),
    )
    runner = CliRunner()
    for jewel_arg in ("Festering Vengeance", "溃烂复仇"):
        res = runner.invoke(
            cli_timeless.timeless_group,
            ["search", "--jewel", jewel_arg, "--socket", "61419", "--json"],
        )
        assert res.exit_code == 0, res.output
        assert "~7,900 ungrouped seeds" in res.stderr
