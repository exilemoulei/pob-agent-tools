"""Read-only build summary, grounding, diagnosis, and inflation audit Click commands for `pob` CLI."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import (
    _SLOT_SLUG_TYPE,
    _emit_json,
    _emit_text,
    _output_options,
    _prepare_load_source,
    _prepend_warning,
    _target_option,
    invoke_daemon,
)
from .cli_inspect_parts import _apply_loop_guard, register_inspect_part_commands

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
    ),
    "gaps": ("healthGaps", "core_synergies", "buildMechanics", "agentProbe"),
    "items": ("equippedItems",),
    "skills": ("mainGroup", "amulet_7th_link", "skillGroups"),
    "cards": ("factCards", "advisories", "askGrounding"),
    "upgrades": ("topNodes", "topGems", "rankErrors"),
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
    parts = full_md.split("\n#### ")
    if len(parts) <= 1:
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
    if not matched:
        return f"{header}\n\n_(no {section} entries)_"
    return header + "\n\n" + "\n\n".join(matched)


def _slice_diagnose_compare_markdown(res: dict[str, Any], section: str) -> str:
    """One section of a dual-build diagnosis, each build's part under its own heading.

    Slicing the joined report would file the `## Build B Diagnosis` heading under Build A's last
    section and print B's numbers as A's.
    """
    from .formatters import format_diagnose_markdown

    head = format_diagnose_markdown(res).split("\n## Build A Diagnosis", 1)[0].rstrip()
    if section != "summary":
        # The comparison block goes; the env line and the update warning under it stay.
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


@click.command("summary")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_summary(target: str, as_json: bool, compact: bool, out_path: str | None) -> None:
    """Display comprehensive build identity, offence, minion offence, defence, and warnings."""
    from .formatters import fmt_num, format_recency_stamp, render_table, strip_pob_escapes

    res = invoke_daemon("summary", {"target": target})
    if as_json:
        _apply_loop_guard(res)
        _emit_json(res, compact=compact, out_path=out_path)
        return

    info = res.get("buildInfo", {})
    pts = info.get("points", {})
    mg = res.get("mainGroup") or {}
    gems_str = ", ".join(
        f"{strip_pob_escapes(g.get('nameZh') or g.get('name'))} ({g.get('level')}/{g.get('quality')}%)"
        for g in mg.get("gems", [])
        if g.get("enabled", True)
    )

    lines = [
        format_recency_stamp(res, slot=target),
        f"### Build Summary (`{target}`): Lv.{info.get('level')} {info.get('classNameZh') or info.get('className')} / {info.get('ascendClassNameZh') or info.get('ascendClassName')}",
        f"- **Points**: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` | **Ascendancy**: `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}` | **Tree**: `{info.get('treeVersion')}`",
        f"- **Main Skill Group**: `#{mg.get('index', '-')} {strip_pob_escapes(mg.get('displayLabelZh') or mg.get('displayLabel') or '-')}` (`{mg.get('slot') or 'Unassigned'}`)",
        f"- **Linked Gems**: {gems_str or '-'}",
    ]

    off = res.get("offence", {})
    crit_mult = off.get("CritMultiplier")
    crit_mult_str = f"{float(crit_mult) * 100:.0f}% (x{float(crit_mult):.2f})" if crit_mult is not None else "-"
    dot_dps = off.get("TotalDotDPS") or off.get("TotalDot")
    off_rows = [
        ["Full DPS", fmt_num(off.get("FullDPS")), "Combined DPS", fmt_num(off.get("CombinedDPS"))],
        ["Hit DPS (TotalDPS)", fmt_num(off.get("TotalDPS")), "DoT DPS (TotalDotDPS)", fmt_num(dot_dps)],
        ["Average Damage", fmt_num(off.get("AverageDamage")), "Attack/Cast Rate", f"{fmt_num(off.get('Speed'), 2)}/s"],
        ["Crit Chance", f"{fmt_num(off.get('CritChance'), 1)}%", "Crit Multiplier", crit_mult_str],
        ["Hit Chance", f"{fmt_num(off.get('HitChance'), 1)}%", "Mana Cost", fmt_num(off.get("ManaCost"))],
    ]
    lines.append("\n#### Offence")
    lines.append(render_table(["Metric", "Value", "Metric ", "Value "], off_rows))

    m_off = res.get("minionOffence")
    if m_off:
        lines.append("\n#### Minion Offence")
        m_rows = [
            ["Minion Combined DPS", fmt_num(m_off.get("MinionCombinedDPS")), "Minion Hit DPS", fmt_num(m_off.get("MinionTotalDPS"))],
            ["Minion Avg Damage", fmt_num(m_off.get("MinionAverageDamage")), "Minion Speed", f"{fmt_num(m_off.get('MinionSpeed'), 2)}/s"],
            ["Minion Crit Chance", f"{fmt_num(m_off.get('MinionCritChance'), 1)}%", "Minion Life", fmt_num(m_off.get("MinionLife"))],
        ]
        for s_row in m_off.get("sidebarRows", []):
            m_rows.append([s_row["label"], s_row["value"], s_row["stat"], "-"])
        lines.append(render_table(["Minion Metric", "Value", "Detail", "Extra"], m_rows))

    dfn = res.get("defence", {})
    def_rows = [
        ["Effective HP (TotalEHP)", fmt_num(dfn.get("TotalEHP")), "Maximum Life / Unres", f"{fmt_num(dfn.get('Life'))} / {fmt_num(dfn.get('LifeUnreserved'))} ({fmt_num(dfn.get('LifeUnreservedPercent'), 0)}%)"],
        ["Energy Shield / Ward", f"{fmt_num(dfn.get('EnergyShield'))} / {fmt_num(dfn.get('Ward'))}", "Maximum Mana / Unres", f"{fmt_num(dfn.get('Mana'))} / {fmt_num(dfn.get('ManaUnreserved'))} ({fmt_num(dfn.get('ManaUnreservedPercent'), 0)}%)"],
        ["Phys / Chaos Max Hit", f"{fmt_num(dfn.get('PhysicalMaximumHitTaken'))} / {fmt_num(dfn.get('ChaosMaximumHitTaken'))}", "Fire/Cold/Light Max Hit", f"{fmt_num(dfn.get('FireMaximumHitTaken'))} / {fmt_num(dfn.get('ColdMaximumHitTaken'))} / {fmt_num(dfn.get('LightningMaximumHitTaken'))}"],
        ["Fire / Cold Resist", f"{fmt_num(dfn.get('FireResist'), 0)}% (+{fmt_num(dfn.get('FireResistOverCap'), 0)}%) / {fmt_num(dfn.get('ColdResist'), 0)}% (+{fmt_num(dfn.get('ColdResistOverCap'), 0)}%)", "Light / Chaos Resist", f"{fmt_num(dfn.get('LightningResist'), 0)}% (+{fmt_num(dfn.get('LightningResistOverCap'), 0)}%) / {fmt_num(dfn.get('ChaosResist'), 0)}% (+{fmt_num(dfn.get('ChaosResistOverCap'), 0)}%)"],
        ["Armour / Evade", f"{fmt_num(dfn.get('Armour'))} / {fmt_num(dfn.get('Evasion'))}", "Block / Spell Block / Supp", f"{fmt_num(dfn.get('EffectiveBlockChance'), 0)}% / {fmt_num(dfn.get('EffectiveSpellBlockChance'), 0)}% / {fmt_num(dfn.get('EffectiveSpellSuppressionChance'), 0)}%"],
        ["Net Life / Mana Regen", f"{fmt_num(dfn.get('NetLifeRegen'), 1)}/s / {fmt_num(dfn.get('NetManaRegen'), 1)}/s", "Str / Dex / Int", f"{fmt_num(dfn.get('Str'))} / {fmt_num(dfn.get('Dex'))} / {fmt_num(dfn.get('Int'))}"],
    ]
    lines.append("\n#### Defence & Sustain")
    lines.append(render_table(["Metric", "Value", "Metric ", "Value "], def_rows))

    warnings = res.get("warnings", [])
    if warnings:
        lines.append("\n#### Warnings")
        for w in warnings:
            lines.append(f"- {w}")

    _emit_text(_apply_loop_guard(res, _prepend_warning("\n".join(lines), res)), out_path=out_path)


@click.command("diagnose")
@click.argument("source", required=False)
@click.option(
    "-c",
    "--compare",
    "compare_source",
    default=None,
    help="Second build source (@file.xml/@file.code/URL/code) for one-shot 3-baseline A/B comparison + Pinnacle audit of both builds.",
)
@click.option(
    "-q",
    "--ask",
    "ask_text",
    default=None,
    help="Player question text to ground (<15ms 3.30 fact cards + PoE2/patch trap alerts) and merge into the diagnosis.",
)
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
@click.option("--top", type=int, default=5, show_default=True, help="Top N passive nodes & candidate support gems.")
@click.option(
    "--rank/--no-rank",
    "rank",
    default=None,
    help="Run or skip passive node & support gem ROI rankings (skipped by default for <1s diagnosis; enabled by --rank, --section upgrades, or explicit --top).",
)
@click.option(
    "--section",
    "section",
    type=click.Choice(list(_DIAGNOSE_SECTIONS), case_sensitive=False),
    default="all",
    show_default=True,
    help="Render a specific diagnosis section (all, summary, gaps, items, skills, cards, upgrades).",
)
@_target_option(allow_slot_alias=True)
@click.option(
    "--ref-target",
    "ref_target",
    type=_SLOT_SLUG_TYPE,
    default=None,
    help="Reference build slot slug for --compare (defaults to 'ref' when target=='main', else '<target>_ref').",
)
@click.option("--char", "char_name", default=None, help="Character name when SOURCE is an official profile URL.")
@click.option("--league", "league", default=None, help="Optional league filter for official profile URL import.")
@click.option(
    "--realm",
    type=click.Choice(["pc", "xbox", "sony", "cn"], case_sensitive=False),
    default=None,
    help="Official profile realm (`pc`, `xbox`, `sony`, or `cn` for poe.game.qq.com).",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_diagnose(
    ctx: click.Context,
    source: str | None,
    compare_source: str | None,
    ask_text: str | None,
    keep: tuple[str, ...],
    no_auto_keep: bool,
    top: int,
    rank: bool | None,
    section: str,
    target: str,
    ref_target: str | None,
    char_name: str | None,
    league: str | None,
    realm: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """One-shot comprehensive build diagnosis (and optional dual-build --compare & --ask question grounding)."""
    from click.core import ParameterSource

    from .daemon.read_cache import compute_source_hash
    from .formatters import format_diagnose_markdown

    sec_norm = (section or "all").strip().lower()
    top_explicit = ctx.get_parameter_source("top") == ParameterSource.COMMANDLINE
    if rank is True:
        eff_no_rank = False
    elif rank is False:
        eff_no_rank = True
    elif sec_norm == "upgrades" or top_explicit:
        eff_no_rank = False
    else:
        eff_no_rank = True

    params: dict[str, Any] = {
        "target": target,
        "top": top,
        "no_rank": eff_no_rank,
    }
    if sec_norm != "all":
        params["section"] = sec_norm
    if char_name and char_name.strip():
        params["char_name"] = char_name.strip()
    if league and league.strip():
        params["league"] = league.strip()
    if realm and realm.strip():
        params["realm"] = realm.strip().lower()
    if keep:
        params["keep"] = list(keep)
    if no_auto_keep:
        params["no_auto_keep"] = True
    if ask_text and ask_text.strip():
        params["ask"] = ask_text.strip()
    if source:
        resolved_src, resolved_xml_code = _prepare_load_source(source)
        if resolved_xml_code is not None:
            params["xml_code"] = resolved_xml_code
        else:
            params["source"] = resolved_src
        src_hash = compute_source_hash(params)
        if src_hash:
            params["source_hash"] = src_hash
    if compare_source:
        eff_ref = ref_target or ("ref" if target == "main" else f"{target}_ref")
        params["ref_target"] = eff_ref
        cmp_src, cmp_xml_code = _prepare_load_source(compare_source)
        if cmp_xml_code is not None:
            params["compare_xml_code"] = cmp_xml_code
        else:
            params["compare_source"] = cmp_src
        cmp_hash = compute_source_hash({"source": cmp_src, "xml_code": cmp_xml_code})
        if cmp_hash:
            params["compare_source_hash"] = cmp_hash

    res = invoke_daemon("diagnose", params, timeout=240.0 if compare_source else 180.0)
    if as_json:
        _apply_loop_guard(res)
        _emit_json(_slice_diagnose_json(res, sec_norm), compact=compact, out_path=out_path)
        return
    if sec_norm != "all" and res.get("mode") == "compare" and isinstance(res.get("comparison"), dict):
        md = _slice_diagnose_compare_markdown(res, sec_norm)
    else:
        md = _slice_diagnose_markdown(format_diagnose_markdown(res), sec_norm)
    extra_notes = [str(n) for n in (res.get("importNotes") or []) if n]
    extra_warns = [str(w) for w in (res.get("translateWarnings") or []) if w]
    if (extra_notes or extra_warns) and sec_norm in ("all", "summary"):
        tail = ["", "#### Import Notes"]
        tail.extend(f"- {n}" for n in extra_notes)
        tail.extend(f"- {w}" for w in extra_warns[:20])
        if len(extra_warns) > 20:
            tail.append(f"- ... ({len(extra_warns) - 20} more untranslated CN mods)")
        md = md + "\n" + "\n".join(tail)
    _emit_text(_apply_loop_guard(res, md), out_path=out_path)


@click.command("ground")
@click.argument("text", nargs=-1, required=True)
@click.option("--limit", type=int, default=8, show_default=True, help="Maximum live gem/unique fact cards to attach.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_ground(
    text: tuple[str, ...],
    limit: int,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Fast (<15ms, daemon-free) entity & PoE2/legacy-patch trap grounder for arbitrary player prompts."""
    from .formatters import format_ground_markdown
    from .game_data import ground_text_entities

    joined = " ".join(t for t in text if t).strip()
    if not joined:
        raise click.UsageError("Provide non-empty prompt text to ground.")
    res = ground_text_entities(joined, limit=limit)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
        return
    _emit_text(format_ground_markdown(res), out_path=out_path)


