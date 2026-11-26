"""Offline boss/invitation/fragment/map drop lookup backed by trimmed APT `item-drop.json`."""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..guardrails import GuardrailScope, check_input, validate_url
from .apt_non_gear import resolve_non_gear_en
from .slang import resolve_player_slang
from .tw_norm import _fold_search_text, normalize_tw_query

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "apt_item_drops.json.gz"

_NS_LABEL: dict[str, str] = {
    "U": "unique",
    "I": "item",
    "D": "divination_card",
    "G": "gem",
    "A": "area",
}


@dataclass(frozen=True)
class _DropEntity:
    name: str
    zh_name: str
    category: str
    folded_keys: tuple[str, ...]

    def to_public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "category": self.category}
        if self.zh_name:
            out["zhName"] = self.zh_name
        return out


@dataclass(frozen=True)
class _DropGroup:
    sources: tuple[_DropEntity, ...]
    items: tuple[_DropEntity, ...]


@dataclass(frozen=True)
class _DropIndex:
    groups: tuple[_DropGroup, ...]
    sources_by_item_en: dict[str, tuple[str, ...]]


def _parse_entity(row: list[Any]) -> _DropEntity | None:
    if not isinstance(row, list) or len(row) < 3:
        return None
    ns = str(row[0] or "I")
    name = str(row[1] or "").strip()
    zh = str(row[2] or "").strip()
    if not name:
        return None
    keys: list[str] = [_fold_search_text(name)]
    if zh:
        zf = _fold_search_text(zh)
        if zf and zf not in keys:
            keys.append(zf)
    if len(row) >= 4 and row[3]:
        for alt in str(row[3]).split("|"):
            af = _fold_search_text(alt.strip())
            if af and af not in keys:
                keys.append(af)
    return _DropEntity(
        name=name,
        zh_name=zh,
        category=_NS_LABEL.get(ns, "item"),
        folded_keys=tuple(keys),
    )


@lru_cache(maxsize=1)
def _load_drop_index() -> _DropIndex:
    if not _DATA_PATH.is_file():
        return _DropIndex(groups=(), sources_by_item_en={})
    payload = json.loads(gzip.decompress(_DATA_PATH.read_bytes()).decode("utf-8"))
    raw_groups = payload.get("g") if isinstance(payload, dict) else None
    if not isinstance(raw_groups, list):
        return _DropIndex(groups=(), sources_by_item_en={})

    groups: list[_DropGroup] = []
    by_item: dict[str, list[str]] = {}
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        sources = tuple(e for r in (g.get("q") or []) if (e := _parse_entity(r)) is not None)
        items = tuple(e for r in (g.get("i") or []) if (e := _parse_entity(r)) is not None)
        if not sources or not items:
            continue
        groups.append(_DropGroup(sources=sources, items=items))
        src_names = [s.name for s in sources]
        for it in items:
            en_key = _fold_search_text(it.name)
            bucket = by_item.setdefault(en_key, [])
            for sn in src_names:
                if sn not in bucket:
                    bucket.append(sn)

    return _DropIndex(
        groups=tuple(groups),
        sources_by_item_en={k: tuple(v) for k, v in by_item.items()},
    )


def lookup_item_drop_sources(name_en: str | None) -> list[str]:
    """Return canonical drop-source names for a unique/item/divcard, or `[]` if not boss/pool-restricted."""
    if not name_en:
        return []
    key = _fold_search_text(str(name_en).strip())
    if not key:
        return []
    return list(_load_drop_index().sources_by_item_en.get(key, ()))


def _entity_match_tier(entity: _DropEntity, probes: tuple[str, ...]) -> int | None:
    best: int | None = None
    for probe in probes:
        for key in entity.folded_keys:
            if key == probe:
                return 0
            if key.startswith(probe):
                tier = 1
            elif probe in key:
                tier = 2
            else:
                continue
            if best is None or tier < best:
                best = tier
    return best


