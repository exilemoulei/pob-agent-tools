"""3-tier deterministic PoE1 build valuation (`item 55`: Tier A ninja, Tier B live P25, Tier C rares)."""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

from ..external_data import fetch_currency_chaos_rates
from ..external_data.ninja_http import _fetch_dense_overviews, _fetch_stash_overviews_batch
from ..locale_cfg import normalize_realm
from ..trade_client import NoResults, RateLimited, TradeError, extract_listing_trade_meta, seller_url
from .appraise_ninja import (
    load_ninja_tables,
    lookup_ninja_item_chaos,
    price_anoint,
)
from .shopping_list import build_shopping_list

_DEFINING_MOD_KINDS: frozenset[str] = frozenset({"watchers_eye", "forbidden_jewel", "radius_jewel"})
_MAX_FETCH_WINDOW = 30
_FETCH_CHUNK_SIZE = 10
_TARGET_DISTINCT_SELLERS = 5

_SLOT_CLI_SLUG: dict[str, str] = {
    "Weapon 1": "weapon1",
    "Weapon 2": "weapon2",
    "Weapon 1 Swap": "weapon1",
    "Weapon 2 Swap": "weapon2",
    "Helmet": "helmet",
    "Body Armour": "body_armour",
    "Gloves": "gloves",
    "Boots": "boots",
    "Amulet": "amulet",
    "Ring 1": "ring1",
    "Ring 2": "ring2",
    "Ring 3": "ring3",
    "Belt": "belt",
}


def _listing_price_and_meta(row: dict[str, Any], rates: dict[str, float]) -> tuple[float | None, str, int | None, str]:
    meta = extract_listing_trade_meta(row)
    amt = meta["priceAmount"]
    rate = rates.get(str(meta["priceCurrency"] or ""))
    chaos = float(amt) * float(rate) if (amt is not None and rate) else None
    return chaos, str(meta["tradeMode"]), meta["goldFee"], str(meta["seller"] or "")


