"""Zero-empty-result suggestions, live 3.30 fact cards, and pre-turn entity grounding."""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..guardrails import normalize_guardrail_text, normalize_zh_compact, scan_text_traps
from .constants import (
    _CJK_RE,
    _REMOVED_LEGACY_GEM_NAMES_LOWER,
    MOD_TAG_PREFIX_RE,
)
from .paths import _sqlite_ro_uri
from .queries_core import _query_gems
from .slang import (
    _GROUND_EN_STOPWORDS,
    _GROUND_ZH_STOPWORDS,
    _SLANG_SORTED_KEYS,
    PLAYER_SLANG_MAP,
)
from .tw_norm import _fold_search_text, fold_han, normalize_tw_query

_GROUND_ENTITY_INDEX_CACHE: dict[str, dict[str, Any]] = {}


def _suggest_similar(
    conn: sqlite3.Connection,
    kind_norm: str,
    q_lower: str,
    *,
    limit: int = 5,
    zh_available: bool = True,
) -> list[dict[str, Any]]:
    """Return up to `limit` fuzzy/token/difflib suggestions ONLY when primary query yields 0 hits."""
    if not q_lower or limit <= 0:
        return []

    is_cjk = bool(_CJK_RE.search(q_lower))
    q_tokens = [t for t in re.split(r"[\s_\-/]+", q_lower) if len(t) >= 2]

    candidates: list[tuple[str, str | None, str]] = []
    if kind_norm == "gem":
        rows = conn.execute("SELECT name, name_lower, zh_name, zh_lower FROM gems").fetchall()
        for nm, nm_l, zh, zh_l in rows:
            candidates.append((nm, zh, (zh_l or nm_l) if is_cjk else nm_l))
    elif kind_norm == "unique":
        rows = conn.execute("SELECT name, name_lower, zh_name, zh_lower FROM uniques").fetchall()
        for nm, nm_l, zh, zh_l in rows:
            candidates.append((nm, zh, (zh_l or nm_l) if is_cjk else nm_l))
    elif kind_norm == "base":
        rows = conn.execute("SELECT name, name_lower, zh_name, zh_lower FROM bases").fetchall()
        for nm, nm_l, zh, zh_l in rows:
            candidates.append((nm, zh, (zh_l or nm_l) if is_cjk else nm_l))
    elif kind_norm == "mod":
        if is_cjk:
            chars = list(dict.fromkeys(ch for ch in q_lower if _CJK_RE.match(ch)))[:4]
            if chars:
                where = " OR ".join("instr(stats_zh_lower, ?) > 0" for _ in chars)
                mrows = conn.execute(
                    f"SELECT id, stats_en_lower, stats_zh_lower, data_json FROM mods WHERE {where} LIMIT 250",
                    chars,
                ).fetchall()
            else:
                mrows = []
        else:
            toks = [t for t in q_tokens if len(t) >= 3][:3] or [q_lower[:3]]
            where = " OR ".join("instr(stats_en_lower, ?) > 0" for _ in toks)
            mrows = conn.execute(
                f"SELECT id, stats_en_lower, stats_zh_lower, data_json FROM mods WHERE {where} LIMIT 250",
                toks,
            ).fetchall()
        seen_mod_txt: set[str] = set()
        for _, en_l, zh_l, dj in mrows:
            obj = json.loads(dj)
            en_s = (obj.get("stats") or [""])[0]
            zh_s = next((s for s in (obj.get("stats_zh") or []) if s), None)
            if en_s and en_s not in seen_mod_txt:
                seen_mod_txt.add(en_s)
                candidates.append((en_s, zh_s, (zh_l or en_l) if is_cjk else en_l))
    else:  # translate
        for tbl in ("gems", "uniques", "bases"):
            rows = conn.execute(f"SELECT name, name_lower, zh_name, zh_lower FROM {tbl}").fetchall()
            for nm, nm_l, zh, zh_l in rows:
                candidates.append((nm, zh, (zh_l or nm_l) if is_cjk else nm_l))
        if zh_available and not candidates:
            rows = conn.execute("SELECT en, en_lower, zh, zh_lower FROM translations WHERE cat_rank <= 2 LIMIT 2000").fetchall()
            for en, en_l, zh, zh_l in rows:
                candidates.append((en, zh, zh_l if is_cjk else en_l))

    if not candidates:
        return []

    scored: list[tuple[float, str, str | None]] = []
    seen_en: set[str] = set()
    q_chars = set(ch for ch in q_lower if _CJK_RE.match(ch)) if is_cjk else set()

    for en, zh, key_txt in candidates:
        if not key_txt or en in seen_en:
            continue
        score = 0.0
        if is_cjk and q_chars:
            overlap = sum(1 for ch in q_chars if ch in key_txt)
            if overlap == 0:
                continue
            score = (overlap / len(q_chars)) * 0.7 + difflib.SequenceMatcher(None, q_lower, key_txt).ratio() * 0.3
        else:
            tok_hits = sum(1 for t in q_tokens if t in key_txt) if q_tokens else 0
            ratio = difflib.SequenceMatcher(None, q_lower, key_txt).ratio()
            if tok_hits == 0 and ratio < 0.45:
                continue
            score = ratio * 0.75 + (tok_hits / max(1, len(q_tokens))) * 0.25
        if score >= 0.40:
            seen_en.add(en)
            scored.append((score, en, zh))

    scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    return [{"name": en, "zh_name": zh, "score": round(sc, 3)} for sc, en, zh in scored[:limit]]


