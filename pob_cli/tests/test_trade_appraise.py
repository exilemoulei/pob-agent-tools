"""Tests for Layer 4: 3-tier build valuation (`pob trade appraise` and `shopping-list --live`)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data

from pob_cli.cli_trade import trade_group
from pob_cli.trade_client import RateLimited
from pob_cli.trade_eval.appraise import appraise_build, compute_deduped_p25_quote
from pob_cli.trade_query import load_local_trade_stats

_STATS: list[dict[str, Any]] = [
    {"id": "explicit.stat_124131830", "text": "+# to Level of all Spell Skill Gems", "type": "explicit"},
    {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
    {"id": "explicit.stat_3372524247", "text": "+#% to Fire Resistance", "type": "explicit"},
    {"id": "explicit.stat_1873457881", "text": "#% additional Physical Damage Reduction while affected by Determination", "type": "explicit"},
    {"id": "explicit.stat_2690790844", "text": "#% increased Life Recovery Rate while affected by Vitality", "type": "explicit"},
    {"id": "explicit.stat_1190333629|47389", "text": "Allocates Profane Bloom if you have the matching modifier on Forbidden Flesh", "type": "explicit"},
    {"id": "explicit.stat_2763429652", "text": "Rightmost # Magic Utility Flasks constantly apply their Flask Effects to you", "type": "explicit"},
]

_FAKE_DENSE: dict[str, list[dict[str, Any]]] = {
    "Currency": [{"name": "Divine Orb", "chaos": 150.0}],
    "UniqueWeapon": [{"name": "Legacy of the Rose", "chaos": 100.0}],
    "UniqueAccessory": [
        {"name": "Ashes of the Stars", "chaos": 1500.0},
        {"name": "Mageblood", "chaos": 30000.0},
    ],
    "UniqueFlask": [{"name": "Progenesis", "chaos": 6000.0}],
    "UniqueJewel": [{"name": "Watcher's Eye", "chaos": 300.0}],
    "ForbiddenJewel": [{"name": "Forbidden Flame", "variant": "Profane Bloom", "chaos": 1500.0}],
    "Oil": [
        {"name": "Amber Oil", "chaos": 1.0},
        {"name": "Verdant Oil", "chaos": 2.0},
        {"name": "Azure Oil", "chaos": 3.0},
    ],
}

_FAKE_STASH: dict[str, list[dict[str, Any]]] = {
    "SkillGem": [
        {
            "name": "Lightning Tendrils of Eccentricity",
            "gemLevel": 21,
            "gemQuality": 20,
            "corrupted": True,
            "chaosValue": 600.0,
        },
        {
            "name": "Enlighten Support",
            "gemLevel": 4,
            "gemQuality": 0,
            "corrupted": True,
            "chaosValue": 3000.0,
        },
    ],
    "UniqueWeapon": [{"name": "Legacy of the Rose", "links": 6, "chaosValue": 450.0}],
    "Oil": _FAKE_DENSE["Oil"],
}


def _row(amount: float, currency: str, seller: str, *, fee: int | None = 12000, securable: bool = True) -> dict[str, Any]:
    listing: dict[str, Any] = {
        "price": {"amount": amount, "currency": currency, "type": "~b/o"},
        "account": {"name": seller},
    }
    if securable:
        listing["fee"] = fee
        listing["hideout_token"] = "tok"
    else:
        listing["whisper"] = "@seller hi"
    return {"id": f"id_{seller}_{amount}", "listing": listing, "item": {}}


def _sample_build() -> tuple[dict[str, Any], dict[str, Any]]:
    items_res = {
        "buildInfo": {"className": "Witch", "ascendClassName": "Occultist", "level": 97},
        "equippedSlots": [
            {
                "slotName": "Weapon 1",
                "itemId": 1,
                "raw": (
                    "Rarity: UNIQUE\nLegacy of the Rose\nJudgement Staff\nSockets: B-B-B-B-B-B\n"
                    "Implicits: 0\n+5 to Level of all Spell Skill Gems\n"
                ),
            },
            {
                "slotName": "Body Armour",
                "itemId": 2,
                "raw": "Rarity: RARE\nSoul Coat\nVaal Regalia\nImplicits: 0\n+110 to maximum Life\n+42% to Fire Resistance\n",
            },
            {
                "slotName": "Amulet",
                "itemId": 3,
                "raw": "Rarity: UNIQUE\nAshes of the Stars\nOnyx Amulet\nImplicits: 1\n{enchant}Allocates Executioner\n",
            },
            {
                "slotName": "Flask 1",
                "isFlask": True,
                "itemId": 4,
                "raw": "Rarity: UNIQUE\nProgenesis\nAmethyst Flask\nImplicits: 0\n",
            },
            {
                "slotName": "Jewel 1",
                "nodeId": 26725,
                "socketIndex": 1,
                "allocated": True,
                "itemId": 5,
                "raw": (
                    "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 0\n"
                    "8% additional Physical Damage Reduction while affected by Determination\n"
                    "30% increased Life Recovery Rate while affected by Vitality\n"
                ),
            },
            {
                "slotName": "Jewel 2",
                "nodeId": 2491,
                "socketIndex": 2,
                "allocated": True,
                "itemId": 6,
                "raw": (
                    "Rarity: UNIQUE\nForbidden Flame\nCrimson Jewel\nImplicits: 0\n"
                    "Allocates Profane Bloom if you have the matching modifier on Forbidden Flesh\n"
                ),
            },
        ],
    }
    skills_res = {
        "groups": [
            {
                "index": 1,
                "slot": "Weapon 1",
                "enabled": True,
                "gems": [
                    {"nameSpec": "Lightning Tendrils of Eccentricity", "level": 21, "quality": 20, "enabled": True, "isSupport": False},
                    {"nameSpec": "Enlighten", "level": 4, "quality": 0, "enabled": True, "isSupport": True},
                ],
            }
        ]
    }
    return items_res, skills_res


class _FakeTradeClient:
    def __init__(self, *, rate_limit_after: int | None = None) -> None:
        self.calls = 0
        self.rate_limit_after = rate_limit_after
        self.realm = "global"

    def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
        if self.rate_limit_after is not None and self.calls >= self.rate_limit_after:
            raise RateLimited(45)
        self.calls += 1
        return {"id": f"q{self.calls}", "result": [f"r{i}" for i in range(6)], "total": 6}

    def fetch_many(self, ids: list[str], query_id: str, limit: int = 10) -> list[dict[str, Any]]:
        # Includes a 1c price-fixer spamming 3 listings + 4 real securable sellers!
        return [
            _row(1, "chaos", "fixer_bot"),
            _row(1, "chaos", "fixer_bot"),
            _row(1, "chaos", "fixer_bot"),
            _row(10, "divine", "real_seller_1", fee=14500),
            _row(11, "divine", "real_seller_2", fee=15000),
            _row(12, "divine", "real_seller_3", fee=15500),
            _row(14, "divine", "real_seller_4", fee=16000),
        ]

    def __enter__(self) -> _FakeTradeClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def test_p25_anti_price_fixer_1_seller_1_vote() -> None:
    rates = {"chaos": 1.0, "divine": 150.0}
    rows = [
        _row(1, "chaos", "fixer_spam"),
        _row(1, "chaos", "fixer_spam"),
        _row(1, "chaos", "fixer_spam"),
        _row(1, "chaos", "fixer_spam"),
        _row(800, "chaos", "honest_a", fee=11000),
        _row(850, "chaos", "honest_b", fee=12000),
        _row(900, "chaos", "honest_c", fee=13000),
        _row(950, "chaos", "honest_d", fee=14000),
    ]
    quote = compute_deduped_p25_quote(rows, rates)
    assert quote is not None
    assert quote["sampleSellers"] == 5
    # Index 1 of [1, 800, 850, 900, 950] -> 800c (never the lone 1c fixer!)
    assert quote["chaos"] == 800.0
    assert quote["tradeMode"] == "securable"
    assert quote["goldFee"] == 11000


def test_appraise_3tier_live_p25_and_tier_c_unpriced() -> None:
    items_res, skills_res = _sample_build()
    client = _FakeTradeClient()
    res = appraise_build(
        items_res,
        skills_res,
        _STATS,
        league="Mirage",
        max_live_queries=8,
        client=client,
        dense_override=_FAKE_DENSE,
        stash_override=_FAKE_STASH,
    )
    assert res["rateLimitedFallback"] is False
    assert res["divineRate"] == 150.0
    assert res["liveQueriesUsed"] == 3  # Forbidden Flame, Watcher's Eye, 6L Legacy of the Rose
    assert res["tierCounts"]["B_live"] == 3
    assert res["tierCounts"]["C"] == 1
    assert res["unpricedRareSlots"] == ["Body Armour"]

    by_slot = {it["slot"]: it for it in res["items"] if it["section"] != "gem"}
    assert by_slot["Body Armour"]["searchCommand"] == "pob trade search --slot body_armour --securable"
    assert by_slot["Jewel #1"]["liveVerified"] is True
    assert by_slot["Jewel #1"]["liveChaos"] == 1500.0  # 10 div * 150c (skipping 1c fixer!)
    assert by_slot["Jewel #1"]["goldFee"] == 14500
    assert by_slot["Jewel #1"]["deltaVsFloorChaos"] == 1200.0
    assert by_slot["Jewel #1"]["sellerUrl"].startswith("https://www.pathofexile.com/trade/search/Mirage/")
    assert by_slot["Flask 1"]["tier"] == "A"
    assert by_slot["Flask 1"]["priceChaos"] == 6000.0
    assert res["totalChaos"] > 10000.0
    assert res["totalDivine"] == round(res["totalChaos"] / 150.0, 2)


def test_appraise_rate_limited_mid_batch_graceful_fallback() -> None:
    items_res, skills_res = _sample_build()
    client = _FakeTradeClient(rate_limit_after=1)
    res = appraise_build(
        items_res,
        skills_res,
        _STATS,
        league="Mirage",
        max_live_queries=8,
        client=client,
        dense_override=_FAKE_DENSE,
        stash_override=_FAKE_STASH,
    )
    assert res["rateLimitedFallback"] is True
    assert res["liveQueriesUsed"] == 1
    assert res["tierCounts"]["B_live"] == 1
    assert res["tierCounts"]["B_fallback"] == 2
    # Fallback items still keep their poe.ninja floor price and L0 URL
    fb_items = [it for it in res["items"] if it["tier"] == "B" and not it["liveVerified"]]
    assert all(it["priceChaos"] is not None and it["url"] for it in fb_items)


def test_appraise_max_live_queries_zero_offline_mode_and_cli() -> None:
    items_res, skills_res = _sample_build()
    runner = CliRunner()

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        return items_res if action == "items" else skills_res

    with (
        patch("pob_cli.cli_trade._daemon_call_safe", side_effect=_fake_daemon),
        patch("pob_cli.cli_trade.shopping_cmd.load_local_trade_stats", return_value=_STATS),
        patch("pob_cli.trade_eval.appraise._fetch_dense_overviews", return_value=_FAKE_DENSE),
        patch(
            "pob_cli.trade_eval.appraise._fetch_stash_overviews_batch",
            return_value=(_FAKE_STASH, set(), set()),
        ),
        patch("pob_cli.trade_eval.appraise.fetch_currency_chaos_rates", return_value={"Divine Orb": 150.0}),
    ):
        res_json = runner.invoke(
            trade_group,
            ["appraise", "--league", "Mirage", "--max-live-queries", "0", "--json", "--compact"],
        )
        assert res_json.exit_code == 0, res_json.output
        payload = json.loads(res_json.output[res_json.output.index("{") :])
        assert payload["liveQueriesUsed"] == 0
        assert payload["tierCounts"]["B_live"] == 0
        assert payload["unpricedRareSlots"] == ["Body Armour"]

        res_md = runner.invoke(trade_group, ["appraise", "--league", "Mirage", "--max-live-queries", "0"])
        assert res_md.exit_code == 0, res_md.output
        assert "PoE1 Build Appraisal" in res_md.output
        assert "Tier C: Crafted Rares" in res_md.output

        # `shopping-list --live` delegates to appraise cleanly
        with patch("pob_cli.cli_trade.TradeClient", return_value=_FakeTradeClient()):
            res_live = runner.invoke(
                trade_group,
                ["shopping-list", "--league", "Mirage", "--live", "--max-live-queries", "2"],
            )
            assert res_live.exit_code == 0, res_live.output
            assert "PoE1 Build Appraisal" in res_live.output
            assert "[一口价" in res_live.output


def test_appraise_dense_variant_awakened_lightning_penetration_tier_a() -> None:
    """Verify dense["SkillGem"] string `variant` ('5/20', '5/20c', '5') matches Tier A without stash ints."""
    items_res = {"buildInfo": {"className": "Witch", "level": 95}, "equippedSlots": []}
    skills_res = {
        "groups": [
            {
                "index": 1,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {
                        "nameSpec": "Awakened Lightning Penetration",
                        "level": 5,
                        "quality": 20,
                        "enabled": True,
                        "support": True,
                    },
                    {
                        "nameSpec": "Awakened Enlighten",
                        "level": 5,
                        "quality": 0,
                        "enabled": True,
                        "support": True,
                    },
                ],
            }
        ]
    }
    dense_only = {
        "Currency": [{"name": "Divine Orb", "chaos": 150.0}],
        "SkillGem": [
            {"name": "Awakened Lightning Penetration Support", "variant": "1/0", "chaos": 40.0},
            {"name": "Awakened Lightning Penetration Support", "variant": "5/20", "chaos": 750.0},
            {"name": "Awakened Lightning Penetration Support", "variant": "5/20c", "chaos": 620.0},
            {"name": "Awakened Enlighten Support", "variant": "4", "chaos": 12000.0},
            {"name": "Awakened Enlighten Support", "variant": "5c", "chaos": 55000.0},
        ],
    }
    res = appraise_build(
        items_res,
        skills_res,
        _STATS,
        league="Covens",
        max_live_queries=0,
        dense_override=dense_only,
        stash_override={},
    )
    gems = {it["name"]: it for it in res["items"]}
    lp = gems["Awakened Lightning Penetration Support"]
    assert lp["tier"] == "A"
    assert lp["ninjaFloorChaos"] == 750.0
    assert lp["priceChaos"] == 750.0

    enl = gems["Awakened Enlighten Support"]
    assert enl["tier"] == "A"
    assert enl["ninjaFloorChaos"] == 55000.0


def test_real_engine_appraise_anoint_and_offline_valuation() -> None:
    engine_root = find_engine_data()
    if engine_root is None:
        pytest.skip("No local PoB engine found")
    items_res, skills_res = _sample_build()
    entries = load_local_trade_stats(pob_root=engine_root)
    assert entries
    res = appraise_build(
        items_res,
        skills_res,
        entries,
        league="Mirage",
        max_live_queries=0,
        pob_dir=engine_root,
        dense_override=_FAKE_DENSE,
        stash_override=_FAKE_STASH,
    )
    assert res["anoint"]["oils"] == ["Amber Oil", "Verdant Oil", "Azure Oil"]
    assert res["anoint"]["priceChaos"] == 6.0
    assert res["sectionTotals"]["anoint"]["chaos"] == 6.0


def test_appraise_tw_realm_static_fallback_unpriced_count_and_live_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pob_cli.formatters.shopping_md import format_appraise_markdown
    from pob_cli.trade_client import TradeError
    from pob_cli.trade_eval.appraise_ninja import price_anoint

    monkeypatch.delenv("POB_TW_DIVINE_CHAOS", raising=False)
    items_res, skills_res = _sample_build()
    res_off = appraise_build(
        items_res,
        skills_res,
        _STATS,
        league="Standard",
        realm="tw",
        max_live_queries=0,
    )
    assert res_off["divineRateSource"] == "static_fallback"
    assert res_off["unpricedCount"] == len(res_off["items"])
    assert any("poe.ninja does not index TW" in w for w in res_off.get("warnings") or ())
    md_off = format_appraise_markdown(res_off)
    assert "static fallback; poe.ninja does not index TW" in md_off
    assert "unpriced item(s)" in md_off

    # With live queries enabled on TW, unpriced Tier A items (e.g. Progenesis) get promoted to live checks
    tw_only_tier_a = {
        "buildInfo": {"className": "Witch", "level": 95},
        "equippedSlots": [
            {
                "slotName": "Flask 1",
                "isFlask": True,
                "raw": "Rarity: UNIQUE\nProgenesis\nAmethyst Flask\nImplicits: 0\n",
            }
        ],
    }
    client = _FakeTradeClient()
    res_live = appraise_build(
        tw_only_tier_a,
        None,
        _STATS,
        league="Standard",
        realm="tw",
        max_live_queries=2,
        client=client,
    )
    assert res_live["liveQueriesUsed"] == 1
    assert res_live["items"][0]["liveVerified"] is True
    assert res_live["unpricedCount"] == 0

    # Partial oil prices never emit a bogus total anoint cost
    partial_anoint = price_anoint(
        {"slot": "Amulet Anoint", "notable": "Test", "oils": ["Amber Oil", "Golden Oil"]},
        {"Oil": [{"name": "Amber Oil", "chaos": 1.0}]},
        {},
        150.0,
    )
    assert partial_anoint is not None
    assert partial_anoint["priceChaos"] is None
    assert partial_anoint["oilPrices"][0]["chaos"] == 1.0

    # Generic TradeError still consumes live query budget (never exceeds max_live_queries)
    class _FailingTradeClient:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            raise TradeError("boom")

    failing = _FailingTradeClient()
    res_err = appraise_build(
        items_res,
        skills_res,
        _STATS,
        league="Mirage",
        max_live_queries=1,
        client=failing,
        dense_override=_FAKE_DENSE,
        stash_override=_FAKE_STASH,
    )
    assert failing.calls == 1
    assert res_err["liveQueriesUsed"] == 1


def test_appraise_gem_variant_no_1_0_fallback_and_defining_mods_guard() -> None:
    items_res = {
        "buildInfo": {"className": "Witch", "level": 95},
        "equippedSlots": [
            {
                "slotName": "Jewel 1",
                "nodeId": 26725,
                "socketIndex": 1,
                "allocated": True,
                "raw": (
                    "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 0\n"
                    "8% additional Physical Damage Reduction while affected by Determination\n"
                ),
            }
        ],
    }
    skills_res = {
        "groups": [
            {
                "index": 1,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {
                        "nameSpec": "Awakened Lightning Penetration",
                        "level": 5,
                        "quality": 20,
                        "enabled": True,
                        "support": True,
                    },
                ],
            }
        ]
    }
    dense_only_1_0 = {
        "Currency": [{"name": "Divine Orb", "chaos": 150.0}],
        "UniqueJewel": [{"name": "Watcher's Eye", "chaos": 300.0}],
        "SkillGem": [
            {"name": "Awakened Lightning Penetration Support", "variant": "1/0", "chaos": 40.0},
        ],
    }
    client = _FakeTradeClient()
    # Empty stat catalog leaves Watcher's Eye defining mods unresolved -> must skip live search!
    res = appraise_build(
        items_res,
        skills_res,
        [],
        league="Covens",
        max_live_queries=4,
        client=client,
        dense_override=dense_only_1_0,
        stash_override={},
    )
    by_name = {it["name"]: it for it in res["items"]}
    # 5/20 Awakened gem must NOT fall back to 1/0 (40c); instead it promotes to Tier B live check!
    lp = by_name["Awakened Lightning Penetration Support"]
    assert lp["ninjaFloorChaos"] is None
    assert lp["liveVerified"] is True
    # Watcher's Eye with 0 resolved defining modifiers must NOT be live-priced as a bare unique!
    we = by_name["Watcher's Eye"]
    assert we["definingModsUnresolved"] is True
    assert we["liveVerified"] is False
    assert we["liveStatus"] == "skipped_defining_mods_unresolved"
    assert client.calls == 1  # only the gem was live-queried, never the unresolved Watcher's Eye


def test_appraise_multi_chunk_distinct_sellers_beats_10_clone_fixer_and_single_seller_md() -> None:
    from pob_cli.formatters.shopping_md import format_appraise_markdown

    class _ChunkedFixerClient:
        def __init__(self, *, single_seller_only: bool = False) -> None:
            self.single_seller_only = single_seller_only
            self.fetch_chunks: list[list[str]] = []

        def search(self, league: str, query: dict[str, Any]) -> dict[str, Any]:
            ids = [f"id_{i}" for i in range(15)]
            return {"id": "q_chunk", "result": ids, "total": len(ids)}

        def fetch(self, ids: list[str], query_id: str) -> list[dict[str, Any]]:
            self.fetch_chunks.append(list(ids))
            rows: list[dict[str, Any]] = []
            for item_id in ids:
                idx = int(item_id.split("_")[1])
                if idx < 10 or self.single_seller_only:
                    rows.append(_row(1, "chaos", "mega_fixer"))
                else:
                    rows.append(_row(10 + (idx - 10), "divine", f"honest_{idx}", fee=12000))
            return rows

    items_res = {
        "buildInfo": {"className": "Witch", "level": 95},
        "equippedSlots": [
            {
                "slotName": "Jewel 1",
                "nodeId": 26725,
                "socketIndex": 1,
                "allocated": True,
                "raw": (
                    "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nImplicits: 0\n"
                    "8% additional Physical Damage Reduction while affected by Determination\n"
                ),
            }
        ],
    }
    skills_res = {
        "groups": [
            {
                "index": 1,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {"nameSpec": "Enlighten", "level": 4, "quality": 0, "enabled": True, "support": True},
                    {"nameSpec": "Enlighten", "level": 4, "quality": 0, "enabled": True, "support": True},
                ],
            }
        ]
    }
    c_multi = _ChunkedFixerClient(single_seller_only=False)
    res_multi = appraise_build(
        items_res,
        skills_res,
        _STATS,
        league="Mirage",
        max_live_queries=4,
        client=c_multi,
        dense_override=_FAKE_DENSE,
        stash_override=_FAKE_STASH,
    )
    assert len(c_multi.fetch_chunks) == 2
    we_m = next(it for it in res_multi["items"] if it["name"] == "Watcher's Eye")
    assert we_m["sampleSellers"] == 6
    assert we_m["liveChaos"] == 1500.0  # 10 div * 150c, NOT the 10x 1c fixer!

    c_single = _ChunkedFixerClient(single_seller_only=True)
    res_single = appraise_build(
        items_res,
        skills_res,
        _STATS,
        league="Mirage",
        max_live_queries=4,
        client=c_single,
        dense_override=_FAKE_DENSE,
        stash_override=_FAKE_STASH,
    )
    we_s = next(it for it in res_single["items"] if it["name"] == "Watcher's Eye")
    assert we_s["singleListingWarning"] is True
    md = format_appraise_markdown(res_single)
    assert "[1 seller]" in md
    assert "4/0 x2" in md
