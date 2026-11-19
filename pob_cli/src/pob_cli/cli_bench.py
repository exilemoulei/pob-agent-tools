"""`pob bench` — offline crafting-bench modifier lookup."""

from __future__ import annotations

import sys

import click

from . import bench
from .formatters import render_table, to_json_str


def _echo(text: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text)


@click.group("bench")
def bench_group() -> None:
    """Look up crafting-bench modifiers for a slot (offline poedb snapshot)."""
    pass


@bench_group.command("list")
@click.option("--slot", required=True, help="Slot, e.g. gloves, helmet, ring1, weapon1.")
@click.option("--search", "text", help="Filter by modifier text, e.g. 'cold resistance'.")
@click.option("--affix", type=click.Choice(["prefix", "suffix", "unknown"]), help="Only this affix type.")
@click.option("--limit", default=25, show_default=True, help="Maximum rows.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_list(slot: str, text: str | None, affix: str | None, limit: int, as_json: bool) -> None:
    """List bench modifiers available for a slot."""
    if text:
        results = bench.search(slot, text, limit=limit)
        items = [result.recipe for result in results]
    else:
        items = bench.list_for_slot(slot, affix=affix)[: max(0, limit)]

    if as_json:
        payload = {
            "slot": slot,
            "count": len(items),
            "source": bench.source_metadata(),
            "recipes": [recipe.as_dict() for recipe in items],
        }
        _echo(to_json_str(payload, sanitize=False))
        return

    if not items:
        _echo(f"No bench modifiers matched for `{slot}`.")
        return

    rows = []
    for recipe in items:
        rows.append(
            [
                recipe.affix,
                recipe.text,
                recipe.text_zh,
                recipe.roll_text,
                recipe.cost,
                recipe.unlock,
            ]
        )
    _echo(
        render_table(
            ["Affix", "Modifier", "中文", "Roll", "Cost", "Unlock"],
            rows,
        )
    )


@bench_group.command("suggest")
@click.option("--slot", required=True, help="Slot, e.g. gloves, helmet, ring1, weapon1.")
@click.option("--stat", "text", required=True, help="Desired modifier, e.g. 'to Cold Resistance'.")
@click.option("--limit", default=5, show_default=True, help="Alternative rows.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_suggest(slot: str, text: str, limit: int, as_json: bool) -> None:
    """Suggest the best bench craft for a desired modifier."""
    ctx = click.get_current_context()
    results = bench.search(slot, text, limit=limit)
    if not results:
        _echo(f"No bench modifier for `{slot}` matched `{text}`.")
        ctx.exit(1)
        return

    best = results[0].recipe
    if as_json:
        payload = {
            "slot": slot,
            "query": text,
            "best": best.as_dict(),
            "alternatives": [result.recipe.as_dict() for result in results[1:]],
            "source": bench.source_metadata(),
        }
        _echo(to_json_str(payload, sanitize=False))
        return

    _echo(f"Best bench craft for `{slot}` / `{text}`:")
    _echo(f"- affix: {best.affix}")
    _echo(f"- modifier: {best.text} ({best.text_zh})")
    _echo(f"- roll: {best.roll_text} (minimum {best.min_value:g} is the safe floor)")
    _echo(f"- cost: {best.cost}")
    _echo(f"- unlock: {best.unlock}")
    _echo(
        "- note: the bench mod is lower tier than a natural roll; the item must "
        "have an open " + ("prefix" if best.affix == "prefix" else "suffix") + "."
    )
    if len(results) > 1:
        _echo("")
        _echo("Alternatives:")
        for result in results[1:]:
            recipe = result.recipe
            _echo(f"- [{recipe.affix}] {recipe.text} ({recipe.roll_text}; {recipe.cost})")
