"""SQLite unique-item queries with Chinese `仿品` / `赝品` (`Replica`) prefix-to-suffix matching."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .constants import MOD_TAG_PREFIX_RE
from .tw_norm import _fold_search_text, extract_zh_replica_stem, normalize_tw_query


def _unique_matching_mod_lines(obj: dict[str, Any], mod_tokens: list[str], mod_phrase: str) -> tuple[int, list[str]]:
    """Return `(tier, matched_lines)` for a unique item against a folded `--mod` query, or `(-1, [])` if unmatched."""
    en_lines: list[str] = []
    zh_lines: list[str] = []
    for k_en, k_zh in (("implicits", "implicits_zh"), ("explicits", "explicits_zh")):
        raw_en = obj.get(k_en) or []
        raw_zh = obj.get(k_zh) or []
        for idx, line in enumerate(raw_en):
            s_en = MOD_TAG_PREFIX_RE.sub("", str(line or "")).strip()
            s_zh = str(raw_zh[idx]).strip() if idx < len(raw_zh) and raw_zh[idx] else ""
            if s_en:
                en_lines.append(s_en)
                zh_lines.append(s_zh)
    for fb in obj.get("foulborn_mods") or []:
        fb_en = fb.get("stats") or []
        fb_zh = fb.get("stats_zh") or []
        for idx, line in enumerate(fb_en):
            s_en = MOD_TAG_PREFIX_RE.sub("", str(line or "")).strip()
            s_zh = str(fb_zh[idx]).strip() if idx < len(fb_zh) and fb_zh[idx] else ""
            if s_en:
                en_lines.append(f"[Foulborn] {s_en}")
                zh_lines.append(s_zh)

    exact_line_hits: list[str] = []
    token_line_hits: list[str] = []
    all_folded_parts: list[str] = []

    for s_en, s_zh in zip(en_lines, zh_lines, strict=False):
        f_en = _fold_search_text(s_en)
        f_zh = _fold_search_text(s_zh) if s_zh else ""
        comb = f"{f_en} {f_zh}".strip()
        all_folded_parts.append(comb)
        disp = f"{s_en} / {s_zh}" if s_zh else s_en
        if mod_phrase and (mod_phrase in f_en or (f_zh and mod_phrase in f_zh)):
            exact_line_hits.append(disp)
        elif mod_tokens and all(t in comb for t in mod_tokens):
            token_line_hits.append(disp)

    if exact_line_hits:
        return 0, exact_line_hits
    if token_line_hits:
        return 1, token_line_hits

    whole_text = " ".join(all_folded_parts)
    if mod_tokens and all(t in whole_text for t in mod_tokens):
        partial_hits = [
            f"{s_en} / {s_zh}" if s_zh else s_en
            for s_en, s_zh in zip(en_lines, zh_lines, strict=False)
            if any(t in _fold_search_text(f"{s_en} {s_zh}") for t in mod_tokens)
        ]
        return 2, partial_hits[:6]
    return -1, []


def _rank_zh_replica_row(
    rep_stem: str,
    rep_tokens: list[str],
    q_lower: str,
    name_l: str,
    zh_l: str,
    fb_l: str,
    zh_fb_l: str,
    hay_extra: str = "",
) -> int | None:
    if not rep_stem:
        return 1
    if q_lower in (name_l, zh_l, fb_l, zh_fb_l):
        return 0
    en_stem = name_l[8:].strip() if name_l.startswith("replica ") else ""
    _, zh_stem = extract_zh_replica_stem(zh_l)
    cand_zh = zh_stem or zh_l
    _, fb_zh_stem = extract_zh_replica_stem(zh_fb_l)
    cand_fb = fb_zh_stem or zh_fb_l
    cands = [c for c in (en_stem, cand_zh, cand_fb) if c]
    if any(c == rep_stem for c in cands):
        return 0
    if any(c.startswith(rep_stem) for c in cands):
        return 1
    if any(rep_stem in c for c in cands):
        return 2
    if len(rep_tokens) > 1 and any(all(t in c for t in rep_tokens) for c in cands):
        return 2
    if hay_extra and rep_tokens and all(t in hay_extra for t in rep_tokens):
        return 3
    return None


def _query_uniques(
    conn: sqlite3.Connection,
    q_lower: str,
    slot: str | None,
    ilvl: int | None,
    limit: int,
    *,
    mod_filter: str | None = None,
) -> list[dict[str, Any]]:
    from .queries_core import _build_fts_match, _normalize_slot_filter, _pool_limit_clause, _slice_limit

    if q_lower:
        q_lower = _fold_search_text(normalize_tw_query(q_lower))
    is_zh_rep, raw_rep_stem = extract_zh_replica_stem(q_lower)
    rep_stem = _fold_search_text(normalize_tw_query(raw_rep_stem)).strip() if is_zh_rep else ""
    rep_tokens = [t for t in rep_stem.split() if t] if rep_stem else []

    slot_spec = _normalize_slot_filter(slot) if slot else None
    mod_lower = _fold_search_text(normalize_tw_query(mod_filter or "")).strip()
    mod_tokens = [t for t in mod_lower.split() if t] if mod_lower else []

    pool_lim = _pool_limit_clause(limit)
    combined_q = " ".join(p for p in (q_lower, mod_lower) if p).strip()
    if is_zh_rep:
        rows = conn.execute(
            """
            SELECT name_lower, zh_lower, foulborn_lower, zh_foulborn_lower, slots_lower, data_json, search_text_lower
            FROM uniques
            WHERE name_lower LIKE 'replica %'
               OR (zh_lower IS NOT NULL AND (instr(zh_lower, '仿品') > 0 OR instr(zh_lower, '赝品') > 0))
            """
        ).fetchall()
    elif not combined_q:
        rows = conn.execute(
            f"SELECT name_lower, zh_lower, foulborn_lower, zh_foulborn_lower, slots_lower, data_json FROM uniques {pool_lim}"
        ).fetchall()
    else:
        fts_expr, _ = _build_fts_match(combined_q)
        if fts_expr:
            rows = conn.execute(
                f"""
                SELECT u.name_lower, u.zh_lower, u.foulborn_lower, u.zh_foulborn_lower, u.slots_lower, u.data_json
                FROM uniques_fts f
                JOIN uniques u ON u.rowid = f.rowid
                WHERE uniques_fts MATCH ?
                {pool_lim}
                """,
                (fts_expr,),
            ).fetchall()
        else:
            probe = mod_lower or q_lower
            rows = conn.execute(
                f"""
                SELECT name_lower, zh_lower, foulborn_lower, zh_foulborn_lower, slots_lower, data_json
                FROM uniques
                WHERE instr(search_text_lower, ?) > 0
                {pool_lim}
                """,
                (probe,),
            ).fetchall()
        if mod_lower and not rows:
            rows = conn.execute(
                "SELECT name_lower, zh_lower, foulborn_lower, zh_foulborn_lower, slots_lower, data_json FROM uniques"
            ).fetchall()

    q_tokens = [t for t in q_lower.split() if t] if (q_lower and not is_zh_rep) else []
    scored: list[tuple[tuple[int, int, int, str], dict[str, Any] | str]] = []
    for row in rows:
        name_l, zh_l, fb_l, zh_fb_l, slots_l, data_json = row[:6]
        zh_l = zh_l or ""
        fb_l = fb_l or ""
        zh_fb_l = zh_fb_l or ""
        if slot_spec:
            norm, wk_targets, _, master_types = slot_spec
            slot_tokens = set(slots_l.split())
            if not (
                norm in slot_tokens
                or any(k in slot_tokens for k in wk_targets)
                or any(mt.lower() in slots_l for mt in master_types)
            ):
                continue

        payload: dict[str, Any] | str = data_json
        mod_tier = 0
        hay_extra = ""
        if mod_lower or ilvl is not None or (q_tokens and mod_lower):
            obj = json.loads(data_json)
            if ilvl is not None and obj.get("req_level") is not None and int(obj["req_level"]) > ilvl:
                continue
            if mod_lower:
                mod_tier, matched_lines = _unique_matching_mod_lines(obj, mod_tokens, mod_lower)
                if mod_tier < 0:
                    continue
                obj["matched_mods"] = matched_lines
            hay_extra = _fold_search_text(
                " ".join(
                    str(x or "")
                    for x in (
                        obj.get("name"),
                        obj.get("zh_name"),
                        obj.get("base_type"),
                        obj.get("base_type_zh"),
                        obj.get("item_class"),
                    )
                )
            )
            if q_tokens and mod_lower and not all(t in hay_extra for t in q_tokens):
                continue
            payload = obj

        if is_zh_rep:
            # The rest of the query may name a base type, a class or a mod, as a plain search would
            # match it: the row's whole search text stands in when the name does not carry it.
            rep_tier = _rank_zh_replica_row(
                rep_stem, rep_tokens, q_lower, name_l, zh_l, fb_l, zh_fb_l, hay_extra=f"{hay_extra} {row[6]}"
            )
            if rep_tier is None:
                continue
            tier = rep_tier
        elif q_lower:
            if q_lower in (name_l, zh_l, fb_l, zh_fb_l):
                tier = 0
            elif any(s.startswith(q_lower) for s in (name_l, zh_l, fb_l, zh_fb_l) if s):
                tier = 1
            elif any(q_lower in s for s in (name_l, zh_l, fb_l, zh_fb_l) if s):
                tier = 2
            else:
                tier = 3
        else:
            tier = 0
        scored.append(((mod_tier, tier, len(name_l), name_l), payload))

    scored.sort(key=lambda item: item[0])
    return [item[1] if isinstance(item[1], dict) else json.loads(item[1]) for item in _slice_limit(scored, limit)]
