"""PoEDB HTTP client, CN/US autocomplete index, page cache, and entity query entrypoints."""

from __future__ import annotations

import concurrent.futures
import difflib
import hashlib
import json
import re
import threading
import time
from typing import Any

import httpx

from ..importers import (
    DEFAULT_HTTP_TIMEOUT,
    _atomic_write_json,
    _make_httpx_client,
    _shared_cache_dir,
    read_stale_cache,
)
from .html_parser import (
    _SKIP_CARD_KEYS,
    _derive_mod_pool_candidates,
    _extract_inline_mod_cards,
    _extract_mods_from_modsview,
    _extract_mods_view_json,
    _mod_matches_filter,
    _PoedbPageParser,
)

_LAST_STALE = threading.local()

POEDB_BASE_URL = "https://poedb.tw"
POEDB_CDN_URL = "https://cdn.poedb.tw"

# Community/legacy CN aliases that differ from poedb.tw current CN client strings
COMMUNITY_ALIASES: dict[str, str] = {
    "傲视头冠": "Archdemon_Crown",
}


class _LazyClient:
    """Thread-safe lazy httpx.Client holder so warm cache hits create zero HTTP clients."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._proxy = proxy
        self._timeout = DEFAULT_HTTP_TIMEOUT if timeout is None else timeout
        self._external = client
        self._owned: httpx.Client | None = None
        self._lock = threading.Lock()

    def get_client(self) -> httpx.Client:
        if self._external is not None:
            return self._external
        with self._lock:
            if self._owned is None:
                self._owned = _make_httpx_client(timeout=self._timeout, proxy=self._proxy)
                self._owned.__enter__()
            return self._owned

    def __enter__(self) -> _LazyClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        owned = self._owned
        self._owned = None
        if owned is not None:
            owned.__exit__(*exc)


def _poedb_get(
    url: str,
    *,
    proxy: str | None = None,
    timeout: float | httpx.Timeout | None = None,
    client: httpx.Client | _LazyClient | None = None,
) -> str:
    resolved_timeout = DEFAULT_HTTP_TIMEOUT if timeout is None else timeout
    try:
        if isinstance(client, _LazyClient):
            resp = client.get_client().get(url)
            resp.raise_for_status()
            return resp.text
        if client is not None:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
        with _make_httpx_client(timeout=resolved_timeout, proxy=proxy) as own_client:
            resp = own_client.get(url)
            resp.raise_for_status()
            return resp.text
    except (httpx.HTTPError, OSError) as exc:
        raise RuntimeError(
            f"PoEDB request failed for '{url}': {exc}. "
            "Check network access or set POB_PROXY / HTTPS_PROXY / --proxy."
        ) from exc


def _discover_autocomplete_urls(
    lang: str = "cn",
    *,
    proxy: str | None = None,
    client: httpx.Client | _LazyClient | None = None,
) -> tuple[str, str]:
    """Parse poedb.tw header script to obtain hashed autocomplete_cn and autocomplete_us URLs."""
    home_html = _poedb_get(f"{POEDB_BASE_URL}/{lang}/", proxy=proxy, client=client)

    cn_m = re.search(r"(https?://cdn\.poedb\.tw/json/)?(autocomplete_cn\.[A-Za-z0-9_-]+\.json)", home_html)
    us_m = re.search(r"(https?://cdn\.poedb\.tw/json/)?(autocomplete_us\.[A-Za-z0-9_-]+\.json)", home_html)
    if cn_m and us_m:
        return f"{POEDB_CDN_URL}/json/{cn_m.group(2)}", f"{POEDB_CDN_URL}/json/{us_m.group(2)}"

    hdr_m = re.search(r'(https?://cdn\.poedb\.tw/js/poedb_header\.[A-Za-z0-9_-]+\.js)', home_html)
    if not hdr_m:
        rel_m = re.search(r'src=["\']([^"\']*poedb_header[^"\']+\.js)["\']', home_html)
        if not rel_m:
            raise RuntimeError("Could not locate poedb_header.<hash>.js on poedb.tw homepage.")
        hdr_url = rel_m.group(1)
        if not hdr_url.startswith("http"):
            hdr_url = f"{POEDB_BASE_URL}/{hdr_url.lstrip('/')}"
    else:
        hdr_url = hdr_m.group(1)

    hdr_js = _poedb_get(hdr_url, proxy=proxy, client=client)
    cn_js_m = re.search(r"(autocomplete_cn\.[A-Za-z0-9_-]+\.json)", hdr_js)
    us_js_m = re.search(r"(autocomplete_us\.[A-Za-z0-9_-]+\.json)", hdr_js)
    if not cn_js_m or not us_js_m:
        raise RuntimeError(f"Could not extract autocomplete_cn/us hashed JSON filenames from {hdr_url}.")

    return f"{POEDB_CDN_URL}/json/{cn_js_m.group(1)}", f"{POEDB_CDN_URL}/json/{us_js_m.group(1)}"


def _load_autocomplete_index(
    *,
    lang: str = "cn",
    proxy: str | None = None,
    ttl_sec: int = 86400,
    client: httpx.Client | _LazyClient | None = None,
) -> list[dict[str, str]]:
    """Load merged CN + US PoEDB autocomplete index with 24h local disk cache."""
    cache_file = _shared_cache_dir() / "poedb_autocomplete_index.json"
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and isinstance(cached.get("entries"), list):
                return cached["entries"]
        except Exception:
            pass

    def _fetch_json(http_client: httpx.Client, target_url: str) -> Any:
        resp = http_client.get(target_url)
        resp.raise_for_status()
        return resp.json()

    try:
        with _LazyClient(proxy=proxy, client=client if isinstance(client, httpx.Client) else None) as own_holder:
            active_holder: httpx.Client | _LazyClient = client if client is not None else own_holder
            cn_url, us_url = _discover_autocomplete_urls(lang=lang, proxy=proxy, client=active_holder)
            http_client = active_holder.get_client() if isinstance(active_holder, _LazyClient) else active_holder
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_cn = pool.submit(_fetch_json, http_client, cn_url)
                fut_us = pool.submit(_fetch_json, http_client, us_url)
                cn_raw = fut_cn.result()
                us_raw = fut_us.result()
    except (httpx.HTTPError, OSError, RuntimeError) as exc:
        stale_entries, _ = read_stale_cache(cache_file, data_key="entries")
        if isinstance(stale_entries, list):
            _LAST_STALE.value = True
            return stale_entries
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            f"Failed to download PoEDB autocomplete index: {exc}. "
            "Check network access or set POB_PROXY / HTTPS_PROXY / --proxy."
        ) from exc

    by_slug: dict[str, dict[str, str]] = {}
    for row in cn_raw if isinstance(cn_raw, list) else []:
        slug = str(row.get("value") or "").strip().lstrip("/")
        if not slug:
            continue
        item = by_slug.setdefault(slug, {"slug": slug, "cnLabel": "", "usLabel": "", "cnDesc": "", "usDesc": ""})
        lbl = str(row.get("label") or "").strip()
        if lbl and not item["cnLabel"]:
            item["cnLabel"] = lbl
        desc = str(row.get("desc") or "").strip()
        if desc and not item["cnDesc"]:
            item["cnDesc"] = desc

    for row in us_raw if isinstance(us_raw, list) else []:
        slug = str(row.get("value") or "").strip().lstrip("/")
        if not slug:
            continue
        item = by_slug.setdefault(slug, {"slug": slug, "cnLabel": "", "usLabel": "", "cnDesc": "", "usDesc": ""})
        lbl = str(row.get("label") or "").strip()
        if lbl and not item["usLabel"]:
            item["usLabel"] = lbl
        desc = str(row.get("desc") or "").strip()
        if desc and not item["usDesc"]:
            item["usDesc"] = desc

    entries = list(by_slug.values())
    _atomic_write_json(
        cache_file,
        {"_ts": now, "cnUrl": cn_url, "usUrl": us_url, "entries": entries},
        ensure_ascii=False,
    )
    return entries


def _resolve_poedb_slug(
    query: str,
    entries: list[dict[str, str]],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Resolve user query (CN, EN, or direct slug) to best PoEDB entry + close matches."""
    q_raw = query.strip()
    q_low = q_raw.lower()
    q_space = q_low.replace("_", " ")

    alias_slug = COMMUNITY_ALIASES.get(q_raw)
    if alias_slug:
        for ent in entries:
            if ent["slug"].lower() == alias_slug.lower():
                return ent, [ent]
        return {"slug": alias_slug, "cnLabel": q_raw, "usLabel": alias_slug.replace("_", " "), "cnDesc": "", "usDesc": ""}, []

    scored: list[tuple[float, dict[str, str]]] = []
    for ent in entries:
        slug = ent["slug"]
        slug_low = slug.lower()
        slug_space = slug_low.replace("_", " ")
        cn_low = ent.get("cnLabel", "").lower()
        us_low = ent.get("usLabel", "").lower()

        if q_low in (cn_low, us_low, slug_low) or q_space == slug_space:
            score = 100.0
        elif any(cand.startswith(q_low) for cand in (cn_low, us_low, slug_low, slug_space) if cand):
            score = 80.0 - min(len(cn_low or us_low or slug_low), 40) * 0.01
        elif any(q_low in cand or q_space in cand for cand in (cn_low, us_low, slug_space) if cand):
            score = 60.0 - min(len(cn_low or us_low or slug_low), 60) * 0.01
        else:
            continue
        scored.append((score, ent))

    if not scored:
        # Fallback: direct slug syntax like `Helmets_str_int`
        if re.fullmatch(r"[A-Za-z0-9_]+", q_raw) and "_" in q_raw:
            synthetic = {"slug": q_raw, "cnLabel": "", "usLabel": q_raw.replace("_", " "), "cnDesc": "", "usDesc": ""}
            return synthetic, []

        # Fuzzy similarity fallback across CN & US labels
        for ent in entries:
            best_ratio = 0.0
            for cand in (ent.get("cnLabel", ""), ent.get("usLabel", ""), ent["slug"].replace("_", " ")):
                if cand:
                    r = difflib.SequenceMatcher(None, q_low, cand.lower()).ratio()
                    if r > best_ratio:
                        best_ratio = r
            if best_ratio >= 0.55:
                scored.append((best_ratio * 50.0, ent))

    if not scored:
        raise ValueError(f"No PoEDB entity matched '{query}'.")

    scored.sort(key=lambda x: x[0], reverse=True)
    top_entries = [item[1] for item in scored[:6]]
    return top_entries[0], top_entries


