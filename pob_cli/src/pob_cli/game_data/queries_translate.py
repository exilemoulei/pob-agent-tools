"""Bidirectional EN/ZH translation search against the local SQLite index."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .constants import _CJK_PARTICLE_RE, _CJK_RE
from .queries_core import _build_fts_match, _fts_phrase
from .tw_norm import _fold_search_text, normalize_tw_query
from .zh_match import resolve_zh_entity_across_locales


def _query_translate_no_zh(conn: sqlite3.Connection, q_lower: str, limit: int) -> list[dict[str, Any]]:
    """Graceful fallback when Data/poe1/zh-rCN and Data/poe1/zh-rTW are absent."""
    results: list[dict[str, Any]] = []
    for tbl, cat in (("gems", "gem"), ("uniques", "unique"), ("bases", "base")):
        for name, name_l in conn.execute(
            f"SELECT name, name_lower FROM {tbl} WHERE instr(name_lower, ?) > 0 ORDER BY length(name) LIMIT ?",
            (q_lower, limit),
        ).fetchall():
            match_type = "exact" if name_l == q_lower else ("prefix" if name_l.startswith(q_lower) else "substring")
            results.append({"en": name, "zh": None, "category": cat, "match_type": match_type})
    results.sort(key=lambda r: (0 if r["match_type"] == "exact" else (1 if r["match_type"] == "prefix" else 2), len(r["en"])))
    return results[:limit]


def _query_translate_candidates(
    conn: sqlite3.Connection, q_lower: str, limit: int
) -> list[tuple[int, str, str, str, str, str, int]]:
    seen_ids: set[int] = set()
    candidates: list[tuple[int, str, str, str, str, str, int]] = []

    exact_rows = conn.execute(
        """
        SELECT id, en, en_lower, zh, zh_lower, category, cat_rank
        FROM translations
        WHERE en_lower = ? OR zh_lower = ?
        LIMIT 100
        """,
        (q_lower, q_lower),
    ).fetchall()
    for row in exact_rows:
        if row[0] not in seen_ids:
            seen_ids.add(row[0])
            candidates.append(row)

    prefix_rows = conn.execute(
        """
        SELECT id, en, en_lower, zh, zh_lower, category, cat_rank
        FROM translations
        WHERE (en_lower >= ? AND en_lower < ?) OR (zh_lower >= ? AND zh_lower < ?)
        LIMIT 150
        """,
        (q_lower, q_lower + "\uffff", q_lower, q_lower + "\uffff"),
    ).fetchall()
    for row in prefix_rows:
        if row[0] not in seen_ids:
            seen_ids.add(row[0])
            candidates.append(row)

    if len(candidates) < limit:
        has_non_ascii = any(ord(ch) > 127 for ch in q_lower)
        fts_expr, tokens = _build_fts_match(q_lower)
        sub_rows: list[Any] = []
        if has_non_ascii and fts_expr:
            sub_rows = conn.execute(
                """
                SELECT t.id, t.en, t.en_lower, t.zh, t.zh_lower, t.category, t.cat_rank
                FROM translations_zh_fts f
                JOIN translations t ON t.id = f.rowid
                WHERE translations_zh_fts MATCH ?
                LIMIT 250
                """,
                (fts_expr,),
            ).fetchall()
        elif not has_non_ascii and tokens:
            en_match = " AND ".join(_fts_phrase(t) for t in tokens)
            sub_rows = conn.execute(
                """
                SELECT t.id, t.en, t.en_lower, t.zh, t.zh_lower, t.category, t.cat_rank
                FROM translations_en_fts f
                JOIN translations t ON t.id = f.rowid
                WHERE translations_en_fts MATCH ?
                LIMIT 250
                """,
                (en_match,),
            ).fetchall()

        if not sub_rows:
            sub_rows = conn.execute(
                """
                SELECT id, en, en_lower, zh, zh_lower, category, cat_rank
                FROM translations
                WHERE instr(en_lower, ?) > 0 OR instr(zh_lower, ?) > 0
                LIMIT 250
                """,
                (q_lower, q_lower),
            ).fetchall()
            tokens = [q_lower]

        for row in sub_rows:
            if row[0] in seen_ids:
                continue
            combined = row[2] + " " + row[4]
            if tokens and not all(t in combined for t in tokens):
                continue
            seen_ids.add(row[0])
            candidates.append(row)

    return candidates


def _query_translate_entity_fallback(
    conn: sqlite3.Connection, q_lower: str
) -> list[tuple[tuple[int, int, int, str], dict[str, Any]]]:
    """Folded substring fallback across indexed uniques/gems/bases/mods when translations had 0 hits."""
    scored: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    for tbl, cat, rank in (("uniques", "unique", 1), ("gems", "gem", 1), ("bases", "base", 1)):
        for name, name_l, zh_val, zh_l in conn.execute(
            f"""
            SELECT name, name_lower, zh_name, zh_lower
            FROM {tbl}
            WHERE instr(name_lower, ?) > 0 OR (zh_lower IS NOT NULL AND instr(zh_lower, ?) > 0)
            LIMIT 50
            """,
            (q_lower, q_lower),
        ).fetchall():
            zh_l_s = zh_l or ""
            if name_l == q_lower or (zh_l_s and zh_l_s == q_lower):
                tier, mtype = 0, "exact"
            elif name_l.startswith(q_lower) or (zh_l_s and zh_l_s.startswith(q_lower)):
                tier, mtype = 1, "prefix"
            else:
                tier, mtype = 2, "substring"
            scored.append(
                (
                    (tier, rank, len(name_l), name_l),
                    {"en": name, "zh": zh_val, "category": cat, "match_type": mtype},
                )
            )
    if scored:
        return scored

    fts_expr, _ = _build_fts_match(q_lower)
    if fts_expr:
        mod_rows = conn.execute(
            """
            SELECT m.data_json
            FROM mods_fts f
            JOIN mods m ON m.rowid = f.rowid
            WHERE mods_fts MATCH ?
            LIMIT 30
            """,
            (fts_expr,),
        ).fetchall()
    else:
        mod_rows = conn.execute(
            """
            SELECT data_json
            FROM mods
            WHERE instr(stats_en_lower, ?) > 0 OR instr(stats_zh_lower, ?) > 0
            LIMIT 30
            """,
            (q_lower, q_lower),
        ).fetchall()
    for (data_json,) in mod_rows:
        m_obj = json.loads(data_json)
        en_stat = (m_obj.get("stats") or [""])[0]
        zh_stat = next((s for s in (m_obj.get("stats_zh") or []) if s), None)
        if en_stat and zh_stat:
            scored.append(
                (
                    (2, 3, len(en_stat), _fold_search_text(en_stat)),
                    {"en": en_stat, "zh": zh_stat, "category": "mod", "match_type": "substring"},
                )
            )
    return scored


def _query_translate_cjk_fallback(
    conn: sqlite3.Connection, q_lower: str
) -> list[tuple[tuple[int, int, int, str], dict[str, Any]]]:
    """Partial CJK fallback (particle-stripped + high-overlap short-entity bigram/char match)."""
    q_cjk = _CJK_PARTICLE_RE.sub("", q_lower)
    cjk_chars = [ch for ch in q_cjk if _CJK_RE.match(ch)]
    if len(cjk_chars) < 2:
        return []

    scored: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    p_rows = conn.execute(
        """
        SELECT en, en_lower, zh, zh_lower, category, cat_rank
        FROM translations
        WHERE cat_rank <= 2
          AND instr(
                replace(
                  replace(
                    replace(
                      replace(
                        replace(
                          replace(zh_lower, '之', ''),
                        '的', ''),
                      '・', ''),
                    '·', ''),
                  '．', ''),
                ' ', ''),
                ?
              ) > 0
        LIMIT 80
        """,
        (q_cjk,),
    ).fetchall()
    for en, en_l, zh, zh_l, cat, cat_rank in p_rows:
        zh_stripped = _CJK_PARTICLE_RE.sub("", zh_l)
        tier = 1 if zh_stripped == q_cjk else 2
        mtype = "substring" if zh_stripped == q_cjk else "partial"
        scored.append(((tier, cat_rank, len(zh_l), en_l), {"en": en, "zh": zh, "category": cat, "match_type": mtype}))

    max_target_len = max(len(q_cjk) * 3, 12)
    q_char_set = set(cjk_chars)
    min_char_hits = max(2, int(-(-len(q_char_set) * 3 // 4)))

    if not scored and len(q_cjk) <= 6:
        bigrams = list(dict.fromkeys(q_cjk[i : i + 2] for i in range(len(q_cjk) - 1)))
        bg_where = " OR ".join("instr(zh_lower, ?) > 0" for _ in bigrams)
        bg_rows = conn.execute(
            f"""
            SELECT en, en_lower, zh, zh_lower, category, cat_rank
            FROM translations
            WHERE cat_rank <= 2 AND ({bg_where})
            ORDER BY length(zh_lower) ASC
            LIMIT 400
            """,
            bigrams,
        ).fetchall()
        for en, en_l, zh, zh_l, cat, cat_rank in bg_rows:
            if len(_CJK_PARTICLE_RE.sub("", zh_l)) > max_target_len:
                continue
            ch_hits = sum(1 for ch in q_char_set if ch in zh_l)
            if ch_hits < min_char_hits or (ch_hits / len(q_char_set)) < 0.75:
                continue
            bg_hits = sum(1 for bg in bigrams if bg in zh_l)
            rank_tier = 30 - min(bg_hits, 9) * 3 - min(ch_hits, 9)
            scored.append(((rank_tier, cat_rank, len(zh_l), en_l), {"en": en, "zh": zh, "category": cat, "match_type": "partial"}))

    if not scored and len(q_cjk) <= 6:
        uniq_chars = list(dict.fromkeys(cjk_chars))
        ch_where = " OR ".join("instr(zh_lower, ?) > 0" for _ in uniq_chars)
        ch_rows = conn.execute(
            f"""
            SELECT en, en_lower, zh, zh_lower, category, cat_rank
            FROM translations
            WHERE cat_rank <= 2 AND ({ch_where})
            ORDER BY length(zh_lower) ASC
            LIMIT 400
            """,
            uniq_chars,
        ).fetchall()
        for en, en_l, zh, zh_l, cat, cat_rank in ch_rows:
            if len(_CJK_PARTICLE_RE.sub("", zh_l)) > max_target_len:
                continue
            ch_hits = sum(1 for ch in q_char_set if ch in zh_l)
            if ch_hits < min_char_hits or (ch_hits / len(q_char_set)) < 0.75:
                continue
            rank_tier = 50 - min(ch_hits, 9) * 5
            scored.append(((rank_tier, cat_rank, len(zh_l), en_l), {"en": en, "zh": zh, "category": cat, "match_type": "partial"}))

    return scored


def _lookup_canonical_en_in_conn(
    conn: sqlite3.Connection, canon_en: str
) -> list[tuple[tuple[int, int, int, str], dict[str, Any]]]:
    en_folded = _fold_search_text(canon_en)
    rows = conn.execute(
        """
        SELECT en, en_lower, zh, zh_lower, category, cat_rank
        FROM translations
        WHERE en_lower = ?
        ORDER BY cat_rank ASC, length(zh_lower) ASC
        LIMIT 5
        """,
        (en_folded,),
    ).fetchall()
    out: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    for en, en_l, zh, zh_l, cat, cat_rank in rows:
        out.append(((0, cat_rank, min(len(en_l), len(zh_l)), en_l), {"en": en, "zh": zh, "category": cat, "match_type": "exact"}))
    return out


def _query_translate(conn: sqlite3.Connection, q_lower: str, limit: int, zh_available: bool) -> list[dict[str, Any]]:
    raw_query = q_lower
    if q_lower:
        q_lower = _fold_search_text(normalize_tw_query(q_lower))
    if not q_lower:
        return []
    if not zh_available:
        return _query_translate_no_zh(conn, q_lower, limit)

    candidates = _query_translate_candidates(conn, q_lower, limit)
    scored: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    has_exact = False
    for _, en, en_l, zh, zh_l, cat, cat_rank in candidates:
        if en_l == q_lower or zh_l == q_lower:
            tier, mtype = 0, "exact"
            has_exact = True
        elif en_l.startswith(q_lower) or zh_l.startswith(q_lower):
            tier, mtype = 1, "prefix"
        else:
            tier, mtype = 2, "substring"
        scored.append(
            (
                (tier, cat_rank, min(len(en_l), len(zh_l)), en_l),
                {"en": en, "zh": zh, "category": cat, "match_type": mtype},
            )
        )

    if not has_exact and (_CJK_RE.search(raw_query) or _CJK_RE.search(q_lower)):
        sibling_en = resolve_zh_entity_across_locales(raw_query) or resolve_zh_entity_across_locales(q_lower)
        if sibling_en:
            scored.extend(_lookup_canonical_en_in_conn(conn, sibling_en))

    if not scored:
        scored = _query_translate_entity_fallback(conn, q_lower)
    if not scored and _CJK_RE.search(q_lower):
        scored = _query_translate_cjk_fallback(conn, q_lower)

    scored.sort(key=lambda item: item[0])
    dedup_out: list[dict[str, Any]] = []
    seen_out: set[tuple[str, str | None]] = set()
    for _, item in scored:
        key = (_fold_search_text(item["en"]), item["zh"])
        if key not in seen_out:
            seen_out.add(key)
            dedup_out.append(item)
            if len(dedup_out) >= limit:
                break
    return dedup_out
