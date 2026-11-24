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
        lines = [f"### Jewel Socket Radius: `{sock_id}` ({sock_name})"]
        radii_map = sr.get("radii")
        if isinstance(radii_map, dict):
            for r_name, r_nodes in radii_map.items():
                if not r_nodes:
                    continue
                lines.append(f"\n#### Radius `{r_name}` ({len(r_nodes)} nodes)")
                rows = [
                    [
                        n.get("id"),
                        strip_pob_escapes(n.get("nameZh") or n.get("name")),
                        n.get("type") or "Normal",
                        "Yes" if n.get("allocated") else "No",
                        "; ".join(str(s) for s in (n.get("statsZh") or n.get("stats") or [])[:2])[:80],
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
                    "; ".join(str(s) for s in (n.get("statsZh") or n.get("stats") or [])[:2])[:80],
                ]
                for n in nearby
            ]
            lines.append(render_table(["Node ID", "Name", "Type", "Allocated", "Radius", "Stats"], rows))
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
            stats_s = "; ".join(str(s) for s in (m.get("statsZh") or m.get("stats") or [])[:2])
            row: list[Any] = [
                m.get("id"),
                strip_pob_escapes(m.get("nameZh") or m.get("name")),
                m.get("type") or "Normal",
                "Yes" if m.get("allocated") else "No",
                m.get("cost") if m.get("cost") is not None else "-",
            ]
            if has_path:
                row.append(_format_path(m.get("path")))
            row.append(stats_s[:80])
            rows.append(row)
            if (masteries or m.get("masteryEffects")) and (m.get("masteryEffects") or m.get("effects")):
                mastery_nodes.append(m)

        label = search_label or ", ".join(str(q) for q in (res.get("queries") or [])) or "Filter"
        lines = [
            f"### Passive Tree Search: `{label}` ({res.get('totalMatches', len(matches))} matches)",
            render_table(headers, rows),
        ]
        for mn in mastery_nodes:
            lines.extend(_format_mastery_effects_lines(mn))
        return "\n".join(lines)

    # Default allocated passive tree summary
    pts = res.get("points") or {}
    lines = [
        f"### Passive Tree Summary: `{pts.get('used', 0)}/{pts.get('usedMax', 0)}` points | Ascendancy `{pts.get('ascUsed', 0)}/{pts.get('ascMax', 0)}`",
    ]
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
                "; ".join(str(s) for s in (n.get("statsZh") or n.get("stats") or [])[:2]),
            ]
            for n in items
        ]
        lines.append(render_table(["ID", "Name", "Stats"], rows))
        if key == "masteries" and (masteries or any(n.get("masteryEffects") for n in items)):
            for mn in items:
                lines.extend(_format_mastery_effects_lines(mn))

    return "\n".join(lines)
