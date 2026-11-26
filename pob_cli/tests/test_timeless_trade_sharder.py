"""Unit tests for `pob_cli.timeless.trade_data` & `trade_sharder` (Layer 1 — offline, zero HTTP)."""

from __future__ import annotations

import base64
import gzip
import json
from typing import Any

import pytest

from pob_cli.timeless.trade_data import parse_devotion_mods, parse_timeless_mod_line
from pob_cli.timeless.trade_sharder import (
    DEFAULT_COMPLEXITY_LIMIT,
    MAX_FILTERS_PER_COUNT_GROUP,
    build_single_seed_query,
    build_timeless_trade_plan,
    canonical_conqueror_key,
    conqueror_trade_stat_id,
    lookup_seed_outcome,
    pob_conqueror_name,
    resolve_conqueror_stat_ids,
    resolve_devotion_stat_ids,
)
from pob_cli.trade_query.auto_query import query_complexity


def _decode_trade_url_query(url: str) -> dict[str, Any]:
    encoded = url.rstrip("/").rsplit("/", 1)[-1]
    pad = "=" * (-len(encoded) % 4)
    raw = gzip.decompress(base64.urlsafe_b64decode(encoded + pad))
    return json.loads(raw.decode("utf-8"))


def _fake_search_result(
    *,
    jewel: str = "Lethal Pride",
    conqueror: str = "Kaom",
    seed_count: int = 60,
    mods: list[str] | None = None,
) -> dict[str, Any]:
    outcomes = [
        {
            "stats": {
                "CombinedDPS": 1_200_000.0 - idx * 5_000.0,
                "TotalEHP": 85_000.0 + idx * 100.0,
                "Life": 5000.0,
            },
            "seeds": [10000 + idx * 7],
        }
        for idx in range(seed_count)
    ]
    return {
        "jewel": jewel,
        "conqueror": conqueror,
        "socket": 61419,
        "mods": list(mods or []),
        "stat": "CombinedDPS",
        "baseline": {"CombinedDPS": 1_000_000.0, "TotalEHP": 80_000.0, "Life": 5000.0},
        "outcomes": outcomes,
        "radiusSummary": {
            "allocatedNotables": [{"id": 1, "name": "Devotion", "nameZh": "奉献"}],
            "unallocatedNotables": [],
        },
    }


def test_mazcotl_ahuana_forward_and_reverse_mapping() -> None:
    assert conqueror_trade_stat_id("Mazcotl") == "explicit.pseudo_timeless_jewel_ahuana"
    assert conqueror_trade_stat_id("mazcotl") == "explicit.pseudo_timeless_jewel_ahuana"
    assert conqueror_trade_stat_id("Ahuana") == "explicit.pseudo_timeless_jewel_ahuana"
    assert conqueror_trade_stat_id("阿呼阿娜") == "explicit.pseudo_timeless_jewel_ahuana"
    assert canonical_conqueror_key("Mazcotl (Immortal Ambition)") == "ahuana"
    assert canonical_conqueror_key("explicit.pseudo_timeless_jewel_ahuana") == "ahuana"
    assert pob_conqueror_name("ahuana") == "Mazcotl"
    assert pob_conqueror_name("explicit.pseudo_timeless_jewel_ahuana") == "Mazcotl"

    assert resolve_conqueror_stat_ids("Glorious Vanity", "Mazcotl") == (["Mazcotl"], ["explicit.pseudo_timeless_jewel_ahuana"])
    assert resolve_conqueror_stat_ids("Glorious Vanity", "Ahuana") == (["Mazcotl"], ["explicit.pseudo_timeless_jewel_ahuana"])