@click.command("audit")
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
@click.option(
    "--no-attribution",
    "no_attribution",
    is_flag=True,
    help="Skip per-item water attribution recalculations.",
)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_audit(
    keep: tuple[str, ...],
    no_auto_keep: bool,
    no_attribution: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Run build health check and non-destructive 4-part PoB inflation ('water') audit."""
    from .formatters import (
        fmt_delta,
        fmt_num,
        fmt_signed_pct,
        format_recency_stamp,
        keep_rejected_warning,
        render_table,
        restore_warning,
    )

    params: dict[str, Any] = {"target": target, "attribution": not no_attribution}
    if keep:
        params["keep"] = list(keep)
    if no_auto_keep:
        params["no_auto_keep"] = True
    res = invoke_daemon("audit", params)
    if as_json:
        _apply_loop_guard(res)
        _emit_json(res, compact=compact, out_path=out_path)
        return

    lines = [format_recency_stamp(res, slot=target), "### Build Health & Inflation ('Water') Audit"]
    r_warn = restore_warning(res)
    if r_warn:
        lines.append(r_warn)
    issues = res.get("healthIssues", [])
    if issues:
        lines.append("\n#### 1. Health & Hard Gaps")
        i_rows = [[x["severity"], x["category"], x["message"]] for x in issues]
        lines.append(render_table(["Severity", "Category", "Finding"], i_rows))
    else:
        lines.append("\n#### 1. Health & Hard Gaps\n- No critical resistance, attribute, or sustain gaps detected.")

    stripped = res.get("inflationStripped", {})
    lines.append("\n#### 2. Inflation ('Water') Stripped for Pinnacle Boss Baseline")
    s_rows = []
    for dim in ("config", "flasks", "skills", "party"):
        items = stripped.get(dim) or []
        s_rows.append([dim.capitalize(), len(items), "; ".join(items) if items else "_(Clean)_"])
    lines.append(render_table(["Dimension", "Count", "Stripped Items"], s_rows))
    auto_kept = stripped.get("autoKept") or []
    if auto_kept:
        lines.append("\n**Auto-Kept (Build-Guaranteed)**: " + "; ".join(str(x) for x in auto_kept))
    kept = stripped.get("kept") or []
    if kept:
        lines.append("\n**Kept (--keep)**: " + "; ".join(kept))
    rej_lines = keep_rejected_warning(stripped)
    if rej_lines:
        lines.append("\n" + "\n".join(rej_lines))

    attr = res.get("waterAttribution") or []
    if attr:
        dps_key = (res.get("original") or {}).get("primaryDpsKey") or "DPS"
        lines.append(
            "\n#### 2b. Per-Item Water Attribution (PoB re-calc, restoring ONE item on the de-watered baseline)"
        )
        a_rows = []
        for r in attr:
            if r.get("error"):
                a_rows.append([r.get("dimension") or "-", r.get("label") or "-", "error", r["error"], "", ""])
                continue
            a_rows.append(
                [
                    r.get("dimension") or "-",
                    r.get("label") or "-",
                    fmt_signed_pct(r.get("dpsDelta"), r.get("dpsPct")),
                    fmt_signed_pct(r.get("totalEhpDelta"), r.get("totalEhpPct")),
                    fmt_signed_pct(r.get("physMaxHitDelta"), r.get("physMaxHitPct")),
                    fmt_signed_pct(r.get("eleMaxHitDelta"), r.get("eleMaxHitPct")),
                ]
            )
        lines.append(
            render_table(
                ["Dim", "Item", f"Δ{dps_key}", "ΔTotalEHP", "ΔPhys MaxHit", "ΔEle MaxHit"],
                a_rows,
            )
        )
        lines.append(
            "_Items your build guarantees in a real boss fight (ascendancy/keystone conditions, "
            "self-applied ailments, always-on charges) can be re-run with `--keep <var>`._"
        )

    orig = res.get("original", {})
    dew = res.get("dewateredPinnacle", {})
    lines.append("\n#### 3. Original Panel vs True Pinnacle Boss Baseline (De-watered)")
    comp_rows = [
        [
            f"Primary DPS ({orig.get('primaryDpsKey')})",
            fmt_num(orig.get("primaryDps")),
            fmt_num(dew.get("primaryDps")),
            fmt_delta(orig.get("primaryDps"), dew.get("primaryDps")),
        ],
        [
            "Effective HP (TotalEHP)",
            fmt_num(orig.get("totalEhp")),
            fmt_num(dew.get("totalEhp")),
            fmt_delta(orig.get("totalEhp"), dew.get("totalEhp")),
        ],
        [
            "Phys Max Hit",
            fmt_num(orig.get("physMaxHit")),
            fmt_num(dew.get("physMaxHit")),
            fmt_delta(orig.get("physMaxHit"), dew.get("physMaxHit")),
        ],
        [
            "Elemental Max Hit (Min)",
            fmt_num(orig.get("eleMaxHit")),
            fmt_num(dew.get("eleMaxHit")),
            fmt_delta(orig.get("eleMaxHit"), dew.get("eleMaxHit")),
        ],
        [
            "Chaos Max Hit",
            fmt_num(orig.get("chaosMaxHit")),
            fmt_num(dew.get("chaosMaxHit")),
            fmt_delta(orig.get("chaosMaxHit"), dew.get("chaosMaxHit")),
        ],
    ]
    lines.append(render_table(["Metric", "Original Panel", "Dewatered Pinnacle", "Water Delta"], comp_rows))
    _emit_text(_apply_loop_guard(res, "\n".join(lines)), out_path=out_path)


def register_inspect_commands(group: click.Group) -> None:
    for cmd in (cmd_summary, cmd_diagnose, cmd_ground, cmd_audit):
        group.add_command(cmd)
    register_inspect_part_commands(group)
