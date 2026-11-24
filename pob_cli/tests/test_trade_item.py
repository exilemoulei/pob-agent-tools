"""Comprehensive unit tests for pob_cli.trade_item (PoE1 3.30 trade/PoB item model & Harvest rules)."""

from __future__ import annotations

import pytest

from pob_cli.trade_item import (
    ELEMENT_LABELS,
    ELEMENTAL_DAMAGE,
    ELEMENTS,
    POSITIVE_REDUCED_PATTERNS,
    RESISTANCE,
    ConvertedItem,
    HarvestGroup,
    HarvestMatch,
    build_item_text,
    classify_mod,
    conversion_note,
    conversion_summary,
    convert_item_text,
    convert_template,
    escape_ggg_string,
    is_convertible_template,
    make_mod,
    match_harvest,
    mods_by_template,
    normalize_template,
    parse_item_text,
    sanitize_text,
    trade_mod_tiers,
)

# ---------------------------------------------------------------------------
# 1. Sanitization, GGG string cleaning, and template normalization
# ---------------------------------------------------------------------------


class TestSanitizationAndNormalization:
    def test_sanitize_text_strips_control_collapses_spaces_and_truncates(self) -> None:
        raw = "  Hello\x00\x07\x1f  world\t\n  from\x7f PoE  "
        assert sanitize_text(raw) == "Hello world from PoE"
        assert sanitize_text("abcdefghij", limit=5) == "abcde"
        assert sanitize_text(None) == ""
        assert sanitize_text(0) == "0"

    def test_escape_ggg_string_handles_links_and_tags(self) -> None:
        assert escape_ggg_string("[Critical|Critical Hit]") == "Critical Hit"
        assert escape_ggg_string("[Critical]") == "Critical"
        assert escape_ggg_string("<augmented>{+42}") == "+42"
        assert (
            escape_ggg_string("<augmented>{+42}% to [Cold|Cold] [Resistance]")
            == "+42% to Cold Resistance"
        )
        assert (
            escape_ggg_string("Deals <fire>{[Ignite|Ignited]} damage on [Hit|Hits]")
            == "Deals Ignited damage on Hits"
        )

    def test_normalize_template_handles_tags_ggg_dashes_ranges_and_numbers(self) -> None:
        assert normalize_template("+42% to Cold Resistance") == "#% to cold resistance"
        assert normalize_template("{crafted}+(35\u201345)% to [Cold|Cold] Resistance.") == (
            "#% to cold resistance"
        )
        assert normalize_template("{fractured}<augmented>{+18}% to Fire Resistance,") == (
            "#% to fire resistance"
        )
        assert (
            normalize_template("Adds (10\u201415) to (25\u221230) Fire Damage to Attacks")
            == "adds # to # fire damage to attacks"
        )
        assert normalize_template("\u22128 to Total Mana Cost of Skills") == (
            "# to total mana cost of skills"
        )
        assert normalize_template("1.5% of Attack Damage Leeched as Life") == (
            "#% of attack damage leeched as life"
        )


# ---------------------------------------------------------------------------
# 2. Mod classification and signed_value (positive reduced bug fix)
# ---------------------------------------------------------------------------


