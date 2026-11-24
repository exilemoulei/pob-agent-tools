"""SQLite writer and versioned game data index manager."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .constants import _INDEX_BUILD_LOCK, _VALID_INDEX_CACHE, SCHEMA_VERSION
from .parsers_items import _parse_bases, _parse_mods, _parse_uniques
from .paths import (
    _prune_old_indexes,
    _resolve_poe1_data_dirs,
    _shared_cache_dir,
    detect_versions,
    resolve_pob_dir,
    resolve_runtime_dir,
)
from .sqlite_lock import (
    _acquire_build_lock,
    _create_schema,
    _is_build_lock_stale,
    _is_index_valid,
    _release_build_lock,
    _try_break_stale_lock,
)
from .tw_norm import _fold_search_text
from .zh_dict import _ZhDictionary

__all__ = [
    "_acquire_build_lock",
    "_create_schema",
    "_insert_bases_table",
    "_insert_gems_table",
    "_insert_mods_table",
    "_insert_translations_table",
    "_insert_uniques_table",
    "_is_build_lock_stale",
    "_is_index_valid",
    "_release_build_lock",
    "_try_break_stale_lock",
    "_write_gamedata_sqlite",
    "ensure_gamedata_index",
]


def _insert_gems_table(conn: sqlite3.Connection, gems: list[dict[str, Any]]) -> None:
    gem_rows = []
    for g in gems:
        s_txt = _fold_search_text(
            " ".join(
                p
                for p in (
                    g["name"],
                    g["display_name"],
                    g["zh_name"] or "",
                    g["tag_string"],
                    g["granted_effect_id"],
                )
                if p
            )
        )
        gem_rows.append(
            (
                g["id"],
                g["name"],
                _fold_search_text(g["name"]),
                _fold_search_text(g["display_name"]),
                g["zh_name"],
                _fold_search_text(g["zh_name"]) if g["zh_name"] else None,
                g["game_id"],
                g["variant_id"],
                int(g["is_support"]),
                int(g["is_vaal"]),
                int(g["is_transfigured"]),
                int(g["is_imbued"]),
                int(bool(g.get("is_exceptional"))),
                int(bool(g.get("legacy_removed_328"))),
                int(g.get("natural_max_level") or 20),
                int(g.get("corrupted_max_level") or (int(g.get("natural_max_level") or 20) + 1)),
                int(bool(g.get("is_item_skill"))),
                g["color"],
                g["tag_string"],
                s_txt,
                json.dumps(g, ensure_ascii=False, separators=(",", ":")),
            )
        )
    conn.executemany("INSERT INTO gems VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", gem_rows)
    conn.execute("INSERT INTO gems_fts(gems_fts) VALUES('rebuild')")


def _insert_bases_table(conn: sqlite3.Connection, bases: list[dict[str, Any]]) -> None:
    base_rows = []
    for b in bases:
        slots_l = _fold_search_text(" ".join([b["type"], b["sub_type"] or "", *b["tags"]]))
        s_txt = _fold_search_text(
            " ".join(
                p
                for p in (
                    b["name"],
                    b["zh_name"] or "",
                    b["type"],
                    b["sub_type"] or "",
                    " ".join(b["implicits"]),
                    " ".join(x for x in b["implicits_zh"] if x),
                )
                if p
            )
        )
        base_rows.append(
            (
                b["name"],
                _fold_search_text(b["name"]),
                b["zh_name"],
                _fold_search_text(b["zh_name"]) if b["zh_name"] else None,
                b["type"],
                b["sub_type"],
                b["req"]["level"],
                int(b["hidden"]),
                slots_l,
                s_txt,
                json.dumps(b, ensure_ascii=False, separators=(",", ":")),
            )
        )
    conn.executemany("INSERT INTO bases VALUES (?,?,?,?,?,?,?,?,?,?,?)", base_rows)
    conn.execute("INSERT INTO bases_fts(bases_fts) VALUES('rebuild')")


def _insert_uniques_table(conn: sqlite3.Connection, uniques: list[dict[str, Any]]) -> None:
    unique_rows = []
    for u in uniques:
        slots_l = _fold_search_text(" ".join([u["item_class"], *u["tags"]]))
        fb_lines = " ".join(
            " ".join(fm["stats"]) + " " + " ".join(x for x in fm["stats_zh"] if x) for fm in u["foulborn_mods"]
        )
        s_txt = _fold_search_text(
            " ".join(
                p
                for p in (
                    u["name"],
                    u["zh_name"] or "",
                    u["foulborn_name"] or "",
                    u["zh_foulborn_name"] or "",
                    u["base_type"],
                    u["base_type_zh"] or "",
                    " ".join(u["implicits"]),
                    " ".join(u["explicits"]),
                    " ".join(x for x in u["implicits_zh"] if x),
                    " ".join(x for x in u["explicits_zh"] if x),
                    fb_lines,
                )
                if p
            )
        )
        unique_rows.append(
            (
                u["name"],
                _fold_search_text(u["name"]),
                u["zh_name"],
                _fold_search_text(u["zh_name"]) if u["zh_name"] else None,
                _fold_search_text(u["foulborn_name"]) if u["foulborn_name"] else None,
                _fold_search_text(u["zh_foulborn_name"]) if u["zh_foulborn_name"] else None,
                u["base_type"],
                u["item_class"],
                slots_l,
                int(bool(u["foulborn_mods"] or u["zh_foulborn_name"])),
                s_txt,
                json.dumps(u, ensure_ascii=False, separators=(",", ":")),
            )
        )
    conn.executemany("INSERT INTO uniques VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", unique_rows)
    conn.execute("INSERT INTO uniques_fts(uniques_fts) VALUES('rebuild')")


def _insert_mods_table(conn: sqlite3.Connection, mods: list[dict[str, Any]]) -> None:
    mod_rows = []
    for m in mods:
        en_stats = _fold_search_text("\n".join(m["stats"]))
        zh_stats = _fold_search_text("\n".join(x for x in m["stats_zh"] if x))
        search_en = _fold_search_text(" ".join([m["id"], m["affix"], m["group"]])) + " " + en_stats
        search_zh = zh_stats
        fts_txt = f"{search_en} {search_zh}".strip()
        mod_rows.append(
            (
                m["id"],
                m["source"],
                m["generation_type"],
                m["group"],
                _fold_search_text(m["group"]),
                m["affix"],
                _fold_search_text(m["affix"]),
                m["level"],
                en_stats,
                zh_stats,
                json.dumps(m["weights"], separators=(",", ":")),
                json.dumps(m["zero_weights"], separators=(",", ":")),
                json.dumps(m["applicable_types"], separators=(",", ":")),
                search_en,
                search_zh,
                fts_txt,
                json.dumps(m, ensure_ascii=False, separators=(",", ":")),
            )
        )
    conn.executemany("INSERT INTO mods VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", mod_rows)
    conn.execute("INSERT INTO mods_fts(mods_fts) VALUES('rebuild')")


def _insert_translations_table(
    conn: sqlite3.Connection,
    zh: _ZhDictionary,
    gems: list[dict[str, Any]],
    uniques: list[dict[str, Any]],
    bases: list[dict[str, Any]],
) -> None:
    tr_best: dict[tuple[str, str], tuple[str, str, str, str, str, int]] = {}

    def _add_tr(en_s: str, zh_s: str, cat_s: str, rank_i: int) -> None:
        en_folded = _fold_search_text(en_s)
        zh_folded = _fold_search_text(zh_s)
        k = (en_folded, zh_s)
        prev = tr_best.get(k)
        if prev is None or rank_i < prev[5]:
            tr_best[k] = (en_s, en_folded, zh_s, zh_folded, cat_s, rank_i)

    for en, zh_str, cat, rank in zh.all_entries:
        _add_tr(en, zh_str, cat, rank)
    for g in gems:
        if g["zh_name"]:
            _add_tr(g["name"], g["zh_name"], "gem", 1)
        for alias_zh in zh.aliases.get(g["name"].lower(), ()):
            _add_tr(g["name"], alias_zh, "gem", 2)
    for u in uniques:
        if u["zh_name"]:
            _add_tr(u["name"], u["zh_name"], "unique", 1)
        for alias_zh in zh.aliases.get(u["name"].lower(), ()):
            _add_tr(u["name"], alias_zh, "unique", 2)
    for b in bases:
        if b["zh_name"]:
            _add_tr(b["name"], b["zh_name"], "base", 1)
        for alias_zh in zh.aliases.get(b["name"].lower(), ()):
            _add_tr(b["name"], alias_zh, "base", 2)
    for p_en in zh.passives:
        for alias_zh in zh.aliases.get(p_en.lower(), ()):
            _add_tr(p_en, alias_zh, "passive", 2)

    conn.executemany(
        "INSERT INTO translations (en, en_lower, zh, zh_lower, category, cat_rank) VALUES (?,?,?,?,?,?)",
        list(tr_best.values()),
    )
    conn.execute("CREATE INDEX idx_tr_en_lower ON translations(en_lower)")
    conn.execute("CREATE INDEX idx_tr_zh_lower ON translations(zh_lower)")
    conn.execute("INSERT INTO translations_zh_fts(translations_zh_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO translations_en_fts(translations_en_fts) VALUES('rebuild')")


def _write_gamedata_sqlite(
    tmp_path: Path,
    *,
    pob_version: str,
    data_version: str,
    zh: _ZhDictionary,
    gems: list[dict[str, Any]],
    bases: list[dict[str, Any]],
    uniques: list[dict[str, Any]],
    mods: list[dict[str, Any]],
) -> None:
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.execute("PRAGMA page_size = 8192")
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -65536")
        _create_schema(conn)
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")

        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("pob_version", pob_version),
                ("data_version", data_version),
                ("zh_available", "1" if zh.available else "0"),
                ("built_at", str(time.time())),
            ],
        )
        _insert_gems_table(conn, gems)
        _insert_bases_table(conn, bases)
        _insert_uniques_table(conn, uniques)
        _insert_mods_table(conn, mods)
        _insert_translations_table(conn, zh, gems, uniques, bases)
        conn.commit()
    finally:
        conn.close()


def ensure_gamedata_index(
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    force_rebuild: bool = False,
) -> Path:
    """Build (if missing/stale) and return path to the versioned SQLite game data index."""
    import pob_cli.game_data as _gd

    rt_dir = resolve_runtime_dir(runtime_dir)
    resolved_pob = resolve_pob_dir(pob_dir, rt_dir)
    poe1_dir, zh_dir = _resolve_poe1_data_dirs(resolved_pob)
    pob_version, data_version = detect_versions(poe1_dir, zh_dir)

    cache_dir = _shared_cache_dir(runtime_dir, pob_root=resolved_pob)
    db_path = cache_dir / f"gamedata_{pob_version}_{data_version}.sqlite"

    cache_key = (str(db_path), pob_version, data_version)
    if not force_rebuild:
        if cache_key in _VALID_INDEX_CACHE and db_path.is_file():
            return db_path
        if _is_index_valid(db_path, pob_version, data_version):
            _VALID_INDEX_CACHE.add(cache_key)
            return db_path
    else:
        _VALID_INDEX_CACHE.discard(cache_key)

    lock_path = Path(f"{db_path}.build.lock")
    with _INDEX_BUILD_LOCK:
        if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
            _VALID_INDEX_CACHE.add(cache_key)
            return db_path

        lock_token = _acquire_build_lock(
            lock_path,
            db_path,
            pob_version,
            data_version,
            force_rebuild=force_rebuild,
        )
        if not lock_token:
            _VALID_INDEX_CACHE.add(cache_key)
            return db_path

        tmp_path = cache_dir / f".gamedata_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.sqlite.tmp"
        try:
            if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
                _VALID_INDEX_CACHE.add(cache_key)
                return db_path

            zh = _ZhDictionary.load(zh_dir)
            gems = _gd._parse_gems_and_skills(poe1_dir, zh)
            bases = _parse_bases(poe1_dir, zh)
            bases_by_name = {b["name"]: b for b in bases}
            mods, foulborn_by_id = _parse_mods(poe1_dir, zh)
            uniques = _parse_uniques(poe1_dir, bases_by_name, foulborn_by_id, zh)

            _write_gamedata_sqlite(
                tmp_path,
                pob_version=pob_version,
                data_version=data_version,
                zh=zh,
                gems=gems,
                bases=bases,
                uniques=uniques,
                mods=mods,
            )

            replaced = False
            last_perm_err: PermissionError | None = None
            for attempt in range(4):
                try:
                    os.replace(tmp_path, db_path)
                    replaced = True
                    break
                except PermissionError as exc:
                    last_perm_err = exc
                    if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
                        replaced = True
                        break
                    if attempt < 3:
                        time.sleep(0.05)

            if not replaced:
                if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
                    replaced = True
                elif last_perm_err is not None:
                    raise last_perm_err

            _VALID_INDEX_CACHE.add(cache_key)
            _prune_old_indexes(cache_dir, "gamedata_*.sqlite", db_path)
            from ..guardrails import GuardrailScope, check_input

            for _sc in (GuardrailScope.STAT_QUERY, GuardrailScope.GEM_NAME, GuardrailScope.DATA_QUERY):
                check_input("x", scope=_sc, expected_game="poe1", emit_stderr=False)
            return db_path
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            if lock_token:
                _release_build_lock(lock_path, lock_token)
