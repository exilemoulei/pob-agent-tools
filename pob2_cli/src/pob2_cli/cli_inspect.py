# SPDX-License-Identifier: GPL-3.0-only
"""CLI inspection, S4/T3.4 deep audit, unique ranking, and 5-dim compare commands (L4)."""

from __future__ import annotations

import click

from .cli_common import daemon_call_cli, emit_output, resolve_target_slot
from .formatters import (
    format_audit_md,
    format_compare_md,
    format_summary_md,
    format_uniques_md,
)
from .guardrails import normalize_poe2_equipment_slot


@click.command("summary")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_summary(ctx: click.Context, target: str | None, as_json: bool) -> None:
    """Show PoE2 build headline summary with PoB2 version & patch stamp."""
    res = daemon_call_cli("summary", {"target": resolve_target_slot(target)})
    emit_output(ctx, res, as_json=as_json, markdown_text=format_summary_md(res))


@click.command("audit")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--overcap", type=int, default=15, show_default=True, help="Recommended elemental overcap buffer (%) for Waystone curses/Exposure.")
@click.option("--chaos-floor", "chaos_floor", type=int, default=0, show_default=True, help="Target Chaos resistance floor (%).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_audit(ctx: click.Context, target: str | None, overcap: int, chaos_floor: int, as_json: bool) -> None:
    """Run S4/T3.4 PoE2 deep audit (conversion dead mods, ArmourRatio=10, Deflection illusion, CI lock, [PoB2未模拟] red cards, 4-dim dehydration)."""
    res = daemon_call_cli("audit", {"target": resolve_target_slot(target), "overcap": overcap, "chaos_floor": chaos_floor})
    emit_output(ctx, res, as_json=as_json, markdown_text=format_audit_md(res))


@click.command("diagnose")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--overcap", type=int, default=15, show_default=True, help="Recommended elemental overcap buffer (%).")
@click.option("--chaos-floor", "chaos_floor", type=int, default=0, show_default=True, help="Target Chaos resistance floor (%).")
@click.option("--node-power", "node_power", is_flag=True, default=False, help="Include top nearby passive node power rankings.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_diagnose(
    ctx: click.Context,
    target: str | None,
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


@click.command("uniques")
@click.option("--slot", type=str, required=True, help="Target equipment slot (including Flask 1..2 and Charm 1..3).")
@click.option("--stat", type=str, default=None, help="Optional ranking stat (default: primary DPS).")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top uniques to return.")
@click.option("--no-sanctified", "no_sanctified", is_flag=True, default=False, help="Skip 0.78x-1.22x Sanctified/Vaal roll-range evaluation.")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_uniques(
    ctx: click.Context,
    slot: str,
    stat: str | None,
    top: int,
    no_sanctified: bool,
    target: str | None,
    as_json: bool,
) -> None:
    """Keyword-free full-slot PoE2 unique ranking (including Flask/Charm) with 0.78x-1.22x Sanctified roll range."""
    canon_slot = normalize_poe2_equipment_slot(slot, command="uniques")
    res = daemon_call_cli(
        "uniques",
        {
            "target": resolve_target_slot(target),
            "slot": canon_slot,
            "stat": stat,
            "top": top,
            "eval_sanctified": not no_sanctified,
        },
    )
    emit_output(ctx, res, as_json=as_json, markdown_text=format_uniques_md(res))


@click.command("compare")
@click.argument("ref_source", required=False, default=None)
@click.option("--ref-target", "ref_target", type=str, default=None, help="Compare against another loaded slot (e.g. ref).")
@click.option("--target", type=str, default=None, help="Primary build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_compare(
    ctx: click.Context,
    ref_source: str | None,
    ref_target: str | None,
    target: str | None,
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


@click.command("items")
@click.option("--slot", type=str, default=None, help="Optional filter by slot name.")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_items(ctx: click.Context, slot: str | None, target: str | None, as_json: bool) -> None:
    """List equipped PoE2 items and slots."""
    canon_slot = normalize_poe2_equipment_slot(slot, command="items") if (slot and slot.strip()) else None
    res = daemon_call_cli("items", {"target": resolve_target_slot(target), "slot": canon_slot})
    emit_output(ctx, res, as_json=as_json)


@click.command("skills")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_skills(ctx: click.Context, target: str | None, as_json: bool) -> None:
    """List PoE2 skill groups, gems, and weapon-set bindings."""
    res = daemon_call_cli("skills", {"target": resolve_target_slot(target)})
    emit_output(ctx, res, as_json=as_json)


@click.command("tree")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_tree(ctx: click.Context, target: str | None, as_json: bool) -> None:
    """Inspect allocated PoE2 passive tree nodes, weapon-set modes, and attribute overrides."""
    res = daemon_call_cli("tree", {"target": resolve_target_slot(target)})
    emit_output(ctx, res, as_json=as_json)


@click.command("config")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_config(ctx: click.Context, target: str | None, as_json: bool) -> None:
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
