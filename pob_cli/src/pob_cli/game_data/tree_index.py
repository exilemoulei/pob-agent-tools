"""0ms daemon-free passive tree parser and versioned SQLite index (`TreeData/*/tree.lua`)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .paths import (
    _prune_old_indexes,
    _resolve_poe1_data_dirs,
    _shared_cache_dir,
    _sqlite_ro_uri,
    resolve_pob_dir,
    resolve_runtime_dir,
)
from .tw_norm import _fold_search_text

TREE_SCHEMA_VERSION = 5
_TREE_INDEX_LOCK = threading.RLock()
_VALID_TREE_INDEX_CACHE: set[tuple[str, str, str]] = set()

_TREE_NODE_BLOCK_RE = re.compile(r"\n\t\t\[(\d+)\]\s*=\s*\{([\s\S]*?)\n\t\t\},")
_TREE_QUOTED_FIELD_RE = re.compile(r'\["(name|ascendancyName)"\]\s*=\s*"((?:\\[\s\S]|[^"\\])*)"')
_TREE_BOOL_FIELD_RE = re.compile(
    r'\["(isKeystone|isNotable|isMastery|isJewelSocket|isBloodline|isMultipleChoiceOption|isProxy|isAscendancyStart)"\]\s*=\s*true'
)
_TREE_STATS_BLOCK_RE = re.compile(r'\["stats"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\},')
_TREE_REMINDER_BLOCK_RE = re.compile(r'\["reminderText"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\},')
_TREE_MASTERY_BLOCK_RE = re.compile(r'\["masteryEffects"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\},')
_TREE_MASTERY_ITEM_RE = re.compile(
    r'\["effect"\]\s*=\s*(\d+),[\s\S]*?\["stats"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\t\t\},'
)
_TREE_LUA_STR_RE = re.compile(r'"((?:\\[\s\S]|[^"\\])*)"')
_TREE_ASC_ID_NAME_RE = re.compile(r'\["id"\]\s*=\s*"([^"]+)"\s*,\s*\["name"\]\s*=\s*"([^"]+)"')


# Every dictionary file _load_tree_zh_maps reads besides tree_sd.json. The index hash covers the
# same list: it used to cover three of them, so a change to the others left stale Chinese names.
_TREE_ZH_NAME_FILES: tuple[str, ...] = (
    "tree_dn.json",
    "Z-S25.json",
    "Z-S26.json",
    "Z-S27.json",
    "Z-S28.json",
    "Z-S29.json",
    "Z-S30.json",
    "Z-S30-Pearl_of_Tsoatha-Passive_Tree.json",
)


def _find_latest_tree_lua(poe1_dir: Path) -> tuple[str, Path]:
    tree_root = poe1_dir / "TreeData"
    if not tree_root.is_dir():
        raise FileNotFoundError(f"PoE1/TreeData directory not found under {poe1_dir}")
    candidates: list[tuple[tuple[int, int, int, str], str, Path]] = []
    for sub in tree_root.iterdir():
        if not sub.is_dir():
            continue
        lua_p = sub / "tree.lua"
        if not lua_p.is_file():
            continue
        m = re.match(r"^(\d+)_(\d+)(.*)$", sub.name)
        if m:
            is_standard = 1 if not m.group(3) else 0
            ver_key = (int(m.group(1)), int(m.group(2)), is_standard, sub.name)
        else:
            ver_key = (0, 0, 0, sub.name)
        candidates.append((ver_key, sub.name, lua_p))
    if not candidates:
        raise FileNotFoundError(f"No tree.lua found under {tree_root}")
    candidates.sort(key=lambda x: x[0])
    _, ver_name, best_path = candidates[-1]
    return ver_name, best_path


def _extract_top_lua_section(txt: str, key: str) -> str | None:
    marker = f'\n\t["{key}"] = {{'
    start = txt.find(marker)
    if start < 0:
        return None
    end = txt.find("\n\t},", start)
    if end < 0:
        end = txt.find("\n\t}", start)
    if end < 0:
        return None
    return txt[start + len(marker) - 1 : end + 3]


def _unescape_tree_str(raw_inner: str) -> list[str]:
    """Unescape a Lua double-quoted string from tree.lua (splitting multiline `\\\\\\n` stats)."""
    s = raw_inner.replace("\\\r\n", "\n").replace("\\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return [part.strip() for part in s.splitlines() if part.strip()]


def _load_tree_zh_maps(zh_dir: Path | None) -> tuple[dict[str, str], dict[str, str]]:
    """Load EN->ZH maps for passive node/ascendancy names (`tree_dn.json`) and stats (`tree_sd.json` + S30)."""
    dn_map: dict[str, str] = {}
    sd_map: dict[str, str] = {}
    if zh_dir is None or not zh_dir.is_dir():
        return dn_map, sd_map
    for fn in _TREE_ZH_NAME_FILES:
        p = zh_dir / fn
        if not p.is_file():
            continue
        try:
            entries = json.loads(p.read_text(encoding="utf-8")).get("entries")
        except Exception:
            continue
        if not isinstance(entries, dict):
            continue
        for k, v in entries.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                dn_map[k.strip()] = v.strip()
                if fn != "tree_dn.json":
                    sd_map[k.strip().lower()] = v.strip()
    sd_path = zh_dir / "tree_sd.json"
    if sd_path.is_file():
        try:
            entries = json.loads(sd_path.read_text(encoding="utf-8")).get("entries")
            if isinstance(entries, dict):
                for k, v in entries.items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                        kl = k.strip().lower()
                        if kl not in sd_map:
                            sd_map[kl] = v.strip()
        except Exception:
            pass
    return dn_map, sd_map


def _parse_legion_passives(
    legion_path: Path, dn_map: dict[str, str], sd_map: dict[str, str]
) -> list[dict[str, Any]]:
    """Index Timeless / Historic Jewel Keystones & Notables from LegionPassives.lua."""
    if not legion_path.is_file():
        return []
    leg_txt = legion_path.read_text(encoding="utf-8", errors="ignore")
    leg_nodes_sec = _extract_top_lua_section(leg_txt, "nodes") or leg_txt
    seen_leg_names: set[str] = set()
    out: list[dict[str, Any]] = []
    for m_leg in _TREE_NODE_BLOCK_RE.finditer(leg_nodes_sec):
        leg_idx = int(m_leg.group(1))
        lbody = m_leg.group(2)
        is_lks = bool(re.search(r'\["ks"\]\s*=\s*true', lbody))
        is_lnot = bool(re.search(r'\["not"\]\s*=\s*true', lbody))
        if not (is_lks or is_lnot):
            continue
        m_dn = re.search(r'\["dn"\]\s*=\s*"((?:\\[\s\S]|[^"\\])*)"', lbody)
        if not m_dn:
            continue
        lname_parts = _unescape_tree_str(m_dn.group(1))
        lname = lname_parts[0] if lname_parts else ""
        if not lname or lname.lower() in seen_leg_names:
            continue
        seen_leg_names.add(lname.lower())
        lstats: list[str] = []
        m_sd = re.search(r'\["sd"\]\s*=\s*\{([\s\S]*?)\n\t\t\t\},', lbody)
        if m_sd:
            for sm in _TREE_LUA_STR_RE.finditer(m_sd.group(1)):
                lstats.extend(_unescape_tree_str(sm.group(1)))
        lzh_name = dn_map.get(lname)
        lstats_zh = [z for s in lstats if (z := sd_map.get(s.lower()))]
        out.append(
            {
                "id": 900000 + leg_idx,
                "name": lname,
                "zhName": lzh_name,
                "type": "Keystone" if is_lks else "Notable",
                "ascId": "Timeless",
                "ascName": "Timeless Jewel",
                "ascNameZh": "永恒珠宝",
                "className": None,
                "isAscendancy": False,
                "isBloodline": False,
                "isAlternateAsc": False,
                "stats": lstats,
                "statsZh": lstats_zh,
                "masteryEffects": [],
                "reminderText": [],
            }
        )
    return out


def _parse_tree_lua_nodes(tree_lua_path: Path, zh_dir: Path | None) -> list[dict[str, Any]]:
    txt = tree_lua_path.read_text(encoding="utf-8", errors="ignore")
    dn_map, sd_map = _load_tree_zh_maps(zh_dir)

    # 1. Build ascendancy ID -> (display_name, class_name, is_alternate_asc, is_bloodline)
    asc_meta: dict[str, tuple[str, str, bool, bool]] = {}
    cls_sec = _extract_top_lua_section(txt, "classes")
    if cls_sec:
        for aid, aname in _TREE_ASC_ID_NAME_RE.findall(cls_sec):
            aid_s = aid.strip()
            aname_s = aname.strip() or aid_s
            if aid_s:
                asc_meta[aid_s] = (aname_s, "", False, False)

    alt_sec = _extract_top_lua_section(txt, "alternate_ascendancies")
    if alt_sec:
        for aid, aname in _TREE_ASC_ID_NAME_RE.findall(alt_sec):
            aid_s = aid.strip()
            aname_s = aname.strip() or aid_s
            if aid_s and aid_s not in asc_meta:
                is_bl = "bloodline" in aname_s.lower()
                asc_meta[aid_s] = (aname_s, "", not is_bl, is_bl)

    nodes_sec = _extract_top_lua_section(txt, "nodes")
    if not nodes_sec:
        return []

    records: list[dict[str, Any]] = []
    for m in _TREE_NODE_BLOCK_RE.finditer(nodes_sec):
        nid = int(m.group(1))
        body = m.group(2)
        flags = set(_TREE_BOOL_FIELD_RE.findall(body))
        if "isProxy" in flags or "isAscendancyStart" in flags or '["classStartIndex"]' in body:
            continue

        fields = dict(_TREE_QUOTED_FIELD_RE.findall(body))
        raw_name = fields.get("name", "")
        name_parts = _unescape_tree_str(raw_name)
        name = name_parts[0] if name_parts else ""
        if not name:
            continue

        raw_asc_id = fields.get("ascendancyName", "").strip()
        stats: list[str] = []
        m_st = _TREE_STATS_BLOCK_RE.search(body)
        if m_st:
            for sm in _TREE_LUA_STR_RE.finditer(m_st.group(1)):
                stats.extend(_unescape_tree_str(sm.group(1)))

        mastery_effects: list[dict[str, Any]] = []
        if "isMastery" in flags:
            m_me = _TREE_MASTERY_BLOCK_RE.search(body)
            if m_me:
                for em in _TREE_MASTERY_ITEM_RE.finditer(m_me.group(1)):
                    eid = int(em.group(1))
                    estats: list[str] = []
                    for sm in _TREE_LUA_STR_RE.finditer(em.group(2)):
                        estats.extend(_unescape_tree_str(sm.group(1)))
                    estats_zh = [sd_map.get(s.lower()) for s in estats]
                    mastery_effects.append(
                        {"effect": eid, "stats": estats, "statsZh": [z for z in estats_zh if z]}
                    )

        reminder: list[str] = []
        m_rem = _TREE_REMINDER_BLOCK_RE.search(body)
        if m_rem:
            for rm in _TREE_LUA_STR_RE.finditer(m_rem.group(1)):
                reminder.extend(_unescape_tree_str(rm.group(1)))

        is_keystone = "isKeystone" in flags
        is_notable = "isNotable" in flags
        is_mastery = "isMastery" in flags
        is_socket = "isJewelSocket" in flags
        is_bloodline_flag = "isBloodline" in flags
        is_multi_opt = "isMultipleChoiceOption" in flags

        asc_display = ""
        class_name = ""
        is_alt_asc = False
        is_bloodline = is_bloodline_flag
        if raw_asc_id:
            if raw_asc_id in asc_meta:
                asc_display, class_name, is_alt_asc, meta_bl = asc_meta[raw_asc_id]
                is_bloodline = is_bloodline or meta_bl
            else:
                asc_display = raw_asc_id

        is_ascendancy = bool(raw_asc_id)
        if is_keystone:
            ntype = "Keystone"
        elif is_mastery:
            ntype = "Mastery"
        elif is_socket:
            ntype = "Socket"
        elif is_bloodline:
            ntype = "Bloodline"
        elif is_ascendancy and (is_notable or is_multi_opt):
            ntype = "Ascendancy"
        elif is_notable:
            ntype = "Notable"
        else:
            ntype = "Normal"

        zh_name = dn_map.get(name)
        asc_zh = dn_map.get(asc_display) if asc_display else None
        stats_zh = [z for s in stats if (z := sd_map.get(s.lower()))]

        records.append(
            {
                "id": nid,
                "name": name,
                "zhName": zh_name,
                "type": ntype,
                "ascId": raw_asc_id or None,
                "ascName": asc_display or None,
                "ascNameZh": asc_zh,
                "className": class_name or None,
                "isAscendancy": is_ascendancy,
                "isBloodline": is_bloodline,
                "isAlternateAsc": is_alt_asc,
                "stats": stats,
                "statsZh": stats_zh,
                "masteryEffects": mastery_effects,
                "reminderText": reminder,
            }
        )

    legion_path = tree_lua_path.parent.parent.parent / "Data" / "TimelessJewelData" / "LegionPassives.lua"
    records.extend(_parse_legion_passives(legion_path, dn_map, sd_map))
    return records


def ensure_treedata_index(
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    force_rebuild: bool = False,
) -> tuple[Path, str]:
    """Build (if missing/stale) and return `(db_path, tree_version)` for 0ms daemon-free passive tree queries."""
    rt_dir = resolve_runtime_dir(runtime_dir)
    resolved_pob = resolve_pob_dir(pob_dir, rt_dir)
    poe1_dir, zh_dir = _resolve_poe1_data_dirs(resolved_pob)
    tree_ver, tree_lua_path = _find_latest_tree_lua(poe1_dir)

    hasher = hashlib.blake2b(digest_size=4)
    st = tree_lua_path.stat()
    hasher.update(f"{tree_ver}:{st.st_size}:{st.st_mtime_ns};".encode())
    if zh_dir is not None and zh_dir.is_dir():
        for fn in (*_TREE_ZH_NAME_FILES, "tree_sd.json"):
            zp = zh_dir / fn
            if zp.is_file():
                zst = zp.stat()
                hasher.update(f"{fn}:{zst.st_size}:{zst.st_mtime_ns};".encode())
    data_hash = hasher.hexdigest()

    cache_dir = _shared_cache_dir(runtime_dir, pob_root=resolved_pob)
    db_path = cache_dir / f"treedata_{tree_ver}_{data_hash}.sqlite"
    cache_key = (str(db_path), tree_ver, data_hash)

    def _is_tree_db_valid() -> bool:
        if not db_path.is_file():
            return False
        try:
            c = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
            try:
                m = dict(c.execute("SELECT key, value FROM meta").fetchall())
                return (
                    m.get("schema_version") == str(TREE_SCHEMA_VERSION)
                    and m.get("tree_version") == tree_ver
                    and m.get("data_hash") == data_hash
                )
            finally:
                c.close()
        except sqlite3.Error:
            return False

    if not force_rebuild:
        if cache_key in _VALID_TREE_INDEX_CACHE and db_path.is_file():
            return db_path, tree_ver
        if _is_tree_db_valid():
            _VALID_TREE_INDEX_CACHE.add(cache_key)
            return db_path, tree_ver
    else:
        _VALID_TREE_INDEX_CACHE.discard(cache_key)

    with _TREE_INDEX_LOCK:
        if not force_rebuild and _is_tree_db_valid():
            _VALID_TREE_INDEX_CACHE.add(cache_key)
            return db_path, tree_ver

        nodes = _parse_tree_lua_nodes(tree_lua_path, zh_dir)
        tmp_path = cache_dir / f".treedata_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.sqlite.tmp"
        try:
            conn = sqlite3.connect(str(tmp_path))
            try:
                conn.execute("PRAGMA page_size = 8192")
                conn.execute("PRAGMA journal_mode = OFF")
                conn.execute("PRAGMA synchronous = OFF")
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE tree_nodes (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        name_lower TEXT NOT NULL,
                        zh_name TEXT,
                        zh_lower TEXT,
                        node_type TEXT NOT NULL,
                        asc_id_lower TEXT,
                        asc_name_lower TEXT,
                        asc_zh_lower TEXT,
                        is_asc INTEGER NOT NULL,
                        is_bloodline INTEGER NOT NULL,
                        is_alt_asc INTEGER NOT NULL,
                        has_stats INTEGER NOT NULL,
                        search_text_lower TEXT NOT NULL,
                        data_json TEXT NOT NULL
                    );
                    CREATE INDEX idx_tn_type ON tree_nodes(node_type);
                    CREATE INDEX idx_tn_asc ON tree_nodes(asc_name_lower);
                    CREATE VIRTUAL TABLE tree_nodes_fts USING fts5(
                        search_text_lower, content='tree_nodes', content_rowid='id', tokenize='trigram'
                    );
                    """
                )
                conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(TREE_SCHEMA_VERSION)),
                        ("tree_version", tree_ver),
                        ("data_hash", data_hash),
                        ("zh_available", "1" if zh_dir is not None else "0"),
                    ],
                )
                rows_to_insert = []
                for nd in nodes:
                    me_texts = []
                    for me in nd["masteryEffects"]:
                        me_texts.extend(me["stats"])
                        me_texts.extend(me["statsZh"])
                    s_txt = _fold_search_text(
                        " ".join(
                            p
                            for p in (
                                nd["name"],
                                nd["zhName"] or "",
                                nd["ascName"] or "",
                                nd["ascNameZh"] or "",
                                nd["ascId"] or "",
                                nd["className"] or "",
                                " ".join(nd["stats"]),
                                " ".join(nd["statsZh"]),
                                " ".join(me_texts),
                            )
                            if p
                        )
                    )
                    rows_to_insert.append(
                        (
                            nd["id"],
                            nd["name"],
                            _fold_search_text(nd["name"]),
                            nd["zhName"],
                            _fold_search_text(nd["zhName"]) if nd["zhName"] else None,
                            nd["type"],
                            _fold_search_text(nd["ascId"]) if nd["ascId"] else None,
                            _fold_search_text(nd["ascName"]) if nd["ascName"] else None,
                            _fold_search_text(nd["ascNameZh"]) if nd["ascNameZh"] else None,
                            int(nd["isAscendancy"]),
                            int(nd["isBloodline"]),
                            int(nd["isAlternateAsc"]),
                            int(bool(nd["stats"] or nd["masteryEffects"])),
                            s_txt,
                            json.dumps(nd, ensure_ascii=False, separators=(",", ":")),
                        )
                    )
                conn.executemany("INSERT INTO tree_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_to_insert)
                conn.execute("INSERT INTO tree_nodes_fts(tree_nodes_fts) VALUES('rebuild')")
                conn.commit()
            finally:
                conn.close()
            try:
                os.replace(tmp_path, db_path)
            except PermissionError:
                if not _is_tree_db_valid():
                    raise
            _VALID_TREE_INDEX_CACHE.add(cache_key)
            _prune_old_indexes(cache_dir, "treedata_*.sqlite", db_path)
            return db_path, tree_ver
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
