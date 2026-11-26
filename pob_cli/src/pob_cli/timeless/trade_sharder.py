"""Complexity-budgeted batch sharder, pre-encoded URL builder, and exact `(conqueror, seed)` lookup."""

from __future__ import annotations

import base64
import copy
import gzip
import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..locale_cfg import current_realm, normalize_realm, trade_base_url_for_realm
from ..trade_client import localize_query_for_tw, map_tw_league, unmapped_tw_ascii_fields
from ..trade_client.utils import _quote
from ..trade_query.auto_query import COMPLEXITY_LIMIT, query_complexity
from .lut import JEWEL_ZH_TW_NAMES, _parse_stat
from .trade_data import (
    CONQUEROR_TRADE_SLUG_OVERRIDES as CONQUEROR_TRADE_SLUG_OVERRIDES,
)
from .trade_data import (
    JEWEL_BASE_ZH_TW,
    canonical_conqueror_key,
    conqueror_trade_stat_id,
    parse_devotion_mods,
    pob_conqueror_name,
    resolve_conqueror_stat_ids,
    resolve_devotion_stat_ids,
    resolve_jewel_spec,
)
from .trade_data import (
    JEWEL_TRADE_SPECS as JEWEL_TRADE_SPECS,
)
from .trade_data import (
    MILITANT_FAITH_DEVOTION_STATS as MILITANT_FAITH_DEVOTION_STATS,
)
from .trade_data import (
    TRADE_SLUG_TO_POB_CONQUEROR as TRADE_SLUG_TO_POB_CONQUEROR,
)
from .trade_data import (
    ZH_CONQUEROR_TO_POB as ZH_CONQUEROR_TO_POB,
)

MAX_FILTERS_PER_COUNT_GROUP = 30
DEFAULT_COMPLEXITY_LIMIT = COMPLEXITY_LIMIT

