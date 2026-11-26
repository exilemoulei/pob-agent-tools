"""Markdown formatters for passive tree inspection, search, masteries, and socket radii."""

from __future__ import annotations

from typing import Any

from .primitives import fmt_num, render_table, strip_pob_escapes


def _format_path(path_val: Any) -> str:
    if not path_val:
        return "-"
    if isinstance(path_val, list):
        parts = []
        for item in path_val:
            if isinstance(item, dict):
                nid = item.get("id")
                nm = strip_pob_escapes(item.get("nameZh") or item.get("name") or "")
                parts.append(f"{nid}({nm})" if nm else str(nid))
            else:
                parts.append(str(item))
        return " -> ".join(parts)
    return str(path_val)


def _format_mastery_effects_lines(node: dict[str, Any]) -> list[str]:
    effects = node.get("masteryEffects") or node.get("effects") or []
    if not effects:
        return []
    nid = node.get("id")
    nname = strip_pob_escapes(node.get("nameZh") or node.get("name") or f"Mastery {nid}")
    sel_id = node.get("selectedEffect") or node.get("selectedEffectId") or node.get("effectId")
    out = [f"\n##### Mastery `{nid}` — {nname}"]
    for eff in effects:
        if isinstance(eff, dict):
            eid = eff.get("effect") or eff.get("effectId") or eff.get("id")
            stats_list = eff.get("statsZh") or eff.get("stats") or []
            if isinstance(stats_list, str):
                stats_txt = stats_list
            else:
                stats_txt = "; ".join(strip_pob_escapes(str(s)) for s in stats_list)
            marker = " **[SELECTED]**" if (sel_id is not None and str(eid) == str(sel_id)) else ""
            out.append(f"- `{nid}:{eid}`{marker}: {stats_txt or '-'}")
        else:
            out.append(f"- {strip_pob_escapes(str(eff))}")
    return out


def _format_node_stats(node: dict[str, Any], *, max_len: int | None = None) -> str:
    stats_list = node.get("statsZh") or node.get("stats") or []
    if not stats_list and node.get("overrideWhy") == "conquered":
        return "_(0 stats — conquered)_"
    text = "; ".join(str(s) for s in stats_list[:2])
    return text[:max_len] if max_len is not None else text


def _tier_exceeds_equipped_radius(
    r_name: str,
    r_nodes: list[dict[str, Any]],
    sr: dict[str, Any],
) -> bool:
    eq_max_r = sr.get("equippedMaxRadius")
    if not isinstance(eq_max_r, (int, float)) or isinstance(eq_max_r, bool):
        return False
    thresholds = sr.get("radiusThresholds")
    if isinstance(thresholds, dict) and r_name in thresholds:
        try:
            return float(thresholds[r_name]) > float(eq_max_r)
        except (TypeError, ValueError):
            pass
    return any(n.get("withinEquippedRadius") is False for n in r_nodes if isinstance(n, dict))


