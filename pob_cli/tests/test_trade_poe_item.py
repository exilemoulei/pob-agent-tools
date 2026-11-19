"""Unit tests for PoB item text parsing, rebuilding and Harvest conversion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pob_cli.poe_item import (
    build_item_text,
    convert_item_text,
    mods_by_template,
    normalize_template,
    parse_item_text,
    trade_mod_tiers,
)

FIXTURES = Path(__file__).parent / "fixtures"
GLOVES_TEXT = (FIXTURES / "item_gloves.txt").read_text(encoding="utf-8")
FETCH_ROWS = json.loads((FIXTURES / "trade_fetch.json").read_text(encoding="utf-8"))["result"]


def test_normalize_template() -> None:
    assert normalize_template("+35% to Fire Resistance") == "#% to fire resistance"
    assert normalize_template("Adds 5 to 10 Cold Damage to Attacks") == (
        "adds # to # cold damage to attacks"
    )
    assert normalize_template("{crafted}+20 to Strength") == "# to strength"


def test_parse_pob_item_text() -> None:
    item = parse_item_text(GLOVES_TEXT)
    assert item.rarity == "RARE"
    assert item.name == "Doom Hand"
    assert item.base_type == "Slink Gloves"
    assert item.item_level == 84
    assert mods_by_template(item)["#% to fire resistance"] == 35
    assert mods_by_template(item)["# to maximum life"] == 70
    assert item.mods[0].kind == "explicit"


def test_parse_implicit_and_crafted_kinds() -> None:
    text = "\n".join(
        [
            "Rarity: RARE",
            "Test Ring",
            "Amethyst Ring",
            "Item Level: 80",
            "Implicits: 1",
            "+20% to Chaos Resistance",
            "{crafted}+30 to Strength",
            "+10% to all Elemental Resistances",
        ]
    )
    item = parse_item_text(text)
    kinds = {mod.template: mod.kind for mod in item.mods}
    assert kinds["#% to chaos resistance"] == "implicit"
    assert kinds["# to strength"] == "crafted"
    assert kinds["#% to all elemental resistances"] == "explicit"


def test_build_item_text_from_trade_json_roundtrip() -> None:
    text = build_item_text(FETCH_ROWS[0]["item"])
    assert text.splitlines()[0] == "Rarity: RARE"
    item = parse_item_text(text)
    assert item.name == "Doom Hand"
    assert item.base_type == "Slink Gloves"
    assert item.item_level == 84
    assert mods_by_template(item)["#% to fire resistance"] == 35


def test_convert_resistance_keeps_value_and_records_cost() -> None:
    converted = convert_item_text(GLOVES_TEXT, "cold")
    assert len(converted.conversions) == 1
    conversion = converted.conversions[0]
    assert conversion.template == "#% to fire resistance"
    assert conversion.converted_template == "#% to cold resistance"
    assert conversion.values == (35.0,)
    assert "300" in conversion.cost
    assert conversion.patch == "3.30"

    assert "+35% to Cold Resistance" in converted.text
    assert "Fire Resistance" not in converted.text
    # Unrelated modifiers are untouched.
    assert "+70 to maximum Life" in converted.text
    assert "20% increased Attack Speed" in converted.text


def test_convert_group_filter_does_not_touch_damage() -> None:
    text = "\n".join(
        [
            "Rarity: RARE",
            "Test Gloves",
            "Slink Gloves",
            "Item Level: 84",
            "+35% to Fire Resistance",
            "Adds 5 to 10 Fire Damage",
        ]
    )
    resistance_only = convert_item_text(text, "cold", group="resistance")
    assert len(resistance_only.conversions) == 1
    assert "+35% to Cold Resistance" in resistance_only.text
    assert "Adds 5 to 10 Fire Damage" in resistance_only.text

    damage_only = convert_item_text(text, "cold", group="elemental_damage")
    assert "+35% to Fire Resistance" in damage_only.text
    assert "Adds 5 to 10 Cold Damage" in damage_only.text


def test_convert_skips_implicit_and_fractured() -> None:
    text = "\n".join(
        [
            "Rarity: RARE",
            "Test Ring",
            "Amethyst Ring",
            "Item Level: 80",
            "Implicits: 1",
            "+20% to Cold Resistance",
            "{fractured}+30% to Fire Resistance",
            "+25% to Fire Resistance",
            "+40% to Lightning Resistance",
        ]
    )
    converted = convert_item_text(text, "lightning")
    assert len(converted.conversions) == 1
    assert converted.conversions[0].template == "#% to fire resistance"
    assert "+20% to Cold Resistance" in converted.text
    assert "{fractured}+30% to Fire Resistance" in converted.text
    assert "+25% to Lightning Resistance" in converted.text
    assert any("fractured" in reason for reason in converted.skipped)
    assert any("implicit" in reason for reason in converted.skipped)


def test_convert_refuses_unique_and_corrupted() -> None:
    unique = build_item_text(FETCH_ROWS[2]["item"])
    result = convert_item_text(unique, "cold")
    assert result.conversions == []
    assert result.skipped

    corrupted = GLOVES_TEXT.replace("Rarity: RARE\n", "Rarity: RARE\n", 1) + "\nCorrupted"
    result = convert_item_text(corrupted, "cold")
    assert result.conversions == []


def test_convert_ignores_all_elemental_resistance() -> None:
    text = "\n".join(
        [
            "Rarity: RARE",
            "Test Ring",
            "Amethyst Ring",
            "Item Level: 80",
            "+10% to all Elemental Resistances",
        ]
    )
    converted = convert_item_text(text, "cold")
    assert converted.conversions == []
    assert "+10% to all Elemental Resistances" in converted.text


def test_convert_rejects_unknown_target() -> None:
    with pytest.raises(ValueError):
        convert_item_text(GLOVES_TEXT, "physical")


def test_trade_mod_tiers_reads_api_tier_labels() -> None:
    tiers = trade_mod_tiers(FETCH_ROWS[0])
    assert tiers["#% to fire resistance"] == "S3"
    assert tiers["# to maximum life"] == "P5"
    assert tiers["#% increased attack speed"] == "S2"


def test_conversion_records_the_tier_that_harvest_keeps() -> None:
    text = build_item_text(FETCH_ROWS[0]["item"])
    converted = convert_item_text(text, "cold", tiers=trade_mod_tiers(FETCH_ROWS[0]))

    assert len(converted.conversions) == 1
    conversion = converted.conversions[0]
    assert conversion.template == "#% to fire resistance"
    assert conversion.tier == "S3"
    assert conversion.as_dict()["tier"] == "S3"
    assert "+35% to Cold Resistance" in converted.text


def test_build_item_text_accepts_dict_mod_entries() -> None:
    text = build_item_text(FETCH_ROWS[1]["item"])
    item = parse_item_text(text)
    assert "+30% to Cold Resistance" in text
    assert mods_by_template(item)["#% to cold resistance"] == 30
