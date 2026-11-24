"""Trade bench variants retain the listing and require certain legal occupancy."""
from types import SimpleNamespace

import pytest

from pob_cli import trade_craft
from pob_cli.trade_eval import Candidate, format_report, report_json
from pob_cli.trade_item import parse_item_text

RAW = 'Rarity: RARE\nTest Hand\nConjurer Gloves\nItem Level: 85\nImplicits: 0\n+115 to maximum Life\n'


@pytest.mark.parametrize('action', ['open', 'replace_craft'])
def test_expand_keeps_original_and_replaces_only_bench_lines(monkeypatch, action):
    raw = RAW + ('{crafted}42% increased Damage while Leeching\n' if action == 'replace_craft' else '')
    candidate = Candidate(raw={}, item=parse_item_text(raw), item_text=raw, listing_id='listing', price_amount=8)
    report = dict(certain=True, craftEligible=True, item={'itemLevel':85}, scaledAffixTypes=[],
                  affixes=([{'source':'Bench','group':'DamageWhileLeeching','lines':['42% increased Damage while Leeching']}] if action == 'replace_craft' else []),
                  suggestions=[{'status':action,'affix':'prefix','want':'Area Damage','id':'area'}])
    monkeypatch.setattr(trade_craft.item_inspect, 'inspect_item', lambda *a, **k: report)
    recipe = SimpleNamespace(id='area', cost='4x Exalted Orb',
        crafted_lines=lambda *, use_min: [f"{{crafted}}{14 if use_min else 16}% increased Area Damage"])
    monkeypatch.setattr(trade_craft.bench, 'search', lambda *a, **k: [SimpleNamespace(recipe=recipe)])
    result = trade_craft.expand_bench_candidates([candidate], slot='Gloves', wants=('Area Damage',))
    assert result[0] is candidate
    assert len(result) == 2
    variant = result[1]
    assert variant.listing_id == 'listing' and variant.price_amount == 8
    assert '+115 to maximum Life' in variant.item_text
    assert 'Damage while Leeching' not in variant.item_text
    assert '{crafted}16% increased Area Damage' in variant.item_text
    assert variant.bench_craft['roll'] == 'maximum'
    assert variant.bench_craft['listed_item_text'] == raw
    assert report_json([variant])[0]['bench_craft']['action'] == action
    text = format_report(build_label='test', slot='Gloves', current=None,
                         candidates=[variant], league='Standard')
    if action == 'replace_craft':
        assert 'Removed craft: 42% increased Damage while Leeching' in text
    else:
        assert 'Removed craft:' not in text
    report['certain'] = False
    assert trade_craft.expand_bench_candidates([candidate], slot='Gloves', wants=('Area Damage',)) == [candidate]


@pytest.mark.parametrize('slot,item_type,maximum', [
    ('weapon1', 'Wand', 66), ('wand', 'Wand', 66), ('Weapon 2', 'Wand', 66),
    ('weapon1', 'Staff', 115), ('staff', 'Staff', 115),
])
def test_weapon_crafts_follow_actual_base_class(monkeypatch, tmp_path, slot, item_type, maximum):
    import json
    import sqlite3

    from pob_cli.bench import BenchRecipe, classes_to_slots

    db = tmp_path / 'game.sqlite'
    recipes = []
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE bases (name_lower TEXT, zh_lower TEXT, item_type TEXT, data_json TEXT)')
        conn.execute('INSERT INTO bases VALUES (?, ?, ?, ?)',
                     ('test base', '', item_type, json.dumps({'tags': [item_type.lower()]})))
        conn.execute('CREATE TABLE mods (source TEXT, weights_json TEXT, zero_weights_json TEXT, types_json TEXT, data_json TEXT)')
        natural = {'id':'cast', 'source':'Explicit', 'generation_type':'Suffix',
                   'group':'IncreasedCastSpeed', 'level':1, 'stats':['12% increased Cast Speed']}
        conn.execute('INSERT INTO mods VALUES (?, ?, ?, ?, ?)',
                     ('Explicit', json.dumps({'wand':1, 'staff':1}), '[]', '[]', json.dumps(natural)))
        for cls, top in [('Wand', 66), ('Staff', 115)]:
            text = f'(1-{top})% increased Spell Damage'
            recipe = BenchRecipe(id=cls, lines=(text,), lines_zh=(), text=text, text_zh='',
                template='#% increased spell damage', affix='prefix', group='SpellDamage', level=1,
                slots=classes_to_slots([cls]), classes=(cls,), values=((1, top),), cost='test', unlock='')
            recipes.append(recipe)
            entry = {'id':cls, 'source':'Bench', 'generation_type':'Bench Prefix',
                     'group':'SpellDamage', 'level':1, 'stats':[text]}
            conn.execute('INSERT INTO mods VALUES (?, ?, ?, ?, ?)',
                         ('Bench', '{}', '[]', json.dumps([cls]), json.dumps(entry)))
    monkeypatch.setattr(trade_craft.item_inspect.game_data, 'ensure_gamedata_index', lambda **k: db)
    monkeypatch.setattr(trade_craft.bench, 'load_recipes', lambda **k: (tuple(recipes), {}))
    raw = 'Rarity: RARE\nTest Weapon\nTest Base\nItem Level: 85\nImplicits: 0\n12% increased Cast Speed'
    candidate = Candidate(raw={}, item=parse_item_text(raw), item_text=raw)
    report = trade_craft.item_inspect.inspect_item(raw, slot=slot, wants=('increased Spell Damage',))
    assert report['benchSlot'] == item_type.lower()
    assert report['suggestions'][0]['id'] == item_type
    variants = trade_craft.expand_bench_candidates([candidate], slot=slot, wants=('increased Spell Damage',))
    assert len(variants) == 2
    assert f'{{crafted}}{maximum}% increased Spell Damage' in variants[1].item_text


