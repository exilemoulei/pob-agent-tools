"""Markdown formatter for `pob compare` (3 baselines + tree/skill/equipment/jewel diffs)."""

from __future__ import annotations

from typing import Any

from .bilingual import format_bilingual
from .dewater_md import format_compare_dewater_lines
from .primitives import keep_rejected_warning, render_table


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
                if "Viscera/Runegraft lines" in lbl and lbl not in fallback_rows:
                    fallback_rows.append(lbl)
    if not notes and fallback_rows:
        for lbl in fallback_rows:
            _add(
                f"{lbl}: check `pob items` and `pob tree` for the Viscera or Runegraft itself; "
                "`--keep customMods` keeps the block whole, so take out the lines the build already carries first."
            )
    return notes


def format_compare_markdown(
    res: dict[str, Any],
    *,
    target: str = "main",
    ref_target: str = "ref",
    compact: bool = False,
) -> str:
    """Format `pob compare` (3 baselines + tree/skill/equipment/jewel diffs) into concise Markdown."""
    m_info = res.get("mainBuildInfo") or {}
    r_info = res.get("refBuildInfo") or {}
    m_asc = format_bilingual(m_info.get("ascendClassName"), m_info.get("ascendClassNameZh"), kind="asc") or "-"
    r_asc = format_bilingual(r_info.get("ascendClassName"), r_info.get("ascendClassNameZh"), kind="asc") or "-"
    lines = [
        f"### A/B Build Comparison: `{target}` (Lv.{m_info.get('level')} {m_asc}) vs `{ref_target}` (Lv.{r_info.get('level')} {r_asc})",
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
    _compact_labels = frozenset(
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
            [r for r in rows if r.get("label") in _compact_labels] or rows
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
    lines.extend(format_compare_dewater_lines(pin_dew, target, ref_target))
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
    m_mg_lbl = format_bilingual(m_mg.get("displayLabel"), m_mg.get("displayLabelZh"), kind="gem") or "-"
    r_mg_lbl = format_bilingual(r_mg.get("displayLabel"), r_mg.get("displayLabelZh"), kind="gem") or "-"
    lines.append("\n#### Skill & Aura Diff")
    lines.append(
        f"- **`{target}` Primary Skill Group**: `#{m_mg.get('index', '-')}` {m_mg_lbl}\n"
        f"- **`{ref_target}` Primary Skill Group**: `#{r_mg.get('index', '-')}` {r_mg_lbl}\n"
        f"- **Active Skills Only in `{target}`**: {', '.join(format_bilingual(x, kind='gem') for x in sd.get('activeSkillsOnlyInMain', [])) or '-'}\n"
        f"- **Active Skills Only in `{ref_target}`**: {', '.join(format_bilingual(x, kind='gem') for x in sd.get('activeSkillsOnlyInRef', [])) or '-'}\n"
        f"- **Shared Active Skills**: {', '.join(format_bilingual(x, kind='gem') for x in sd.get('sharedActiveSkills', [])) or '-'}"
    )

    slot_diff = res.get("itemSlotDiff") or []
    if compact:
        slot_diff = [s for s in slot_diff if s.get("mainItem") != s.get("refItem")]
    if slot_diff:
        lines.append("\n#### Equipment Slot Diff")
        slot_rows = [
            [
                s["slot"],
                format_bilingual(s["mainItem"], kind="item") or "-",
                format_bilingual(s["refItem"], kind="item") or "-",
            ]
            for s in slot_diff
        ]
        lines.append(render_table(["Slot", f"{target} Item", f"{ref_target} Item"], slot_rows))

    jewel_diff = res.get("jewelSocketDiff") or []
    if compact:
        jewel_diff = [j for j in jewel_diff if j.get("status") != "same"]
    if jewel_diff:
        lines.append("\n#### Jewel Sockets Diff (by nodeId)")
        j_rows = [
            [
                f"node:{j['nodeId']}",
                format_bilingual(j.get("mainItem"), kind="item") if j.get("mainItem") not in (None, "-") else ("(empty)" if j.get("mainAllocated") else "-"),
                format_bilingual(j.get("refItem"), kind="item") if j.get("refItem") not in (None, "-") else ("(empty)" if j.get("refAllocated") else "-"),
                j.get("status") or "-",
            ]
            for j in jewel_diff
        ]
        lines.append(render_table(["Socket", f"{target} Jewel", f"{ref_target} Jewel", "Status"], j_rows))

    return "\n".join(lines)
