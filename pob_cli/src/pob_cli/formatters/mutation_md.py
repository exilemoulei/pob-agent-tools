"""Markdown formatters for mutation cautions and mutation diff reports."""

from __future__ import annotations

from typing import Any

from .bilingual import format_bilingual
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


def _mutation_subject_line(detail: dict[str, Any]) -> str | None:
    item = detail.get("item") if isinstance(detail.get("item"), dict) else None
    if item:
        rarity = str(item.get("rarity") or "").upper()
        title = format_bilingual(
            item.get("title") or item.get("name"),
            item.get("titleZh") or item.get("nameZh"),
            kind="unique" if rarity in ("UNIQUE", "RELIC") else "item",
        )
        base = format_bilingual(item.get("baseName"), item.get("baseNameZh"), kind="base")
        if title and base and base != title:
            return f"- **Item**: {title} — {base}"
        if title or base:
            return f"- **Item**: {title or base}"
    gem = detail.get("gem") if isinstance(detail.get("gem"), dict) else None
    if gem and (gem.get("name") or gem.get("nameSpec")):
        lbl = format_bilingual(gem.get("name") or gem.get("nameSpec"), gem.get("nameZh"), kind="gem")
        if lbl:
            return f"- **Gem**: {lbl}"
    return None


def format_mutation_report(title: str, res: dict[str, Any], *, target: str | None = None) -> str:
    """Format mutation results (`tree change`, `skill set`, `skill add`, `batch`, etc.) into Markdown."""
    stamp = format_recency_stamp(res, slot=target or res.get("targetSlot"))
    lines = [stamp, f"### {title} — `{res.get('mode', '')}`"]
    detail = res.get("detail") if isinstance(res.get("detail"), dict) else {}
    subj = _mutation_subject_line(detail)
    if subj:
        lines.append(subj)
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

    raw_noop = detail.get("noopWarnings") or res.get("noopWarnings") or []
    if isinstance(raw_noop, (list, tuple)):
        noop_lines = [f"> [WARN] {strip_pob_escapes(str(w or '')).strip()}" for w in raw_noop if str(w or "").strip()]
        if noop_lines:
            lines.append("")
            lines.extend(noop_lines)

    cascade_warn = res.get("cascadeWarning") or detail.get("cascadeWarning")
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
