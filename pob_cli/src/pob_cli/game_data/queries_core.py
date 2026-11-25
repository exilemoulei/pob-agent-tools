"""Core SQLite query implementations for gems, uniques, bases, and mods."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Sequence
from typing import Any

from .constants import MOD_TAG_PREFIX_RE, SLOT_ALIASES
from .tw_norm import _fold_search_text, fold_han, normalize_tw_query


def _fts_phrase(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def _pool_limit_clause(limit: int) -> str:
    return "" if limit <= 0 else f"LIMIT {max(2500, limit * 5)}"


def _slice_limit(items: list[Any], limit: int) -> list[Any]:
    return items if limit <= 0 else items[:limit]


def _build_fts_match(query_lower: str) -> tuple[str | None, list[str]]:
    tokens = [t for t in query_lower.split() if t]
    long_tokens = [t for t in tokens if len(t) >= 3]
    if not long_tokens:
        return None, tokens
    return " AND ".join(_fts_phrase(t) for t in long_tokens), tokens


def _normalize_slot_filter(slot: str) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    norm = re.sub(r"[\s\-]+", "_", fold_han(slot.strip().lower()))
    if norm in SLOT_ALIASES:
        wk, parent_tags, master_types = SLOT_ALIASES[norm]
        return norm, wk, parent_tags, master_types
    # Not a known slot: only mods that name it as a weight key or bench type match. With parent
    # tags it would inherit the default weight, and a typo would list every generic mod.
    return norm, (norm,), (), (slot.strip(),)


def _types_match_slot(
    types_list: list[str], norm: str, master_types: tuple[str, ...], *, allow_subtype: bool = False
) -> bool:
    if not types_list:
        return False
    types_lower = [t.lower() for t in types_list]
    types_set = set(types_lower)
    if any(mt.lower() in types_set for mt in master_types):
        return True
    if not allow_subtype:
        return norm in {re.sub(r"[\s\-]+", "_", tl) for tl in types_lower}
    for mt in master_types:
        mtl = mt.lower()
        if any(tl.startswith(mtl + ":") or tl.endswith(": " + mtl) for tl in types_lower):
            return True
    return norm in {re.sub(r"[\s\-]+", "_", tl.split(":", 1)[0].strip()) for tl in types_lower}


# One kind of base a slot stands for: the tags it carries, and the tags an influence can add to it.
BaseTagSet = tuple[frozenset[str], tuple[str, ...]]
# A base with no class tag of its own ("Random One Hand Sword") names a pick, not an item to roll on.
_PLACEHOLDER_BASE_TAGS = frozenset({"default", "not_for_sale"})
# ModExplicit and ModCorrupted cover every gear class, so their `default` weight is a real fallback.
# The other files hold one family each (jewel, tincture, eldritch), whose `default` is not for gear.
_DEFAULT_WEIGHT_SOURCES = frozenset({"Explicit", "Corrupted"})
# PoB rolls these bases from their own mod files, so a gear file's `default` never reaches them.
_OWN_MOD_FILE_TAGS = frozenset({"flask", "jewel", "abyss_jewel", "tincture", "animal_charm"})


def _slot_tag_sets(conn: sqlite3.Connection, master_types: Sequence[str]) -> tuple[BaseTagSet, ...]:
    """The distinct tag sets of the bases behind a slot's item classes; empty when the index has none."""
    wanted = [mt.lower() for mt in master_types if mt]
    if not wanted:
        return ()
    marks = ",".join("?" * len(wanted))
    try:
        rows = conn.execute(
            f"SELECT data_json FROM bases WHERE lower(item_type) IN ({marks})"
            f" OR lower(coalesce(sub_type, '') || ' ' || item_type) IN ({marks})",
            wanted + wanted,
        ).fetchall()
    except sqlite3.Error:
        return ()
    found: dict[BaseTagSet, None] = {}
    for (data_json,) in rows:
        base = json.loads(data_json)
        tags = frozenset(base.get("tags") or ())
        if tags - _PLACEHOLDER_BASE_TAGS:
            found[(tags, tuple(base.get("influence_tags") or ()))] = None
    return tuple(found)