class TestModClassificationAndSignedValue:
    @pytest.mark.parametrize(
        ("raw", "expected_cat", "expected_sign"),
        [
            ("8% reduced Mana Cost of Skills", "mana", 8.0),
            ("12% reduced Cost of Skills", "mana", 12.0),
            ("10% less Mana Cost of Skills", "mana", 10.0),
            ("25% reduced Attribute Requirements", "attributes", 25.0),
            ("20% reduced Enemy Stun Threshold", "defense", 20.0),
            ("30% reduced Extra Damage from Critical Strikes", "defense", 30.0),
            ("50% reduced Reflected Elemental Damage taken", "defense", 50.0),
            ("10% reduced Damage taken", "defense", 10.0),
            ("15% less Damage taken", "defense", 15.0),
            ("40% reduced Effect of Curses on you", "defense", 40.0),
            ("35% reduced Effect of Chill on you", "defense", 35.0),
            ("35% reduced Effect of Shock on you", "defense", 35.0),
            ("25% reduced Duration of Ailments on you", "defense", 25.0),
            ("15% reduced Soul Gain Prevention Duration", "other", 15.0),
        ],
    )
    def test_positive_reduced_mods_stay_positive_and_not_penalty(
        self, raw: str, expected_cat: str, expected_sign: float
    ) -> None:
        mod = make_mod(raw)
        assert mod.category == expected_cat
        assert mod.category != "penalty"
        assert mod.signed_value == pytest.approx(expected_sign)

    @pytest.mark.parametrize(
        ("raw", "expected_sign"),
        [
            ("10% reduced Movement Speed", -10.0),
            ("15% less Attack Speed", -15.0),
            ("20% slower start of Energy Shield Recharge", -20.0),
            ("12% decreased Global Defences", -12.0),
            ("5% lower Critical Strike Chance", -5.0),
        ],
    )
    def test_genuine_negative_mods_flip_negative(self, raw: str, expected_sign: float) -> None:
        mod = make_mod(raw)
        assert mod.signed_value == pytest.approx(expected_sign)

    def test_penalties_and_standard_categories(self) -> None:
        assert classify_mod("#% increased mana cost of skills") == "penalty"
        assert classify_mod("#% increased attribute requirements") == "penalty"
        assert classify_mod("#% increased mana reservation of skills") == "penalty"
        assert classify_mod("#% increased mana reservation efficiency of skills") == "mana"
        assert classify_mod("#% to cold resistance") == "resist"
        assert classify_mod("# to maximum life") == "life"
        assert classify_mod("# to maximum mana") == "mana"
        assert classify_mod("# to maximum energy shield") == "energy_shield"
        assert classify_mod("# to armour") == "armour"
        assert classify_mod("# to evasion rating") == "evasion"
        assert classify_mod("# to strength") == "attributes"
        assert classify_mod("# to level of all spell skill gems") == "skill_level"
        assert classify_mod("#% increased attack speed") == "speed"
        assert classify_mod("#% increased critical strike chance") == "crit"
        assert classify_mod("# to accuracy rating") == "accuracy"
        assert classify_mod("adds # to # fire damage") == "damage"
        assert classify_mod("#% increased rarity of items found") == "rarity"
        # PoE2-only terms dropped
        assert classify_mod("# to spirit") == "other"
        assert classify_mod("#% increased reload speed") == "other"

    def test_adds_damage_averages_min_max_values(self) -> None:
        mod = make_mod("Adds 12 to 28 Fire Damage to Attacks")
        assert mod.values == (12.0, 28.0)
        assert mod.signed_value == pytest.approx(20.0)
        assert mod.category == "damage"

    def test_word_boundary_prevents_false_negative_match(self) -> None:
        # "blessed" contains "less", "flower" contains "lower"
        mod = make_mod("10% increased Blessed Damage")
        assert mod.signed_value == pytest.approx(10.0)

    def test_positive_reduced_patterns_constant_has_all_required_entries(self) -> None:
        assert "reduced mana cost" in POSITIVE_REDUCED_PATTERNS
        assert "less damage taken" in POSITIVE_REDUCED_PATTERNS
        assert "less mana cost" in POSITIVE_REDUCED_PATTERNS


# ---------------------------------------------------------------------------
# 3. Harvest 3.30 rules (single-element only)
# ---------------------------------------------------------------------------


