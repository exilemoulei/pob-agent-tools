"""`pob trade plan`: budget-limited upgrade plan across slots from saved `pob trade search --json` results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from ..cli_common import _emit_json, _emit_text, _output_options
from ..formatters import render_table
from ..scoring import parse_score
from ..trade_eval.budget import PLAN_STATS, plan_budget
from .other_cmds import trade_group


def _load(paths: tuple[str, ...]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for raw in paths:
        p = Path(raw)
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise click.ClickException(f"Cannot read trade search JSON {raw}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
            raise click.ClickException(f"{raw} is not `pob trade search --json` output (no candidates list).")
        payloads.append(data)
    return payloads


def _fmt_pct(v: Any) -> str:
    return f"{v:+.1f}%" if isinstance(v, (int, float)) else "-"


@trade_group.command("plan")
@click.argument("search_files", nargs=-1, required=True)
@click.option("--budget", type=float, required=True, help="Total chaos to spend across all slots.")
@click.option("--score", "score_terms", multiple=True, help="Objective over PoB-verified %: dps=W,ehp=W (default dps=1,ehp=0.25).")
@click.option("--top", type=click.IntRange(min=1), default=10, show_default=True, help="Rows in the best-value-per-chaos list.")
@click.option(
    "--divine-rate",
    type=float,
    help="Chaos per Divine for listings saved without a chaos price (searches without --sort price or --bargain).",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_trade_plan(
    search_files: tuple[str, ...],
    budget: float,
    score_terms: tuple[str, ...],
    top: int,
    divine_rate: float | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Pick at most one PoB-verified listing per slot that maximises score within --budget (offline, no API calls)."""
    try:
        score = parse_score(score_terms)
        bad = [k for k in score if k.lower() not in PLAN_STATS]
        if bad:
            raise ValueError(f"--score for trade plan accepts only {', '.join(PLAN_STATS)}; got {', '.join(bad)}")
        res = plan_budget(_load(search_files), budget=budget, score={k.lower(): w for k, w in score.items()} or None, top=top,
                          divine_rate=divine_rate)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_plan_markdown(res, budget=budget), out_path=out_path)


def format_plan_markdown(res: dict[str, Any], *, budget: float) -> str:
    """Picks and best-value-per-chaos tables for a `plan_budget` result."""
    terms = " + ".join(f"{w:g}×{k}%" for k, w in res["score"].items())
    pick_rows = [[p["slot"], p["name"], f"{p['chaos']:g}", _fmt_pct(p["dpsPct"]), _fmt_pct(p["ehpPct"]), f"{p['score']:+.2f}"] for p in res["picks"]]
    value_rows = [
        [i, p["slot"], p["name"], f"{p['chaos']:g}", f"{p['perChaos']:.3f}" if p["perChaos"] is not None else "free"]
        for i, p in enumerate(res["bestPerChaos"], 1)
    ]
    lines = [
        f"### Upgrade Plan (budget {budget:g}c, Score: `{terms}`)",
        f"- Spend {res['totalChaos']:g}c for score {res['totalScore']:+.2f} over slots: {', '.join(res['slots']) or '-'}"
        f"; {res['skippedUnverified']} listing(s) skipped (no chaos price, no PoB result or no gain)"
        + ("; Divine listings saved without a chaos price need --divine-rate" if res.get("divineRate") is None else ""),
        render_table(["Slot", "Item", "Chaos", "ΔDPS", "ΔEHP", "Score"], pick_rows) if pick_rows else "- Nothing fits the budget.",
        "",
        "#### Best value per chaos",
        render_table(["#", "Slot", "Item", "Chaos", "Score/Chaos"], value_rows) if value_rows else "- No verified gains.",
        "> Each delta was PoB-verified alone against the current build; combined gains across slots are an estimate.",
    ]
    return "\n".join(lines)
