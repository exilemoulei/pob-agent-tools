"""Tests for zero-noise PoE1 3.30 Volatile Vaal Orb diagnostic opportunity hints."""

from __future__ import annotations

from pob_cli.analyzer.volatile_vaal import detect_volatile_vaal_opportunity
from pob_cli.formatters import format_diagnose_markdown


def test_volatile_vaal_fires_for_uncorrupted_core_uniques_only() -> None:
    slots = [
        {
            "slotName": "Body Armour",
            "item": {"title": "The Ivory Tower", "rarity": "UNIQUE"},
            "raw": (
                "Rarity: UNIQUE\nThe Ivory Tower\nSaint's Hauberk\n"
                "+60 to Intelligence\n+30 to maximum Energy Shield per 100 Reserved Life\n"
            ),
        },
        {
            "slotName": "Weapon 1",
            "item": {"title": "Ephemeral Edge", "rarity": "UNIQUE"},
            "raw": "Rarity: UNIQUE\nEphemeral Edge\nDusk Blade\n45% increased maximum Energy Shield\n",
        },
    ]
    hint = detect_volatile_vaal_opportunity(equipped_slots_raw=slots)
    assert hint is not None
    assert hint["tag"] == "[VOLATILE-VAAL]"
    assert [c["name"] for c in hint["candidates"]] == ["The Ivory Tower", "Ephemeral Edge"]
    assert "0.78x-1.22x in 0.01 steps" in hint["message"]
    assert "50% destroy risk" in hint["message"]
    assert "in-game preview" in hint["message"]
    assert "values 1 and 2 cannot change due to rounding" in hint["message"]
    assert "check live corrupted supply on official trade" in hint["message"]

    md = format_diagnose_markdown(
        {
            "buildInfo": {"level": 95, "className": "Scion", "ascendClassName": "Reliquarian"},
            "buildMechanics": {"volatileVaal": hint},
        }
    )
    assert "[VOLATILE-VAAL]" in md
    assert "The Ivory Tower" in md


def test_volatile_vaal_zero_noise_on_corrupted_rare_magic_and_ordinary_builds() -> None:
    # Already corrupted core uniques -> 0 output
    corrupted_slots = [
        {
            "slotName": "Body Armour",
            "item": {"title": "The Ivory Tower", "rarity": "UNIQUE"},
            "raw": "Rarity: UNIQUE\nThe Ivory Tower\nSaint's Hauberk\nCorrupted\n",
        },
        {
            "slotName": "Belt",
            "item": {"title": "Mageblood", "rarity": "UNIQUE", "corrupted": True},
        },
    ]
    assert detect_volatile_vaal_opportunity(equipped_slots_raw=corrupted_slots) is None

    # Rare & Magic items + ordinary non-core unique -> 0 output
    ordinary_slots = [
        {"slotName": "Body Armour", "item": {"title": "Doom Coat", "rarity": "RARE"}},
        {"slotName": "Belt", "item": {"title": "Heavy Belt of Warding", "rarity": "MAGIC"}},
        {"slotName": "Ring 1", "item": {"title": "Praxis", "rarity": "UNIQUE"}},
        {"slotName": "Weapon 1 Swap", "item": {"title": "Ephemeral Edge", "rarity": "UNIQUE"}},
    ]
    assert detect_volatile_vaal_opportunity(equipped_slots_raw=ordinary_slots) is None

    md = format_diagnose_markdown(
        {"buildInfo": {"level": 90, "className": "Witch", "ascendClassName": "Necromancer"}}
    )
    assert "VOLATILE-VAAL" not in md


def test_volatile_vaal_reliquarian_shadowstitch_synergy() -> None:
    slots = [
        {"slotName": "Ring 1", "item": {"title": "Praxis", "rarity": "UNIQUE"}},
        {"slotName": "Body Armour", "item": {"title": "Shadowstitch", "rarity": "UNIQUE"}, "raw": "Corrupted\n"},
    ]
    hint = detect_volatile_vaal_opportunity(
        equipped_slots_raw=slots,
        build_info={"ascendClassName": "Reliquarian"},
        ascendancy_nodes=[
            {
                "name": "Shadowstitch",
                "asc": "Reliquarian",
                "stats": ["6% increased Maximum Energy Shield for each Corrupted Item Equipped"],
            }
        ],
    )
    assert hint is not None
    assert hint["reliquarianCorruptedSynergy"] is True
    assert [c["name"] for c in hint["candidates"]] == ["Praxis"]
    assert hint["candidates"][0]["reason"] == "reliquarian_corrupted_synergy"