def query_drop_sources(query: str, *, limit: int = 20) -> dict[str, Any]:
    """Bidirectional boss/fragment -> loot (forward) and unique/item -> drop-sources (reverse) lookup."""
    t0 = time.perf_counter()
    q_clean = str(query or "").strip()
    if not q_clean:
        raise ValueError("Drop query cannot be empty.")
    validate_url(q_clean, expected_game="poe1", cli_subcommand="data drop")
    q_tw = normalize_tw_query(q_clean)
    warnings = check_input(q_clean, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    if q_tw and q_tw != q_clean:
        seen_rids = {w.rule_id for w in warnings}
        for w_tw in check_input(q_tw, scope=GuardrailScope.DATA_QUERY, expected_game="poe1"):
            if w_tw.rule_id not in seen_rids:
                warnings.append(w_tw)
                seen_rids.add(w_tw.rule_id)

    resolved_alias: dict[str, str] | None = None
    raw_probes: list[str] = [q_clean]
    if q_tw and q_tw != q_clean:
        raw_probes.append(q_tw)

    slang_hit = resolve_player_slang(q_tw) or (resolve_player_slang(q_clean) if q_tw != q_clean else None)
    if slang_hit is not None:
        raw_probes.append(slang_hit[0])
        resolved_alias = {"from": q_clean, "to": slang_hit[0]}
    non_gear_en = resolve_non_gear_en(q_clean) or (resolve_non_gear_en(q_tw) if q_tw != q_clean else None)
    if non_gear_en and non_gear_en not in raw_probes:
        raw_probes.append(non_gear_en)
        if resolved_alias is None and _fold_search_text(non_gear_en) != _fold_search_text(q_clean):
            resolved_alias = {"from": q_clean, "to": non_gear_en}

    probes = tuple(dict.fromkeys(pf for p in raw_probes if (pf := _fold_search_text(p))))
    idx = _load_drop_index()

    forward_scored: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    reverse_by_item: dict[str, tuple[int, _DropEntity, list[_DropEntity]]] = {}

    for grp in idx.groups:
        src_tiers = [t for s in grp.sources if (t := _entity_match_tier(s, probes)) is not None]
        if src_tiers:
            best_t = min(src_tiers)
            mtype = "exact" if best_t == 0 else ("prefix" if best_t == 1 else "substring")
            forward_scored.append(
                (
                    (best_t, len(grp.sources[0].name), grp.sources[0].name.lower()),
                    {
                        "matchType": mtype,
                        "sources": [s.to_public() for s in grp.sources],
                        "items": [it.to_public() for it in grp.items],
                    },
                )
            )

        for it in grp.items:
            it_tier = _entity_match_tier(it, probes)
            if it_tier is None:
                continue
            en_k = _fold_search_text(it.name)
            prev = reverse_by_item.get(en_k)
            if prev is None:
                reverse_by_item[en_k] = (it_tier, it, list(grp.sources))
            else:
                prev_tier, prev_ent, prev_srcs = prev
                seen_src = {s.name for s in prev_srcs}
                for s in grp.sources:
                    if s.name not in seen_src:
                        seen_src.add(s.name)
                        prev_srcs.append(s)
                if it_tier < prev_tier:
                    reverse_by_item[en_k] = (it_tier, prev_ent, prev_srcs)

    forward_scored.sort(key=lambda row: row[0])
    forward = [row[1] for row in forward_scored]

    reverse_scored = sorted(
        reverse_by_item.values(),
        key=lambda row: (row[0], len(row[1].name), row[1].name.lower()),
    )
    reverse = [
        {
            "matchType": "exact" if tier == 0 else ("prefix" if tier == 1 else "substring"),
            "item": ent.to_public(),
            "dropSources": [s.to_public() for s in srcs],
        }
        for tier, ent, srcs in reverse_scored
    ]

    if limit > 0:
        forward = forward[:limit]
        reverse = reverse[:limit]

    out: dict[str, Any] = {
        "kind": "drop",
        "query": q_clean,
        "source": "apt",
        "count": len(forward) + len(reverse),
        "forward": forward,
        "reverse": reverse,
        "elapsedMs": round((time.perf_counter() - t0) * 1000.0, 2),
    }
    if resolved_alias is not None:
        out["resolvedAlias"] = resolved_alias
    if warnings:
        out["warnings"] = [w.to_dict() for w in warnings]
    return out


def query_drop_sources_multi(queries: list[str], *, limit: int = 20) -> dict[str, Any]:
    """Run multiple offline drop queries and aggregate results for variadic `pob data drop`."""
    grouped = {q: query_drop_sources(q, limit=limit) for q in queries}
    return {
        "kind": "drop",
        "queries": queries,
        "source": "apt",
        "count": sum(int(sub.get("count") or 0) for sub in grouped.values()),
        "results": grouped,
    }