class TestHarvestRules:
    def test_groups_and_metadata(self) -> None:
        assert ELEMENTS == ("fire", "cold", "lightning")
        assert ELEMENT_LABELS == {"fire": "Fire", "cold": "Cold", "lightning": "Lightning"}
        assert isinstance(RESISTANCE, HarvestGroup)
        assert RESISTANCE.key == "resistance"
        assert RESISTANCE.kind == "resistance"
        assert RESISTANCE.patch == "3.30"
        assert "300 Lifeforce" in RESISTANCE.cost
        assert ELEMENTAL_DAMAGE.key == "elemental_damage"
        assert ELEMENTAL_DAMAGE.kind == "damage"
        assert "Crystallised Rancour" in ELEMENTAL_DAMAGE.cost
        assert ELEMENTAL_DAMAGE.patch == "3.30"

        d = RESISTANCE.as_dict("fire", "cold")
        assert d["group"] == "resistance"
        assert d["from"] == "fire"
        assert d["to"] == "cold"
        assert d["rerolled"] is True
        assert "Fire Resistance" in d["craft"]
        assert "Cold Resistance" in d["craft"]

    @pytest.mark.parametrize(
        ("template", "group_key", "elem", "subkind"),
        [
            ("#% to fire resistance", "resistance", "fire", "resistance"),
            ("#% to cold resistance", "resistance", "cold", "resistance"),
            ("#% to lightning resistance", "resistance", "lightning", "resistance"),
            ("adds # to # fire damage", "elemental_damage", "fire", "added_damage_local"),
            (
                "adds # to # cold damage to attacks",
                "elemental_damage",
                "cold",
                "added_damage_attacks",
            ),
            (
                "adds # to # lightning damage to spells",
                "elemental_damage",
                "lightning",
                "added_damage_spells",
            ),
            ("#% increased fire damage", "elemental_damage", "fire", "increased_damage"),
            ("#% increased cold damage", "elemental_damage", "cold", "increased_damage"),
            ("#% increased lightning damage", "elemental_damage", "lightning", "increased_damage"),
        ],
    )
    def test_match_harvest_positive_cases(
        self, template: str, group_key: str, elem: str, subkind: str
    ) -> None:
        found = match_harvest(template)
        assert isinstance(found, HarvestMatch)
        assert found.group.key == group_key
        assert found.element == elem
        assert found.subkind == subkind
        assert found.element_label == elem.title()
        assert is_convertible_template(template) is True

    @pytest.mark.parametrize(
        "template",
        [
            "#% to all elemental resistances",
            "#% to maximum fire resistance",
            "#% to fire and cold resistances",
            "damage penetrates #% fire resistance",
            "#% to chaos resistance",
            "adds # to # physical damage",
            "adds # to # chaos damage to attacks",
            "#% increased elemental damage with attack skills",
            "#% increased physical damage",
            "",
        ],
    )
    def test_match_harvest_rejects_non_single_element_mods(self, template: str) -> None:
        assert match_harvest(template) is None
        assert is_convertible_template(template) is False
        assert convert_template(template, "cold") is None
        assert conversion_note(template, "cold") == ""

    def test_convert_template_and_note(self) -> None:
        assert (
            convert_template("adds # to # fire damage to attacks", "lightning")
            == "adds # to # lightning damage to attacks"
        )
        assert convert_template("#% to cold resistance", "fire") == "#% to fire resistance"
        note = conversion_note("#% to fire resistance", "cold")
        assert "Fire -> Cold" in note
        assert "3.30" in note
        assert "300 Lifeforce" in note
        assert "randomized" in note.lower()

        with pytest.raises(ValueError, match="Unsupported conversion target"):
            convert_template("#% to fire resistance", "chaos")


# ---------------------------------------------------------------------------
# 4. build_item_text (PoE1 3.30 + legacy schemas)
# ---------------------------------------------------------------------------


