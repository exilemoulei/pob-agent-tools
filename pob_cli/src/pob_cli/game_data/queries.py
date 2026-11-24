"""Public query entrypoints for local game data, gem bounds, and tick breakpoints."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ..guardrails import GuardrailScope, check_input, validate_url
from ..ticks import build_server_tick_table
from .constants import (
    _ACTIVE_AWAKENED_EXCEPTIONAL_LOWER,
    _EXCEPTIONAL_GEM_FALLBACK_BOUNDS,
    _POB_ESCAPE_RE,
    _REMOVED_LEGACY_GEM_NAMES_LOWER,
)
from .grounding import _build_zero_result_hint, _suggest_similar
from .paths import _sqlite_ro_uri
from .quality import _enrich_gem_quality_stats
from .queries_core import _query_bases, _query_gems, _query_mods, _query_uniques
from .queries_translate import _query_translate
from .slang import resolve_player_slang
from .tw_norm import _fold_search_text, is_legacy_awakened_gem, normalize_tw_query

_TICK_BASE_CD_PRESETS: dict[str, tuple[float, str]] = {
    "coc": (0.15, "Cast On Critical Strike (0.15s)"),
    "cospri": (0.25, "Cospri's Malice (0.25s)"),
    "mjolner": (0.25, "Mjölner (0.25s)"),
    "poet": (0.25, "The Poet's Pen (0.25s)"),
    "cwdt": (0.25, "Cast when Damage Taken (0.25s)"),
    "soa": (0.50, "The Sword of Ages / Temporal Mirage (0.50s)"),
    "mirage": (0.50, "Temporal Mirage (0.50s)"),
    "hidden": (0.50, "The Hidden Blade (0.50s)"),
    "spellslinger": (0.60, "Spellslinger (0.60s)"),
    "kitava": (0.10, "Kitava's Thirst (0.10s)"),
}


def _dispatch_query(
    conn: sqlite3.Connection,
    kind_norm: str,
    q_folded: str,
    *,
    slot: str | None,
    ilvl: int | None,
    limit: int,
    zh_available: bool,
    mod_filter: str | None = None,
) -> list[dict[str, Any]]:
    if limit == 0:
        return []
    if kind_norm == "gem":
        return _query_gems(conn, q_folded, limit)
    if kind_norm == "unique":
        return _query_uniques(conn, q_folded, slot, ilvl, limit, mod_filter=mod_filter)
    if kind_norm == "base":
        return _query_bases(conn, q_folded, slot, ilvl, limit)
    if kind_norm == "mod":
        return _query_mods(conn, q_folded, slot, ilvl, limit)
    return _query_translate(conn, q_folded, limit, zh_available)


def _guardrail_scope_for_kind(kind_norm: str) -> Any:
    if kind_norm == "gem":
        return GuardrailScope.GEM_NAME
    if kind_norm == "mod":
        return GuardrailScope.STAT_QUERY
    return GuardrailScope.DATA_QUERY


def _annotate_legacy_gem_rows(results: list[dict[str, Any]], kind_norm: str) -> None:
    """Explicitly tag retained legacy-only PoB `Gems.lua` rows (e.g. Divine Blessing)."""
    if kind_norm != "gem":
        return
    _enrich_gem_quality_stats(results, kind_norm)
    for r in results:
        if not isinstance(r, dict):
            continue
        nm_low = str(r.get("name") or "").strip().lower()
        disp_low = str(r.get("display_name") or "").strip().lower()
        if nm_low in _REMOVED_LEGACY_GEM_NAMES_LOWER or disp_low in _REMOVED_LEGACY_GEM_NAMES_LOWER:
            r["legacyOnly"] = True
            r["removedInPatch"] = "3.25"
            r["legacyNote"] = (
                "REMOVED in 3.25 (retained in PoB Gems.lua solely for importing pre-3.25 Standard characters; "
                "drop-disabled & unobtainable in 3.25+ leagues)."
            )


def get_gem_level_bounds(name: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    """Return level bounds and 3.28+ Exceptional / legacy-Awakened metadata for a PoE1 gem."""
    import pob_cli.game_data as _gd

    if not name:
        return None
    cleaned = _POB_ESCAPE_RE.sub("", str(name)).strip()
    if not cleaned:
        return None

    norm = normalize_tw_query(cleaned).strip()
    slang_hit = resolve_player_slang(norm)
    if slang_hit is not None and slang_hit[1] == "gem":
        norm = slang_hit[0]

    stripped_disp = re.sub(r"\s+support$", "", norm, flags=re.IGNORECASE).strip()
    norm_low = _fold_search_text(norm)
    stripped_low = _fold_search_text(stripped_disp)

    resolved_db: Path | None = None
    if db_path is not None:
        cand = Path(db_path)
        if cand.is_file():
            resolved_db = cand
    else:
        try:
            cand = _gd.ensure_gamedata_index()
            if cand.is_file():
                resolved_db = cand
        except (FileNotFoundError, OSError, sqlite3.Error):
            resolved_db = None

    if resolved_db is not None:
        try:
            conn = sqlite3.connect(_sqlite_ro_uri(resolved_db), uri=True)
            try:
                row = conn.execute(
                    """
                    SELECT data_json FROM gems
                    WHERE name_lower IN (?, ?) OR display_name_lower IN (?, ?) OR zh_lower IN (?, ?)
                    ORDER BY is_item_skill ASC
                    LIMIT 1
                    """,
                    (norm_low, stripped_low, norm_low, stripped_low, norm_low, stripped_low),
                ).fetchone()
                if row is not None:
                    obj = json.loads(row[0])
                    g_name = str(obj.get("name") or stripped_disp)
                    legacy_328 = (
                        bool(obj["legacy_removed_328"])
                        if "legacy_removed_328" in obj
                        else is_legacy_awakened_gem(g_name)
                    )
                    is_exc = (
                        bool(obj["is_exceptional"])
                        if "is_exceptional" in obj
                        else (
                            "exceptional" in {str(t).lower() for t in (obj.get("tags") or [])}
                            or _fold_search_text(g_name) in _EXCEPTIONAL_GEM_FALLBACK_BOUNDS
                        )
                    )
                    nat_max = int(
                        obj.get("natural_max_level")
                        or (
                            4
                            if _fold_search_text(g_name) in _ACTIVE_AWAKENED_EXCEPTIONAL_LOWER
                            else (5 if legacy_328 else (3 if is_exc else 20))
                        )
                    )
                    corrupt_max = int(obj.get("corrupted_max_level") or (nat_max + 1))
                    return {
                        "name": g_name,
                        "natural_max_level": nat_max,
                        "corrupted_max_level": corrupt_max,
                        "is_exceptional": is_exc,
                        "legacy_removed_328": legacy_328,
                    }
            finally:
                conn.close()
        except (OSError, sqlite3.Error, ValueError):
            pass

    fallback = _EXCEPTIONAL_GEM_FALLBACK_BOUNDS.get(stripped_low)
    if fallback is not None:
        return dict(fallback)

    if is_legacy_awakened_gem(norm):
        return {
            "name": stripped_disp,
            "natural_max_level": 5,
            "corrupted_max_level": 6,
            "is_exceptional": False,
            "legacy_removed_328": True,
        }

    return None


def query_local_data(
    kind: str,
    query: str = "",
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    slot: str | None = None,
    ilvl: int | None = None,
    mod_filter: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Query local PoB game data (`gem`, `unique`, `mod`, `base`, `translate`) via SQLite index."""
    import pob_cli.game_data as _gd

    kind_norm = kind.strip().lower()
    if kind_norm not in ("gem", "unique", "mod", "base", "translate"):
        raise ValueError(
            f"Unsupported kind {kind!r}; expected one of: gem, unique, mod, base, translate."
        )

    q_clean = str(query or "").strip()
    q_tw = normalize_tw_query(q_clean) if q_clean else ""
    mod_clean = str(mod_filter or "").strip() or None
    mod_tw = normalize_tw_query(mod_clean) if mod_clean else None
    if mod_clean and kind_norm != "unique":
        raise ValueError("--mod filter is only supported for kind='unique'.")
    if q_clean:
        validate_url(q_clean, expected_game="poe1", cli_subcommand=f"data {kind_norm}")
    q_scope = _guardrail_scope_for_kind(kind_norm)
    warnings = check_input(q_clean, scope=q_scope, expected_game="poe1") if q_clean else []
    if q_tw and q_tw != q_clean:
        seen_rids = {w.rule_id for w in warnings}
        for w_tw in check_input(q_tw, scope=q_scope, expected_game="poe1"):
            if w_tw.rule_id not in seen_rids:
                warnings.append(w_tw)
                seen_rids.add(w_tw.rule_id)
    if mod_tw:
        warnings.extend(check_input(mod_tw, scope=GuardrailScope.STAT_QUERY, expected_game="poe1"))
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")

    db_path = _gd.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    limit = max(0, int(limit))
    q_lower = _fold_search_text(q_tw)

    resolved_alias: dict[str, str] | None = None
    suggestions: list[dict[str, Any]] | None = None
    hint: str | None = None

    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        zh_available = meta.get("zh_available") == "1"
        results = _dispatch_query(
            conn,
            kind_norm,
            q_lower,
            slot=slot,
            ilvl=ilvl,
            limit=limit,
            zh_available=zh_available,
            mod_filter=mod_tw,
        )
        needs_slang = (not results) or (
            kind_norm == "translate" and not any(r.get("match_type") == "exact" for r in results)
        )
        if needs_slang and limit > 0 and q_tw:
            slang_hit = resolve_player_slang(q_tw) or (resolve_player_slang(q_clean) if q_tw != q_clean else None)
            if slang_hit is not None:
                canon_en, _ = slang_hit
                slang_res = _dispatch_query(
                    conn,
                    kind_norm,
                    _fold_search_text(canon_en),
                    slot=slot,
                    ilvl=ilvl,
                    limit=limit,
                    zh_available=zh_available,
                    mod_filter=mod_tw,
                )
                if slang_res:
                    results = slang_res
                    resolved_alias = {"from": q_clean, "to": canon_en}
            if not results and kind_norm != "translate":
                from .zh_match import english_for_zh

                en_canon = english_for_zh(q_clean, pob_dir=pob_dir)
                if en_canon and _fold_search_text(en_canon) != q_lower:
                    sib_res = _dispatch_query(
                        conn,
                        kind_norm,
                        _fold_search_text(en_canon),
                        slot=slot,
                        ilvl=ilvl,
                        limit=limit,
                        zh_available=zh_available,
                        mod_filter=mod_tw,
                    )
                    if sib_res:
                        results = sib_res
                        resolved_alias = {"from": q_clean, "to": en_canon}
            if not results and not mod_tw:
                suggestions = _suggest_similar(conn, kind_norm, q_lower, limit=5, zh_available=zh_available)
                hint = _build_zero_result_hint(kind_norm, q_clean, suggestions)
        elif not results and mod_tw and limit > 0:
            slang_mod = resolve_player_slang(mod_tw)
            if slang_mod is not None:
                canon_mod, _ = slang_mod
                slang_res = _dispatch_query(
                    conn,
                    kind_norm,
                    q_lower,
                    slot=slot,
                    ilvl=ilvl,
                    limit=limit,
                    zh_available=zh_available,
                    mod_filter=canon_mod,
                )
                if slang_res:
                    results = slang_res
                    resolved_alias = {"from": mod_clean or mod_tw, "to": canon_mod}
        _annotate_legacy_gem_rows(results, kind_norm)
    finally:
        conn.close()

    out: dict[str, Any] = {
        "kind": kind_norm,
        "query": q_clean,
        "filters": {"slot": slot, "ilvl": ilvl, "mod": mod_clean, "limit": limit},
        "pobVersion": meta.get("pob_version", "unknown"),
        "dataVersion": meta.get("data_version", "unknown"),
        "zhAvailable": zh_available,
        "count": len(results),
        "results": results,
    }
    if resolved_alias is not None:
        out["resolvedAlias"] = resolved_alias
    if suggestions is not None:
        out["suggestions"] = suggestions
    if hint is not None:
        out["hint"] = hint
    if warnings:
        out["warnings"] = [w.to_dict() for w in warnings]
    return out


