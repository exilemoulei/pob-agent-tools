# SPDX-License-Identifier: GPL-3.0-only
"""CLI CoW what-if & build diff commands (L4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from .cli_common import daemon_call_cli, emit_output, resolve_target_slot
from .formatters import format_what_if_md


def _parse_config_value(raw: str | None) -> Any:
    if raw is None:
        return None
    low = raw.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _format_what_if_with_details_md(payload: dict[str, Any]) -> str:
    base_md = format_what_if_md(payload)
    extra: list[str] = []
    for op in payload.get("detail") or []:
        if not isinstance(op, dict):
            continue
        if op.get("op") == "tree_click":
            nid = op.get("nodeId")
            nm = op.get("nodeName") or str(nid)
            pb = op.get("pointsUsedBefore")
            pa = op.get("pointsUsedAfter")
            nd = op.get("nodesDeltaCount")
            pnodes = op.get("allocatedPathNodes") or []
            mode_tag = " [anoint/isolated preview]" if op.get("isolatedMode") else ""
            path_part = f" | path travel nodes: {len(pnodes)} ({pnodes[:8]}{'...' if len(pnodes) > 8 else ''})" if pnodes else ""
            extra.append(f"- Tree Node `{nid}` ({nm}){mode_tag}: points {pb} -> {pa} (delta {nd:+d}){path_part}")
            if op.get("connectivityWarning"):
                extra.append(f"- Warning: {op['connectivityWarning']}")
            iso = op.get("isolatedNodeEffect")
            if isinstance(iso, dict) and isinstance(iso.get("delta"), dict):
                d = iso["delta"]
                extra.append(
                    f"- Isolated 1-node effect: dDPS {float(d.get('primaryDps') or 0):+.1f} "
                    f"({float(d.get('primaryDpsPct') or 0):+.2f}%) | "
                    f"dEHP {float(d.get('totalEhp') or 0):+.1f} ({float(d.get('totalEhpPct') or 0):+.2f}%) | "
                    f"dLife {float(d.get('life') or 0):+.0f}"
                )
    if not extra:
        return base_md
    return base_md + "\n" + "\n".join(extra)


@click.command("what-if")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--apply", "do_apply", is_flag=True, default=False, help="Commit mutation to slot history (default: CoW rollback).")
@click.option("--level", type=int, default=None, help="Test character level change.")
@click.option("--slot", type=str, default=None, help="Equipment slot to equip --item-text / --item-file into.")
@click.option("--item-text", "item_text", type=str, default=None, help="Raw PoE2 item text to test in --slot.")
@click.option("--item-file", "item_file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Path to raw PoE2 item text file.")
@click.option("--unequip", "unequip_slot", type=str, default=None, help="Unequip the specified slot.")
@click.option("--weapon-swap", "weapon_swap", type=bool, default=None, help="Toggle Weapon Set 2 active state.")
@click.option("--alloc-mode", "alloc_mode", type=int, default=None, help="Passive tree allocMode (0=shared, 1=WS1, 2=WS2).")
@click.option("--tree-node", "tree_node", type=int, default=None, help="Toggle allocation of a passive tree node ID.")
@click.option(
    "--anoint-preview",
    "--isolated-node",
    "anoint_preview",
    is_flag=True,
    default=False,
    help="Preview --tree-node in isolation (like an amulet anoint) without allocating intermediate travel nodes.",
)
@click.option("--attribute-node", "attribute_node", type=int, default=None, help="Attribute travel node ID to switch.")
@click.option("--attribute", type=str, default=None, help="Target attribute (str/dex/int or 1/2/3).")
@click.option("--gem-group", "gem_group", type=int, default=None, help="Skill socket group index (1-based).")
@click.option("--gem-index", "gem_index", type=int, default=None, help="Gem index inside socket group (1-based).")
@click.option("--gem-level", "gem_level", type=int, default=None, help="Gem level override.")
@click.option("--gem-quality", "gem_quality", type=int, default=None, help="Gem quality override.")
@click.option("--gem-enabled", "gem_enabled", type=bool, default=None, help="Enable/disable gem.")
@click.option("--set1", type=bool, default=None, help="Bind skill group to Weapon Set 1.")
@click.option("--set2", type=bool, default=None, help="Bind skill group to Weapon Set 2.")
@click.option("--config-key", "config_key", type=str, default=None, help="Config input key to toggle/set.")
@click.option("--config-value", "config_value", type=str, default=None, help="Config input value (true/false/int/str).")
@click.option("--custom-mods", "custom_mods", type=str, default=None, help="Replace Configuration custom modifiers text.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_what_if(
    ctx: click.Context,
    target: str | None,
    do_apply: bool,
    level: int | None,
    slot: str | None,
    item_text: str | None,
    item_file: Path | None,
    unequip_slot: str | None,
    weapon_swap: bool | None,
    alloc_mode: int | None,
    tree_node: int | None,
    anoint_preview: bool,
    attribute_node: int | None,
    attribute: str | None,
    gem_group: int | None,
    gem_index: int | None,
    gem_level: int | None,
    gem_quality: int | None,
    gem_enabled: bool | None,
    set1: bool | None,
    set2: bool | None,
    config_key: str | None,
    config_value: str | None,
    custom_mods: str | None,
    as_json: bool,
) -> None:
    """Test PoE2 gear, passive, gem, weapon-set, or config edits with 100% CoW rollback (or --apply)."""
    raw_item = item_file.read_text(encoding="utf-8") if item_file is not None else item_text
    params: dict[str, Any] = {
        "target": resolve_target_slot(target),
        "apply": do_apply,
        "level": level,
        "slot": slot,
        "item_text": raw_item,
        "unequip_slot": unequip_slot,
        "weapon_swap": weapon_swap,
        "alloc_mode": alloc_mode,
        "tree_node": tree_node,
        "anoint_preview": True if anoint_preview else None,
        "attribute_node": attribute_node,
        "attribute": attribute,
        "gem_group": gem_group,
        "gem_index": gem_index,
        "gem_level": gem_level,
        "gem_quality": gem_quality,
        "gem_enabled": gem_enabled,
        "set1": set1,
        "set2": set2,
        "config_key": config_key,
        "config_value": _parse_config_value(config_value),
        "custom_mods": custom_mods,
    }
    clean = {k: v for k, v in params.items() if v is not None}
    res = daemon_call_cli("what_if", clean)
    emit_output(ctx, res, as_json=as_json, markdown_text=_format_what_if_with_details_md(res))


@click.command("diff")
@click.argument("source", required=True)
@click.option("--target", type=str, default=None, help="Base build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_diff(ctx: click.Context, source: str, target: str | None, as_json: bool) -> None:
    """Diff the active PoE2 slot build against another build code, file, or share URL."""
    res = daemon_call_cli("diff", {"target": resolve_target_slot(target), "source": source})
    emit_output(ctx, res, as_json=as_json, markdown_text=format_what_if_md(res))


__all__ = ["cmd_diff", "cmd_what_if"]
