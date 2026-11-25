"""Read-only items, skills, passive tree, and calcs breakdown Click commands for `pob` CLI."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    _echo_stderr_utf8,
    _emit_json,
    _emit_text,
    _output_options,
    _prepend_active_guardrail_warnings,
    _target_option,
    invoke_daemon,
)


def _apply_loop_guard(res: dict[str, Any], body: str | None = None) -> str:
    warn = str(res.get("loopGuardWarning") or "").strip() if isinstance(res, dict) else ""
    if warn:
        _echo_stderr_utf8(warn)
    if body is None:
        return ""
    return f"> {warn}\n\n{body}" if warn else body


@click.command("items")
@click.option("--slot", help="Inspect a specific slot (e.g. 'Weapon 1', 'Amulet', 'Flask 1', or '#1' jewel socket).")
@click.option("--all", "all_items", is_flag=True, help="Fetch all equipped items and allocated jewel sockets with full raw mods.")
@click.option("--raw", is_flag=True, help="Include raw PoE item text.")
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_items(
    slot: str | None,
    all_items: bool,
    raw: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """List equipped items, inspect a single slot, or dump all equipped items & jewels (`--all --raw`)."""
    from .formatters import render_table, strip_pob_escapes

    if slot:
        from .guardrails import GuardrailScope, check_input

        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")

    res = invoke_daemon(
        "items",
        {"target": target, "slot": slot, "raw": bool(raw or all_items), "allRaw": bool(all_items)},
    )
    if as_json:
        _apply_loop_guard(res)
        _emit_json(res, compact=compact, out_path=out_path)
        return

    if slot:
        item = res.get("item")
        slot_name = (res.get("slot") or {}).get("name", slot)
        if not item:
            _emit_text(_apply_loop_guard(res, f"Slot `{slot_name}` is empty."), out_path=out_path)
            return
        lines = [
            f"### Slot `{slot_name}`: {strip_pob_escapes(item.get('titleZh') or item.get('title') or item.get('name'))} (`{item.get('rarity')}`)",
        ]
        tip = res.get("tooltip") or {}
        for ln in tip.get("lines", []):
            txt = strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
            if txt:
                lines.append(f"  {txt}")
        if raw and res.get("raw"):
            lines.append("\n```text\n" + res["raw"].strip() + "\n```")
        _emit_text(_apply_loop_guard(res, _prepend_active_guardrail_warnings("\n".join(lines))), out_path=out_path)
        return

    if all_items:
        slots = res.get("equippedSlots", [])
        lines = [
            f"### All Equipped Items & Allocated Jewels (`{res.get('equippedCount', 0)}` equipped / `{len(slots)}` slots)",
        ]
        for s in slots:
            slot_disp = s["slotName"]
            if s.get("socketIndex"):
                slot_disp = f"{slot_disp} (#{s['socketIndex']})"
            it = s.get("item")
            if not it:
                lines.append(f"\n#### `{slot_disp}` — _(Empty)_")
                continue
            title = strip_pob_escapes(it.get("titleZh") or it.get("title") or it.get("nameZh") or it.get("name") or "-")
            base = strip_pob_escapes(it.get("baseNameZh") or it.get("baseName") or "")
            status = " [OFF]" if (s.get("isFlask") and not s.get("active")) else ""
            lines.append(f"\n#### `{slot_disp}`{status}: {title} — {base} (`{it.get('rarity') or '-'}`)")
            raw_txt = s.get("raw")
            if raw_txt:
                lines.append("```text\n" + str(raw_txt).strip() + "\n```")
        _emit_text(_apply_loop_guard(res, _prepend_active_guardrail_warnings("\n".join(lines))), out_path=out_path)
        return

    rows = []
    for s in res.get("equippedSlots", []):
        it = s.get("item") or {}
        slot_disp = s["slotName"]
        if s.get("socketIndex"):
            slot_disp = f"{slot_disp} (#{s['socketIndex']})"
        state = "Active" if s.get("active") else ("Flask Off" if s.get("isFlask") else ("Inactive" if s.get("inactive") else "Equipped"))
        rows.append(
            [
                slot_disp,
                strip_pob_escapes(it.get("titleZh") or it.get("title") or it.get("nameZh") or it.get("name") or "-"),
                it.get("baseNameZh") or it.get("baseName") or "-",
                it.get("rarity") or "-",
                state if it else "Empty",
            ]
        )
    _emit_text(_apply_loop_guard(res, render_table(["Slot", "Item Name", "Base", "Rarity", "Status"], rows)), out_path=out_path)


@click.command("skills")
@click.option("--group", help="Inspect a specific skill group ('main', index, or label).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_skills(group: str | None, target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """List all skill groups or inspect gems and per-gem DPS tooltips in a group."""
    from .formatters import fmt_num, render_table, strip_pob_escapes

    res = invoke_daemon("skills", {"target": target, "group": group})
    if as_json:
        _apply_loop_guard(res)
        _emit_json(res, compact=compact, out_path=out_path)
        return

    if group is not None:
        grp = res.get("group", {})
        rows = []
        for gm in grp.get("gems", []):
            dps_info = gm.get("tooltipDps") or {}
            rows.append(
                [
                    gm.get("index"),
                    strip_pob_escapes(gm.get("nameZh") or gm.get("name") or gm.get("nameSpec")),
                    "Support" if gm.get("support") else "Active",
                    f"{gm.get('level')}/{gm.get('quality')}%",
                    "Yes" if gm.get("enabled", True) else "No",
                    f"{dps_info.get('pct', 0):+.1f}% ({fmt_num(dps_info.get('delta'))})" if dps_info.get("pct") is not None else "-",
                ]
            )
        body = (
            f"### Skill Group #{grp.get('index')}: {strip_pob_escapes(grp.get('displayLabelZh') or grp.get('displayLabel'))} (Slot: `{grp.get('slot') or '-'}`)\n"
            + render_table(["#", "Gem", "Type", "Lv/Qual", "Enabled", "Tooltip DPS Impact"], rows)
        )
        _emit_text(_apply_loop_guard(res, body), out_path=out_path)
        return

    rows = []
    for g in res.get("groups", []):
        gems_summary = ", ".join(
            f"{strip_pob_escapes(gm.get('nameZh') or gm.get('name'))}{'' if gm.get('enabled', True) else '[OFF]'}"
            for gm in g.get("gems", [])
        )
        rows.append(
            [
                f"#{g['index']}{' *MAIN*' if g.get('isMain') else ''}",
                strip_pob_escapes(g.get("displayLabelZh") or g.get("displayLabel") or g.get("label")),
                g.get("slot") or "-",
                "Yes" if g.get("enabled", True) else "No",
                "Yes" if g.get("includeInFullDPS") else "No",
                gems_summary,
            ]
        )
    _emit_text(_apply_loop_guard(res, render_table(["Group", "Label", "Slot", "Enabled", "FullDPS", "Gems"], rows)), out_path=out_path)


def _run_tree_inspect(
    *,
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
    limit: int = 100,
) -> None:
    """Summarize allocated passive tree nodes or search/filter tree nodes with paths, masteries, or jewel radius."""
    from .formatters import format_tree_markdown
    from .guardrails import GuardrailScope, check_input

    search_list = [s for s in searches if s]
    for s in search_list:
        check_input(s, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    params: dict[str, Any] = {
        "target": target,
        "search": search_list[0] if search_list else None,
        "queries": search_list or None,
        "node_types": list(node_types) or None,
        "allocated": allocated,
        "max_cost": max_cost,
        "show_path": show_path,
        "masteries": masteries,
        "socket_radius": socket_radius,
        "limit": limit,
    }
    res = invoke_daemon("tree", params)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    search_label = ", ".join(search_list) if search_list else None
    body = format_tree_markdown(res, search_label=search_label, show_path=show_path, masteries=masteries)
    _emit_text(_prepend_active_guardrail_warnings(body), out_path=out_path)


@click.command("calc")
@click.argument("keywords", nargs=-1, required=True)
@click.option("--limit", type=click.IntRange(min=0), default=20, show_default=True, help="Maximum matched rows to return (0 = all).")
@click.option(
    "--max-breakdowns",
    "max_breakdowns",
    type=click.IntRange(min=0),
    default=30,
    show_default=True,
    help="Maximum calcs_breakdown sub-tables to expand per keyword (0 = all).",
)
@click.option("--no-breakdown", "no_breakdown", is_flag=True, help="Skip calcs_breakdown sub-tables (fast numeric values only).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_calc(
    keywords: tuple[str, ...],
    limit: int,
    max_breakdowns: int,
    no_breakdown: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Search PoB Calcs breakdown tables and formulas by one or more English/Chinese keywords."""
    from .formatters import render_table, strip_pob_escapes
    from .guardrails import GuardrailScope, check_input

    kw_list = list(keywords)
    for kw in kw_list:
        if kw.strip():
            check_input(kw, scope=GuardrailScope.CALC_QUERY, expected_game="poe1")
    res = invoke_daemon(
        "calc",
        {
            "target": target,
            "keyword": kw_list[0],
            "keywords": kw_list,
            "limit": limit,
            "max_breakdowns": max_breakdowns,
            "no_breakdown": no_breakdown,
        },
    )
    if as_json:
        _apply_loop_guard(res)
        _emit_json(res, compact=compact, out_path=out_path)
        return

    matches = res.get("matches") or []
    kw_disp = ", ".join(kw_list)

    def _header(total: int) -> list[str]:
        out = [f"### Calcs Breakdown for `{kw_disp}` ({total} matches)"]
        skill = res.get("calcsSkill")

        def _named(group_no: Any, group: Any, active: Any) -> str:
            """`#3 Group / Active skill`; the skill only when it is not the group's own name."""
            group, active = strip_pob_escapes(group), strip_pob_escapes(active)
            return f"#{group_no} {group}" + (f" / {active}" if active and active != group else "")

        if isinstance(skill, dict):
            shown = _named(
                skill.get("socketGroup"),
                skill.get("labelZh") or skill.get("label"),
                skill.get("skillLabelZh") or skill.get("skillLabel"),
            )
            out.append(f"- Calcs tab skill: {shown}")
            if skill.get("isMainSkill") is False:
                main = _named(
                    skill.get("mainSocketGroup"),
                    skill.get("mainLabelZh") or skill.get("mainLabel"),
                    skill.get("mainSkillLabelZh") or skill.get("mainSkillLabel"),
                )
                out.append(
                    f"> [WARN] These rows are for {shown}, the skill selected in PoB's Calcs tab, not the "
                    f"main skill ({main}). `pob summary` reports the main skill."
                )
        return out

    def _append_calc_match(lines_out: list[str], m: dict[str, Any]) -> None:
        cell_texts = " | ".join(
            str(c.get("text") or c.get("raw") or c.get("value") or "")
            for c in m.get("cells", [])
            if c.get("text") or c.get("raw") or c.get("value") is not None
        )
        lines_out.append(
            f"\n#### [{m.get('sectionGroup')} / {m.get('subsection')}] {m.get('rowLabelZh') or m.get('rowLabel')}: `{cell_texts}`"
        )
        if no_breakdown:
            return
        for c in m.get("cells", []):
            for sec in c.get("breakdown") or []:
                if sec.get("type") == "text":
                    for ln in sec.get("lines", []):
                        lines_out.append(f"  - {strip_pob_escapes(ln)}")
                elif sec.get("type") == "table":
                    cols = sec.get("cols", [])
                    headers = [col.get("label") or col.get("key") or "" for col in cols]
                    keys = [col.get("key") for col in cols]
                    t_rows = [[r.get(k, "") for k in keys] for r in sec.get("rows", [])]
                    if headers and t_rows:
                        lines_out.append(render_table(headers, t_rows))

    sub_results = res.get("results")
    if isinstance(sub_results, list):
        sub_dicts = [sr for sr in sub_results if isinstance(sr, dict)]
        total_shown = sum(len(sr.get("matches") or []) for sr in sub_dicts)
        if total_shown == 0 and not matches:
            _emit_text(_apply_loop_guard(res, f"No calculation rows matched `{kw_disp}`."), out_path=out_path)
            return
        lines = _header(res.get("totalMatches", total_shown))
        for sub in sub_dicts:
            sub_kw = str(sub.get("keyword") or "-")
            sub_matches = sub.get("matches") or []
            sub_total = sub.get("totalMatches", len(sub_matches))
            lines.append(f"\n### {sub_kw} ({sub_total} matches)")
            if not sub_matches:
                lines.append("_(no matches)_")
                continue
            for m in (sub_matches if limit <= 0 else sub_matches[:limit]):
                _append_calc_match(lines, m)
        _emit_text(_apply_loop_guard(res, _prepend_active_guardrail_warnings("\n".join(lines))), out_path=out_path)
        return

    if not matches:
        _emit_text(_apply_loop_guard(res, f"No calculation rows matched `{kw_disp}`."), out_path=out_path)
        return

    lines = _header(res.get("totalMatches", len(matches)))
    for m in (matches if limit <= 0 else matches[:limit]):
        _append_calc_match(lines, m)

    _emit_text(_apply_loop_guard(res, _prepend_active_guardrail_warnings("\n".join(lines))), out_path=out_path)


def register_inspect_part_commands(group: click.Group) -> None:
    for cmd in (cmd_items, cmd_skills, cmd_calc):
        group.add_command(cmd)