_ASCII_RE = re.compile(r"[A-Za-z]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

__all__ = [
    "CONQUEROR_TRADE_SLUG_OVERRIDES",
    "DEFAULT_COMPLEXITY_LIMIT",
    "JEWEL_TRADE_SPECS",
    "MAX_FILTERS_PER_COUNT_GROUP",
    "MILITANT_FAITH_DEVOTION_STATS",
    "TRADE_SLUG_TO_POB_CONQUEROR",
    "ZH_CONQUEROR_TO_POB",
    "build_single_seed_query",
    "build_timeless_trade_plan",
    "canonical_conqueror_key",
    "conqueror_trade_stat_id",
    "encode_trade_search_url",
    "lookup_seed_outcome",
    "parse_devotion_mods",
    "pob_conqueror_name",
    "resolve_conqueror_stat_ids",
    "resolve_devotion_stat_ids",
    "resolve_jewel_spec",
]


def _safe_delta(after: Any, before: Any) -> tuple[float | None, float | None]:
    af, bf = _parse_stat(after), _parse_stat(before)
    if af is None or bf is None or not (math.isfinite(af) and math.isfinite(bf)):
        return None, None
    delta = round(af - bf, 4)
    pct = round(((af - bf) / abs(bf)) * 100.0, 2) if abs(bf) > 1e-9 else None
    return delta, pct


def _extract_outcome_notables(outcome: dict[str, Any], radius_summary: dict[str, Any] | None) -> list[str]:
    for key in ("transformedNotables", "notables", "keystones", "changedNotables"):
        raw = outcome.get(key)
        if isinstance(raw, list) and raw:
            out = [
                str(e.get("nameZh") or e.get("name") or "").strip() if isinstance(e, dict) else str(e).strip()
                for e in raw
            ]
            out = [x for x in out if x]
            if out:
                return out
    if isinstance(radius_summary, dict):
        return [
            str(n.get("nameZh") or n.get("name") or "").strip()
            for n in (radius_summary.get("allocatedNotables") or [])
            if isinstance(n, dict) and (n.get("nameZh") or n.get("name"))
        ]
    return []


def _flatten_ranked_candidates(
    search_result: dict[str, Any],
    *,
    canon_jewel: str,
    default_conquerors: Sequence[str],
    expand_unspecified_any: bool,
    max_seeds: int | None = None,
) -> list[dict[str, Any]]:
    """Flatten single- or multi-socket PoB timeless outcomes in descending gain order with exact conquerors."""
    stat = str(search_result.get("stat") or "CombinedDPS")
    top_baseline = search_result.get("baseline") or {}
    radius_by_socket: dict[int, dict[str, Any]] = {}
    baseline_by_socket: dict[int, dict[str, Any]] = {}
    conq_by_socket: dict[int, str] = {}

    if isinstance(search_result.get("socketResults"), list):
        for sres in search_result["socketResults"]:
            if not isinstance(sres, dict) or sres.get("socket") is None:
                continue
            sid = int(sres["socket"])
            if isinstance(sres.get("radiusSummary"), dict):
                radius_by_socket[sid] = sres["radiusSummary"]
            if isinstance(sres.get("baseline"), dict):
                baseline_by_socket[sid] = sres["baseline"]
            if isinstance(sres.get("conqueror"), str) and sres["conqueror"].strip():
                conq_by_socket[sid] = sres["conqueror"].strip()
        raw_items, default_socket = list(search_result.get("topSeeds") or []), None
    else:
        raw_items, default_socket = list(search_result.get("outcomes") or []), search_result.get("socket")
        if default_socket is not None and isinstance(search_result.get("radiusSummary"), dict):
            radius_by_socket[int(default_socket)] = search_result["radiusSummary"]

    res_conq = str(search_result.get("conqueror") or "").strip()
    candidates: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int]] = set()

    for rank_idx, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        stats = item.get("stats") or {}
        seeds = [int(s) for s in (item.get("seeds") or [])] or (
            [int(item["seed"])] if item.get("seed") is not None else []
        )
        sock = item.get("socket", default_socket)
        sock_int = int(sock) if sock is not None else None
        base = baseline_by_socket.get(sock_int, top_baseline) if sock_int is not None else top_baseline
        notables = _extract_outcome_notables(item, radius_by_socket.get(sock_int) if sock_int is not None else None)
        d_dps, d_dps_pct = _safe_delta(stats.get("CombinedDPS"), base.get("CombinedDPS"))
        d_ehp, d_ehp_pct = _safe_delta(stats.get("TotalEHP"), base.get("TotalEHP"))
        d_stat, d_stat_pct = _safe_delta(stats.get(stat), base.get(stat))

        row_conq_raw = str(item.get("conqueror") or "").strip()
        if row_conq_raw and row_conq_raw.lower() not in ("any", "*"):
            cand_conqs = resolve_conqueror_stat_ids(canon_jewel, row_conq_raw)[0]
        elif expand_unspecified_any:
            cand_conqs = list(default_conquerors)
        else:
            fallback_conq = (conq_by_socket.get(sock_int) if sock_int is not None else None) or res_conq
            if fallback_conq and fallback_conq.lower() not in ("any", "*"):
                cand_conqs = resolve_conqueror_stat_ids(canon_jewel, fallback_conq)[0]
            else:
                cand_conqs = list(default_conquerors[:1]) if default_conquerors else [""]

        if not cand_conqs:
            cand_conqs = [""]

        for seed in seeds:
            if expand_unspecified_any and not row_conq_raw and len(cand_conqs) > 1:
                dedup_key = ("*", seed)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                candidates.append(
                    {
                        "seed": seed, "rank": rank_idx, "conqueror": None, "conquerors": list(cand_conqs),
                        "socket": sock_int, "socketLabel": item.get("socketLabel"),
                        "stat": stat, "statValue": _parse_stat(stats.get(stat)),
                        "deltaStat": d_stat, "deltaStatPct": d_stat_pct,
                        "dps": _parse_stat(stats.get("CombinedDPS")), "deltaDps": d_dps, "deltaDpsPct": d_dps_pct,
                        "ehp": _parse_stat(stats.get("TotalEHP")), "deltaEhp": d_ehp, "deltaEhpPct": d_ehp_pct,
                        "life": _parse_stat(stats.get("Life")), "keyNotables": notables, "seedGroupSize": len(seeds),
                    }
                )
            else:
                cname = cand_conqs[0]
                dedup_key = (canonical_conqueror_key(cname), seed)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                candidates.append(
                    {
                        "seed": seed, "rank": rank_idx, "conqueror": cname or None,
                        "conquerors": [cname] if cname else [],
                        "socket": sock_int, "socketLabel": item.get("socketLabel"),
                        "stat": stat, "statValue": _parse_stat(stats.get(stat)),
                        "deltaStat": d_stat, "deltaStatPct": d_stat_pct,
                        "dps": _parse_stat(stats.get("CombinedDPS")), "deltaDps": d_dps, "deltaDpsPct": d_dps_pct,
                        "ehp": _parse_stat(stats.get("TotalEHP")), "deltaEhp": d_ehp, "deltaEhpPct": d_ehp_pct,
                        "life": _parse_stat(stats.get("Life")), "keyNotables": notables, "seedGroupSize": len(seeds),
                    }
                )
            if max_seeds is not None and len(candidates) >= max_seeds:
                return candidates
    return candidates


