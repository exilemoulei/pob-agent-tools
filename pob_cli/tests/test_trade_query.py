"""Unit tests for stat resolution and trade query construction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pob_cli.trade_query import (
    build_query,
    category_for_slot,
    flatten_stats,
    parse_stat_argument,
    plan_stats,
    pob_slot_for_slot,
    resolve_stat,
    suggest_stats,
)

FIXTURES = Path(__file__).parent / "fixtures"
STATS = json.loads((FIXTURES / "trade_stats.json").read_text(encoding="utf-8"))["result"]
ENTRIES = flatten_stats(STATS)

FIRE_ID = "explicit.stat_3962278098"
COLD_ID = "explicit.stat_3372524247"
LIGHTNING_ID = "explicit.stat_1671376347"


def test_resolve_short_text() -> None:
    match = resolve_stat("to Cold Resistance", ENTRIES)
    assert match is not None
    assert match.id == COLD_ID
    assert match.template == "#% to cold resistance"


def test_resolve_explicit_preferred_over_crafted() -> None:
    match = resolve_stat("#% to Fire Resistance", ENTRIES)
    assert match is not None
    assert match.id == FIRE_ID


def test_resolve_unknown_returns_suggestions() -> None:
    assert resolve_stat("to Unicorn Resistance", ENTRIES) is None
    suggestions = suggest_stats("to Cold Resistanc", ENTRIES, limit=3)
    assert suggestions
    assert suggestions[0].id == COLD_ID


def test_parse_stat_argument() -> None:
    request = parse_stat_argument("to Cold Resistance=40")
    assert request.text == "to Cold Resistance"
    assert request.min_value == 40.0
    default = parse_stat_argument("to Cold Resistance")
    assert default.min_value == 1.0
    with pytest.raises(ValueError):
        parse_stat_argument("   ")


def test_plain_query_is_and_group() -> None:
    plan = plan_stats([parse_stat_argument("to Cold Resistance=40")], ENTRIES)
    assert plan.conversions == []
    query = build_query(
        plan=plan,
        category=category_for_slot("gloves"),
        rarity="rare",
        item_level_min=80,
        currency="chaos",
        budget=20,
    )
    body = query["query"]
    assert body["status"]["option"] == "online"
    assert body["stats"] == [
        {"type": "and", "filters": [{"id": COLD_ID, "value": {"min": 40.0}, "disabled": False}]}
    ]
    type_filters = body["filters"]["type_filters"]["filters"]
    assert type_filters["category"] == {"option": "armour.gloves"}
    assert type_filters["rarity"] == {"option": "rare"}
    assert type_filters["ilvl"] == {"min": 80}
    assert body["filters"]["trade_filters"]["filters"]["price"] == {
        "option": "chaos",
        "max": 20,
    }
    assert query["sort"] == {"price": "asc"}


def test_conversion_expands_to_count_group() -> None:
    plan = plan_stats(
        [parse_stat_argument("to Cold Resistance=40")],
        ENTRIES,
        allow_conversion=True,
    )
    assert len(plan.conversions) == 1
    conversion = plan.conversions[0]
    assert conversion.group == "resistance"
    assert conversion.target == "cold"
    assert set(conversion.stat_ids) == {"fire", "cold", "lightning"}

    query = build_query(plan=plan, category=None, rarity="rare")
    groups = query["query"]["stats"]
    assert len(groups) == 1
    assert groups[0]["type"] == "count"
    assert groups[0]["value"] == {"min": 1}
    assert {filter_["id"] for filter_ in groups[0]["filters"]} == {
        FIRE_ID,
        COLD_ID,
        LIGHTNING_ID,
    }
    assert all(filter_["value"]["min"] == 40.0 for filter_ in groups[0]["filters"])


def test_conversion_target_override() -> None:
    plan = plan_stats(
        [parse_stat_argument("to Cold Resistance=30")],
        ENTRIES,
        allow_conversion=True,
        convert_to="fire",
    )
    assert plan.conversions[0].target == "fire"


def test_conversion_rejects_unavailable_target() -> None:
    limited = [entry for entry in ENTRIES if entry["id"] != FIRE_ID]
    with pytest.raises(ValueError):
        plan_stats(
            [parse_stat_argument("to Cold Resistance=30")],
            limited,
            allow_conversion=True,
            convert_to="fire",
        )


def test_multiple_stats_keep_plain_group_alongside_count_group() -> None:
    plan = plan_stats(
        [parse_stat_argument("to Cold Resistance=40"), parse_stat_argument("to maximum Life=60")],
        ENTRIES,
        allow_conversion=True,
    )
    assert len(plan.plain) == 1
    assert len(plan.conversions) == 1
    query = build_query(plan=plan, category="armour.gloves", rarity="rare")
    groups = query["query"]["stats"]
    assert [group["type"] for group in groups] == ["and", "count"]


def test_slot_mappings() -> None:
    assert category_for_slot("gloves") == "armour.gloves"
    assert category_for_slot("ring2") == "accessory.ring"
    assert category_for_slot("weapon1") is None
    assert pob_slot_for_slot("shield") == "Weapon 2"
    assert pob_slot_for_slot("ring2") == "Ring 2"


def test_pseudo_filters_are_added_as_an_and_group() -> None:
    plan = plan_stats([], ENTRIES)
    query = build_query(
        plan=plan,
        category="armour.gloves",
        rarity="rare",
        pseudo_filters=[
            {
                "id": "pseudo.pseudo_number_of_empty_suffix_mods",
                "value": {"min": 1},
                "disabled": False,
            }
        ],
    )
    groups = query["query"]["stats"]
    assert groups == [
        {
            "type": "and",
            "filters": [
                {
                    "id": "pseudo.pseudo_number_of_empty_suffix_mods",
                    "value": {"min": 1},
                    "disabled": False,
                }
            ],
        }
    ]