def test_conqueror_validation_is_strictly_jewel_scoped() -> None:
    with pytest.raises(ValueError, match="Unknown conqueror"):
        resolve_conqueror_stat_ids("Lethal Pride", "Doryani")
    with pytest.raises(ValueError, match="Unknown conqueror"):
        resolve_conqueror_stat_ids("Lethal Pride", "Ahuana")
    with pytest.raises(ValueError, match="Unknown conqueror"):
        resolve_conqueror_stat_ids("Festering Vengeance", "Kaom")
    assert resolve_conqueror_stat_ids("Heroic Tragedy", "any") == (
        ["Vorana", "Uhtred", "Medved"],
        ["explicit.pseudo_timeless_jewel_vorana", "explicit.pseudo_timeless_jewel_uhtred", "explicit.pseudo_timeless_jewel_medved"],
    )
    assert resolve_conqueror_stat_ids("Festering Vengeance", "any") == (["Tecrod"], ["explicit.pseudo_timeless_jewel_tecrod"])


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Bathed in the blood of 4321 sacrificed in the name of Ahuana", (4321, "Mazcotl")),
        ("Carved to glorify 5100 new faithful converted by High Templar Dominus", (5100, "Dominus")),
        ("Commanded leadership over 12345 warriors under Kaom", (12345, "Kaom")),
        ("Commissioned 42000 coins to commemorate Cadiro", (42000, "Cadiro")),
        ("Denoted service of 1888 dekhara in the akhara of Balbala", (1888, "Balbala")),
        ("Remembrancing 2222 songworthy deeds by the line of Vorana", (2222, "Vorana")),
        ("Subjugating 3333 souls in the thrall of Tecrod", (3333, "Tecrod")),
        ("Binding 4444 souls to phylacteries to sustain Zorath", (4444, "Zorath")),
        ("浸泡在以阿呼阿娜之名獻祭的 5555 條生命中", (5555, "Mazcotl")),
        ("為了禮讚 6123 名受到神主感化的信眾所雕刻", (6123, "Dominus")),
        ("岡姆指揮領導超過 14567 戰士", (14567, "Kaom")),
        ("授銜 88000 個硬幣以紀念卡迪羅", (88000, "Cadiro")),
        ("777 位部屬宣誓服從於貝爾巴拉的血脈", (777, "Balbala")),
        ("緬懷由 4321 位梅德偉(沃拉娜-梅德偉)血脈所傳誦的壯舉", (4321, "Medved")),
        ("在特克羅德的奴役下征服 2468 位靈魂", (2468, "Tecrod")),
        ("將 3579 位靈魂縛於命匣以維持澤洛斯", (3579, "Zorath")),
    ],
)
def test_parse_all_8_en_and_zh_timeless_mod_lines(line: str, expected: tuple[int, str]) -> None:
    assert parse_timeless_mod_line(line) == expected


def test_dynamic_sharding_single_vs_any_vs_militant_faith_never_exceeds_budget() -> None:
    single_plan = build_timeless_trade_plan(
        _fake_search_result(jewel="Lethal Pride", conqueror="Kaom", seed_count=60),
        league="Covens", realm="global", status="securable",
    )
    any_plan = build_timeless_trade_plan(
        _fake_search_result(jewel="Lethal Pride", conqueror="Kaom", seed_count=60),
        league="Covens", realm="global", conqueror="any", status="securable",
    )
    mf_plan = build_timeless_trade_plan(
        _fake_search_result(
            jewel="Militant Faith", conqueror="Dominus", seed_count=60,
            mods=["4% increased Area Damage per 10 Devotion", "1% increased effect of Non-Curse Auras per 10 Devotion"],
        ),
        league="Covens", realm="global", budget=5.0, currency="divine",
    )

    assert set(mf_plan["devotionStatIds"]) == {"explicit.stat_1724614884", "explicit.stat_2585926696"}
    for plan in (single_plan, any_plan, mf_plan):
        assert sum(s["seedCount"] for s in plan["shards"]) == 60
        for shard in plan["shards"]:
            assert shard["countFilterCount"] <= MAX_FILTERS_PER_COUNT_GROUP
            assert shard["complexity"] <= DEFAULT_COMPLEXITY_LIMIT
            assert query_complexity(shard["payload"]) <= DEFAULT_COMPLEXITY_LIMIT

    assert any_plan["shards"][0]["seedCount"] * 3 == single_plan["shards"][0]["seedCount"]
    assert mf_plan["shards"][0]["seedCount"] < single_plan["shards"][0]["seedCount"]
    assert mf_plan["shards"][0]["payload"]["query"]["filters"]["trade_filters"]["filters"]["price"] == {"option": "divine", "max": 5.0}


def test_exact_conqueror_seed_lookup_never_cross_attributes() -> None:
    kaom_plan = build_timeless_trade_plan(
        _fake_search_result(jewel="Lethal Pride", conqueror="Kaom", seed_count=3),
        league="Covens", realm="global",
    )
    assert lookup_seed_outcome(kaom_plan, 10000, "Kaom") is not None
    assert lookup_seed_outcome(kaom_plan, 10000, "Rakiata") is None
    assert lookup_seed_outcome(kaom_plan, 10000) is not None

    # True multi-conqueror outcomes keep distinct PoB gains for the same seed under different conquerors
    multi_conq_res = {
        "jewel": "Glorious Vanity", "conqueror": "any", "socket": 61419, "stat": "CombinedDPS",
        "baseline": {"CombinedDPS": 1_000_000.0, "TotalEHP": 80_000.0},
        "outcomes": [
            {"conqueror": "Mazcotl", "stats": {"CombinedDPS": 1_300_000.0, "TotalEHP": 90_000.0}, "seeds": [5000]},
            {"conqueror": "Doryani", "stats": {"CombinedDPS": 1_150_000.0, "TotalEHP": 110_000.0}, "seeds": [5000]},
        ],
    }
    mc_plan = build_timeless_trade_plan(multi_conq_res, league="Covens", realm="global", conqueror="any")
    assert lookup_seed_outcome(mc_plan, 5000, "Ahuana")["deltaDps"] == pytest.approx(300_000.0)  # type: ignore[index]
    assert lookup_seed_outcome(mc_plan, 5000, "阿呼阿娜")["deltaDps"] == pytest.approx(300_000.0)  # type: ignore[index]
    assert lookup_seed_outcome(mc_plan, 5000, "Doryani")["deltaDps"] == pytest.approx(150_000.0)  # type: ignore[index]
    assert lookup_seed_outcome(mc_plan, 5000, "Xibaqua") is None
    # Emits only the 2 actually evaluated (conqueror, seed) filters, not Xibaqua
    emitted_ids = [f["id"] for f in mc_plan["shards"][0]["payload"]["query"]["stats"][0]["filters"]]
    assert emitted_ids == ["explicit.pseudo_timeless_jewel_ahuana", "explicit.pseudo_timeless_jewel_doryani"]


