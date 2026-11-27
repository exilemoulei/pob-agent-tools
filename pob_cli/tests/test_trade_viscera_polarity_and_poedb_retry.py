"""Tests for 3.30 Viscera/Graft trade slots, reduced<->increased polarity inversion, and PoEDB retry/fallback."""

from __future__ import annotations

import json
import ssl
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data

from pob_cli import cli_research
from pob_cli.cli_trade import cmd_trade_search, trade_group
from pob_cli.cli_trade.common import _is_known_slot
from pob_cli.guardrails import GuardrailScope, check_input
from pob_cli.poedb_client import client as poedb_client
from pob_cli.trade_query import (
    build_query,
    category_for_slot,
    is_uncategorized_trade_slot,
    load_local_trade_stats,
    parse_stat_argument,
    parse_sum_group_argument,
    plan_stats,
    pob_slot_for_slot,
    resolve_stat,
)

SAMPLE_STATS: list[dict[str, Any]] = [
    {
        "id": "implicit.stat_1590183647",
        "text": "#% increased bonuses gained from Equipped Boots",
        "type": "implicit",
    },
    {
        "id": "implicit.stat_3971521684",
        "text": "#% increased bonuses gained from Equipped Gloves",
        "type": "implicit",
    },
    {
        "id": "explicit.stat_474294393",
        "text": "#% reduced Mana Cost of Skills",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_1000000001",
        "text": "#% more Damage Taken",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_2000000001",
        "text": "#% reduced Elemental Resistances",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_2000000002",
        "text": "#% less Mine Damage",
        "type": "explicit",
    },
    {
        "id": "explicit.stat_3299347043",
        "text": "+# to maximum Life",
        "type": "explicit",
    },
]


# ---------------------------------------------------------------------------
# 1. 3.30 Viscera / Graft / Wickerman Trade Slot Support
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slot_name",
    [
        "viscera",
        "graft",
        "graft 1",
        "graft 2",
        "graft1",
        "graft2",
        "wickerman",
        "脏器",
        "臟器",
        "假肢",
        "肢体",
        "viscera_head",
        "viscera_body",
        "viscera_arm",
        "viscera_leg",
    ],
)
def test_viscera_and_graft_slots_are_known_and_uncategorized(slot_name: str) -> None:
    check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    assert _is_known_slot(slot_name), f"Expected {slot_name!r} to be recognized"
    assert category_for_slot(slot_name) is None
    assert is_uncategorized_trade_slot(slot_name) is True


def test_viscera_and_graft_pob_slot_mappings() -> None:
    assert pob_slot_for_slot("viscera_head") == "Head"
    assert pob_slot_for_slot("viscera_body") == "Body"
    assert pob_slot_for_slot("viscera_arm") == "Arm"
    assert pob_slot_for_slot("viscera_leg") == "Leg"
    assert pob_slot_for_slot("graft 1") == "Graft 1"
    assert pob_slot_for_slot("graft 2") == "Graft 2"


