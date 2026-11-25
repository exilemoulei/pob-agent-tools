"""Markdown formatters for matrix, sweep, compare, cautions, and mutation reports."""

from __future__ import annotations

import math
from typing import Any

from .primitives import (
    _parse_numeric_or_none,
    fmt_delta,
    fmt_num,
    keep_rejected_warning,
    render_table,
    restore_warning,
    strip_pob_escapes,
)


def extract_custom_mods_330_notes(*sources: Any) -> list[str]:
    """Collect deduplicated 3.30 customMods simulation notes from stripped dicts or water-source rows."""
    notes: list[str] = []

    def _add(val: Any) -> None:
        if isinstance(val, str) and val.strip() and val.strip() not in notes:
            notes.append(val.strip())
        elif isinstance(val, (list, tuple)):
            for item in val:
                _add(item)

    fallback_rows: list[str] = []
    for src in sources:
        if isinstance(src, dict):
            _add(src.get("customMods330Notes"))
            _add(src.get("customMods330Note"))
        elif isinstance(src, (list, tuple)):
            for entry in src:
                if not isinstance(entry, dict):
                    continue
                _add(entry.get("customMods330Notes"))
                _add(entry.get("customMods330Note"))
                lbl = str(entry.get("label") or entry.get("item") or "").strip()
                if "3.30 Viscera/Runegraft simulation" in lbl and lbl not in fallback_rows:
                    fallback_rows.append(lbl)
    if not notes and fallback_rows:
        for lbl in fallback_rows:
            _add(
                f"{lbl} — check equipped `Head/Body/Arm/Leg` Viscera & Runegrafts (`pob data tattoo`); "
                "pass `--keep customMods` only for verified active 3.30 lines."
            )
    return notes


def _safe_min_ele_max_hit(stats: dict[str, Any]) -> float | str | None:
    if not stats:
        return None
    if stats.get("eleMaxHit") is not None:
        return stats.get("eleMaxHit")
    vals: list[float] = []
    for key_group in (
        ("FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken"),
        ("fireMaxHit", "coldMaxHit", "lightningMaxHit"),
    ):
        for key in key_group:
            raw = stats.get(key)
            if raw is None:
                continue
            parsed = _parse_numeric_or_none(raw)
            if parsed is not None and not math.isnan(parsed):
                vals.append(parsed)
        if vals:
            break
    if not vals:
        return None
    m = min(vals)
    return "inf" if math.isinf(m) and m > 0 else m


