"""Offline PoE1 3.30 Tattoo and Runegraft parser, SQLite table writer, and bilingual query engine."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .lua_parser import _as_seq, _parse_lua_return
from .tw_norm import _fold_search_text, normalize_tw_query
from .zh_dict import _ZhDictionary

_PURE_CJK_LONG_RE = re.compile(r"^[\u4e00-\u9fff]{4,}$")

_RAW_TATTOO_SLOT_ALIASES: dict[str, tuple[tuple[str, str], ...]] = {
    **dict.fromkeys(
        ("small_str", "small_strength", "small strength", "str", "strength", "力量", "小力量", "力量小点", "+10 str", "+10 strength"),
        (("small strength", ""),),
    ),
    **dict.fromkeys(
        ("small_dex", "small_dexterity", "small dexterity", "dex", "dexterity", "敏捷", "小敏捷", "敏捷小点", "+10 dex", "+10 dexterity"),
        (("small dexterity", ""),),
    ),
    **dict.fromkeys(
        ("small_int", "small_intelligence", "small intelligence", "int", "intelligence", "智慧", "智力", "小智慧", "小智力", "智慧小点", "智力小点", "+10 int", "+10 intelligence"),
        (("small intelligence", ""),),
    ),
    **dict.fromkeys(
        ("small_attr", "small", "attribute", "属性", "小点", "属性小点"),
        (("small strength", ""), ("small dexterity", ""), ("small intelligence", ""), ("small attribute", "")),
    ),
    **dict.fromkeys(("small_attribute", "small attribute"), (("small attribute", ""),)),
    **dict.fromkeys(("notable_attr", "+30", "+30_attr", "+30 attr", "属性大点", "+30大点", "+30 大点"), (("notable", "+30 to"),)),
    **dict.fromkeys(("notable_str", "notable str", "+30_str", "+30 str", "+30力量", "+30 力量", "力量大点"), (("notable", "+30 to strength"),)),
    **dict.fromkeys(("notable_dex", "notable dex", "+30_dex", "+30 dex", "+30敏捷", "+30 敏捷", "敏捷大点"), (("notable", "+30 to dexterity"),)),
    **dict.fromkeys(("notable_int", "notable int", "+30_int", "+30 int", "+30智慧", "+30 智慧", "+30智力", "+30 智力", "智慧大点", "智力大点"), (("notable", "+30 to intelligence"),)),
    **dict.fromkeys(("notable", "核心", "大点", "天赋大点"), (("notable", ""),)),
    **dict.fromkeys(("mastery", "专精", "专精节点"), (("mastery", ""),)),
    **dict.fromkeys(("keystone", "基石", "基石天赋"), (("keystone", ""),)),
}
_TATTOO_SLOT_ALIASES: dict[str, tuple[tuple[str, str], ...]] = {
    _fold_search_text(k): v for k, v in _RAW_TATTOO_SLOT_ALIASES.items()
}

_RAW_TATTOO_QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(("法术压制", "法压"), ("法术伤害压制率", "suppress spell damage")),
    **dict.fromkeys(("混抗", "混沌抗"), ("混沌抗性", "chaos resistance")),
    **dict.fromkeys(("电抗", "闪电抗"), ("闪电抗性", "lightning resistance")),
    **dict.fromkeys(("火抗", "火焰抗"), ("火焰抗性", "fire resistance")),
    **dict.fromkeys(("冰抗", "冰霜抗"), ("冰霜抗性", "cold resistance")),
    **dict.fromkeys(("三抗", "元素抗"), ("抗性", "resistance")),
    **dict.fromkeys(("保留", "魔力保留", "保留效能"), ("魔力保留", "保留效能", "reservation")),
    "药剂持续": ("药剂效果的持续时间延长", "flask effect duration"),
    **dict.fromkeys(("药剂生命", "血药回复", "血药", "药水回复", "药剂回复"), ("药剂给予的生命恢复提高", "life recovery from flasks")),
    **dict.fromkeys(("暴击减伤", "防暴", "减暴伤"), ("受到的暴击伤害降低", "reduced extra damage from critical strikes")),
    "暴伤": ("暴击伤害", "受到的暴击伤害降低", "critical strike"),
    **dict.fromkeys(("秒回", "生命秒回"), ("每秒再生", "生命再生", "regenerate", "life regeneration")),
    **dict.fromkeys(("回蓝", "魔力再生"), ("魔力再生率", "魔力再生", "mana regeneration")),
    "吸血": ("生命偷取", "leeched as life"),
    "移速": ("移动速度", "movement speed"),
    "投速": ("投射物速度", "projectile speed"),
    "战吼冷却": ("战吼冷却回复速度", "warcry cooldown recovery"),
    **dict.fromkeys(("攻击格挡", "格挡"), ("攻击伤害格挡几率", "block attack damage")),
    "护甲": ("护甲", "armour"),
    "闪避": ("闪避", "evasion"),
    **dict.fromkeys(("护盾", "能量护盾"), ("能量护盾", "energy shield")),
    "免晕": ("避免被晕眩", "avoid being stunned"),
    "免毒": ("避免中毒", "avoid being poisoned"),
    "免流血": ("避免致流血", "避免流血", "avoid bleeding"),
    "免冰": ("避免被冰缓", "avoid being chilled"),
    "免冻": ("避免被冰冻", "avoid being frozen"),
}
_TATTOO_QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
    _fold_search_text(k): tuple(_fold_search_text(v) for v in vs)
    for k, vs in _RAW_TATTOO_QUERY_SYNONYMS.items()
}

_TARGET_SEARCH_EXTRAS: dict[str, str] = {
    "Small Strength": "str strength small_str 力量 小力量 力量小点",
    "Small Dexterity": "dex dexterity small_dex 敏捷 小敏捷 敏捷小点",
    "Small Intelligence": "int intelligence small_int 智慧 智力 小智慧 小智力 智慧小点",
    "Small Attribute": "attribute small_attr 属性 小属性 属性小点",
    "Mastery": "mastery 专精 专精节点",
    "Notable": "notable notable_attr 核心 大点 天赋大点 属性大点",
    "Keystone": "keystone 基石 基石天赋",
}

_REPLACES_ZH_BY_TYPE: dict[str, str] = {
    "small strength": "+10 力量小点",
    "small dexterity": "+10 敏捷小点",
    "small intelligence": "+10 智慧小点",
    "small attribute": "+10 任意属性小点",
    "mastery": "专精节点",
    "keystone": "基石天赋",
}
_NOTABLE_VAL_ZH: tuple[tuple[str, str], ...] = (
    ("+30 to strength", "+30 力量大点"),
    ("+30 to dexterity", "+30 敏捷大点"),
    ("+30 to intelligence", "+30 智慧大点"),
)


def format_tattoo_replaces(target_type: str, target_value: str = "", *, include_zh: bool = True) -> tuple[str, str]:
    """Return `(replaces_display, replaces_zh)` for a tattoo/runegraft target slot."""
    tt, tv = str(target_type or "").strip() or "-", str(target_value or "").strip()
    tt_l, tv_l = tt.lower(), tv.lower()
    en = f"{tt}: {tv}" if tv else tt
    zh = _REPLACES_ZH_BY_TYPE.get(tt_l, "")
    if tt_l == "notable":
        zh = next((lbl for key, lbl in _NOTABLE_VAL_ZH if key in tv_l), "天赋大点")
    return (f"{en} ({zh})" if (include_zh and zh) else en), zh


def _cjk_bigram_chunks(token: str) -> list[str]:
    """Split a pure-CJK token of length >= 4 into 2-char chunks (with trailing 2-char window for odd length)."""
    if not _PURE_CJK_LONG_RE.match(token):
        return []
    chunks = [token[i : i + 2] for i in range(0, len(token) - 1, 2)]
    if len(token) % 2 == 1:
        chunks.append(token[-2:])
    return chunks


def _token_matches_direct_or_syn(tok: str, text_l: str) -> bool:
    if tok in text_l:
        return True
    syns = _TATTOO_QUERY_SYNONYMS.get(tok)
    return bool(syns and any(s in text_l for s in syns))


def _token_matches_with_cjk_fallback(tok: str, text_l: str) -> bool:
    if _token_matches_direct_or_syn(tok, text_l):
        return True
    chunks = _cjk_bigram_chunks(tok)
    return bool(chunks) and all(c in text_l for c in chunks)


def tattoo_suggestion_candidates(
    conn: sqlite3.Connection,
    kind_norm: str,
    is_cjk: bool,
) -> list[tuple[str, str | None, str]]:
    """Return `(name, zh_name, key_txt)` candidates strictly from active `tattoos` rows."""
    sql = "SELECT name, name_lower, zh_name, zh_lower, search_text_lower FROM tattoos WHERE legacy = 0"
    params: list[Any] = []
    if kind_norm == "runegraft":
        sql += " AND kind = ?"
        params.append("runegraft")
    out: list[tuple[str, str | None, str]] = []
    for nm, nm_l, zh, zh_l, search_l in conn.execute(sql, params).fetchall():
        key_txt = (" ".join(_zh_tattoo_variants(zh_l)) if zh_l else (search_l or nm_l)) if is_cjk else nm_l
        out.append((nm, zh, key_txt))
    return out


def tattoo_zero_result_hint(kind_norm: str, query: str, suggestions: list[dict[str, Any]]) -> str:
    """Build a tattoo/runegraft-scoped zero-hit hint (never pointing to unrelated gem/unique tables)."""
    cmd_hint = "`pob data runegraft`" if kind_norm == "runegraft" else "`pob data tattoo --slot small_dex` or `pob data tattoo`"
    legacy_note = " (add --legacy for retired 3.22 Small Attribute / Honoured tattoos)" if kind_norm == "tattoo" else ""
    if suggestions:
        top = ", ".join(f"{s['name']} ({s['zh_name']})" if s.get("zh_name") else s["name"] for s in suggestions[:3])
        return f"0 exact {kind_norm} matches for '{query}'. Did you mean: {top}? Try re-querying with {cmd_hint}{legacy_note}."
    return f"0 exact {kind_norm} matches for '{query}'. List available entries with {cmd_hint}{legacy_note} — do NOT guess stats from memory."


def _zh_tattoo_variants(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    variants = [text]
    for src, dst in (("文身", "纹身"), ("纹身", "文身"), ("符文之结", "符文嵌板"), ("符文嵌板", "符文之结"), ("药水", "药剂")):
        for cur in list(variants):
            if src in cur:
                variants.append(cur.replace(src, dst))
    return tuple(dict.fromkeys(variants))


def _parse_tattoos(poe1_dir: Path, zh: _ZhDictionary) -> list[dict[str, Any]]:
    """Parse PoE1/Data/TattooPassives.lua into structured tattoo and runegraft records."""
    path = poe1_dir / "Data" / "TattooPassives.lua"
    if not path.is_file():
        return []
    raw = _parse_lua_return(path)
    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes, dict):
        return []

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_key, entry in nodes.items():
        if not isinstance(entry, dict):
            continue
        dn = str(entry.get("dn") or "").strip()
        if not dn:
            continue
        rec_id = str(entry.get("id") or raw_key).strip() or dn
        if rec_id in seen_ids:
            continue
        seen_ids.add(rec_id)

        override_type = str(entry.get("overrideType") or "").strip()
        kind = "runegraft" if override_type == "AlternateMastery" else "tattoo"
        target_type = str(entry.get("targetType") or "").strip()
        target_value = str(entry.get("targetValue") or "").strip()
        legacy = bool(entry.get("legacy"))

        sd_lines = [str(s).strip() for s in _as_seq(entry.get("sd")) if isinstance(s, str) and str(s).strip()]
        limit_lines = [s for s in sd_lines if s.lower().startswith("limited to ")]
        effect_lines = [s for s in sd_lines if not s.lower().startswith("limited to ")]
        stats = effect_lines if effect_lines else sd_lines
        stats_zh = [zh.translate_stat_line(s) for s in stats] if zh.available else []
        limit_text = limit_lines[0] if limit_lines else None
        limit_text_zh = zh.translate_stat_line(limit_text) if (zh.available and limit_text) else None

        zh_name = (zh.passives.get(dn) or zh.bases.get(dn)) if zh.available else None
        min_conn = int(entry.get("MinimumConnected") or 0)
        max_conn = int(entry.get("MaximumConnected") if entry.get("MaximumConnected") is not None else 100)
        rep_disp, rep_zh = format_tattoo_replaces(target_type, target_value, include_zh=True)

        records.append(
            {
                "id": rec_id,
                "name": dn,
                "zh_name": zh_name,
                "kind": kind,
                "override_type": override_type,
                "target_type": target_type,
                "target_value": target_value,
                "replaces_zh": rep_zh,
                "replaces_display": rep_disp,
                "legacy": legacy,
                "stats": stats,
                "stats_zh": stats_zh,
                "limit_text": limit_text,
                "limit_text_zh": limit_text_zh,
                "full_stats": sd_lines,
                "min_connected": min_conn,
                "max_connected": max_conn,
            }
        )

    records.sort(key=lambda r: (0 if r["kind"] == "runegraft" else 1, int(r["legacy"]), r["name"].lower(), r["id"]))
    return records


def _insert_tattoos_table(conn: sqlite3.Connection, tattoos: list[dict[str, Any]]) -> None:
    rows = []
    for t in tattoos:
        zh_name = t.get("zh_name")
        zh_variants = " ".join(_zh_tattoo_variants(zh_name or ""))
        kind_kw = "runegraft 符文嵌板 符文之结 符文嫁接" if t["kind"] == "runegraft" else "tattoo 纹身 文身"
        target_extra = _TARGET_SEARCH_EXTRAS.get(t["target_type"], "")
        stats_blob = _fold_search_text(
            " ".join(p for p in (" ".join(t["stats"]), " ".join(x for x in t["stats_zh"] if x), t.get("limit_text") or "", t.get("limit_text_zh") or "") if p)
        )
        syn_extras = [k for k, tgts in _TATTOO_QUERY_SYNONYMS.items() if any(tgt in stats_blob for tgt in tgts)]
        s_txt = _fold_search_text(
            " ".join(
                p
                for p in (
                    t["id"], t["name"], zh_variants, kind_kw, t["override_type"], t["target_type"],
                    t["target_value"], target_extra, " ".join(t["stats"]), " ".join(x for x in t["stats_zh"] if x),
                    t.get("limit_text") or "", t.get("limit_text_zh") or "", " ".join(syn_extras),
                )
                if p
            )
        )
        rows.append(
            (
                t["id"], t["name"], _fold_search_text(t["name"]), zh_name,
                _fold_search_text(zh_name) if zh_name else None, t["kind"], t["override_type"],
                t["target_type"], _fold_search_text(t["target_type"]), t["target_value"],
                int(bool(t["legacy"])), s_txt, json.dumps(t, ensure_ascii=False, separators=(",", ":")),
            )
        )
    conn.executemany("INSERT INTO tattoos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO tattoos_fts(tattoos_fts) VALUES('rebuild')")


def _target_matches_slot(target_type_l: str, target_val_l: str, slot_filter: str) -> bool:
    raw = " ".join(normalize_tw_query(slot_filter).strip().lower().split())
    folded = _fold_search_text(raw)
    for key in (raw, raw.replace("-", "_"), raw.replace("_", " "), raw.replace("-", " "), folded, folded.replace("-", "_"), folded.replace("_", " "), folded.replace("-", " ")):
        if key in _TATTOO_SLOT_ALIASES:
            return any(target_type_l == req_t and (not req_v or req_v in target_val_l) for req_t, req_v in _TATTOO_SLOT_ALIASES[key])
    return bool(folded) and (folded in target_type_l or (bool(target_val_l) and folded in target_val_l))


def _record_matches_mod(rec: dict[str, Any], mod_filter: str) -> bool:
    mf_tw = normalize_tw_query(mod_filter).strip()
    if not mf_tw:
        return True
    haystack = _fold_search_text(" ".join([*rec.get("stats", ()), *(x for x in rec.get("stats_zh", ()) if x), *rec.get("full_stats", ())]))
    for cand in _zh_tattoo_variants(mf_tw):
        folded_cand = _fold_search_text(cand)
        if not folded_cand:
            continue
        if _token_matches_direct_or_syn(folded_cand, haystack):
            return True
        toks = [t for t in folded_cand.split() if t]
        if toks and all(_token_matches_with_cjk_fallback(tok, haystack) for tok in toks):
            return True
    return False


def query_tattoo_data(
    conn: sqlite3.Connection,
    query: str = "",
    *,
    kind: str = "all",
    slot_filter: str | None = None,
    mod_filter: str | None = None,
    include_legacy: bool = False,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Query local `tattoos` SQLite table with bilingual name/stat/target matching."""
    kind_norm = str(kind or "all").strip().lower()
    if kind_norm not in ("all", "tattoo", "runegraft"):
        raise ValueError(f"Unsupported tattoo kind {kind!r}; expected one of: all, tattoo, runegraft.")

    q_raw = str(query or "").strip()
    q_tw = normalize_tw_query(q_raw) if q_raw else ""
    q_cands = [_fold_search_text(v) for v in _zh_tattoo_variants(q_tw) if _fold_search_text(v)]

    sql = (
        "SELECT id, name_lower, zh_lower, kind, target_type_lower, target_value, legacy, search_text_lower, data_json "
        "FROM tattoos"
    )
    conds: list[str] = []
    params: list[Any] = []
    if kind_norm in ("tattoo", "runegraft"):
        conds.append("kind = ?")
        params.append(kind_norm)
    if not include_legacy:
        conds.append("legacy = 0")
    if conds:
        sql += " WHERE " + " AND ".join(conds)

    rows = conn.execute(sql, params).fetchall()
    slot_clean = str(slot_filter or "").strip() or None
    mod_clean = str(mod_filter or "").strip() or None

    scored: list[tuple[tuple[int, int, int, str, str], dict[str, Any]]] = []
    for rec_id, name_l, zh_l, rec_kind, target_l, target_val, legacy_i, search_l, dj in rows:
        target_val_l = _fold_search_text(target_val) if target_val else ""
        if slot_clean and not _target_matches_slot(target_l, target_val_l, slot_clean):
            continue

        zh_s = zh_l or ""
        zh_cands = [_fold_search_text(v) for v in _zh_tattoo_variants(zh_s) if v]
        zh_nod_cands = [zc.replace("的", "") for zc in zh_cands if "的" in zc]
        id_l = _fold_search_text(rec_id)

        tier = 2
        if q_cands:
            best = 99
            for qc in q_cands:
                toks = [t for t in qc.split() if t]
                if not toks:
                    continue
                qc_nod = qc.replace("的", "")
                if qc == name_l or qc == id_l or qc in zh_cands:
                    best = min(best, 0)
                elif name_l.startswith(qc) or any(zc.startswith(qc) for zc in zh_cands):
                    best = min(best, 1)
                elif qc in name_l or any(qc in zc for zc in zh_cands) or (bool(qc_nod) and any(qc_nod in znd for znd in zh_nod_cands)):
                    best = min(best, 2)
                elif _token_matches_direct_or_syn(qc, search_l) or all(_token_matches_direct_or_syn(tok, search_l) for tok in toks):
                    best = min(best, 3)
                elif all(_token_matches_with_cjk_fallback(tok, search_l) for tok in toks):
                    best = min(best, 4)
            if best >= 99:
                continue
            tier = best

        rec = json.loads(dj)
        if mod_clean and not _record_matches_mod(rec, mod_clean):
            continue

        rec["replaces_display"], rec["replaces_zh"] = format_tattoo_replaces(
            str(rec.get("target_type") or ""), str(rec.get("target_value") or ""), include_zh=True
        )
        kind_rank = 0 if rec_kind == "runegraft" else 1
        scored.append(((tier, int(legacy_i), kind_rank, name_l, id_l), rec))

    scored.sort(key=lambda item: item[0])
    out = [rec for _, rec in scored]
    return out[: int(limit)] if int(limit) > 0 else out
