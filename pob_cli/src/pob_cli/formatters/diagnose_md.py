"""Markdown formatters for single-build `pob diagnose` reports."""

from __future__ import annotations

import math
from typing import Any

from .diagnose_compare_md import (
    _fmt_ask_grounding_line,
    _fmt_diagnose_fact_cards_and_upgrades,
    format_diagnose_compare_markdown,
)
from .primitives import (
    _parse_numeric_or_none,
    fmt_delta,
    fmt_num,
    fmt_signed_pct,
    format_recency_stamp,
    keep_rejected_warning,
    render_table,
    render_update_warning,
    restore_warning,
    strip_pob_escapes,
)


def _fmt_diagnose_header(res: dict[str, Any], *, compact: bool) -> list[str]:
    env = res.get("env") or {}
    env_banner = format_recency_stamp(
        res,
        slot=env.get("slot") or res.get("targetSlot") or "main",
        patch=env.get("patch") or "3.30",
        pob_version=env.get("pobVersion"),
        data_version=env.get("dataVersion"),
        tree_version=env.get("treeVersion"),
        rev=env.get("rev") if env.get("rev") is not None else res.get("rev"),
    )
    lines: list[str] = [env_banner]

    u_warn = render_update_warning(res.get("updateWarning"))
    if u_warn:
        lines.append(u_warn.strip())
    r_warn = restore_warning(res.get("restoreCheck"))
    if r_warn:
        lines.append(r_warn)

    ask_line = _fmt_ask_grounding_line(res)
    if ask_line:
        lines.append(ask_line)

    info = res.get("buildInfo") or {}
    pts = info.get("points") or {}
    mg = res.get("mainGroup") or {}
    cls_str = strip_pob_escapes(info.get("classNameZh") or info.get("className") or "-")
    asc_str = strip_pob_escapes(info.get("ascendClassNameZh") or info.get("ascendClassName") or "-")
    mg_lbl = strip_pob_escapes(mg.get("displayLabelZh") or mg.get("displayLabel") or "-")
    gems_str = ", ".join(
        f"{strip_pob_escapes(g.get('nameZh') or g.get('name') or '')} ({g.get('level')}/{g.get('quality')}%)"
        for g in (mg.get("gems") or [])
        if g.get("enabled", True)
    )
    lines.append(f"\n### Build Diagnosis: Lv.{info.get('level') or '-'} {cls_str} / {asc_str}")
    lines.append(
        f"- **Points**: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` (Asc `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}`) | "
        f"**Main Skill**: `#{mg.get('index', '-')} {mg_lbl}` (`{mg.get('slot') or '-'}`)"
    )
    amulet_7l = res.get("amulet_7th_link") or res.get("amulet7thLink") or []
    amulet_7l_str = ", ".join(strip_pob_escapes(str(x)) for x in amulet_7l if str(x).strip())
    amulet_7l_suffix = f" + [Amulet 7L: {amulet_7l_str}]" if amulet_7l_str else ""
    if gems_str:
        lines.append(f"- **Linked Gems**: {gems_str}{amulet_7l_suffix}")
    elif amulet_7l_str:
        lines.append(f"- **Linked Gems**: [Amulet 7L: {amulet_7l_str}]")
    ks_list = res.get("keystones") or []
    if ks_list:
        lines.append(f"- **Keystones**: {', '.join(ks_list[:10])}")
    panth = res.get("pantheon") or {}
    if isinstance(panth, dict) and (panth.get("major") or panth.get("minor")):
        p_parts: list[str] = []
        if panth.get("major"):
            mh = f" ({panth['majorHint']})" if panth.get("majorHint") else ""
            p_parts.append(f"Major `{panth['major']}`{mh}")
        if panth.get("minor"):
            nh = f" ({panth['minorHint']})" if panth.get("minorHint") else ""
            p_parts.append(f"Minor `{panth['minor']}`{nh}")
        lines.append(f"- **Pantheon**: {' | '.join(p_parts)}")

    asc_nodes = res.get("ascendancyNodes") or []
    if asc_nodes:
        lines.append("- **Allocated Ascendancy & Bloodline Nodes**:")
        for an in asc_nodes[: (10 if compact else 16)]:
            an_zh = strip_pob_escapes(str(an.get("nameZh") or "")).strip()
            an_en = strip_pob_escapes(str(an.get("name") or "")).strip()
            lbl = f"{an_zh} ({an_en})" if (an_zh and an_en and an_zh != an_en) else (an_zh or an_en or str(an.get("id")))
            asc_tag = f" [{an['asc']}]" if an.get("asc") else ""
            st_list = an.get("stats") or an.get("statsZh") or []
            st_txt = "; ".join(str(s) for s in st_list[:4])
            lines.append(f"  - `{an.get('id')}` **{lbl}**{asc_tag}: {st_txt}")
    return lines