def _localize_timeless_query(
    body: dict[str, Any], *, realm: str, canon_jewel: str, base_type: str, pob_dir: Path | None = None
) -> dict[str, Any]:
    if realm != "tw":
        return body
    localized = localize_query_for_tw(body, pob_dir=pob_dir)
    inner = localized.get("query")
    if isinstance(inner, dict):
        name_val = str(inner.get("name") or "")
        if name_val and _ASCII_RE.search(name_val) and not _CJK_RE.search(name_val):
            inner["name"] = JEWEL_ZH_TW_NAMES.get(canon_jewel, name_val)
        type_val = str(inner.get("type") or "")
        if type_val and _ASCII_RE.search(type_val) and not _CJK_RE.search(type_val):
            inner["type"] = JEWEL_BASE_ZH_TW.get(base_type, type_val)
    return localized


def encode_trade_search_url(league: str, payload: dict[str, Any], *, realm: str = "global") -> str:
    """Build a zero-HTTP pre-encoded official PoE1 Trade browser URL from a search payload."""
    eff_realm = normalize_realm(realm)
    eff_league = map_tw_league(league) if eff_realm == "tw" else league.strip()
    inner = payload.get("query") if isinstance(payload.get("query"), dict) else payload
    blob = gzip.compress(json.dumps(inner, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), mtime=0)
    query_id = base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
    host = trade_base_url_for_realm(eff_realm).rstrip("/")
    return f"{host}/trade/search/{_quote(eff_league)}/{query_id}"


def _make_shard_payload(
    *, canon_jewel: str, base_type: str, count_filters: list[dict[str, Any]],
    devotion_stat_ids: Sequence[str], status: str, budget: float | None,
    currency: str, realm: str, pob_dir: Path | None,
) -> dict[str, Any]:
    stats_groups: list[dict[str, Any]] = [
        {"type": "count", "value": {"min": 1}, "filters": copy.deepcopy(count_filters)}
    ]
    if devotion_stat_ids:
        stats_groups.append({"type": "and", "filters": [{"id": sid} for sid in devotion_stat_ids]})
    inner: dict[str, Any] = {
        "status": {"option": status}, "name": canon_jewel, "type": base_type, "stats": stats_groups,
    }
    if budget is not None and budget > 0:
        cur = (currency or "divine").strip().lower()
        pf: dict[str, Any] = {"max": budget} if cur in ("", "chaos_equiv", "equiv") else {"option": cur, "max": budget}
        inner["filters"] = {"trade_filters": {"filters": {"price": pf}}}
    body: dict[str, Any] = {"query": inner, "sort": {"price": "asc"}}
    return _localize_timeless_query(body, realm=realm, canon_jewel=canon_jewel, base_type=base_type, pob_dir=pob_dir)


