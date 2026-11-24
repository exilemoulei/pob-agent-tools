"""Affix occupancy must be conservative when item lines are incomplete or ambiguous."""

from __future__ import annotations

import pytest

from pob_cli import item_inspect
from pob_cli.trade_item import make_mod


def _entry(ident: str, kind: str, group: str, *lines: str, level: int = 1, source: str = "Explicit") -> dict:
    return {
        "id": ident, "source": source,
        "generation_type": f"{'Bench ' if source == 'Bench' else ''}{kind}",
        "group": group, "affix": ident, "level": level, "stats": list(lines),
    }


def test_hybrid_uses_one_prefix_and_reports_open_slots() -> None:
    entries = [
        _entry("hybrid", "Prefix", "ESAndStun", "(6-13)% increased Energy Shield", "(6-7)% increased Stun and Block Recovery"),
        _entry("plain", "Prefix", "EnergyShield", "(6-13)% increased Energy Shield"),
        _entry("fire", "Suffix", "FireResistance", "+(30-35)% to Fire Resistance"),
    ]
    mods = [make_mod("9% increased Energy Shield"), make_mod("6% increased Stun and Block Recovery"),
            make_mod("+34% to Fire Resistance")]
    result = item_inspect._classify(mods, entries)
    assert result["certain"] is True
    assert result["counts"] == {"prefix": 1, "suffix": 1}
    assert result["open"] == {"prefix": 2, "suffix": 2}
    assert result["affixes"][0]["lines"] == [mods[0].raw, mods[1].raw]


def test_ambiguous_or_unmatched_lines_hide_open_slot_count() -> None:
    entries = [
        _entry("prefix", "Prefix", "RarityPrefix", "(30-50)% increased Rarity of Items found"),
        _entry("suffix", "Suffix", "RaritySuffix", "(30-50)% increased Rarity of Items found"),
    ]
    result = item_inspect._classify([make_mod("45% increased Rarity of Items found")], entries)
    assert result["certain"] is False
    assert result["open"] == {"prefix": None, "suffix": None}
    assert result["affixes"][0]["ambiguous"] is True

    missing = item_inspect._classify([make_mod("Unlisted modifier")], entries)
    assert missing["unmatched"] == ["Unlisted modifier"]
    assert missing["open"] == {"prefix": None, "suffix": None}


def test_crafted_lines_only_match_bench_source() -> None:
    entries = [
        _entry("natural", "Suffix", "ColdResistance", "+(21-28)% to Cold Resistance"),
        _entry("crafted", "Suffix", "ColdResistance", "+(21-28)% to Cold Resistance", source="Bench"),
    ]
    result = item_inspect._classify([make_mod("+24% to Cold Resistance", kind="crafted")], entries)
    assert result["certain"] is True
    assert result["affixes"][0]["source"] == "Bench"


def test_unique_item_never_reports_craftable_space(monkeypatch) -> None:
    monkeypatch.setattr(item_inspect, "_load_entries", lambda *args, **kwargs: ([], False, frozenset()))
    report = item_inspect.inspect_item("Rarity: UNIQUE\nGold Ring\nItem Level: 80\n", slot="Ring 1")
    assert report["craftEligible"] is False
    assert report["open"] == {"prefix": None, "suffix": None}
    assert report["suggestions"] == []


def test_cogwork_ring_capacity_and_scaled_suffix_rolls(monkeypatch) -> None:
    entries = [
        _entry("life", "Prefix", "Life", "+(90-109) to maximum Life"),
        _entry("strength", "Suffix", "Strength", "+(60-79) to Strength"),
        _entry("fire", "Suffix", "FireResistance", "+(40-47)% to Fire Resistance"),
    ]
    monkeypatch.setattr(item_inspect, "_load_entries", lambda *args, **kwargs: (entries, True, frozenset()))
    raw = "\n".join([
        "Rarity: RARE", "Test Ring", "Cogwork Ring", "Item Level: 85", "Implicits: 3",
        "-1 Prefix Modifier allowed", "+1 Suffix Modifier allowed",
        "25% increased Suffix Modifier magnitudes", "+100 to maximum Life",
        "+75 to Strength", "+45% to Fire Resistance",
    ])
    report = item_inspect.inspect_item(raw, slot="Ring 2")
    assert report["capacity"] == {"prefix": 2, "suffix": 4}
    assert report["counts"] == {"prefix": 1, "suffix": 2}
    assert report["open"] == {"prefix": 1, "suffix": 2}
    assert report["scaledAffixTypes"] == ["suffix"]
    assert all(not affix["tierLevels"] for affix in report["affixes"] if affix["type"] == "suffix")