def _extract_metrics_block(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalize metrics from either full `digest` or compact `summary`/`coreStats` payloads."""
    dig = entry.get("digest") if isinstance(entry.get("digest"), dict) else {}
    summ = entry.get("summary") if isinstance(entry.get("summary"), dict) else {}
    core = entry.get("coreStats") if isinstance(entry.get("coreStats"), dict) else {}
    if not core and isinstance(summ.get("coreStats"), dict):
        core = summ["coreStats"]
    st = dig.get("stats") if isinstance(dig.get("stats"), dict) else {}
    if not st and isinstance(entry.get("stats"), dict):
        st = entry["stats"]
    off = summ.get("offence") if isinstance(summ.get("offence"), dict) else {}
    dfn = summ.get("defence") if isinstance(summ.get("defence"), dict) else {}

    def _pick(*candidates: Any) -> Any:
        for val in candidates:
            if val is not None:
                return val
        return None

    primary_key = _pick(
        dig.get("primaryDpsKey"),
        summ.get("primaryDpsKey"),
        off.get("primaryDpsKey"),
        entry.get("primaryDpsKey"),
        "CombinedDPS",
    )
    primary_dps = _pick(
        dig.get("primaryDps"),
        summ.get("primaryDps"),
        off.get("primaryDps"),
        off.get(primary_key),
        core.get(primary_key),
        st.get(primary_key),
        entry.get("primaryDps"),
    )
    total_ehp = _pick(
        dig.get("totalEhp"),
        summ.get("totalEhp"),
        dfn.get("TotalEHP"),
        dfn.get("totalEhp"),
        core.get("TotalEHP"),
        st.get("TotalEHP"),
        entry.get("totalEhp"),
    )
    avg_hit = _pick(
        st.get("AverageDamage"),
        core.get("AverageDamage"),
        off.get("AverageDamage"),
        dig.get("averageDamage"),
        summ.get("averageDamage"),
    )
    speed = _pick(
        st.get("Speed"),
        core.get("Speed"),
        off.get("Speed"),
        dig.get("speed"),
        summ.get("speed"),
    )
    life = _pick(
        st.get("Life"),
        core.get("Life"),
        dfn.get("Life"),
        dig.get("life"),
        summ.get("life"),
        entry.get("life"),
    )
    es = _pick(
        st.get("EnergyShield"),
        core.get("EnergyShield"),
        dfn.get("EnergyShield"),
        dig.get("energyShield"),
        summ.get("energyShield"),
        entry.get("energyShield"),
    )
    phys_max = _pick(
        st.get("PhysicalMaximumHitTaken"),
        core.get("PhysicalMaximumHitTaken"),
        dfn.get("PhysicalMaximumHitTaken"),
        dig.get("physMaxHit"),
        summ.get("physMaxHit"),
    )
    ele_max = _pick(
        dig.get("eleMaxHit"),
        summ.get("eleMaxHit"),
        entry.get("eleMaxHit"),
        _safe_min_ele_max_hit(st) if st else None,
        _safe_min_ele_max_hit(core) if core else None,
        _safe_min_ele_max_hit(dfn) if dfn else None,
        _safe_min_ele_max_hit(summ) if summ else None,
        _safe_min_ele_max_hit(dig) if dig else None,
        _safe_min_ele_max_hit(entry),
    )
    chaos_max = _pick(
        st.get("ChaosMaximumHitTaken"),
        core.get("ChaosMaximumHitTaken"),
        dfn.get("ChaosMaximumHitTaken"),
        dig.get("chaosMaxHit"),
        summ.get("chaosMaxHit"),
    )
    return {
        "primaryDpsKey": primary_key,
        "primaryDps": primary_dps,
        "totalEhp": total_ehp,
        "averageDamage": avg_hit,
        "speed": speed,
        "life": life,
        "energyShield": es,
        "physMaxHit": phys_max,
        "eleMaxHit": ele_max,
        "chaosMaxHit": chaos_max,
    }


def _format_calc_cell(val: Any) -> str:
    if val is None:
        return "-"
    if isinstance(val, (int, float, str)):
        return fmt_num(val)
    if isinstance(val, dict):
        values = val.get("values")
        if isinstance(values, list) and values:
            for item in values:
                formatted = _format_calc_cell(item)
                if formatted and formatted != "-":
                    return formatted
        for k in ("raw", "text", "value", "display"):
            if val.get(k) is not None and str(val.get(k)).strip() != "":
                return fmt_num(val[k])
        return "-"
    if isinstance(val, list):
        parts = [_format_calc_cell(x) for x in val[:2]]
        return "; ".join(p for p in parts if p and p != "-") or "-"
    return str(val)[:40]


def format_matrix_markdown(res: dict[str, Any], *, target: str = "main") -> str:
    """Format `pob matrix` results for both compact (default) and `--full` payloads."""
    warn = restore_warning(res.get("restoreCheck"))
    sc_list = res.get("scenarios") or []
    if not sc_list:
        return f"{warn}\n_(No matrix scenarios evaluated)_" if warn else "_(No matrix scenarios evaluated)_"

    base_m = _extract_metrics_block(sc_list[0])
    base_dps = base_m["primaryDps"]
    base_ehp = base_m["totalEhp"]
    p_key = str(res.get("primaryDpsKey") or base_m["primaryDpsKey"] or "CombinedDPS")

    rows: list[list[Any]] = []
    auto_kept: list[str] = []
    kept: list[str] = []
    rejected: list[str] = []
    cm_notes: list[str] = extract_custom_mods_330_notes(res, res.get("waterSources"), res.get("topWaterSources"))
    for idx, sc in enumerate(sc_list):
        m = _extract_metrics_block(sc)
        p_dps = m["primaryDps"]
        ehp = m["totalEhp"]
        deltas = sc.get("deltasVsFirst") if isinstance(sc.get("deltasVsFirst"), dict) else {}
        if idx == 0:
            dps_delta = "Baseline"
            ehp_delta = "Baseline"
        else:
            dps_delta = (
                deltas.get("primaryDpsStr")
                or deltas.get("dpsDeltaStr")
                or (fmt_delta(base_dps, p_dps) if base_dps is not None and p_dps is not None else "-")
            )
            ehp_delta = (
                deltas.get("totalEhpStr")
                or deltas.get("ehpDeltaStr")
                or (fmt_delta(base_ehp, ehp) if base_ehp is not None and ehp is not None else "-")
            )

        max_hit_str = (
            f"{fmt_num(m['physMaxHit'])} / "
            f"{fmt_num(m['eleMaxHit'])} / "
            f"{fmt_num(m['chaosMaxHit'])}"
        )
        speed_val = m["speed"]
        rows.append(
            [
                sc.get("name") or "-",
                fmt_num(p_dps),
                dps_delta,
                fmt_num(m["averageDamage"]),
                f"{fmt_num(speed_val, 2)}/s" if speed_val is not None else "-",
                fmt_num(ehp),
                ehp_delta,
                f"{fmt_num(m['life'])} / {fmt_num(m['energyShield'])}",
                max_hit_str,
            ]
        )

        st_info = (sc.get("applied") or {}).get("stripped") or {}
        if isinstance(st_info, dict):
            for ak in st_info.get("autoKept") or []:
                if ak not in auto_kept:
                    auto_kept.append(str(ak))
            for k in st_info.get("kept") or []:
                if k not in kept:
                    kept.append(k)
            for r in st_info.get("keepRejected") or []:
                if r not in rejected:
                    rejected.append(r)
            for cn in extract_custom_mods_330_notes(st_info):
                if cn not in cm_notes:
                    cm_notes.append(cn)

    header = f"### Scenario Matrix Comparison (`{target}`, Baseline: `{res.get('baseline')}`, Primary: `{p_key}`)"
    table = render_table(
        ["Scenario", p_key, "DPS Delta", "Avg Hit", "Speed", "TotalEHP", "EHP Delta", "Life / ES", "MaxHit (P/E/C)"],
        rows,
    )
    parts: list[str] = []
    if warn:
        parts.append(warn)
    parts.append(header)
    parts.append(table)
    for cn in cm_notes:
        parts.append(f"\n> [3.30 CustomMods Note] {cn}")
    if auto_kept:
        parts.append("\n**Auto-Kept (Build-Guaranteed)**: " + "; ".join(auto_kept))
    if kept:
        parts.append("\n**Kept (--keep)**: " + "; ".join(kept))
    rej_lines = keep_rejected_warning(rejected)
    if rej_lines:
        parts.append("\n" + "\n".join(rej_lines))
    return "\n".join(parts)


def format_sweep_markdown(result: dict[str, Any], *, target: str | None = None) -> str:
    """Format `pob sweep` variant comparison results into a concise Markdown table."""
    warn = restore_warning(result.get("restoreCheck"))
    variants = result.get("variants") or result.get("results") or []
    if not variants:
        return f"{warn}\n_(No sweep variants evaluated)_" if warn else "_(No sweep variants evaluated)_"

    base_entry = result.get("baseline") or result.get("base") or {}
    base_m = _extract_metrics_block(base_entry) if isinstance(base_entry, dict) and base_entry else None
    if base_m is None or base_m["primaryDps"] is None:
        first_ok = next((v for v in variants if v.get("ok", True) and not v.get("error")), None)
        base_m = _extract_metrics_block(first_ok) if first_ok else _extract_metrics_block({})

    base_dps = base_m["primaryDps"]
    base_ehp = base_m["totalEhp"]
    p_key = str(result.get("primaryDpsKey") or base_m["primaryDpsKey"] or "CombinedDPS")

    calc_keys: list[str] = []
    for k in result.get("calcKeywords") or result.get("calc_keywords") or []:
        ks = str(k).strip()
        if ks and ks not in calc_keys:
            calc_keys.append(ks)
    if not calc_keys:
        for v in variants:
            calcs = v.get("calcs")
            if isinstance(calcs, dict):
                for ck in calcs.keys():
                    if ck not in calc_keys:
                        calc_keys.append(str(ck))

    headers = ["Variant", "Status", p_key, "DPS Delta", "TotalEHP", "EHP Delta", "Life / ES", "MaxHit (P/E/C)"]
    headers.extend(calc_keys[:4])

    rows: list[list[Any]] = []
    errors: list[tuple[str, str]] = []

    if isinstance(base_entry, dict) and base_entry and base_m["primaryDps"] is not None:
        b_mh = f"{fmt_num(base_m['physMaxHit'])} / {fmt_num(base_m['eleMaxHit'])} / {fmt_num(base_m['chaosMaxHit'])}"
        b_row: list[Any] = [
            base_entry.get("name") or "(Baseline)",
            "Base",
            fmt_num(base_m["primaryDps"]),
            "Baseline",
            fmt_num(base_m["totalEhp"]),
            "Baseline",
            f"{fmt_num(base_m['life'])} / {fmt_num(base_m['energyShield'])}",
            b_mh,
        ]
        b_calcs = base_entry.get("calcs") if isinstance(base_entry.get("calcs"), dict) else {}
        for ck in calc_keys[:4]:
            b_row.append(_format_calc_cell(b_calcs.get(ck)))
        rows.append(b_row)

    for v in variants:
        v_name = str(v.get("name") or "-")
        if not v.get("ok", True) or v.get("error"):
            err_msg = str(v.get("error") or "failed")
            errors.append((v_name, err_msg))
            err_row: list[Any] = [v_name, "ERROR", "-", err_msg[:48], "-", "-", "-", "-"]
            err_row.extend(["-"] * len(calc_keys[:4]))
            rows.append(err_row)
            continue

        m = _extract_metrics_block(v)
        deltas = v.get("deltas") if isinstance(v.get("deltas"), dict) else {}
        dps_delta = (
            deltas.get("primaryDpsStr")
            or deltas.get("dpsDeltaStr")
            or (fmt_delta(base_dps, m["primaryDps"]) if base_dps is not None and m["primaryDps"] is not None else "-")
        )
        ehp_delta = (
            deltas.get("totalEhpStr")
            or deltas.get("ehpDeltaStr")
            or (fmt_delta(base_ehp, m["totalEhp"]) if base_ehp is not None and m["totalEhp"] is not None else "-")
        )
        mh = f"{fmt_num(m['physMaxHit'])} / {fmt_num(m['eleMaxHit'])} / {fmt_num(m['chaosMaxHit'])}"
        row: list[Any] = [
            v_name,
            "OK",
            fmt_num(m["primaryDps"]),
            dps_delta,
            fmt_num(m["totalEhp"]),
            ehp_delta,
            f"{fmt_num(m['life'])} / {fmt_num(m['energyShield'])}",
            mh,
        ]
        v_calcs = v.get("calcs") if isinstance(v.get("calcs"), dict) else {}
        for ck in calc_keys[:4]:
            row.append(_format_calc_cell(v_calcs.get(ck)))
        rows.append(row)

    meta_bits = []
    if target:
        meta_bits.append(f"Slot: `{target}`")
    raw_sc = result.get("scenario")
    if raw_sc:
        sc_label = (raw_sc.get("name") or raw_sc.get("base") or "Custom") if isinstance(raw_sc, dict) else str(raw_sc)
        meta_bits.append(f"Scenario: `{sc_label}`")
    meta_bits.append(f"Variants: `{len(variants)}`")
    meta_bits.append(f"Primary: `{p_key}`")
    lines: list[str] = []
    if warn:
        lines.append(warn)
    lines.extend([f"### Build Variant Sweep ({', '.join(meta_bits)})", render_table(headers, rows)])

    sc_applied = result.get("scenarioApplied")
    st_info = sc_applied.get("stripped") if isinstance(sc_applied, dict) else None
    for cn in extract_custom_mods_330_notes(st_info, result, result.get("waterSources"), result.get("topWaterSources")):
        lines.append(f"\n> [3.30 CustomMods Note] {cn}")
    if isinstance(st_info, dict):
        kept = st_info.get("kept") or []
        if kept:
            lines.append("\n**Kept (--keep)**: " + "; ".join(kept))
        rej_lines = keep_rejected_warning(st_info)
        if rej_lines:
            lines.append("\n" + "\n".join(rej_lines))

    if errors:
        lines.append("\n#### Variant Errors")
        for ename, emsg in errors:
            lines.append(f"- `{ename}`: {emsg}")
    return "\n".join(lines)


def format_compare_markdown(
    res: dict[str, Any],
    *,
    target: str = "main",
    ref_target: str = "ref",
    compact: bool = False,
) -> str:
    """Format `pob compare` (3 baselines + tree/skill/equipment diffs) into concise Markdown."""
    m_info = res.get("mainBuildInfo") or {}
    r_info = res.get("refBuildInfo") or {}
    lines = [
        f"### A/B Build Comparison: `{target}` (Lv.{m_info.get('level')} {strip_pob_escapes(m_info.get('ascendClassNameZh') or m_info.get('ascendClassName') or '-')}) vs `{ref_target}` (Lv.{r_info.get('level')} {strip_pob_escapes(r_info.get('ascendClassNameZh') or r_info.get('ascendClassName') or '-')})",
    ]

    baselines = res.get("baselines") or {}
    b_sections = (
        (
            ("Baseline 1: Original Panels", baselines.get("original", [])),
            ("Baseline 3: Both Dewatered + Pinnacle Boss (True Baseline)", (baselines.get("pinnacleDewatered") or {}).get("rows", [])),
        )
        if compact
        else (
            ("Baseline 1: Original Panels (As Configured)", baselines.get("original", [])),
            (f"Baseline 2: External Target Environment Aligned to `{target}`", (baselines.get("envAlignedToMain") or {}).get("rows", [])),
            ("Baseline 3: Both Dewatered + Pinnacle Boss (True Baseline)", (baselines.get("pinnacleDewatered") or {}).get("rows", [])),
        )
    )
    if compact and (baselines.get("envAlignedToMain") or {}).get("rows"):
        b_sections = (
            ("Baseline 1: Original Panels", baselines.get("original", [])),
            (f"Baseline 2: Env Aligned to `{target}`", (baselines.get("envAlignedToMain") or {}).get("rows", [])),
            ("Baseline 3: Both Dewatered + Pinnacle Boss (True Baseline)", (baselines.get("pinnacleDewatered") or {}).get("rows", [])),
        )
    _COMPACT_COMPARE_LABELS = frozenset(
        {
            "Full DPS",
            "Combined DPS",
            "Effective HP (TotalEHP)",
            "Maximum Life",
            "Energy Shield",
            "Phys Max Hit",
            "Fire Max Hit",
            "Cold Max Hit",
            "Lightning Max Hit",
            "Chaos Max Hit",
        }
    )
    for title, rows in b_sections:
        lines.append(f"\n#### {title}")
        use_rows = (
            [r for r in rows if r.get("label") in _COMPACT_COMPARE_LABELS] or rows
            if compact
            else rows
        )
        t_rows = [[r["label"], r["mainStr"], r["refStr"], r["diffRefVsMainStr"]] for r in use_rows]
        lines.append(render_table(["Metric", target, ref_target, f"{ref_target} vs {target}"], t_rows))

    pin_dew = baselines.get("pinnacleDewatered") or {}
    auto_parts: list[str] = []
    kept_parts: list[str] = []
    rej_lines: list[str] = []
    cm_notes: list[str] = []
    for slot_lbl, st_key in ((target, "mainStripped"), (ref_target, "refStripped")):
        st_d = pin_dew.get(st_key)
        if not isinstance(st_d, dict):
            continue
        ak = [str(x) for x in (st_d.get("autoKept") or []) if x]
        if ak:
            auto_parts.append(f"`{slot_lbl}`: " + "; ".join(ak))
        kp = [str(x) for x in (st_d.get("kept") or []) if x]
        if kp:
            kept_parts.append(f"`{slot_lbl}`: " + "; ".join(kp))
        for r_ln in keep_rejected_warning(st_d):
            if r_ln not in rej_lines:
                rej_lines.append(r_ln)
        for cn in extract_custom_mods_330_notes(st_d):
            tagged = f"`{slot_lbl}`: {cn}"
            if tagged not in cm_notes:
                cm_notes.append(tagged)
    for cn in cm_notes:
        lines.append(f"\n> [3.30 CustomMods Note] {cn}")
    if auto_parts:
        lines.append("\n**Auto-Kept (Build-Guaranteed)**: " + " | ".join(auto_parts))
    if kept_parts:
        lines.append("\n**Kept (--keep)**: " + " | ".join(kept_parts))
    if rej_lines:
        lines.extend(rej_lines)

    td = res.get("treeDiff") or {}
    ks = td.get("keystones") or {}
    asc_d = td.get("ascendancy") or {}
    lines.append("\n#### Passive Tree Diff")
    lines.append(
        f"- **Keystones Only in `{target}`**: {', '.join(x['name'] for x in ks.get('onlyInMain', [])) or '-'}\n"
        f"- **Keystones Only in `{ref_target}`**: {', '.join(x['name'] for x in ks.get('onlyInRef', [])) or '-'}\n"
        f"- **Ascendancy Only in `{target}`**: {', '.join(x['name'] for x in (asc_d.get('onlyInMain') or [])[:8]) or '-'}\n"
        f"- **Ascendancy Only in `{ref_target}`**: {', '.join(x['name'] for x in (asc_d.get('onlyInRef') or [])[:8]) or '-'}\n"
        f"- **Notables Shared**: `{(td.get('notables') or {}).get('sharedCount', 0)}` | **Only in `{target}`**: `{len((td.get('notables') or {}).get('onlyInMain', []))}` | **Only in `{ref_target}`**: `{len((td.get('notables') or {}).get('onlyInRef', []))}`"
    )

    sd = res.get("skillDiff") or {}
    m_mg = sd.get("mainGroupMain") or {}
    r_mg = sd.get("refGroupMain") or {}
    m_mg_lbl = strip_pob_escapes(m_mg.get("displayLabelZh") or m_mg.get("displayLabel") or "-")
    r_mg_lbl = strip_pob_escapes(r_mg.get("displayLabelZh") or r_mg.get("displayLabel") or "-")
    lines.append("\n#### Skill & Aura Diff")
    lines.append(
        f"- **`{target}` Primary Skill Group**: `#{m_mg.get('index', '-')}` {m_mg_lbl}\n"
        f"- **`{ref_target}` Primary Skill Group**: `#{r_mg.get('index', '-')}` {r_mg_lbl}\n"
        f"- **Active Skills Only in `{target}`**: {', '.join(sd.get('activeSkillsOnlyInMain', [])) or '-'}\n"
        f"- **Active Skills Only in `{ref_target}`**: {', '.join(sd.get('activeSkillsOnlyInRef', [])) or '-'}\n"
        f"- **Shared Active Skills**: {', '.join(sd.get('sharedActiveSkills', [])) or '-'}"
    )

    slot_diff = res.get("itemSlotDiff") or []
    if compact:
        slot_diff = [s for s in slot_diff if s.get("mainItem") != s.get("refItem")]
    if slot_diff:
        lines.append("\n#### Equipment Slot Diff")
        slot_rows = [[s["slot"], s["mainItem"], s["refItem"]] for s in slot_diff]
        lines.append(render_table(["Slot", f"{target} Item", f"{ref_target} Item"], slot_rows))

    return "\n".join(lines)
