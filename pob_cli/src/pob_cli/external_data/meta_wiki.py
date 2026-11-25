"""poe.ninja build-index-state meta snapshot and poewiki.net MediaWiki API queries."""

from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import re
import sys
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


def _pkg() -> Any:
    return sys.modules["pob_cli.external_data"]


def query_meta(
    *,
    league: str | None = None,
    skill: str | None = None,
    class_name: str | None = None,
    top: int = 20,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Query poe.ninja build-index-state meta snapshot (total builds, ascendancies, top skills)."""
    cache_file = _shared_cache_dir() / "poe_ninja_build_index_state.json"
    is_stale = False
    pre_ts: float | None = None
    if cache_file.exists():
        try:
            pre_ts = float(json.loads(cache_file.read_text(encoding="utf-8")).get("_ts", 0))
        except Exception:
            pre_ts = None

    fetch_build_state = _pkg()._fetch_poe_ninja_build_index_state
    try:
        data = (
            fetch_build_state(ttl_sec=900, proxy=proxy)
            if proxy is not None
            else fetch_build_state(ttl_sec=900)
        )
        # Detect if importers._fetch_poe_ninja_build_index_state silently fell back to an expired cache
        if pre_ts is not None and (time.time() - pre_ts) >= 900 and cache_file.exists():
            try:
                post_ts = float(json.loads(cache_file.read_text(encoding="utf-8")).get("_ts", 0))
                if post_ts == pre_ts:
                    is_stale = True
            except Exception:
                pass
    except (httpx.HTTPError, OSError):
        stale_data, _ = read_stale_cache(cache_file, data_key="data")
        if isinstance(stale_data, dict):
            data = stale_data
            is_stale = True
        else:
            raise
    league_builds = data.get("leagueBuilds", []) or []
    if not league_builds:
        raise RuntimeError("poe.ninja build-index-state returned no leagueBuilds entries.")

    target_lb: dict[str, Any] | None = None
    if league and league.strip():
        needle = league.strip().lower()
        for lb in league_builds:
            if (
                str(lb.get("leagueName") or "").lower() == needle
                or str(lb.get("leagueUrl") or "").lower() == needle
            ):
                target_lb = lb
                break
        if target_lb is None:
            sample_names = ", ".join(str(x.get("leagueName")) for x in league_builds[:8])
            raise ValueError(f"League '{league}' not found in poe.ninja build-index-state (e.g. {sample_names}).")
    else:
        for lb in league_builds:
            if not lb.get("hardcore") and lb.get("category") == 0:
                target_lb = lb
                break
        if target_lb is None:
            target_lb = league_builds[0]

    total_builds = int(target_lb.get("total") or 0)
    stats = target_lb.get("statistics", []) or []

    skill_q = skill.strip().lower() if skill and skill.strip() else None
    class_q = class_name.strip().lower() if class_name and class_name.strip() else None

    asc_map: dict[str, dict[str, Any]] = {}
    skill_map: dict[str, dict[str, Any]] = {}
    combinations: list[dict[str, Any]] = []

    for row in stats:
        cls = str(row.get("class") or "")
        sk = str(row.get("skill") or "")
        pct = float(row.get("percentage") or 0.0)
        trend = row.get("trend")

        if class_q and class_q not in cls.lower():
            continue
        if skill_q and skill_q not in sk.lower():
            continue

        est_builds = round(total_builds * pct / 100.0)
        combinations.append(
            {
                "class": cls,
                "skill": sk,
                "percentage": round(pct, 2),
                "estimatedBuilds": est_builds,
                "trend": trend,
            }
        )

        if cls:
            a = asc_map.setdefault(cls, {"class": cls, "percentage": 0.0, "estimatedBuilds": 0, "skills": []})
            a["percentage"] += pct
            a["estimatedBuilds"] += est_builds
            if sk and sk not in a["skills"]:
                a["skills"].append(sk)

        if sk:
            s = skill_map.setdefault(sk, {"skill": sk, "percentage": 0.0, "estimatedBuilds": 0, "classes": []})
            s["percentage"] += pct
            s["estimatedBuilds"] += est_builds
            if cls and cls not in s["classes"]:
                s["classes"].append(cls)

    ascendancies = sorted(
        (
            {
                **v,
                "percentage": round(v["percentage"], 2),
            }
            for v in asc_map.values()
        ),
        key=lambda x: x["percentage"],
        reverse=True,
    )[:top]

    top_skills = sorted(
        (
            {
                **v,
                "percentage": round(v["percentage"], 2),
            }
            for v in skill_map.values()
        ),
        key=lambda x: x["percentage"],
        reverse=True,
    )[:top]

    combinations.sort(key=lambda x: x["percentage"], reverse=True)

    out: dict[str, Any] = {
        "league": target_lb.get("leagueName"),
        "leagueUrl": target_lb.get("leagueUrl"),
        "totalBuilds": total_builds,
        "hardcore": bool(target_lb.get("hardcore", False)),
        "filters": {"skill": skill, "className": class_name},
        "ascendancies": ascendancies,
        "topSkills": top_skills,
        "combinations": combinations[:top],
        "availableLeagues": [x.get("leagueName") for x in league_builds[:12] if x.get("leagueName")],
    }
    if is_stale:
        out["stale"] = True
    return out


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_HEADING_RE = re.compile(r"<h([2-4])\b[^>]*>(.*?)</h\1>", flags=re.DOTALL | re.IGNORECASE)
_EDIT_SECTION_RE = re.compile(r"<span[^>]*mw-editsection[^>]*>.*?</span>", flags=re.DOTALL | re.IGNORECASE)
_PRIORITY_TIER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:skill\s+functions?\s+and\s+interactions?|mechanics?|interactions?)", re.IGNORECASE),
    re.compile(r"(?:trigger|damage|modifiers?)", re.IGNORECASE),
    re.compile(r"version\s+history", re.IGNORECASE),
)


def _strip_html(raw_html: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    txt = re.sub(r"</?(br|p|li|tr|h[1-6]|div)[^>]*>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"</?(td|th)[^>]*>", " | ", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.splitlines()]
    cleaned = [ln for ln in lines if ln]
    return "\n".join(cleaned)


_POEDB_CN_SUFFIX_RE = re.compile(r"(?:[（(]辅[）)]|辅助|輔助)\s*$")
_POEDB_INDEX_MEM: dict[str, Any] = {"path": "", "mtime": -1.0, "exact": {}, "stripped": {}}


def _lookup_poedb_local_en(candidates: tuple[str, ...]) -> str | None:
    """Zero-hallucination exact CN->US lookup from local cached `poedb_autocomplete_index.json`."""
    try:
        cache_file = _shared_cache_dir() / "poedb_autocomplete_index.json"
        if not cache_file.is_file():
            return None
        mtime = cache_file.stat().st_mtime
        p_str = str(cache_file)
        if _POEDB_INDEX_MEM["path"] != p_str or _POEDB_INDEX_MEM["mtime"] != mtime:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            exact: dict[str, str] = {}
            stripped: dict[str, str] = {}
            for ent in raw.get("entries") or ():
                if not isinstance(ent, dict):
                    continue
                cn = str(ent.get("cnLabel") or "").strip()
                en = str(ent.get("usLabel") or "").strip() or str(ent.get("slug") or "").replace("_", " ").strip()
                if not cn or not en or _CJK_RE.search(en):
                    continue
                exact.setdefault(cn, en)
                base = _POEDB_CN_SUFFIX_RE.sub("", cn).strip()
                if base and base != cn:
                    stripped.setdefault(base, en)
            _POEDB_INDEX_MEM.update({"path": p_str, "mtime": mtime, "exact": exact, "stripped": stripped})
    except Exception:
        return None

    exact_map: dict[str, str] = _POEDB_INDEX_MEM["exact"]
    stripped_map: dict[str, str] = _POEDB_INDEX_MEM["stripped"]
    for cand in candidates:
        if cand in exact_map:
            return exact_map[cand]
        base = _POEDB_CN_SUFFIX_RE.sub("", cand).strip()
        if base in exact_map:
            return exact_map[base]
        if base in stripped_map:
            return stripped_map[base]
    return None


def _resolve_wiki_english_query(topic: str) -> str | None:
    """Resolve CJK PoE1 entity to exact English title via local PoEDB index first, then exact PoB zh-rCN dict."""
    q = topic.strip()
    if not q or not _CJK_RE.search(q):
        return None

    q_tw = q
    try:
        from ..game_data.tw_norm import normalize_tw_query

        q_tw = normalize_tw_query(q) or q
    except Exception:
        pass
    cands = (q, q_tw) if q_tw != q else (q,)

    # 1. Local cached PoEDB CN<->US official slug index (0ms, exact, zero hallucination)
    poedb_hit = _lookup_poedb_local_en(cands)
    if poedb_hit:
        return poedb_hit

    # 2. Exact-only fallback via local PoB slang / zh-rCN official dictionary (no fuzzy guessing)
    try:
        from ..game_data.queries import query_local_data
        from ..game_data.slang import resolve_player_slang
    except Exception:
        return None

    for cand in cands:
        try:
            slang_hit = resolve_player_slang(cand)
            if slang_hit and slang_hit[0] and not _CJK_RE.search(slang_hit[0]):
                return slang_hit[0].strip()
        except Exception:
            pass

    for cand in cands:
        try:
            tr = query_local_data("translate", cand, limit=3)
            for r in tr.get("results") or ():
                if isinstance(r, dict) and r.get("match_type") == "exact":
                    en_val = str(r.get("en") or "").strip()
                    if en_val and not _CJK_RE.search(en_val):
                        return en_val
        except Exception:
            pass

    for kind in ("gem", "unique"):
        try:
            res = query_local_data(kind, q, limit=3)
            for r in res.get("results") or ():
                if not isinstance(r, dict):
                    continue
                zh_val = str(r.get("zh_name") or "").strip()
                if zh_val and zh_val not in cands and _POEDB_CN_SUFFIX_RE.sub("", zh_val).strip() not in cands:
                    continue
                en_val = str(r.get("display_name") or r.get("name") or "").strip()
                if en_val and not _CJK_RE.search(en_val):
                    return en_val
        except Exception:
            pass
    return None


def _priority_tier(heading: str) -> int | None:
    for idx, pat in enumerate(_PRIORITY_TIER_RES):
        if pat.search(heading):
            return idx
    return None


def _extract_wiki_sections_and_summary(
    raw_html: str, *, max_len: int = 12000, lead_cap: int | None = None
) -> tuple[str, list[dict[str, str]]]:
    """Extract mechanics/interaction sections and assemble a prioritized summary (`max_len <= 0` = unlimited)."""
    unlimited = max_len <= 0
    matches = list(_HEADING_RE.finditer(raw_html))
    if not matches:
        plain = _strip_html(raw_html)
        return (plain if unlimited else plain[:max_len]), []

    lead_text = _strip_html(raw_html[: matches[0].start()]).strip()
    priority_entries: list[tuple[int, int, str, str]] = []
    other_entries: list[tuple[str, str]] = []

    for idx, m in enumerate(matches):
        raw_heading = _EDIT_SECTION_RE.sub("", m.group(2))
        heading = re.sub(r"\s*\[edit[^\]]*\]\s*$", "", _strip_html(raw_heading), flags=re.IGNORECASE).strip()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw_html)
        body_text = _strip_html(raw_html[m.end() : body_end]).strip()
        if not heading or not body_text:
            continue
        tier = _priority_tier(heading)
        if tier is not None:
            priority_entries.append((tier, idx, heading, body_text))
        else:
            other_entries.append((heading, body_text))

    if not priority_entries:
        plain = _strip_html(raw_html)
        return (plain if unlimited else plain[:max_len]), []

    priority_entries.sort(key=lambda item: (item[0], item[1]))
    eff_lead_cap = lead_cap if lead_cap is not None else (0 if unlimited else max(900, (max_len * 900) // 3500))
    lead_shown = lead_text if (unlimited and lead_cap is None) else lead_text[:eff_lead_cap]
    if unlimited:
        mechanics_sections = [
            {"heading": heading, "text": body_text}
            for _, _, heading, body_text in priority_entries
        ]
    else:
        sec_cap = max(1800, (max_len * 1800) // 3500)
        max_secs = max(6, (max_len * 6) // 3500)
        # The sections are printed in full under the lead paragraph, so lead and sections together
        # stay within `max_len`: the per-section caps alone allow ten times that.
        mechanics_sections = []
        budget = max_len - len(lead_shown)
        for _, _, heading, body_text in priority_entries[:max_secs]:
            if budget <= 0:
                break
            text = body_text[: min(sec_cap, budget)]
            mechanics_sections.append({"heading": heading, "text": text})
            budget -= len(text)

    blocks: list[str] = []
    if lead_shown:
        blocks.append(lead_shown)
    for sec in mechanics_sections:
        blocks.append(f"## {sec['heading']}\n{sec['text']}")
    for heading, body_text in other_entries:
        if not unlimited and sum(len(b) + 2 for b in blocks) >= max_len:
            break
        blocks.append(f"## {heading}\n{body_text}")

    joined = "\n\n".join(blocks)
    return (joined if unlimited else joined[:max_len]), mechanics_sections


def _search_poewiki_titles(http_client: httpx.Client, api_url: str, term: str) -> tuple[list[str], list[str]]:
    s_resp = http_client.get(
        api_url,
        params={
            "action": "opensearch",
            "search": term,
            "limit": 5,
            "namespace": 0,
            "format": "json",
        },
    )
    s_resp.raise_for_status()
    s_json = s_resp.json()
    titles: list[str] = s_json[1] if len(s_json) > 1 else []
    urls: list[str] = s_json[3] if len(s_json) > 3 else []

    if not titles:
        q_resp = http_client.get(
            api_url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": term,
                "srlimit": 5,
                "format": "json",
            },
        )
        q_resp.raise_for_status()
        s_hits = q_resp.json().get("query", {}).get("search", [])
        titles = [h["title"] for h in s_hits]
        urls = [f"https://www.poewiki.net/wiki/{t.replace(' ', '_')}" for t in titles]
    return titles, urls


def _wiki_cache_path(q: str, max_chars: int) -> Any:
    slug_hash = hashlib.sha1(q.lower().encode("utf-8")).hexdigest()[:12]
    suffix = "" if max_chars == 12000 else f"_{max_chars}"
    return _shared_cache_dir() / f"poewiki_{slug_hash}{suffix}.json"


def query_wiki(
    topic: str,
    *,
    ttl_sec: int = 86400,
    proxy: str | None = None,
    client: httpx.Client | None = None,
    max_chars: int = 12000,
) -> dict[str, Any]:
    """Query poewiki.net MediaWiki API for mechanics/uniques/keystones with 24h local disk cache."""
    from ..guardrails import GuardrailScope, check_input, validate_url

    q = topic.strip()
    if not q:
        raise ValueError("Wiki topic cannot be empty.")
    validate_url(q, expected_game="poe1", cli_subcommand="wiki")
    check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")

    cache_file = _wiki_cache_path(q, max_chars)
    now = time.time()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                return cached["data"]
        except Exception:
            pass

    api_url = "https://www.poewiki.net/w/api.php"

    def _fetch_with_client(http_client: httpx.Client) -> dict[str, Any]:
        resolved_en = _resolve_wiki_english_query(q)
        candidates = [resolved_en, q] if (resolved_en and resolved_en.lower() != q.lower()) else [q]
        titles: list[str] = []
        urls: list[str] = []
        used_query = q
        for cand in candidates:
            titles, urls = _search_poewiki_titles(http_client, api_url, cand)
            if titles:
                used_query = cand
                break

        if not titles:
            raise ValueError(f"No poewiki.net page found for '{topic}'.")

        page_title = titles[0]
        page_url = urls[0] if urls else f"https://www.poewiki.net/wiki/{page_title.replace(' ', '_')}"

        p_resp = http_client.get(
            api_url,
            params={
                "action": "parse",
                "page": page_title,
                "prop": "text|sections",
                "redirects": 1,
                "format": "json",
            },
        )
        p_resp.raise_for_status()
        p_data = p_resp.json().get("parse", {})
        resolved_title = p_data.get("title") or page_title
        raw_html = p_data.get("text", {}).get("*", "")
        summary_text, mechanics_sections = _extract_wiki_sections_and_summary(raw_html, max_len=max_chars)

        out: dict[str, Any] = {
            "query": topic,
            "title": resolved_title,
            "url": page_url,
            "relatedTitles": titles[1:5],
            "summary": summary_text,
        }
        if resolved_en and resolved_en.lower() != q.lower() and used_query == resolved_en:
            out["resolvedQuery"] = resolved_en
        if mechanics_sections:
            out["mechanicsSections"] = mechanics_sections
        return out

    try:
        if client is not None:
            result = _fetch_with_client(client)
        else:
            with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as own_client:
                result = _fetch_with_client(own_client)
    except (httpx.HTTPError, OSError):
        stale_data, _ = read_stale_cache(cache_file, data_key="data")
        if isinstance(stale_data, dict):
            return {**stale_data, "stale": True}
        raise

    _atomic_write_json(cache_file, {"_ts": now, "data": result})
    return result


def query_wiki_multi(
    queries: list[str],
    *,
    ttl_sec: int = 86400,
    proxy: str | None = None,
    max_workers: int = 4,
    max_chars: int = 12000,
) -> dict[str, Any]:
    """Query multiple poewiki.net topics concurrently using ONE shared httpx.Client."""
    from ..compat import call_compat
    from ..guardrails import GuardrailScope, check_input, validate_url

    clean_queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not clean_queries:
        raise ValueError("Wiki queries cannot be empty.")
    for q_item in clean_queries:
        validate_url(q_item, expected_game="poe1", cli_subcommand="wiki")
        check_input(q_item, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")

    now = time.time()
    cached_hits: dict[str, dict[str, Any]] = {}
    uncached: list[str] = []
    for q in clean_queries:
        cache_file = _wiki_cache_path(q, max_chars)
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if now - cached.get("_ts", 0) < ttl_sec and "data" in cached:
                    cached_hits[q] = cached["data"]
                    continue
            except Exception:
                pass
        if q not in uncached:
            uncached.append(q)

    fetched: dict[str, dict[str, Any]] = {}
    wiki_fn = _pkg().query_wiki
    if uncached:
        with _make_httpx_client(timeout=DEFAULT_HTTP_TIMEOUT, proxy=proxy) as shared_client:
            def _run_one(topic: str) -> tuple[str, dict[str, Any]]:
                try:
                    return topic, call_compat(
                        wiki_fn, topic, ttl_sec=ttl_sec, proxy=proxy, client=shared_client, max_chars=max_chars
                    )
                except Exception as exc:
                    return topic, {"query": topic, "error": str(exc)}

            if len(uncached) == 1:
                t, res = _run_one(uncached[0])
                fetched[t] = res
            else:
                workers = max(1, min(int(max_workers), len(uncached)))
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    for t, res in pool.map(_run_one, uncached):
                        fetched[t] = res

    results = [cached_hits[q] if q in cached_hits else fetched[q] for q in clean_queries]
    out: dict[str, Any] = {
        "queries": clean_queries,
        "count": len(results),
        "results": results,
    }
    if any(bool(r.get("stale")) for r in results):
        out["stale"] = True
    return out
