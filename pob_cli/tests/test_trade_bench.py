"""Tests for the offline crafting-bench dataset and `pob bench` commands."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from pob_cli import bench
from pob_cli.cli_bench import bench_group


def test_dataset_metadata() -> None:
    meta = bench.source_metadata()
    assert "poedb.tw" in meta["cn"]
    assert "poedb.tw" in meta["en"]
    assert meta["fetched"]
    assert "3.30" in meta["gameVersion"]
    assert len(bench.recipes()) > 500


def test_gloves_cold_resistance_is_a_suffix() -> None:
    results = bench.search("gloves", "to Cold Resistance")
    assert results
    best = results[0].recipe
    assert best.affix == "suffix"
    assert "cold resistance" in best.text.lower()
    assert best.min_value <= best.max_value
    assert best.cost


def test_gloves_maximum_life_is_a_prefix() -> None:
    best = bench.best("gloves", "maximum Life")
    assert best is not None
    assert best.affix == "prefix"
    assert "life" in best.text.lower()
    assert best.min_value >= 15


def test_list_for_slot_filters_affix() -> None:
    items = bench.list_for_slot("gloves", affix="suffix")
    assert items
    assert all(recipe.affix == "suffix" for recipe in items)
    assert all("gloves" in recipe.slots for recipe in items)


def test_slot_aliases() -> None:
    assert bench.slot_key("ring") == "ring1"
    assert bench.slot_key("Weapon 1") == "weapon1"
    assert bench.recipes_for_slot("nonsense") == []


def test_recipe_roll_text_and_dict() -> None:
    recipe = bench.best("gloves", "to Cold Resistance")
    assert recipe is not None
    payload = recipe.as_dict()
    assert payload["affix"] == "suffix"
    assert payload["values"]
    assert "-" in recipe.roll_text or recipe.roll_text.replace("-", "").isdigit()


def test_bench_list_cli_json() -> None:
    result = CliRunner().invoke(
        bench_group, ["list", "--slot", "gloves", "--search", "cold resistance", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] >= 1
    assert any(item["affix"] == "suffix" for item in payload["recipes"])
    assert payload["source"]["fetched"]


def test_bench_list_cli_markdown() -> None:
    result = CliRunner().invoke(bench_group, ["list", "--slot", "gloves", "--affix", "prefix"])
    assert result.exit_code == 0, result.output
    assert "Modifier" in result.output


def test_bench_suggest_cli() -> None:
    result = CliRunner().invoke(
        bench_group, ["suggest", "--slot", "gloves", "--stat", "to Cold Resistance"]
    )
    assert result.exit_code == 0, result.output
    assert "suffix" in result.output
    assert "open suffix" in result.output


def test_bench_suggest_unknown_stat_exit_code() -> None:
    result = CliRunner().invoke(
        bench_group, ["suggest", "--slot", "gloves", "--stat", "to Unicorn Resistance"]
    )
    assert result.exit_code == 1


@pytest.mark.parametrize("slot", ["helmet", "boots", "amulet", "ring1"])
def test_every_equipment_slot_has_recipes(slot: str) -> None:
    assert bench.recipes_for_slot(slot)