def _fmt_diagnose_baseline(res: dict[str, Any], *, compact: bool) -> list[str]:
    lines: list[str] = []
    orig = res.get("original") or {}
    dew = res.get("dewateredPinnacle") or {}
    p_key = orig.get("primaryDpsKey") or "CombinedDPS"
    lines.append("\n#### 1. Panel vs True Pinnacle Boss Baseline (Dewatered)")
    comp_rows = [
        [f"Primary DPS ({p_key})", fmt_num(orig.get("primaryDps")), fmt_num(dew.get("primaryDps")), fmt_delta(orig.get("primaryDps"), dew.get("primaryDps"))],
        ["Effective HP (TotalEHP)", fmt_num(orig.get("totalEhp")), fmt_num(dew.get("totalEhp")), fmt_delta(orig.get("totalEhp"), dew.get("totalEhp"))],
        ["Phys Max Hit", fmt_num(orig.get("physMaxHit")), fmt_num(dew.get("physMaxHit")), fmt_delta(orig.get("physMaxHit"), dew.get("physMaxHit"))],
        ["Elemental Max Hit (Min)", fmt_num(orig.get("eleMaxHit")), fmt_num(dew.get("eleMaxHit")), fmt_delta(orig.get("eleMaxHit"), dew.get("eleMaxHit"))],
        ["Chaos Max Hit", fmt_num(orig.get("chaosMaxHit")), fmt_num(dew.get("chaosMaxHit")), fmt_delta(orig.get("chaosMaxHit"), dew.get("chaosMaxHit"))],
    ]
    lines.append(render_table(["Metric", "Original Panel", "Dewatered Pinnacle", "Water Delta"], comp_rows))

    dfn = res.get("defence") or {}
    off = res.get("offence") or {}
    if dfn:
        life_n = _parse_numeric_or_none(dfn.get("Life"))
        cmh_n = _parse_numeric_or_none(orig.get("chaosMaxHit"))
        is_ci_disp = life_n is not None and life_n <= 1.0 and cmh_n is not None and math.isinf(cmh_n)
        chaos_res_disp = "100(CI)" if is_ci_disp else fmt_num(dfn.get("ChaosResist"), 0)
        life_unres_p = (
            f" (Unres `{fmt_num(dfn.get('LifeUnreserved'))}`/{fmt_num(dfn.get('LifeUnreservedPercent'), 0)}%)"
            if dfn.get("LifeUnreservedPercent") is not None and (_parse_numeric_or_none(dfn.get("LifeUnreservedPercent")) or 100.0) < 99.0
            else ""
        )
        lines.append(
            f"- **Core Pool & Res**: Life `{fmt_num(dfn.get('Life'))}`{life_unres_p} | ES `{fmt_num(dfn.get('EnergyShield'))}` | "
            f"Mana `{fmt_num(dfn.get('Mana'))}` (Unres `{fmt_num(dfn.get('ManaUnreserved'))}`) | "
            f"Res F/C/L/Ch `{fmt_num(dfn.get('FireResist'), 0)}/{fmt_num(dfn.get('ColdResist'), 0)}/{fmt_num(dfn.get('LightningResist'), 0)}/{chaos_res_disp}%` | "
            f"Supp `{fmt_num(dfn.get('EffectiveSpellSuppressionChance'), 0)}%` | "
            f"Block `{fmt_num(dfn.get('EffectiveBlockChance'), 0)}/{fmt_num(dfn.get('EffectiveSpellBlockChance'), 0)}%` | "
            f"Regen L/M `{fmt_num(dfn.get('NetLifeRegen'), 1)}/{fmt_num(dfn.get('NetManaRegen'), 1)}/s` | "
            f"ManaCost `{fmt_num(off.get('ManaCost'))}`"
        )

    top_water = res.get("topWaterSources") or []
    if top_water:
        w_rows = [
            [
                r.get("dimension") or "-",
                r.get("label") or "-",
                fmt_signed_pct(r.get("dpsDelta"), r.get("dpsPct")),
                fmt_signed_pct(r.get("totalEhpDelta"), r.get("totalEhpPct")),
            ]
            for r in top_water[: (4 if compact else 6)]
        ]
        lines.append("\n**Top Water Sources Stripped**:")
        lines.append(render_table(["Dim", "Item", f"Δ{p_key}", "ΔTotalEHP"], w_rows))

    stripped = res.get("inflationStripped") or {}
    kept = stripped.get("kept") or []
    if kept:
        lines.append("\n**Kept (--keep)**: " + "; ".join(kept))
    rej = keep_rejected_warning(stripped)
    if rej:
        lines.extend(rej)
    return lines


