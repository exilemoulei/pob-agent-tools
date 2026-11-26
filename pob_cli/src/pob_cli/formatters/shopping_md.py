"""Markdown formatters for `pob trade shopping-list` and `pob trade appraise`."""

from __future__ import annotations

from typing import Any

from .primitives import fmt_num, render_table

_SECTION_LABELS: tuple[tuple[str, str], ...] = (
    ("gear", "Equipped Gear"),
    ("flask", "Flasks"),
    ("jewel", "Active Jewels"),
    ("gem", "Key Skill & Support Gems"),
)


def _item_badges(it: dict[str, Any]) -> str:
    tags: list[str] = []
    if it.get("foulborn"):
        tags.append("Foulborn")
    if it.get("links") and int(it["links"]) >= 5:
        tags.append(f"{it['links']}L")
    if it.get("corrupted"):
        tags.append("Corrupted")
    if it.get("kind") == "gem":
        lv = it.get("gemLevel")
        ql = it.get("gemQuality") or 0
        qty = int(it.get("quantity") or 1)
        qty_s = f" x{qty}" if qty > 1 else ""
        tags.append(f"{lv}/{ql}{qty_s}")
    return " · ".join(tags) if tags else "-"


def format_shopping_list_markdown(res: dict[str, Any]) -> str:
    """Render L0 zero-HTTP full-build shopping list as scannable Markdown tables."""
    build_info = res.get("buildInfo") if isinstance(res.get("buildInfo"), dict) else {}
    cls_str = " ".join(
        p for p in (build_info.get("ascendClassName") or build_info.get("className"),) if p
    ).strip()
    lv_str = f" Lv{build_info['level']}" if build_info.get("level") else ""
    build_lbl = f" — `{cls_str}{lv_str}`" if cls_str else ""

    lines = [
        f"### PoE1 Build Shopping List (`{res.get('league')}` · `{res.get('realm')}` · status=`{res.get('status')}`){build_lbl}",
        f"- **Items**: `{res.get('totalItems', 0)}` | **Trade API HTTP Requests**: `{res.get('httpRequests', 0)}` | **Elapsed**: `{res.get('elapsedMs', 0)}ms`",
    ]
    anoint = res.get("anoint")
    if isinstance(anoint, dict) and anoint.get("notable"):
        oils = anoint.get("oils") or []
        oil_s = f" (`{' + '.join(oils)}`)" if oils else ""
        lines.append(f"- **Amulet Anoint**: `{anoint['notable']}`{oil_s}")
    for warn in res.get("warnings") or ():
        lines.append(f"> [WARN] {warn}")

    items = res.get("items") or []
    for sec_key, sec_title in _SECTION_LABELS:
        sec_items = [it for it in items if it.get("section") == sec_key]
        if not sec_items:
            continue
        lines.append(f"\n#### {sec_title} ({len(sec_items)})")
        rows: list[list[Any]] = []
        for it in sec_items:
            name = str(it.get("name") or "-")
            base = str(it.get("baseType") or "")
            disp = f"{name} ({base})" if (base and base.lower() != name.lower() and sec_key != "gem") else name
            notes = "; ".join(str(n) for n in (it.get("notes") or [])[:3]) or "-"
            if len(notes) > 72:
                notes = notes[:69] + "..."
            url = str(it.get("url") or "")
            link_cell = f"[Trade]({url})" if url else "-"
            rows.append([it.get("slot") or "-", disp, _item_badges(it), notes, link_cell])
        lines.append(render_table(["Slot", "Item", "Tags", "Key Filters", "Link"], rows))

    if not items:
        lines.append("\n_(no equipped items found in active build slot)_")
    lines.append(
        "\n> Links are generated locally (0 GGG Trade API calls) and open directly on the official Trade site."
    )
    return "\n".join(lines)


def fmt_price_pair(chaos: float | None, div_rate: float | None) -> str:
    if chaos is None:
        return "-"
    if div_rate and div_rate > 0 and chaos >= div_rate * 0.5:
        return f"{fmt_num(chaos / div_rate, 2)}d ({fmt_num(chaos, 0)}c)"
    return f"{fmt_num(chaos, 0)}c"
