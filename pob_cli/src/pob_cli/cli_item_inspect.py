"""`pob item inspect` Click command (affix occupancy, bench suggestions, and `--ablate` CoW analysis)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from .cli_common import (
    _emit_json,
    _emit_text,
    _output_options,
    _target_option,
    invoke_daemon,
)


@click.command("inspect")
@click.argument("slot_arg", metavar="[SLOT]", required=False, default=None)
@click.option("--slot", "slot_opt", help="Equipped slot, or the item type for pasted text (e.g. 'Ring 1').")
@click.option("--raw", "raw_text", help="Pasted PoE/PoB item text.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="File containing item text.")
@click.option("--want", "wants", multiple=True, help="Bench target, e.g. 'Cold Resistance' (repeatable).")
@click.option("--limit", type=click.IntRange(min=1), default=8, show_default=True)
@click.option(
    "--ablate",
    is_flag=True,
    help="Single-mod CoW ablation: toggle each mod line off via PoB to measure marginal DPS/EHP/res/attr deltas (0-residue).",
)
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output structured JSON.")
@_output_options
def cmd_item_inspect(
    slot_arg: str | None,
    slot_opt: str | None,
    raw_text: str | None,
    file_path: str | None,
    wants: tuple[str, ...],
    limit: int,
    ablate: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Classify item affixes, show bench options, or run `--ablate` single-mod CoW ablation (read-only)."""
    from .formatters.item_ablate_md import format_item_ablation_markdown
    from .guardrails import GuardrailScope, check_input, check_input_item_raw, validate_url
    from .item_inspect import format_inspection, inspect_item

    if slot_arg and slot_opt and slot_arg.strip().lower() != slot_opt.strip().lower():
        raise click.UsageError("Specify slot either positionally or via --slot, not both.")
    slot = slot_opt or slot_arg

    if raw_text is not None and file_path is not None:
        raise click.UsageError("Use either --raw or --file, not both.")
    if ablate and (raw_text is not None or file_path is not None):
        raise click.UsageError("--ablate operates on an equipped slot; omit --raw and --file.")
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    for w_val in wants:
        validate_url(w_val, expected_game="poe1", cli_subcommand="item inspect")
        check_input(w_val, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")

    ablation_res: dict[str, Any] | None = None
    if ablate:
        if not slot:
            raise click.UsageError("--ablate requires an equipped slot (e.g. `pob item inspect 'Ring 1' --ablate`).")
        ablation_res = invoke_daemon("item_ablate", {"target": target, "slot": slot}, timeout=90.0)
        raw_text = str(ablation_res.get("raw") or "") or None
        slot = str(ablation_res.get("slotName") or slot)

    if file_path is not None:
        raw_text = Path(file_path).read_text(encoding="utf-8-sig", errors="replace")
    if raw_text is None:
        if not slot:
            raise click.UsageError("Pass SLOT / --slot, --raw, or --file.")
        loaded = invoke_daemon("items", {"target": target, "slot": slot, "raw": True})
        raw_text = loaded.get("raw")
        if not raw_text:
            raise click.ClickException(f"Slot {slot!r} is empty or its item text is unavailable.")
        slot = (loaded.get("slot") or {}).get("name") or slot

    check_input_item_raw(raw_text, expected_game="poe1")
    report: dict[str, Any]
    has_inspect = True
    try:
        report = inspect_item(raw_text, slot=slot, wants=wants, limit=limit)
    except (ValueError, FileNotFoundError) as exc:
        if ablation_res is None:
            raise click.ClickException(str(exc)) from exc
        report = {"item": {"name": ablation_res.get("itemName"), "slot": slot}}
        has_inspect = False

    if ablation_res is not None:
        report["ablation"] = ablation_res

    if as_json:
        _emit_json(report, compact=compact, out_path=out_path)
        return

    parts: list[str] = []
    if has_inspect:
        parts.append(format_inspection(report))
    if ablation_res is not None:
        parts.append(format_item_ablation_markdown(ablation_res))
    _emit_text("\n\n".join(p for p in parts if p), out_path=out_path)