def build_single_seed_query(
    plan: dict[str, Any], seed: int, conqueror: str | None = None, *, pob_dir: Path | None = None
) -> dict[str, Any]:
    """Build a minimal localized single-seed inner `query` dict for direct seller/item URLs."""
    canon_jewel = str(plan.get("jewel") or "")
    base_type = str(plan.get("baseType") or "Timeless Jewel")
    realm = str(plan.get("realm") or "global")
    status = str(plan.get("status") or "securable")
    devotion_ids = list(plan.get("devotionStatIds") or [])
    stat_ids = (
        [conqueror_trade_stat_id(conqueror)]
        if conqueror and str(conqueror).strip()
        else list(plan.get("conquerorStatIds") or [])
    )
    filters = [{"id": sid, "value": {"min": int(seed), "max": int(seed)}} for sid in stat_ids]
    payload = _make_shard_payload(
        canon_jewel=canon_jewel, base_type=base_type, count_filters=filters,
        devotion_stat_ids=devotion_ids, status=status, budget=None,
        currency=str(plan.get("currency") or "divine"), realm=realm, pob_dir=pob_dir,
    )
    return dict(payload["query"])


def _filters_for_candidate(cand: dict[str, Any], fallback_stat_ids: Sequence[str]) -> list[dict[str, Any]]:
    seed = int(cand["seed"])
    conqs = [c for c in (cand.get("conquerors") or ()) if c]
    sids = [conqueror_trade_stat_id(c) for c in conqs] if conqs else list(fallback_stat_ids)
    return [{"id": sid, "value": {"min": seed, "max": seed}} for sid in sids]


