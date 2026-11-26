"""ConfigTab Click command group (and `grp_tree` re-export) for `pob` CLI."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    _emit_json,
    _emit_text,
    _output_options,
    _prepend_active_guardrail_warnings,
    _target_option,
    invoke_daemon,
)
from .cli_tree import (
    cmd_tree_change,
    cmd_tree_prune,
    cmd_tree_set_class,
    grp_tree,
)


def _format_mutation_report(title: str, res: dict[str, Any]) -> str:
    from .formatters import format_mutation_report

    return format_mutation_report(title, res)


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
        _emit_text(_format_mutation_report(f"Config Set ({', '.join(desc_parts)})", res), out_path=out_path)


def register_mutate_commands(group: click.Group) -> None:
    from .cli_mutate_skills import skill

    group.add_command(skill)
    group.add_command(grp_tree)
    group.add_command(grp_config)


__all__ = [
    "cmd_config_list",
    "cmd_config_set",
    "cmd_tree_change",
    "cmd_tree_prune",
    "cmd_tree_set_class",
    "grp_config",
    "grp_tree",
    "register_mutate_commands",
]