def _build_zero_result_hint(kind_norm: str, query: str, suggestions: list[dict[str, Any]]) -> str:
    if suggestions:
        top_names = ", ".join(
            f"{s['name']} ({s['zh_name']})" if s.get("zh_name") else s["name"]
            for s in suggestions[:3]
        )
        return (
            f"0 exact {kind_norm} matches for '{query}'. "
            f"Did you mean: {top_names}? Try re-querying with one of those names or `pob data translate \"{query}\"`."
        )
    return (
        f"0 exact {kind_norm} matches for '{query}'. "
        f"Verify the exact PoE1 3.30 name via `pob data translate \"{query}\"` or `pob poedb \"{query}\"` — do NOT guess stats from memory."
    )


def _format_gem_fact_card(gem_obj: dict[str, Any], *, matched_by: str | None = None) -> dict[str, Any]:
    lvls = gem_obj.get("levels") or {}
    lv20 = lvls.get("20") or lvls.get("1") or (next(iter(lvls.values())) if lvls else {})
    q_stats = gem_obj.get("quality_stats") or []
    card: dict[str, Any] = {
        "kind": "gem",
        "name": gem_obj.get("display_name") or gem_obj.get("name"),
        "zhName": gem_obj.get("zh_name"),
        "isSupport": bool(gem_obj.get("is_support")),
        "color": gem_obj.get("color"),
        "tags": gem_obj.get("tag_string"),
        "castTime": gem_obj.get("cast_time"),
        "cooldown": gem_obj.get("cooldown"),
        "storedUses": gem_obj.get("stored_uses"),
        "isExceptional": bool(gem_obj.get("is_exceptional")),
        "legacyRemoved328": bool(gem_obj.get("legacy_removed_328")),
        "naturalMaxLevel": gem_obj.get("natural_max_level"),
        "corruptedMaxLevel": gem_obj.get("corrupted_max_level"),
        "critChance": gem_obj.get("crit_chance") or lv20.get("critChance"),
        "damageEffectiveness": gem_obj.get("damage_effectiveness") or lv20.get("damageEffectiveness"),
        "manaMultiplier": gem_obj.get("mana_multiplier"),
        "lv20Cost": lv20.get("cost"),
        "lv20Stats": dict(list((lv20.get("stats") or {}).items())[:6]),
        "quality20": [
            {
                "stat": q.get("stat"),
                "perQuality": q.get("per_quality"),
                "at20": q.get("at_20_quality"),
                "at40": round(float(q.get("per_quality") or 0.0) * 40, 4),
            }
            for q in q_stats[:3]
        ],
    }
    variants = gem_obj.get("variants") or []
    if variants:
        card["variants"] = [
            f"{v.get('name')} ({v['zh_name']})" if v.get("zh_name") else str(v.get("name"))
            for v in variants[:4]
        ]
    if matched_by:
        card["matchedBy"] = matched_by
    return card