def _fetch_poedb_page(
    slug: str,
    *,
    lang: str = "cn",
    proxy: str | None = None,
    ttl_sec: int = 43200,
    client: httpx.Client | _LazyClient | None = None,
) -> tuple[str, str]:
    """Fetch a PoEDB HTML page with 12h disk cache and stale fallback. Returns `(url, html)`."""
    norm_lang = "us" if lang.strip().lower() in ("us", "en") else "cn"
    clean_slug = slug.strip().lstrip("/")
    url = f"{POEDB_BASE_URL}/{norm_lang}/{clean_slug}"
    slug_hash = hashlib.sha1(f"{norm_lang}:{clean_slug}".encode()).hexdigest()[:16]
    cache_file = _shared_cache_dir() / f"poedb_page_{norm_lang}_{slug_hash}.json"
    now = time.time()

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and isinstance(cached.get("html"), str):
                return url, cached["html"]
        except Exception:
            pass

    try:
        page_html = _poedb_get(url, proxy=proxy, client=client)
    except RuntimeError:
        stale_html, _ = read_stale_cache(cache_file, data_key="html")
        if isinstance(stale_html, str):
            _LAST_STALE.value = True
            return url, stale_html
        raise
    _atomic_write_json(
        cache_file,
        {"_ts": now, "url": url, "html": page_html},
        ensure_ascii=False,
    )
    return url, page_html


