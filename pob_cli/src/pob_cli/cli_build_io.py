"""Build I/O, slot switching, and undo/redo Click commands for `pob` CLI."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    _emit_json,
    _emit_text,
    _output_options,
    _prepare_load_source,
    _prepend_warning,
    _target_option,
    invoke_daemon,
)


@click.command("new")
@click.option("--class", "class_name", help="Starting character class (e.g. Witch, Templar, Shadow).")
@click.option("--asc", "ascendancy", help="Primary ascendancy name (e.g. Hierophant, Saboteur).")
@click.option("--secondary-asc", "secondary_asc", help="Secondary / Bloodline ascendancy name.")
@click.option("--level", type=int, help="Character level (1..100).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_new(
    class_name: str | None,
    ascendancy: str | None,
    secondary_asc: str | None,
    level: int | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Create a fresh empty build in the target slot with optional class, ascendancy, and level."""
    from .formatters import render_table
    from .guardrails import check_poe2_class_or_ascendancy

    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_asc, field="secondary_ascendancy")

    res = invoke_daemon(
        "new_build",
        {
            "target": target,
            "class_name": class_name,
            "ascendancy": ascendancy,
            "secondary_ascendancy": secondary_asc,
            "level": level,
            "apply": True,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    detail = res.get("detail") if isinstance(res.get("detail"), dict) else {}
    info = detail.get("buildInfo") or res.get("buildInfo") or (res.get("after") or {}).get("buildInfo") or {}
    pts = info.get("points") or (res.get("after") or {}).get("points") or {}
    eff_level = info.get("level") or detail.get("level") or level or "-"
    rows = [
        ["Target Slot", target],
        [
            "Class / Ascendancy",
            f"{info.get('classNameZh') or info.get('className') or class_name or '-'} / "
            f"{info.get('ascendClassNameZh') or info.get('ascendClassName') or ascendancy or '-'}",
        ],
        ["Level", eff_level],
        [
            "Passive Points",
            f"{pts.get('used', 0)}/{pts.get('usedMax', 0)} (Asc: {pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)})",
        ],
    ]
    _emit_text("### New Build Created\n" + render_table(["Field", "Value"], rows), out_path=out_path)


@click.command("load")
@click.argument("source")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_load(source: str, target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Load a build from local .xml/.txt file (or @file), share URL (pobb.in/poedb/poe.ninja/...), or Base64 code."""
    from .daemon import emit_stderr_update_alert
    from .errors import CrossGameHallucinationError
    from .formatters import render_table, strip_pob_escapes
    from .updater import load_update_state

    params: dict[str, Any] = {"target": target}
    resolved_src, resolved_xml_code = _prepare_load_source(source)
    if resolved_xml_code is not None:
        params["xml_code"] = resolved_xml_code
    else:
        params["source"] = resolved_src

    try:
        res = invoke_daemon("load", params)
    except CrossGameHallucinationError:
        raise
    except click.exceptions.Exit:
        raise
    except click.ClickException:
        upd_state = load_update_state()
        if str(upd_state.get("state") or "").startswith("degraded_"):
            emit_stderr_update_alert(upd_state)
        raise
    except Exception as exc:
        upd_state = load_update_state()
        if str(upd_state.get("state") or "").startswith("degraded_"):
            emit_stderr_update_alert(upd_state)
        raise click.ClickException(str(exc)) from exc

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    info = res.get("buildInfo") or {}
    pts = info.get("points") or {}
    header = res.get("buildHeader") or {}
    main_sg = header.get("mainSocketGroup") or {}
    main_sk = header.get("mainSkill") or {}

    def _dd_label(dd: dict[str, Any]) -> str:
        lst = dd.get("list") or []
        idx = max(0, int(dd.get("index") or 1) - 1)
        if not lst or idx >= len(lst):
            return "-"
        entry = lst[idx]
        if isinstance(entry, dict):
            return strip_pob_escapes(entry.get("labelZh") or entry.get("label") or "-")
        return strip_pob_escapes(str(entry))

    rows = [
        ["Target Slot", target],
        ["Import Method", res.get("importMethod")],
        [
            "Class / Ascendancy",
            f"{info.get('classNameZh') or info.get('className')} / {info.get('ascendClassNameZh') or info.get('ascendClassName')}",
        ],
        ["Level", info.get("level")],
        [
            "Passive Points",
            f"{pts.get('used', 0)}/{pts.get('usedMax', 0)} (Asc: {pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)})",
        ],
        ["Tree Version", info.get("treeVersion")],
        ["Main Skill Group", f"#{main_sg.get('index')} ({_dd_label(main_sg)})"],
        ["Active Skill", _dd_label(main_sk)],
    ]
    body = render_table(["Field", "Value"], rows)
    _emit_text(_prepend_warning(body, res), out_path=out_path)


@click.command("save")
@click.argument("xml_path", type=click.Path(dir_okay=False, writable=True))
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_save(xml_path: str, target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Save the current build to a local .xml file (written by CLI, R11)."""
    from pathlib import Path

    res = invoke_daemon("export", {"target": target, "include_xml": True})
    out_file = Path(xml_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    xml_content = res.get("xml") or ""
    out_file.write_text(xml_content, encoding="utf-8")
    payload = {"savedPath": str(out_file), "bytes": len(xml_content.encode("utf-8")), "target": target}
    if as_json:
        _emit_json(payload, compact=compact, out_path=out_path)
    else:
        _emit_text(f"Saved `{target}` build XML to `{out_file}` ({payload['bytes']:,} bytes).", out_path=out_path)


@click.command("export-code")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_export_code(target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Export current build as a URL-safe Base64 PoB share code."""
    res = invoke_daemon("export", {"target": target, "include_xml": False})
    if as_json:
        _emit_json({"target": target, "code": res.get("code")}, compact=compact, out_path=out_path)
    else:
        _emit_text(res.get("code") or "", out_path=out_path)


@click.command("switch-main")
@click.option("--spec", "spec_index", help="1-based passive tree spec index or title.")
@click.option("--skill-group", "skill_group", help="1-based skill group index or label.")
@click.option("--skill", "main_skill_index", type=int, help="1-based active skill index in group.")
@click.option("--part", "skill_part_index", type=int, help="1-based skill part index.")
@click.option("--item-set", "item_set_id", help="Item set ID or title.")
@click.option("--skill-set", "skill_set_id", help="Skill set ID or title.")
@click.option("--config-set", "config_set_id", help="Config set ID or title.")
@click.option("--loadout", "loadout_index", help="1-based synced loadout index or title.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_switch_main(
    spec_index: str | None,
    skill_group: str | None,
    main_skill_index: int | None,
    skill_part_index: int | None,
    item_set_id: str | None,
    skill_set_id: str | None,
    config_set_id: str | None,
    loadout_index: str | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Switch active passive tree spec, main skill group/part, item/skill/config set, or loadout."""
    from .formatters import format_mutation_report

    res = invoke_daemon(
        "switch_main",
        {
            "target": target,
            "spec_index": spec_index,
            "skill_group": skill_group,
            "main_skill_index": main_skill_index,
            "skill_part_index": skill_part_index,
            "item_set_id": item_set_id,
            "skill_set_id": skill_set_id,
            "config_set_id": config_set_id,
            "loadout_index": loadout_index,
            "apply": True,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        _emit_text(format_mutation_report("Switch Active Selection", res), out_path=out_path)


def _run_undo_redo(
    action: str, target: str, as_json: bool, compact: bool, out_path: str | None
) -> None:
    """Shared body of `pob undo` / `pob redo` (`action` is display label e.g. 'Undo'; RPC is `action.lower()`)."""
    from .formatters import render_table

    res = invoke_daemon(action.lower(), {"target": target})
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    deltas = res.get("diff", {}).get("deltas", [])
    rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
    body = (
        f"### {action} Applied (Undo depth: `{res.get('undoDepth')}`, Redo depth: `{res.get('redoDepth')}`)\n"
        + render_table(["Stat", f"Before {action}", f"After {action}", "Delta"], rows)
    )
    _emit_text(body, out_path=out_path)


@click.command("undo")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_undo(target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Undo the last `--apply` mutation."""
    _run_undo_redo("Undo", target, as_json, compact, out_path)


@click.command("redo")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_redo(target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Redo the last undone mutation."""
    _run_undo_redo("Redo", target, as_json, compact, out_path)


def register_build_io_commands(group: click.Group) -> None:
    for cmd in (cmd_new, cmd_load, cmd_save, cmd_export_code, cmd_switch_main, cmd_undo, cmd_redo):
        group.add_command(cmd)