def _format_unique_fact_card(u_obj: dict[str, Any], *, matched_by: str | None = None) -> dict[str, Any]:
    implicits = [MOD_TAG_PREFIX_RE.sub("", s).strip() for s in (u_obj.get("implicits") or [])[:3]]
    explicits = [MOD_TAG_PREFIX_RE.sub("", s).strip() for s in (u_obj.get("explicits") or [])[:8]]
    card: dict[str, Any] = {
        "kind": "unique",
        "name": u_obj.get("name"),
        "zhName": u_obj.get("zh_name"),
        "baseType": u_obj.get("base_type"),
        "baseTypeZh": u_obj.get("base_type_zh"),
        "itemClass": u_obj.get("item_class"),
        "reqLevel": u_obj.get("req_level"),
        "implicits": [s for s in implicits if s],
        "explicits": [s for s in explicits if s],
    }
    if matched_by:
        card["matchedBy"] = matched_by
    return card


def _get_ground_entity_index(conn: sqlite3.Connection, db_key: str) -> dict[str, Any]:
    cached = _GROUND_ENTITY_INDEX_CACHE.get(db_key)
    if cached is not None:
        return cached

    zh_entries: list[tuple[str, str, int, str]] = []
    en_entries: list[tuple[str, str, int, str]] = []

    for kind, tbl in (("gem", "gems"), ("unique", "uniques")):
        rows = conn.execute(f"SELECT rowid, name, name_lower, zh_lower FROM {tbl}").fetchall()
        for rid, name, name_l, zh_l in rows:
            if kind == "gem" and name_l in _REMOVED_LEGACY_GEM_NAMES_LOWER:
                continue
            if name_l and len(name_l) >= 3 and name_l not in _GROUND_EN_STOPWORDS:
                en_entries.append((name_l, kind, int(rid), name))
            if zh_l:
                zh_clean = re.sub(r"\s*(?:\(辅\)|（辅）|\(强辅\)|（强辅）|辅助)$", "", zh_l).strip()
                zh_compact = re.sub(r"[\s·・.．:：()（）]", "", zh_clean)
                if len(zh_compact) >= 2 and zh_compact not in _GROUND_ZH_STOPWORDS:
                    zh_entries.append((zh_compact, kind, int(rid), name))

    zh_entries.sort(key=lambda x: (-len(x[0]), 0 if x[1] == "gem" else 1, x[3]))
    en_entries.sort(key=lambda x: (-len(x[0]), 0 if x[1] == "gem" else 1, x[3]))

    idx = {"zh": zh_entries, "en": en_entries}
    _GROUND_ENTITY_INDEX_CACHE[db_key] = idx
    return idx