def _query_poedb_with_entries(
    query: str,
    entries: list[dict[str, str]],
    *,
    include_mods: bool = False,
    norm_lang: str = "cn",
    proxy: str | None = None,
    limit_mods: int = 80,
    mod_filter: str | None = None,
    client: httpx.Client | _LazyClient | None = None,
    index_stale: bool = False,
) -> dict[str, Any]:
    _LAST_STALE.value = bool(index_stale)
    q = query.strip()
    if not q:
        raise ValueError("PoEDB query cannot be empty.")

    primary, close_hits = _resolve_poedb_slug(q, entries)
    slug = primary["slug"]

    page_url, page_html = _fetch_poedb_page(slug, lang=norm_lang, proxy=proxy, client=client)
    parser = _PoedbPageParser()
    parser.feed(page_html)

    raw_title = parser.h1_text.strip() or parser.title_text.strip()
    clean_title = re.sub(r"\s*-\s*(?:流亡编年史|PoEDB|Path of Exile Wiki).*$", "", raw_title, flags=re.IGNORECASE).strip()
    if not clean_title:
        clean_title = (
            (primary.get("cnLabel") if norm_lang == "cn" else primary.get("usLabel"))
            or primary.get("usLabel")
            or primary.get("cnLabel")
            or slug.replace("_", " ")
        )

    props: list[str] = []
    seen_props: set[str] = set()
    for p in parser.properties:
        if p and p not in seen_props:
            seen_props.add(p)
            props.append(p)
    for k, v in parser.card_pairs:
        if k in _SKIP_CARD_KEYS:
            continue
        kv = f"{k}: {v}"
        if kv not in seen_props:
            seen_props.add(kv)
            props.append(kv)

    category = (
        (primary.get("cnDesc") if norm_lang == "cn" else primary.get("usDesc"))
        or primary.get("usDesc")
        or primary.get("cnDesc")
        or parser.class_href
        or ""
    )

    result: dict[str, Any] = {
        "query": query,
        "title": clean_title,
        "slug": slug,
        "lang": norm_lang,
        "url": page_url,
        "category": category or None,
        "matches": [
            {
                "slug": h["slug"],
                "label": h.get("cnLabel") or h.get("usLabel") or h["slug"],
                "labelEn": h.get("usLabel") or h["slug"].replace("_", " "),
                "category": (h.get("cnDesc") if norm_lang == "cn" else h.get("usDesc")) or h.get("usDesc") or None,
            }
            for h in close_hits[:5]
        ],
        "properties": props[:25],
        "implicits": parser.implicits,
        "explicits": parser.explicits,
        "description": " | ".join(parser.descriptions) if parser.descriptions else None,
        "versionHistory": parser.version_history[:25],
    }

    mv = _extract_mods_view_json(page_html)
    inline_cards = _extract_inline_mod_cards(page_html)
    if include_mods or mv is not None:
        mod_pool_slug: str | None = slug if mv is not None else None
        mod_pool_url: str | None = page_url if mv is not None else None
        if mv is None:
            for cand_slug in _derive_mod_pool_candidates(slug, parser):
                try:
                    mp_url, mp_html = _fetch_poedb_page(
                        cand_slug, lang=norm_lang, proxy=proxy, client=client
                    )
                except RuntimeError:
                    continue
                cand_mv = _extract_mods_view_json(mp_html)
                if cand_mv is not None:
                    mv = cand_mv
                    mod_pool_slug = cand_slug
                    mod_pool_url = mp_url
                    break

        if mv is not None:
            mods_list, total_mods, mod_summary = _extract_mods_from_modsview(
                mv, limit_mods=limit_mods, mod_filter=mod_filter
            )
            result["modPoolSlug"] = mod_pool_slug
            result["modPoolUrl"] = mod_pool_url
            result["modCountTotal"] = total_mods
            result["modPoolSummary"] = mod_summary
            if mod_filter and mod_filter.strip():
                result["modFilter"] = mod_filter.strip()
            result["mods"] = mods_list
        elif inline_cards:
            cards_filtered = (
                [c for c in inline_cards if _mod_matches_filter(c, mod_filter)]
                if (mod_filter and mod_filter.strip())
                else inline_cards
            )
            if mod_filter and mod_filter.strip():
                result["modFilter"] = mod_filter.strip()
            result["modCountTotal"] = len(cards_filtered)
            result["mods"] = cards_filtered[:limit_mods] if limit_mods > 0 else cards_filtered
        else:
            result["modCountTotal"] = 0
            result["mods"] = []

    if getattr(_LAST_STALE, "value", False):
        result["stale"] = True
    return result