def _fmt_diagnose_gaps_and_mechanics(res: dict[str, Any], *, compact: bool) -> list[str]:
    lines: list[str] = []
    gaps = res.get("healthGaps") or []
    lines.append("\n#### 2. Hard Defensive & Mechanic Gaps")
    if gaps:
        if compact:
            for g in gaps:
                lines.append(f"- **[{g.get('severity') or '-'}/{g.get('category') or '-'}]** {g.get('message') or '-'}")
        else:
            g_rows = [[g.get("severity") or "-", g.get("category") or "-", g.get("message") or "-"] for g in gaps]
            lines.append(render_table(["Severity", "Category", "Finding"], g_rows))
    else:
        lines.append("- No critical resistance, attribute, accuracy, mana, or point gaps detected.")

    core_syn = (
        res.get("core_synergies")
        or res.get("coreSynergies")
        or (res.get("buildMechanics") or {}).get("coreSynergies")
        or []
    )
    if core_syn:
        lines.append("\n#### 2b. Core Build Synergies & Do-Not-Brick Advisories")
        for syn in core_syn:
            syn_txt = strip_pob_escapes(str(syn)).strip()
            if syn_txt:
                lines.append(f"- {syn_txt}")

    bm = res.get("buildMechanics") or {}
    has_bm_section = bool(
        isinstance(bm.get("swordOfAges"), dict)
        or (bm.get("triggers") or [])
        or (isinstance(bm.get("tinctureSetup"), dict) and bm["tinctureSetup"].get("note"))
        or isinstance(bm.get("resistanceInversion"), dict)
    )
    if has_bm_section:
        lines.append("\n#### 2c. Build-Specific Mechanics & PoB Modeling")
        soa = bm.get("swordOfAges")
        if isinstance(soa, dict):
            tm = soa.get("temporalMirageCooldown") or {}
            src_str = ", ".join(
                f"{s.get('source')}: +{fmt_num(s.get('value'), 0)}%" for s in (soa.get("cdrSources") or [])
            ) or "none (0%)"
            nb = tm.get("nextBreakpoint") or {}
            next_str = (
                f"Next tick breakpoint: **`{nb.get('requiredCdrPct')}%` CDR (`+{nb.get('cdrNeededPct')}%` more)** -> "
                f"`{nb.get('targetTicks')}` ticks = **`{nb.get('effectiveCdSec')}s` (`{nb.get('effectiveCdMs')}ms`, max `{nb.get('maxTriggersPerSec')}/s`)**"
                if nb
                else "at minimum 1-tick cap"
            )
            lines.append(
                f"- **The Sword of Ages (`Temporal Mirage`, base CD `0.50s`, PoE1 server tick `0.033s = 33ms`)**: "
                f"Global CDR = **`{fmt_num(soa.get('globalCdrPct'), 0)}%`** ({src_str}) -> "
                f"raw `{tm.get('rawCdSec')}s` -> **`{tm.get('ticks')}` ticks = `{tm.get('effectiveCdSec')}s` (`{tm.get('effectiveCdMs')}ms`, max `{tm.get('maxTriggersPerSec')}/s`)**. "
                f"{next_str}."
            )
            for adv_ln in soa.get("advisories") or []:
                lines.append(f"  - {adv_ln}")
        for trig in bm.get("triggers") or []:
            if not isinstance(trig, dict):
                continue
            if trig.get("kind") == "fixed_interval_channelling":
                lines.append(f"- **Trigger ({trig.get('name')}, Group #{trig.get('groupIndex')})**: {trig.get('note')}")
            else:
                cd = trig.get("cooldown") or {}
                nb = cd.get("nextBreakpoint") or {}
                gem_cdr = trig.get("gemCdrPct") or 0
                cdr_detail = f"{fmt_num(trig.get('totalCdrPct'), 0)}% CDR" + (
                    f" (global {fmt_num(trig.get('globalCdrPct'), 0)}% + gem {gem_cdr}%)" if gem_cdr else ""
                )
                aps_part = ""
                if cd.get("aps") is not None:
                    flag = " **[OVERSPEED!]**" if cd.get("overspeed") else " [OK]"
                    aps_part = f" | Trigger APS `{cd['aps']:.2f}/s` vs cap `{cd.get('effectiveCapPerSec')}/s`{flag}"
                next_part = (
                    f" | Next: `{nb.get('requiredCdrPct')}%` CDR (`+{nb.get('cdrNeededPct')}%`) -> `{nb.get('targetTicks')}t`=`{nb.get('effectiveCdMs')}ms` (`{nb.get('effectiveCapPerSec')}/s`)"
                    if nb
                    else ""
                )
                lines.append(
                    f"- **Trigger (`{trig.get('name')}`, base `{trig.get('baseCooldownSec')}s`, `0.033s` tick)**: "
                    f"**{cdr_detail}** -> **`{cd.get('ticks')}t` = `{cd.get('effectiveCdSec')}s` (`{cd.get('effectiveCdMs')}ms`, cap `{cd.get('effectiveCapPerSec')}/s`)**{aps_part}{next_part}"
                )
        tinc = bm.get("tinctureSetup")
        if isinstance(tinc, dict) and tinc.get("note"):
            lines.append(f"- **Tincture / Bloodsoaked Blade Mechanics**: {tinc['note']}")
        inv = bm.get("resistanceInversion")
        if isinstance(inv, dict):
            lines.append(
                f"- **Resistance Inversion**: {', '.join((inv.get('sources') or [])[:3])} — {inv.get('note') or ''}"
            )
    return lines


