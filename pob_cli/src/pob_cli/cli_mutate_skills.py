"""Skill group and gem mutation Click command group (`pob skill`) for `pob` CLI."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    _emit_json,
    _emit_text,
    _output_options,
    _parse_gem_spec,
    _prepend_active_guardrail_warnings,
    _read_json_input,
    _target_option,
    invoke_daemon,
)


def _format_mutation_report(title: str, res: dict[str, Any]) -> str:
    from .formatters import format_mutation_report

    return _prepend_active_guardrail_warnings(format_mutation_report(title, res))


@click.group("skill")
def skill() -> None:
    """Modify, add, or replace skill groups and support/active gems."""


grp_skill = skill


@skill.command("set")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", "--gem-name", "gem", help="Gem 1-based index or name within group.")
@click.option("--new-gem", "--name", "new_gem", help="Replace gem with a new gem name (English or Chinese).")
@click.option("--level", type=int, help="Gem level.")
@click.option("--quality", type=int, help="Gem quality.")
@click.option("--enabled", type=click.Choice(["true", "false"], case_sensitive=False), help="Enable/disable gem or group.")
@click.option("--count", type=int, help="Skill/minion count.")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
@click.option("--allow-legacy", is_flag=True, help="Allow legacy/removed gems with a warning instead of blocking.")
@click.option("--allow-illegal-level", is_flag=True, help="Bypass Exceptional support gem max-level check.")
@click.option("--apply", is_flag=True, help="Persist change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_set(
    group: str,
    gem: str | None,
    new_gem: str | None,
    level: int | None,
    quality: int | None,
    enabled: str | None,
    count: int | None,
    full_dps: str | None,
    allow_legacy: bool,
    allow_illegal_level: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Modify gem level/quality/enabled/count/gemId or group FullDPS state."""
    if new_gem:
        from .guardrails import GuardrailScope, check_input

        check_input(new_gem, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=not allow_legacy)
    payload: dict[str, Any] = {
        "target": target,
        "group": group,
        "gem": gem,
        "new_gem": new_gem,
        "level": level,
        "quality": quality,
        "enabled": (enabled.lower() == "true") if enabled is not None else None,
        "count": count,
        "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
        "apply": apply,
    }
    if allow_legacy:
        payload["allow_legacy"] = True
    if allow_illegal_level:
        payload["allow_illegal_level"] = True
    res = invoke_daemon("skill_set", payload)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(_format_mutation_report(f"Skill Set (Group: {group}, Gem: {gem or 'Group'})", res), out_path=out_path)


