"""`pob trade plan`: pick at most one PoB-verified listing per slot to maximise score within a chaos budget.

Inputs are saved `pob trade search --json` payloads, so no network call happens here. Each listing's
delta was verified alone against the current build; summing them across slots is an approximation.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from ..trade_query import pob_slot_for_slot

PLAN_STATS = ("dps", "ehp")
DEFAULT_PLAN_SCORE: dict[str, float] = {"dps": 1.0, "ehp": 0.25}  # same blend as `combined_pct`


def _listing_score(cand: dict[str, Any], score: dict[str, float]) -> float | None:
    pob = cand.get("pob") or {}
    total = 0.0
    for key, weight in score.items():
        if not weight:
            continue
        pct = pob.get(f"{key}_pct")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool) or not math.isfinite(pct):
            return None
        total += weight * pct
    return total


def _finite(val: Any) -> float | None:
    return float(val) if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val) else None


def listing_chaos(price: dict[str, Any] | None, divine_rate: float | None = None) -> float | None:
    """Chaos price of a listing: `price.chaos`, else a chaos-priced amount, else divine x `divine_rate`.

    `pob trade search --json` leaves `price.chaos` null for Divine listings unless it looked up rates.
    """
    price = price or {}
    chaos = _finite(price.get("chaos"))
    if chaos is None:
        amount, cur = _finite(price.get("amount")), str(price.get("currency") or "").strip().lower()
        if amount is not None and cur == "chaos":
            chaos = amount
        elif amount is not None and cur == "divine" and divine_rate is not None:
            chaos = amount * divine_rate
    return chaos if chaos is not None and chaos >= 0 else None


def _slot_key(payload: dict[str, Any]) -> str:
    """PoB slot name, so `ring1` and `Ring 1` searches count as one slot."""
    raw = str(payload.get("slot") or payload.get("category") or "?")
    return pob_slot_for_slot(raw) or raw


def collect_listings(
    payloads: list[dict[str, Any]], score: dict[str, float], *, divine_rate: float | None = None
) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    """Group priced, PoB-verified, positive-score listings by slot; returns (by_slot, skipped, needDivineRate)."""
    by_slot: dict[str, list[dict[str, Any]]] = {}
    skipped = need_rate = 0
    for payload in payloads:
        slot = _slot_key(payload)
        for cand in payload.get("candidates") or []:
            chaos = listing_chaos(cand.get("price"), divine_rate)
            value = _listing_score(cand, score)
            if chaos is None or value is None or value <= 0:
                skipped += 1
                price = cand.get("price") or {}
                if chaos is None and divine_rate is None and str(price.get("currency") or "").strip().lower() == "divine":
                    need_rate += 1
                continue
            by_slot.setdefault(slot, []).append(
                {"slot": slot, "listingId": cand.get("listing_id"), "name": cand.get("name"), "chaos": float(chaos), "score": value,
                 "perChaos": value / chaos if chaos > 0 else None, "url": cand.get("url"),
                 "dpsPct": (cand.get("pob") or {}).get("dps_pct"), "ehpPct": (cand.get("pob") or {}).get("ehp_pct")}
            )
    # Two files for one slot can hold the same listing: keep its best copy there. Across slots
    # (Ring 1 / Ring 2) every copy stays; `plan_budget` buys a listing at most once.
    for slot, items in by_slot.items():
        best: dict[Any, dict[str, Any]] = {}
        for i in items:
            key = i["listingId"] or id(i)
            if key not in best or i["score"] > best[key]["score"]:
                best[key] = i
        by_slot[slot] = list(best.values())
    return by_slot, skipped, need_rate


_State = tuple[float, float, tuple[dict[str, Any], ...]]  # (chaos, score, picks)


def _pareto(states: list[_State]) -> list[_State]:
    """Drop every state that costs at least as much as another one for no more score."""
    out: list[_State] = []
    for st in sorted(states, key=lambda t: (t[0], -t[1])):
        if not out or st[1] > out[-1][1]:
            out.append(st)
    return out


def plan_budget(
    payloads: list[dict[str, Any]],
    *,
    budget: float,
    score: dict[str, float] | None = None,
    top: int = 10,
    divine_rate: float | None = None,
) -> dict[str, Any]:
    """Multiple-choice knapsack over slots (at most one listing each) under `budget` chaos."""
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("--budget must be a non-negative number of chaos.")
    if divine_rate is not None and (not math.isfinite(divine_rate) or divine_rate <= 0):
        raise ValueError("--divine-rate must be a positive number of chaos.")
    score = score or dict(DEFAULT_PLAN_SCORE)
    by_slot, skipped, need_rate = collect_listings(payloads, score, divine_rate=divine_rate)
    limit = budget + 1e-9 * max(1.0, budget)  # exact chaos prices; only float noise is tolerated
    seen: dict[Any, int] = defaultdict(int)
    for items in by_slot.values():
        for i in items:
            if i["listingId"]:
                seen[i["listingId"]] += 1
    shared = {k for k, n in seen.items() if n > 1}
    # Pareto frontier per set of bought shared listings, so a listing found for two slots is bought once.
    fronts: dict[frozenset[Any], list[_State]] = {frozenset(): [(0.0, 0.0, ())]}
    for items in by_slot.values():
        nxt: dict[frozenset[Any], list[_State]] = defaultdict(list)
        for used, front in fronts.items():
            nxt[used].extend(front)  # leave this slot as it is
            for cost, value, picks in front:
                for item in items:
                    lid = item["listingId"]
                    if cost + item["chaos"] > limit or lid in used:
                        continue
                    key = used | {lid} if lid in shared else used
                    nxt[key].append((cost + item["chaos"], value + item["score"], (*picks, item)))
        fronts = {k: _pareto(v) for k, v in nxt.items()}
    _, total, picks = max((st for front in fronts.values() for st in front), key=lambda t: (t[1], -t[0]))
    every: dict[Any, dict[str, Any]] = {}
    for i in (i for items in by_slot.values() for i in items):
        key = i["listingId"] or id(i)
        if key not in every or i["score"] > every[key]["score"]:
            every[key] = i
    return {
        "budget": budget,
        "score": score,
        "divineRate": divine_rate,
        "picks": list(picks),
        "totalChaos": sum(p["chaos"] for p in picks),
        "totalScore": total,
        "bestPerChaos": sorted(every.values(), key=lambda i: (i["perChaos"] is not None, -(i["perChaos"] or 0.0)))[:top],  # free first
        "slots": sorted(by_slot),
        "skippedUnverified": skipped,
        "skippedNeedDivineRate": need_rate,
    }
