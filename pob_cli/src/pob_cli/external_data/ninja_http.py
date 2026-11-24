"""poe.ninja economy HTTP fetchers, disk caches, and trade league resolution."""

from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from ..importers import (
    DEFAULT_HTTP_TIMEOUT,
    _atomic_write_json,
    _make_httpx_client,
    _shared_cache_dir,
    read_stale_cache,
)
from .constants import (
    _LAST_STALE,
    CURRENCY_TYPES,
    NINJA_DENSE_URL,
    NINJA_STASH_CURRENCY_URL,
    NINJA_STASH_ITEM_URL,
    POE_LEAGUES_URL,
)


def _pkg() -> Any:
    return sys.modules["pob_cli.external_data"]


def _extract_challenge_league_from_index_state(*, proxy: str | None = None) -> str | None:
    try:
        fetch_fn = _pkg()._fetch_poe_ninja_index_state
        state = fetch_fn(proxy=proxy) if proxy is not None else fetch_fn()
        econ_leagues = state.get("economyLeagues", [])
        if econ_leagues and econ_leagues[0].get("name"):
            return str(econ_leagues[0]["name"]).strip()
    except Exception:
        pass
    return None


def resolve_current_trade_league(ttl_sec: int = 3600, *, proxy: str | None = None) -> str:
    """Resolve current primary softcore trade league name with 1h disk cache.

    Stale 'Standard' cache entries are automatically invalidated/replaced whenever
    poe.ninja index-state reports a non-Standard challenge league in economyLeagues[0].
    """
    cache_file = _shared_cache_dir() / "poe_trade_league.json"
    now = time.time()
    cached_league: str | None = None

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and cached.get("league"):
                cached_league = str(cached["league"]).strip()
        except Exception:
            cached_league = None

    if cached_league and cached_league.lower() != "standard":
        return cached_league

    ninja_league = _extract_challenge_league_from_index_state(proxy=proxy)
    if ninja_league:
        if ninja_league.lower() != "standard" or not cached_league:
            _atomic_write_json(cache_file, {"_ts": now, "league": ninja_league})
            return ninja_league

    if cached_league:
        return cached_league

    resolved: str | None = None
    got_http_200 = False
    last_exc: Exception | None = None
    try:
        with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as client:
            resp = client.get(POE_LEAGUES_URL)
            if resp.status_code == 200:
                got_http_200 = True
                resolved = "Standard"
                leagues = resp.json()
                for lg in leagues:
                    lid = str(lg.get("id") or "").strip()
                    if lid and lid not in ("Standard", "Hardcore", "Solo Self-Found", "Hardcore SSF", "Ruthless"):
                        if not any(tag in lid for tag in ("Hardcore", "SSF", "Ruthless")):
                            resolved = lid
                            break
    except Exception as exc:
        last_exc = exc

    if got_http_200 and resolved:
        _atomic_write_json(cache_file, {"_ts": now, "league": resolved})
        return resolved

    stale_lg, _ = read_stale_cache(cache_file, data_key="league")
    if isinstance(stale_lg, str) and stale_lg.strip():
        return stale_lg.strip()

    if last_exc is not None:
        raise RuntimeError(f"Failed to resolve current PoE trade league: {last_exc}") from last_exc
    raise RuntimeError("Failed to resolve current PoE trade league (no HTTP 200 response).")


def _fetch_dense_overviews(
    league: str,
    ttl_sec: int = 900,
    *,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, list[dict[str, Any]]]:
    safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", league)
    cache_file = _shared_cache_dir() / f"ninja_dense_{safe_key}.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                return cached["data"]
        except Exception:
            pass

    type_to_lines: dict[str, list[dict[str, Any]]] = {}
    try:
        if client is not None:
            resp = client.get(NINJA_DENSE_URL, params={"league": league, "language": "en"})
            resp.raise_for_status()
            data = resp.json()
        else:
            with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as own_client:
                resp = own_client.get(NINJA_DENSE_URL, params={"league": league, "language": "en"})
                resp.raise_for_status()
                data = resp.json()
        for section in ("itemOverviews", "currencyOverviews"):
            for ov in data.get(section, []):
                ov_type = ov.get("type")
                if ov_type:
                    type_to_lines[str(ov_type)] = ov.get("lines", [])
    except (httpx.HTTPError, OSError):
        stale_data, _ = read_stale_cache(cache_file, data_key="data")
        if isinstance(stale_data, dict):
            _LAST_STALE.value = True
            return stale_data
        raise

    _atomic_write_json(cache_file, {"_ts": now, "data": type_to_lines})
    return type_to_lines


def fetch_currency_chaos_rates(
    league: str,
    *,
    proxy: str | None = None,
) -> dict[str, float]:
    """Return poe.ninja Currency name -> chaos value map (15m dense-overview cache)."""
    rates: dict[str, float] = {}
    for line in _pkg()._fetch_dense_overviews(league, proxy=proxy).get("Currency") or []:
        name = line.get("name")
        chaos = line.get("chaos")
        if name and chaos:
            rates.setdefault(str(name), float(chaos))
    return rates