class TestBuildItemText:
    def test_build_330_structured_item_with_properties_sockets_multiline_enchant_and_flags(
        self,
    ) -> None:
        fetch_row = {
            "id": "abc123",
            "listing": {"price": {"amount": 5, "currency": "divine"}},
            "item": {
                "rarity": "Rare",
                "name": "Bramble Claw",
                "typeLine": "Imperial Claw",
                "ilvl": 86,
                "properties": [
                    {"name": "Claws", "values": []},
                    {"name": "Quality", "values": [["<augmented>{+20%}", 1]]},
                    {"name": "Physical Damage", "values": [["50-100", 1]]},
                    {"name": "Elemental Damage", "values": [["10-20", 4], ["30-50", 5]]},
                    {"name": "Critical Strike Chance", "values": [["6.50%", 0]]},
                    {"name": "Attacks per Second", "values": [["1.65", 1]]},
                ],
                "sockets": [
                    {"group": 0, "sColour": "R"},
                    {"group": 0, "sColour": "G"},
                    {"group": 0, "sColour": "B"},
                    {"group": 1, "sColour": "R"},
                    {"group": 1, "sColour": "B"},
                    {"group": 2, "sColour": "W"},
                ],
                "enchantMods": [
                    {
                        "description": (
                            "Adds 8 Passive Skills\n"
                            "Added Small Passive Skills grant: 12% increased [Fire|Fire] Damage"
                        ),
                        "flags": {"enchant": True},
                    }
                ],
                "scourgeMods": [
                    {"description": "+10% to Cold Resistance", "flags": {}},
                ],
                "implicitMods": [
                    {"description": "Gain 46 Life for each Enemy hit by Attacks", "flags": {}},
                ],
                "explicitMods": [
                    {"description": "+42% to [Cold|Cold] Resistance", "flags": {"fractured": True}},
                    {"description": "+80 to maximum Life", "flags": {}},
                    {"description": "+25% to Fire Resistance", "flags": {"crafted": True}},
                    {"description": "10% increased Attack Speed", "flags": {"mutated": True}},
                ],
                "influences": {"shaper": True, "searing": True, "tangled": True},
                "warlord": True,
                "synthesised": True,
                "fractured": True,
                "split": True,
                "duplicated": True,
                "corrupted": True,
                "identified": False,
            },
        }

        text = build_item_text(fetch_row)
        lines = text.split("\n")

        assert lines[0] == "Rarity: RARE"
        assert lines[1] == "Bramble Claw"
        assert lines[2] == "Imperial Claw"
        assert "Claws" not in lines
        assert "Quality: +20%" in lines
        assert "Physical Damage: 50-100" in lines
        assert "Elemental Damage: 10-20, 30-50" in lines
        assert "Critical Strike Chance: 6.50%" in lines
        assert "Attacks per Second: 1.65" in lines
        assert "Item Level: 86" in lines
        assert "Sockets: R-G-B R-B W" in lines

        # Multiline enchant splits into 2 physical lines + 1 scourge + 1 implicit = 4
        assert "Implicits: 4" in lines
        imp_idx = lines.index("Implicits: 4")
        assert lines[imp_idx + 1] == "{enchant}Adds 8 Passive Skills"
        assert (
            lines[imp_idx + 2]
            == "{enchant}Added Small Passive Skills grant: 12% increased Fire Damage"
        )
        assert lines[imp_idx + 3] == "{scourge}+10% to Cold Resistance"
        assert lines[imp_idx + 4] == "Gain 46 Life for each Enemy hit by Attacks"

        assert "{fractured}+42% to Cold Resistance" in lines
        assert "+80 to maximum Life" in lines
        assert "{crafted}+25% to Fire Resistance" in lines
        assert "{mutated}10% increased Attack Speed" in lines

        assert "Shaper Item" in lines
        assert "Warlord Item" in lines
        assert "Searing Exarch Item" in lines
        assert "Eater of Worlds Item" in lines
        assert "Synthesised Item" in lines
        assert "Fractured Item" in lines
        assert "Split" in lines
        assert "Mirrored" in lines
        assert "Corrupted" in lines
        assert "Unidentified" in lines

        # Ensure round-trip through parse_item_text preserves every flag & mod kind cleanly
        parsed = parse_item_text(text)
        assert parsed.rarity == "RARE"
        assert parsed.name == "Bramble Claw"
        assert parsed.base_type == "Imperial Claw"
        assert parsed.item_level == 86
        assert parsed.synthesised is True
        assert parsed.fractured is True
        assert parsed.split is True
        assert parsed.mirrored is True
        assert parsed.corrupted is True
        assert parsed.unidentified is True
        assert set(parsed.influences) == {"Shaper", "Warlord", "Searing Exarch", "Eater of Worlds"}
        assert [m.kind for m in parsed.mods] == [
            "enchant",
            "enchant",
            "scourge",
            "implicit",
            "fractured",
            "explicit",
            "crafted",
            "mutated",
        ]

    def test_build_legacy_arrays_and_deduplicates_against_explicit(self) -> None:
        raw_item = {
            "frameType": 2,
            "name": "Storm Band",
            "baseType": "Two-Stone Ring",
            "itemLevel": 82,
            "implicitMods": ["+15% to Fire and Cold Resistances"],
            "explicitMods": [
                "+75 to maximum Life",
                "{crafted}+20% to Lightning Resistance",
            ],
            "fracturedMods": ["+45% to Fire Resistance"],
            "craftedMods": ["+20% to Lightning Resistance", "+55 to maximum Mana"],
            "mutatedMods": ["5% increased Cast Speed"],
            "veiledMods": ["Veiled Suffix"],
            "influences": {"elder": True, "hunter": True, "crusader": True, "redeemer": True},
        }
        text = build_item_text(raw_item)
        lines = text.split("\n")

        assert lines[0] == "Rarity: RARE"
        assert lines[1] == "Storm Band"
        assert lines[2] == "Two-Stone Ring"
        assert "Implicits: 1" in lines
        assert "{fractured}+45% to Fire Resistance" in lines
        # Already in explicitMods -> not duplicated
        assert lines.count("{crafted}+20% to Lightning Resistance") == 1
        # Not in explicitMods -> included
        assert "{crafted}+55 to maximum Mana" in lines
        assert "{mutated}5% increased Cast Speed" in lines
        assert "{veiled}Veiled Suffix" in lines
        assert "Elder Item" in lines
        assert "Hunter Item" in lines
        assert "Crusader Item" in lines
        assert "Redeemer Item" in lines

    def test_build_magic_item_omits_empty_name(self) -> None:
        raw_item = {
            "frameType": 1,
            "name": "",
            "typeLine": "Flaming Slink Gloves of the Kiln",
            "explicitMods": ["+42% to Fire Resistance"],
        }
        text = build_item_text(raw_item)
        assert text == (
            "Rarity: MAGIC\n"
            "Flaming Slink Gloves of the Kiln\n"
            "Implicits: 0\n"
            "+42% to Fire Resistance"
        )

    def test_build_item_text_validates_payload(self) -> None:
        with pytest.raises(ValueError, match="no base type"):
            build_item_text({"frameType": 2, "name": "Nameless"})
        with pytest.raises(ValueError, match="not an object"):
            build_item_text("bad")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. trade_mod_tiers (3.30 + legacy extended.mods.explicit)