def _fmt_diagnose_skills_and_gear(res: dict[str, Any], *, compact: bool) -> list[str]:
    lines: list[str] = []
    all_groups = res.get("skillGroups") or []
    if all_groups:
        lines.append("\n#### 2c. All Skill Groups Overview")
        sg_rows: list[list[Any]] = []
        for g in all_groups:
            g_tag = f"#{g.get('index')}{'*' if g.get('isMain') else ''}"
            if not g.get("enabled", True):
                g_tag += "[OFF]"
            if g.get("includeInFullDPS"):
                g_tag += "[Full]"
            g_gems_parts = []
            for gm in g.get("gems") or []:
                nm = strip_pob_escapes(str(gm.get("nameZh") or gm.get("name") or "-"))
                lv = gm.get("level")
                ql = gm.get("quality")
                meta = f"({lv}/{ql}%)" if lv is not None else ""
                if gm.get("count") and int(gm["count"]) > 1:
                    meta += f"x{gm['count']}"
                if not gm.get("enabled", True):
                    meta += "[OFF]"
                if gm.get("fromItem"):
                    meta += "[Item]"
                g_gems_parts.append(f"{nm}{meta}")
            sg_rows.append([g_tag, g.get("slot") or "-", ", ".join(g_gems_parts) or "-"])
        if compact:
            for r in sg_rows:
                lines.append(f"- `{r[0]}` (`{r[1]}`): {r[2]}")
        else:
            lines.append(render_table(["Group", "Slot", "Gems"], sg_rows))

    eq_items = res.get("equippedItems") or []
    if eq_items:
        gear_bits: list[str] = []
        flask_bits: list[str] = []
        graft_bits: list[str] = []
        jewel_bits: list[str] = []
        for it in eq_items:
            sn = str(it.get("slot") or "-")
            nm = strip_pob_escapes(str(it.get("nameZh") or it.get("name") or "-"))
            en_nm = strip_pob_escapes(str(it.get("name") or ""))
            disp = f"{nm} ({en_nm})" if (it.get("nameZh") and en_nm and nm != en_nm and it.get("rarity") in ("UNIQUE", "RELIC")) else nm
            if it.get("isFlask"):
                st_tag = "[ON]" if it.get("active") else "[OFF]"
                t_tag = "[Tincture]" if it.get("isTincture") else ""
                flask_bits.append(f"`{sn}`{t_tag}{st_tag} {disp}")
            elif sn in ("Head", "Body", "Arm", "Leg") or str(it.get("type") or "") == "Viscera":
                graft_bits.append(f"`{sn}` {disp}")
            elif it.get("nodeId") is not None or sn.startswith("Jewel"):
                nid_tag = f"Jewel `{it.get('nodeId')}`" if it.get("nodeId") is not None else f"`{sn}`"
                jewel_bits.append(f"{nid_tag}: {disp}")
            else:
                gear_bits.append(f"`{sn}`: {disp}")
        if not compact or graft_bits or jewel_bits or flask_bits:
            lines.append("\n#### 2d. Equipped Items, Grafts & Jewels")
            if gear_bits and not compact:
                lines.append("- **Gear**: " + " | ".join(gear_bits))
            if flask_bits:
                lines.append("- **Flasks/Tinctures**: " + " | ".join(flask_bits))
            if graft_bits:
                lines.append("- **3.30 Wickerman Grafts**: " + " | ".join(graft_bits))
            if jewel_bits:
                lines.append("- **Jewels**: " + " | ".join(jewel_bits))
    return lines


def format_diagnose_markdown(res: dict[str, Any], *, compact: bool = False) -> str:
    """Render a low-token, cohesive one-shot Markdown report for `pob diagnose`."""
    if res.get("mode") == "compare" and isinstance(res.get("comparison"), dict):
        return format_diagnose_compare_markdown(res)

    lines: list[str] = []
    lines.extend(_fmt_diagnose_header(res, compact=compact))
    lines.extend(_fmt_diagnose_baseline(res, compact=compact))
    lines.extend(_fmt_diagnose_gaps_and_mechanics(res, compact=compact))
    lines.extend(_fmt_diagnose_skills_and_gear(res, compact=compact))
    lines.extend(_fmt_diagnose_fact_cards_and_upgrades(res, compact=compact))
    return "\n".join(lines)
