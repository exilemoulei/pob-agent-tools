"""`pob item db` Click command (Unique/Rare DB search, slot/type filters, and PoB stat sorting)."""

from __future__ import annotations

import click

from .cli_common import (
    _emit_json,
    _emit_text,
    _output_options,
    _target_option,
    invoke_daemon,
)


@click.command("db")
@click.argument("query", metavar="[QUERY]", required=False, default="")
@click.option(
    "--type",
    "db_type",
    type=click.Choice(["UNIQUE", "RARE"], case_sensitive=False),
    default="UNIQUE",
    show_default=True,
    help="PoB item database kind.",
)
@click.option("--slot", help="Filter DB by slot (e.g. 'Helmet', 'Body Armour', 'Ring 2', '头盔') and target slot when equipping.")
@click.option(
    "--sort",
    "sort_mode",
    help="Sort Unique DB by PoB engine metric (e.g. dps, combined, hit, dot, ehp, maxhit, life, es, minion, or exact sortMode).",
)
@click.option("--item-type", "item_type", help="Filter DB by PoB item type (e.g. 'Bow', 'Armour', 'One Handed Melee').")
@click.option("--requirement", help="Filter Unique DB by requirement (any, level, attributes, useable).")
@click.option(
    "--obtainable",
    help="Filter Unique DB by source (default: obtainable; use 'any' for retired/legacy, or boss, corruption, vendor, core).",
)
@click.option("--search-mode", "search_mode", help="Match QUERY in anywhere (default), names, or modifiers.")
@click.option("--league", help="Filter Unique DB by league (e.g. 'any', 'No league', or league name).")
@click.option("--list-sorts", "list_sorts", is_flag=True, help="List all supported PoB ItemDB sort modes and filter options.")
@click.option("--index", "item_index", type=int, help="1-based match index to select when equipping.")
@click.option("--equip", is_flag=True, help="Try equipping selected DB item (what-if unless --apply).")
@click.option("--apply", is_flag=True, help="Persist equipped DB item into build (implies --equip).")
@click.option("--limit", type=int, default=15, show_default=True, help="Maximum matches to return.")
@click.option("--page", type=int, default=1, show_default=True, help="1-based results page.")
@click.option("--with-tooltips", "with_tooltips", is_flag=True, help="Fetch native PoB tooltips for search matches (slower).")
@click.option("--with-raw", "with_raw", is_flag=True, help="Include raw PoE item text in search listing.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_db(
    query: str,
    db_type: str,
    slot: str | None,
    sort_mode: str | None,
    item_type: str | None,
    requirement: str | None,
    obtainable: str | None,
    search_mode: str | None,
    league: str | None,
    list_sorts: bool,
    item_index: int | None,
    equip: bool,
    apply: bool,
    limit: int,
    page: int,
    with_tooltips: bool,
    with_raw: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Search, filter, stat-rank, or try/equip items from PoB's built-in Unique/Rare database."""
    from .formatters.item_db_md import format_item_db_markdown
    from .guardrails import GuardrailScope, check_input

    q = (query or "").strip()
    if q:
        check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    if item_type:
        check_input(item_type, scope=GuardrailScope.CATEGORY, expected_game="poe1")

    if not list_sorts and not q and not slot and not item_type and not league and not sort_mode:
        raise click.UsageError(
            "Provide QUERY, --slot, --item-type, --league, or --list-sorts "
            '(e.g. `pob item db "Mageblood"` or `pob item db --slot Helmet --sort dps`).'
        )

    payload = {
        "target": target,
        "query": q,
        "db_type": db_type.upper(),
        "slot": slot,
        "sort": sort_mode,
        "item_type": item_type,
        "requirement": requirement,
        "obtainable": obtainable,
        "search_mode": search_mode,
        "league": league,
        "list_sorts": list_sorts,
        "index": item_index,
        "equip": bool(equip or apply),
        "apply": apply,
        "limit": limit,
        "page": page,
        "with_tooltips": with_tooltips,
        "with_raw": with_raw,
    }
    res = invoke_daemon("item_db", payload, timeout=120.0 if sort_mode else 60.0)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_item_db_markdown(res), out_path=out_path)
