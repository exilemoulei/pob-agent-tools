"""Offline PoE1 3.30 Tattoo and Runegraft parser, SQLite table writer, and bilingual query engine."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .lua_parser import _as_seq, _parse_lua_return
from .tw_norm import _fold_search_text, normalize_tw_query
from .zh_dict import _ZhDictionary

_TATTOO_SLOT_ALIASES: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(("str", "strength", "力量"), ("small strength", "small attribute", "+30 to strength")),
    **dict.fromkeys(("small_strength", "small strength", "小力量"), ("small strength",)),
    **dict.fromkeys(("dex", "dexterity", "敏捷"), ("small dexterity", "small attribute", "+30 to dexterity")),
    **dict.fromkeys(("small_dexterity", "small dexterity", "小敏捷"), ("small dexterity",)),
    **dict.fromkeys(("int", "intelligence", "智慧", "智力"), ("small intelligence", "small attribute", "+30 to intelligence")),
    **dict.fromkeys(("small_intelligence", "small intelligence", "小智慧"), ("small intelligence",)),
    **dict.fromkeys(("attribute", "属性"), ("small attribute", "small strength", "small dexterity", "small intelligence")),
    **dict.fromkeys(("small_attribute", "small attribute"), ("small attribute",)),
    **dict.fromkeys(("mastery", "专精"), ("mastery",)),
    **dict.fromkeys(("notable", "核心", "大点", "天赋大点"), ("notable",)),
    **dict.fromkeys(("keystone", "基石"), ("keystone",)),
}

_TARGET_SEARCH_EXTRAS: dict[str, str] = {
    "Small Strength": "str strength 力量 小力量",
    "Small Dexterity": "dex dexterity 敏捷 小敏捷",
    "Small Intelligence": "int intelligence 智慧 智力 小智慧",
    "Small Attribute": "attribute 属性 小属性",
    "Mastery": "mastery 专精",
    "Notable": "notable 核心 大点 天赋大点",
    "Keystone": "keystone 基石",
}


def _zh_tattoo_variants(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    variants = [text]
    if "文身" in text:
        variants.append(text.replace("文身", "纹身"))
    if "纹身" in text:
        variants.append(text.replace("纹身", "文身"))
    if "符文之结" in text:
        variants.append(text.replace("符文之结", "符文嵌板"))
    if "符文嵌板" in text:
        variants.append(text.replace("符文嵌板", "符文之结"))
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

        records.append(
            {
                "id": rec_id,
                "name": dn,
                "zh_name": zh_name,
                "kind": kind,
                "override_type": override_type,
                "target_type": target_type,
                "target_value": target_value,
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
        kind_kw = "runegraft 符文嵌板 符文之结" if t["kind"] == "runegraft" else "tattoo 纹身 文身"
        target_extra = _TARGET_SEARCH_EXTRAS.get(t["target_type"], "")
        s_txt = _fold_search_text(
            " ".join(
                p
                for p in (
                    t["id"],
                    t["name"],
                    zh_variants,
                    kind_kw,
                    t["override_type"],
                    t["target_type"],
                    t["target_value"],
                    target_extra,
                    " ".join(t["stats"]),
                    " ".join(x for x in t["stats_zh"] if x),
                    t.get("limit_text") or "",
                    t.get("limit_text_zh") or "",
                )
                if p
            )
        )
        rows.append(
            (
                t["id"],
                t["name"],
                _fold_search_text(t["name"]),
                zh_name,
                _fold_search_text(zh_name) if zh_name else None,
                t["kind"],
                t["override_type"],
                t["target_type"],
                _fold_search_text(t["target_type"]),
                t["target_value"],
                int(bool(t["legacy"])),
                s_txt,
                json.dumps(t, ensure_ascii=False, separators=(",", ":")),
            )
        )
    conn.executemany("INSERT INTO tattoos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO tattoos_fts(tattoos_fts) VALUES('rebuild')")


def _target_matches_slot(target_type_l: str, target_val_l: str, slot_filter: str) -> bool:
    raw = normalize_tw_query(slot_filter).strip().lower().replace("-", "_")
    folded = _fold_search_text(slot_filter)
    for key in (raw, raw.replace("_", " "), folded):
        if key in _TATTOO_SLOT_ALIASES:
            targets = _TATTOO_SLOT_ALIASES[key]
            return any(t in target_type_l or (target_val_l and t in target_val_l) for t in targets)
    return bool(folded) and (folded in target_type_l or (bool(target_val_l) and folded in target_val_l))


def _record_matches_mod(rec: dict[str, Any], mod_filter: str) -> bool:
    mf_tw = normalize_tw_query(mod_filter).strip()
    if not mf_tw:
        return True
    haystack = _fold_search_text(
        " ".join(
            [
                *rec.get("stats", ()),
                *(x for x in rec.get("stats_zh", ()) if x),
                *rec.get("full_stats", ()),
            ]
        )
    )
    for cand in _zh_tattoo_variants(mf_tw):
        toks = [t for t in _fold_search_text(cand).split() if t]
        if toks and all(tok in haystack for tok in toks):
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
        id_l = _fold_search_text(rec_id)

        tier = 2
        if q_cands:
            best = 99
            for qc in q_cands:
                toks = [t for t in qc.split() if t]
                if not toks:
                    continue
                if qc == name_l or qc == id_l or qc in zh_cands:
                    best = min(best, 0)
                elif name_l.startswith(qc) or any(zc.startswith(qc) for zc in zh_cands):
                    best = min(best, 1)
                elif qc in name_l or any(qc in zc for zc in zh_cands):
                    best = min(best, 2)
                elif all(tok in search_l for tok in toks):
                    best = min(best, 3)
            if best >= 99:
                continue
            tier = best

        rec = json.loads(dj)
        if mod_clean and not _record_matches_mod(rec, mod_clean):
            continue

        kind_rank = 0 if rec_kind == "runegraft" else 1
        scored.append(((tier, int(legacy_i), kind_rank, name_l, id_l), rec))

    scored.sort(key=lambda item: item[0])
    out = [rec for _, rec in scored]
    return out[: int(limit)] if int(limit) > 0 else out


_VESTIGIAL_CAPABLE_CLASSES: frozenset[str] = frozenset(
    {"Body Armour", "Helmet", "Gloves", "Boots", "Shield", "Quiver"}
)


def _augment_unique_vestigial_md(rendered: str, res: dict[str, Any]) -> str:
    results = res.get("results") or []
    if not results or "\n#### " not in rendered:
        return rendered
    parts = rendered.split("\n#### ")
    cards: list[str] = []
    for idx, card in enumerate(parts[1:]):
        u = results[idx] if idx < len(results) and isinstance(results[idx], dict) else {}
        extra: list[str] = []
        d_en = u.get("vestigial_donor_mods") or []
        d_zh = u.get("vestigial_donor_mods_zh") or []
        if d_en:
            pairs = [
                f"{en} / {d_zh[i]}" if (i < len(d_zh) and d_zh[i] and d_zh[i] != en) else str(en)
                for i, en in enumerate(d_en)
            ]
            extra.append(f"- **Vestigial Donor Mods**: {'; '.join(pairs)}")
        icls = str(u.get("item_class") or "").strip()
        if icls in _VESTIGIAL_CAPABLE_CLASSES:
            extra.append(
                f"- **3.30 Vestigial Slot Note**: Any `{icls}` unique can legally carry a `{{vestigial}}` implicit "
                "(or PoB `====` custom line) inherited from another same-slot donor unique "
                "(e.g. `Cloak of Flame` inheriting `+200 to maximum Life` from `Kaom's Heart`); "
                'verify via `pob data mod "<stat>"` or `pob trade search` before removing.'
            )
        cards.append(card.rstrip() + "\n" + "\n".join(extra) if extra else card)
    return parts[0] + "\n#### " + "\n#### ".join(cards)


def render_data_with_vestigial_and_tattoos(
    res: dict[str, Any],
    kind: str,
    query: str,
    format_game_data_fn: Any,
    render_table_fn: Any,
) -> str:
    """Render `tattoo`/`runegraft` tables and enrich `unique`/`mod` Vestigial Markdown."""
    kl = kind.strip().lower()
    if kl in ("tattoo", "runegraft"):
        lines: list[str] = []
        for w in res.get("warnings") or []:
            if isinstance(w, dict):
                tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
                canon = f" (canonical: {w['canonicalTerm']})" if w.get("canonicalTerm") else ""
                lines.append(f"> {tag} `{w.get('trigger', '')}`{canon}: {w.get('warning', '')}")
        k_lbl = str(res.get("kind") or "tattoo")
        q_lbl = query or str(res.get("query") or "all")
        lines.append(
            f"### Local Game Data (`{k_lbl}`): `{q_lbl}` — `{res.get('count', 0)}` hits "
            f"(PoB `{res.get('pobVersion', 'unknown')}` / Data `{res.get('dataVersion', 'unknown')}`)"
        )
        results = res.get("results") or []
        if not results:
            lines.append("_(no matching tattoos or runegrafts)_")
            return "\n".join(lines)
        rows: list[list[Any]] = []
        for r in results:
            en, zh = str(r.get("name") or "-"), str(r.get("zh_name") or "")
            disp = f"{zh} ({en})" if (zh and zh != en) else en
            rk = ("Runegraft" if r.get("kind") == "runegraft" else "Tattoo") + (" [Legacy]" if r.get("legacy") else "")
            tt, tv = str(r.get("target_type") or "-"), str(r.get("target_value") or "")
            s_en, s_zh = r.get("stats") or [], r.get("stats_zh") or []
            sp = [f"{s} / {s_zh[i]}" if (i < len(s_zh) and s_zh[i] and s_zh[i] != s) else str(s) for i, s in enumerate(s_en)]
            rows.append([rk, disp, f"{tt} ({tv})" if tv else tt, "; ".join(sp) or "-", r.get("limit_text") or "-"])
        lines.append(render_table_fn(["Kind", "Name", "Target", "Stats", "Limit"], rows))
        return "\n".join(lines)

    rendered = str(format_game_data_fn(res, kind=kind, query=query))
    if kl == "unique":
        return _augment_unique_vestigial_md(rendered, res)
    if kl == "mod":
        bullets = [
            f"- `{m.get('id')}` ({'; '.join(str(s) for s in (m.get('stats') or ()) if s)}) <- Donor: **{', '.join(str(d) for d in m['vestigialDonors'])}**"
            for m in (res.get("results") or [])
            if isinstance(m, dict) and m.get("source") == "Vestigial" and m.get("vestigialDonors")
        ]
        if bullets:
            return rendered.rstrip() + "\n\n#### Vestigial Donor Attribution\n" + "\n".join(bullets)
    return rendered