def test_replacing_suffix_does_not_free_a_prefix(monkeypatch) -> None:
    from types import SimpleNamespace
    entries = [_entry('life', 'Prefix', 'Life', '+100 to maximum Life'),
               _entry('mana', 'Prefix', 'Mana', '+50 to maximum Mana'),
               _entry('armour', 'Prefix', 'Armour', '+100 to Armour'),
               _entry('res', 'Suffix', 'FireResistance', '+20% to Fire Resistance', source='Bench')]
    monkeypatch.setattr(item_inspect, '_load_entries', lambda *a, **k: (entries, True, frozenset()))
    recipe = SimpleNamespace(group='AreaDamage', affix='prefix', level=1)
    monkeypatch.setattr(item_inspect.bench, 'search', lambda *a, **k: [SimpleNamespace(score=1, recipe=recipe)])
    monkeypatch.setattr(item_inspect.bench, 'can_craft_on_item', lambda *a, **k: (True, ''))
    raw = '\n'.join(['Rarity: RARE', 'Test', 'Iron Ring', 'Item Level: 85', 'Implicits: 0',
                     '+100 to maximum Life', '+50 to maximum Mana', '+100 to Armour',
                     '{crafted}+20% to Fire Resistance'])
    report = item_inspect.inspect_item(raw, slot='Ring 1', wants=('Area Damage',))
    assert report['certain']
    assert report['open']['prefix'] == 0
    assert report['suggestions'] == []


@pytest.mark.parametrize('identified', [True, False])
def test_unidentified_item_has_unknown_capacity(monkeypatch, identified):
    monkeypatch.setattr(item_inspect, '_load_entries', lambda *a, **k: ([], True, frozenset()))
    monkeypatch.setattr(item_inspect.bench, 'search', lambda *a, **k: [])
    raw = 'Rarity: RARE\nTest Ring\nIron Ring\nItem Level: 85\nImplicits: 0\n'
    if not identified:
        raw += 'Unidentified\n'
    report = item_inspect.inspect_item(raw, slot='Ring 1')
    assert report['certain'] is identified
    assert report['open'] == ({'prefix': 3, 'suffix': 3} if identified else {'prefix': None, 'suffix': None})
    assert report['craftEligible'] is identified


@pytest.mark.parametrize('raw', ['', '   \n'])
def test_inspect_empty_input_is_clean_cli_error(raw):
    from click.testing import CliRunner

    from pob_cli.cli import main
    result = CliRunner().invoke(main, ['--offline', 'item', 'inspect', '--raw', raw])
    assert result.exit_code == 1
    assert 'Error:' in result.output
    assert 'Traceback' not in result.output
    assert not isinstance(result.exception, ValueError)


def test_merged_line_outside_any_tier_hides_open_slots(monkeypatch) -> None:
    # Pure ES% and the ES% part of a hybrid display as one summed line; it fits no
    # single tier, so the item may have a hidden third prefix.
    entries = [
        _entry("es", "Prefix", "LocalEnergyShieldPercent", "(80-91)% increased Energy Shield"),
        _entry("life", "Prefix", "IncreasedLife", "+(90-99) to maximum Life"),
        _entry("fire", "Suffix", "FireResistance", "+(36-41)% to Fire Resistance"),
    ]
    monkeypatch.setattr(item_inspect, "_load_entries", lambda *a, **k: (entries, True, frozenset()))
    raw = "\n".join(["Rarity: RARE", "Doom Shell", "Vaal Regalia", "Item Level: 86", "Implicits: 0",
                     "136% increased Energy Shield", "+98 to maximum Life", "+40% to Fire Resistance"])
    report = item_inspect.inspect_item(raw, slot="Body Armour", wants=("Energy Shield",))
    assert report["certain"] is False
    assert report["open"] == {"prefix": None, "suffix": None}
    assert any("fits no single tier" in w for w in report["dataWarnings"])
    assert all(s["status"] == "verify_item" for s in report["suggestions"])


def test_local_defence_craft_requires_matching_base_defence() -> None:
    from types import SimpleNamespace
    armour = SimpleNamespace(id="Master_LocalPhysicalDamageReductionRating_Prefix_30_132",
                             lines=("+(50-75) to Armour",), text="+(50-75) to Armour")
    hybrid = SimpleNamespace(id="Master_LocalArmourAndEnergyShield_Prefix_30_154",
                             lines=("(26-35)% increased Armour and Energy Shield",), text="")
    global_es = SimpleNamespace(id="Master_EnergyShield_Prefix_50_149",
                                lines=("+(21-25) to maximum Energy Shield",), text="")
    es_base = frozenset({"armour", "body_armour", "int_armour", "default"})
    str_int_base = frozenset({"armour", "shield", "str_int_armour", "str_int_shield"})
    assert not item_inspect._defence_craft_fits_base(armour, es_base)
    assert not item_inspect._defence_craft_fits_base(hybrid, es_base)
    assert item_inspect._defence_craft_fits_base(hybrid, str_int_base)
    assert item_inspect._defence_craft_fits_base(global_es, frozenset({"ring", "default"}))
    assert item_inspect._defence_craft_fits_base(armour, frozenset())
