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
    from .formatters import format_bilingual, render_table, strip_pob_escapes

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
        is_uniq = str(item.get("rarity") or "").upper() in ("UNIQUE", "RELIC")
        item_title = format_bilingual(
            item.get("title") or item.get("name"),
            item.get("titleZh") or item.get("nameZh"),
            kind="unique" if is_uniq else "item",
        ) or "-"
        lines = [
            f"### Slot `{slot_name}`: {item_title} (`{item.get('rarity')}`)",
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
            is_uniq = str(it.get("rarity") or "").upper() in ("UNIQUE", "RELIC")
            title = (
                format_bilingual(
                    it.get("title") or it.get("name"),
                    it.get("titleZh") or it.get("nameZh"),
                    kind="unique" if is_uniq else "item",
                )
                or "-"
            )
            base = format_bilingual(it.get("baseName"), it.get("baseNameZh"), kind="base")
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
        is_uniq = str(it.get("rarity") or "").upper() in ("UNIQUE", "RELIC")
        rows.append(
            [
                slot_disp,
                format_bilingual(
                    it.get("title") or it.get("name"),
                    it.get("titleZh") or it.get("nameZh"),
                    kind="unique" if is_uniq else "item",
                )
                or "-",
                format_bilingual(it.get("baseName"), it.get("baseNameZh"), kind="base") or "-",
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
    from .formatters import fmt_num, format_bilingual, render_table

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
                    format_bilingual(gm.get("name") or gm.get("nameSpec"), gm.get("nameZh"), kind="gem") or "-",
                    "Support" if gm.get("support") else "Active",
                    f"{gm.get('level')}/{gm.get('quality')}%",
                    "Yes" if gm.get("enabled", True) else "No",
                    f"{dps_info.get('pct', 0):+.1f}% ({fmt_num(dps_info.get('delta'))})" if dps_info.get("pct") is not None else "-",
                ]
            )
        grp_lbl = format_bilingual(grp.get("displayLabel"), grp.get("displayLabelZh"), kind="gem") or "-"
        body = (
            f"### Skill Group #{grp.get('index')}: {grp_lbl} (Slot: `{grp.get('slot') or '-'}`)\n"
            + render_table(["#", "Gem", "Type", "Lv/Qual", "Enabled", "Tooltip DPS Impact"], rows)
        )
        _emit_text(_apply_loop_guard(res, body), out_path=out_path)
        return

    rows = []
    for g in res.get("groups", []):
        gems_summary = ", ".join(
            f"{format_bilingual(gm.get('name') or gm.get('nameSpec'), gm.get('nameZh'), kind='gem')}{'' if gm.get('enabled', True) else '[OFF]'}"
            for gm in g.get("gems", [])
        )
        rows.append(
            [
                f"#{g['index']}{' *MAIN*' if g.get('isMain') else ''}",
                format_bilingual(g.get("displayLabel") or g.get("label"), g.get("displayLabelZh"), kind="gem") or "-",
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
@click.option("--group", default=None, help="Skill group (1-based index, 'main', or group/gem name substring).")
@click.option(
    "--skill",
    default=None,
    help=(
        "With --group: an active skill of that group (1-based index, 'main' or gem name). "
        "Without --group: a gem or group name looked up in every group, or a group number / 'main'."
    ),
)
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
    group: str | None,
    skill: str | None,
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
    rpc_params: dict[str, Any] = {
        "target": target,
        "keyword": kw_list[0],
        "keywords": kw_list,
        "limit": limit,
        "max_breakdowns": max_breakdowns,
        "no_breakdown": no_breakdown,
    }
    if group is not None:
        rpc_params["group"] = group
    if skill is not None:
        rpc_params["skill"] = skill
    res = invoke_daemon("calc", rpc_params)
    if as_json:
        _apply_loop_guard(res)
        _emit_json(res, compact=compact, out_path=out_path)
        return

    matches = res.get("matches") or []
    kw_disp = ", ".join(kw_list)
    explicit_selector = group is not None or skill is not None

    def _header(total: int) -> list[str]:
        actor_tag = " [Actor: Minion]" if res.get("actor") == "minion" else ""
        out = [f"### Calcs Breakdown for `{kw_disp}`{actor_tag} ({total} matches)"]
        calcs_skill = res.get("calcsSkill")

        def _named(group_no: Any, grp_lbl: Any, active: Any) -> str:
            """`#3 Group / Active skill`; the skill only when it is not the group's own name."""
            grp_lbl, active = strip_pob_escapes(grp_lbl), strip_pob_escapes(active)
            return f"#{group_no} {grp_lbl}" + (f" / {active}" if active and active != grp_lbl else "")

        if isinstance(calcs_skill, dict):
            shown = _named(
                calcs_skill.get("socketGroup"),
                calcs_skill.get("labelZh") or calcs_skill.get("label"),
                calcs_skill.get("skillLabelZh") or calcs_skill.get("skillLabel"),
            )
            out.append(f"- Calcs tab skill: {shown}{actor_tag}")
            if calcs_skill.get("isMainSkill") is False and not explicit_selector:
                main = _named(
                    calcs_skill.get("mainSocketGroup"),
                    calcs_skill.get("mainLabelZh") or calcs_skill.get("mainLabel"),
                    calcs_skill.get("mainSkillLabelZh") or calcs_skill.get("mainSkillLabel"),
                )
                out.append(
                    f"> [WARN] These rows are for {shown}, the skill selected in PoB's Calcs tab, not the "
                    f"main skill ({main}). `pob summary` reports the main skill."
                )
        if res.get("minionHint"):
            out.append(f"- Hint: {res['minionHint']}")
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


_DIAGNOSE_SECTIONS = ("all", "summary", "gaps", "items", "skills", "cards", "upgrades")
_DIAGNOSE_JSON_KEYS: dict[str, tuple[str, ...]] = {
    "summary": (
        "amulet_7th_link",
        "keystones",
        "pantheon",
        "ascendancyNodes",
        "offence",
        "minionOffence",
        "defence",
        "original",
        "dewateredPinnacle",
        "inflationStripped",
        "topWaterSources",
        "notes",
        "notesMeta",
    ),
    "gaps": ("healthGaps", "thresholdProbes", "core_synergies", "buildMechanics", "agentProbe", "socketSummary"),
    "items": ("equippedItems",),
    "skills": ("mainGroup", "amulet_7th_link", "skillGroups"),
    "cards": ("factCards", "advisories", "askGrounding"),
    "upgrades": ("topNodes", "topGems", "socketSummary", "nearbySockets", "rankErrors"),
}


def _slice_diagnose_json(res: dict[str, Any], section: str) -> dict[str, Any]:
    if section == "all" or not isinstance(res, dict):
        return res
    if res.get("mode") == "compare":
        out_cmp = dict(res)
        out_cmp["section"] = section
        if isinstance(res.get("buildA"), dict):
            out_cmp["buildA"] = _slice_diagnose_json(res["buildA"], section)
        if isinstance(res.get("buildB"), dict):
            out_cmp["buildB"] = _slice_diagnose_json(res["buildB"], section)
        return out_cmp
    out: dict[str, Any] = {"section": section}
    for meta_k in ("env", "buildInfo", "mainGroup", "targetSlot", "rev", "sourceHash", "cached", "loopGuardWarning", "updateWarning", "restoreCheck"):
        if meta_k in res:
            out[meta_k] = res[meta_k]
    for k in _DIAGNOSE_JSON_KEYS.get(section, ()):
        if k in res:
            out[k] = res[k]
    return out


def _slice_diagnose_markdown(full_md: str, section: str) -> str:
    if section == "all":
        return full_md
    notes_marker = "\n#### Author Notes (作者备注)"
    notes_block = ""
    body_md = full_md
    if notes_marker in full_md:
        body_md, rest = full_md.split(notes_marker, 1)
        tip_marker = "\n\n_Tip: run `"
        if tip_marker in rest:
            rest, _ = rest.split(tip_marker, 1)
        notes_block = "#### Author Notes (作者备注)" + rest
    parts = body_md.split("\n#### ")
    if len(parts) <= 1 and not notes_block:
        return full_md
    preamble = parts[0]
    if section == "summary":
        header = preamble
    else:
        hdr_lines = [
            ln
            for ln in preamble.splitlines()
            if ln.startswith(("[Env:", "### Build Diagnosis:", "> "))
        ]
        header = "\n".join(hdr_lines) if hdr_lines else preamble

    matched: list[str] = []
    for raw_sec in parts[1:]:
        title = raw_sec.split("\n", 1)[0].strip()
        keep_sec = (
            (section == "summary" and title.startswith("1."))
            or (section == "gaps" and title.startswith(("2.", "2b.", "2c. Build-Specific")))
            or (section == "skills" and title.startswith("2c. All Skill Groups"))
            or (section == "items" and title.startswith("2d."))
            or (section == "cards" and title.startswith("3."))
            or (section == "upgrades" and title.startswith("4."))
        )
        if keep_sec:
            matched.append("#### " + raw_sec)
    if section == "summary" and notes_block:
        matched.append(notes_block)
    if not matched:
        return f"{header}\n\n_(no {section} entries)_"
    return header + "\n\n" + "\n\n".join(matched)


def _slice_diagnose_compare_markdown(res: dict[str, Any], section: str) -> str:
    """One section of a dual-build diagnosis, each build's part under its own heading."""
    from .formatters import format_diagnose_markdown

    head = format_diagnose_markdown(res).split("\n## Build A Diagnosis", 1)[0].rstrip()
    if section != "summary":
        kept = [ln for ln in head.split("\n## ", 1)[0].splitlines() if ln.startswith(("[Env:", "> "))]
        head = "\n".join(kept) if kept else head.split("\n", 1)[0]
    parts = [head]
    for label, key, slot in (
        ("A", "buildA", res.get("target") or "main"),
        ("B", "buildB", res.get("refTarget") or "ref"),
    ):
        build = res.get(key)
        if isinstance(build, dict) and build:
            parts.append(f"\n## Build {label} Diagnosis (`{slot}`)")
            parts.append(_slice_diagnose_markdown(format_diagnose_markdown(build, compact=True), section))
    return "\n".join(parts)


def register_inspect_part_commands(group: click.Group) -> None:
    for cmd in (cmd_items, cmd_skills, cmd_calc):
        group.add_command(cmd)
