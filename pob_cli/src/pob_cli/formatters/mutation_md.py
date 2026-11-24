"""Markdown formatters for mutation cautions and mutation diff reports."""

from __future__ import annotations

from typing import Any

from .primitives import (
    fmt_delta,
    fmt_num,
    format_recency_stamp,
    render_table,
    strip_pob_escapes,
)


def format_cautions(res: dict[str, Any] | list[str] | tuple[str, ...] | None) -> list[str]:
    """Extract and format mutation caution lines (`> [CAUTION] <msg>`) deduplicated in order."""
    raw_items: list[Any] = []
    if isinstance(res, (list, tuple)):
        raw_items.extend(res)
    elif isinstance(res, dict):
        if isinstance(res.get("cautions"), (list, tuple)):
            raw_items.extend(res["cautions"])
        detail = res.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("cautions"), (list, tuple)):
            raw_items.extend(detail["cautions"])
        steps = res.get("steps") or (detail.get("steps") if isinstance(detail, dict) else None) or []
        if isinstance(steps, list):
            for st in steps:
                if isinstance(st, dict) and isinstance(st.get("cautions"), (list, tuple)):
                    raw_items.extend(st["cautions"])
    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        msg = strip_pob_escapes(str(item or "")).strip()
        if msg and msg not in seen:
            seen.add(msg)
            out.append(f"> [CAUTION] {msg}")
    return out


def format_mutation_report(title: str, res: dict[str, Any], *, target: str | None = None) -> str:
    """Format mutation results (`tree change`, `skill set`, `skill add`, `batch`, etc.) into Markdown."""
    stamp = format_recency_stamp(res, slot=target or res.get("targetSlot"))
    lines = [stamp, f"### {title} — `{res.get('mode', '')}`"]
    before = res.get("before") or {}
    after = res.get("after") or {}
    no_undo = bool(res.get("noUndo")) or (not before and bool(after))
    p_key = after.get("primaryDpsKey") or before.get("primaryDpsKey") or "CombinedDPS"
    b_pts = before.get("points") or {}
    a_pts = after.get("points") or {}

    if no_undo:
        summary_rows = [
            [f"Primary DPS ({p_key})", "-", fmt_num(after.get("primaryDps")), "(no-undo)"],
            ["Effective HP (TotalEHP)", "-", fmt_num(after.get("totalEhp")), "(no-undo)"],
            ["Maximum Life", "-", fmt_num(after.get("life")), "(no-undo)"],
            [
                "Passive Points",
                "-",
                f"{a_pts.get('used', 0)}/{a_pts.get('usedMax', 0)} (Asc {a_pts.get('ascUsed', 0)}/{a_pts.get('ascMax', 0)})",
                "(no-undo)",
            ],
        ]
    else:
        summary_rows = [
            [
                f"Primary DPS ({p_key})",
                fmt_num(before.get("primaryDps")),
                fmt_num(after.get("primaryDps")),
                fmt_delta(before.get("primaryDps"), after.get("primaryDps")),
            ],
            [
                "Effective HP (TotalEHP)",
                fmt_num(before.get("totalEhp")),
                fmt_num(after.get("totalEhp")),
                fmt_delta(before.get("totalEhp"), after.get("totalEhp")),
            ],
            [
                "Maximum Life",
                fmt_num(before.get("life")),
                fmt_num(after.get("life")),
                fmt_delta(before.get("life"), after.get("life")),
            ],
            [
                "Passive Points",
                f"{b_pts.get('used', 0)}/{b_pts.get('usedMax', 0)} (Asc {b_pts.get('ascUsed', 0)}/{b_pts.get('ascMax', 0)})",
                f"{a_pts.get('used', 0)}/{a_pts.get('usedMax', 0)} (Asc {a_pts.get('ascUsed', 0)}/{a_pts.get('ascMax', 0)})",
                fmt_delta(b_pts.get("used", 0), a_pts.get("used", 0), " pts"),
            ],
        ]
    lines.append(render_table(["Headline Metric", "Before", "After", "Delta"], summary_rows))

    diff = res.get("diff") or {}
    deltas = diff.get("deltas") or []
    if deltas:
        lines.append("\n#### Changed Stats")
        d_rows = [[d["label"], d["beforeStr"], d["afterStr"], d["deltaStr"]] for d in deltas]
        lines.append(render_table(["Stat", "Before", "After", "Delta"], d_rows))
    elif no_undo:
        lines.append("\n_(Before/diff snapshot skipped in `--no-undo` mode)_")
    else:
        lines.append("\n_(No stat changes detected)_")

    cascade_warn = res.get("cascadeWarning") or (res.get("detail") or {}).get("cascadeWarning")
    if cascade_warn:
        lines.append(f"\n> [WARN] {cascade_warn}")

    caution_lines = format_cautions(res)
    if caution_lines:
        lines.append("")
        lines.extend(caution_lines)

    new_w = diff.get("newWarnings") or []
    res_w = diff.get("resolvedWarnings") or []
    if new_w:
        lines.append("\n#### New Warnings")
        for w in new_w:
            lines.append(f"- [NEW WARNING] {w}")
    if res_w:
        lines.append("\n#### Resolved Warnings")
        for w in res_w:
            lines.append(f"- [RESOLVED] {w}")

    return "\n".join(lines)
