"""A/B build comparison (`compare`) and cross-build transplant (`borrow`) Click commands."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    _SLOT_SLUG_TYPE,
    _emit_json,
    _emit_text,
    _output_options,
    _prepare_load_source_with_stage,
    _ref_stage_options,
    _target_option,
    invoke_daemon,
)
from .formatters import format_mutation_report, keep_rejected_warning, restore_warning


@click.command("compare")
@click.argument("ref_source", required=False)
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed config/flask/skill/customMods/party items during dewatering (repeatable or comma-separated).",
)
@click.option(
    "--no-auto-keep",
    "no_auto_keep",
    is_flag=True,
    help="Disable automatic mechanism-based config retention during Pinnacle dewatering.",
)
@_target_option(allow_slot_alias=True)
@click.option(
    "--ref-target",
    "ref_target",
    type=_SLOT_SLUG_TYPE,
    default="ref",
    show_default=True,
    help="Reference build slot slug (default: 'ref').",
)
@_ref_stage_options
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_compare(
    ref_source: str | None,
    keep: tuple[str, ...],
    no_auto_keep: bool,
    target: str,
    ref_target: str,
    ref_stage: str | None,
    ref_spec: str | None,
    ref_item_set: str | None,
    ref_skill_set: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Compare target build vs reference build across 3 baselines + tree/skill/equipment diffs."""
    from .formatters import format_compare_markdown

    params: dict[str, Any] = {"target": target, "ref_target": ref_target}
    if keep:
        params["keep"] = list(keep)
    if no_auto_keep:
        params["no_auto_keep"] = True
    ref_frag: dict[str, str | None] = {}
    if ref_source:
        resolved_src, resolved_xml_code, ref_frag = _prepare_load_source_with_stage(ref_source)
        if resolved_xml_code is not None:
            params["ref_xml_code"] = resolved_xml_code
        else:
            params["ref_source"] = resolved_src
    for pkey, fkey, cli_val in (
        ("ref_stage", "stage", ref_stage),
        ("ref_spec", "spec", ref_spec),
        ("ref_item_set", "item_set", ref_item_set),
        ("ref_skill_set", "skill_set", ref_skill_set),
    ):
        eff = cli_val if (cli_val is not None and cli_val.strip()) else ref_frag.get(fkey)
        if eff and eff.strip():
            params[pkey] = eff.strip()

    res = invoke_daemon("compare", params)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    _emit_text(format_compare_markdown(res, target=target, ref_target=ref_target), out_path=out_path)


@click.command("borrow")
@click.option(
    "--item-slot",
    "item_slot",
    help="Comma-separated equipment slot(s) to borrow from reference into target (e.g. 'Weapon 1,Amulet,Body,node:33631').",
)
@click.option("--skill-group", "skill_group", help="Skill group ('main', index, or label) to borrow from reference into target.")
@click.option("--tree", "borrow_tree", is_flag=True, help="Transplant the reference build's passive tree (and allocated socket jewels).")
@click.option(
    "--no-tree-jewels",
    "no_tree_jewels",
    is_flag=True,
    help="With --tree, transplant passive nodes/masteries without auto-borrowing allocated socket jewels.",
)
@click.option(
    "--scenario",
    "scenario",
    type=str,
    default=None,
    help="Measure what-if deltas under a temporary combat baseline (e.g. Pinnacle, Uber); cannot combine with --apply.",
)
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed items when --scenario applies Pinnacle/Uber dewatering.",
)
@click.option("--apply", is_flag=True, help="Persist borrowed items/skills/tree into target build.")
@_target_option(allow_slot_alias=True)
@click.option(
    "--ref-target",
    "ref_target",
    type=_SLOT_SLUG_TYPE,
    default="ref",
    show_default=True,
    help="Reference build slot slug (default: 'ref').",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_borrow(
    item_slot: str | None,
    skill_group: str | None,
    borrow_tree: bool,
    no_tree_jewels: bool,
    scenario: str | None,
    keep: tuple[str, ...],
    apply: bool,
    target: str,
    ref_target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Borrow items, a skill group, and/or passive tree from reference slot into target slot."""
    has_scenario = bool(scenario and scenario.strip())
    if has_scenario and apply:
        raise click.ClickException(
            "--scenario measures what-if deltas under a temporary combat baseline and cannot be combined "
            "with --apply; omit --scenario when persisting changes."
        )
    if keep and not has_scenario:
        raise click.ClickException(
            "--keep on `pob borrow` requires `--scenario <name>` (e.g. `--scenario Pinnacle`)."
        )
    slots = [x.strip() for x in item_slot.split(",") if x.strip()] if item_slot else []
    params: dict[str, Any] = {
        "target": target,
        "ref_target": ref_target,
        "item_slots": slots,
        "skill_group": skill_group,
        "apply": apply,
    }
    if borrow_tree:
        params["borrow_tree"] = True
    if no_tree_jewels:
        params["borrow_tree_jewels"] = False
    if has_scenario and scenario is not None:
        params["scenario"] = scenario.strip()
    if keep:
        params["keep"] = list(keep)
    res = invoke_daemon("borrow", params)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    notices = (res.get("detail") or {}).get("notices") or []
    report = format_mutation_report(f"Borrow from Reference Build (`{ref_target}` -> `{target}`)", res)
    r_warn = restore_warning(res)
    if r_warn:
        report = f"{r_warn}\n{report}"
    stripped = (res.get("scenarioApplied") or {}).get("stripped") or {}
    rej_lines = keep_rejected_warning(stripped) if isinstance(stripped, dict) else []
    if rej_lines:
        report += "\n" + "\n".join(rej_lines)
    if notices:
        notice_block = "\n#### Transplant Notices\n" + "\n".join(f"- {n}" for n in notices)
        report += notice_block
    _emit_text(report, out_path=out_path)
