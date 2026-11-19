"""Unit tests for the Harvest equivalent rules (no network, no PoB)."""

from __future__ import annotations

import pytest

from pob_cli import equivalences


def test_match_single_element_resistance() -> None:
    found = equivalences.match("#% to cold resistance")
    assert found is not None
    assert found.group is equivalences.RESISTANCE
    assert found.element == "cold"


def test_match_damage_variants() -> None:
    cases = (
        ("adds # to # fire damage", "fire"),
        ("adds # to # lightning damage to attacks", "lightning"),
        ("adds # to # cold damage to spells", "cold"),
        ("#% increased fire damage", "fire"),
    )
    for template, element in cases:
        found = equivalences.match(template)
        assert found is not None, template
        assert found.group is equivalences.ELEMENTAL_DAMAGE
        assert found.element == element


@pytest.mark.parametrize(
    "template",
    [
        "#% to all elemental resistances",
        "#% to maximum fire resistance",
        "adds # to # chaos damage",
        "adds # to # physical damage",
        "#% to fire and cold resistances",
        "#% increased elemental damage",
        "adds # to # fire damage to attacks and spells",
    ],
)
def test_non_convertible_templates(template: str) -> None:
    assert equivalences.match(template) is None


def test_convert_template_keeps_shape() -> None:
    assert (
        equivalences.convert_template("#% to cold resistance", "fire")
        == "#% to fire resistance"
    )
    assert (
        equivalences.convert_template("adds # to # fire damage to attacks", "cold")
        == "adds # to # cold damage to attacks"
    )
    assert (
        equivalences.convert_template("#% increased lightning damage", "cold")
        == "#% increased cold damage"
    )


def test_convert_template_rejects_unknown_target() -> None:
    with pytest.raises(ValueError):
        equivalences.convert_template("#% to cold resistance", "physical")


def test_group_metadata_and_note() -> None:
    assert equivalences.RESISTANCE.patch == "3.30"
    assert "300" in equivalences.RESISTANCE.cost
    assert "Crystallised Rancour" in equivalences.ELEMENTAL_DAMAGE.cost

    payload = equivalences.RESISTANCE.as_dict("fire", "cold")
    assert payload["rerolled"] == "true"
    assert "similar-tier" in payload["craft"]

    note = equivalences.conversion_note("#% to fire resistance", "cold")
    assert "re-check" in note