def mod_rolls_on_base(
    source: str,
    weight_keys: Sequence[str],
    weights: dict[str, int],
    tags: Collection[str],
    influence_tags: Sequence[str] = (),
    *,
    elevated: bool = False,
) -> bool:
    """Whether a mod can roll on a base, by PoB's `Item:GetModSpawnWeight`.

    The first weight key the item carries decides, and its weight may be 0: `{ranged = 0, weapon =
    1000}` keeps a mod off every bow. An influence adds one tag to the base, so the base is tried
    bare and once with each influence tag the mod names. A Maven-elevated mod has weight 0 in the
    data and exists only on a base with its influence, which is how PoB weighs it too.
    """
    default_counts = source in _DEFAULT_WEIGHT_SOURCES and _OWN_MOD_FILE_TAGS.isdisjoint(tags)
    for extra in (None, *(t for t in influence_tags if t in weight_keys)):
        for key in weight_keys:
            if key in tags or key == extra:
                if elevated and key == extra:
                    return True
                if weights.get(key, 0) > 0 and (key != "default" or default_counts):
                    return True
                break
    return False


def _mod_matches_slot(
    source: str,
    weights_json: str,
    zero_weights_json: str,
    types_json: str,
    slot_spec: tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    *,
    weight_keys_json: str | None = None,
    tag_sets: Sequence[BaseTagSet] = (),
    elevated: bool = False,
) -> bool:
    norm, wk_targets, parent_tags, master_types = slot_spec
    if source == "Bench":
        return _types_match_slot(json.loads(types_json), norm, master_types)

    if weights_json == "{}" and types_json != "[]" and _types_match_slot(
        json.loads(types_json), norm, master_types, allow_subtype=True
    ):
        return True

    weights: dict[str, int] = json.loads(weights_json)
    if tag_sets and weight_keys_json is not None:
        weight_keys = json.loads(weight_keys_json)
        return any(
            mod_rolls_on_base(source, weight_keys, weights, tags, infl, elevated=elevated) for tags, infl in tag_sets
        )

    # No bases to go by (a jewel slot, an unknown slot name): the slot's own tag list stands in.
    for k in wk_targets:
        if weights.get(k, 0) > 0:
            return True
    # Also support exact/prefix influence weightKey like "body_armour_elder" when slot="body_armour"
    for k, v in weights.items():
        if v > 0 and (k == norm or k.startswith(norm + "_")):
            return True

    # PoB ordered tag inheritance (e.g. gloves -> armour -> default; wand -> one_hand_weapon -> weapon -> default):
    # first matching tag decides; an explicit 0 on a child/parent tag blocks broader fallback.
    if parent_tags:
        zeros = set(json.loads(zero_weights_json))
        for pt in parent_tags:
            if pt in zeros:
                break
            if pt == "default" and source not in ("Explicit", "Corrupted"):
                continue
            if weights.get(pt, 0) > 0:
                return True
    return False