def test_singleton_budget_validation_raises_value_error() -> None:
    res = _fake_search_result(jewel="Lethal Pride", conqueror="Kaom", seed_count=2)
    with pytest.raises(ValueError, match="exceeds Trade API budget"):
        build_timeless_trade_plan(res, league="Covens", conqueror="any", max_filters_per_group=2)
    with pytest.raises(ValueError, match="exceeds Trade API budget"):
        build_timeless_trade_plan(res, league="Covens", complexity_limit=50)


def test_multi_socket_per_socket_baseline_and_unmatched_devotion_mods() -> None:
    multi_sock = {
        "jewel": "Militant Faith", "conqueror": "Dominus", "stat": "CombinedDPS",
        "mods": ["4% increased Area Damage per 10 Devotion", "Unknown Custom Line"],
        "baseline": {"CombinedDPS": 1_000_000.0, "TotalEHP": 80_000.0},
        "socketResults": [
            {"socket": 100, "conqueror": "Dominus", "baseline": {"CombinedDPS": 1_000_000.0, "TotalEHP": 80_000.0}, "outcomes": []},
            {"socket": 200, "conqueror": "Dominus", "baseline": {"CombinedDPS": 900_000.0, "TotalEHP": 70_000.0}, "outcomes": []},
        ],
        "topSeeds": [
            {"socket": 200, "socketLabel": "Witch", "stats": {"CombinedDPS": 1_100_000.0, "TotalEHP": 75_000.0}, "seeds": [3000]},
            {"socket": 100, "socketLabel": "Marauder", "stats": {"CombinedDPS": 1_050_000.0, "TotalEHP": 82_000.0}, "seeds": [3000]},
        ],
    }
    plan = build_timeless_trade_plan(multi_sock, league="Covens", realm="global")
    assert plan["unmatchedDevotionMods"] == ["Unknown Custom Line"]
    assert parse_devotion_mods("Militant Faith", ["Non-Curse Aura Effect", "Mystery"]) == (["explicit.stat_2585926696"], ["Mystery"])
    assert resolve_devotion_stat_ids("Lethal Pride", ["4% increased Area Damage per 10 Devotion"]) == []
    hit = lookup_seed_outcome(plan, 3000, "Dominus")
    assert hit is not None
    assert hit["socket"] == 200 and hit["socketLabel"] == "Witch"
    assert hit["deltaDps"] == pytest.approx(200_000.0)


def test_tw_realm_league_mapping_unmapped_fields_and_untruncated_default_shards() -> None:
    res = _fake_search_result(jewel="Glorious Vanity", conqueror="Mazcotl", seed_count=150)
    plan = build_timeless_trade_plan(res, league="Covens", realm="tw", status="securable")

    assert plan["league"] == "巫影密會"
    assert plan["unmappedTwFields"] == []
    assert len(plan["shards"]) > 5
    assert plan["truncatedShardCandidates"] == 0
    decoded = _decode_trade_url_query(plan["shards"][0]["url"])
    assert decoded["name"] == "輝煌的虛榮"
    assert decoded["type"] == "永恆珠寶"

    capped = build_timeless_trade_plan(res, league="Standard", realm="tw", max_shards=2)
    assert len(capped["shards"]) == 2
    assert capped["truncatedShardCandidates"] == 150 - capped["shardedSeedCount"]
    single_q = build_single_seed_query(capped, 10000, "Mazcotl")
    assert single_q["stats"][0]["filters"] == [
        {"id": "explicit.pseudo_timeless_jewel_ahuana", "value": {"min": 10000, "max": 10000}}
    ]


def test_tw_realm_unmapped_ascii_league_emits_warning_entry() -> None:
    res = _fake_search_result(jewel="Glorious Vanity", conqueror="Mazcotl", seed_count=2)
    plan = build_timeless_trade_plan(res, league="FutureUnmappedLeague", realm="tw", status="securable")
    assert plan["league"] == "FutureUnmappedLeague"
    assert {"field": "league", "value": "FutureUnmappedLeague"} in plan["unmappedTwFields"]