def lookup_fact_cards_for_build(
    gem_names: list[str],
    unique_names: list[str],
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Fetch compact live 3.30 gem & unique fact cards from SQLite for `pob diagnose` (<5ms)."""
    import pob_cli.game_data as _gd

    db_path = _gd.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        gem_cards: list[dict[str, Any]] = []
        seen_gems: set[str] = set()
        for raw_g in gem_names:
            g_norm = normalize_tw_query(str(raw_g or "").strip())
            g_clean = re.sub(r"\s+support$", "", g_norm, flags=re.IGNORECASE)
            if not g_clean:
                continue
            g_low = _fold_search_text(g_clean)
            row = conn.execute(
                "SELECT data_json FROM gems WHERE name_lower = ? OR display_name_lower = ? OR zh_lower = ? LIMIT 1",
                (g_low, g_low, g_low),
            ).fetchone()
            if row is None:
                hits = _query_gems(conn, g_low, 1)
                if hits:
                    obj = hits[0]
                else:
                    continue
            else:
                obj = json.loads(row[0])
            cname = str(obj.get("name") or "")
            if cname and cname not in seen_gems:
                seen_gems.add(cname)
                gem_cards.append(_format_gem_fact_card(obj))

        unique_cards: list[dict[str, Any]] = []
        seen_uniques: set[str] = set()
        for raw_u in unique_names:
            u_clean = normalize_tw_query(str(raw_u or "").strip()).strip()
            if not u_clean:
                continue
            u_low = _fold_search_text(u_clean)
            row = conn.execute(
                "SELECT data_json FROM uniques WHERE name_lower = ? OR zh_lower = ? LIMIT 1",
                (u_low, u_low),
            ).fetchone()
            if row is None:
                continue
            obj = json.loads(row[0])
            uname = str(obj.get("name") or "")
            if uname and uname not in seen_uniques:
                seen_uniques.add(uname)
                unique_cards.append(_format_unique_fact_card(obj))

        return {
            "pobVersion": meta.get("pob_version", "unknown"),
            "dataVersion": meta.get("data_version", "unknown"),
            "gems": gem_cards,
            "uniques": unique_cards,
        }
    finally:
        conn.close()


def _format_ground_context_block(
    *,
    pob_ver: str,
    data_ver: str,
    gamedata_available: bool,
    warnings: list[dict[str, Any]],
    resolved_slang: list[dict[str, str]],
    entities: list[dict[str, Any]],
) -> str:
    ctx_lines = [f"[Env: PoE1 3.30 | PoB {pob_ver} | Data {data_ver}]"]
    if not gamedata_available:
        ctx_lines.append(
            "- [INFO] Local PoE1 game data not bootstrapped yet; run `pob bootstrap` to attach live gem/unique fact cards."
        )
    for w in warnings:
        ctx_lines.append(f"- {w['tag']} `{w['trigger']}`: {w['message']}")
    if resolved_slang:
        slang_bits = ", ".join(f"{s['slang']}={s['canonical']}" for s in resolved_slang[:6])
        ctx_lines.append(f"- Slang: {slang_bits}")
    for ent in entities:
        zh_p = f" ({ent['zhName']})" if ent.get("zhName") else ""
        if ent["kind"] == "gem":
            bits = []
            if ent.get("isSupport"):
                bits.append("Support")
            if ent.get("castTime") is not None:
                bits.append(f"Cast={ent['castTime']}s")
            if ent.get("tags"):
                bits.append(f"Tags={ent['tags']}")
            st_items = list((ent.get("lv20Stats") or {}).items())[:4]
            if st_items:
                bits.append("Lv20: " + ", ".join(f"{k}={v}" for k, v in st_items))
            ctx_lines.append(f"- [Gem] {ent['name']}{zh_p}: " + " | ".join(bits))
        else:
            mods_preview = "; ".join((ent.get("explicits") or [])[:4])
            ctx_lines.append(f"- [Unique] {ent['name']}{zh_p} ({ent.get('baseType') or '-'}): {mods_preview}")
    return "\n".join(ctx_lines)


def ground_text_entities(
    text: str,
    *,
    limit: int = 8,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Scan arbitrary CN/EN player prompt text (<15ms warm) against live 3.30 SQLite + guardrails + patch traps."""
    import pob_cli.game_data as _gd

    t0 = time.perf_counter()
    raw = str(text or "").strip()
    raw_tw = normalize_tw_query(raw) if raw else ""
    folded_tw = fold_han(raw_tw)  # slang keys and the index are Simplified
    limit = max(0, int(limit))

    traps = scan_text_traps(raw_tw)

    resolved_slang: list[dict[str, str]] = []
    if raw_tw and limit > 0:
        for slang in _SLANG_SORTED_KEYS:
            if slang in folded_tw:
                canon_en, kind_hint = PLAYER_SLANG_MAP[slang]
                resolved_slang.append({"slang": slang, "canonical": canon_en, "kind": kind_hint})

    entities: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    pob_ver = "unbootstrapped"
    data_ver = "none"
    gamedata_available = False

    try:
        db_path: Path | None = _gd.ensure_gamedata_index(pob_dir=pob_dir, runtime_dir=runtime_dir)
    except FileNotFoundError:
        db_path = None

    if db_path is not None:
        gamedata_available = True
        conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            pob_ver = meta.get("pob_version", "unknown")
            data_ver = meta.get("data_version", "unknown")

            if raw and limit > 0:
                for slang_item in resolved_slang:
                    if len(entities) >= limit:
                        break
                    slang = slang_item["slang"]
                    canon_en = slang_item["canonical"]
                    kind_hint = slang_item["kind"]
                    c_low = _fold_search_text(canon_en)
                    if kind_hint == "unique":
                        row = conn.execute(
                            "SELECT data_json FROM uniques WHERE name_lower = ? LIMIT 1", (c_low,)
                        ).fetchone()
                        if row:
                            obj = json.loads(row[0])
                            ekey = ("unique", str(obj.get("name")))
                            if ekey not in seen_keys:
                                seen_keys.add(ekey)
                                entities.append(_format_unique_fact_card(obj, matched_by=f"slang:{slang}"))
                    elif kind_hint == "gem":
                        row = conn.execute(
                            "SELECT data_json FROM gems WHERE name_lower = ? OR display_name_lower = ? LIMIT 1",
                            (c_low, c_low),
                        ).fetchone()
                        if row:
                            obj = json.loads(row[0])
                            ekey = ("gem", str(obj.get("name")))
                            if ekey not in seen_keys:
                                seen_keys.add(ekey)
                                entities.append(_format_gem_fact_card(obj, matched_by=f"slang:{slang}"))

                if len(entities) < limit:
                    idx = _get_ground_entity_index(conn, f"{db_path}:{pob_ver}:{data_ver}")
                    has_cjk = bool(_CJK_RE.search(raw_tw))
                    text_zh_compact = normalize_zh_compact(folded_tw) if has_cjk else ""
                    text_en_norm = normalize_guardrail_text(raw_tw)
                    has_ascii = bool(re.search(r"[a-z]{3,}", text_en_norm))

                    matched_refs: list[tuple[str, int, str]] = []
                    if has_cjk and text_zh_compact:
                        covered_zh: list[str] = []
                        for zh_c, kind, rid, name in idx["zh"]:
                            if len(entities) + len(matched_refs) >= limit:
                                break
                            if zh_c in text_zh_compact:
                                if any(zh_c in prev for prev in covered_zh):
                                    continue
                                ekey = (kind, name)
                                if ekey not in seen_keys:
                                    seen_keys.add(ekey)
                                    covered_zh.append(zh_c)
                                    matched_refs.append((kind, rid, f"zh:{zh_c}"))

                    if has_ascii and len(entities) + len(matched_refs) < limit:
                        for name_l, kind, rid, name in idx["en"]:
                            if len(entities) + len(matched_refs) >= limit:
                                break
                            if name_l not in text_en_norm:
                                continue
                            if not re.search(rf"(?<![a-z0-9_'-]){re.escape(name_l)}(?![a-z0-9_'-])", text_en_norm):
                                continue
                            ekey = (kind, name)
                            if ekey not in seen_keys:
                                seen_keys.add(ekey)
                                matched_refs.append((kind, rid, f"en:{name_l}"))

                    for kind, rid, mby in matched_refs:
                        tbl = "gems" if kind == "gem" else "uniques"
                        row = conn.execute(f"SELECT data_json FROM {tbl} WHERE rowid = ?", (rid,)).fetchone()
                        if not row:
                            continue
                        obj = json.loads(row[0])
                        if kind == "gem":
                            entities.append(_format_gem_fact_card(obj, matched_by=mby))
                        else:
                            entities.append(_format_unique_fact_card(obj, matched_by=mby))
        finally:
            conn.close()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    env = {"game": "poe1", "patch": "3.30", "pobVersion": pob_ver, "dataVersion": data_ver}
    context_block = _format_ground_context_block(
        pob_ver=pob_ver,
        data_ver=data_ver,
        gamedata_available=gamedata_available,
        warnings=traps["warnings"],
        resolved_slang=resolved_slang,
        entities=entities,
    )
    return {
        "env": env,
        "query": raw,
        "gamedataAvailable": gamedata_available,
        "entities": entities,
        "resolvedSlang": resolved_slang,
        "poe2Traps": traps["poe2Traps"],
        "patchTraps": traps["patchTraps"],
        "falseFriends": traps["falseFriends"],
        "warnings": traps["warnings"],
        "hasAlerts": bool(traps["warnings"]),
        "contextBlock": context_block,
        "elapsedMs": elapsed_ms,
    }
