"""`pob rank-nodes`: passive ROI ranking, multi-objective `--score/--floor`, and `--refund` of allocated nodes."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import _emit_json, _emit_text, _output_options, _target_option, invoke_daemon


@click.command("rank-nodes")
@click.option("--stat", default="CombinedDPS", show_default=True, help="Target stat (CombinedDPS, FullDPS, TotalEHP, Life, etc.).")
@click.option("--max-dist", type=int, default=5, show_default=True, help="Maximum path distance in points.")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top candidates to show.")
@click.option("--score", "score_terms", multiple=True, help="Rank by weighted % gain over several stats, STAT=W (repeatable or comma-joined); replaces --stat.")
@click.option("--floor", "floor_terms", multiple=True, help="Drop nodes whose result breaks STAT>=N or STAT<=N (repeatable).")
@click.option(
    "--refund",
    is_flag=True,
    help="Rank ALLOCATED passives by score lost per refunded point (cheapest respec first); default score primaryDps=1,totalEhp=1.",
)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_rank_nodes(
    stat: str,
    max_dist: int,
    top: int,
    score_terms: tuple[str, ...],
    floor_terms: tuple[str, ...],
    refund: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Rank unallocated passive tree nodes by ROI per point (`pathPower`) and total gain (`power`)."""
    from .formatters import fmt_num, render_table, strip_pob_escapes
    from .scoring import floor_to_json, parse_floors, parse_score, stat_keys

    if refund:
        _run_refund(score_terms, floor_terms, top=top, target=target, as_json=as_json, compact=compact, out_path=out_path)
        return
    try:
        score, floors = parse_score(score_terms), parse_floors(floor_terms)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    params: dict[str, Any] = {"target": target, "stat": stat, "max_dist": max_dist, "top": top}
    if score or floors:
        params.update(score=score, floors=floor_to_json(floors))
    # One engine node_power pass per scored stat.
    passes = len(stat_keys(score or {stat: 1.0}, floors)) if (score or floors) else 1
    res = invoke_daemon("rank_nodes", params, timeout=180.0 * passes)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    if res.get("stat") == "score":
        _emit_text(_format_scored_nodes(res, max_dist=max_dist), out_path=out_path)
        return

    rows = []
    for idx, r in enumerate(res.get("byPerPoint", []), start=1):
        rows.append(
            [
                idx,
                r.get("id"),
                strip_pob_escapes(r.get("nameZh") or r.get("name")),
                r.get("type"),
                r.get("pathDist"),
                strip_pob_escapes(r.get("pathPowerStr") or fmt_num(r.get("pathPower"), 3)),
                strip_pob_escapes(r.get("powerStr") or fmt_num(r.get("power"), 3)),
            ]
        )
    body = (
        f"### Passive Node ROI Ranking (Stat: `{res.get('stat')}`, Max Dist: `{max_dist}` pts)\n"
        + render_table(["#", "Node ID", "Name", "Type", "Cost", "Per-Point Gain", "Total Gain"], rows)
    )
    if res.get("stat_fallback"):
        body += f"\n_Stat fallback applied: `{stat}` -> `{res['stat_fallback']}`_"

    sock_summary = res.get("socketSummary")
    raw_nearby = res.get("nearbySockets")
    if isinstance(sock_summary, dict) and (sock_summary.get("allocatedEmptyIds") or []):
        empty_str = ", ".join(str(x) for x in sock_summary["allocatedEmptyIds"])
        body += f"\n\n> **[EMPTY-ALLOCATED-SOCKET]** Allocated jewel socket(s) with no jewel: `nodeId={empty_str}`."
    if isinstance(raw_nearby, list) and raw_nearby:
        from .analyzer.tree_sockets import filter_sockets_for_view, socket_display_kind

        sock_cands, _ = filter_sockets_for_view(raw_nearby, sock_summary if isinstance(sock_summary, dict) else None, mode="rank_nodes", max_rows=5)
        if sock_cands:
            s_rows = [
                [
                    s.get("nodeId"),
                    socket_display_kind(s),
                    s.get("cost"),
                    "; ".join(s.get("travelStats") or [])[:70] or "-",
                ]
                for s in sock_cands
            ]
            body += (
                "\n\n### Nearby Unallocated Jewel & Cluster Sockets (Unranked — no synthetic DPS)\n"
                + render_table(["Node ID", "Kind / Sector", "Cost", "Travel Stats"], s_rows)
            )
    _emit_text(body, out_path=out_path)


def _format_scored_nodes(res: dict[str, Any], *, max_dist: int) -> str:
    from .formatters import render_table, strip_pob_escapes

    def _pcts(p: dict[str, Any]) -> str:
        return " / ".join(f"{k} {v:+.2f}%" if v is not None else f"{k} -" for k, v in p.items())

    rows = [
        [
            idx,
            r.get("id"),
            strip_pob_escapes(r.get("nameZh") or r.get("name")),
            r.get("type"),
            r.get("pathDist"),
            f"{r['scorePerPoint']:+.3f}",
            f"{r['score']:+.3f}",
            _pcts(r.get("pcts") or {}),
        ]
        for idx, r in enumerate(res.get("byPerPoint", []), start=1)
    ]
    terms = " + ".join(f"{w:g}×{k}%" for k, w in (res.get("score") or {}).items())
    body = (
        f"### Passive Node Score Ranking (Score: `{terms}`, Max Dist: `{max_dist}` pts)\n"
        + render_table(["#", "Node ID", "Name", "Type", "Cost", "Score/Point", "Score", "Gain %"], rows)
    )
    if res.get("floors"):
        body += f"\n_Floors: {', '.join(res['floors'])}; {res.get('floorExcluded', 0)} node(s) dropped for breaking one._"
    return body


