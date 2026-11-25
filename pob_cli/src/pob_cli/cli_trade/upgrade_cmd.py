"""`pob trade upgrade`: weakest gear slots -> live PoB-verified trade search -> budget plan + free moves.

A reference list, not a shopping list: each listing is verified alone against the current build, so
the plan's total is an estimate. Nothing is applied to the build.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from pathlib import Path
from typing import Any

import click

from ..cli_common import _emit_json, _emit_text, _output_options, invoke_daemon
from ..scoring import parse_score
from ..trade_eval.budget import DEFAULT_PLAN_SCORE, PLAN_STATS, plan_budget
from ..trade_eval.upgrade import UPGRADE_SLOTS, rank_weak_slots, unequip_loss
from ..trade_query import pob_slot_for_slot
from .common import _TARGET_SLOT_TYPE, _default_target_slot
from .other_cmds import trade_group
from .plan_cmd import format_plan_markdown
from .search_cmd import _try_auto_trade_query, cmd_trade_search

_SUMMARY_STAT = {"dps": "primaryDps", "ehp": "totalEhp"}


def _search_slot(ctx: click.Context, trade_slot: str, *, per_slot: int, league: str | None, proxy: str | None, target: str) -> dict[str, Any]:
    """Run `pob trade search --auto-query --pob on --json` for one slot and return its payload."""
    # trade search silently falls back to a stat-less cheapest-first query when auto-query fails.
    if _try_auto_trade_query(pob_slot=pob_slot_for_slot(trade_slot), slot=trade_slot, target=target, league=league) is None:
        raise RuntimeError("auto-query failed")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "search.json"
        try:
            # The inner command echoes "Saved JSON to ..." on stdout; keep the outer --json parseable.
            with contextlib.redirect_stdout(io.StringIO()):
                ctx.invoke(
                    cmd_trade_search, slot=trade_slot, auto_query=True, pob_mode="on", pob_limit=per_slot,
                    league=league, proxy=proxy, target=target, as_json=True, out_path=str(out),
                )
        except click.exceptions.Exit as exc:
            if exc.exit_code:
                raise RuntimeError(f"trade search exited {exc.exit_code}") from exc
        except click.ClickException as exc:
            raise RuntimeError(exc.format_message()) from exc
        if not out.exists():
            raise RuntimeError("trade search returned no results")
        data = json.loads(out.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise RuntimeError("trade search returned no candidates")
    return data


def _resolve_slots(requested: tuple[str, ...]) -> list[dict[str, Any]]:
    by_trade = {t: p for p, t in UPGRADE_SLOTS.items()}
    by_pob = {p.lower(): p for p in UPGRADE_SLOTS}
    rows = []
    for raw in requested:
        key = raw.strip()
        pob = by_trade.get(key.lower()) or by_pob.get(key.lower())
        if pob is None:
            raise click.ClickException(f"Unknown --slot {raw!r}; expected one of {', '.join(UPGRADE_SLOTS.values())}.")
        rows.append({"slot": pob, "tradeSlot": UPGRADE_SLOTS[pob], "reason": "requested", "loss": None})
    return rows


def _pick_slots(target: str, score: dict[str, float], top: int) -> list[dict[str, Any]]:
    items = invoke_daemon("items", {"target": target})
    losses: dict[str, float | None] = {}
    for s in items.get("slots") or []:
        name = s.get("name")
        if name in UPGRADE_SLOTS and s.get("selItemId") and not s.get("inactive"):
            try:
                losses[name] = unequip_loss(invoke_daemon("item_unequip", {"target": target, "slot": name, "apply": False}), score)
            except click.ClickException:
                losses[name] = None  # unverified: ranked after every measured slot
    return rank_weak_slots(items, losses, top)


def _free_moves(target: str, score: dict[str, float]) -> dict[str, Any]:
    summary_score = {_SUMMARY_STAT[k]: w for k, w in score.items()}
    calls = {
        "respec": ("respec", {"target": target, "score": summary_score, "floors": [], "rounds": 1, "max_dist": 5}, 900.0),
        "gems": ("rank_gems", {"target": target, "group": "main", "field": "CombinedDPS", "include_legacy": False, "optimize": True, "passes": 1}, 600.0),
    }
    out: dict[str, Any] = {}
    for key, (action, params, timeout) in calls.items():
        try:
            out[key] = invoke_daemon(action, params, timeout=timeout)
        except click.ClickException as exc:
            out[key] = {"error": exc.format_message()}
    return out


def format_upgrade_markdown(res: dict[str, Any], *, target: str) -> str:
    """Plan tables, searched/skipped slots, then the free (0-chaos) moves."""
    from ..cli_optimize import _format_link_plan
    from ..cli_rank_nodes import format_respec_markdown

    lines = [
        format_plan_markdown(res["plan"], budget=res["budget"]),
        "- Slots searched: " + (", ".join(f"{s['slot']} ({s['reason']})" for s in res["slots"]) or "-"),
    ]
    if res["skippedSlots"]:
        lines.append("- Skipped: " + ", ".join(f"{s['slot']} ({s['error']})" for s in res["skippedSlots"]))
    lines += ["", "### Free moves"]
    for key, render in (("respec", lambda r: format_respec_markdown(r, target=target)), ("gems", _format_link_plan)):
        move = res["free"][key]
        lines.append(f"- {key}: {move['error']}" if "error" in move else render(move))
    return "\n".join(lines)


@trade_group.command("upgrade")
@click.option("--budget", type=float, required=True, help="Total chaos to spend across all slots.")
@click.option("--slot", "slots", multiple=True, help=f"Slot to search (repeatable; default: the weakest). One of {', '.join(UPGRADE_SLOTS.values())}.")
@click.option("--top-slots", type=click.IntRange(1, len(UPGRADE_SLOTS)), default=3, show_default=True, help="Weakest slots to search when --slot is not given.")
@click.option("--per-slot", type=click.IntRange(1, 20), default=5, show_default=True, help="Listings PoB-verified per slot.")
@click.option("--score", "score_terms", multiple=True, help="Objective over PoB-verified %: dps=W,ehp=W (default dps=1,ehp=0.25).")
@click.option("--league", help="PoE1 trade league (defaults to current challenge league).")
@click.option("--proxy", help="HTTP/SOCKS5 proxy URL (overrides POB_PROXY).")
@click.option("--target", type=_TARGET_SLOT_TYPE, default=_default_target_slot)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_trade_upgrade(
    budget: float,
    slots: tuple[str, ...],
    top_slots: int,
    per_slot: int,
    score_terms: tuple[str, ...],
    league: str | None,
    proxy: str | None,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Reference upgrade list: weakest slots -> live trade (PoB-verified) -> best combo within --budget, plus free moves."""
    ctx = click.get_current_context()
    if budget < 0:
        raise click.ClickException("--budget must be a non-negative number of chaos.")
    try:
        parsed = parse_score(score_terms)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    bad = [k for k in parsed if k.lower() not in PLAN_STATS]
    if bad:
        raise click.ClickException(f"--score accepts only {', '.join(PLAN_STATS)}; got {', '.join(bad)}")
    score = {k.lower(): w for k, w in parsed.items()} or dict(DEFAULT_PLAN_SCORE)
    chosen = _resolve_slots(slots) if slots else None

    free = _free_moves(target, score)  # first, so a trade timeout still leaves something to show
    skipped: list[dict[str, str]] = []
    if chosen is None:
        try:
            chosen = _pick_slots(target, score, top_slots)
        except click.ClickException as exc:
            chosen = []
            skipped.append({"slot": "*", "error": f"slot probe: {exc.format_message()}"})
    payloads: list[dict[str, Any]] = []
    for row in chosen:
        try:
            payloads.append(_search_slot(ctx, row["tradeSlot"], per_slot=per_slot, league=league, proxy=proxy, target=target))
        except RuntimeError as exc:
            skipped.append({"slot": row["tradeSlot"], "error": str(exc)})
    res = {
        "budget": budget, "score": score, "slots": chosen,
        "plan": plan_budget(payloads, budget=budget, score=score),
        "free": free, "skippedSlots": skipped,
    }
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_upgrade_markdown(res, target=target), out_path=out_path)