def query_multi(
    db: Path | str | sqlite3.Connection | None = None,
    kind: str = "",
    queries: list[str] | None = None,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    slot: str | None = None,
    ilvl: int | None = None,
    mod_filter: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Run multiple local game-data queries in one SQLite connection for variadic `pob data`."""
    import pob_cli.game_data as _gd

    kind_norm = str(kind).strip().lower()
    if kind_norm not in ("gem", "unique", "mod", "base", "translate"):
        raise ValueError(
            f"Unsupported kind {kind!r}; expected one of: gem, unique, mod, base, translate."
        )

    clean_queries = list(
        dict.fromkeys(q.strip() for q in (queries or []) if isinstance(q, str) and q.strip())
    )
    if not clean_queries:
        raise ValueError("queries must contain at least one non-empty query string.")

    q_scope = _guardrail_scope_for_kind(kind_norm)
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    mod_clean = str(mod_filter or "").strip() or None
    mod_tw = normalize_tw_query(mod_clean) if mod_clean else None
    warnings_by_query: dict[str, list[dict[str, Any]]] = {}
    q_tw_map: dict[str, str] = {}
    for q_clean in clean_queries:
        validate_url(q_clean, expected_game="poe1", cli_subcommand=f"data {kind_norm}")
        q_tw = normalize_tw_query(q_clean)
        q_tw_map[q_clean] = q_tw
        ws = check_input(q_clean, scope=q_scope, expected_game="poe1")
        if q_tw and q_tw != q_clean:
            seen_rids = {w.rule_id for w in ws}
            for w_tw in check_input(q_tw, scope=q_scope, expected_game="poe1"):
                if w_tw.rule_id not in seen_rids:
                    ws.append(w_tw)
                    seen_rids.add(w_tw.rule_id)
        if ws:
            warnings_by_query[q_clean] = [w.to_dict() for w in ws]

    limit = max(0, int(limit))
    own_conn = False
    if isinstance(db, sqlite3.Connection):
        conn = db
    else:
        db_path = Path(db) if db is not None else _gd.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
        conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
        own_conn = True

    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        zh_available = meta.get("zh_available") == "1"
        pob_ver = meta.get("pob_version", "unknown")
        data_ver = meta.get("data_version", "unknown")
        results_by_query: dict[str, dict[str, Any]] = {}
        for q_clean in clean_queries:
            q_tw = q_tw_map[q_clean]
            q_folded = _fold_search_text(q_tw)
            res = _dispatch_query(
                conn,
                kind_norm,
                q_folded,
                slot=slot,
                ilvl=ilvl,
                limit=limit,
                zh_available=zh_available,
                mod_filter=mod_tw,
            )
            resolved_alias: dict[str, str] | None = None
            suggestions: list[dict[str, Any]] | None = None
            hint: str | None = None
            needs_slang_m = (not res) or (
                kind_norm == "translate" and not any(r.get("match_type") == "exact" for r in res)
            )
            if needs_slang_m and limit > 0 and q_tw:
                slang_hit = resolve_player_slang(q_tw) or (resolve_player_slang(q_clean) if q_tw != q_clean else None)
                if slang_hit is not None:
                    canon_en, _ = slang_hit
                    slang_res = _dispatch_query(
                        conn,
                        kind_norm,
                        _fold_search_text(canon_en),
                        slot=slot,
                        ilvl=ilvl,
                        limit=limit,
                        zh_available=zh_available,
                        mod_filter=mod_tw,
                    )
                    if slang_res:
                        res = slang_res
                        resolved_alias = {"from": q_clean, "to": canon_en}
                if not res and not mod_tw:
                    suggestions = _suggest_similar(conn, kind_norm, q_folded, limit=5, zh_available=zh_available)
                    hint = _build_zero_result_hint(kind_norm, q_clean, suggestions)
            _annotate_legacy_gem_rows(res, kind_norm)
            entry: dict[str, Any] = {
                "kind": kind_norm,
                "query": q_clean,
                "filters": {"slot": slot, "ilvl": ilvl, "mod": mod_clean, "limit": limit},
                "pobVersion": pob_ver,
                "dataVersion": data_ver,
                "zhAvailable": zh_available,
                "count": len(res),
                "results": res,
            }
            if resolved_alias is not None:
                entry["resolvedAlias"] = resolved_alias
            if suggestions is not None:
                entry["suggestions"] = suggestions
            if hint is not None:
                entry["hint"] = hint
            if q_clean in warnings_by_query:
                entry["warnings"] = warnings_by_query[q_clean]
            results_by_query[q_clean] = entry
    finally:
        if own_conn:
            conn.close()

    out_multi: dict[str, Any] = {
        "kind": kind_norm,
        "queries": clean_queries,
        "filters": {"slot": slot, "ilvl": ilvl, "mod": mod_clean, "limit": limit},
        "pobVersion": pob_ver,
        "dataVersion": data_ver,
        "zhAvailable": zh_available,
        "count": sum(sub["count"] for sub in results_by_query.values()),
        "results": results_by_query,
    }
    dedup_warns: list[dict[str, Any]] = []
    seen_warn_keys: set[tuple[str, str]] = set()
    for ws in warnings_by_query.values():
        for w in ws:
            wkey = (str(w.get("ruleId") or ""), str(w.get("trigger") or ""))
            if wkey not in seen_warn_keys:
                seen_warn_keys.add(wkey)
                dedup_warns.append(w)
    if dedup_warns:
        out_multi["warnings"] = dedup_warns
    return out_multi


def query_tick_breakpoints(
    base_cd_spec: str | float,
    *,
    cdr_pct: float | None = None,
    aps: float | None = None,
    triggered_spells: int = 1,
) -> dict[str, Any]:
    """Daemon-free 0ms PoE1 0.033s (33ms) server-tick cooldown breakpoint calculator."""
    raw = str(base_cd_spec or "").strip()
    low = raw.lower()
    preset_label: str | None = None
    if low in _TICK_BASE_CD_PRESETS:
        base_cd, preset_label = _TICK_BASE_CD_PRESETS[low]
    elif low.endswith("ms"):
        try:
            base_cd = float(low[:-2].strip()) / 1000.0
        except ValueError as exc:
            raise ValueError(
                f"Invalid base cooldown {base_cd_spec!r}; pass seconds (e.g. 0.15), ms (150ms), or preset ({', '.join(_TICK_BASE_CD_PRESETS)})."
            ) from exc
    else:
        try:
            base_cd = float(low.rstrip("s").strip())
        except ValueError as exc:
            raise ValueError(
                f"Invalid base cooldown {base_cd_spec!r}; pass seconds (e.g. 0.15), ms (150ms), or preset ({', '.join(_TICK_BASE_CD_PRESETS)})."
            ) from exc
    if base_cd <= 0:
        raise ValueError(f"Base cooldown must be > 0 (got {base_cd}).")

    tbl = build_server_tick_table(
        base_cd,
        cdr_pct=cdr_pct,
        aps=aps,
        triggered_spells=triggered_spells,
    )
    tbl["kind"] = "tick"
    if preset_label:
        tbl["preset"] = preset_label
    return tbl