def _query_gems(conn: sqlite3.Connection, q_lower: str, limit: int) -> list[dict[str, Any]]:
    if q_lower:
        q_lower = _fold_search_text(normalize_tw_query(q_lower))
    if not q_lower:
        if limit <= 0:
            rows = conn.execute("SELECT data_json FROM gems ORDER BY is_item_skill, name").fetchall()
        else:
            rows = conn.execute("SELECT data_json FROM gems ORDER BY is_item_skill, name LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    pool_lim = _pool_limit_clause(limit)
    fts_expr, tokens = _build_fts_match(q_lower)
    if fts_expr:
        rows = conn.execute(
            f"""
            SELECT g.name_lower, g.display_name_lower, g.zh_lower, g.is_item_skill,
                   g.is_vaal, g.is_transfigured, g.search_text_lower, g.data_json
            FROM gems_fts f
            JOIN gems g ON g.rowid = f.rowid
            WHERE gems_fts MATCH ?
            {pool_lim}
            """,
            (fts_expr,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT name_lower, display_name_lower, zh_lower, is_item_skill,
                   is_vaal, is_transfigured, search_text_lower, data_json
            FROM gems
            WHERE instr(search_text_lower, ?) > 0
            {pool_lim}
            """,
            (q_lower,),
        ).fetchall()

    scored: list[tuple[tuple[int, int, int, int, str], str]] = []
    for name_l, disp_l, zh_l, is_item_skill, is_vaal, is_trans, search_l, data_json in rows:
        zh_l = zh_l or ""
        if tokens and not all(t in search_l for t in tokens):
            continue
        if q_lower == name_l or q_lower == disp_l or (zh_l and q_lower == zh_l):
            tier = 0
        elif name_l.startswith(q_lower) or disp_l.startswith(q_lower) or (zh_l and zh_l.startswith(q_lower)):
            tier = 1
        elif q_lower in name_l or q_lower in disp_l or (zh_l and q_lower in zh_l):
            tier = 2
        else:
            tier = 3
        scored.append(((tier, int(is_item_skill), is_vaal + is_trans, len(name_l), name_l), data_json))

    scored.sort(key=lambda item: item[0])
    return [json.loads(item[1]) for item in _slice_limit(scored, limit)]


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


def _query_uniques(
    conn: sqlite3.Connection,
    q_lower: str,
    slot: str | None,
    ilvl: int | None,
    limit: int,
    *,
    mod_filter: str | None = None,
) -> list[dict[str, Any]]:
    if q_lower:
        q_lower = _fold_search_text(normalize_tw_query(q_lower))
    slot_spec = _normalize_slot_filter(slot) if slot else None
    mod_lower = _fold_search_text(normalize_tw_query(mod_filter or "")).strip()
    mod_tokens = [t for t in mod_lower.split() if t] if mod_lower else []

    pool_lim = _pool_limit_clause(limit)
    combined_q = " ".join(p for p in (q_lower, mod_lower) if p).strip()
    if not combined_q:
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

    q_tokens = [t for t in q_lower.split() if t] if q_lower else []
    scored: list[tuple[tuple[int, int, int, str], dict[str, Any] | str]] = []
    for name_l, zh_l, fb_l, zh_fb_l, slots_l, data_json in rows:
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
        if mod_lower or ilvl is not None or (q_tokens and mod_lower):
            obj = json.loads(data_json)
            if ilvl is not None and obj.get("req_level") is not None and int(obj["req_level"]) > ilvl:
                continue
            if mod_lower:
                mod_tier, matched_lines = _unique_matching_mod_lines(obj, mod_tokens, mod_lower)
                if mod_tier < 0:
                    continue
                obj["matched_mods"] = matched_lines
            if q_tokens and mod_lower:
                hay_q = _fold_search_text(
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
                if not all(t in hay_q for t in q_tokens):
                    continue
            payload = obj

        if q_lower:
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


def _query_bases(
    conn: sqlite3.Connection, q_lower: str, slot: str | None, ilvl: int | None, limit: int
) -> list[dict[str, Any]]:
    slot_spec = _normalize_slot_filter(slot) if slot else None
    where_ilvl = "AND b.req_level <= ?" if ilvl is not None else ""
    pool_lim = _pool_limit_clause(limit)
    params: list[Any] = []

    if not q_lower:
        sql = f"SELECT b.name_lower, b.zh_lower, b.hidden, b.slots_lower, b.data_json FROM bases b WHERE 1=1 {where_ilvl} {pool_lim}"
        if ilvl is not None:
            params.append(ilvl)
        rows = conn.execute(sql, params).fetchall()
    else:
        fts_expr, _ = _build_fts_match(q_lower)
        if fts_expr:
            sql = f"""
            SELECT b.name_lower, b.zh_lower, b.hidden, b.slots_lower, b.data_json
            FROM bases_fts f
            JOIN bases b ON b.rowid = f.rowid
            WHERE bases_fts MATCH ? {where_ilvl}
            {pool_lim}
            """
            params = [fts_expr] + ([ilvl] if ilvl is not None else [])
            rows = conn.execute(sql, params).fetchall()
        else:
            sql = f"""
            SELECT b.name_lower, b.zh_lower, b.hidden, b.slots_lower, b.data_json
            FROM bases b
            WHERE instr(b.search_text_lower, ?) > 0 {where_ilvl}
            {pool_lim}
            """
            params = [q_lower] + ([ilvl] if ilvl is not None else [])
            rows = conn.execute(sql, params).fetchall()

    scored: list[tuple[tuple[int, int, int, str], str]] = []
    for name_l, zh_l, hidden, slots_l, data_json in rows:
        zh_l = zh_l or ""
        if slot_spec:
            norm, wk_targets, _, master_types = slot_spec
            slot_tokens = set(slots_l.split())
            if not (
                norm in slot_tokens
                or any(k in slot_tokens for k in wk_targets)
                or any(mt.lower() in slots_l for mt in master_types)
            ):
                continue

        if not q_lower:
            tier = 0
        elif q_lower == name_l or (zh_l and q_lower == zh_l):
            tier = 0
        elif name_l.startswith(q_lower) or (zh_l and zh_l.startswith(q_lower)):
            tier = 1
        elif q_lower in name_l or (zh_l and q_lower in zh_l):
            tier = 2
        else:
            tier = 3
        scored.append(((tier, hidden, len(name_l), name_l), data_json))

    scored.sort(key=lambda item: item[0])
    return [json.loads(item[1]) for item in _slice_limit(scored, limit)]


def _query_mods(
    conn: sqlite3.Connection,
    q_lower: str,
    slot: str | None,
    ilvl: int | None,
    limit: int,
    *,
    drop_only_sink: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    slot_spec = _normalize_slot_filter(slot) if slot else None
    tag_sets = _slot_tag_sets(conn, slot_spec[3]) if slot_spec else ()
    where_ilvl = "AND m.level <= ?" if ilvl is not None else ""
    pool_lim = _pool_limit_clause(limit)

    if not q_lower:
        sql = f"""
        SELECT m.id, m.source, m.level, m.affix_lower, m.mod_group_lower,
               m.stats_en_lower, m.stats_zh_lower, m.weights_json, m.zero_weights_json, m.weight_keys_json,
               m.types_json, m.data_json
        FROM mods m
        WHERE 1=1 {where_ilvl}
        {pool_lim}
        """
        params: list[Any] = [ilvl] if ilvl is not None else []
        rows = conn.execute(sql, params).fetchall()
        tokens: list[str] = []
    else:
        fts_expr, tokens = _build_fts_match(q_lower)
        if fts_expr:
            sql = f"""
            SELECT m.id, m.source, m.level, m.affix_lower, m.mod_group_lower,
                   m.stats_en_lower, m.stats_zh_lower, m.weights_json, m.zero_weights_json, m.weight_keys_json,
                   m.types_json, m.data_json
            FROM mods_fts f
            JOIN mods m ON m.rowid = f.rowid
            WHERE mods_fts MATCH ? {where_ilvl}
            {pool_lim}
            """
            params = [fts_expr] + ([ilvl] if ilvl is not None else [])
            rows = conn.execute(sql, params).fetchall()
        else:
            sql = f"""
            SELECT m.id, m.source, m.level, m.affix_lower, m.mod_group_lower,
                   m.stats_en_lower, m.stats_zh_lower, m.weights_json, m.zero_weights_json, m.weight_keys_json,
                   m.types_json, m.data_json
            FROM mods m
            WHERE (instr(m.search_en_lower, ?) > 0 OR instr(m.search_zh_lower, ?) > 0) {where_ilvl}
            {pool_lim}
            """
            params = [q_lower, q_lower] + ([ilvl] if ilvl is not None else [])
            rows = conn.execute(sql, params).fetchall()

    scored: list[tuple[tuple[int, int, int, str], str]] = []
    for (
        mod_id,
        source,
        level,
        affix_l,
        grp_l,
        en_l,
        zh_l,
        weights_j,
        zero_j,
        keys_j,
        types_j,
        data_json,
    ) in rows:
        if q_lower:
            combined = f"{mod_id.lower()} {source.lower()} {affix_l} {grp_l} {en_l} {zh_l}"
            if tokens and source == "Vestigial" and not all(t in combined for t in tokens):
                # The index finds a Vestigial mod by its donor's name; this check has to know it too.
                donors = json.loads(data_json).get("vestigialDonors") or ()
                combined += " " + _fold_search_text(" ".join(str(d) for d in donors))
            if tokens and not all(t in combined for t in tokens):
                continue
            if q_lower == mod_id.lower() or q_lower == grp_l or q_lower == affix_l or q_lower in en_l or q_lower in zh_l:
                tier = 0 if (q_lower in en_l or q_lower in zh_l or q_lower == mod_id.lower()) else 1
            else:
                tier = 2
        else:
            tier = 0

        if slot_spec and not _mod_matches_slot(
            source,
            weights_j,
            zero_j,
            types_j,
            slot_spec,
            weight_keys_json=keys_j,
            tag_sets=tag_sets,
            elevated="elevated" in affix_l,  # PoB's own test: "Elevated Shaper's", "of the Elevated Hunt"
        ):
            if (
                drop_only_sink is not None
                and weights_j == "{}"
                and source in ("Explicit", "Delve", "Veiled")
                and len(drop_only_sink) < 8
            ):
                drop_only_sink.append(json.loads(data_json))
            continue

        has_pos = 0 if (weights_j != "{}" or types_j != "[]") else 1
        scored.append(((tier, has_pos, -int(level), mod_id), data_json))

    scored.sort(key=lambda item: item[0])
    return [json.loads(item[1]) for item in _slice_limit(scored, limit)]