def _stash_cache_path(league: str, item_type: str) -> Path:
    safe_league = re.sub(r"[^A-Za-z0-9_-]", "_", league)
    safe_type = re.sub(r"[^A-Za-z0-9_-]", "_", item_type)
    return _shared_cache_dir() / f"ninja_stash_{safe_league}_{safe_type}.json"


def _read_fresh_stash_cache(cache_file: Path, ttl_sec: int, now: float) -> list[dict[str, Any]] | None:
    if not cache_file.exists():
        return None
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if now - cached.get("_ts", 0) < ttl_sec and "lines" in cached:
            return cached["lines"]
    except Exception:
        pass
    return None


def _fetch_stash_overview(
    league: str,
    item_type: str,
    ttl_sec: int = 900,
    *,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Fetch detailed poe.ninja stash overview for a single item/currency type (15m disk cache)."""
    cache_file = _stash_cache_path(league, item_type)
    now = time.time()
    fresh = _read_fresh_stash_cache(cache_file, ttl_sec, now)
    if fresh is not None:
        return fresh

    def _do_fetch(http_client: httpx.Client) -> list[dict[str, Any]]:
        if item_type in CURRENCY_TYPES:
            resp = http_client.get(NINJA_STASH_CURRENCY_URL, params={"league": league, "type": item_type})
            resp.raise_for_status()
            payload = resp.json()
            out: list[dict[str, Any]] = []
            for row in payload.get("lines", []) or []:
                recv = row.get("receive") or {}
                spark = row.get("receiveSparkLine") or {}
                out.append(
                    {
                        "name": row.get("currencyTypeName") or row.get("name"),
                        "chaosValue": row.get("chaosEquivalent") or recv.get("value") or 0.0,
                        "listingCount": recv.get("listing_count") or recv.get("count"),
                        "count": recv.get("count"),
                        "sparkLine": spark,
                        "detailsId": row.get("detailsId"),
                    }
                )
            return out
        resp = http_client.get(NINJA_STASH_ITEM_URL, params={"league": league, "type": item_type})
        resp.raise_for_status()
        payload = resp.json()
        return payload.get("lines", []) or []

    try:
        if client is not None:
            lines = _do_fetch(client)
        else:
            with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as own_client:
                lines = _do_fetch(own_client)
    except (httpx.HTTPError, OSError):
        stale_lines, _ = read_stale_cache(cache_file, data_key="lines")
        if isinstance(stale_lines, list):
            _LAST_STALE.value = True
            return stale_lines
        raise

    _atomic_write_json(cache_file, {"_ts": now, "lines": lines})
    return lines


def _fetch_stash_overviews_batch(
    league: str,
    item_types: tuple[str, ...] | list[str],
    *,
    ttl_sec: int = 900,
    proxy: str | None = None,
    max_workers: int = 6,
) -> tuple[dict[str, list[dict[str, Any]]], set[str], set[str]]:
    """Fetch multiple poe.ninja stash categories concurrently using ONE shared httpx.Client.

    Returns ``(results_by_type, stale_types, failed_types)``; per-type HTTP/OS failures are
    recorded in ``failed_types`` instead of aborting sibling queries in the batch.
    """
    now = time.time()
    results: dict[str, list[dict[str, Any]]] = {}
    stale_types: set[str] = set()
    failed_types: set[str] = set()
    missing: list[str] = []
    seen: set[str] = set()
    for ov_type in item_types:
        if ov_type in seen:
            continue
        seen.add(ov_type)
        fresh = _read_fresh_stash_cache(_stash_cache_path(league, ov_type), ttl_sec, now)
        if fresh is not None:
            results[ov_type] = fresh
        else:
            missing.append(ov_type)

    if not missing:
        return results, stale_types, failed_types

    with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as shared_client:
        def _worker(ov_type: str) -> tuple[str, list[dict[str, Any]] | None, bool, bool]:
            _LAST_STALE.value = False
            try:
                lines = _fetch_stash_overview(
                    league, ov_type, ttl_sec=ttl_sec, proxy=proxy, client=shared_client
                )
                return ov_type, lines, bool(getattr(_LAST_STALE, "value", False)), False
            except (httpx.HTTPError, OSError):
                return ov_type, None, False, True

        if len(missing) == 1:
            ov_type, lines, was_stale, failed = _worker(missing[0])
            if failed or lines is None:
                failed_types.add(ov_type)
            else:
                results[ov_type] = lines
                if was_stale:
                    stale_types.add(ov_type)
        else:
            workers = max(1, min(int(max_workers), len(missing)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                for ov_type, lines, was_stale, failed in pool.map(_worker, missing):
                    if failed or lines is None:
                        failed_types.add(ov_type)
                    else:
                        results[ov_type] = lines
                        if was_stale:
                            stale_types.add(ov_type)
    return results, stale_types, failed_types
