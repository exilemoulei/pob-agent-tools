"""Markdown formatters for `pob tree diff` and multi-stage `pob load` Progression Stages."""

from __future__ import annotations

from typing import Any

from .primitives import render_table, strip_pob_escapes


def _disp_name(entry: dict[str, Any]) -> str:
    zh = strip_pob_escapes(str(entry.get("nameZh") or "")).strip()
    en = strip_pob_escapes(str(entry.get("name") or "")).strip()
    if zh and en and zh != en:
        return f"{zh} ({en})"
    return zh or en or str(entry.get("id") or "-")


def format_tree_diff_markdown(res: dict[str, Any]) -> str:
    """Render 5-section structured passive tree diff into concise human- & agent-readable Markdown."""
    s = res.get("summary") or {}
    lbl_a = s.get("labelA") or "current"
    lbl_b = s.get("labelB") or "ref"
    pts_a = int(s.get("pointsA") or 0)
    pts_b = int(s.get("pointsB") or 0)
    pts_delta = int(s.get("pointsDelta") or 0)

    lines: list[str] = [
        f"### Passive Tree Diff: `{lbl_a}` -> `{lbl_b}`",
        f"- **Points**: `{pts_a}` -> `{pts_b}` (`{pts_delta:+d}` pts | `{s.get('sharedPoints', 0)}` shared)",
    ]

    # 1. Ascendancy / Bloodline summary
    asc_add = s.get("ascendancyAdded") or []
    asc_rm = s.get("ascendancyRemoved") or []
    if asc_add or asc_rm:
        lines.append("\n#### 1. Ascendancy & Bloodline Diff")
        if asc_add:
            lines.append("- **Added (+ in Ref)**: " + ", ".join(_disp_name(x) for x in asc_add))
        if asc_rm:
            lines.append("- **Removed (- only in Current)**: " + ", ".join(_disp_name(x) for x in asc_rm))

    # 2. Keystones
    ks = res.get("keystones") or {}
    ks_ref = ks.get("onlyInRef") or []
    ks_cur = ks.get("onlyInCurrent") or []
    if ks_ref or ks_cur:
        lines.append("\n#### 2. Keystones")
        k_rows: list[list[Any]] = []
        for x in ks_ref:
            st = "; ".join((x.get("statsZh") or x.get("stats") or [])[:2])[:80]
            k_rows.append(["+ Only in Ref", _disp_name(x), st or "-"])
        for x in ks_cur:
            st = "; ".join((x.get("statsZh") or x.get("stats") or [])[:2])[:80]
            k_rows.append(["- Only in Current", _disp_name(x), st or "-"])
        lines.append(render_table(["Diff", "Keystone", "Summary"], k_rows))

    # 3. Core Wheels & Highway Travel
    wh = res.get("wheels") or {}
    w_ref = wh.get("onlyInRef") or []
    w_cur = wh.get("onlyInCurrent") or []
    w_part = wh.get("partialDeltas") or []
    hw = wh.get("highwayTravel") or {}
    lines.append("\n#### 3. Core Wheels & Highway Travel")
    if w_ref:
        lines.append("\n**Only in Ref / Missing Wheels**:")
        rows_r = [
            [
                _disp_name(w),
                w.get("points"),
                f"{w['shortestHopFromCurrent']} hops" if w.get("shortestHopFromCurrent") is not None else "-",
                ", ".join(w.get("notables") or []) or "-",
            ]
            for w in w_ref[:15]
        ]
        lines.append(render_table(["Wheel", "Pts", "Dist from Current", "Notables"], rows_r))
    if w_cur:
        lines.append("\n**Only in Current / Extra Wheels**:")
        rows_c = [
            [_disp_name(w), w.get("points"), ", ".join(w.get("notables") or []) or "-"]
            for w in w_cur[:15]
        ]
        lines.append(render_table(["Wheel", "Pts", "Notables"], rows_c))
    if w_part:
        lines.append("\n**Partial Wheel Deltas**:")
        rows_p = [
            [
                _disp_name(w),
                f"{w.get('pointsA')} -> {w.get('pointsB')} ({int(w.get('pointsDelta') or 0):+d})",
                ", ".join(f"+{n}" for n in (w.get("addedNotables") or [])) or "-",
                ", ".join(f"-{n}" for n in (w.get("removedNotables") or [])) or "-",
            ]
            for w in w_part[:12]
        ]
        lines.append(render_table(["Wheel", "Points", "Added Notables", "Removed Notables"], rows_p))
    if not (w_ref or w_cur or w_part):
        lines.append("- _(No wheel differences.)_")
    lines.append(
        f"- **Highway Travel Points**: `{hw.get('currentPoints', 0)}` -> `{hw.get('refPoints', 0)}` (`{int(hw.get('delta') or 0):+d}` pts)"
    )

    # 4. Masteries
    mast = res.get("masteries") or {}
    m_ref = mast.get("onlyInRef") or []
    m_cur = mast.get("onlyInCurrent") or []
    m_chg = mast.get("changedEffect") or []
    if m_ref or m_cur or m_chg:
        lines.append("\n#### 4. Masteries")
        m_rows: list[list[Any]] = []
        for m in m_ref:
            st = "; ".join((m.get("statsZh") or m.get("stats") or [])[:2])
            m_rows.append(["+ Only in Ref", f"`{m.get('allocSpec')}`", m.get("wheel") or m.get("name") or "-", st or "-"])
        for m in m_cur:
            st = "; ".join((m.get("statsZh") or m.get("stats") or [])[:2])
            m_rows.append(["- Only in Current", f"`{m.get('allocSpec')}`", m.get("wheel") or m.get("name") or "-", st or "-"])
        for ch in m_chg:
            ca, cb = ch.get("current") or {}, ch.get("ref") or {}
            sa = "; ".join((ca.get("statsZh") or ca.get("stats") or [])[:1]) or "-"
            sb = "; ".join((cb.get("statsZh") or cb.get("stats") or [])[:1]) or "-"
            m_rows.append(
                [
                    "~ Different Effect",
                    f"`{ca.get('allocSpec')}` -> `{cb.get('allocSpec')}`",
                    ch.get("wheel") or ca.get("name") or "-",
                    f"{sa} -> {sb}",
                ]
            )
        lines.append(render_table(["Diff", "Mastery Spec", "Wheel", "Effect"], m_rows))

    # 5. Cluster Jewels & Jewel Sockets
    sk = res.get("sockets") or {}
    lines.append("\n#### 5. Cluster Jewels & Jewel Sockets")
    if res.get("mode") == "specs" or res.get("socketsNote"):
        note = str(
            res.get("socketsNote")
            or "Intra-build Spec diff compares passive allocations only; equipped jewels/clusters reflect the slot's active ItemSet."
        )
        lines.append(f"_{note}_")
    lines.append(
        f"- **Allocated Jewel Sockets**: `{sk.get('allocatedCountA', 0)}` -> `{sk.get('allocatedCountB', 0)}` "
        f"(`{int(sk.get('delta') or 0):+d}`)"
    )
    if sk.get("onlyInRef"):
        lines.append("- **Sockets Only in Ref**: `" + ", ".join(str(x) for x in sk["onlyInRef"]) + "`")
    if sk.get("onlyInCurrent"):
        lines.append("- **Sockets Only in Current**: `" + ", ".join(str(x) for x in sk["onlyInCurrent"]) + "`")
    cl_a = sk.get("outerClustersA") or []
    cl_b = sk.get("outerClustersB") or []
    if cl_a or cl_b:
        c_rows: list[list[Any]] = []
        for tag, cl_list in (("Current", cl_a), ("Ref", cl_b)):
            for c in cl_list:
                c_rows.append(
                    [
                        tag,
                        c.get("nodeId"),
                        c.get("sector") or "-",
                        c.get("enchant") or c.get("itemName") or ("Occupied" if c.get("occupied") else "[EMPTY]"),
                        ", ".join(c.get("allocatedClusterNotables") or []) or "-",
                    ]
                )
        lines.append(render_table(["Build", "Socket ID", "Sector", "Cluster / Enchant", "Allocated Notables"], c_rows))

    return "\n".join(lines)