def query_poedb(
    query: str,
    *,
    include_mods: bool = False,
    lang: str = "cn",
    proxy: str | None = None,
    limit_mods: int = 80,
    mod_filter: str | None = None,
) -> dict[str, Any]:
    """Search PoEDB autocomplete index (CN+US) and parse page + optional mod pool into compact JSON."""
    from ..guardrails import GuardrailScope, check_input, validate_url

    q = query.strip()
    if not q:
        raise ValueError("PoEDB query cannot be empty.")
    validate_url(q, expected_game="poe1", cli_subcommand="poedb")
    check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")

    norm_lang = "us" if lang.strip().lower() in ("us", "en") else "cn"
    _LAST_STALE.value = False
    with _LazyClient(proxy=proxy) as holder:
        entries = _load_autocomplete_index(lang=norm_lang, proxy=proxy, client=holder)
        index_stale = bool(getattr(_LAST_STALE, "value", False))
        return _query_poedb_with_entries(
            query,
            entries,
            include_mods=include_mods,
            norm_lang=norm_lang,
            proxy=proxy,
            limit_mods=limit_mods,
            mod_filter=mod_filter,
            client=holder,
            index_stale=index_stale,
        )


def query_poedb_multi(
    queries: list[str],
    *,
    include_mods: bool = False,
    lang: str = "cn",
    proxy: str | None = None,
    limit_mods: int = 80,
    mod_filter: str | None = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Query multiple PoEDB entities concurrently with a shared HTTP client and stale-cache fallback."""
    from ..guardrails import GuardrailScope, check_input, validate_url

    clean_queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not clean_queries:
        raise ValueError("PoEDB queries cannot be empty.")
    for q_item in clean_queries:
        validate_url(q_item, expected_game="poe1", cli_subcommand="poedb")
        check_input(q_item, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")

    norm_lang = "us" if lang.strip().lower() in ("us", "en") else "cn"
    _LAST_STALE.value = False
    with _LazyClient(proxy=proxy) as holder:
        entries = _load_autocomplete_index(lang=norm_lang, proxy=proxy, client=holder)
        index_stale = bool(getattr(_LAST_STALE, "value", False))

        def _run_one(q: str) -> dict[str, Any]:
            try:
                return _query_poedb_with_entries(
                    q,
                    entries,
                    include_mods=include_mods,
                    norm_lang=norm_lang,
                    proxy=proxy,
                    limit_mods=limit_mods,
                    mod_filter=mod_filter,
                    client=holder,
                    index_stale=index_stale,
                )
            except Exception as exc:
                return {"query": q, "error": str(exc)}

        if len(clean_queries) == 1:
            results = [_run_one(clean_queries[0])]
        else:
            workers = max(1, min(int(max_workers), len(clean_queries)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_run_one, clean_queries))

    out: dict[str, Any] = {
        "queries": clean_queries,
        "lang": norm_lang,
        "count": len(results),
        "results": results,
    }
    if index_stale or any(bool(r.get("stale")) for r in results):
        out["stale"] = True
    return out
