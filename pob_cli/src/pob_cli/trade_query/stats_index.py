"""Trade stat catalog loader and zero-network local TradeSiteStats.lua cache."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ..importers import _get_cache_dir
from .stats_matcher import (
    StatMatch as StatMatch,
)
from .stats_matcher import (
    resolve_stat as resolve_stat,
)
from .stats_matcher import (
    suggest_stats as suggest_stats,
)

# Process-local cache for parsed TradeSiteStats.lua entries
_LOCAL_STATS_MEMO: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
_LOCAL_STATS_LOCK = threading.Lock()


def flatten_stats(groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten official `/api/trade/data/stats` groups into a list of stat entries."""
    entries: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_entries = group.get("entries")
        if isinstance(group_entries, list):
            group_type = str(group.get("id") or "").strip().lower()
            for entry in group_entries:
                if not isinstance(entry, dict):
                    continue
                stat_id = entry.get("id")
                text = entry.get("text")
                if stat_id and text:
                    item: dict[str, Any] = {
                        "id": str(stat_id),
                        "text": str(text),
                        "type": str(entry.get("type") or group_type or str(stat_id).split(".", 1)[0]),
                    }
                    entries.append(item)
        elif group.get("id") and group.get("text"):
            stat_id = str(group["id"])
            entries.append(
                {
                    "id": stat_id,
                    "text": str(group["text"]),
                    "type": str(group.get("type") or stat_id.split(".", 1)[0]),
                }
            )
    return entries


def _unescape_lua_string(raw: str) -> str:
    if "\\" not in raw:
        return raw
    return raw.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


def _parse_trade_site_stats_lua(lua_path: Path) -> list[dict[str, Any]]:
    """Fast depth-aware scanner for PoB's ``PoE1/Data/TradeSiteStats.lua``.

    Tracks table nesting depth so nested ``["option"] = { ["options"] = { ... } }``
    tables never collide with outer stat entry ``["id"]`` / ``["text"]`` / ``["type"]``.
    """
    text = lua_path.read_text(encoding="utf-8", errors="replace")
    entries: list[dict[str, Any]] = []
    depth = 0
    cur: dict[str, str] | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped.endswith("{"):
            depth += 1
            if depth == 4 and stripped == "{":
                cur = {}
            continue
        if stripped.startswith("}"):
            if depth == 4 and cur is not None:
                if "id" in cur and "text" in cur:
                    if "type" not in cur:
                        cur["type"] = cur["id"].split(".", 1)[0]
                    entries.append(cur)
                cur = None
            depth -= 1
            continue
        if depth == 4 and cur is not None:
            if stripped.startswith('["id"] = "'):
                end_q = stripped.rfind('"')
                if end_q > 10:
                    cur["id"] = stripped[10:end_q]
            elif stripped.startswith('["text"] = "'):
                end_q = stripped.rfind('"')
                if end_q > 12:
                    cur["text"] = _unescape_lua_string(stripped[12:end_q])
            elif stripped.startswith('["type"] = "'):
                end_q = stripped.rfind('"')
                if end_q > 12:
                    cur["type"] = stripped[12:end_q]
    return entries


def load_local_trade_stats(
    pob_root: Path | None = None,
    cache_dir: Path | None = None,
) -> list[dict[str, Any]] | None:
    """Load PoE1 trade stats from local ``PoE1/Data/TradeSiteStats.lua`` with JSON cache.

    Returns ``None`` cleanly when the local engine or ``TradeSiteStats.lua`` is absent.
    """
    import pob_cli.trade_query as _tq

    engine_root = Path(pob_root) if pob_root is not None else _tq.resolve_engine_root()
    lua_path = engine_root / "PoE1" / "Data" / "TradeSiteStats.lua"
    if not lua_path.is_file():
        return None

    try:
        st = lua_path.stat()
    except OSError:
        return None

    memo_key = (str(lua_path.resolve()), st.st_mtime_ns, st.st_size)
    with _LOCAL_STATS_LOCK:
        cached_mem = _LOCAL_STATS_MEMO.get(memo_key)
        if cached_mem is not None:
            return cached_mem

    resolved_cache_dir = Path(cache_dir) if cache_dir is not None else _get_cache_dir()
    cache_file = resolved_cache_dir / f"trade_stats_local_{st.st_mtime_ns}_{st.st_size}.json"

    if cache_file.is_file():
        try:
            loaded = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(loaded, list) and loaded:
                with _LOCAL_STATS_LOCK:
                    _LOCAL_STATS_MEMO[memo_key] = loaded
                return loaded
        except (OSError, ValueError):
            pass

    entries = _parse_trade_site_stats_lua(lua_path)
    if not entries:
        return None

    try:
        resolved_cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = resolved_cache_dir / f".{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp_path.write_text(
            json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp_path, cache_file)
    except OSError:
        pass

    with _LOCAL_STATS_LOCK:
        _LOCAL_STATS_MEMO[memo_key] = entries
    return entries


def _normalize_remote_stats(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict) and isinstance(raw.get("result"), list):
        return flatten_stats(raw["result"])
    if isinstance(raw, Iterable):
        return flatten_stats(raw)
    return []


def load_trade_stats(
    *,
    pob_root: Path | None = None,
    cache_dir: Path | None = None,
    remote_fetcher: Callable[[], Any] | None = None,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Load PoE1 trade stats preferring zero-network local ``TradeSiteStats.lua``.

    Returns ``(entries, source)`` where ``source`` is ``"local_engine"`` or ``"remote_api"``.
    Raises ``RuntimeError`` when neither local nor remote stats are available.
    """
    if not force_refresh:
        local = load_local_trade_stats(pob_root=pob_root, cache_dir=cache_dir)
        if local:
            return local, "local_engine"

    if remote_fetcher is not None:
        remote_raw = remote_fetcher()
        remote_entries = _normalize_remote_stats(remote_raw)
        if remote_entries:
            return remote_entries, "remote_api"

    if force_refresh:
        local = load_local_trade_stats(pob_root=pob_root, cache_dir=cache_dir)
        if local:
            return local, "local_engine"

    raise RuntimeError("No trade stats available locally or remotely.")
