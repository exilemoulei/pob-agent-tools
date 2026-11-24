"""SQLite schema DDL, index validation, and atomic cross-process build lockfile."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from ..os_util import _is_lockfile_stale
from ..os_util import _try_break_stale_lock as _try_break_stale_lock
from .constants import SCHEMA_VERSION
from .paths import _sqlite_ro_uri


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE gems (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            display_name_lower TEXT NOT NULL,
            zh_name TEXT,
            zh_lower TEXT,
            game_id TEXT,
            variant_id TEXT,
            is_support INTEGER NOT NULL,
            is_vaal INTEGER NOT NULL,
            is_transfigured INTEGER NOT NULL,
            is_imbued INTEGER NOT NULL,
            is_exceptional INTEGER NOT NULL DEFAULT 0,
            legacy_removed_328 INTEGER NOT NULL DEFAULT 0,
            natural_max_level INTEGER NOT NULL DEFAULT 20,
            corrupted_max_level INTEGER NOT NULL DEFAULT 21,
            is_item_skill INTEGER NOT NULL DEFAULT 0,
            color TEXT,
            tag_string TEXT,
            search_text_lower TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX idx_gems_name_lower ON gems(name_lower);
        CREATE INDEX idx_gems_display_lower ON gems(display_name_lower);
        CREATE INDEX idx_gems_zh_lower ON gems(zh_lower);

        CREATE TABLE uniques (
            name TEXT PRIMARY KEY,
            name_lower TEXT NOT NULL,
            zh_name TEXT,
            zh_lower TEXT,
            foulborn_lower TEXT,
            zh_foulborn_lower TEXT,
            base_type TEXT NOT NULL,
            item_class TEXT NOT NULL,
            slots_lower TEXT NOT NULL,
            has_foulborn INTEGER NOT NULL,
            search_text_lower TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX idx_uniques_name_lower ON uniques(name_lower);
        CREATE INDEX idx_uniques_zh_lower ON uniques(zh_lower);

        CREATE TABLE bases (
            name TEXT PRIMARY KEY,
            name_lower TEXT NOT NULL,
            zh_name TEXT,
            zh_lower TEXT,
            item_type TEXT NOT NULL,
            sub_type TEXT,
            req_level INTEGER NOT NULL,
            hidden INTEGER NOT NULL,
            slots_lower TEXT NOT NULL,
            search_text_lower TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX idx_bases_name_lower ON bases(name_lower);
        CREATE INDEX idx_bases_zh_lower ON bases(zh_lower);

        CREATE TABLE mods (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            gen_type TEXT NOT NULL,
            mod_group TEXT NOT NULL,
            mod_group_lower TEXT NOT NULL,
            affix TEXT NOT NULL,
            affix_lower TEXT NOT NULL,
            level INTEGER NOT NULL,
            stats_en_lower TEXT NOT NULL,
            stats_zh_lower TEXT NOT NULL,
            weights_json TEXT NOT NULL,
            zero_weights_json TEXT NOT NULL,
            types_json TEXT NOT NULL,
            search_en_lower TEXT NOT NULL,
            search_zh_lower TEXT NOT NULL,
            search_text_lower TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX idx_mods_level ON mods(level);
        CREATE INDEX idx_mods_group ON mods(mod_group_lower);

        CREATE TABLE translations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            en TEXT NOT NULL,
            en_lower TEXT NOT NULL,
            zh TEXT NOT NULL,
            zh_lower TEXT NOT NULL,
            category TEXT NOT NULL,
            cat_rank INTEGER NOT NULL
        );

        CREATE VIRTUAL TABLE gems_fts USING fts5(search_text_lower, content='gems', tokenize='trigram');
        CREATE VIRTUAL TABLE uniques_fts USING fts5(search_text_lower, content='uniques', tokenize='trigram');
        CREATE VIRTUAL TABLE bases_fts USING fts5(search_text_lower, content='bases', tokenize='trigram');
        CREATE VIRTUAL TABLE mods_fts USING fts5(search_text_lower, content='mods', tokenize='trigram');
        CREATE VIRTUAL TABLE translations_zh_fts USING fts5(
            zh_lower, content='translations', content_rowid='id', tokenize='trigram'
        );
        CREATE VIRTUAL TABLE translations_en_fts USING fts5(
            en_lower, content='translations', content_rowid='id', tokenize='unicode61'
        );
        """
    )


def _is_index_valid(db_path: Path, pob_version: str, data_version: str) -> bool:
    if not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
        try:
            rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            return (
                rows.get("schema_version") == str(SCHEMA_VERSION)
                and rows.get("pob_version") == pob_version
                and rows.get("data_version") == data_version
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _is_build_lock_stale(lock_path: Path, *, stale_sec: float = 60.0) -> bool:
    """SQLite build-lock variant (60s default); logic lives in os_util (L0)."""
    return _is_lockfile_stale(lock_path, stale_sec=stale_sec)


def _release_build_lock(lock_path: Path, owner_token: str) -> None:
    """Unlink ``lock_path`` only if its stored owner token still matches ``owner_token``."""
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("token") != owner_token:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _acquire_build_lock(
    lock_path: Path,
    db_path: Path,
    pob_version: str,
    data_version: str,
    *,
    force_rebuild: bool = False,
    timeout_sec: float = 45.0,
    stale_sec: float = 60.0,
) -> str | None:
    """Acquire atomic cross-process lockfile; returns owner token if acquired, None if DB became valid while waiting."""
    deadline = time.monotonic() + max(0.05, float(timeout_sec))
    deadline_break_attempted = False
    stale_breaks = 0
    while True:
        token = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(
                    fd,
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "tid": threading.get_ident(),
                            "token": token,
                            "ts": time.time(),
                        }
                    ).encode("utf-8"),
                )
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            else:
                try:
                    os.close(fd)
                except BaseException:
                    try:
                        lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
            return token
        except FileExistsError:
            if not force_rebuild and _is_index_valid(db_path, pob_version, data_version):
                return None
            now_mono = time.monotonic()
            if _is_build_lock_stale(lock_path, stale_sec=stale_sec):
                if _try_break_stale_lock(lock_path):
                    stale_breaks += 1
                    if now_mono < deadline:
                        if stale_breaks > 1:
                            time.sleep(0.05)
                        continue
                    if not deadline_break_attempted:
                        deadline_break_attempted = True
                        continue
                    raise TimeoutError(f"Timed out acquiring game data build lock: {lock_path}") from None
                if now_mono >= deadline:
                    raise TimeoutError(f"Timed out breaking stale game data build lock: {lock_path}") from None
                time.sleep(0.05)
                continue
            if now_mono >= deadline:
                if not deadline_break_attempted:
                    deadline_break_attempted = True
                    if _try_break_stale_lock(lock_path):
                        continue
                raise TimeoutError(f"Timed out waiting for game data build lock: {lock_path}") from None
            time.sleep(0.05)
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Failed to acquire game data build lock {lock_path}: {exc}") from exc
            time.sleep(0.05)
