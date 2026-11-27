"""Markdown renderer for poe.ninja price search results (L2)."""

from __future__ import annotations

from typing import Any

from .primitives import fmt_num, render_table


def render_single_price_markdown(res: dict[str, Any], query: str) -> str:
    """Render a single poe.ninja price lookup response dict as Markdown."""
    if res.get("error"):
        return f"### poe.ninja Price Search: `{query}` — Error: {res['error']}"

    matches = res.get("matches", [])
    is_det = bool(res.get("detailed")) or any(
        m.get("gemLevel") is not None or m.get("corrupted") is not None for m in matches
    )
    div_rate = res.get("divineChaosRate")
    rate_str = f" | 1 Divine = `{fmt_num(div_rate, 1)}` Chaos" if div_rate else ""
    det_tag = " [Detailed]" if is_det else ""
    alias = res.get("resolvedAlias")
    alias_note = f" -> `{alias['to']}`" if isinstance(alias, dict) and alias.get("to") else ""
    lines = [
        f"### poe.ninja Price Search{det_tag}: `{query}`{alias_note} (League: `{res.get('league')}`{rate_str}, {res.get('totalMatches', len(matches))} matches)"
    ]
    if res.get("stale"):
        lines.append("> [WARN] Serving stale cached poe.ninja price data due to network error.")
    if res.get("categoryFallback"):
        src_cat = res.get("category") or "requested"
        lines.append(
            f"> [NOTE] Category fallback: `{src_cat}` -> `{res['categoryFallback']}` (0 matches in `{src_cat}`)."
        )

    if is_det:
        rows = []
        for m in matches:
            lv = m.get("gemLevel")
            ql = m.get("gemQuality")
            lv_q = f"{lv}/{ql or 0}" if lv is not None else "-"
            corr = "Yes" if m.get("corrupted") is True else ("No" if m.get("corrupted") is False else "-")
            links = f"{m['links']}L" if m.get("links") else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or "-",
                    lv_q,
                    corr,
                    links,
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        lines.append(
            render_table(
                ["Item / Currency", "Category", "Variant", "Lvl/Qual", "Corrupted", "Links", "Chaos", "Divine", "Listings"],
                rows,
            )
        )
    else:
        rows = []
        for m in matches:
            trend = m.get("trend24hPct")
            trend_str = f"{float(trend):+.1f}%" if trend is not None else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or (f"{m['links']}L" if m.get("links") else "-"),
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    trend_str,
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        lines.append(
            render_table(["Item / Currency", "Category", "Variant / Links", "Chaos", "Divine", "24h Trend", "Listings"], rows)
        )
    return "\n".join(lines)