def test_trade_search_dump_query_viscera_with_name_and_reduced_stat(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.cli_trade as ct

    monkeypatch.setattr(ct, "load_trade_stats", lambda **kw: (SAMPLE_STATS, "test_fixture"))
    monkeypatch.setattr(ct, "resolve_trade_league", lambda league, **kw: (league or "Standard", None))

    runner = CliRunner()
    res = runner.invoke(
        cmd_trade_search,
        [
            "--slot",
            "viscera",
            "--name",
            "Greater Scholar's Leg",
            "--stat",
            "reduced bonuses gained from Equipped Boots>=47",
            "--dump-query",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["slot"] == "viscera"
    assert payload["category"] is None
    assert "slotNote" in payload
    q = payload["query"]["query"]
    assert q["name"] == "Greater Scholar's Leg"
    # Must NOT impose an armour category filter
    tf = ((q.get("filters") or {}).get("type_filters") or {}).get("filters") or {}
    assert "category" not in tf
    stat_flt = q["stats"][0]["filters"][0]
    assert stat_flt["id"] == "implicit.stat_1590183647"
    assert stat_flt["value"] == {"max": -47.0}


def test_trade_search_dump_query_graft_coven_queen_crown(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.cli_trade as ct

    monkeypatch.setattr(ct, "load_trade_stats", lambda **kw: (SAMPLE_STATS, "test_fixture"))
    monkeypatch.setattr(ct, "resolve_trade_league", lambda league, **kw: (league or "Standard", None))

    runner = CliRunner()
    res = runner.invoke(
        cmd_trade_search,
        [
            "--slot",
            "脏器",
            "--name",
            "Coven Queen's Crown",
            "--stat",
            "reduced bonuses gained from Equipped Gloves<=50",
            "--dump-query",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["query"]["query"]["name"] == "Coven Queen's Crown"
    stat_flt = payload["query"]["query"]["stats"][0]["filters"][0]
    assert stat_flt["id"] == "implicit.stat_3971521684"
    assert stat_flt["value"] == {"min": -50.0}


# ---------------------------------------------------------------------------
# 2. English reduced <-> increased & less <-> more Negative Polarity Inversion
# ---------------------------------------------------------------------------


def test_reduced_to_increased_bounds_inversion_cases() -> None:
    # >= 47 reduced -> max = -47 on increased
    req_ge = parse_stat_argument("reduced bonuses gained from Equipped Boots>=47")
    plan_ge = plan_stats([req_ge], SAMPLE_STATS)
    q_ge = build_query(plan=plan_ge, category=None, rarity="any")
    assert q_ge["query"]["stats"][0]["filters"][0] == {
        "id": "implicit.stat_1590183647",
        "value": {"max": -47.0},
        "disabled": False,
    }

    # <= 50 reduced -> min = -50 on increased
    req_le = parse_stat_argument("reduced bonuses gained from Equipped Boots<=50")
    plan_le = plan_stats([req_le], SAMPLE_STATS)
    q_le = build_query(plan=plan_le, category=None, rarity="any")
    assert q_le["query"]["stats"][0]["filters"][0] == {
        "id": "implicit.stat_1590183647",
        "value": {"min": -50.0},
        "disabled": False,
    }

    # 40..50 reduced -> min = -50, max = -40 on increased
    req_rng = parse_stat_argument("reduced bonuses gained from Equipped Boots=40..50")
    plan_rng = plan_stats([req_rng], SAMPLE_STATS)
    q_rng = build_query(plan=plan_rng, category=None, rarity="any")
    assert q_rng["query"]["stats"][0]["filters"][0] == {
        "id": "implicit.stat_1590183647",
        "value": {"min": -50.0, "max": -40.0},
        "disabled": False,
    }


def test_polarity_inversion_does_not_double_negate_already_negative_value() -> None:
    req = parse_stat_argument("reduced bonuses gained from Equipped Boots=-47")
    plan = plan_stats([req], SAMPLE_STATS)
    q = build_query(plan=plan, category=None, rarity="any")
    assert q["query"]["stats"][0]["filters"][0]["value"] == {"min": -47.0}


def test_direct_reduced_stat_is_never_inverted() -> None:
    match = resolve_stat("reduced Mana Cost of Skills", SAMPLE_STATS)
    assert match is not None
    assert match.id == "explicit.stat_474294393"
    assert match.polarity_inverted is False

    req = parse_stat_argument("reduced Mana Cost of Skills>=10")
    plan = plan_stats([req], SAMPLE_STATS)
    q = build_query(plan=plan, category=None, rarity="any")
    assert q["query"]["stats"][0]["filters"][0]["id"] == "explicit.stat_474294393"
    assert q["query"]["stats"][0]["filters"][0]["value"] == {"min": 10.0}


def test_less_to_more_polarity_inversion() -> None:
    req = parse_stat_argument("less Damage Taken>=15")
    plan = plan_stats([req], SAMPLE_STATS)
    q = build_query(plan=plan, category=None, rarity="any")
    assert q["query"]["stats"][0]["filters"][0] == {
        "id": "explicit.stat_1000000001",
        "value": {"max": -15.0},
        "disabled": False,
    }


def test_positive_query_never_inverts_onto_native_reduced_or_less() -> None:
    assert resolve_stat("increased Elemental Resistances", SAMPLE_STATS) is None
    assert resolve_stat("more Mine Damage", SAMPLE_STATS) is None
    with pytest.raises(ValueError, match="Could not map this modifier"):
        plan_stats([parse_stat_argument("increased Elemental Resistances>=20")], SAMPLE_STATS)
    with pytest.raises(ValueError, match="Could not map this modifier"):
        plan_stats([parse_stat_argument("more Mine Damage>=20")], SAMPLE_STATS)


@pytest.mark.parametrize(
    "zh_query",
    [
        "从已装备长靴获得的加成减少>=47",
        "從已裝備長靴獲得的加成減少>=47",
    ],
)
def test_zh_negative_polarity_inverts_when_apt_maps_to_increased(zh_query: str) -> None:
    req = parse_stat_argument(zh_query)
    plan = plan_stats([req], SAMPLE_STATS)
    q = build_query(plan=plan, category=None, rarity="any")
    assert q["query"]["stats"][0]["filters"][0] == {
        "id": "implicit.stat_1590183647",
        "value": {"max": -47.0},
        "disabled": False,
    }


def test_zh_negative_polarity_no_double_negation_and_native_reduced_preserved() -> None:
    req_neg = parse_stat_argument("从已装备长靴获得的加成减少=-47")
    plan_neg = plan_stats([req_neg], SAMPLE_STATS)
    q_neg = build_query(plan=plan_neg, category=None, rarity="any")
    assert q_neg["query"]["stats"][0]["filters"][0]["value"] == {"min": -47.0}

    # Native `#% reduced ...` stat queried in Chinese must stay positive (not inverted)
    native_entries = [
        {
            "id": "explicit.stat_reflected_1",
            "text": "You and your Minions take #% reduced Reflected Damage",
            "type": "explicit",
        },
    ]
    req_nat = parse_stat_argument("你和你的召唤物减少承受 #% 反射伤害>=50")
    plan_nat = plan_stats([req_nat], native_entries)
    q_nat = build_query(plan=plan_nat, category=None, rarity="any")
    assert q_nat["query"]["stats"][0]["filters"][0]["value"] == {"min": 50.0}


def test_weighted_sum_inverts_weight_and_bound_on_polarity_inverted_stat() -> None:
    wgroup = parse_sum_group_argument("reduced bonuses gained from Equipped Boots=40*1>=40")
    plan = plan_stats([], SAMPLE_STATS, weighted_groups=[wgroup])
    q = build_query(plan=plan, category=None, rarity="any")
    w_grp = q["query"]["stats"][0]
    assert w_grp["type"] == "weight"
    assert w_grp["value"] == {"min": 40.0}
    assert w_grp["filters"][0] == {
        "id": "implicit.stat_1590183647",
        "value": {"weight": -1.0, "max": -40.0},
        "disabled": False,
    }


def test_trade_stats_cli_surfaces_polarity_inverted_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import pob_cli.cli_trade as ct

    monkeypatch.setattr(ct, "load_trade_stats", lambda **kw: (SAMPLE_STATS, "test_fixture"))
    runner = CliRunner()
    res = runner.invoke(
        trade_group,
        ["stats", "reduced bonuses gained from Equipped Boots", "--json"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["match"]["id"] == "implicit.stat_1590183647"
    assert data["match"]["polarityInverted"] is True

    res_md = runner.invoke(
        trade_group,
        ["stats", "reduced bonuses gained from Equipped Boots"],
    )
    assert res_md.exit_code == 0, res_md.output
    assert "bounds inverted (negated)" in res_md.output


def test_real_trade_site_stats_reduced_boots_and_mana_cost() -> None:
    engine = find_engine_data()
    if engine is None:
        pytest.skip("PoB engine data not installed")
    entries = load_local_trade_stats(pob_root=engine)
    assert entries is not None and len(entries) > 1000

    # Real TradeSiteStats.lua has ONLY `#% increased bonuses gained from Equipped Boots`
    for query in (
        "reduced bonuses gained from Equipped Boots>=47",
        "从已装备长靴获得的加成减少>=47",
        "從已裝備長靴獲得的加成減少>=47",
    ):
        req = parse_stat_argument(query)
        plan = plan_stats([req], entries)
        q = build_query(plan=plan, category=None, rarity="any")
        flt = q["query"]["stats"][0]["filters"][0]
        assert flt["id"] == "implicit.stat_1590183647", query
        assert flt["value"] == {"max": -47.0}, query

    # Real TradeSiteStats.lua HAS native `#% reduced Mana Cost of Skills` -> must NOT invert
    req_native = parse_stat_argument("reduced Mana Cost of Skills>=10")
    plan_native = plan_stats([req_native], entries)
    q_native = build_query(plan=plan_native, category=None, rarity="any")
    flt_native = q_native["query"]["stats"][0]["filters"][0]
    assert flt_native["id"] == "explicit.stat_474294393"
    assert flt_native["value"] == {"min": 10.0}


# ---------------------------------------------------------------------------
# 3. PoEDB Transient SSL/Timeout 1x Retry & Local DB Fallback Hint
# ---------------------------------------------------------------------------


def test_poedb_get_retries_once_on_transient_ssl_or_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(poedb_client.time, "sleep", lambda s: sleeps.append(s))

    attempts = 0

    class FakeResp:
        text = "<html>ok</html>"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def get(self, url: str) -> FakeResp:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectTimeout("TLS handshake timed out")
            return FakeResp()

    body = poedb_client._poedb_get("https://poedb.tw/cn/Test", client=FakeClient())  # type: ignore[arg-type]
    assert body == "<html>ok</html>"
    assert attempts == 2
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_poedb_get_retries_once_on_ssl_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(poedb_client.time, "sleep", lambda s: sleeps.append(s))

    attempts = 0

    class FakeResp:
        text = "ok-after-ssl"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def get(self, url: str) -> FakeResp:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ssl.SSLError("EOF occurred in violation of protocol")
            return FakeResp()

    body = poedb_client._poedb_get("https://poedb.tw/cn/Test", client=FakeClient())  # type: ignore[arg-type]
    assert body == "ok-after-ssl"
    assert attempts == 2
    assert len(sleeps) == 1


def test_poedb_get_never_retries_http_404(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(poedb_client.time, "sleep", lambda s: sleeps.append(s))

    attempts = 0
    req = httpx.Request("GET", "https://poedb.tw/cn/Missing")
    resp_404 = httpx.Response(404, request=req, text="Not Found")

    class FakeClient:
        def get(self, url: str) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return resp_404

    with pytest.raises(RuntimeError, match="404"):
        poedb_client._poedb_get("https://poedb.tw/cn/Missing", client=FakeClient())  # type: ignore[arg-type]
    assert attempts == 1
    assert sleeps == []


@pytest.mark.parametrize("status_code", [429, 502, 503])
def test_poedb_get_retries_once_on_transient_5xx_or_429(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(poedb_client.time, "sleep", lambda s: sleeps.append(s))

    attempts = 0
    req = httpx.Request("GET", "https://poedb.tw/cn/Busy")

    class FakeClient:
        def get(self, url: str) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(status_code, request=req, text="Temporary error")
            return httpx.Response(200, request=req, text="<html>recovered</html>")

    body = poedb_client._poedb_get("https://poedb.tw/cn/Busy", client=FakeClient())  # type: ignore[arg-type]
    assert body == "<html>recovered</html>"
    assert attempts == 2
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_research_partial_failure_appends_local_db_fallback_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli import external_data as ext
    from pob_cli import poedb_client as pc

    monkeypatch.setattr(
        pc,
        "query_poedb",
        lambda q, **kw: (_ for _ in ()).throw(RuntimeError("PoEDB request failed: SSL timeout")),
    )
    monkeypatch.setattr(
        ext,
        "query_wiki",
        lambda q, **kw: {"query": q, "title": "Mageblood", "url": "https://www.poewiki.net/wiki/Mageblood", "summary": "Belt."},
    )

    res = cli_research.run_dual_research(["Mageblood"])
    item = res["results"][0]
    assert item["okSources"] == ["wiki"]
    assert "poedb" in item["errors"]
    assert 'pob data <unique|gem|mod|tattoo> "Mageblood"' in item["localFallbackHint"]

    md = cli_research._render_research_markdown(res)
    assert "[PARTIAL]" in md
    assert "[FALLBACK]" in md
    assert 'pob data <unique|gem|mod|tattoo> "Mageblood"' in md