def compute_deduped_p25_quote(
    rows: list[dict[str, Any]],
    rates: dict[str, float],
    *,
    prefer_securable: bool = True,
) -> dict[str, Any] | None:
    """Compute 1-seller-1-vote P25 (or median for <4 sellers) so a lone 1c fixer never skews price."""
    all_entries: list[tuple[float, str, int | None, str]] = []
    sec_entries: list[tuple[float, str, int | None, str]] = []
    for row in rows:
        chaos, mode, fee, seller = _listing_price_and_meta(row, rates)
        if chaos is None or chaos <= 0:
            continue
        item = (chaos, mode, fee, seller)
        all_entries.append(item)
        if mode == "securable":
            sec_entries.append(item)

    def _dedupe(pool: list[tuple[float, str, int | None, str]]) -> list[tuple[float, str, int | None, str]]:
        by_seller: dict[str, tuple[float, str, int | None, str]] = {}
        anon: list[tuple[float, str, int | None, str]] = []
        for entry in pool:
            s_key = entry[3].lower()
            if not s_key:
                anon.append(entry)
                continue
            prev = by_seller.get(s_key)
            if prev is None or entry[0] < prev[0]:
                by_seller[s_key] = entry
        merged = [*by_seller.values(), *anon]
        merged.sort(key=lambda e: e[0])
        return merged

    sec_deduped = _dedupe(sec_entries)
    pool = sec_deduped if (prefer_securable and len(sec_deduped) >= 3) else _dedupe(all_entries)
    if not pool:
        return None

    prices = [e[0] for e in pool]
    n = len(prices)
    if n >= 4:
        anchor_idx = max(1, n // 4)
        anchor_chaos = prices[anchor_idx]
        method = "p25_1seller1vote"
        rep = pool[anchor_idx]
    elif n >= 2:
        anchor_chaos = float(statistics.median(prices))
        method = "median_1seller1vote"
        rep = min(pool, key=lambda e: abs(e[0] - anchor_chaos))
    else:
        anchor_chaos = prices[0]
        method = "single_listing"
        rep = pool[0]

    return {
        "chaos": round(anchor_chaos, 2),
        "method": method,
        "sampleSellers": n,
        "tradeMode": rep[1],
        "goldFee": rep[2],
        "seller": rep[3] or None,
        "singleListingWarning": n == 1,
    }


def _classify_item_tier(item_rec: dict[str, Any], ninja_chaos: float | None) -> tuple[str, int, str]:
    kind = str(item_rec.get("kind") or "")
    if kind in ("rare", "rare_jewel", "magic_flask"):
        return "C", 99, "crafted_rare_or_magic"

    if kind == "forbidden_jewel":
        return "B", 1, "forbidden_notable_specific"
    if kind == "watchers_eye":
        return "B", 1, "watchers_eye_mod_combo"
    if item_rec.get("foulborn"):
        return "B", 1, "foulborn_mutated_unique"

    if kind == "radius_jewel":
        return "B", 2, "radius_or_keystone_variant"
    if kind == "timeless_jewel" and item_rec.get("timelessSeed"):
        return "B", 2, "timeless_seed_specific"
    if kind == "unique" and (item_rec.get("links") or 0) >= 5:
        return "B", 2, f"{item_rec['links']}L_unique_premium"
    if kind == "unique" and item_rec.get("corrupted"):
        return "B", 2, "corrupted_implicit_unique"

    if kind == "cluster_jewel":
        notables = item_rec.get("clusterNotables") or []
        if len(notables) >= 2:
            return "B", 3, f"{len(notables)}_notable_cluster"
        return "C", 99, "craftable_cluster_base"

    stats_groups = ((item_rec.get("query") or {}).get("query") or {}).get("stats") or []
    has_stat_filters = bool(stats_groups and (stats_groups[0].get("filters") or []))
    if kind == "unique" and has_stat_filters:
        return "B", 3, "key_roll_unique"

    if kind == "gem" and ninja_chaos is None and (
        item_rec.get("isTransfigured") or item_rec.get("isAwakened") or item_rec.get("isExceptional")
    ):
        return "B", 3, "unpriced_special_gem"

    return "A", 10, "ninja_floor"


def _search_command_hint(slot: str, kind: str) -> str:
    if kind == "magic_flask":
        return f"pob trade search --slot {slot.lower().replace(' ', '')} --securable"
    slug = _SLOT_CLI_SLUG.get(slot)
    if slug:
        return f"pob trade search --slot {slug} --securable"
    if kind == "rare_jewel":
        return "pob trade search --slot jewel --securable"
    return "pob trade search --category jewel.cluster --securable"


def _has_unresolved_defining_mods(rec: dict[str, Any]) -> bool:
    if rec.get("definingModsUnresolved"):
        return True
    kind = str(rec.get("kind") or "")
    if kind not in _DEFINING_MOD_KINDS:
        return False
    stats_groups = ((rec.get("query") or {}).get("query") or {}).get("stats") or []
    return not any(
        isinstance(flt, dict) and not str(flt.get("id") or "").lower().startswith("implicit.")
        for grp in stats_groups
        if isinstance(grp, dict)
        for flt in (grp.get("filters") or ())
    )


def _fetch_rows_for_distinct_sellers(client: Any, ids: list[str], qid: str) -> list[dict[str, Any]]:
    """Fetch up to 30 listing IDs in 10-ID chunks, stopping once >=5 distinct sellers are observed."""
    window = ids[:_MAX_FETCH_WINDOW]
    if not window:
        return []
    fetch_fn = getattr(client, "fetch", None)
    if not callable(fetch_fn):
        return list(client.fetch_many(window, qid, limit=len(window)) or [])

    collected: list[dict[str, Any]] = []
    distinct_sellers: set[str] = set()
    for start in range(0, len(window), _FETCH_CHUNK_SIZE):
        chunk_ids = window[start : start + _FETCH_CHUNK_SIZE]
        chunk_rows = list(fetch_fn(chunk_ids, qid) or [])
        collected.extend(chunk_rows)
        for row in chunk_rows:
            seller = str(extract_listing_trade_meta(row).get("seller") or "").strip().lower()
            if seller:
                distinct_sellers.add(seller)
        if len(distinct_sellers) >= _TARGET_DISTINCT_SELLERS:
            break
    return collected


def _apply_live_tier_b(
    enriched: list[dict[str, Any]],
    tier_b_candidates: list[tuple[int, float, int]],
    *,
    client: Any,
    league: str,
    eff_realm: str,
    max_live_queries: int,
    rates: dict[str, float],
    div_rate: float,
    warnings_out: list[str],
) -> tuple[int, bool]:
    live_used = 0
    rate_limited_fallback = False
    if max_live_queries <= 0 or client is None:
        return live_used, rate_limited_fallback

    tier_b_candidates.sort(key=lambda tup: (tup[0], tup[1]))
    for _prio, _neg_c, idx in tier_b_candidates:
        if live_used >= max_live_queries or rate_limited_fallback:
            break
        rec = enriched[idx]
        if _has_unresolved_defining_mods(rec):
            rec["definingModsUnresolved"] = True
            rec["liveVerified"] = False
            rec["liveStatus"] = "skipped_defining_mods_unresolved"
            warnings_out.append(
                f"Skipped live pricing for {rec.get('slot')} ({rec.get('name')}): defining modifiers unresolved."
            )
            continue
        try:
            live_used += 1
            found = client.search(league, rec["query"])
            qid = str(found.get("id") or "")
            ids = [str(x) for x in (found.get("result") or [])]
            rows = _fetch_rows_for_distinct_sellers(client, ids, qid) if ids else []
            quote_res = compute_deduped_p25_quote(rows, rates, prefer_securable=True)
            if quote_res is not None:
                live_c = float(quote_res["chaos"])
                rec["tier"] = "B"
                rec["liveVerified"] = True
                rec["liveChaos"] = live_c
                rec["liveDivine"] = round(live_c / div_rate, 3) if div_rate > 0 else None
                rec["priceChaos"] = live_c
                rec["priceDivine"] = rec["liveDivine"]
                rec["priceSource"] = f"trade_live_{quote_res['method']}"
                rec["tradeMode"] = quote_res["tradeMode"]
                rec["goldFee"] = quote_res["goldFee"]
                rec["sampleSellers"] = quote_res["sampleSellers"]
                if quote_res.get("singleListingWarning"):
                    rec["singleListingWarning"] = True
                if quote_res.get("seller"):
                    rec["sellerUrl"] = seller_url(league, rec["query"]["query"], quote_res["seller"], realm=eff_realm)
                floor_c = rec.get("ninjaFloorChaos")
                if floor_c and floor_c > 0:
                    rec["deltaVsFloorChaos"] = round(live_c - floor_c, 2)
                    rec["deltaVsFloorPct"] = round(((live_c / floor_c) - 1.0) * 100.0, 1)
            else:
                rec["liveStatus"] = "unpriced_listings"
        except RateLimited as exc:
            live_used -= 1
            rate_limited_fallback = True
            rec["liveStatus"] = f"rate_limited_wait_{exc.seconds}s"
            break
        except NoResults:
            rec["liveStatus"] = "no_results"
        except TradeError as exc:
            rec["liveStatus"] = f"trade_error: {exc}"
    return live_used, rate_limited_fallback


def appraise_build(
    items_res: dict[str, Any],
    skills_res: dict[str, Any] | None,
    stat_entries: list[dict[str, Any]],
    *,
    league: str,
    realm: str = "global",
    status: str = "securable",
    include_gems: bool = True,
    max_live_queries: int = 8,
    client: Any | None = None,
    proxy: str | None = None,
    pob_dir: Path | None = None,
    dense_override: dict[str, list[dict[str, Any]]] | None = None,
    stash_override: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run 3-tier build valuation (Tier A poe.ninja, Tier B bounded live P25, Tier C crafted rares)."""
    t0 = time.perf_counter()
    eff_realm = normalize_realm(realm)
    shopping = build_shopping_list(
        items_res,
        skills_res,
        stat_entries,
        league=league,
        realm=eff_realm,
        status=status,
        include_gems=include_gems,
        pob_dir=pob_dir,
    )
    report_warnings: list[str] = list(shopping.get("warnings") or ())
    dense, stash, div_rate, rates, div_rate_source = load_ninja_tables(
        league,
        realm=eff_realm,
        proxy=proxy,
        dense_override=dense_override,
        stash_override=stash_override,
        fetch_dense=_fetch_dense_overviews,
        fetch_stash=_fetch_stash_overviews_batch,
        fetch_rates=fetch_currency_chaos_rates,
    )
    ninja_unavailable = not dense and not stash
    if eff_realm == "tw" and ninja_unavailable:
        report_warnings.append(
            "poe.ninja does not index TW (pathofexile.tw); using "
            f"{div_rate_source} divine rate ({div_rate:g}c) and promoting unpriced Tier A items to bounded live queries."
        )
    elif ninja_unavailable:
        report_warnings.append("poe.ninja economy data unavailable; Tier A floor prices could not be loaded.")

    anoint_priced = price_anoint(shopping.get("anoint"), dense, stash, div_rate)

    enriched: list[dict[str, Any]] = []
    tier_b_candidates: list[tuple[int, float, int]] = []
    for idx, it in enumerate(shopping["items"]):
        ninja_c = lookup_ninja_item_chaos(it, dense, stash)
        tier, prio, reason = _classify_item_tier(it, ninja_c)
        rec: dict[str, Any] = {
            **it,
            "tier": tier,
            "tierReason": reason,
            "ninjaFloorChaos": round(ninja_c, 2) if ninja_c is not None else None,
            "liveChaos": None,
            "liveDivine": None,
            "priceChaos": round(ninja_c, 2) if (tier != "C" and ninja_c is not None) else None,
            "priceDivine": round(ninja_c / div_rate, 3) if (tier != "C" and ninja_c is not None and div_rate > 0) else None,
            "priceSource": "poe_ninja" if (tier != "C" and ninja_c is not None) else ("tier_c_unpriced" if tier == "C" else "unpriced"),
            "liveVerified": False,
        }
        if tier == "C":
            rec["searchCommand"] = _search_command_hint(str(it.get("slot") or ""), str(it.get("kind") or ""))
        elif tier == "B":
            tier_b_candidates.append((prio, -(ninja_c or 0.0), idx))
        elif eff_realm == "tw" and ninja_c is None:
            tw_prio = 4 if str(it.get("section") or "") in ("gear", "flask", "jewel") else 5
            tier_b_candidates.append((tw_prio, 0.0, idx))
        enriched.append(rec)

    live_used, rate_limited_fallback = _apply_live_tier_b(
        enriched,
        tier_b_candidates,
        client=client,
        league=shopping["league"],
        eff_realm=eff_realm,
        max_live_queries=max(0, int(max_live_queries)),
        rates=rates,
        div_rate=div_rate,
        warnings_out=report_warnings,
    )

    tier_counts = {"A": 0, "B_live": 0, "B_fallback": 0, "C": 0}
    section_totals: dict[str, float] = {"gear": 0.0, "flask": 0.0, "jewel": 0.0, "gem": 0.0, "anoint": 0.0}
    unpriced_rares: list[str] = []
    unpriced_count = 0
    for rec in enriched:
        t = rec["tier"]
        if t == "A":
            tier_counts["A"] += 1
        elif t == "B":
            tier_counts["B_live" if rec.get("liveVerified") else "B_fallback"] += 1
        else:
            tier_counts["C"] += 1
            unpriced_rares.append(str(rec.get("slot") or rec.get("name") or "-"))
        p_c = rec.get("priceChaos")
        if p_c is not None:
            qty = int(rec.get("quantity") or 1)
            sec = str(rec.get("section") or "gear")
            section_totals[sec] = section_totals.get(sec, 0.0) + float(p_c) * qty
        else:
            unpriced_count += 1

    if anoint_priced and anoint_priced.get("priceChaos") is not None:
        section_totals["anoint"] = float(anoint_priced["priceChaos"])

    total_chaos = round(sum(section_totals.values()), 2)
    total_div = round(total_chaos / div_rate, 2) if div_rate > 0 else None
    sec_out = {
        sec: {
            "chaos": round(val, 2),
            "divine": round(val / div_rate, 2) if div_rate > 0 else None,
        }
        for sec, val in section_totals.items()
    }

    out: dict[str, Any] = {
        "mode": "L1_appraise_3tier",
        "realm": eff_realm,
        "league": shopping["league"],
        "status": status,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 1),
        "divineRate": div_rate,
        "divineRateSource": div_rate_source,
        "totalChaos": total_chaos,
        "totalDivine": total_div,
        "unpricedCount": unpriced_count,
        "sectionTotals": sec_out,
        "tierCounts": tier_counts,
        "liveQueriesUsed": live_used,
        "maxLiveQueries": max(0, int(max_live_queries)),
        "rateLimitedFallback": rate_limited_fallback,
        "unpricedRareSlots": unpriced_rares,
        "anoint": anoint_priced,
        "buildInfo": shopping.get("buildInfo"),
        "items": enriched,
    }
    if report_warnings:
        out["warnings"] = report_warnings
    return out
