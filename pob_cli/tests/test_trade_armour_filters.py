"""Offline unit & CLI tests for PoE1 Trade API armour panel filters and semantic routing."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import cli_trade
from pob_cli.cli import main
from pob_cli.trade_query import (
    build_query,
    merge_armour_filters,
    normalize_armour_filters,
    parse_stat_argument,
    plan_stats,
)

ARMOUR_SAMPLE_STATS: list[dict[str, str]] = [
    {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
    {"id": "explicit.stat_3489782002", "text": "+# to maximum Energy Shield", "type": "explicit"},
    {"id": "explicit.stat_4052037485", "text": "+# to maximum Energy Shield (Local)", "type": "explicit"},
    {"id": "explicit.stat_4015621042", "text": "#% increased Energy Shield (Local)", "type": "explicit"},
    {"id": "pseudo.pseudo_total_energy_shield", "text": "+# total maximum Energy Shield", "type": "pseudo"},
    {"id": "explicit.stat_809229260", "text": "+# to Armour", "type": "explicit"},
    {"id": "explicit.stat_3484657501", "text": "+# to Armour (Local)", "type": "explicit"},
    {"id": "explicit.stat_53045048", "text": "+# to Evasion Rating (Local)", "type": "explicit"},
    {"id": "explicit.stat_3214536851", "text": "+# to Ward (Local)", "type": "explicit"},
]


def _mock_armour_stats(*_args: Any, **_kwargs: Any) -> tuple[list[dict[str, str]], str]:
    return list(ARMOUR_SAMPLE_STATS), "mock_local"


def _invoke_dump(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> tuple[Any, dict[str, Any] | None]:
    monkeypatch.setattr(cli_trade, "load_trade_stats", _mock_armour_stats)
    monkeypatch.setattr(cli_trade, "resolve_trade_league", lambda *_a, **_kw: ("Keepers", None))
    runner = CliRunner()
    res = runner.invoke(main, ["trade", "search", *args, "--dump-query", "--json"])
    payload = json.loads(res.stdout) if res.exit_code == 0 and res.stdout.strip() else None
    return res, payload


def test_cli_armour_filter_flags_dump_query_json(monkeypatch: pytest.MonkeyPatch) -> None:
    res, data = _invoke_dump(
        monkeypatch,
        [
            "--slot", "body_armour",
            "--es-min", "700", "--es-max", "950",
            "--ar-min", "1200",
            "--ev-min", "800",
            "--ward-min", "250", "--ward-max", "400",
        ],
    )
    assert res.exit_code == 0, res.output
    assert data is not None
    af = data["query"]["query"]["filters"]["armour_filters"]["filters"]
    assert af == {
        "es": {"min": 700, "max": 950},
        "ar": {"min": 1200},
        "ev": {"min": 800},
        "ward": {"min": 250, "max": 400},
    }
    assert "stats" not in data["query"]["query"]


def test_cli_armour_filter_emits_only_specified_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    res, data = _invoke_dump(monkeypatch, ["--slot", "helmet", "--es-min", "350"])
    assert res.exit_code == 0, res.output
    assert data is not None
    af = data["query"]["query"]["filters"]["armour_filters"]["filters"]
    assert af == {"es": {"min": 350}}


@pytest.mark.parametrize(
    "bad_args",
    [
        ["--slot", "body_armour", "--es-min", "800", "--es-max", "700"],
        ["--slot", "body_armour", "--ar-min", "-10"],
        ["--slot", "body_armour", "--ev-max", "-1"],
        ["--slot", "body_armour", "--ward-min", "500", "--ward-max", "200"],
        ["--slot", "body_armour", "--es-max", "600", "--stat", "total ES>=700"],
    ],
)
def test_cli_armour_filter_validation_errors(
    monkeypatch: pytest.MonkeyPatch, bad_args: list[str]
) -> None:
    res, _ = _invoke_dump(monkeypatch, bad_args)
    assert res.exit_code == 1
    assert res.stderr.strip() or res.output.strip()


@pytest.mark.parametrize(
    ("slot", "stat_expr", "expected_key", "expected_bounds"),
    [
        ("body_armour", "total Energy Shield=700", "es", {"min": 700}),
        ("Body Armour", "total ES>=700", "es", {"min": 700}),
        ("胸甲", "面板护盾>=700", "es", {"min": 700}),
        ("body_armour", "总能量护盾>=700", "es", {"min": 700}),
        ("body_armour", "总护盾=650", "es", {"min": 650}),
        ("body_armour", "總能量護盾>=720", "es", {"min": 720}),
        ("helmet", "total Energy Shield<=500", "es", {"max": 500}),
        ("shield", "面板护盾=300..450", "es", {"min": 300, "max": 450}),
        ("gloves", "total Armour>=600", "ar", {"min": 600}),
        ("boots", "面板护甲=400..800", "ar", {"min": 400, "max": 800}),
        ("boots", "总闪避>=500", "ev", {"min": 500}),
        ("gloves", "total Evasion<=900", "ev", {"max": 900}),
        ("helmet", "total Ward>=300", "ward", {"min": 300}),
        ("helmet", "面板结界>=250", "ward", {"min": 250}),
    ],
)
def test_armour_slot_semantic_routing_never_uses_pseudo_es(
    monkeypatch: pytest.MonkeyPatch,
    slot: str,
    stat_expr: str,
    expected_key: str,
    expected_bounds: dict[str, int],
) -> None:
    res, data = _invoke_dump(monkeypatch, ["--slot", slot, "--stat", stat_expr])
    assert res.exit_code == 0, res.output
    assert data is not None
    inner = data["query"]["query"]
    assert inner["filters"]["armour_filters"]["filters"] == {expected_key: expected_bounds}
    assert "pseudo.pseudo_total_energy_shield" not in json.dumps(inner)
    assert "stats" not in inner


def test_semantic_routing_merges_with_cli_flags_and_other_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tighter CLI --es-min 800 is preserved over routed total ES>=700.
    res, data = _invoke_dump(
        monkeypatch,
        [
            "--slot", "body_armour",
            "--es-min", "800",
            "--es-max", "1000",
            "--stat", "total Energy Shield>=700",
            "--stat", "maximum Life=80",
        ],
    )
    assert res.exit_code == 0, res.output
    assert data is not None
    inner = data["query"]["query"]
    assert inner["filters"]["armour_filters"]["filters"] == {"es": {"min": 800, "max": 1000}}
    stat_ids = [f["id"] for g in inner["stats"] for f in g["filters"]]
    assert stat_ids == ["explicit.stat_3299347043"]


def test_non_armour_slot_does_not_route_to_armour_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    res, data = _invoke_dump(
        monkeypatch,
        ["--slot", "ring1", "--stat", "total maximum Energy Shield=40"],
    )
    assert res.exit_code == 0, res.output
    assert data is not None
    inner = data["query"]["query"]
    assert "armour_filters" not in inner.get("filters", {})
    stat_ids = [f["id"] for g in inner["stats"] for f in g["filters"]]
    assert stat_ids == ["pseudo.pseudo_total_energy_shield"]

    # Unmapped "total Energy Shield" on non-armour slot must NOT be intercepted into armour_filters.
    res_unmapped, _ = _invoke_dump(
        monkeypatch,
        ["--slot", "ring1", "--stat", "total Energy Shield=40"],
    )
    assert res_unmapped.exit_code == 1


@pytest.mark.parametrize(
    ("stat_expr", "expected_flag", "expected_val"),
    [
        ("能量护盾=700", "--es-min", "700"),
        ("护甲=1500", "--ar-min", "1500"),
        ("闪避=1200", "--ev-min", "1200"),
        ("结界=300", "--ward-min", "300"),
    ],
)
def test_local_flat_high_value_emits_non_blocking_hint(
    monkeypatch: pytest.MonkeyPatch,
    stat_expr: str,
    expected_flag: str,
    expected_val: str,
) -> None:
    res, data = _invoke_dump(monkeypatch, ["--slot", "body_armour", "--stat", stat_expr])
    assert res.exit_code == 0, res.output
    assert data is not None
    assert "hints" in data and len(data["hints"]) == 1
    assert f"{expected_flag} {expected_val}" in data["hints"][0]
    assert f"{expected_flag} {expected_val}" in res.stderr


def test_local_flat_low_value_and_non_armour_emit_no_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    res_low, data_low = _invoke_dump(monkeypatch, ["--slot", "body_armour", "--stat", "能量护盾=100"])
    assert res_low.exit_code == 0, res_low.output
    assert data_low is not None and "hints" not in data_low

    res_ring, data_ring = _invoke_dump(monkeypatch, ["--slot", "ring1", "--stat", "能量护盾=200"])
    assert res_ring.exit_code == 0, res_ring.output
    assert data_ring is not None and "hints" not in data_ring


def test_direct_planner_api_merge_and_range_parse() -> None:
    req = parse_stat_argument("面板护盾=700..900")
    assert req.text == "面板护盾" and req.min_value == 700.0 and req.max_value == 900.0
    plan = plan_stats([req], ARMOUR_SAMPLE_STATS, slot="body_armour")
    body = build_query(
        plan=plan,
        category="armour.chest",
        rarity="rare",
        armour_filters=normalize_armour_filters(es_min=750),
    )
    assert body["query"]["filters"]["armour_filters"] == {"filters": {"es": {"min": 750, "max": 900}}}
    with pytest.raises(ValueError, match="Conflicting es"):
        merge_armour_filters({"es": {"min": 800}}, {"es": {"max": 700}})