def format_tree_markdown(
    res: dict[str, Any],
    *,
    search_label: str | None = None,
    show_path: bool = False,
    masteries: bool = False,
) -> str:
    """Format passive tree inspection, search/filter, mastery effect lists, and socket radius queries."""
    # Socket radius view
    sr = res.get("socketRadius") or res.get("socket")
    if isinstance(sr, dict) and (sr.get("nodes") is not None or sr.get("radii") is not None or res.get("nearbyNodes") is not None):
        sock_id = sr.get("id") or sr.get("nodeId") or "-"
        sock_name = strip_pob_escapes(sr.get("nameZh") or sr.get("name") or f"Socket {sock_id}")
        hdr = f"### Jewel Socket Radius: `{sock_id}` ({sock_name})"
        eq_jewel = strip_pob_escapes(sr.get("equippedJewel") or "").strip()
        eq_r_label = strip_pob_escapes(sr.get("equippedRadiusLabel") or "").strip()
        if eq_jewel and eq_r_label:
            hdr += f" — Equipped: {eq_jewel} (Active Radius: {eq_r_label})"
        elif eq_jewel:
            hdr += f" — Equipped: {eq_jewel}"
        lines = [hdr]
        radii_map = sr.get("radii")
        if isinstance(radii_map, dict):
            for r_name, r_nodes in radii_map.items():
                if not r_nodes:
                    continue
                sec_hdr = f"\n#### Radius `{r_name}` ({len(r_nodes)} nodes)"
                if eq_r_label and _tier_exceeds_equipped_radius(r_name, r_nodes, sr):
                    sec_hdr += f" — outside equipped {eq_r_label} radius (not affected by this jewel)"
                lines.append(sec_hdr)
                rows = [
                    [
                        n.get("id"),
                        strip_pob_escapes(n.get("nameZh") or n.get("name")),
                        n.get("type") or "Normal",
                        "Yes" if n.get("allocated") else "No",
                        _format_node_stats(n, max_len=80),
                    ]
                    for n in r_nodes
                ]
                lines.append(render_table(["Node ID", "Name", "Type", "Allocated", "Stats"], rows))
        else:
            nearby = sr.get("nodes") or res.get("nearbyNodes") or res.get("matches") or []
            rows = [
                [
                    n.get("id"),
                    strip_pob_escapes(n.get("nameZh") or n.get("name")),
                    n.get("type") or "Normal",
                    "Yes" if n.get("allocated") else "No",
                    n.get("radius") or n.get("ring") or (fmt_num(n.get("distance"), 1) if n.get("distance") is not None else "-"),
                    _format_node_stats(n, max_len=80),
                ]
                for n in nearby
            ]
            lines.append(render_table(["Node ID", "Name", "Type", "Allocated", "Radius", "Stats"], rows))
        if res.get("allocationMismatchHint"):
            lines.append(f"\n> **Hint**: {strip_pob_escapes(str(res['allocationMismatchHint']))}")
        return "\n".join(lines)

    # Search / filtered matches view
    matches = res.get("matches")
    if search_label or matches is not None:
        matches = matches or []
        has_path = show_path or any(m.get("path") for m in matches)
        headers = ["Node ID", "Name", "Type", "Allocated", "Path Cost"]
        if has_path:
            headers.append("Path")
        headers.append("Stats")

        rows = []
        mastery_nodes: list[dict[str, Any]] = []
        for m in matches:
            row: list[Any] = [
                m.get("id"),
                strip_pob_escapes(m.get("nameZh") or m.get("name")),
                m.get("type") or "Normal",
                "Yes" if m.get("allocated") else "No",
                m.get("cost") if m.get("cost") is not None else "-",
            ]
            if has_path:
                row.append(_format_path(m.get("path")))
            row.append(_format_node_stats(m, max_len=80))
            rows.append(row)
            if (masteries or m.get("masteryEffects")) and (m.get("masteryEffects") or m.get("effects")):
                mastery_nodes.append(m)

        label = search_label or ", ".join(str(q) for q in (res.get("queries") or [])) or "Filter"
        lines = [
            f"### Passive Tree Search: `{label}` ({res.get('totalMatches', len(matches))} matches)",
            render_table(headers, rows),
        ]
        if res.get("allocationMismatchHint"):
            lines.append(f"\n> **Hint**: {strip_pob_escapes(str(res['allocationMismatchHint']))}")
        for mn in mastery_nodes:
            lines.extend(_format_mastery_effects_lines(mn))
        return "\n".join(lines)

    # Default allocated passive tree summary
    pts = res.get("points") or {}
    lines = [
        f"### Passive Tree Summary: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` points | Ascendancy `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}`",
    ]
    conquered_zero_normals = [
        n
        for n in (res.get("conqueredSmallPassives") or [])
        if isinstance(n, dict) and not (n.get("statsZh") or n.get("stats"))
    ]
    if conquered_zero_normals:
        by_name: dict[str, list[str]] = {}
        for n in conquered_zero_normals:
            nm = strip_pob_escapes(n.get("nameZh") or n.get("name") or "Price of Glory").strip() or "Price of Glory"
            by_name.setdefault(nm, []).append(str(n.get("id")))
        parts = [f"{nm}: {', '.join(ids)}" for nm, ids in by_name.items()]
        lines.append(
            f"- **Conquered 0-Stat Small Passives**: {len(conquered_zero_normals)} allocated ({'; '.join(parts)})"
        )
    for title, key in (
        ("Keystones", "keystones"),
        ("Ascendancy Nodes", "ascendancyNodes"),
        ("Masteries", "masteries"),
        ("Notables", "notables"),
    ):
        items = res.get(key) or []
        lines.append(f"\n#### {title} ({len(items)})")
        rows = [
            [
                n["id"],
                strip_pob_escapes(n.get("nameZh") or n.get("name")),
                _format_node_stats(n),
            ]
            for n in items
        ]
        lines.append(render_table(["ID", "Name", "Stats"], rows))
        if key == "masteries" and (masteries or any(n.get("masteryEffects") for n in items)):
            for mn in items:
                lines.extend(_format_mastery_effects_lines(mn))

    jewel_sockets = [s for s in (res.get("jewelSockets") or []) if isinstance(s, dict) and s.get("kind") != "charm"]
    nearby_sockets = [s for s in (res.get("nearbySockets") or []) if isinstance(s, dict)]
    sock_sum = res.get("socketSummary") or {}
    if jewel_sockets or nearby_sockets:
        alloc_socks = [s for s in jewel_sockets if s.get("allocated")]
        unalloc_nearby = [s for s in nearby_sockets if not s.get("allocated")]
        empty_cnt = len(sock_sum.get("allocatedEmptyIds") or [s for s in alloc_socks if not s.get("occupied")])
        near3_cnt = sum(1 for s in unalloc_nearby if int(s.get("cost") or 99) <= 3)
        lines.append(
            f"\n#### Jewel Sockets (Allocated: {len(alloc_socks)}, Empty: {empty_cnt} | Nearby <=3pt: {near3_cnt})"
        )
        sock_rows: list[list[Any]] = []
        for s in alloc_socks:
            idx_lbl = f"#{s['socketIndex']}" if s.get("socketIndex") else "-"
            kind_lbl = str(s.get("kind") or "basic")
            if s.get("sector"):
                kind_lbl = f"{kind_lbl} ({s['sector']})"
            status_lbl = (
                strip_pob_escapes(s.get("nameZh") or s.get("name") or "Occupied")
                if s.get("occupied") or s.get("itemId") or s.get("name")
                else "[EMPTY]"
            )
            sock_rows.append([s.get("nodeId"), idx_lbl, kind_lbl, "0", status_lbl])
        for s in unalloc_nearby[:6]:
            kind_lbl = str(s.get("kind") or "basic")
            if s.get("sector"):
                kind_lbl = f"{kind_lbl} ({s['sector']})"
            travel = "; ".join(s.get("travelStats") or [])[:60]
            note = f"Unallocated ({travel})" if travel else "Unallocated"
            sock_rows.append([s.get("nodeId"), "-", kind_lbl, s.get("cost"), note])
        lines.append(render_table(["Node ID", "Slot", "Kind / Sector", "Cost", "Status / Travel"], sock_rows))

    return "\n".join(lines)
