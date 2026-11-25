"""Batch, sweep, matrix, ROI ranking, A/B compare, and borrow Click commands for `pob` CLI."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    _SLOT_SLUG_TYPE,
    _emit_json,
    _emit_text,
    _merge_keep_tokens,
    _output_options,
    _prepare_load_source,
    _read_json_input,
    _target_option,
    invoke_daemon,
)
from .formatters import format_mutation_report, keep_rejected_warning, restore_warning


@click.command("batch")
@click.option("--file", "file_path", type=str, help="JSON file containing batch operations list (or '-' for stdin).")
@click.option("--json-ops", "json_ops", help="Inline JSON array of batch operations (or '@path').")
@click.option("--apply", is_flag=True, help="Persist batch changes into build.")
@click.option("--no-undo", "no_undo", is_flag=True, help="Skip undo snapshot overhead when --apply is set.")
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed items in audit/matrix/Pinnacle scenarios.",
)
@click.option(
    "--return",
    "return_spec",
    type=str,
    default=None,
    help="Comma-separated readouts to attach (e.g. 'summary,audit,audit:full,matrix,calc:DPS;Mana,export').",
)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_batch(
    file_path: str | None,
    json_ops: str | None,
    apply: bool,
    no_undo: bool,
    keep: tuple[str, ...],
    return_spec: str | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Execute multiple item/skill/tree/config/build mutations in a single snapshot transaction."""
    parsed = _read_json_input(file_path, json_ops, label="batch ops")
    ops = parsed.get("ops") if isinstance(parsed, dict) else parsed
    if not isinstance(ops, list):
        raise click.UsageError('Batch input must be a JSON list of operation objects (or `{"ops": [...]}`).')

    json_keep = parsed.get("keep") if isinstance(parsed, dict) else None
    eff_keep = _merge_keep_tokens(json_keep, keep)
    ret_parts: list[str] = []
    for tok in (return_spec or "").split(","):
        t = tok.strip()
        if not t:
            continue
        if ":" not in t and any(ch.isspace() for ch in t):
            ret_parts.extend(x for x in t.split() if x)
        else:
            ret_parts.append(t)
    return_sections = ret_parts or None
    params: dict[str, Any] = {
        "target": target,
        "ops": ops,
        "apply": apply,
        "no_undo": no_undo,
        "return_sections": return_sections,
    }
    if eff_keep:
        params["keep"] = eff_keep
    res = invoke_daemon("batch", params, timeout=300.0)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    parts = [format_mutation_report(f"Batch Execution ({len(ops)} steps)", res)]
    returns = res.get("returns")
    if isinstance(returns, dict) and returns:
        from .formatters import format_matrix_markdown

        parts.append("\n#### Attached Returns")
        for sec_name, sec_val in returns.items():
            if sec_name == "matrix" and isinstance(sec_val, dict):
                parts.append(format_matrix_markdown(sec_val, target=target))
            elif sec_name == "audit" and isinstance(sec_val, dict):
                parts.append("- **audit**: attached (`--json` for full payload)")
                r_warn = restore_warning(sec_val)
                if r_warn:
                    parts.append(r_warn)
                stripped = sec_val.get("inflationStripped")
                if isinstance(stripped, dict):
                    kept = stripped.get("kept") or []
                    if kept:
                        parts.append("**Kept (--keep)**: " + "; ".join(kept))
                    for line in keep_rejected_warning(stripped):
                        parts.append(line)
            elif sec_name == "export":
                code_str = (
                    sec_val
                    if isinstance(sec_val, str)
                    else (str(sec_val.get("code") or "") if isinstance(sec_val, dict) else "")
                )
                if code_str:
                    preview = f"{code_str[:64]}..." if len(code_str) > 64 else code_str
                    parts.append(f"- **export**: `{preview}` ({len(code_str)} chars)")
                else:
                    parts.append("- **export**: _(empty)_")
            else:
                parts.append(f"- **{sec_name}**: attached (`--json` for full payload)")
    _emit_text("\n".join(parts), out_path=out_path)


