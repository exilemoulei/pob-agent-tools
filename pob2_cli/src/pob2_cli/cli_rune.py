# SPDX-License-Identifier: GPL-3.0-only
"""Click CLI entry points for `pob2 rune`, `pob2 instill`, and `pob2 rank-instill` (L4)."""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from .compat import ensure_utf8
from .errors import DaemonRequestError
from .game_profile import stamp_game
from .guardrails import GuardrailScope, check_input
from .rune import (
    load_rune_instill_catalog,
    normalize_emotion_name,
    price_instill_recipe,
    rank_instillations,
    rank_runes_for_slot,
    resolve_rune_price_ex,
)


def _echo(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def _emit_json(ctx: click.Context | None, payload: dict[str, Any]) -> None:
    root_obj = (ctx.find_root().obj or {}) if ctx is not None else {}
    compact = bool(root_obj.get("compact"))
    text = json.dumps(
        stamp_game(payload),
        ensure_ascii=False,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    )
    _echo(text)


@click.group("rune")
def rune_group() -> None:
    """Inspect and Pareto-rank PoE2 0.5.5 runes, soul cores, and idols with Item.lua socket parity."""


@rune_group.command("inspect")
@click.argument("name", required=True)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_rune_inspect(ctx: click.Context, name: str, as_json: bool) -> None:
    """Inspect a PoE2 rune, soul core, or idol (slots, req_level, isSocketBound, Bonded mods, price)."""
    check_input(name, GuardrailScope.DATA_QUERY, expected_game="poe2")
    cat = load_rune_instill_catalog()
    entry = cat.lookup_rune(name)
    if entry is None:
        raise DaemonRequestError(f"Unknown PoE2 rune/soul core/idol: {name!r}")
    px, src = resolve_rune_price_ex(entry)
    payload = {
        "ok": True,
        "patch": cat.version,
        "name": entry.name,
        "zhCn": entry.zh_cn,
        "zhTw": entry.zh_tw,
        "kind": entry.kind,
        "reqLevel": entry.req_level,
        "isSocketBound": entry.is_socket_bound,
        "priceEx": round(px, 3),
        "priceSource": src,
        "slots": {
            k: {
                "kind": v.kind,
                "reqLevel": v.req_level,
                "isSocketBound": v.is_socket_bound,
                "stats": list(v.stats),
                "bonded": list(v.bonded),
            }
            for k, v in sorted(entry.slots.items())
        },
    }
    if as_json:
        _emit_json(ctx, payload)
        return
    bound_tag = " [isSocketBound=true: 不可覆盖]" if entry.is_socket_bound else ""
    lines = [f"{entry.name} ({entry.zh_cn}) | {entry.kind} | ReqLv={entry.req_level} | ~{px:.1f} Ex{bound_tag}"]
    for skey, sval in sorted(entry.slots.items()):
        lines.append(f"  [{skey}] {'; '.join(sval.stats)}")
        if sval.bonded:
            lines.append(f"    (Shaman) {'; '.join(sval.bonded)}")
    _echo("\n".join(lines))


@rune_group.command("rank")
@click.option("--type", "item_type", required=True, help="Item type (e.g. Wand, Quarterstaff, Buckler, Helmet, Body Armour).")
@click.option("--slot", "slot_name", default="Body Armour", show_default=True, help="Equipment slot label.")
@click.option("--base", "base_name", default="", help="Optional item base name (for Buckler/subtype parity).")
@click.option("--level", "char_level", type=int, default=90, show_default=True, help="Character level cap (req_level <= level).")
@click.option("--shaman", "is_shaman", is_flag=True, default=False, help="Include Shaman-only Bonded rune bonuses.")
@click.option("--metric", default="CombinedDPS", show_default=True, help="Target metric (CombinedDPS | TotalEHP | Spirit).")
@click.option("--budget", "max_budget_ex", type=float, default=None, help="Optional max price cap in Exalted Orbs.")
@click.option("--limit", type=int, default=12, show_default=True, help="Maximum top candidates to output.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_rune_rank(
    ctx: click.Context,
    item_type: str,
    slot_name: str,
    base_name: str,
    char_level: int,
    is_shaman: bool,
    metric: str,
    max_budget_ex: float | None,
    limit: int,
    as_json: bool,
) -> None:
    """Rank legal runes/soul cores/idols for an item type with BiS anchor + Pareto budget frontier."""
    check_input(item_type, GuardrailScope.CATEGORY, expected_game="poe2")
    report = rank_runes_for_slot(
        item_type=item_type,
        slot_name=slot_name,
        base_name=base_name,
        char_level=char_level,
        is_shaman=is_shaman,
        metric=metric,
        max_budget_ex=max_budget_ex,
        limit=limit,
    )
    if as_json:
        _emit_json(ctx, report.to_dict())
        return
    _echo(_format_pareto_human(report.to_dict()))


@click.group("instill")
def instill_group() -> None:
    """Lookup and bridge PoE2 0.5.5 ordered amulet instillations & Liquid Emotions."""


@instill_group.command("lookup")
@click.argument("query", required=False, default="")
@click.option(
    "--emotions",
    "emotions_csv",
    default=None,
    help="Exact ordered 3 emotions `Slot1,Slot2,Slot3` (accepts short/legacy/live/CN names).",
)
@click.option("--limit", type=int, default=10, show_default=True, help="Max matches for substring queries.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_instill_lookup(
    ctx: click.Context,
    query: str,
    emotions_csv: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """Lookup ordered `[Slot 1 -> Slot 2 -> Slot 3]` amulet instillation recipe and live emotion cost."""
    cat = load_rune_instill_catalog()
    if emotions_csv:
        parts = [p.strip() for p in emotions_csv.split(",") if p.strip()]
        if len(parts) != 3:
            raise DaemonRequestError("`--emotions` requires exactly 3 comma-separated emotions in Slot1,Slot2,Slot3 order.")
        try:
            e1, e2, e3 = (normalize_emotion_name(p, catalog=cat) for p in parts)
        except ValueError as exc:
            raise DaemonRequestError(str(exc)) from exc
        rec = cat.lookup_instill_by_recipe(e1.short, e2.short, e3.short)
        matches = [rec] if rec is not None else []
    else:
        if query:
            check_input(query, GuardrailScope.NOTABLE_OR_OIL, expected_game="poe2")
        matches = cat.search_instills(query, limit=limit)

    priced = [
        {**price_instill_recipe(inst, catalog=cat), "stats": list(inst.stats)}
        for inst in matches
    ]
    payload = {"ok": True, "patch": cat.version, "count": len(priced), "instills": priced}
    if as_json:
        _emit_json(ctx, payload)
        return
    if not priced:
        _echo("No matching PoE2 amulet instillation found.")
        return
    lines = []
    for item in priced:
        lines.append(
            f"{item['name']} ({item['zhCn']}) [node={item['nodeId']}] | ~{item['totalCostEx']:.1f} Ex\n"
            f"  Order: {item['orderedDisplayEn']}\n"
            f"  Stats: {'; '.join(item['stats'])}"
        )
    _echo("\n".join(lines))


@instill_group.command("bridge")
@click.argument("emotion", required=True)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_instill_bridge(ctx: click.Context, emotion: str, as_json: bool) -> None:
    """Bridge legacy `Distilled <X>` <-> live 0.5.5 `Liquid <X>` (rejects Ancient Time-Lost Jewel emotions)."""
    cat = load_rune_instill_catalog()
    try:
        em = normalize_emotion_name(emotion, catalog=cat)
    except ValueError as exc:
        raise DaemonRequestError(str(exc)) from exc
    payload = {
        "ok": True,
        "short": em.short,
        "legacyName": em.legacy_name,
        "liveName": em.live_name,
        "tierLevel": em.tier_level,
        "zhCn": em.zh_cn,
        "zhTw": em.zh_tw,
        "radiusJewel": False,
    }
    if as_json:
        _emit_json(ctx, payload)
        return
    _echo(f"{em.legacy_name} <-> {em.live_name} ({em.zh_cn}, tierLevel={em.tier_level}, amulet-valid)")


@click.command("rank-instill")
@click.option("--metric", default="CombinedDPS", show_default=True, help="Target metric (CombinedDPS | TotalEHP).")
@click.option("--budget", "max_budget_ex", type=float, default=None, help="Optional max instillation cost in Exalted Orbs.")
@click.option("--limit", type=int, default=15, show_default=True, help="Maximum top candidates to output.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_rank_instill(
    ctx: click.Context,
    metric: str,
    max_budget_ex: float | None,
    limit: int,
    as_json: bool,
) -> None:
    """Pareto cost-efficiency ranking for all 875 ordered PoE2 amulet instillations."""
    report = rank_instillations(metric=metric, max_budget_ex=max_budget_ex, limit=limit)
    if as_json:
        _emit_json(ctx, report.to_dict())
        return
    _echo(_format_pareto_human(report.to_dict()))


instill_group.add_command(cmd_rank_instill, name="rank")


def _format_pareto_human(data: dict[str, Any]) -> str:
    lines = [f"PoE2 {data['patch']} Pareto Ranking ({data['kind']} | metric={data['metric']} | evaluated={data['evaluatedCount']})"]
    bis = data.get("bisAnchor")
    if bis:
        lines.append(f"  [BiS Anchor] {bis['name']} ({bis['zhCn']}) | gain={bis['gain']:.2f} | cost={bis['costEx']:.1f} Ex")
    champ = data.get("budgetChampion")
    if champ:
        lines.append(
            f"  [Budget Pareto] {champ['name']} ({champ['zhCn']}) | {champ['pctOfBis']:.1f}% of BiS | cost={champ['costEx']:.1f} Ex"
        )
    for row in data.get("paretoFrontier") or []:
        order = f" | {row['orderedDisplayEn']}" if "orderedDisplayEn" in row else ""
        lines.append(f"  - {row['name']}: gain={row['gain']:.2f} ({row['pctOfBis']:.1f}% BiS), cost={row['costEx']:.1f} Ex{order}")
    return "\n".join(lines)


__all__ = [
    "cmd_rank_instill",
    "instill_group",
    "rune_group",
]
