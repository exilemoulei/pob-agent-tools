"""Skill, passive tree, and ConfigTab Click command groups for `pob` CLI."""

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
from .cli_inspect_parts import _run_tree_inspect
from .formatters import format_mutation_report


@click.group("skill")
def grp_skill() -> None:
    """Modify, add, or replace skill groups and support/active gems."""


@grp_skill.command("set")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", "--gem-name", "gem", help="Gem 1-based index or name within group.")
@click.option("--new-gem", "--name", "new_gem", help="Replace gem with a new gem name (English or Chinese).")
@click.option("--level", type=int, help="Gem level.")
@click.option("--quality", type=int, help="Gem quality.")
@click.option("--enabled", type=click.Choice(["true", "false"], case_sensitive=False), help="Enable/disable gem or group.")
@click.option("--count", type=int, help="Skill/minion count.")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
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
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Modify gem level/quality/enabled/count/gemId or group FullDPS state."""
    if new_gem:
        from .guardrails import GuardrailScope, check_input

        check_input(new_gem, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
    res = invoke_daemon(
        "skill_set",
        {
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
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(format_mutation_report(f"Skill Set (Group: {group}, Gem: {gem or 'Group'})", res), out_path=out_path)


@grp_skill.command("add")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gem", "--gem-name", "--name", "gem", required=True, help="Gem name (English or Chinese).")
@click.option(
    "--level",
    type=int,
    default=None,
    help="Gem level (defaults to gem natural max level: 20 normal, 3 Exceptional, 4 Awakened Exceptional).",
)
@click.option("--quality", type=int, default=20, show_default=True)
@click.option("--apply", is_flag=True, help="Persist change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skill_add(
    group: str,
    gem: str,
    level: int | None,
    quality: int,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Add a gem to a skill group."""
    from .guardrails import GuardrailScope, check_input

    check_input(gem, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
    res = invoke_daemon(
        "skill_add",
        {
            "target": target,
            "group": group,
            "gem": gem,
            "level": level,
            "quality": quality,
            "apply": apply,
        },
    )
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
            format_mutation_report(f"Add Gem '{gem}' ({eff_lv}/{quality}%) to Group '{group}'", res),
            out_path=out_path,
        )


@grp_skill.command("rm")
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
        _emit_text(format_mutation_report(f"Remove Gem '{gem}' from Group '{group}'", res), out_path=out_path)


