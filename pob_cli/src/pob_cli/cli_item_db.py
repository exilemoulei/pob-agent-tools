"""`pob item db` Click command (Unique/Rare DB search and what-if equip)."""

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
@click.argument("query")
@click.option(
    "--type",
    "db_type",
    type=click.Choice(["UNIQUE", "RARE"], case_sensitive=False),
    default="UNIQUE",
    show_default=True,
    help="PoB item database kind.",
)
@click.option("--slot", help="Target equipment slot when equipping/comparing.")
@click.option("--index", "item_index", type=int, help="1-based match index to select when equipping.")
@click.option("--equip", is_flag=True, help="Try equipping selected DB item (what-if unless --apply).")
@click.option("--apply", is_flag=True, help="Persist equipped DB item into build (implies --equip).")
@click.option("--limit", type=int, default=15, show_default=True, help="Maximum matches to return.")
@click.option("--with-tooltips", "with_tooltips", is_flag=True, help="Fetch native PoB tooltips for search matches (slower).")
@click.option("--with-raw", "with_raw", is_flag=True, help="Include raw PoE item text in search listing.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_db(
    query: str,
    db_type: str,
    slot: str | None,
    item_index: int | None,
    equip: bool,
    apply: bool,
    limit: int,
    with_tooltips: bool,
    with_raw: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Search PoB's built-in Unique/Rare item database (EN/ZH) and optionally try/equip a match."""
    from .formatters import format_mutation_report, render_table, strip_pob_escapes
    from .guardrails import GuardrailScope, check_input

    check_input(query, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")

    res = invoke_daemon(
        "item_db",
        {
            "target": target,
            "query": query,
            "db_type": db_type.upper(),
            "slot": slot,
            "index": item_index,
            "equip": bool(equip or apply),
            "apply": apply,
            "limit": limit,
            "with_tooltips": with_tooltips,
            "with_raw": with_raw,
        },
        timeout=60.0,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    if not res.get("equipped"):
        rows = []
        for m in res.get("matches", []):
            title = strip_pob_escapes(m.get("titleZh") or m.get("title") or m.get("nameZh") or m.get("name") or "-")
            base = strip_pob_escapes(m.get("baseNameZh") or m.get("baseName") or m.get("type") or "-")
            rows.append([m.get("index"), title, base, m.get("primarySlot") or "-", m.get("league") or "-"])
        body = (
            f"### PoB Item DB Search (`{res.get('dbType')}`): `{query}` ({res.get('total', 0)} hits)\n"
            + render_table(["#", "Title", "Base", "Primary Slot", "League"], rows)
        )
        _emit_text(body, out_path=out_path)
        return

    sel = res.get("selectedItem") or {}
    sel_name = strip_pob_escapes(sel.get("titleZh") or sel.get("title") or sel.get("name") or query)
    lines = [f"### Item DB Equip: {sel_name} (Slot: `{res.get('slotName') or 'Auto'}`)"]
    tip = res.get("tooltip") or {}
    t_lines = tip.get("lines") or []
    if t_lines:
        lines.append("\n#### Native PoB Tooltip")
        for ln in t_lines:
            txt = (
                strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
                if isinstance(ln, dict)
                else strip_pob_escapes(str(ln))
            )
            if txt:
                lines.append(f"  {txt}")
    if res.get("mutation"):
        lines.append("\n" + format_mutation_report(f"Item DB Simulation ({sel_name})", res["mutation"]))
    _emit_text("\n".join(lines), out_path=out_path)
