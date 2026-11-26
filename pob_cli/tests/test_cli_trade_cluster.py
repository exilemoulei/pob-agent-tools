"""CLI tests for `pob trade cluster` 8p2s Front-2 Back-1 Large Cluster Jewel solver."""

from __future__ import annotations

import base64
import gzip
import json
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data

from pob_cli.cli_trade import trade_group


def _decode_b64gzip_query_id(query_id: str) -> dict[str, Any]:
    pad = "=" * ((4 - len(query_id) % 4) % 4)
    raw = gzip.decompress(base64.urlsafe_b64decode(query_id + pad))
    return json.loads(raw.decode("utf-8"))


def test_cli_trade_cluster_zero_http_lightning_front2_back1(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.cli_trade as ct

    monkeypatch.setattr(ct, "resolve_trade_league", lambda league, **kw: (league or "Mirage", None))

    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        [
            "cluster",
            "--enchant",
            "12% increased Lightning Damage",
            "--notables",
            "Sadist,Snowstorm",
            "--league",
            "Mirage",
            "--budget",
            "5",
            "--currency",
            "divine",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output[res.output.index("{") :])
    assert data["mode"] == "zero_http"
    assert data["possible"] is True
    assert data["league"] == "Mirage"
    assert data["url"].startswith("https://www.pathofexile.com/trade/search/Mirage/")

    q = data["query"]["query"]
    assert "type" not in q
    assert q["status"] == {"option": "securable"}
    assert q["filters"]["type_filters"]["filters"]["category"] == {"option": "jewel.cluster"}
    assert q["filters"]["trade_filters"]["filters"]["price"] == {"option": "divine", "max": 5.0}

    # Group 0: AND (8 passives, 2 sockets, enchant option 14, Sadist, Snowstorm)
    and_grp = q["stats"][0]
    assert and_grp["type"] == "and"
    and_by_id = {f["id"]: f["value"] for f in and_grp["filters"]}
    assert and_by_id["enchant.stat_3086156145"] == {"min": 8, "max": 8}
    assert and_by_id["enchant.stat_4079888060"] == {"min": 2, "max": 2}
    assert and_by_id["enchant.stat_3948993189"] == {"option": 14}
    assert "explicit.stat_3638731729" in and_by_id  # Sadist
    assert "explicit.stat_1595367309" in and_by_id  # Snowstorm

    # Group 1: COUNT min=1 over the 3 legal Suffix back-seat notables
    cnt_grp = q["stats"][1]
    assert cnt_grp["type"] == "count"
    assert cnt_grp["value"] == {"min": 1}
    cnt_ids = {f["id"] for f in cnt_grp["filters"]}
    assert cnt_ids == {
        "explicit.stat_228455793",   # Doryani's Lesson
        "explicit.stat_3206911230",  # Disorienting Display
        "explicit.stat_1678643716",  # Widespread Destruction
    }

    # Verify self-contained gzip+urlsafe_b64 URL round-trips identically
    decoded = _decode_b64gzip_query_id(data["encodedQueryId"])
    assert decoded == q


def test_cli_trade_cluster_bilingual_zh_and_tw_realm(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.cli_trade as ct

    monkeypatch.setattr(ct, "resolve_trade_league", lambda league, **kw: ("標準模式", None))

    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        [
            "cluster",
            "--enchant",
            "闪电伤害",
            "--notables",
            "多里亚尼之训，暴风雪",
            "--realm",
            "tw",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output[res.output.index("{") :])
    assert data["possible"] is True
    assert data["realm"] == "tw"
    assert data["url"].startswith("https://pathofexile.tw/trade/search/")
    assert "type" not in data["query"]["query"]
    # Only Prismatic Heart is a valid Prefix filler between Doryani's Lesson (Suffix) and Snowstorm (Prefix)
    cnt_ids = [f["id"] for f in data["query"]["query"]["stats"][1]["filters"]]
    assert cnt_ids == ["explicit.stat_2342448236"]


def test_cli_trade_cluster_impossible_combo_explains_and_provides_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pob_cli.cli_trade as ct

    monkeypatch.setattr(ct, "resolve_trade_league", lambda league, **kw: ("Standard", None))

    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        [
            "cluster",
            "--enchant",
            "Lightning Damage",
            "--notables",
            "Snowstorm,Scintillating Idea",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "IMPOSSIBLE" in res.output
    assert "affix_cap_conflict" in res.output
    assert "2-Notable Fallback Link" in res.output


def test_cli_trade_cluster_live_mock_buyout_table(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.cli_trade as ct

    captured: dict[str, Any] = {}

    class FakeClient:
        base_url = "https://www.pathofexile.com"

        def __init__(self, **kw: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def search(self, league: str, body: dict[str, Any]) -> dict[str, Any]:
            captured["league"] = league
            captured["body"] = body
            return {"id": "Q123", "total": 2, "result": ["L1", "L2"]}

        def fetch_many(self, ids: list[str], search_id: str, limit: int = 10) -> list[dict[str, Any]]:
            captured["limit"] = limit
            return [
                {
                    "id": "L1",
                    "listing": {
                        "price": {"amount": 2, "currency": "divine", "type": "~b/o"},
                        "fee": 4200,
                        "hideout_token": "tok_abc",
                        "account": {"name": "ClusterCrafter"},
                        "whisper": "@ClusterCrafter Hi",
                    },
                    "item": {
                        "ilvl": 75,
                        "explicitMods": [
                            "1 Added Passive Skill is Sadist",
                            "1 Added Passive Skill is Widespread Destruction",
                            "1 Added Passive Skill is Snowstorm",
                        ],
                    },
                },
                {
                    "id": "L2",
                    "listing": {
                        "price": {"amount": 3.5, "currency": "divine", "type": "~b/o"},
                        "account": {"name": "SparkRunner"},
                        "whisper": "@SparkRunner Hi",
                    },
                    "item": {
                        "ilvl": 68,
                        "explicitMods": [
                            "1 Added Passive Skill is Sadist",
                            "1 Added Passive Skill is Doryani's Lesson",
                            "1 Added Passive Skill is Snowstorm",
                        ],
                    },
                },
            ]

    monkeypatch.setattr(ct, "TradeClient", FakeClient)
    monkeypatch.setattr(ct, "resolve_trade_league", lambda league, **kw: (league or "Mirage", None))

    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        [
            "cluster",
            "--enchant",
            "Lightning Damage",
            "--notables",
            "Sadist,Snowstorm",
            "--live",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output[res.output.index("{") :])
    assert data["mode"] == "live"
    assert captured["league"] == "Mirage"
    live = data["live"]
    assert live["total"] == 2
    assert live["listings"][0]["backNotable"] == "Widespread Destruction"
    assert live["listings"][0]["tradeMode"] == "securable"
    assert live["listings"][0]["goldFee"] == 4200
    assert live["listings"][0]["seller"] == "ClusterCrafter"
    assert live["listings"][1]["backNotable"] == "Doryani's Lesson"
    assert live["listings"][1]["tradeMode"] == "in_person"

    # Also verify Markdown rendering with --live
    res_md = runner.invoke(
        trade_group,
        [
            "cluster",
            "--enchant",
            "Lightning Damage",
            "--notables",
            "Sadist,Snowstorm",
            "--live",
        ],
    )
    assert res_md.exit_code == 0, res_md.output
    assert "Widespread Destruction" in res_md.output
    assert "instant (4200g)" in res_md.output
    assert "ClusterCrafter" in res_md.output


def test_cli_trade_cluster_live_back_notable_extraction_shapes() -> None:
    from pob_cli.cli_trade.cluster_cmd import _extract_back_notable_from_listing

    front = {"Sadist", "Snowstorm"}
    valid_names = {"Doryani's Lesson", "Disorienting Display", "Widespread Destruction"}
    valid_ids = {
        "explicit.stat_228455793": "Doryani's Lesson",
        "explicit.stat_3206911230": "Disorienting Display",
        "explicit.stat_1678643716": "Widespread Destruction",
    }

    # 1. Dict explicitMods with stat.* hash and wiki-link markup
    item_dict_mod = {
        "explicitMods": [
            {
                "description": "1 Added Passive Skill is [affliction_notable_widespread_destruction|Widespread Destruction]",
                "hash": "stat.explicit.stat_1678643716",
            }
        ]
    }
    assert _extract_back_notable_from_listing(item_dict_mod, front, valid_names, valid_ids) == "Widespread Destruction"

    # 2. TW localized text resolved via extended.hashes.explicit
    item_tw_ext = {
        "explicitMods": ["1 個附加天賦為[affliction_notable_doryanis_lesson|多里亞尼之訓]"],
        "extended": {
            "hashes": {
                "explicit": [
                    ["explicit.stat_3638731729", [0]],
                    ["explicit.stat_228455793", [1]],
                ]
            }
        },
    }
    assert _extract_back_notable_from_listing(item_tw_ext, front, valid_names, valid_ids) == "Doryani's Lesson"

    # 3. Dict explicitMods without hash -> strips [tag|Label] markup
    item_markup_only = {
        "explicitMods": [
            {"description": "1 Added Passive Skill is [affliction_notable_disorienting_display|Disorienting Display]"}
        ]
    }
    assert _extract_back_notable_from_listing(item_markup_only, front, valid_names, {}) == "Disorienting Display"

    # 4. Plain str explicitMods
    item_plain = {"explicitMods": ["1 Added Passive Skill is Sadist", "1 Added Passive Skill is Widespread Destruction"]}
    assert _extract_back_notable_from_listing(item_plain, front, valid_names, {}) == "Widespread Destruction"


def test_cli_trade_cluster_markdown_discloses_offline_fallback_and_no_fallback_for_two_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pob_cli.cli_trade as ct
    import pob_cli.trade_query.cluster_topology as ctop
    from pob_cli.trade_query.cluster_data import _build_offline_fallback_data

    monkeypatch.setattr(ct, "resolve_trade_league", lambda league, **kw: ("Standard", None))
    monkeypatch.setattr(ctop, "load_cluster_data", lambda **kw: _build_offline_fallback_data())

    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        ["cluster", "--enchant", "Lightning Damage", "--notables", "Doryani's Lesson,Widespread Destruction"],
    )
    assert res.exit_code == 0, res.output
    assert "dataSource=`offline_fallback`" in res.output
    assert "Using limited offline_fallback cluster dataset" in res.output
    assert "2-Notable Fallback Link" not in res.output
    assert "2-notable" not in res.output


def test_cli_trade_cluster_invalid_notables_count_exits_1() -> None:
    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        ["cluster", "--enchant", "Lightning Damage", "--notables", "Snowstorm"],
    )
    assert res.exit_code == 1


def test_cli_trade_cluster_real_engine_shield_attack(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = find_engine_data()
    if engine is None:
        pytest.skip("PoB engine data not installed")

    import pob_cli.cli_trade as ct

    monkeypatch.setattr(ct, "resolve_trade_league", lambda league, **kw: ("Standard", None))
    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        [
            "cluster",
            "--enchant",
            "Attack Damage while holding a Shield",
            "--notables",
            "Prodigious Defence,Feed the Fury",
            "--ilvl-max",
            "67",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output[res.output.index("{") :])
    assert data["possible"] is True
    q = data["query"]["query"]
    assert "ilvl" not in q["filters"]["type_filters"]["filters"]
    assert q["filters"]["misc_filters"]["filters"]["ilvl"] == {"max": 67}
    and_by_id = {f["id"]: f["value"] for f in q["stats"][0]["filters"]}
    assert and_by_id["enchant.stat_3948993189"] == {"option": 8}
    assert len(q["stats"][1]["filters"]) == 3


def test_cli_trade_cluster_ilvl_in_misc_filters_and_rejects_zero_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.cli_trade as ct

    monkeypatch.setattr(ct, "resolve_trade_league", lambda league, **kw: (league or "Standard", None))
    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        [
            "cluster",
            "--enchant",
            "Lightning Damage",
            "--notables",
            "Sadist,Snowstorm",
            "--ilvl-max",
            "74",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output[res.output.index("{") :])
    q = data["query"]["query"]
    assert "ilvl" not in q["filters"]["type_filters"]["filters"]
    assert q["filters"]["misc_filters"]["filters"]["ilvl"] == {"max": 74}

    res_zero = runner.invoke(
        trade_group,
        [
            "cluster",
            "--enchant",
            "Lightning Damage",
            "--notables",
            "Sadist,Snowstorm",
            "--sockets",
            "0",
        ],
    )
    assert res_zero.exit_code == 1
    assert "1 or 2 jewel sockets" in res_zero.output


def test_cli_trade_cluster_live_passes_limit_and_rebuilds_league_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.cli_trade as ct

    captured: dict[str, Any] = {}

    class FakeClient:
        base_url = "https://www.pathofexile.com"

        def __init__(self, **kw: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def search(self, league: str, body: dict[str, Any]) -> dict[str, Any]:
            captured["league"] = league
            return {"id": "Q999", "total": 20, "result": [f"L{i}" for i in range(20)]}

        def fetch_many(self, ids: list[str], search_id: str, limit: int = 10) -> list[dict[str, Any]]:
            captured["ids_len"] = len(ids)
            captured["limit"] = limit
            return [
                {
                    "id": item_id,
                    "listing": {
                        "price": {"amount": 1, "currency": "divine", "type": "~b/o"},
                        "account": {"name": f"Seller{i}"},
                        "whisper": f"@Seller{i} Hi",
                    },
                    "item": {
                        "ilvl": 75,
                        "explicitMods": [
                            "1 Added Passive Skill is Sadist",
                            "1 Added Passive Skill is Widespread Destruction",
                            "1 Added Passive Skill is Snowstorm",
                        ],
                    },
                }
                for i, item_id in enumerate(ids[:limit])
            ]

    def _resolve_league(league: str | None, *, offline_only: bool = False, **kw: Any) -> tuple[str, str | None]:
        if league:
            return league, None
        return ("Standard", None) if offline_only else ("Covens", None)

    monkeypatch.setattr(ct, "TradeClient", FakeClient)
    monkeypatch.setattr(ct, "resolve_trade_league", _resolve_league)

    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        [
            "cluster",
            "--enchant",
            "Lightning Damage",
            "--notables",
            "Sadist,Snowstorm",
            "--live",
            "--max-results",
            "20",
            "--compact",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output[res.output.index("{") :])
    assert data["league"] == "Covens"
    assert data["url"].startswith("https://www.pathofexile.com/trade/search/Covens/")
    assert data["queryParamUrl"].startswith("https://www.pathofexile.com/trade/search/Covens?q=")
    assert captured["ids_len"] == 20
    assert captured["limit"] == 20
    assert data["live"]["fetched"] == 20


def test_polarity_sweep_native_reduced_and_less_stats_keep_positive_min() -> None:
    import re

    from pob_cli.trade_query import load_local_trade_stats, parse_stat_argument, resolve_requests

    local = load_local_trade_stats()
    if local:
        all_stats = local
        candidates = [
            e
            for e in local
            if str(e.get("id") or "").startswith("explicit.")
            and re.match(r"^#%\s+(?:reduced|less)\s+", str(e.get("text") or ""), re.IGNORECASE)
            and "\n" not in str(e.get("text") or "")
            and "(" not in str(e.get("text") or "")
        ][:40]
    else:
        candidates = [
            {"id": f"explicit.stat_{100000 + i}", "text": txt, "type": "explicit"}
            for i, txt in enumerate(
                [
                    "#% reduced Mana Cost of Skills",
                    "#% reduced Mana Cost of Attacks",
                    "#% reduced Mana Cost of Non-Channelling Skills",
                    "#% reduced Attribute Requirements",
                    "#% reduced Enemy Stun Threshold",
                    "#% reduced Flask Charges used",
                    "#% reduced Effect of Curses on you",
                    "#% reduced Effect of Chill on you",
                    "#% reduced Effect of Shock on you",
                    "#% reduced Ignite Duration on you",
                    "#% reduced Poison Duration on you",
                    "#% reduced Bleeding Duration on you",
                    "#% reduced Extra Damage from Critical Strikes",
                    "#% reduced Reflected Elemental Damage taken",
                    "#% reduced Reflected Physical Damage taken",
                    "#% reduced Damage taken from Damage Over Time",
                    "#% reduced Soul Gain Prevention Duration",
                    "#% reduced Light Radius",
                    "#% reduced Movement Speed",
                    "#% reduced Skill Effect Duration",
                    "#% reduced Elemental Resistances",
                    "#% less Mine Damage",
                    "#% less Trap Damage",
                    "#% less Duration",
                    "#% less Area of Effect",
                    "#% less Damage taken from Hits",
                ]
            )
        ]
        all_stats = candidates

    assert len(candidates) >= 25
    for entry in candidates:
        query_text = str(entry["text"]).replace("#%", "").strip()
        req = parse_stat_argument(f"{query_text}>=10")
        resolve_requests([req], all_stats, slot=None)
        assert req.match is not None, f"Failed to resolve {entry['text']!r}"
        assert req.min_value == 10.0, f"Native negative stat {entry['text']!r} inverted: {req}"
        assert req.max_value is None, f"Native negative stat {entry['text']!r} got max={req.max_value}: {req}"


def test_polarity_sweep_zh_native_negative_vs_signed_inverted() -> None:
    from pob_cli.trade_query import parse_stat_argument, resolve_requests

    stats = [
        {"id": "explicit.stat_474294393", "text": "#% reduced Mana Cost of Skills", "type": "explicit"},
        {"id": "explicit.stat_3759663284", "text": "#% increased Projectile Speed", "type": "explicit"},
    ]
    zh_req = parse_stat_argument("减少魔力消耗>=10")
    resolve_requests([zh_req], stats, slot=None)
    assert zh_req.match is not None
    assert zh_req.match.id == "explicit.stat_474294393"
    assert zh_req.min_value == 10.0
    assert zh_req.max_value is None

    inv_req = parse_stat_argument("reduced Projectile Speed>=10")
    resolve_requests([inv_req], stats, slot=None)
    assert inv_req.match is not None
    assert inv_req.match.id == "explicit.stat_3759663284"
    assert inv_req.min_value is None
    assert inv_req.max_value == -10.0