# ---------------------------------------------------------------------------


class TestTradeModTiers:
    def test_extracts_330_structured_tiers(self) -> None:
        payload = {
            "item": {
                "explicitMods": [
                    {
                        "description": "+44% to [Cold|Cold] Resistance",
                        "flags": {},
                        "mods": [
                            {
                                "tier": "S2",
                                "name": "of the Yeti",
                                "level": 72,
                                "magnitudes": [{"hash": "explicit.stat_1", "min": "42", "max": "45"}],
                            }
                        ],
                    },
                    {
                        "description": "+92 to maximum Life",
                        "flags": {},
                        "mods": [
                            {
                                "tier": "P2",
                                "name": "Rapturous",
                                "level": 64,
                                "min": 90,
                                "max": 99,
                            }
                        ],
                    },
                ]
            }
        }
        tiers = trade_mod_tiers(payload)
        assert tiers["#% to cold resistance"] == {
            "tier": "S2",
            "name": "of the Yeti",
            "level": 72,
            "min": 42.0,
            "max": 45.0,
        }
        assert tiers["# to maximum life"] == {
            "tier": "P2",
            "name": "Rapturous",
            "level": 64,
            "min": 90.0,
            "max": 99.0,
        }

    def test_extracts_legacy_extended_tiers_via_hashes(self) -> None:
        payload = {
            "explicitMods": [
                "+85 to maximum Life",
                "+43% to Fire Resistance",
            ],
            "extended": {
                "mods": {
                    "explicit": [
                        {
                            "name": "of the Magma",
                            "tier": "S2",
                            "level": 72,
                            "magnitudes": [{"hash": "explicit.stat_fire", "min": "42", "max": "45"}],
                        },
                        {
                            "name": "Virile",
                            "tier": "P3",
                            "level": 54,
                            "magnitudes": [{"hash": "explicit.stat_life", "min": "80", "max": "89"}],
                        },
                    ]
                },
                "hashes": {
                    "explicit": [
                        ["explicit.stat_life", [1]],
                        ["explicit.stat_fire", [0]],
                    ]
                },
            },
        }
        tiers = trade_mod_tiers(payload)
        assert tiers["# to maximum life"] == {
            "tier": "P3",
            "name": "Virile",
            "level": 54,
            "min": 80.0,
            "max": 89.0,
        }
        assert tiers["#% to fire resistance"] == {
            "tier": "S2",
            "name": "of the Magma",
            "level": 72,
            "min": 42.0,
            "max": 45.0,
        }

    def test_extracts_legacy_extended_tiers_fallback_without_hashes(self) -> None:
        payload = {
            "explicitMods": [
                "+44% to Lightning Resistance",
                "+82 to maximum Life",
            ],
            "extended": {
                "mods": {
                    "explicit": [
                        {
                            "name": "Virile",
                            "tier": "P3",
                            "level": 54,
                            "magnitudes": [{"min": "80", "max": "89"}],
                        },
                        {
                            "name": "of the Lightning",
                            "tier": "S2",
                            "level": 72,
                            "magnitudes": [{"min": "42", "max": "45"}],
                        },
                    ]
                }
            },
        }
        tiers = trade_mod_tiers(payload)
        assert tiers["#% to lightning resistance"]["tier"] == "S2"
        assert tiers["# to maximum life"]["tier"] == "P3"