def test_bench_craft_coexists_with_weighted_sum_report(monkeypatch):
    candidate = Candidate(
        raw={},
        item=parse_item_text(RAW),
        item_text=RAW,
        listing_id='listing-sum',
        price_amount=5,
        price_currency='divine',
        weighted_sum=92.0,
    )
    report = dict(
        certain=True,
        craftEligible=True,
        item={'itemLevel': 85},
        scaledAffixTypes=[],
        affixes=[],
        suggestions=[{'status': 'open', 'affix': 'prefix', 'want': 'Area Damage', 'id': 'area'}],
    )
    monkeypatch.setattr(trade_craft.item_inspect, 'inspect_item', lambda *a, **k: report)
    recipe = SimpleNamespace(
        id='area',
        cost='4x Exalted Orb',
        crafted_lines=lambda *, use_min: ['{crafted}16% increased Area Damage'],
    )
    monkeypatch.setattr(trade_craft.bench, 'search', lambda *a, **k: [SimpleNamespace(recipe=recipe)])
    variants = trade_craft.expand_bench_candidates([candidate], slot='Gloves', wants=('Area Damage',))
    assert len(variants) == 2
    assert variants[1].weighted_sum == 92.0
    text = format_report(build_label='test', slot='Gloves', current=None, candidates=variants, league='Standard')
    table_lines = [ln for ln in text.splitlines() if ln.startswith('|')]
    assert table_lines
    header_cells = [cell.strip() for cell in table_lines[0].strip('|').split('|')]
    assert 'Sum' in header_cells
    assert any(
        '92' in ln and '[bench: {crafted}16% increased Area Damage; +4x Exalted Orb]' in ln
        for ln in table_lines[2:]
    )
    payload = report_json(variants)
    assert payload[1]['weighted_sum'] == 92.0
    assert payload[1]['bench_craft']['recipe_id'] == 'area'


def test_item_inspect_and_bench_craft_poe2_guardrails_and_missing_engine(monkeypatch):
    from click.testing import CliRunner

    from pob_cli import cli as cli_mod
    from pob_cli import item_inspect

    runner = CliRunner()
    r_slot = runner.invoke(cli_mod.main, ['item', 'inspect', '--slot', 'charm1', '--raw', RAW])
    assert r_slot.exit_code == 1
    assert 'PoE2' in r_slot.output or 'poe2' in r_slot.output.lower()

    r_rune = runner.invoke(
        cli_mod.main,
        ['item', 'inspect', '--slot', 'Gloves', '--raw', RAW + '{rune}+20 to Spirit\n'],
    )
    assert r_rune.exit_code == 1
    assert 'PoE2' in r_rune.output or 'poe2' in r_rune.output.lower()

    r_want = runner.invoke(
        cli_mod.main,
        ['item', 'inspect', '--slot', 'Gloves', '--raw', RAW, '--want', 'Spirit'],
    )
    assert r_want.exit_code == 1
    assert 'PoE2' in r_want.output or 'poe2' in r_want.output.lower()

    r_trade = runner.invoke(
        cli_mod.main,
        ['trade', 'search', '--slot', 'gloves', '--bench-craft', 'Spirit', '--pob', 'on'],
    )
    assert r_trade.exit_code == 1
    assert 'PoE2' in r_trade.output or 'poe2' in r_trade.output.lower()

    monkeypatch.setattr(
        item_inspect,
        'inspect_item',
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError('PoE1 engine not bootstrapped')),
    )
    r_missing = runner.invoke(cli_mod.main, ['item', 'inspect', '--slot', 'Gloves', '--raw', RAW])
    assert r_missing.exit_code == 1
    assert 'Error:' in r_missing.output
    assert 'Traceback' not in r_missing.output

