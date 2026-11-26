# SPDX-License-Identifier: GPL-3.0-only
"""CLI inspection, S4/T3.4 deep audit, unique ranking, and 5-dim compare commands (L4)."""

from __future__ import annotations

import click

from .cli_common import (
    daemon_call_cli,
    emit_output,
    output_options,
    pob2_command,
    resolve_target_slot,
    target_option,
)
from .cli_item import run_unique_slot_ranking
from .formatters import (
    format_audit_md,
    format_compare_md,
    format_summary_md,
)
from .guardrails import normalize_poe2_equipment_slot


@pob2_command("summary")
@target_option(allow_slot_alias=True)
@output_options
@click.pass_context
def cmd_summary(ctx: click.Context, target: str, as_json: bool) -> None:
    """Show PoE2 build headline summary with PoB2 version & patch stamp."""
    res = daemon_call_cli("summary", {"target": resolve_target_slot(target)})
    emit_output(ctx, res, as_json=as_json, markdown_text=format_summary_md(res))


@pob2_command("audit")
@target_option(allow_slot_alias=True)
@click.option("--overcap", type=int, default=15, show_default=True, help="Recommended elemental overcap buffer (%) for Waystone curses/Exposure.")
@click.option("--chaos-floor", "chaos_floor", type=int, default=0, show_default=True, help="Target Chaos resistance floor (%).")
@output_options
@click.pass_context
def cmd_audit(ctx: click.Context, target: str, overcap: int, chaos_floor: int, as_json: bool) -> None:
    """Run S4/T3.4 PoE2 deep audit (conversion dead mods, ArmourRatio=10, Deflection illusion, CI lock, [PoB2未模拟] red cards, 4-dim dehydration)."""
    res = daemon_call_cli("audit", {"target": resolve_target_slot(target), "overcap": overcap, "chaos_floor": chaos_floor})
    emit_output(ctx, res, as_json=as_json, markdown_text=format_audit_md(res))


@pob2_command("diagnose")
@target_option(allow_slot_alias=True)
@click.option("--overcap", type=int, default=15, show_default=True, help="Recommended elemental overcap buffer (%).")
@click.option("--chaos-floor", "chaos_floor", type=int, default=0, show_default=True, help="Target Chaos resistance floor (%).")
@click.option("--node-power", "node_power", is_flag=True, default=False, help="Include top nearby passive node power rankings.")
@output_options
@click.pass_context
def cmd_diagnose(
    ctx: click.Context,
    target: str,
    overcap: int,
    chaos_floor: int,
    node_power: bool,
    as_json: bool,
) -> None:
    """One-shot PoE2 build diagnosis combining summary, S4/T3.4 deep audit, and author notes."""
    res = daemon_call_cli(
        "diagnose",
        {"target": resolve_target_slot(target), "overcap": overcap, "chaos_floor": chaos_floor, "node_power": node_power},
    )
    md = format_summary_md(res.get("summary") or {}) + "\n\n" + format_audit_md(res.get("audit") or {})
    emit_output(ctx, res, as_json=as_json, markdown_text=md)


@pob2_command("uniques")
@click.option("--slot", type=str, required=True, help="Target equipment slot (including Flask 1..2 and Charm 1..3).")
@click.option("--stat", "--sort", "sort_stat", type=str, default=None, help="Optional ranking stat/metric (default: primary DPS).")
@click.option("--top", "--limit", "limit", type=click.IntRange(min=1), default=15, show_default=True, help="Number of top uniques to return.")
@click.option("--no-sanctified", "no_sanctified", is_flag=True, default=False, help="Skip 0.78x-1.22x Sanctified/Vaal roll-range evaluation.")
@target_option(allow_slot_alias=False)
@output_options
@click.pass_context
def cmd_uniques(
    ctx: click.Context,
    slot: str,
    sort_stat: str | None,
    limit: int,
    no_sanctified: bool,
    target: str,
    as_json: bool,
) -> None:
    """Keyword-free full-slot PoE2 unique ranking (alias of `pob2 item db`; incl. Flask/Charm & Sanctified 0.78x-1.22x)."""
    run_unique_slot_ranking(
        ctx,
        slot=slot,
        sort_stat=sort_stat,
        limit=limit,
        no_sanctified=no_sanctified,
        target=target,
        as_json=as_json,
        command_label="uniques",
    )


@pob2_command("compare")
@click.argument("ref_source", required=False, default=None)
@click.option("--ref-target", "ref_target", type=str, default=None, help="Compare against another loaded slot (e.g. ref).")
@target_option(allow_slot_alias=True, help_text="Primary build slot (default: main).")
@output_options
@click.pass_context
def cmd_compare(
    ctx: click.Context,
    ref_source: str | None,
    ref_target: str | None,
    target: str,
    as_json: bool,
) -> None:
    """5-dimension Compare Power Report (Items / Tree / Main Skill / Supports / Config) against a reference build."""
    params = {"target": resolve_target_slot(target)}
    if ref_source:
        params["ref_source"] = ref_source
    elif ref_target:
        params["ref_target"] = ref_target
    else:
        params["ref_target"] = "ref"
    res = daemon_call_cli("compare", params)
    emit_output(ctx, res, as_json=as_json, markdown_text=format_compare_md(res))


@pob2_command("items")
@click.option("--slot", type=str, default=None, help="Optional filter by PoE2 equipment slot name.")
@target_option(allow_slot_alias=False)
@output_options
@click.pass_context
def cmd_items(ctx: click.Context, slot: str | None, target: str, as_json: bool) -> None:
    """List equipped PoE2 items and slots."""
    canon_slot = normalize_poe2_equipment_slot(slot, command="items") if (slot and slot.strip()) else None
    res = daemon_call_cli("items", {"target": resolve_target_slot(target), "slot": canon_slot})
    emit_output(ctx, res, as_json=as_json)


@pob2_command("skills")
@target_option(allow_slot_alias=True)
@output_options
@click.pass_context
def cmd_skills(ctx: click.Context, target: str, as_json: bool) -> None:
    """List PoE2 skill groups, gems, and weapon-set bindings."""
    res = daemon_call_cli("skills", {"target": resolve_target_slot(target)})
    emit_output(ctx, res, as_json=as_json)


@pob2_command("tree")
@target_option(allow_slot_alias=True)
@output_options
@click.pass_context
def cmd_tree(ctx: click.Context, target: str, as_json: bool) -> None:
    """Inspect allocated PoE2 passive tree nodes, weapon-set modes, and attribute overrides."""
    res = daemon_call_cli("tree", {"target": resolve_target_slot(target)})
    emit_output(ctx, res, as_json=as_json)


@pob2_command("config")
@target_option(allow_slot_alias=True)
@output_options
@click.pass_context
def cmd_config(ctx: click.Context, target: str, as_json: bool) -> None:
    """Inspect PoB2 Configuration options and active toggles."""
    res = daemon_call_cli("config_list", {"target": resolve_target_slot(target)})
    emit_output(ctx, res, as_json=as_json)


__all__ = [
    "cmd_audit",
    "cmd_compare",
    "cmd_config",
    "cmd_diagnose",
    "cmd_items",
    "cmd_skills",
    "cmd_summary",
    "cmd_tree",
    "cmd_uniques",
]
