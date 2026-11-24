"""Item mutation, inspection, flask, anoint, and PoE2 reverse-guardrail trap Click commands."""

from __future__ import annotations

import click

from .cli_common import (
    _emit_json,
    _emit_text,
    _output_options,
    _target_option,
    invoke_daemon,
)
from .formatters import format_mutation_report


@click.group("item")
def grp_item() -> None:
    """Inspect, evaluate, edit, or mutate equipped items, flasks, and anoints."""


@grp_item.command("inspect")
@click.option("--slot", help="Equipped slot, or the item type for pasted text (e.g. 'Ring 1').")
@click.option("--raw", "raw_text", help="Pasted PoE/PoB item text.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="File containing item text.")
@click.option("--want", "wants", multiple=True, help="Bench target, e.g. 'Cold Resistance' (repeatable).")
@click.option("--limit", type=click.IntRange(min=1, max=20), default=8, show_default=True)
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output structured JSON.")
@_output_options
def cmd_item_inspect(
    slot: str | None,
    raw_text: str | None,
    file_path: str | None,
    wants: tuple[str, ...],
    limit: int,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Classify item affixes and show compatible crafting-bench options (read-only)."""
    from pathlib import Path

    from .guardrails import GuardrailScope, check_input, check_input_item_raw, validate_url
    from .item_inspect import format_inspection, inspect_item

    if raw_text is not None and file_path is not None:
        raise click.UsageError("Use either --raw or --file, not both.")
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    for w_val in wants:
        validate_url(w_val, expected_game="poe1", cli_subcommand="item inspect")
        check_input(w_val, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    if file_path is not None:
        raw_text = Path(file_path).read_text(encoding="utf-8-sig", errors="replace")
    if raw_text is None:
        if not slot:
            raise click.UsageError("Pass --slot, --raw, or --file.")
        loaded = invoke_daemon("items", {"target": target, "slot": slot, "raw": True})
        raw_text = loaded.get("raw")
        if not raw_text:
            raise click.ClickException(f"Slot {slot!r} is empty or its item text is unavailable.")
        slot = (loaded.get("slot") or {}).get("name") or slot
    check_input_item_raw(raw_text, expected_game="poe1")
    try:
        report = inspect_item(raw_text, slot=slot, wants=wants, limit=limit)
    except (ValueError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit_json(report, compact=compact, out_path=out_path)
    else:
        _emit_text(format_inspection(report), out_path=out_path)


@grp_item.command("try")
@click.option("--slot", help="Target equipment slot (e.g. 'Weapon 1', 'Ring 1', '#1').")
@click.option("--raw", "raw_text", help="Raw PoE item text string.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="File containing raw PoE item text.")
@click.option("--tooltip-only", is_flag=True, help="Fast native tooltip preview only (skip snapshot secondary checks).")
@click.option("--apply", is_flag=True, help="Persist equipped item into build and push undo snapshot.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_try(
    slot: str | None,
    raw_text: str | None,
    file_path: str | None,
    tooltip_only: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Evaluate or equip a raw PoE item with native tooltip comparison + full snapshot diff."""
    from pathlib import Path

    from .formatters import strip_pob_escapes

    if file_path:
        raw_text = Path(file_path).read_text(encoding="utf-8-sig", errors="replace").lstrip("\ufeff")
    if not raw_text:
        raise click.UsageError('Provide either --raw "<item_text>" or --file <item.txt>.')

    from .guardrails import GuardrailScope, check_input, check_input_item_raw

    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input_item_raw(raw_text, expected_game="poe1")

    res = invoke_daemon(
        "item_try",
        {
            "target": target,
            "slot": slot,
            "raw": raw_text,
            "tooltip_only": tooltip_only,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    lines = [f"### Item Evaluation (Slot: `{res.get('slotName') or 'Auto'}`)"]
    slot_tips = res.get("slotTooltips") or []
    if not slot_tips and res.get("tooltip"):
        slot_tips = [res["tooltip"]]
    for st in slot_tips:
        st_slot = st.get("slotName") or res.get("slotName") or "Default"
        t_lines = st.get("lines") or []
        if t_lines:
            lines.append(f"\n#### Native PoB Tooltip (`{st_slot}`)")
            for ln in t_lines:
                txt = (
                    strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
                    if isinstance(ln, dict)
                    else strip_pob_escapes(str(ln))
                )
                if txt:
                    lines.append(f"  {txt}")

    if res.get("mutation"):
        lines.append("\n" + format_mutation_report("Snapshot Full Simulation", res["mutation"]))
    _emit_text("\n".join(lines), out_path=out_path)


@grp_item.command("edit")
@click.option("--slot", required=True, help="Equipped slot to edit (e.g. 'Ring 1', 'Weapon 1', '#1').")
@click.option(
    "--replace-mod",
    "replace_mods",
    nargs=2,
    multiple=True,
    metavar="<OLD> <NEW>",
    help="Replace matching mod line(s) with NEW text (repeatable).",
)
@click.option("--add-mod", "add_mods", multiple=True, help="Append an explicit mod line (repeatable).")
@click.option("--remove-mod", "remove_mods", multiple=True, help="Remove matching mod line(s) (repeatable).")
@click.option("--apply", is_flag=True, help="Persist edited item into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_edit(
    slot: str,
    replace_mods: tuple[tuple[str, str], ...],
    add_mods: tuple[str, ...],
    remove_mods: tuple[str, ...],
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """In-place replace, add, or remove mod lines on an equipped item (0-residue what-if by default)."""
    from .formatters import render_table

    if not replace_mods and not add_mods and not remove_mods:
        raise click.UsageError("Provide at least one of --replace-mod, --add-mod, or --remove-mod.")

    from .guardrails import GuardrailScope, check_input

    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    for _, new_line in replace_mods:
        check_input(new_line, scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    for new_mod in add_mods:
        check_input(new_mod, scope=GuardrailScope.ITEM_RAW, expected_game="poe1")

    res = invoke_daemon(
        "item_edit",
        {
            "target": target,
            "slot": slot,
            "replace_mods": [list(p) for p in replace_mods],
            "add_mods": list(add_mods),
            "remove_mods": list(remove_mods),
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    lines = [f"### Item Mod Edit (`{res.get('slotName')}`)"]
    changes = res.get("changes") or []
    if changes:
        c_rows = [
            [
                c.get("action"),
                c.get("section") or "-",
                c.get("before") or "-",
                c.get("after") or "-",
            ]
            for c in changes
        ]
        lines.append(render_table(["Action", "Section", "Before", "After"], c_rows))
    if res.get("mutation"):
        lines.append("\n" + format_mutation_report(f"Item Edit Impact ({res.get('slotName')})", res["mutation"]))
    _emit_text("\n".join(lines), out_path=out_path)


@grp_item.command("unequip")
@click.option("--slot", required=True, help="Equipment slot to unequip (e.g. 'Weapon 2', 'Ring 2', '#1').")
@click.option("--apply", is_flag=True, help="Persist unequip change into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_unequip(
    slot: str,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Unequip the item in an equipment or jewel slot (0-residue what-if unless --apply)."""
    from .guardrails import GuardrailScope, check_input

    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    res = invoke_daemon("item_unequip", {"target": target, "slot": slot, "apply": apply})
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(format_mutation_report(f"Unequip Slot `{slot}`", res), out_path=out_path)


@grp_item.command("db")
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
    from .formatters import render_table, strip_pob_escapes
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


@grp_item.command("flask")
@click.argument("index", type=str)
@click.argument("state", type=click.Choice(["on", "off"], case_sensitive=False))
@click.option("--apply", is_flag=True, help="Persist change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_flask(
    index: str,
    state: str,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Toggle Flask 1..5 or `all` on or off."""
    res = invoke_daemon(
        "item_flask",
        {
            "target": target,
            "flask_index": index,
            "active": state.lower() == "on",
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(format_mutation_report(f"Flask {index} -> {state.upper()}", res), out_path=out_path)


@grp_item.command("anoint")
@click.option("--slot", default="Amulet", show_default=True, help="Equipment slot to anoint.")
@click.option("--notable", "--oil", "notable", required=True, help="Notable passive name (Chinese or English).")
@click.option("--index", "anoint_idx", type=int, default=1, show_default=True, help="Anoint slot index (1..4).")
@click.option("--apply", is_flag=True, help="Persist anoint into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_item_anoint(
    slot: str,
    notable: str,
    anoint_idx: int,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Anoint an equipped item via PoB's native item_edit_popup workflow."""
    from .formatters import strip_pob_escapes
    from .guardrails import GuardrailScope, check_input

    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(notable, scope=GuardrailScope.NOTABLE_OR_OIL, expected_game="poe1")

    res = invoke_daemon(
        "item_anoint",
        {
            "target": target,
            "slot": slot,
            "notable": notable,
            "index": anoint_idx,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        notable_obj = res.get("detail", {}).get("notable", {})
        lbl = strip_pob_escapes(notable_obj.get("labelZh") or notable_obj.get("label") or notable)
        _emit_text(format_mutation_report(f"Anoint {slot} (#{anoint_idx}) -> {lbl}", res), out_path=out_path)


def _extract_trap_opt(raw_args: tuple[str, ...], names: tuple[str, ...]) -> str | None:
    args_list = list(raw_args)
    for idx, tok in enumerate(args_list):
        if tok in names and idx + 1 < len(args_list):
            return args_list[idx + 1]
        for nm in names:
            if tok.startswith(f"{nm}="):
                return tok.split("=", 1)[1]
    return None


@grp_item.command(
    "charm",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    short_help="[TRAP -> pob item flask] PoE1 uses Flask 1..5, not PoE2 belt Charms.",
)
@click.argument("raw_args", nargs=-1, type=click.UNPROCESSED)
def cmd_item_charm_trap(raw_args: tuple[str, ...]) -> None:
    """Intentional reverse guardrail trap for PoE2 `item charm` invocations in `pob`."""
    from .errors import CrossGameHallucinationError

    pos = [a for a in raw_args if not a.startswith("-")]
    idx_tok = pos[0] if pos and pos[0] in ("1", "2", "3", "4", "5", "all") else "1"
    state_tok = pos[1].lower() if len(pos) >= 2 and pos[1].lower() in ("on", "off") else "on"
    raise CrossGameHallucinationError(
        game="poe1",
        detected_game="poe2",
        trigger=" ".join(["pob item charm", *raw_args]).strip(),
        error="Belt `Charm 1..3` slots (`护符`/`咒符`) are a PoE2 mechanic; Path of Exile 1 does not have belt Charm slots.",
        remediation=(
            "In PoE1, toggle `Flask 1..5` via `pob item flask <1..5|all> <on|off>`, "
            "or run `pob2 item charm` for PoE2 builds."
        ),
        equivalent_command=f"pob item flask {idx_tok} {state_tok}",
        rule_id="CLI_TRAP_ITEM_CHARM_IN_POB1",
    )


@grp_item.command(
    "instill",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    short_help="[TRAP -> pob item anoint] PoE1 uses Blight Oils (`anoint`), not PoE2 `instill`.",
)
@click.argument("raw_args", nargs=-1, type=click.UNPROCESSED)
def cmd_item_instill_trap(raw_args: tuple[str, ...]) -> None:
    """Intentional reverse guardrail trap for PoE2 `item instill` invocations in `pob`."""
    from .errors import CrossGameHallucinationError

    slot_val = _extract_trap_opt(raw_args, ("--slot",))
    notable_val = _extract_trap_opt(raw_args, ("--notable",))
    slot_part = f"--slot {slot_val} " if slot_val else ""
    notable_part = f'--notable "{notable_val}"' if notable_val else '--notable "<name>"'
    raise CrossGameHallucinationError(
        game="poe1",
        detected_game="poe2",
        trigger=" ".join(["pob item instill", *raw_args]).strip(),
        error="Amulet Instilling (`涂灌` with Delirium Distilled/Liquid Emotions) is a PoE2 mechanic.",
        remediation=(
            "In PoE1, anoint amulets/rings with Blight Oils (`圣油`/`涂油`) via `pob item anoint`, "
            "or run `pob2 item instill` for PoE2 builds."
        ),
        equivalent_command=f"pob item anoint {slot_part}{notable_part}".strip(),
        rule_id="CLI_TRAP_ITEM_INSTILL_IN_POB1",
    )


@click.command(
    "rune",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    short_help="[TRAP -> pob bench] PoE1 uses Hideout Crafting Bench, not PoE2 equipment Runes.",
)
@click.argument("raw_args", nargs=-1, type=click.UNPROCESSED)
def cmd_rune_trap(raw_args: tuple[str, ...]) -> None:
    """Intentional reverse guardrail trap for PoE2 `rune` invocations in `pob`."""
    from .errors import CrossGameHallucinationError

    slot_val = _extract_trap_opt(raw_args, ("--slot",)) or "helmet"
    stat_val = _extract_trap_opt(raw_args, ("--stat", "--search"))
    if stat_val:
        equiv = f'pob bench suggest --slot {slot_val} --stat "{stat_val}"'
    else:
        equiv = f"pob bench list --slot {slot_val}"
    raise CrossGameHallucinationError(
        game="poe1",
        detected_game="poe2",
        trigger=" ".join(["pob rune", *raw_args]).strip(),
        error="Path of Exile 1 does not have PoE2 equipment Rune/Soul Core sockets (`pob2 rune` / `ModRunes.lua`).",
        remediation=(
            "In PoE1, gear crafting uses the Hideout Crafting Bench (`{crafted}` via `pob bench list` / `pob bench suggest`), "
            "or run `pob2 rune` for PoE2."
        ),
        equivalent_command=equiv,
        rule_id="CLI_TRAP_RUNE_IN_POB1",
    )


@click.command(
    "instill",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    short_help="[TRAP -> pob item anoint] PoE1 uses Blight Oils (`pob item anoint`), not PoE2 `instill`.",
)
@click.argument("raw_args", nargs=-1, type=click.UNPROCESSED)
def cmd_instill_trap(raw_args: tuple[str, ...]) -> None:
    """Intentional reverse guardrail trap for PoE2 `instill` invocations in `pob`."""
    from .errors import CrossGameHallucinationError

    slot_val = _extract_trap_opt(raw_args, ("--slot",))
    notable_val = _extract_trap_opt(raw_args, ("--notable",))
    slot_part = f"--slot {slot_val} " if slot_val else ""
    notable_part = f'--notable "{notable_val}"' if notable_val else '--notable "<name>"'
    raise CrossGameHallucinationError(
        game="poe1",
        detected_game="poe2",
        trigger=" ".join(["pob instill", *raw_args]).strip(),
        error="Amulet Instilling (`涂灌` with Delirium Distilled/Liquid Emotions) is a PoE2 mechanic.",
        remediation=(
            "In PoE1, anoint items with Blight Oils (`圣油`/`涂油`) via `pob item anoint`, "
            "or run `pob2 item instill` for PoE2."
        ),
        equivalent_command=f"pob item anoint {slot_part}{notable_part}".strip(),
        rule_id="CLI_TRAP_INSTILL_IN_POB1",
    )


def register_item_commands(group: click.Group) -> None:
    group.add_command(grp_item)
    group.add_command(cmd_rune_trap)
    group.add_command(cmd_instill_trap)