# ---------------------------------------------------------------------------
# 6. parse_item_text & mods_by_template
# ---------------------------------------------------------------------------


class TestParseItemText:
    def test_magic_single_line_header_does_not_swallow_first_mod(self) -> None:
        text = (
            "Rarity: MAGIC\n"
            "Flaming Slink Gloves of the Kiln\n"
            "+42% to Fire Resistance\n"
            "Adds 10 to 20 Fire Damage to Attacks"
        )
        item = parse_item_text(text)
        assert item.rarity == "MAGIC"
        assert item.name == ""
        assert item.base_type == "Flaming Slink Gloves of the Kiln"
        assert item.display_name == "Flaming Slink Gloves of the Kiln"
        assert len(item.mods) == 2
        assert item.mods[0].template == "#% to fire resistance"
        assert item.mods[1].template == "adds # to # fire damage to attacks"

    def test_unidentified_rare_single_line_header(self) -> None:
        text = (
            "Rarity: RARE\n"
            "Hubris Circlet\n"
            "Item Level: 84\n"
            "Unidentified"
        )
        item = parse_item_text(text)
        assert item.rarity == "RARE"
        assert item.name == ""
        assert item.base_type == "Hubris Circlet"
        assert item.item_level == 84
        assert item.unidentified is True
        assert item.mods == []

    def test_unidentified_unique_with_only_state_line_after_base(self) -> None:
        text = "Rarity: UNIQUE\nLeather Belt\nUnidentified"
        item = parse_item_text(text)
        assert item.rarity == "UNIQUE"
        assert item.name == ""
        assert item.base_type == "Leather Belt"
        assert item.unidentified is True
        assert item.mods == []

    def test_state_lines_and_influences_are_flags_not_mods(self) -> None:
        text = (
            "Item Class: Body Armours\n"
            "Rarity: RARE\n"
            "Apocalypse Shell\n"
            "Vaal Regalia\n"
            "Quality (Defense Modifiers): +20%\n"
            "Energy Shield: 620 (augmented)\n"
            "Item Level: 86\n"
            "Sockets: B-B-B-B-B-B\n"
            "Implicits: 1\n"
            "{implicit}+15% to Fire Resistance\n"
            "{crafted}+40 to maximum Energy Shield\n"
            "{fractured}+45% to Cold Resistance\n"
            "{enchant}Allocates Beef\n"
            "{scourge}+10% to Lightning Resistance\n"
            "{mutated}+20 to Intelligence\n"
            "{crucible}10% increased Attack Speed\n"
            "Shaper Item\n"
            "Elder Item\n"
            "Searing Exarch Item\n"
            "Eater of Worlds Item\n"
            "Synthesised Item\n"
            "Fractured Item\n"
            "Split\n"
            "Mirrored\n"
            "Corrupted\n"
        )
        item = parse_item_text(text)
        assert item.display_name == "Apocalypse Shell (Vaal Regalia)"
        assert item.corrupted is True
        assert item.mirrored is True
        assert item.synthesised is True
        assert item.fractured is True
        assert item.split is True
        assert item.influences == ["Shaper", "Elder", "Searing Exarch", "Eater of Worlds"]
        assert [m.kind for m in item.mods] == [
            "implicit",
            "crafted",
            "fractured",
            "enchant",
            "scourge",
            "mutated",
            "crucible",
        ]

    def test_mods_by_template_stacks_values(self) -> None:
        text = (
            "Rarity: RARE\n"
            "Test Ring\n"
            "Ruby Ring\n"
            "Implicits: 1\n"
            "+30% to Fire Resistance\n"
            "+42% to Fire Resistance\n"
            "8% reduced Mana Cost of Skills\n"
            "10% reduced Movement Speed"
        )
        item = parse_item_text(text)
        totals = mods_by_template(item)
        assert totals["#% to fire resistance"] == pytest.approx(72.0)
        assert totals["#% reduced mana cost of skills"] == pytest.approx(8.0)
        assert totals["#% reduced movement speed"] == pytest.approx(-10.0)
        assert mods_by_template(None) == {}

    def test_parse_item_text_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_item_text("   \n---\n  ")


