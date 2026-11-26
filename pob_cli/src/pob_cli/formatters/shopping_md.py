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


def _fmt_gold_tag(it: dict[str, Any]) -> str:
    if it.get("tradeMode") != "securable":
        return ""
    fee = it.get("goldFee")
    if fee is None:
        return " [一口价]"
    if int(fee) >= 1000:
        return f" [一口价 · {int(fee) / 1000.0:.1f}kG]"
    return f" [一口价 · {int(fee)}G]"


def format_appraise_markdown(res: dict[str, Any]) -> str:
    """Render 3-tier build valuation report as scannable Markdown."""
    div_rate = res.get("divineRate")
    rate_src = str(res.get("divineRateSource") or "")
    realm = str(res.get("realm") or "global")
    tc = res.get("tierCounts") or {}
    tot_str = fmt_price_pair(res.get("totalChaos"), div_rate)
    unpriced_n = int(res.get("unpricedCount") or 0)
    unpriced_s = f" (+ {unpriced_n} unpriced item(s))" if unpriced_n > 0 else ""
    rate_note = ""
    if rate_src == "static_fallback":
        rate_note = " (static fallback; poe.ninja does not index TW)" if realm == "tw" else " (static fallback)"
    elif rate_src == "env":
        rate_note = " (env override)"
    rate_s = f"1d = {fmt_num(div_rate, 1)}c{rate_note}" if div_rate else "-"
    lines = [
        f"### PoE1 Build Appraisal (`{res.get('league')}` · `{realm}` · status=`{res.get('status')}`)",
        f"- **Estimated Known Total**: **{tot_str}**{unpriced_s} (`{rate_s}` | Tier A=`{tc.get('A', 0)}`, Tier B Live=`{tc.get('B_live', 0)}`, Tier B Floor=`{tc.get('B_fallback', 0)}`, Tier C Crafted=`{tc.get('C', 0)}`)",
        f"- **Live Trade Queries**: `{res.get('liveQueriesUsed', 0)}/{res.get('maxLiveQueries', 0)}` | **Elapsed**: `{res.get('elapsedMs', 0)}ms`",
    ]
    if res.get("rateLimitedFallback"):
        lines.append(
            "> [WARN] Trade API rate limit reached mid-batch; already-fetched Tier B prices are preserved and remaining items fell back to poe.ninja floor + L0 links."
        )
    for warn in res.get("warnings") or ():
        lines.append(f"> [WARN] {warn}")

    sec_totals = res.get("sectionTotals") or {}
    parts = [
        f"{k}={fmt_price_pair(v.get('chaos'), div_rate)}"
        for k, v in sec_totals.items()
        if isinstance(v, dict) and (v.get("chaos") or 0) > 0
    ]
    if parts:
        lines.append("- **By Section**: " + " | ".join(parts))

    anoint = res.get("anoint")
    if isinstance(anoint, dict) and anoint.get("notable"):
        oils = " + ".join(anoint.get("oils") or [])
        a_cost = fmt_price_pair(anoint.get("priceChaos"), div_rate)
        lines.append(f"- **Amulet Anoint**: `{anoint['notable']}` ({oils or 'oils unknown'}) -> `{a_cost}`")

    items = res.get("items") or []
    priced_items = [it for it in items if it.get("tier") != "C"]
    if priced_items:
        lines.append(f"\n#### Priced Uniques, Jewels & Key Gems ({len(priced_items)})")
        rows: list[list[Any]] = []
        for it in priced_items:
            t_lbl = "B-Live" if it.get("liveVerified") else ("B-Floor" if it.get("tier") == "B" else "A")
            floor_s = fmt_price_pair(it.get("ninjaFloorChaos"), div_rate)
            single_tag = " [1 seller]" if it.get("singleListingWarning") else ""
            if it.get("singleListingWarning"):
                t_lbl = f"{t_lbl} (1 seller)"
            price_s = fmt_price_pair(it.get("priceChaos"), div_rate) + _fmt_gold_tag(it) + single_tag
            delta_pct = it.get("deltaVsFloorPct")
            delta_s = f"{float(delta_pct):+.0f}%" if delta_pct is not None else "-"
            link_url = str(it.get("sellerUrl") or it.get("url") or "")
            link_cell = f"[Buy]({link_url})" if link_url else "-"
            rows.append([it.get("slot") or "-", it.get("name") or "-", _item_badges(it), t_lbl, floor_s, price_s, delta_s, link_cell])
        lines.append(render_table(["Slot", "Item", "Tags", "Tier", "Ninja Floor", "Appraised", "vs Floor", "Link"], rows))

    tier_c_items = [it for it in items if it.get("tier") == "C"]
    if tier_c_items:
        lines.append(f"\n#### Tier C: Crafted Rares & Magic Flasks — Not Auto-Queried ({len(tier_c_items)})")
        c_rows: list[list[Any]] = []
        for it in tier_c_items:
            notes = "; ".join(str(n) for n in (it.get("notes") or [])[:2]) or "-"
            url = str(it.get("url") or "")
            link_cell = f"[L0 Trade]({url})" if url else "-"
            cmd = f"`{it.get('searchCommand') or '-'}`"
            c_rows.append([it.get("slot") or "-", it.get("name") or "-", notes, link_cell, cmd])
        lines.append(render_table(["Slot", "Base / Item", "Flags", "L0 Link", "Deep-Dive Command"], c_rows))

    return "\n".join(lines)