@skill.command("add")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", "--gem-name", "--name", "gem", required=True, help="Gem name (English or Chinese).")
@click.option(
    "--level",
    type=int,
    default=None,
    help="Gem level (defaults to gem natural max level: 20 normal, 3 Exceptional, 4 Awakened Exceptional).",
)
@click.option("--quality", type=int, default=20, show_default=True)
@click.option("--allow-legacy", is_flag=True, help="Allow legacy/removed gems with a warning instead of blocking.")
@click.option("--allow-illegal-level", is_flag=True, help="Bypass Exceptional support gem max-level check.")
@click.option("--apply", is_flag=True, help="Persist change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_add(
    group: str,
    gem: str,
    level: int | None,
    quality: int,
    allow_legacy: bool,
    allow_illegal_level: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Add a gem to a skill group."""
    from .guardrails import GuardrailScope, check_input

    check_input(gem, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=not allow_legacy)
    payload: dict[str, Any] = {
        "target": target,
        "group": group,
        "gem": gem,
        "level": level,
        "quality": quality,
        "apply": apply,
    }
    if allow_legacy:
        payload["allow_legacy"] = True
    if allow_illegal_level:
        payload["allow_illegal_level"] = True
    res = invoke_daemon("skill_add", payload)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        eff_lv: Any = level
        if eff_lv is None:
            detail = res.get("detail") or {}
            added_gem = detail.get("addedGem") or {}
            gem_obj = added_gem.get("gem") if isinstance(added_gem, dict) else None
            eff_lv = (gem_obj or {}).get("level") or detail.get("effectiveLevel") or "auto"
        _emit_text(
            _format_mutation_report(f"Add Gem '{gem}' ({eff_lv}/{quality}%) to Group '{group}'", res),
            out_path=out_path,
        )


@skill.command("rm")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", "--gem-name", "--name", "gem", required=True, help="Gem 1-based index or name to remove.")
@click.option("--apply", is_flag=True, help="Persist change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_rm(
    group: str,
    gem: str,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Remove a gem from a skill group."""
    res = invoke_daemon(
        "skill_rm",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(_format_mutation_report(f"Remove Gem '{gem}' from Group '{group}'", res), out_path=out_path)


@skill.command("new-group")
@click.option("--label", help="Skill group label.")
@click.option("--slot", help="Item slot for the skill group (e.g. 'Body Armour', 'Helmet').")
@click.option("--gem", "gem_specs", multiple=True, help="Gem spec 'Name[:level[:quality]]' (repeatable).")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
@click.option("--enabled", type=click.Choice(["true", "false"], case_sensitive=False), help="Enable/disable group.")
@click.option("--allow-legacy", is_flag=True, help="Allow legacy/removed gems with a warning instead of blocking.")
@click.option("--allow-illegal-level", is_flag=True, help="Bypass Exceptional support gem max-level check.")
@click.option("--apply", is_flag=True, help="Persist new skill group into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_new_group(
    label: str | None,
    slot: str | None,
    gem_specs: tuple[str, ...],
    full_dps: str | None,
    enabled: str | None,
    allow_legacy: bool,
    allow_illegal_level: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Create a new skill group and optionally populate its linked gems in one transaction."""
    from .guardrails import GuardrailScope, check_input

    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    gems = [_parse_gem_spec(g) for g in gem_specs] if gem_specs else None
    for g_item in gems or []:
        if g_item.get("name"):
            check_input(
                str(g_item["name"]),
                scope=GuardrailScope.GEM_NAME,
                expected_game="poe1",
                block_legacy_gems=not allow_legacy,
            )
    payload: dict[str, Any] = {
        "target": target,
        "gems": gems,
        "label": label,
        "slot": slot,
        "enabled": (enabled.lower() == "true") if enabled is not None else True,
        "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
        "apply": apply,
    }
    if allow_legacy:
        payload["allow_legacy"] = True
    if allow_illegal_level:
        payload["allow_illegal_level"] = True
    res = invoke_daemon("skill_new_group", payload)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(_format_mutation_report(f"New Skill Group ({label or slot or 'Unnamed'})", res), out_path=out_path)


@skill.command("replace-group")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gems", "gems_json", help="JSON array of gem objects or 'Name[:lvl[:qual]]' strings.")
@click.option("--gem", "gem_specs", multiple=True, help="Gem spec 'Name[:level[:quality]]' (repeatable).")
@click.option("--label", help="Optional new group label.")
@click.option("--slot", help="Optional equipment slot for the skill group.")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
@click.option("--allow-legacy", is_flag=True, help="Allow legacy/removed gems with a warning instead of blocking.")
@click.option("--allow-illegal-level", is_flag=True, help="Bypass Exceptional support gem max-level check.")
@click.option("--apply", is_flag=True, help="Persist replaced skill group into build.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_replace_group(
    group: str,
    gems_json: str | None,
    gem_specs: tuple[str, ...],
    label: str | None,
    slot: str | None,
    full_dps: str | None,
    allow_legacy: bool,
    allow_illegal_level: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Atomically replace all gems in a skill group in ONE daemon call."""
    parsed_gems: list[dict[str, Any]] = []
    if gems_json:
        raw_list = _read_json_input(None, gems_json, label="--gems")
        if not isinstance(raw_list, list):
            raise click.UsageError("--gems must be a JSON array.")
        for entry in raw_list:
            if isinstance(entry, str):
                parsed_gems.append(_parse_gem_spec(entry))
            elif isinstance(entry, dict):
                parsed_gems.append(entry)
            else:
                raise click.UsageError(f"Unsupported gem entry in --gems: {entry!r}")
    for spec in gem_specs:
        parsed_gems.append(_parse_gem_spec(spec))

    if not parsed_gems:
        raise click.UsageError("Provide at least one gem via --gem 'Name[:lvl[:qual]]' or --gems '[...]'.")

    from .guardrails import GuardrailScope, check_input

    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    for g_item in parsed_gems:
        g_name = str(g_item.get("name") or g_item.get("nameSpec") or g_item.get("gem") or "").strip()
        if g_name:
            check_input(
                g_name,
                scope=GuardrailScope.GEM_NAME,
                expected_game="poe1",
                block_legacy_gems=not allow_legacy,
            )

    payload: dict[str, Any] = {
        "target": target,
        "group": group,
        "gems": parsed_gems,
        "label": label,
        "slot": slot,
        "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
        "apply": apply,
    }
    if allow_legacy:
        payload["allow_legacy"] = True
    if allow_illegal_level:
        payload["allow_illegal_level"] = True
    res = invoke_daemon("skill_replace_group", payload)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(
            _format_mutation_report(f"Replace Skill Group '{group}' ({len(parsed_gems)} gems)", res),
            out_path=out_path,
        )