@grp_skill.command("new-group")
@click.option("--label", help="Skill group label.")
@click.option("--slot", help="Item slot for the skill group (e.g. 'Body Armour', 'Helmet').")
@click.option("--gem", "gem_specs", multiple=True, help="Gem spec 'Name[:level[:quality]]' (repeatable).")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
@click.option("--enabled", type=click.Choice(["true", "false"], case_sensitive=False), help="Enable/disable group.")
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
            check_input(str(g_item["name"]), scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)
    res = invoke_daemon(
        "skill_new_group",
        {
            "target": target,
            "gems": gems,
            "label": label,
            "slot": slot,
            "enabled": (enabled.lower() == "true") if enabled is not None else True,
            "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(format_mutation_report(f"New Skill Group ({label or slot or 'Unnamed'})", res), out_path=out_path)


@grp_skill.command("replace-group")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--gems", "gems_json", help="JSON array of gem objects or 'Name[:lvl[:qual]]' strings.")
@click.option("--gem", "gem_specs", multiple=True, help="Gem spec 'Name[:level[:quality]]' (repeatable).")
@click.option("--label", help="Optional new group label.")
@click.option("--slot", help="Optional equipment slot for the skill group.")
@click.option("--full-dps", type=click.Choice(["true", "false"], case_sensitive=False), help="Include group in FullDPS.")
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
            check_input(g_name, scope=GuardrailScope.GEM_NAME, expected_game="poe1", block_legacy_gems=True)

    res = invoke_daemon(
        "skill_replace_group",
        {
            "target": target,
            "group": group,
            "gems": parsed_gems,
            "label": label,
            "slot": slot,
            "include_in_full_dps": (full_dps.lower() == "true") if full_dps is not None else None,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(
            format_mutation_report(f"Replace Skill Group '{group}' ({len(parsed_gems)} gems)", res),
            out_path=out_path,
        )


@click.group("tree", invoke_without_command=True)
@click.option("--search", "searches", multiple=True, help="Search passive tree nodes by EN/ZH name, stat, ID, or '#1' (repeatable).")
@click.option("--type", "node_types", multiple=True, help="Filter by node type (Keystone, Notable, Mastery, Socket, Ascendancy; repeatable).")
@click.option("--allocated/--unallocated", "allocated", default=None, help="Filter to allocated or unallocated nodes.")
@click.option("--max-cost", "max_cost", type=int, default=None, help="Maximum shortest-path point cost.")
@click.option("--show-path", "show_path", is_flag=True, help="Include shortest allocation path node IDs.")
@click.option("--masteries", "masteries", is_flag=True, help="Include full masteryEffects IDs and text without truncation.")
@click.option("--socket-radius", "socket_radius", type=str, default=None, help="Inspect nodes within jewel radii of socket (ID, '#1', or name).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def grp_tree(
    ctx: click.Context,
    searches: tuple[str, ...],
    node_types: tuple[str, ...],
    allocated: bool | None,
    max_cost: int | None,
    show_path: bool,
    masteries: bool,
    socket_radius: str | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Inspect passive tree (`pob tree [--search ...]`), mutate nodes (`change`), or switch class/ascendancy (`set-class`)."""
    if ctx.invoked_subcommand is None:
        _run_tree_inspect(
            searches=searches,
            node_types=node_types,
            allocated=allocated,
            max_cost=max_cost,
            show_path=show_path,
            masteries=masteries,
            socket_radius=socket_radius,
            target=target,
            as_json=as_json,
            compact=compact,
            out_path=out_path,
        )
        return

    from click.core import ParameterSource

    ctx.ensure_object(dict)
    tree_opts: dict[str, Any] = {}
    if ctx.get_parameter_source("target") == ParameterSource.COMMANDLINE:
        tree_opts["target"] = target
    if ctx.get_parameter_source("as_json") == ParameterSource.COMMANDLINE:
        tree_opts["as_json"] = as_json
    if ctx.get_parameter_source("compact") == ParameterSource.COMMANDLINE:
        tree_opts["compact"] = compact
    if ctx.get_parameter_source("out_path") == ParameterSource.COMMANDLINE:
        tree_opts["out_path"] = out_path
    ctx.obj["tree_group_opts"] = tree_opts


def _resolve_tree_subcommand_opts(
    ctx: click.Context,
    *,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> tuple[str, bool, bool, str | None]:
    """Propagate CLI-provided `pob tree` group options into `change` / `set-class` when not overridden."""
    from click.core import ParameterSource

    parent_opts = (ctx.obj.get("tree_group_opts") if isinstance(ctx.obj, dict) else None) or {}
    if "target" in parent_opts and ctx.get_parameter_source("target") != ParameterSource.COMMANDLINE:
        target = parent_opts["target"]
    if "as_json" in parent_opts and ctx.get_parameter_source("as_json") != ParameterSource.COMMANDLINE:
        as_json = bool(parent_opts["as_json"])
    if "compact" in parent_opts and ctx.get_parameter_source("compact") != ParameterSource.COMMANDLINE:
        compact = bool(parent_opts["compact"])
    if "out_path" in parent_opts and ctx.get_parameter_source("out_path") != ParameterSource.COMMANDLINE:
        out_path = parent_opts["out_path"]
    return target, as_json, compact, out_path


@grp_tree.command("change")
@click.option("--allocate", help="Comma-separated node IDs, names, or Mastery `<nodeId>:<effectId>` to allocate.")
@click.option("--deallocate", help="Comma-separated node IDs or names to deallocate.")
@click.option(
    "--allow-cascade/--no-allow-cascade",
    "allow_cascade",
    default=True,
    show_default=True,
    help="Allow deallocations that cascade-orphan >3 downstream passive nodes.",
)
@click.option("--apply", is_flag=True, help="Persist passive tree changes into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_change(
    ctx: click.Context,
    allocate: str | None,
    deallocate: str | None,
    allow_cascade: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Allocate or deallocate passive tree nodes idempotently (R12)."""
    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    alloc_list = [x.strip() for x in allocate.split(",") if x.strip()] if allocate else []
    dealloc_list = [x.strip() for x in deallocate.split(",") if x.strip()] if deallocate else []
    res = invoke_daemon(
        "tree_change",
        {
            "target": target,
            "allocate": alloc_list,
            "deallocate": dealloc_list,
            "allow_cascade": allow_cascade,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_mutation_report("Passive Tree Change", res), out_path=out_path)


@grp_tree.command("set-class")
@click.argument("class_name", required=False)
@click.argument("ascendancy", required=False)
@click.option("--secondary-asc", "secondary_asc", help="Secondary / Bloodline ascendancy name (or 'None' to clear).")
@click.option("--apply", is_flag=True, help="Persist class/ascendancy change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_set_class(
    ctx: click.Context,
    class_name: str | None,
    ascendancy: str | None,
    secondary_asc: str | None,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Switch character class, primary ascendancy, and/or secondary (Bloodline) ascendancy."""
    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    if not class_name and not ascendancy and not secondary_asc:
        raise click.UsageError("Specify class_name, ascendancy, and/or --secondary-asc.")

    from .guardrails import check_poe2_class_or_ascendancy

    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_asc, field="secondary_ascendancy")

    res = invoke_daemon(
        "tree_set_class",
        {
            "target": target,
            "class_name": class_name,
            "ascendancy_name": ascendancy,
            "secondary_ascendancy_name": secondary_asc,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    detail = res.get("detail") or {}
    changes = detail.get("changes") or []
    title = f"Set Class / Ascendancy ({'; '.join(changes) or 'no-op'})"
    _emit_text(format_mutation_report(title, res), out_path=out_path)


@click.group("config")
def grp_config() -> None:
    """Inspect or modify PoB ConfigTab parameters and customMods."""


@grp_config.command("list")
@click.option("--all", "include_defaults", is_flag=True, help="Include default/inactive config variables.")
@click.option("--search", "search", help="Filter config variables by case-insensitive EN/ZH substring.")
@click.option("--with-options", "with_options", is_flag=True, help="Include full dropdown options arrays in JSON.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_config_list(
    include_defaults: bool,
    search: str | None,
    with_options: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """List active (non-default) ConfigTab variables and raw customMods."""
    from .formatters import fmt_num, render_table

    if search:
        from .guardrails import GuardrailScope, check_input

        check_input(search, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")

    res = invoke_daemon(
        "config_list",
        {
            "target": target,
            "includeDefaults": include_defaults,
            "search": search,
            "with_options": with_options,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    active_set = res.get("activeConfigSet") or {}
    set_title = active_set.get("title") or res.get("activeConfigSetId") or "Default"
    filter_tag = f" | Filter: `{search}`" if search else ""
    lines = [
        f"### ConfigTab (`{set_title}`) — `{res.get('activeCount', 0)}` active / `{res.get('totalCount', 0)}` total{filter_tag}",
    ]
    rows = [
        [
            v.get("sectionZh") or v.get("section") or "-",
            v.get("var") or "-",
            fmt_num(v.get("value")),
            v.get("labelZh") or v.get("label") or "-",
        ]
        for v in res.get("variables", [])
    ]
    lines.append(render_table(["Section", "Var", "Value", "Label"], rows))

    cm_text = (res.get("customModsText") or "").strip()
    if cm_text:
        lines.append("\n#### Custom Modifiers\n```text\n" + cm_text + "\n```")
    _emit_text(_prepend_active_guardrail_warnings("\n".join(lines)), out_path=out_path)


@grp_config.command("set")
@click.argument("pairs", nargs=-1, required=False)
@click.option("--custom-mods", "custom_mods", help="Replace custom modifiers text (or 'clear' to disable).")
@click.option("--append-mods", "append_mods", help="Append custom modifier line(s) without overwriting existing customMods.")
@click.option("--apply", is_flag=True, help="Persist config changes into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_config_set(
    pairs: tuple[str, ...],
    custom_mods: str | None,
    append_mods: str | None,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Set/reset ConfigTab variables (`key=value` or `key=reset`), replace `--custom-mods`, or `--append-mods`."""
    if not pairs and custom_mods is None and append_mods is None:
        raise click.UsageError("Provide at least one `key=value` assignment, `--custom-mods`, or `--append-mods`.")
    assignments: list[list[str]] = []
    for p in pairs:
        if "=" not in p:
            raise click.UsageError(f"Invalid config assignment '{p}'; expected key=value.")
        k, v = p.split("=", 1)
        assignments.append([k.strip(), v.strip()])

    from .guardrails import GuardrailScope, check_input

    for k, _ in assignments:
        check_input(k, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
    if custom_mods is not None and custom_mods.strip().lower() not in ("clear", "reset", "none", "disable", "disabled", ""):
        check_input(custom_mods, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")
    if append_mods is not None:
        check_input(append_mods, scope=GuardrailScope.CONFIG_VAR, expected_game="poe1")

    res = invoke_daemon(
        "config_set",
        {
            "target": target,
            "assignments": assignments,
            "custom_mods": custom_mods,
            "appendCustomMods": append_mods,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        desc_parts = list(pairs)
        if custom_mods is not None:
            desc_parts.append(f"customMods={custom_mods!r}")
        if append_mods is not None:
            desc_parts.append(f"appendMods={append_mods!r}")
        _emit_text(format_mutation_report(f"Config Set ({', '.join(desc_parts)})", res), out_path=out_path)


def register_mutate_commands(group: click.Group) -> None:
    group.add_command(grp_skill)
    group.add_command(grp_tree)
    group.add_command(grp_config)
