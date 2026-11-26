"""Markdown formatter for `pob item db` search, stat-sorted rankings, and `--list-sorts`."""

from __future__ import annotations

import math
from typing import Any

from .bilingual import format_bilingual
from .mutation_md import format_mutation_report
from .primitives import fmt_num, render_table, strip_pob_escapes

_TAKEN_HIT_STATS: frozenset[str] = frozenset(
    {
        "PhysicalTakenHit",
        "LightningTakenHit",
        "ColdTakenHit",
        "FireTakenHit",
        "ChaosTakenHit",
    }
)


def _fmt_compact_metric(val: Any) -> str:
    """Format a PoB metric compactly (e.g., `70.82M`, `13.43K`, `915`, `inf`)."""
    if val is None or isinstance(val, bool):
        return "-"
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("inf", "+inf", "infinity", "+infinity"):
            return "inf"
        if s in ("-inf", "-infinity"):
            return "-inf"
        if s == "nan":
            return "-"
        try:
            v = float(s.replace(",", ""))
        except ValueError:
            return val
    else:
        try:
            v = float(val)
        except (TypeError, ValueError):
            return str(val)

    if math.isnan(v):
        return "-"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    av = abs(v)
    if av >= 1e9:
        return f"{v / 1e9:.2f}B"
    if av >= 1e6:
        return f"{v / 1e6:.2f}M"
    if av >= 1e4:
        return f"{v / 1e3:.2f}K"
    return fmt_num(v)


def _fmt_power_cell(measured: Any, delta: Any, delta_pct: Any) -> str:
    """Render `70.82M (+13.43M, +23.4%)` safely across zero baselines and +/-inf."""
    val_str = _fmt_compact_metric(measured)
    if val_str == "-":
        return "-"
    if delta is None or isinstance(delta, bool):
        return val_str
    try:
        d = float(delta)
    except (TypeError, ValueError):
        return val_str
    if not math.isfinite(d):
        return val_str
    if abs(d) < 1e-6:
        return f"{val_str} (0)"
    d_sign = "+" if d > 0 else ""
    d_str = f"{d_sign}{_fmt_compact_metric(d)}"
    if delta_pct is not None and not isinstance(delta_pct, bool):
        try:
            p = float(delta_pct)
        except (TypeError, ValueError):
            p = math.nan
        if math.isfinite(p):
            p_sign = "+" if (p > 0 or (p == 0 and d > 0)) else ""
            return f"{val_str} ({d_str}, {p_sign}{p:.1f}%)"
    return f"{val_str} ({d_str})"


def _fmt_filter_summary(query: str, filters: dict[str, Any]) -> str:
    parts: list[str] = []
    if query:
        parts.append(f"`{query}`")
    slot = str(filters.get("slot") or "")
    target_slot = str(filters.get("targetSlot") or "")
    if slot and slot != "Any slot":
        parts.append(f"slot=`{target_slot}`" if (target_slot and target_slot != slot) else f"slot=`{slot}`")
    elif target_slot:
        parts.append(f"slot=`{target_slot}`")
    itype = str(filters.get("itemType") or "")
    if itype and itype != "Any type":
        parts.append(f"type=`{itype}`")
    req = str(filters.get("requirement") or "")
    if req and req != "Any requirements":
        parts.append(f"req=`{req}`")
    obt = str(filters.get("obtainable") or "")
    if obt:
        parts.append(f"obtainable=`{obt}`")
    lg = str(filters.get("league") or "")
    if lg and lg != "Any league":
        parts.append(f"league=`{lg}`")
    sm = str(filters.get("searchMode") or "")
    if sm and sm != "Anywhere":
        parts.append(f"search=`{sm}`")
    return ", ".join(parts) if parts else "all"


def _fmt_item_title(m: dict[str, Any]) -> str:
    en = strip_pob_escapes(str(m.get("title") or m.get("name") or "")).strip()
    zh = strip_pob_escapes(str(m.get("titleZh") or m.get("nameZh") or "")).strip()
    return format_bilingual(en, zh, kind="unique") or "-"


def _fmt_item_base(m: dict[str, Any]) -> str:
    en = strip_pob_escapes(str(m.get("baseName") or m.get("type") or "")).strip()
    zh = strip_pob_escapes(str(m.get("baseNameZh") or "")).strip()
    return format_bilingual(en, zh, kind="base") or "-"


def _power_col_header(sort_mode: str) -> str:
    if sort_mode.removeprefix("Minion") in _TAKEN_HIT_STATS:
        return f"New (-{sort_mode}, higher=less taken) / Delta"
    return f"New ({sort_mode}) / Delta"