@click.command("sweep")
@click.option("--file", "file_path", type=str, help="JSON file of sweep variants (or '-' for stdin).")
@click.option("--json-variants", "json_variants", help="Inline JSON array of variants (or '@path').")
@click.option("--base-ops", "base_ops_str", help="Optional base ops JSON array (or '@path') applied once before variants.")
@click.option("--calc", "calc_keywords", multiple=True, help="Calcs keyword(s) to extract per variant (repeatable).")
@click.option("--scenario", "scenario", help="Optional baseline scenario (e.g. Pinnacle, Uber, FlasksOff).")
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed items in audit/matrix/Pinnacle scenarios.",
)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_sweep(
    file_path: str | None,
    json_variants: str | None,
    base_ops_str: str | None,
    calc_keywords: tuple[str, ...],
    scenario: str | None,
    keep: tuple[str, ...],
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Evaluate N named build variants in one warm daemon worker with fault isolation and 0-residue rollback."""
    from .formatters import format_sweep_markdown

    parsed = _read_json_input(file_path, json_variants, label="sweep variants")
    base_ops: list[dict[str, Any]] | None = None
    eff_calcs = list(calc_keywords)
    eff_scenario: Any = scenario
    json_keep: Any = None

    if isinstance(parsed, dict):
        variants = parsed.get("variants")
        json_keep = parsed.get("keep")
        if base_ops_str is None and isinstance(parsed.get("base_ops") or parsed.get("baseOps"), list):
            base_ops = parsed.get("base_ops") or parsed.get("baseOps")
        if not eff_calcs and isinstance(parsed.get("calc_keywords") or parsed.get("calcKeywords"), list):
            eff_calcs = [str(x) for x in (parsed.get("calc_keywords") or parsed.get("calcKeywords"))]
        if eff_scenario is None and parsed.get("scenario"):
            raw_sc = parsed["scenario"]
            eff_scenario = raw_sc if isinstance(raw_sc, dict) else str(raw_sc)
    else:
        variants = parsed

    if not isinstance(variants, list):
        raise click.UsageError('Sweep input must be a JSON list of variant objects (or `{"variants": [...]}`).')

    if base_ops_str:
        parsed_base = _read_json_input(None, base_ops_str, label="--base-ops")
        if isinstance(parsed_base, dict) and isinstance(parsed_base.get("ops"), list):
            base_ops = parsed_base["ops"]
        elif isinstance(parsed_base, list):
            base_ops = parsed_base
        else:
            raise click.UsageError("--base-ops must be a JSON list of operation objects.")

    eff_keep = _merge_keep_tokens(json_keep, keep)
    params: dict[str, Any] = {
        "target": target,
        "variants": variants,
        "base_ops": base_ops,
        "calc_keywords": eff_calcs or None,
        "scenario": eff_scenario,
    }
    if eff_keep:
        params["keep"] = eff_keep
    res = invoke_daemon("sweep", params, timeout=300.0)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_sweep_markdown(res, target=target), out_path=out_path)


@click.command("matrix")
@click.argument("scenarios", nargs=-1, required=False)
@click.option(
    "-s",
    "--scenario",
    "opt_scenarios",
    multiple=True,
    help="Scenario spec (e.g. Original, Pinnacle, Uber, FlasksOff, 'Wither15:Pinnacle,MultiplierWitheredStackCount=15').",
)
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed items in Pinnacle/Uber scenarios (repeatable or comma-separated).",
)
@click.option("--full", "full_payload", is_flag=True, help="Include full raw 200-key stats & headers per scenario (default is compact).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_matrix(
    scenarios: tuple[str, ...],
    opt_scenarios: tuple[str, ...],
    keep: tuple[str, ...],
    full_payload: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Run multi-scenario what-if comparison in one atomic 0-residue transaction (default: Original/Pinnacle/Uber/FlasksOff)."""
    from .formatters import format_matrix_markdown

    all_specs = [*scenarios, *opt_scenarios] or None
    params: dict[str, Any] = {"target": target, "scenarios": all_specs, "full": full_payload}
    if keep:
        params["keep"] = list(keep)
    res = invoke_daemon("matrix", params)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_matrix_markdown(res, target=target), out_path=out_path)


