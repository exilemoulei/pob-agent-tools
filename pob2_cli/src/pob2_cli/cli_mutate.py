# SPDX-License-Identifier: GPL-3.0-only
"""CLI CoW what-if command group (equip|tree|skill|config|apply) & build diff (L4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from .cli_common import (
    daemon_call_cli,
    emit_output,
    output_options,
    pob2_command,
    pob2_group,
    resolve_target_slot,
    target_option,
)
from .errors import DaemonRequestError
from .formatters import format_what_if_md
from .guardrails import normalize_poe2_equipment_slot

_SPEC_KEY_MAP: dict[str, str] = {
    "apply": "apply",
    "level": "level",
    "slot": "slot",
    "item_text": "item_text",
    "itemText": "item_text",
    "item_file": "item_file",
    "itemFile": "item_file",
    "unequip": "unequip_slot",
    "unequip_slot": "unequip_slot",
    "unequipSlot": "unequip_slot",
    "weapon_swap": "weapon_swap",
    "weaponSwap": "weapon_swap",
    "alloc_mode": "alloc_mode",
    "allocMode": "alloc_mode",
    "tree_node": "tree_node",
    "treeNode": "tree_node",
    "anoint_preview": "anoint_preview",
    "anointPreview": "anoint_preview",
    "isolated_node": "anoint_preview",
    "isolatedNode": "anoint_preview",
    "attribute_node": "attribute_node",
    "attributeNode": "attribute_node",
    "attribute": "attribute",
    "gem_group": "gem_group",
    "gemGroup": "gem_group",
    "gem_index": "gem_index",
    "gemIndex": "gem_index",
    "gem_level": "gem_level",
    "gemLevel": "gem_level",
    "gem_quality": "gem_quality",
    "gemQuality": "gem_quality",
    "gem_enabled": "gem_enabled",
    "gemEnabled": "gem_enabled",
    "set1": "set1",
    "set2": "set2",
    "config_key": "config_key",
    "configKey": "config_key",
    "config_value": "config_value",
    "configValue": "config_value",
    "custom_mods": "custom_mods",
    "customMods": "custom_mods",
}


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
            mode_tag = " [instill/isolated preview]" if op.get("isolatedMode") else ""
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


def _run_what_if(
    ctx: click.Context,
    *,
    target: str,
    do_apply: bool,
    as_json: bool,
    params: dict[str, Any],
) -> None:
    clean = {"target": resolve_target_slot(target), "apply": bool(do_apply)}
    for k, v in params.items():
        if v is not None:
            clean[k] = v
    res = daemon_call_cli("what_if", clean)
    emit_output(ctx, res, as_json=as_json, markdown_text=_format_what_if_with_details_md(res))


@pob2_group("what-if")
def what_if_group() -> None:
    """Run CoW what-if mutations by domain (`equip`, `tree`, `skill`, `config`) or multi-domain `apply`."""


@what_if_group.command("equip")
@target_option(allow_slot_alias=False)
@click.option("--apply", "do_apply", is_flag=True, default=False, help="Commit mutation to slot history (default: CoW rollback).")
@click.option("--slot", type=str, default=None, help="Equipment slot to equip --item-text / --item-file into.")
@click.option("--item-text", "item_text", type=str, default=None, help="Raw PoE2 item text to test in --slot.")
@click.option("--item-file", "item_file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Path to raw PoE2 item text file.")
@click.option("--unequip", "unequip_slot", type=str, default=None, help="Unequip the specified slot.")
@click.option("--weapon-swap", "weapon_swap", type=bool, default=None, help="Toggle Weapon Set 2 active state.")
@output_options
@click.pass_context
def cmd_what_if_equip(
    ctx: click.Context,
    target: str,
    do_apply: bool,
    slot: str | None,
    item_text: str | None,
    item_file: Path | None,
    unequip_slot: str | None,
    weapon_swap: bool | None,
    as_json: bool,
) -> None:
    """Test equipping/unequipping an item or toggling Weapon Set 2 in CoW."""
    if slot is None and unequip_slot is None and weapon_swap is None:
        raise click.UsageError("Specify at least one of `--slot`, `--unequip`, or `--weapon-swap`.")
    if item_text is not None and item_file is not None:
        raise click.UsageError("Use either `--item-text` or `--item-file`, not both.")
    canon_slot = normalize_poe2_equipment_slot(slot, command="what-if equip") if slot is not None else None
    canon_unequip = (
        normalize_poe2_equipment_slot(unequip_slot, command="what-if equip") if unequip_slot is not None else None
    )
    raw_item = item_file.read_text(encoding="utf-8") if item_file is not None else item_text
    _run_what_if(
        ctx,
        target=target,
        do_apply=do_apply,
        as_json=as_json,
        params={
            "slot": canon_slot,
            "item_text": raw_item,
            "unequip_slot": canon_unequip,
            "weapon_swap": weapon_swap,
        },
    )


@what_if_group.command("tree")
@target_option(allow_slot_alias=False)
@click.option("--apply", "do_apply", is_flag=True, default=False, help="Commit mutation to slot history (default: CoW rollback).")
@click.option("--tree-node", "tree_node", type=int, default=None, help="Toggle allocation of a passive tree node ID.")
@click.option("--alloc-mode", "alloc_mode", type=int, default=None, help="Passive tree allocMode (0=shared, 1=WS1, 2=WS2).")
@click.option(
    "--isolated-node",
    "--anoint-preview",
    "anoint_preview",
    is_flag=True,
    default=False,
    help="Preview --tree-node in isolation (like an amulet instillation) without allocating intermediate travel nodes.",
)
@click.option("--attribute-node", "attribute_node", type=int, default=None, help="Attribute travel node ID to switch.")
@click.option("--attribute", type=str, default=None, help="Target attribute (str/dex/int or 1/2/3).")
@output_options
@click.pass_context
def cmd_what_if_tree(
    ctx: click.Context,
    target: str,
    do_apply: bool,
    tree_node: int | None,
    alloc_mode: int | None,
    anoint_preview: bool,
    attribute_node: int | None,
    attribute: str | None,
    as_json: bool,
) -> None:
    """Test passive tree node toggles, weapon-set allocMode, or +5 attribute overrides in CoW."""
    if tree_node is None and alloc_mode is None and attribute_node is None:
        raise click.UsageError("Specify at least one of `--tree-node`, `--alloc-mode`, or `--attribute-node`.")
    _run_what_if(
        ctx,
        target=target,
        do_apply=do_apply,
        as_json=as_json,
        params={
            "tree_node": tree_node,
            "alloc_mode": alloc_mode,
            "anoint_preview": True if anoint_preview else None,
            "attribute_node": attribute_node,
            "attribute": attribute,
        },
    )


@what_if_group.command("skill")
@target_option(allow_slot_alias=False)
@click.option("--apply", "do_apply", is_flag=True, default=False, help="Commit mutation to slot history (default: CoW rollback).")
@click.option("--gem-group", "gem_group", type=int, required=True, help="Skill socket group index (1-based).")
@click.option("--gem-index", "gem_index", type=int, default=None, help="Gem index inside socket group (1-based).")
@click.option("--gem-level", "gem_level", type=int, default=None, help="Gem level override.")
@click.option("--gem-quality", "gem_quality", type=int, default=None, help="Gem quality override.")
@click.option("--gem-enabled", "gem_enabled", type=bool, default=None, help="Enable/disable gem.")
@click.option("--set1", type=bool, default=None, help="Bind skill group to Weapon Set 1.")
@click.option("--set2", type=bool, default=None, help="Bind skill group to Weapon Set 2.")
@output_options
@click.pass_context
def cmd_what_if_skill(
    ctx: click.Context,
    target: str,
    do_apply: bool,
    gem_group: int,
    gem_index: int | None,
    gem_level: int | None,
    gem_quality: int | None,
    gem_enabled: bool | None,
    set1: bool | None,
    set2: bool | None,
    as_json: bool,
) -> None:
    """Test gem level/quality/enable edits or skill group weapon-set bindings in CoW."""
    if all(v is None for v in (gem_index, gem_level, gem_quality, gem_enabled, set1, set2)):
        raise click.UsageError(
            "Specify at least one skill edit (`--gem-level`, `--gem-quality`, `--gem-enabled`, `--set1`, or `--set2`)."
        )
    _run_what_if(
        ctx,
        target=target,
        do_apply=do_apply,
        as_json=as_json,
        params={
            "gem_group": gem_group,
            "gem_index": gem_index,
            "gem_level": gem_level,
            "gem_quality": gem_quality,
            "gem_enabled": gem_enabled,
            "set1": set1,
            "set2": set2,
        },
    )


@what_if_group.command("config")
@target_option(allow_slot_alias=False)
@click.option("--apply", "do_apply", is_flag=True, default=False, help="Commit mutation to slot history (default: CoW rollback).")
@click.option("--level", type=int, default=None, help="Test character level change.")
@click.option("--config-key", "config_key", type=str, default=None, help="Config input key to toggle/set.")
@click.option("--config-value", "config_value", type=str, default=None, help="Config input value (true/false/int/str).")
@click.option("--custom-mods", "custom_mods", type=str, default=None, help="Replace Configuration custom modifiers text.")
@output_options
@click.pass_context
def cmd_what_if_config(
    ctx: click.Context,
    target: str,
    do_apply: bool,
    level: int | None,
    config_key: str | None,
    config_value: str | None,
    custom_mods: str | None,
    as_json: bool,
) -> None:
    """Test character level, Configuration toggles, or custom modifiers in CoW."""
    if level is None and config_key is None and custom_mods is None:
        raise click.UsageError("Specify at least one of `--level`, `--config-key`, or `--custom-mods`.")
    _run_what_if(
        ctx,
        target=target,
        do_apply=do_apply,
        as_json=as_json,
        params={
            "level": level,
            "config_key": config_key,
            "config_value": _parse_config_value(config_value),
            "custom_mods": custom_mods,
        },
    )


def _load_apply_spec(spec: str | None, spec_file: Path | None) -> dict[str, Any]:
    if spec is not None and spec_file is not None:
        raise click.UsageError("Use either `--spec` or `--spec-file`, not both.")
    if spec is None and spec_file is None:
        raise click.UsageError("Provide a JSON mutation spec via `--spec` (JSON or @file) or `--spec-file`.")
    if spec_file is not None:
        raw_text = spec_file.read_text(encoding="utf-8")
    else:
        assert spec is not None
        stripped = spec.strip()
        if stripped.startswith("@"):
            p = Path(stripped[1:])
            if not p.is_file():
                raise FileNotFoundError(f"Spec file not found: {p}")
            raw_text = p.read_text(encoding="utf-8")
        else:
            raw_text = stripped
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DaemonRequestError(f"[POE2-INVALID-SPEC] Invalid JSON in `--spec`: {exc.msg}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise DaemonRequestError("[POE2-INVALID-SPEC] `--spec` must be a non-empty JSON object.")

    unknown = sorted(k for k in parsed if k not in _SPEC_KEY_MAP)
    if unknown:
        allowed = ", ".join(sorted(set(_SPEC_KEY_MAP.values())))
        raise DaemonRequestError(
            f"[POE2-INVALID-SPEC] Unknown what-if spec key(s): {', '.join(unknown)}. Allowed keys: {allowed}."
        )
    out: dict[str, Any] = {}
    for k, v in parsed.items():
        canon_k = _SPEC_KEY_MAP[k]
        if v is not None:
            out[canon_k] = v
    if "slot" in out:
        out["slot"] = normalize_poe2_equipment_slot(str(out["slot"]), command="what-if apply")
    if "unequip_slot" in out:
        out["unequip_slot"] = normalize_poe2_equipment_slot(str(out["unequip_slot"]), command="what-if apply")
    if "item_file" in out:
        p = Path(str(out.pop("item_file")))
        if not p.is_file():
            raise FileNotFoundError(f"Item file not found: {p}")
        out["item_text"] = p.read_text(encoding="utf-8")
    if "config_value" in out and isinstance(out["config_value"], str):
        out["config_value"] = _parse_config_value(out["config_value"])
    return out


@what_if_group.command("apply")
@target_option(allow_slot_alias=False)
@click.option("--spec", type=str, default=None, help="JSON object (or @path/to/spec.json) combining equip/tree/skill/config edits in one CoW transaction.")
@click.option("--spec-file", "spec_file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Path to JSON mutation spec file.")
@click.option("--apply", "--commit", "do_apply", is_flag=True, default=False, help="Commit combined mutation to slot history (default: CoW rollback).")
@output_options
@click.pass_context
def cmd_what_if_apply(
    ctx: click.Context,
    target: str,
    spec: str | None,
    spec_file: Path | None,
    do_apply: bool,
    as_json: bool,
) -> None:
    """Run a multi-domain atomic CoW mutation from a JSON `--spec` (equip + tree + skill + config in 1 call)."""
    spec_params = _load_apply_spec(spec, spec_file)
    eff_apply = bool(do_apply or spec_params.pop("apply", False))
    if not spec_params:
        raise DaemonRequestError("[POE2-INVALID-SPEC] `--spec` must contain at least one mutation field.")
    _run_what_if(ctx, target=target, do_apply=eff_apply, as_json=as_json, params=spec_params)


@pob2_command("diff")
@click.argument("source", required=True)
@target_option(allow_slot_alias=True, help_text="Base build slot (default: main).")
@output_options
@click.pass_context
def cmd_diff(ctx: click.Context, source: str, target: str, as_json: bool) -> None:
    """Diff the active PoE2 slot build against another build code, file, or share URL."""
    res = daemon_call_cli("diff", {"target": resolve_target_slot(target), "source": source})
    emit_output(ctx, res, as_json=as_json, markdown_text=format_what_if_md(res))


__all__ = [
    "cmd_diff",
    "cmd_what_if_apply",
    "cmd_what_if_config",
    "cmd_what_if_equip",
    "cmd_what_if_skill",
    "cmd_what_if_tree",
    "what_if_group",
]