def format_progression_stages_markdown(stages: dict[str, Any], selected: dict[str, Any] | None = None) -> str:
    """Render compact `Progression Stages` overview for multi-stage `pob load` Markdown."""
    if not isinstance(stages, dict) or not stages.get("hasMultipleStages"):
        return ""
    lines = ["", "#### Progression Stages"]
    if isinstance(selected, dict) and selected:
        sel_parts = []
        if selected.get("spec"):
            sel_parts.append(f"Spec #{selected['spec']['index']} `{selected['spec']['title']}`")
        if selected.get("itemSet"):
            sel_parts.append(f"ItemSet #{selected['itemSet']['id']} `{selected['itemSet']['title']}`")
        if selected.get("skillSet"):
            sel_parts.append(f"SkillSet #{selected['skillSet']['id']} `{selected['skillSet']['title']}`")
        if sel_parts:
            lines.append("- **Selected**: " + " | ".join(sel_parts))

    for label, key in (("Tree Specs", "specs"), ("Item Sets", "itemSets"), ("Skill Sets", "skillSets")):
        items = stages.get(key) or []
        if len(items) > 1:
            rendered = []
            for it in items:
                mark = "*" if it.get("active") else ""
                idx_lbl = it.get("index") if key == "specs" else it.get("id")
                rendered.append(f"`{idx_lbl}{mark}` {it.get('title')}")
            lines.append(f"- **{label}** ({len(items)}): " + " | ".join(rendered))
    lines.append("_Use `pob load <source> --stage <idx|title>` (or `--spec` / `--item-set` / `--skill-set`) to switch stages._")
    return "\n".join(lines)
