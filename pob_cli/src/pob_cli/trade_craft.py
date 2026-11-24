"""Build conservative, independently verified bench variants of trade listings."""

from dataclasses import replace

from . import bench, item_inspect
from .trade_eval import Candidate
from .trade_item import parse_item_text


def expand_bench_candidates(candidates: list[Candidate], *, slot: str,
                            wants: tuple[str, ...]) -> list[Candidate]:
    """Keep each listed item and add legal maximum-roll single-craft variants.

    Uncertain occupancy, special scaled rolls and multi-craft items are excluded.
    Listing price is preserved; crafting cost is reported separately by currency.
    """
    expanded: list[Candidate] = []
    for candidate in candidates:
        expanded.append(candidate)
        report = item_inspect.inspect_item(candidate.item_text, slot=slot, wants=wants, limit=20)
        if not report['certain'] or not report['craftEligible'] or not report['item']['itemLevel']:
            continue
        bench_slot = report.get('benchSlot', slot)
        if not bench_slot:
            continue
        crafted = [a for a in report['affixes'] if a['source'] == 'Bench']
        if len(crafted) > 1 or any('multimod' in str(a['group']).lower() for a in crafted):
            continue
        removed = [line for affix in crafted for line in affix['lines']]
        base_lines = [line for line in candidate.item_text.splitlines()
                      if not line.startswith('{crafted}')]
        seen: set[str] = set()
        for suggestion in report['suggestions']:
            if suggestion['status'] not in ('open', 'replace_craft'):
                continue
            if suggestion['affix'] in report['scaledAffixTypes']:
                continue
            recipe = next((r.recipe for r in bench.search(bench_slot, suggestion['want'], limit=15,
                           allow_engine=True) if r.recipe.id == suggestion['id']), None)
            if recipe is None:
                continue
            lines = recipe.crafted_lines(use_min=False)
            if sorted(line.removeprefix('{crafted}') for line in lines) == sorted(removed):
                continue
            raw = '\n'.join(base_lines + lines)
            if raw in seen:
                continue
            seen.add(raw)
            expanded.append(replace(candidate, item=parse_item_text(raw), item_text=raw,
                pob=None, pob_error=None, bench_craft={
                    'recipe_id': recipe.id, 'action': suggestion['status'],
                    'lines': lines, 'removed_lines': removed, 'cost': recipe.cost,
                    'roll': 'maximum', 'listed_item_text': candidate.item_text,
                    'cost_note': 'Crafting cost is additional to listing price; replacement also requires craft removal. Maximum rolls may require repeated crafts; reroll costs are not included.',
                }))
    return expanded