def format_item_db_markdown(res: dict[str, Any]) -> str:
    """Render `pob item db` response (sort listing, search listing, `--list-sorts`, or `--equip`)."""
    if res.get("listSorts"):
        rows = [
            [s.get("sortMode") or "-", s.get("label") or "-", s.get("labelZh") or "-"]
            for s in (res.get("sorts") or [])
        ]
        lines = [
            f"### PoB Item DB Sort Modes (`{res.get('dbType', 'UNIQUE')}`, {len(rows)} metrics)",
            "- Common aliases: `name`, `dps`/`fulldps`, `combined`, `hit`, `dot`, `bleed`, `ignite`, "
            "`ehp`, `maxhit`, `life`/`hp`, `es`, `mana`, `armour`, `evasion`, `minion`/`miniondps`.",
            render_table(["Sort Mode", "Label (EN)", "Label (ZH)"], rows),
        ]
        return "\n".join(lines)

    if res.get("equipped"):
        sel = res.get("selectedItem") or {}
        sel_name = _fmt_item_title(sel)
        if sel_name == "-":
            sel_name = str(res.get("query") or "Item")
        lines = [f"### Item DB Equip: {sel_name} (Slot: `{res.get('slotName') or 'Auto'}`)"]
        tip = res.get("tooltip") or {}
        t_lines = tip.get("lines") or []
        if t_lines:
            lines.append("\n#### Native PoB Tooltip")
            for ln in t_lines:
                txt = (
                    strip_pob_escapes(ln.get("text") or ln.get("raw") or "")
                    if isinstance(ln, dict)
                    else strip_pob_escapes(str(ln))
                )
                if txt:
                    lines.append(f"  {txt}")
        mut = res.get("mutation")
        if isinstance(mut, dict):
            env_d = res.get("env") if isinstance(res.get("env"), dict) else {}
            slot_slug = res.get("buildSlot") or res.get("targetSlot") or env_d.get("slot")
            mut_payload = dict(mut)
            if slot_slug:
                mut_payload.setdefault("buildSlot", slot_slug)
                mut_payload.setdefault("targetSlot", slot_slug)
            if env_d and "env" not in mut_payload:
                mut_payload["env"] = env_d
            lines.append(
                "\n"
                + format_mutation_report(
                    f"Item DB Simulation ({sel_name})",
                    mut_payload,
                    target=str(slot_slug) if slot_slug else None,
                )
            )
        return "\n".join(lines)

    query = str(res.get("query") or "").strip()
    filters = res.get("filters") if isinstance(res.get("filters"), dict) else {}
    summary = _fmt_filter_summary(query, filters)
    total = int(res.get("total") or 0)
    db_type = str(res.get("dbType") or "UNIQUE")
    slot_note = str(res.get("slotNote") or "").strip()
    hint = str(res.get("hint") or "").strip()

    if res.get("statSort"):
        sort_mode = str(res.get("sortMode") or "FullDPS")
        base_str = _fmt_compact_metric(res.get("baselineValue"))
        rows = [
            [
                m.get("index"),
                _fmt_item_title(m),
                _fmt_item_base(m),
                _fmt_power_cell(m.get("measuredPower"), m.get("delta"), m.get("deltaPct")),
            ]
            for m in (res.get("matches") or [])
        ]
        lines = [
            f"### PoB Item DB Rank (`{db_type}` · `{sort_mode}`): {summary} ({total} hits)",
            f"- **Baseline (`{sort_mode}`)**: `{base_str}`",
            render_table(["#", "Unique", "Base", _power_col_header(sort_mode)], rows),
        ]
        if slot_note:
            lines.append(f"\n_{slot_note}_")
        note = str(res.get("templateNote") or "").strip()
        if note:
            lines.append(f"\n_{note}_")
        if hint:
            lines.append(f"\n> {hint}")
        return "\n".join(lines)

    rows = [
        [
            m.get("index"),
            _fmt_item_title(m),
            _fmt_item_base(m),
            m.get("primarySlot") or "-",
            m.get("league") or "-",
        ]
        for m in (res.get("matches") or [])
    ]
    lines = [
        f"### PoB Item DB Search (`{db_type}`): {summary} ({total} hits)",
        render_table(["#", "Title", "Base", "Primary Slot", "League"], rows),
    ]
    if slot_note:
        lines.append(f"\n_{slot_note}_")
    if hint:
        lines.append(f"\n> {hint}")
    return "\n".join(lines)