@click.command("rank-nodes")
@click.option("--stat", default="CombinedDPS", show_default=True, help="Target stat (CombinedDPS, FullDPS, TotalEHP, Life, etc.).")
@click.option("--max-dist", type=int, default=5, show_default=True, help="Maximum path distance in points.")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top candidates to show.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_rank_nodes(
    stat: str,
    max_dist: int,
    top: int,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Rank unallocated passive tree nodes by ROI per point (`pathPower`) and total gain (`power`)."""
    from .formatters import fmt_num, render_table, strip_pob_escapes

    res = invoke_daemon(
        "rank_nodes",
        {"target": target, "stat": stat, "max_dist": max_dist, "top": top},
        timeout=180.0,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    rows = []
    for idx, r in enumerate(res.get("byPerPoint", []), start=1):
        rows.append(
            [
                idx,
                r.get("id"),
                strip_pob_escapes(r.get("nameZh") or r.get("name")),
                r.get("type"),
                r.get("pathDist"),
                strip_pob_escapes(r.get("pathPowerStr") or fmt_num(r.get("pathPower"), 3)),
                strip_pob_escapes(r.get("powerStr") or fmt_num(r.get("power"), 3)),
            ]
        )
    body = (
        f"### Passive Node ROI Ranking (Stat: `{res.get('stat')}`, Max Dist: `{max_dist}` pts)\n"
        + render_table(["#", "Node ID", "Name", "Type", "Cost", "Per-Point Gain", "Total Gain"], rows)
    )
    if res.get("stat_fallback"):
        body += f"\n_Stat fallback applied: `{stat}` -> `{res['stat_fallback']}`_"
    _emit_text(body, out_path=out_path)


@click.command("rank-gems")
@click.option("--group", default="main", show_default=True, help="Skill group ('main', index, or label).")
@click.option("--slot", "slot_idx", type=int, help="1-based gem slot index to replace.")
@click.option("--add-link", is_flag=True, help="Rank as an additional support link instead of replacing a slot.")
@click.option("--field", default="CombinedDPS", show_default=True, help="DPS field to sort by (CombinedDPS, FullDPS, TotalDPS, etc.).")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top gems to return.")
@click.option(
    "--include-legacy",
    "include_legacy",
    is_flag=True,
    default=False,
    help="Include PoE1 3.28+ drop-disabled legacy Awakened supports in candidate ranking.",
)
@_target_option(allow_slot_alias=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_rank_gems(
    group: str,
    slot_idx: int | None,
    add_link: bool,
    field: str,
    top: int,
    include_legacy: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Rank support/active candidate gems for a skill group by DPS gain."""
    from .formatters import fmt_num, render_table, strip_pob_escapes

    res = invoke_daemon(
        "rank_gems",
        {
            "target": target,
            "group": group,
            "slot_idx": slot_idx,
            "add_link": add_link,
            "field": field,
            "top": top,
            "include_legacy": include_legacy,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    cands = res.get("candidates", [])
    has_caveats = any(gm.get("caveat") for gm in cands)
    rows = []
    for idx, gm in enumerate(cands, start=1):
        pct = gm.get("pct")
        pct_str = f"{float(pct):+.1f}%" if pct is not None else "-"
        row_g: list[Any] = [
            idx,
            strip_pob_escapes(gm.get("nameZh") or gm.get("name")),
            strip_pob_escapes(gm.get("name")),
            fmt_num(gm.get("dps")),
            fmt_num(gm.get("delta")),
            pct_str,
        ]
        if has_caveats:
            row_g.append(str(gm.get("caveat") or "-"))
        rows.append(row_g)
    headers_g = ["#", "Gem (ZH)", "Gem (EN)", "New DPS", "Delta", "Gain %"] + (["Caveat"] if has_caveats else [])
    body = (
        f"### Candidate Gem Ranking — Group #{res.get('groupIndex')} `{strip_pob_escapes(res.get('groupLabel'))}` ({res.get('mode')}, Base `{res.get('dpsField')}`: `{fmt_num(res.get('baseDps'))}`)\n"
        + render_table(headers_g, rows)
    )
    skipped = res.get("filteredArchetypeSwaps") or []
    if skipped:
        body += f"\n_Filtered archetype-changing / invalid supports: {', '.join(skipped[:8])}_"
    skipped_legacy = res.get("filtered_legacy") or []
    if skipped_legacy:
        body += f"\n_Filtered legacy 3.28+ removed Awakened supports (pass --include-legacy to show): {', '.join(skipped_legacy[:8])}_"
    _emit_text(body, out_path=out_path)


@click.command("compare")
@click.argument("ref_source", required=False)
@click.option(
    "--keep",
    "keep",
    multiple=True,
    help="Keep build-guaranteed config/flask/skill/customMods/party items during dewatering (repeatable or comma-separated).",
)
@click.option(
    "--no-auto-keep",
    "no_auto_keep",
    is_flag=True,
    help="Disable automatic mechanism-based config retention during Pinnacle dewatering.",
)
@_target_option(allow_slot_alias=True)
@click.option(
    "--ref-target",
    "ref_target",
    type=_SLOT_SLUG_TYPE,
    default="ref",
    show_default=True,
    help="Reference build slot slug (default: 'ref').",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_compare(
    ref_source: str | None,
    keep: tuple[str, ...],
    no_auto_keep: bool,
    target: str,
    ref_target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Compare target build vs reference build across 3 baselines + tree/skill/equipment diffs."""
    from .formatters import format_compare_markdown

    params: dict[str, Any] = {"target": target, "ref_target": ref_target}
    if keep:
        params["keep"] = list(keep)
    if no_auto_keep:
        params["no_auto_keep"] = True
    if ref_source:
        resolved_src, resolved_xml_code = _prepare_load_source(ref_source)
        if resolved_xml_code is not None:
            params["ref_xml_code"] = resolved_xml_code
        else:
            params["ref_source"] = resolved_src

    res = invoke_daemon("compare", params)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    _emit_text(format_compare_markdown(res, target=target, ref_target=ref_target), out_path=out_path)


@click.command("borrow")
@click.option("--item-slot", "item_slot", help="Comma-separated equipment slot(s) to borrow from reference into target (e.g. 'Weapon 1,Amulet').")
@click.option("--skill-group", "skill_group", help="Skill group ('main', index, or label) to borrow from reference into target.")
@click.option("--apply", is_flag=True, help="Persist borrowed items/skills into target build.")
@_target_option(allow_slot_alias=True)
@click.option(
    "--ref-target",
    "ref_target",
    type=_SLOT_SLUG_TYPE,
    default="ref",
    show_default=True,
    help="Reference build slot slug (default: 'ref').",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_borrow(
    item_slot: str | None,
    skill_group: str | None,
    apply: bool,
    target: str,
    ref_target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Borrow items and/or a skill group from reference slot into target slot and compute exact delta."""
    slots = [x.strip() for x in item_slot.split(",") if x.strip()] if item_slot else []
    res = invoke_daemon(
        "borrow",
        {
            "target": target,
            "ref_target": ref_target,
            "item_slots": slots,
            "skill_group": skill_group,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return

    notices = (res.get("detail") or {}).get("notices") or []
    report = format_mutation_report(f"Borrow from Reference Build (`{ref_target}` -> `{target}`)", res)
    if notices:
        notice_block = "\n#### Transplant Notices\n" + "\n".join(f"- {n}" for n in notices)
        report += notice_block
    _emit_text(report, out_path=out_path)


def register_optimize_commands(group: click.Group) -> None:
    for cmd in (cmd_batch, cmd_sweep, cmd_matrix, cmd_rank_nodes, cmd_rank_gems, cmd_compare, cmd_borrow):
        group.add_command(cmd)
