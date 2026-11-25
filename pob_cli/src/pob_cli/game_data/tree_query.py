"""0ms daemon-free passive tree search over the local treedata SQLite index."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..guardrails import GuardrailScope, check_input
from .paths import _sqlite_ro_uri
from .slang import resolve_player_slang
from .tw_norm import _fold_search_text, normalize_tw_query

_TREE_TYPE_RANK: dict[str, int] = {
    "Keystone": 0,
    "Ascendancy": 1,
    "Bloodline": 1,
    "Notable": 2,
    "Mastery": 3,
    "Socket": 4,
    "Normal": 5,
}


def _expand_tree_queries(
    raw_queries: list[str], asc_clean: str | None
) -> tuple[list[tuple[str, str]], list[str], list[dict[str, str]]]:
    """Normalize TW + slang aliases on passive tree queries and --asc."""
    resolved_aliases: list[dict[str, str]] = []
    expanded_queries: list[tuple[str, str]] = []
    for q in raw_queries:
        q_tw = normalize_tw_query(q)
        qf = _fold_search_text(q_tw)
        sl = resolve_player_slang(q_tw) or resolve_player_slang(q)
        if sl is not None:
            resolved_aliases.append({"from": q, "to": sl[0]})
            expanded_queries.append((qf, _fold_search_text(sl[0])))
        else:
            expanded_queries.append((qf, ""))

    asc_folded = ""
    asc_alt = ""
    if asc_clean:
        asc_tw = normalize_tw_query(asc_clean)
        asc_folded = _fold_search_text(asc_tw)
        sl_a = resolve_player_slang(asc_tw) or resolve_player_slang(asc_clean)
        if sl_a is not None:
            resolved_aliases.append({"from": asc_clean, "to": sl_a[0]})
            asc_alt = _fold_search_text(sl_a[0])

    asc_targets = [x for x in (asc_folded, asc_alt) if x]
    return expanded_queries, asc_targets, resolved_aliases


def _match_tree_row_tier(
    expanded_queries: list[tuple[str, str]], name_l: str, zh_l: str, search_l: str
) -> int:
    """Return best match tier (0..3) or 99 if unmatched."""
    best_q_tier = 99
    for q_prim, q_alt in expanded_queries:
        for cand_q in (q_prim, q_alt):
            if not cand_q:
                continue
            toks = [t for t in cand_q.split() if t]
            if not toks:
                continue
            if cand_q == name_l or (zh_l and cand_q == zh_l):
                best_q_tier = min(best_q_tier, 0)
            elif name_l.startswith(cand_q) or (zh_l and zh_l.startswith(cand_q)):
                best_q_tier = min(best_q_tier, 1)
            elif cand_q in name_l or (zh_l and cand_q in zh_l):
                best_q_tier = min(best_q_tier, 2)
            elif all(t in search_l for t in toks):
                best_q_tier = min(best_q_tier, 3)
    return best_q_tier


def _filter_and_score_tree_rows(
    rows: list[tuple[Any, ...]],
    type_low: str,
    asc_targets: list[str],
    expanded_queries: list[tuple[str, str]],
) -> list[tuple[tuple[int, int, int, int, str], dict[str, Any]]]:
    asc_matches_active_exact = False
    if asc_targets:
        for r in rows:
            if r[9] == 0 and ((r[5] and r[5] in asc_targets) or (r[6] and r[6] in asc_targets)):
                asc_matches_active_exact = True
                break

    scored: list[tuple[tuple[int, int, int, int, str], dict[str, Any]]] = []
    for (
        _nid,
        name_l,
        zh_l,
        ntype,
        asc_id_l,
        asc_name_l,
        asc_zh_l,
        is_asc,
        is_bl,
        is_alt_asc,
        has_stats,
        search_l,
        dj,
    ) in rows:
        zh_l = zh_l or ""
        asc_id_l = asc_id_l or ""
        asc_name_l = asc_name_l or ""
        asc_zh_l = asc_zh_l or ""

        if type_low:
            if type_low in ("asc", "ascend", "ascendancy"):
                if not is_asc:
                    continue
            elif type_low == "bloodline":
                if not is_bl:
                    continue
            elif ntype.lower() != type_low:
                continue

        if asc_targets:
            if not is_asc:
                continue
            if asc_matches_active_exact:
                if is_alt_asc or not (asc_name_l in asc_targets or asc_zh_l in asc_targets):
                    continue
            elif not any((t in asc_name_l or t in asc_zh_l or t == asc_id_l) for t in asc_targets):
                continue

        if not expanded_queries and asc_targets and ntype == "Normal" and not has_stats:
            continue

        match_tier = 2
        if expanded_queries:
            best_q_tier = _match_tree_row_tier(expanded_queries, name_l, zh_l, search_l)
            if best_q_tier >= 99:
                continue
            match_tier = best_q_tier

        scored.append(
            (
                (is_alt_asc, match_tier, _TREE_TYPE_RANK.get(ntype, 6), 0 if has_stats else 1, name_l),
                json.loads(dj),
            )
        )

    scored.sort(key=lambda item: item[0])
    return scored


def query_tree_data(
    queries: list[str] | str | None = None,
    *,
    node_type: str | None = None,
    asc: str | None = None,
    limit: int = 25,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """0ms daemon-free search directly over PoB's local `TreeData/3_30/tree.lua` + ZH translation overlay."""
    import pob_cli.game_data as _gd

    t0 = time.perf_counter()
    if isinstance(queries, str):
        raw_queries = [queries.strip()] if queries.strip() else []
    else:
        raw_queries = [str(q).strip() for q in (queries or []) if str(q or "").strip()]

    asc_clean = str(asc or "").strip() or None
    type_clean = str(node_type or "").strip() or None
    warnings: list[dict[str, Any]] = []
    for q in raw_queries:
        ws = check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
        warnings.extend(w.to_dict() for w in ws)
    if asc_clean:
        ws_a = check_input(asc_clean, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
        warnings.extend(w.to_dict() for w in ws_a)

    expanded_queries, asc_targets, resolved_aliases = _expand_tree_queries(raw_queries, asc_clean)

    db_path, tree_ver = _gd.ensure_treedata_index(
        pob_dir=pob_dir, runtime_dir=runtime_dir, force_rebuild=force_rebuild
    )
    limit = int(limit)
    type_low = type_clean.lower() if type_clean else ""

    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        rows = conn.execute(
            """
            SELECT id, name_lower, zh_lower, node_type, asc_id_lower, asc_name_lower, asc_zh_lower,
                   is_asc, is_bloodline, is_alt_asc, has_stats, search_text_lower, data_json
            FROM tree_nodes
            """
        ).fetchall()

        scored = _filter_and_score_tree_rows(rows, type_low, asc_targets, expanded_queries)

        results: list[dict[str, Any]] = []
        seen_small: set[tuple[str, str, tuple[str, ...]]] = set()
        for _, nd in scored:
            if nd["type"] == "Normal" and not nd.get("isBloodline"):
                skey = (nd["name"], str(nd.get("ascName") or ""), tuple(nd.get("stats") or ()))
                if skey in seen_small:
                    continue
                seen_small.add(skey)
            results.append(nd)
            if limit > 0 and len(results) >= limit:
                break
    finally:
        conn.close()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    out: dict[str, Any] = {
        "kind": "tree",
        "queries": raw_queries,
        "filters": {"type": type_clean, "asc": asc_clean, "limit": limit},
        "treeVersion": tree_ver,
        "count": len(results),
        "totalMatched": len(scored),
        "elapsedMs": elapsed_ms,
        "results": results,
    }
    if resolved_aliases:
        out["resolvedAliases"] = resolved_aliases
    if warnings:
        out["warnings"] = warnings
    return out