# ---------------------------------------------------------------------------
# 7. Strict convert_item_text & conversion_summary
# ---------------------------------------------------------------------------


class TestConvertItemText:
    def test_refuses_corrupted_mirrored_unidentified_and_non_magic_rare(self) -> None:
        corrupted = "Rarity: RARE\nA\nRing\nImplicits: 0\n+42% to Fire Resistance\nCorrupted"
        res = convert_item_text(corrupted, "cold")
        assert res.conversions == []
        assert any("corrupted" in s.lower() for s in res.skipped)

        mirrored = "Rarity: RARE\nA\nRing\nImplicits: 0\n+42% to Fire Resistance\nMirrored"
        res = convert_item_text(mirrored, "cold")
        assert res.conversions == []
        assert any("mirrored" in s.lower() for s in res.skipped)

        unid = "Rarity: RARE\nRing\nUnidentified"
        res = convert_item_text(unid, "cold")
        assert res.conversions == []
        assert any("unidentified" in s.lower() for s in res.skipped)

        unique = "Rarity: UNIQUE\nBerek's Respite\nTwo-Stone Ring\nImplicits: 0\n+42% to Fire Resistance"
        res = convert_item_text(unique, "cold")
        assert res.conversions == []
        assert any("unique" in s.lower() for s in res.skipped)

        normal = "Rarity: NORMAL\nTwo-Stone Ring\nImplicits: 0\n+42% to Fire Resistance"
        res = convert_item_text(normal, "cold")
        assert res.conversions == []
        assert any("normal" in s.lower() for s in res.skipped)

        # force=True bypasses item-level refusal
        forced = convert_item_text(corrupted, "cold", force=True)
        assert len(forced.conversions) == 1
        assert forced.conversions[0].line == "+42% to Cold Resistance"

    def test_refuses_all_non_explicit_mod_kinds(self) -> None:
        text = (
            "Rarity: RARE\n"
            "Test\n"
            "Ruby Ring\n"
            "Implicits: 3\n"
            "{enchant}+10% to Fire Resistance\n"
            "{scourge}+11% to Fire Resistance\n"
            "+30% to Fire Resistance\n"
            "{fractured}+40% to Fire Resistance\n"
            "{crafted}+25% to Fire Resistance\n"
            "{crucible}+15% to Fire Resistance\n"
            "{mutated}+16% to Fire Resistance\n"
            "{veiled}+17% to Fire Resistance\n"
            "{rune}+18% to Fire Resistance\n"
        )
        res = convert_item_text(text, "cold")
        assert res.conversions == []
        assert len(res.skipped) == 9
        for kind in (
            "enchant",
            "scourge",
            "implicit",
            "fractured",
            "crafted",
            "crucible",
            "mutated",
            "veiled",
            "rune",
        ):
            assert any(f"{kind} modifiers cannot be converted" in s for s in res.skipped)

    def test_skips_when_explicit_or_fractured_target_already_exists_in_same_group(self) -> None:
        # Existing explicit target
        explicit_conflict = (
            "Rarity: RARE\n"
            "Storm Grip\n"
            "Slink Gloves\n"
            "Implicits: 0\n"
            "+42% to Cold Resistance\n"
            "+45% to Fire Resistance"
        )
        res = convert_item_text(explicit_conflict, "cold")
        assert res.conversions == []
        assert any("already has target cold modifier in the same group" in s for s in res.skipped)

        # Existing fractured target
        fractured_conflict = (
            "Rarity: RARE\n"
            "Storm Grip\n"
            "Slink Gloves\n"
            "Implicits: 0\n"
            "{fractured}+42% to Cold Resistance\n"
            "+45% to Fire Resistance"
        )
        res_frac = convert_item_text(fractured_conflict, "cold")
        assert res_frac.conversions == []
        assert any(
            "already has target cold modifier in the same group" in s for s in res_frac.skipped
        )

    def test_allows_conversion_with_warning_when_crafted_target_exists(self) -> None:
        text = (
            "Rarity: RARE\n"
            "Storm Grip\n"
            "Slink Gloves\n"
            "Implicits: 0\n"
            "+45% to Fire Resistance\n"
            "{crafted}+25% to Cold Resistance"
        )
        res = convert_item_text(text, "cold")
        assert len(res.conversions) == 1
        assert res.conversions[0].line == "+45% to Cold Resistance"
        assert res.warnings == [
            "Item has a crafted target modifier; remove it at the crafting bench before Harvest conversion."
        ]

    def test_converts_highest_signed_value_only_within_same_group_subkind(self) -> None:
        text = (
            "Rarity: RARE\n"
            "Dual Res Ring\n"
            "Coral Ring\n"
            "Implicits: 0\n"
            "+32% to Fire Resistance\n"
            "+46% to Lightning Resistance\n"
            "Adds 10 to 20 Fire Damage to Attacks\n"
            "Adds 20 to 40 Lightning Damage to Attacks\n"
            "25% increased Fire Damage"
        )
        tiers = {
            "#% to lightning resistance": {"tier": "S1", "name": "of Tzteosh", "level": 84, "min": 46.0, "max": 48.0},
            "adds # to # lightning damage to attacks": {
                "tier": "P1",
                "name": "Electrocuting",
                "level": 80,
                "min": 20.0,
                "max": 40.0,
            },
        }
        res = convert_item_text(text, "cold", tiers=tiers)

        # 3 distinct (group, subkind) pairs converted, picking the strongest candidate in each
        assert len(res.conversions) == 3
        by_subkind = {c.subkind: c for c in res.conversions}
        assert by_subkind["resistance"].element == "lightning"
        assert by_subkind["resistance"].line == "+46% to Cold Resistance"
        assert by_subkind["resistance"].tier == "S1"
        assert by_subkind["resistance"].tier_range == "46-48"
        assert by_subkind["resistance"].as_dict()["rerolled"] is True

        assert by_subkind["added_damage_attacks"].element == "lightning"
        assert by_subkind["added_damage_attacks"].line == "Adds 20 to 40 Cold Damage to Attacks"
        assert by_subkind["added_damage_attacks"].tier == "P1"

        assert by_subkind["increased_damage"].element == "fire"
        assert by_subkind["increased_damage"].line == "25% increased Cold Damage"

        # Weaker duplicates skipped with clear reason
        assert len(res.skipped) == 2
        assert any("+32% to Fire Resistance" in s for s in res.skipped)
        assert any("Adds 10 to 20 Fire Damage to Attacks" in s for s in res.skipped)

        # Original weaker mods stay untouched in converted text
        assert "+32% to Fire Resistance" in res.text
        assert "+46% to Cold Resistance" in res.text
        assert "Adds 10 to 20 Fire Damage to Attacks" in res.text
        assert "Adds 20 to 40 Cold Damage to Attacks" in res.text

        summary = conversion_summary(res)
        assert "#% to lightning resistance -> #% to cold resistance [S1 46-48]" in summary
        assert "tier kept, roll re-rolled" in summary

    def test_group_filter_limits_conversion_family(self) -> None:
        text = (
            "Rarity: RARE\n"
            "Woe Grip\n"
            "Slink Gloves\n"
            "Implicits: 0\n"
            "+42% to Fire Resistance\n"
            "Adds 12 to 24 Fire Damage to Attacks"
        )
        res_only = convert_item_text(text, "cold", group="resistance")
        assert len(res_only.conversions) == 1
        assert res_only.conversions[0].group == "resistance"
        assert "Adds 12 to 24 Fire Damage to Attacks" in res_only.text

        dmg_only = convert_item_text(text, "cold", group="elemental_damage")
        assert len(dmg_only.conversions) == 1
        assert dmg_only.conversions[0].group == "elemental_damage"
        assert "+42% to Fire Resistance" in dmg_only.text

    def test_magic_item_conversion_succeeds(self) -> None:
        text = (
            "Rarity: MAGIC\n"
            "Flaming Slink Gloves of the Kiln\n"
            "+42% to Fire Resistance"
        )
        res = convert_item_text(text, "lightning")
        assert len(res.conversions) == 1
        assert res.conversions[0].line == "+42% to Lightning Resistance"
        assert "+42% to Lightning Resistance" in res.text

    def test_unsupported_target_and_empty_summary(self) -> None:
        text = "Rarity: RARE\nA\nRing\nImplicits: 0\n+80 to maximum Life"
        with pytest.raises(ValueError, match="Unsupported conversion target"):
            convert_item_text(text, "chaos")

        res = convert_item_text(text, "cold")
        assert isinstance(res, ConvertedItem)
        assert res.conversions == []
        assert conversion_summary(res) == "no convertible modifiers found"
