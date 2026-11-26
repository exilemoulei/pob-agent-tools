# SPDX-License-Identifier: GPL-3.0-only
"""Pareto cost-efficiency ranker for PoE2 287 runes/soul cores/idols and 875 amulet instillations (L2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..craft.price_book import PriceBook
from .catalog import InstillRecipe, RuneEntry, RuneInstillCatalog, load_rune_instill_catalog
from .instill_bridge import (
    format_ordered_recipe,
    price_instill_recipe,
    resolve_rune_price_ex,
)
from .live_eval import (
    BuildProfile,
    eval_item_raw_deltas_live,
    extract_build_profile,
    get_allocated_node_ids,
    read_metric_value,
    score_stat_lines,
)
from .socket_rules import (
    apply_anoint_to_amulet_raw,
    apply_runes_to_item_raw,
    eligible_runes_for_item,
    resolve_item_augment_types,
    strip_anoint_from_amulet_raw,
)


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """Single evaluated rune or instillation candidate with cost, gain, and Pareto flags."""

    name: str
    zh_cn: str
    cost_ex: float
    gain: float
    gain_pct: float
    efficiency_per_ex: float
    price_source: str
    details: dict[str, Any] = field(default_factory=dict)
    is_pareto: bool = False
    is_bis_anchor: bool = False
    is_budget_champion: bool = False
    pct_of_bis: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "zhCn": self.zh_cn,
            "costEx": round(self.cost_ex, 3),
            "gain": round(self.gain, 4),
            "gainPct": round(self.gain_pct, 3),
            "efficiencyPerEx": round(self.efficiency_per_ex, 4),
            "pctOfBis": round(self.pct_of_bis, 2),
            "isPareto": self.is_pareto,
            "isBisAnchor": self.is_bis_anchor,
            "isBudgetChampion": self.is_budget_champion,
            "priceSource": self.price_source,
            **self.details,
        }


@dataclass(frozen=True)
class ParetoRankingReport:
    """BiS anchor + non-dominated Pareto frontier + full ranked list."""

    kind: str  # "rune" | "instill"
    metric: str
    evaluated_count: int
    bis_anchor: RankedCandidate | None
    budget_champion: RankedCandidate | None
    pareto_frontier: tuple[RankedCandidate, ...]
    top_candidates: tuple[RankedCandidate, ...]
    warnings: tuple[str, ...] = ()
    eval_mode: str = "offline_heuristic"
    shortlisted_from: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "game": "poe2",
            "patch": "0.5.5",
            "kind": self.kind,
            "metric": self.metric,
            "evalMode": self.eval_mode,
            "evaluatedCount": self.evaluated_count,
            "bisAnchor": self.bis_anchor.to_dict() if self.bis_anchor else None,
            "budgetChampion": self.budget_champion.to_dict() if self.budget_champion else None,
            "paretoFrontier": [c.to_dict() for c in self.pareto_frontier],
            "topCandidates": [c.to_dict() for c in self.top_candidates],
            "warnings": list(self.warnings),
        }
        if self.shortlisted_from is not None:
            out["shortlistedFrom"] = self.shortlisted_from
        return out


def compute_pareto_frontier(
    candidates: list[RankedCandidate],
    *,
    budget_sweet_spot_min_pct_bis: float = 75.0,
) -> tuple[RankedCandidate | None, RankedCandidate | None, list[RankedCandidate], list[RankedCandidate]]:
    """Compute BiS anchor, non-dominated `(cost_ex asc, gain desc)` Pareto frontier, and budget champion."""
    positive = [c for c in candidates if c.gain > 0.0 and c.cost_ex > 0.0]
    if not positive:
        ordered = sorted(candidates, key=lambda c: (-c.gain, c.cost_ex, c.name))
        return None, None, [], ordered

    bis = max(positive, key=lambda c: (c.gain, -c.cost_ex, c.name))
    bis_gain = bis.gain

    # Sort ascending by cost, descending by gain; keep strictly improving running-max gain.
    by_cost = sorted(positive, key=lambda c: (c.cost_ex, -c.gain, c.name))
    frontier_names: set[str] = set()
    best_gain = -1.0
    for cand in by_cost:
        if cand.gain > best_gain + 1e-12:
            frontier_names.add(cand.name)
            best_gain = cand.gain

    # Pick budget champion: cheapest Pareto point reaching >=75% of BiS (or max efficiency if none cheaper than BiS)
    sweet_candidates = [
        c for c in by_cost if c.name in frontier_names and (c.gain / bis_gain) * 100.0 >= budget_sweet_spot_min_pct_bis
    ]
    if sweet_candidates:
        champ_name = sweet_candidates[0].name
    else:
        champ_name = max(
            (c for c in by_cost if c.name in frontier_names),
            key=lambda c: (c.efficiency_per_ex, c.gain),
        ).name

    annotated: list[RankedCandidate] = []
    for c in candidates:
        pct_bis = (c.gain / bis_gain * 100.0) if bis_gain > 0 else 0.0
        annotated.append(
            RankedCandidate(
                name=c.name,
                zh_cn=c.zh_cn,
                cost_ex=c.cost_ex,
                gain=c.gain,
                gain_pct=c.gain_pct,
                efficiency_per_ex=c.gain / c.cost_ex if c.cost_ex > 0 else 0.0,
                price_source=c.price_source,
                details=c.details,
                is_pareto=(c.name in frontier_names),
                is_bis_anchor=(c.name == bis.name),
                is_budget_champion=(c.name == champ_name),
                pct_of_bis=pct_bis,
            )
        )

    bis_out = next(c for c in annotated if c.is_bis_anchor)
    champ_out = next(c for c in annotated if c.is_budget_champion)
    frontier_out = sorted((c for c in annotated if c.is_pareto), key=lambda c: (c.cost_ex, -c.gain))
    ranked_out = sorted(annotated, key=lambda c: (-c.gain, c.cost_ex, c.name))
    return bis_out, champ_out, frontier_out, ranked_out


def _offline_stat_score(lines: tuple[str, ...], metric: str, profile: BuildProfile | None = None) -> float:
    """Deterministic stat-line scorer (offline heuristic or build-aware live shortlist)."""
    return score_stat_lines(lines, metric, profile=profile)


def _find_equipped_slot_item(worker: Any, slot_name: str) -> tuple[int, str, dict[str, Any]]:
    listing = worker.call("list_items", {})
    items_by_id = {int(it["id"]): it for it in (listing.get("items") or ()) if isinstance(it, dict) and "id" in it}
    slot_low = slot_name.strip().lower()
    for sl in listing.get("slots") or ():
        if not isinstance(sl, dict):
            continue
        if str(sl.get("name") or "").strip().lower() == slot_low:
            sel_id = int(sl.get("selItemId") or sl.get("selectedId") or sl.get("itemId") or 0)
            if sel_id > 0 and sel_id in items_by_id:
                raw = str(worker.call("item_raw", {"id": sel_id}).get("raw") or "")
                return sel_id, raw, items_by_id[sel_id]
    raise ValueError(f"No equipped item found in slot `{slot_name}`.")


def _select_live_rune_shortlist(
    eligible: list[tuple[RuneEntry, tuple[str, ...]]],
    parity: Any,
    metric: str,
    price_book: PriceBook | None,
    top_live_eval: int,
) -> list[tuple[RuneEntry, tuple[str, ...]]]:
    """Pre-screen legal runes via fast offline score ∪ efficiency (score/cost_ex) to keep live CoW <3s."""
    if top_live_eval <= 0 or len(eligible) <= top_live_eval:
        return eligible
    scored: list[tuple[float, float, float, RuneEntry, tuple[str, ...]]] = []
    for rune, mods in eligible:
        cost_ex, _src = resolve_rune_price_ex(rune, price_book=price_book)
        mult = parity.effect_multiplier(rune.kind)
        sc = _offline_stat_score(mods, metric) * mult
        eff = sc / max(0.05, cost_ex)
        scored.append((sc, eff, cost_ex, rune, mods))

    half = max(1, top_live_eval // 2)
    by_gain = sorted(scored, key=lambda x: (-x[0], x[2], x[3].name))[:half]
    by_eff = sorted(scored, key=lambda x: (-x[1], -x[0], x[3].name))[:top_live_eval]
    by_cheap = sorted((x for x in scored if x[0] > 0.0), key=lambda x: (x[2], -x[0], x[3].name))[:4]

    chosen: list[tuple[RuneEntry, tuple[str, ...]]] = []
    seen: set[str] = set()
    for _sc, _eff, _cost, rune, mods in (*by_gain, *by_eff, *by_cheap):
        if rune.name not in seen:
            seen.add(rune.name)
            chosen.append((rune, mods))
            if len(chosen) >= top_live_eval:
                break
    return chosen


def rank_runes_for_slot(
    *,
    item_type: str = "",
    slot_name: str = "Body Armour",
    raw_item_text: str = "",
    sub_type: str | None = None,
    base_name: str = "",
    char_level: int = 100,
    is_shaman: bool = False,
    metric: str = "CombinedDPS",
    max_budget_ex: float | None = None,
    limit: int = 15,
    top_live_eval: int = 28,
    include_socket_bound: bool = False,
    price_book: PriceBook | None = None,
    worker: Any | None = None,
    evaluator: Callable[[RuneEntry, tuple[str, ...]], float] | None = None,
    catalog: RuneInstillCatalog | None = None,
) -> ParetoRankingReport:
    """Rank legal PoE2 runes/soul cores/idols for an item/slot with BiS anchor + Pareto frontier."""
    cat = catalog or load_rune_instill_catalog()
    warnings: list[str] = []

    if worker is not None and not raw_item_text:
        _item_id, raw_item_text, meta = _find_equipped_slot_item(worker, slot_name)
        item_type = item_type or str(meta.get("type") or "")
        base_name = base_name or str(meta.get("baseName") or "")

    parity = resolve_item_augment_types(
        item_type, sub_type=sub_type, raw_text=raw_item_text, base_name=base_name, catalog=cat
    )
    if parity.is_mirrored:
        return ParetoRankingReport(
            kind="rune",
            metric=metric,
            evaluated_count=0,
            bis_anchor=None,
            budget_champion=None,
            pareto_frontier=(),
            top_candidates=(),
            warnings=("Target item is Mirrored; socketed runes cannot be changed.",),
            eval_mode="live_cow" if worker is not None else "offline_heuristic",
        )

    eligible = eligible_runes_for_item(
        parity,
        char_level=char_level,
        is_shaman=is_shaman,
        include_socket_bound=include_socket_bound,
        catalog=cat,
    )
    if max_budget_ex is not None:
        eligible = [
            (r, m) for r, m in eligible if resolve_rune_price_ex(r, price_book=price_book)[0] <= max_budget_ex
        ]

    total_eligible = len(eligible)
    candidates: list[RankedCandidate] = []
    if worker is not None:
        shortlist = _select_live_rune_shortlist(eligible, parity, metric, price_book, top_live_eval)
        baseline = read_metric_value(worker, metric)
        valid_pairs: list[tuple[RuneEntry, tuple[str, ...], str]] = []
        for rune, mods in shortlist:
            try:
                mutated_raw = apply_runes_to_item_raw(
                    raw_item_text,
                    [rune.name],
                    item_type=item_type,
                    sub_type=sub_type,
                    base_name=base_name,
                    char_level=char_level,
                    is_shaman=is_shaman,
                    allow_overwrite_bound=True,
                    catalog=cat,
                )
            except ValueError:
                continue
            valid_pairs.append((rune, mods, mutated_raw))
        deltas = eval_item_raw_deltas_live(
            worker, slot_name, [mraw for _, _, mraw in valid_pairs], metric, baseline
        )
        for (rune, mods, _mraw), gain in zip(valid_pairs, deltas, strict=False):
            cost_ex, src = resolve_rune_price_ex(rune, price_book=price_book)
            gain_pct = (gain / baseline * 100.0) if baseline > 0 else 0.0
            candidates.append(
                RankedCandidate(
                    name=rune.name,
                    zh_cn=rune.zh_cn,
                    cost_ex=cost_ex,
                    gain=gain,
                    gain_pct=gain_pct,
                    efficiency_per_ex=gain / cost_ex,
                    price_source=src,
                    details={
                        "kind": rune.kind,
                        "reqLevel": rune.req_level,
                        "isSocketBound": rune.is_socket_bound,
                        "effectiveMods": list(mods),
                        "effectMultiplier": round(parity.effect_multiplier(rune.kind), 3),
                    },
                )
            )
    else:
        for rune, mods in eligible:
            cost_ex, src = resolve_rune_price_ex(rune, price_book=price_book)
            mult = parity.effect_multiplier(rune.kind)
            raw_gain = evaluator(rune, mods) if evaluator is not None else (_offline_stat_score(mods, metric) * mult)
            candidates.append(
                RankedCandidate(
                    name=rune.name,
                    zh_cn=rune.zh_cn,
                    cost_ex=cost_ex,
                    gain=raw_gain,
                    gain_pct=raw_gain,
                    efficiency_per_ex=raw_gain / cost_ex,
                    price_source=src,
                    details={
                        "kind": rune.kind,
                        "reqLevel": rune.req_level,
                        "isSocketBound": rune.is_socket_bound,
                        "effectiveMods": list(mods),
                        "effectMultiplier": round(mult, 3),
                    },
                )
            )

    bis, champ, frontier, ranked = compute_pareto_frontier(candidates)
    return ParetoRankingReport(
        kind="rune",
        metric=metric,
        evaluated_count=len(candidates),
        bis_anchor=bis,
        budget_champion=champ,
        pareto_frontier=tuple(frontier),
        top_candidates=tuple(ranked[: max(1, limit)]),
        warnings=tuple(warnings),
        eval_mode="live_cow" if worker is not None else "offline_heuristic",
        shortlisted_from=total_eligible if worker is not None else None,
    )


def _select_live_instill_shortlist(
    worker: Any,
    priced_pool: list[tuple[InstillRecipe, dict[str, Any]]],
    metric: str,
    top_live_eval: int,
) -> list[tuple[InstillRecipe, dict[str, Any]]]:
    """Fast (<5ms) offline pre-scoring over `priced_pool` (skipping already-allocated nodes).

    Takes union of Top-12 offline stat score + Top-12 efficiency (score/cost_ex) + Top-4 budget picks,
    capped at `top_live_eval` (default 20), avoiding the 35s full-tree `node_power` scan.
    """
    alloc_ids = get_allocated_node_ids(worker)
    profile = extract_build_profile(worker)
    pool = [(inst, pinfo) for inst, pinfo in priced_pool if inst.node_id not in alloc_ids] or priced_pool
    k_limit = max(1, int(top_live_eval))
    if len(pool) <= k_limit:
        return list(pool)

    scored: list[tuple[float, float, float, InstillRecipe, dict[str, Any]]] = []
    for inst, pinfo in pool:
        cost_ex = max(0.05, float(pinfo["totalCostEx"]))
        sc = _offline_stat_score(inst.stats, metric, profile=profile)
        scored.append((sc, sc / cost_ex, cost_ex, inst, pinfo))

    by_score = sorted(scored, key=lambda x: (-x[0], x[2], x[3].name))[:12]
    by_eff = sorted(scored, key=lambda x: (-x[1], -x[0], x[3].name))[:12]
    by_budget = sorted((x for x in scored if x[0] > 0.0), key=lambda x: (x[2], -x[0], x[3].name))[:4]
    by_all = sorted(scored, key=lambda x: (-x[0], x[2], x[3].name))

    chosen: list[tuple[InstillRecipe, dict[str, Any]]] = []
    seen: set[int] = set()
    for _sc, _eff, _cost, inst, pinfo in (*by_score, *by_eff, *by_budget, *by_all):
        if inst.node_id not in seen:
            seen.add(inst.node_id)
            chosen.append((inst, pinfo))
            if len(chosen) >= k_limit:
                break
    return chosen


def rank_instillations(
    *,
    metric: str = "CombinedDPS",
    max_budget_ex: float | None = None,
    limit: int = 15,
    top_live_eval: int = 20,
    price_book: PriceBook | None = None,
    worker: Any | None = None,
    evaluator: Callable[[InstillRecipe], float] | None = None,
    catalog: RuneInstillCatalog | None = None,
) -> ParetoRankingReport:
    """Pareto-rank PoE2 875 ordered amulet instillations with live LiquidEmotions pricing."""
    cat = catalog or load_rune_instill_catalog()
    warnings: list[str] = []

    priced_pool: list[tuple[InstillRecipe, dict[str, Any]]] = []
    for inst in cat.instills:
        pinfo = price_instill_recipe(inst, price_book=price_book, catalog=cat)
        if max_budget_ex is not None and pinfo["totalCostEx"] > max_budget_ex:
            continue
        priced_pool.append((inst, pinfo))

    candidates: list[RankedCandidate] = []
    if worker is not None:
        try:
            _am_id, am_raw, _meta = _find_equipped_slot_item(worker, "Amulet")
        except ValueError as exc:
            return ParetoRankingReport(
                kind="instill", metric=metric, evaluated_count=0,
                bis_anchor=None, budget_champion=None, pareto_frontier=(), top_candidates=(),
                warnings=(str(exc),),
            )
        prescored = _select_live_instill_shortlist(worker, priced_pool, metric, top_live_eval)
        equipped_baseline = read_metric_value(worker, metric)
        unanointed_raw, existing_anoint = strip_anoint_from_amulet_raw(am_raw)

        valid_pairs: list[tuple[InstillRecipe, dict[str, Any], str]] = []
        for inst, pinfo in prescored:
            try:
                mutated = apply_anoint_to_amulet_raw(am_raw, inst.name)
            except ValueError as exc:
                warnings.append(str(exc))
                break
            valid_pairs.append((inst, pinfo, mutated))

        batch_raws = [mraw for _, _, mraw in valid_pairs]
        if existing_anoint and valid_pairs:
            batch_raws = [unanointed_raw, *batch_raws]
        raw_deltas = eval_item_raw_deltas_live(worker, "Amulet", batch_raws, metric, equipped_baseline)
        if existing_anoint and raw_deltas:
            delta_unanointed = raw_deltas[0]
            cand_deltas = raw_deltas[1:]
            unanointed_baseline = equipped_baseline + delta_unanointed
            existing_contrib = -delta_unanointed
            existing_pct = (existing_contrib / unanointed_baseline * 100.0) if unanointed_baseline > 0 else 0.0
            warnings.append(
                f"Equipped amulet already allocates `{existing_anoint}` "
                f"({existing_contrib:+.1f} / {existing_pct:+.2f}% vs unanointed); "
                "Pareto ranking uses intrinsic `gainVsUnanointed` and reports net `gainVsEquipped` per candidate."
            )
        else:
            delta_unanointed = 0.0
            cand_deltas = raw_deltas
            unanointed_baseline = equipped_baseline

        for (inst, pinfo, _mraw), gain_vs_eq in zip(valid_pairs, cand_deltas, strict=False):
            gain_vs_un = gain_vs_eq - delta_unanointed
            gain_pct_eq = (gain_vs_eq / equipped_baseline * 100.0) if equipped_baseline > 0 else 0.0
            gain_pct_un = (gain_vs_un / unanointed_baseline * 100.0) if unanointed_baseline > 0 else 0.0
            cost_ex = float(pinfo["totalCostEx"])
            candidates.append(
                RankedCandidate(
                    name=inst.name,
                    zh_cn=inst.zh_cn,
                    cost_ex=cost_ex,
                    gain=gain_vs_un,
                    gain_pct=gain_pct_un,
                    efficiency_per_ex=gain_vs_un / cost_ex,
                    price_source=",".join(pinfo["priceSources"]),
                    details={
                        "nodeId": inst.node_id,
                        "orderedRecipe": list(inst.recipe),
                        "orderedDisplayEn": pinfo["orderedDisplayEn"],
                        "orderedDisplayZh": pinfo["orderedDisplayZh"],
                        "stats": list(inst.stats),
                        "existingAnoint": existing_anoint,
                        "gainVsEquipped": round(gain_vs_eq, 4),
                        "gainPctVsEquipped": round(gain_pct_eq, 3),
                        "gainVsUnanointed": round(gain_vs_un, 4),
                        "gainPctVsUnanointed": round(gain_pct_un, 3),
                    },
                )
            )
    else:
        for inst, pinfo in priced_pool:
            cost_ex = float(pinfo["totalCostEx"])
            gain = evaluator(inst) if evaluator is not None else _offline_stat_score(inst.stats, metric)
            candidates.append(
                RankedCandidate(
                    name=inst.name,
                    zh_cn=inst.zh_cn,
                    cost_ex=cost_ex,
                    gain=gain,
                    gain_pct=gain,
                    efficiency_per_ex=gain / cost_ex,
                    price_source=",".join(pinfo["priceSources"]),
                    details={
                        "nodeId": inst.node_id,
                        "orderedRecipe": list(inst.recipe),
                        "orderedDisplayEn": format_ordered_recipe(inst.recipe, lang="en", catalog=cat),
                        "orderedDisplayZh": format_ordered_recipe(inst.recipe, lang="zh-CN", catalog=cat),
                        "stats": list(inst.stats),
                    },
                )
            )

    bis, champ, frontier, ranked = compute_pareto_frontier(candidates)
    return ParetoRankingReport(
        kind="instill",
        metric=metric,
        evaluated_count=len(candidates),
        bis_anchor=bis,
        budget_champion=champ,
        pareto_frontier=tuple(frontier),
        top_candidates=tuple(ranked[: max(1, limit)]),
        warnings=tuple(warnings),
        eval_mode="live_cow" if worker is not None else "offline_heuristic",
        shortlisted_from=len(priced_pool) if worker is not None else None,
    )


__all__ = [
    "ParetoRankingReport",
    "RankedCandidate",
    "compute_pareto_frontier",
    "rank_instillations",
    "rank_runes_for_slot",
]