def build_timeless_trade_plan(
    search_result: dict[str, Any], *, league: str, realm: str | None = None,
    conqueror: str | None = None, status: str = "securable", budget: float | None = None,
    currency: str = "divine", devotion_mods: Sequence[str] | None = None,
    max_seeds: int | None = None, max_shards: int | None = None,
    complexity_limit: int = DEFAULT_COMPLEXITY_LIMIT,
    max_filters_per_group: int = MAX_FILTERS_PER_COUNT_GROUP, pob_dir: Path | None = None,
) -> dict[str, Any]:
    """Shard ranked timeless outcomes into complexity-safe Trade API batches with exact lookup."""
    eff_realm = normalize_realm(realm) if realm is not None else current_realm()
    eff_league = map_tw_league(league) if eff_realm == "tw" else league.strip()
    canon_jewel, spec = resolve_jewel_spec(str(search_result.get("jewel") or ""))
    base_type = str(spec["base"])

    eff_conqueror = conqueror if conqueror is not None else search_result.get("conqueror")
    conqueror_names, stat_ids = resolve_conqueror_stat_ids(canon_jewel, eff_conqueror)
    raw_mods = devotion_mods if devotion_mods is not None else (search_result.get("mods") or ())
    devotion_stat_ids, unmatched_devotions = parse_devotion_mods(canon_jewel, raw_mods)

    is_any_req = (eff_conqueror is None or str(eff_conqueror).strip().lower() in ("any", "*")) and len(conqueror_names) > 1
    candidates = _flatten_ranked_candidates(
        search_result,
        canon_jewel=canon_jewel,
        default_conquerors=conqueror_names,
        expand_unspecified_any=is_any_req,
        max_seeds=max_seeds,
    )

    reverse_by_pair: dict[tuple[str, int], dict[str, Any]] = {}
    reverse_by_seed: dict[int, list[dict[str, Any]]] = {}
    for cand in candidates:
        seed = int(cand["seed"])
        reverse_by_seed.setdefault(seed, []).append(cand)
        for cname in cand.get("conquerors") or ():
            if cname:
                reverse_by_pair.setdefault((canonical_conqueror_key(cname), seed), cand)

    def _emit_shard(idx: int, s_cands: list[dict[str, Any]], s_filters: list[dict[str, Any]]) -> dict[str, Any]:
        payload = _make_shard_payload(
            canon_jewel=canon_jewel, base_type=base_type, count_filters=s_filters,
            devotion_stat_ids=devotion_stat_ids, status=status, budget=budget,
            currency=currency, realm=eff_realm, pob_dir=pob_dir,
        )
        comp = query_complexity(payload)
        if len(s_filters) > max_filters_per_group or comp > complexity_limit:
            raise ValueError(
                f"Shard {idx} exceeds Trade API budget ({len(s_filters)}/{max_filters_per_group} filters, "
                f"complexity {comp}/{complexity_limit})."
            )
        return {
            "batch": idx, "seeds": [int(c["seed"]) for c in s_cands], "seedCount": len(s_cands),
            "countFilterCount": len(s_filters), "devotionFilterCount": len(devotion_stat_ids),
            "filterCount": len(s_filters) + len(devotion_stat_ids), "complexity": comp,
            "payload": payload, "url": encode_trade_search_url(eff_league, payload, realm=eff_realm),
        }

    shards: list[dict[str, Any]] = []
    cur_cands: list[dict[str, Any]] = []
    cur_filters: list[dict[str, Any]] = []
    for cand in candidates:
        add_filters = _filters_for_candidate(cand, stat_ids)
        singleton_payload = _make_shard_payload(
            canon_jewel=canon_jewel, base_type=base_type, count_filters=add_filters,
            devotion_stat_ids=devotion_stat_ids, status=status, budget=budget,
            currency=currency, realm=eff_realm, pob_dir=pob_dir,
        )
        singleton_comp = query_complexity(singleton_payload)
        if len(add_filters) > max_filters_per_group or singleton_comp > complexity_limit:
            raise ValueError(
                f"Single candidate seed {cand['seed']} exceeds Trade API budget "
                f"({len(add_filters)}/{max_filters_per_group} filters, complexity {singleton_comp}/{complexity_limit})."
            )
        trial_filters = [*cur_filters, *add_filters]
        trial_payload = _make_shard_payload(
            canon_jewel=canon_jewel, base_type=base_type, count_filters=trial_filters,
            devotion_stat_ids=devotion_stat_ids, status=status, budget=budget,
            currency=currency, realm=eff_realm, pob_dir=pob_dir,
        )
        if cur_cands and (len(trial_filters) > max_filters_per_group or query_complexity(trial_payload) > complexity_limit):
            shards.append(_emit_shard(len(shards) + 1, cur_cands, cur_filters))
            cur_cands, cur_filters = [], []
            if max_shards is not None and len(shards) >= max_shards:
                break
        cur_cands.append(cand)
        cur_filters.extend(add_filters)
    if cur_cands and (max_shards is None or len(shards) < max_shards):
        shards.append(_emit_shard(len(shards) + 1, cur_cands, cur_filters))

    sharded_seeds = sum(int(s["seedCount"]) for s in shards)
    unmapped_tw = (
        [{"field": fld, "value": val} for fld, val in unmapped_tw_ascii_fields(shards[0]["payload"])]
        if (eff_realm == "tw" and shards)
        else []
    )
    return {
        "jewel": canon_jewel, "baseType": base_type, "conqueror": "any" if is_any_req else (conqueror_names[0] if conqueror_names else ""),
        "conquerors": conqueror_names, "conquerorStatIds": stat_ids,
        "devotionStatIds": devotion_stat_ids, "unmatchedDevotionMods": unmatched_devotions,
        "realm": eff_realm, "league": eff_league, "unmappedTwFields": unmapped_tw,
        "status": status, "budget": budget, "currency": currency,
        "stat": str(search_result.get("stat") or "CombinedDPS"), "complexityLimit": complexity_limit,
        "maxFiltersPerGroup": max_filters_per_group, "candidateCount": len(candidates),
        "shardedSeedCount": sharded_seeds, "truncatedShardCandidates": max(0, len(candidates) - sharded_seeds),
        "shards": shards, "reverseIndex": {"byConquerorSeed": reverse_by_pair, "bySeed": reverse_by_seed},
    }


def lookup_seed_outcome(plan: dict[str, Any], seed: int, conqueror: str | None = None) -> dict[str, Any] | None:
    """Resolve `(conqueror, seed)` strictly when `conqueror` is given; fallback to `seed` only when empty."""
    rev = plan.get("reverseIndex") or {}
    if conqueror and str(conqueror).strip():
        return (rev.get("byConquerorSeed") or {}).get((canonical_conqueror_key(conqueror), int(seed)))
    hits = (rev.get("bySeed") or {}).get(int(seed)) or []
    return hits[0] if hits else None