def _run_refund(
    score_terms: tuple[str, ...],
    floor_terms: tuple[str, ...],
    *,
    top: int,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    from .cli_weights import _parse_score_options
    from .scoring import floor_to_json

    # Refund reads the sweep summary, so validate against its stat names before the slow sweep.
    score, floors = _parse_score_options(score_terms, floor_terms)
    params: dict[str, Any] = {"target": target, "top": top, "refund": True, "score": score, "floors": floor_to_json(floors)}
    res = invoke_daemon("rank_nodes", params, timeout=600.0)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_refund_markdown(res), out_path=out_path)


def format_refund_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table, restore_warning, strip_pob_escapes

    def _pcts(p: dict[str, Any]) -> str:
        return " / ".join(f"{k} {v:+.2f}%" if v is not None else f"{k} -" for k, v in p.items())

    rows = [
        [
            idx,
            r.get("id"),
            strip_pob_escapes(r.get("nameZh") or r.get("name")),
            r.get("type"),
            r.get("pointsRefunded"),
            f"{r['lossPerPoint']:+.3f}",
            _pcts(r.get("pcts") or {}),
        ]
        for idx, r in enumerate(res.get("cheapest", []), start=1)
    ]
    terms = " + ".join(f"{w:g}×{k}%" for k, w in (res.get("score") or {}).items())
    lines = [
        f"### Passive Refund Ranking (Score: `{terms}`; lowest loss per point first)",
        f"- Checked {res.get('allocatedChecked', 0)} allocated passives; {res.get('refundable', 0)} refundable alone"
        + (f"; {res['floorBlocked']} blocked by floors {', '.join(res['floors'])}" if res.get("floors") else ""),
        render_table(["#", "Node ID", "Name", "Type", "Points", "Loss/Point", "Change %"], rows),
        "> Loss/Point <= 0 means the node gives nothing to the score: refund it first.",
        "> Nodes that would orphan a larger branch are skipped (see `notRefundable` in --json).",
    ]
    warn = restore_warning(res.get("restoreCheck"))
    if warn:
        lines.append(f"> [WARN] {warn}")
    return "\n".join(lines)


@click.command("respec")
@click.option("--score", "score_terms", multiple=True, help="Objective, STAT=W over sweep summary stats (default primaryDps=1,totalEhp=1).")
@click.option("--floor", "floor_terms", multiple=True, help="Reject swaps whose result breaks STAT>=N or STAT<=N (repeatable).")
@click.option("--rounds", type=click.IntRange(1, 20), default=3, show_default=True, help="Maximum refund+allocate swaps.")
@click.option("--max-dist", type=int, default=5, show_default=True, help="Maximum path distance for the allocated node.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_respec(
    score_terms: tuple[str, ...],
    floor_terms: tuple[str, ...],
    rounds: int,
    max_dist: int,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Plan greedy passive swaps (refund cheapest, allocate best), each PoB-verified; build left unchanged."""
    from .cli_weights import _parse_score_options
    from .scoring import floor_to_json

    score, floors = _parse_score_options(score_terms, floor_terms)
    params: dict[str, Any] = {"target": target, "score": score, "floors": floor_to_json(floors), "rounds": rounds, "max_dist": max_dist}
    res = invoke_daemon("respec", params, timeout=600.0 + 300.0 * rounds)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_respec_markdown(res, target=target), out_path=out_path)


def format_respec_markdown(res: dict[str, Any], *, target: str) -> str:
    import json

    from .formatters import render_table, restore_warning, strip_pob_escapes

    def _nm(n: dict[str, Any]) -> str:
        return f"{strip_pob_escapes(n.get('nameZh') or n.get('name'))} ({n.get('id')})"

    terms = " + ".join(f"{w:g}×{k}%" for k, w in (res.get("score") or {}).items())
    lines = [f"### Respec Plan (`{target}`, Score: `{terms}`)"]
    rounds = res.get("rounds") or []
    if not rounds:
        lines.append("- No swap improves the score without breaking a floor or using more points.")
    else:
        rows = [[i, _nm(r["refund"]), _nm(r["add"]), f"{r['gain']:+.3f}", r.get("pointsDelta")] for i, r in enumerate(rounds, 1)]
        lines.append(render_table(["#", "Refund", "Allocate", "Score Gain", "Points Δ"], rows))
        before, after = res.get("before") or {}, res.get("after") or {}
        lines.append("- Total score gain: " + f"{res.get('totalGain', 0):+.3f}" + "; " + ", ".join(
            f"{k} {before.get(k) if before.get(k) is not None else '-'} -> {after.get(k) if after.get(k) is not None else '-'}" for k in after
        ))
        lines.append("- Apply: `pob batch --json-ops '" + json.dumps(res.get("ops") or [], ensure_ascii=False) + "' --apply`")
    warn = restore_warning(res.get("restoreCheck"))
    if warn:
        lines.append(f"> [WARN] {warn}")
    return "\n".join(lines)


def register_rank_nodes_commands(group: click.Group) -> None:
    group.add_command(cmd_rank_nodes)
    group.add_command(cmd_respec)
