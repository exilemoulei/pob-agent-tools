"""Markdown formatter for `pob tree rank-wheels` with strictly separated `[Proxy Benchmark]` socket section."""

from __future__ import annotations

from typing import Any

from .primitives import fmt_num, render_table, strip_pob_escapes


def _fmt_pct(val: Any) -> str:
    if val is None:
        return "-"
    try:
        return f"{float(val):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def format_wheel_rank_markdown(res: dict[str, Any]) -> str:
    """Format `pob tree rank-wheels` keeping Real Build Wheels and `[Proxy Benchmark]` sockets strictly separated."""
    dps_key = res.get("dpsKey") or "CombinedDPS"
    sort_by = res.get("sort") or "dps"
    elapsed = res.get("elapsedMs")
    time_tag = f" | `{elapsed:.0f}ms`" if isinstance(elapsed, (int, float)) else ""
    tags_str = ", ".join(res.get("buildTags") or []) or "general"
    filter_note = (
        f"all `{res.get('totalWheelsScanned', 0)}` wheels (`--all-tags`)"
        if res.get("allTags")
        else f"`{res.get('tagFilteredWheels', 0)}/{res.get('totalWheelsScanned', 0)}` tag-matched wheels (tags: `{tags_str}`)"
    )

    lines: list[str] = [
        f"### Real Build Wheel Ranking (`{dps_key}` base `{fmt_num(res.get('baseDps'))}`, EHP `{fmt_num(res.get('baseEhp'))}`, sort: `{sort_by}`{time_tag})",
        f"- **Scope**: {filter_note} | evaluated `{res.get('evaluatedPackages', 0)}` packages (`max-dist={res.get('maxDist')}`, `max-pts={res.get('maxPts')}`)",
    ]

    wheels = res.get("wheels") or []
    if wheels:
        rows: list[list[Any]] = []
        for idx, w in enumerate(wheels, start=1):
            w_name = strip_pob_escapes(str(w.get("wheelNameZh") or w.get("wheelName") or "-"))
            mast = w.get("mastery")
            if isinstance(mast, dict) and mast.get("allocSpec"):
                m_st = "; ".join((mast.get("statsZh") or mast.get("stats") or [])[:1])
                if len(m_st) > 48:
                    m_st = m_st[:45] + "..."
                w_disp = f"{w_name} + Mastery `{mast['allocSpec']}` ({m_st})" if m_st else f"{w_name} + Mastery `{mast['allocSpec']}`"
            else:
                w_disp = w_name
            cost_disp = f"{w.get('totalPoints', 0)} ({w.get('travelCost', 0)}+{w.get('wheelCost', 0)})"
            notes = "; ".join(w.get("notes") or []) or "-"
            rows.append(
                [
                    idx,
                    w_disp,
                    cost_disp,
                    _fmt_pct(w.get("dpsPct")),
                    _fmt_pct(w.get("dpsPctPerPt")),
                    _fmt_pct(w.get("ehpPct")),
                    _fmt_pct(w.get("ehpPctPerPt")),
                    notes,
                    f"`{w.get('allocateCommand') or '-'}`",
                ]
            )
        lines.append(
            render_table(
                ["#", "Wheel (Notable + Best Mastery)", "Cost (T+W)", "ΔDPS%", "ΔDPS%/pt", "ΔEHP%", "ΔEHP%/pt", "Guardrail / Threshold Notes", "Allocate Spec"],
                rows,
            )
        )
    else:
        lines.append("- _(No positive reachable wheel packages found within distance/point limits.)_")

    vetoed = res.get("vetoedWheels") or []
    if vetoed:
        sample = "; ".join(
            f"{strip_pob_escapes(str(v.get('wheelName') or '-'))} {v.get('vetoReason') or '[VETO]'}"
            for v in vetoed[:5]
        )
        lines.append(f"\n_Vetoed `{len(vetoed)}` wheel package(s): {sample}_")

    # Strictly isolated `[Proxy Benchmark]` section (`R5` Iron Law) — NEVER mixed into Real Build Wheel Ranking!
    proxies = res.get("proxySocketBenchmarks")
    if isinstance(proxies, list) and proxies:
        lines.append("\n### Reachable Socket Reference ROI [Proxy Benchmark]")
        lines.append(
            "_Note: Values below use explicit tag-matched proxy templates (NOT your real inventory) to benchmark socket ROI per point._"
        )
        p_rows: list[list[Any]] = []
        for p in proxies[:8]:
            kind_lbl = str(p.get("kind") or "basic")
            if p.get("sector"):
                kind_lbl = f"{kind_lbl} ({p['sector']})"
            path_csv = ",".join(str(x) for x in (p.get("pathIds") or [p.get("nodeId")]))
            path_cmd = p.get("pathCommand") or f"pob tree change --allocate {path_csv}"
            p_rows.append(
                [
                    f"[Proxy Benchmark] `{p.get('nodeId')}`",
                    kind_lbl,
                    p.get("templateSummary") or "-",
                    p.get("pointsBreakdown") or f"{p.get('assumedPoints')} pts",
                    _fmt_pct(p.get("dpsPctPerPt")),
                    _fmt_pct(p.get("ehpPctPerPt")),
                    f"`{path_cmd}`",
                ]
            )
        lines.append(
            render_table(
                ["Socket", "Kind / Sector", "Proxy Template", "Assumed Budget", "ΔDPS%/pt", "ΔEHP%/pt", "Path Spec"],
                p_rows,
            )
        )

    return "\n".join(lines)
