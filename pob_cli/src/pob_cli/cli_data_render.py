"""Markdown renderers for offline game data, PoEDB, poe.ninja prices, and PoE Wiki CLI outputs."""

from __future__ import annotations

from typing import Any


def _render_single_data_markdown(res: dict[str, Any], kind: str, query: str) -> str:
    from .formatters import format_game_data

    return format_game_data(res, kind=kind, query=query)


def _render_gem_quality_breakpoints_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    lines: list[str] = []
    for w in res.get("warnings") or []:
        if isinstance(w, dict):
            tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
            lines.append(f"> {tag} `{w.get('trigger', '')}`: {w.get('warning', '')}")
    if res.get("qualityRule"):
        lines.append(f"> **[PoE1 3.30 Gem Quality Rule]** {res['qualityRule']}")

    scope_lbl = "Active Skill Gems Only" if res.get("activeOnly") else "All Gems (Active + Support)"
    q_lbl = f" | Search: `{res['query']}`" if res.get("query") else ""
    lines.append(
        f"\n### PoE1 3.30 Gem Quality Breakpoints ({scope_lbl}{q_lbl}) — "
        f"showing `{res.get('count', 0)}` of `{res.get('totalMatched', 0)}` "
        f"(PoB `{res.get('pobVersion')}` / `{res.get('elapsedMs')}ms`)"
    )
    results = res.get("results") or []
    if not results:
        lines.append("_(no matching gem quality stats)_")
        return "\n".join(lines)

    rows: list[list[Any]] = []
    for idx, g in enumerate(results, start=1):
        name_en = str(g.get("name") or "-")
        name_zh = str(g.get("zhName") or "-")
        kind_s = "Support" if g.get("isSupport") else "Active"
        q_list = g.get("qualityStats") or []
        q0 = q_list[0] if q_list else {}
        stat_desc = "; ".join(str(q.get("label") or q.get("stat") or "") for q in q_list[:2])
        bp_note = "; ".join(str(q.get("breakpointNote") or "") for q in q_list[:2] if q.get("breakpointNote"))
        rows.append(
            [
                idx,
                name_zh,
                name_en,
                kind_s,
                q0.get("category") or "-",
                stat_desc[:90],
                bp_note[:70] or "-",
            ]
        )
    lines.append(
        render_table(
            ["#", "Gem (ZH)", "Gem (EN)", "Type", "Tier", "Quality Effect (@20% -> @40%)", "Breakpoint / Step"],
            rows,
        )
    )
    return "\n".join(lines)


def _render_tree_data_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    lines: list[str] = []
    for w in res.get("warnings") or []:
        if isinstance(w, dict):
            tag = "[POE1-PATCH-WARN]" if w.get("category") == "legacy_patch" else "[POE1-TRAP-WARN]"
            canon = f" (canonical: {w['canonicalTerm']})" if w.get("canonicalTerm") else ""
            lines.append(f"> {tag} `{w.get('trigger', '')}`{canon}: {w.get('warning', '')}")

    filters_obj = res.get("filters") if isinstance(res.get("filters"), dict) else {}
    filters: list[str] = []
    if res.get("queries"):
        filters.append("Q=`" + ", ".join(str(x) for x in res["queries"]) + "`")
    if filters_obj.get("type"):
        filters.append(f"Type=`{filters_obj['type']}`")
    if filters_obj.get("asc"):
        filters.append(f"Asc=`{filters_obj['asc']}`")
    filter_s = " | ".join(filters) or "all"
    lines.append(
        f"### Local Passive Tree Data (`Tree {res.get('treeVersion') or '3_30'}`): {filter_s} — "
        f"`{res.get('count', 0)}` hits (`{res.get('elapsedMs')}ms`)"
    )
    aliases = res.get("resolvedAliases") or []
    if aliases:
        lines.append("- **Resolved Slang**: " + ", ".join(f"`{a['from']}` -> `{a['to']}`" for a in aliases))
    results = res.get("results") or []
    if not results:
        lines.append("_(no matching passive tree nodes)_")
        return "\n".join(lines)

    rows: list[list[Any]] = []
    for n in results:
        name_en = str(n.get("name") or "-")
        name_zh = str(n.get("zhName") or "")
        disp = f"{name_zh} ({name_en})" if (name_zh and name_zh != name_en) else name_en
        asc_en = str(n.get("ascName") or n.get("asc") or "")
        asc_zh = str(n.get("ascNameZh") or n.get("ascZh") or "")
        if asc_zh and asc_en and asc_zh != asc_en:
            asc_disp = f"{asc_zh} ({asc_en})"
        else:
            asc_disp = asc_zh or asc_en or "-"
        if n.get("isBloodline"):
            asc_disp = f"{asc_disp} [Bloodline]"
        stats_lines = n.get("stats") or []
        if not stats_lines and n.get("masteryEffects"):
            stats_lines = ["; ".join(me.get("stats") or []) for me in (n.get("masteryEffects") or [])[:3]]
        stats_txt = "; ".join(str(s) for s in stats_lines[:6]) or "-"
        rows.append([n.get("id"), disp, n.get("type") or "-", asc_disp, stats_txt])
    lines.append(render_table(["ID", "Node", "Type", "Ascendancy", "Live 3.30 Stats"], rows))
    return "\n".join(lines)


def _render_tick_data_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    cd = res.get("current") or {}
    nb = cd.get("nextBreakpoint") or {}
    preset = res.get("preset") or res.get("presetLabel")
    label = f" (`{preset}`)" if preset else ""
    base_cd = res.get("baseCdSec")
    cdr_val = cd.get("globalCdrPct", 0)
    spells = res.get("triggeredSpells", 1)
    lines = [
        f"### PoE1 Server Tick Cooldown Calculator (`0.033s = 33ms` tick){label}",
        "- **Formula**: `ticks = ceil((base_cd / (1 + cdr/100)) / 0.033)`, `effective_cd = ticks * 0.033s` (NOT `1/30 = 33.33ms`)",
    ]
    if cd:
        lines.append(
            f"- **Current (`base={base_cd}s`, `CDR={cdr_val:g}%`, `spells={spells}`)**: "
            f"raw `{cd.get('rawCdSec')}s` -> **`{cd.get('ticks')}` ticks = `{cd.get('effectiveCdSec')}s` (`{cd.get('effectiveCdMs')}ms`)**, "
            f"cap **`{cd.get('effectiveCapPerSec')}/s`**"
            + (
                f" | **Next breakpoint**: `{nb.get('requiredCdrPct')}%` CDR (`+{nb.get('cdrNeededPct')}%` more) -> "
                f"`{nb.get('targetTicks')}` ticks = `{nb.get('effectiveCdMs')}ms` (`{nb.get('effectiveCapPerSec')}/s`)"
                if nb
                else " | at minimum 1-tick cap"
            )
        )
    if cd.get("aps") is not None:
        if cd.get("overspeed"):
            lines.append(f"> **[OVERSPEED WARNING]** {cd.get('overspeedNote')}")
        else:
            lines.append(
                f"- **APS Check**: OK — `{cd['aps']:.2f}/s` <= cap `{cd.get('effectiveCapPerSec')}/s` "
                f"(ratio `{cd.get('apsRatioToCap')}`)"
            )
    if res.get("cwcNote"):
        lines.append(f"- **CwC Reminder**: {res['cwcNote']}")

    bp_rows = [
        [
            f"{bp.get('ticks')}t" + (" *" if cd and bp.get("ticks") == cd.get("ticks") else ""),
            f"{bp.get('effectiveCdSec')}s ({bp.get('effectiveCdMs')}ms)",
            f"{bp.get('minCdrPct')}%",
            f"{bp.get('maxApsCap') or bp.get('maxTriggersPerSec')}/s",
        ]
        for bp in (res.get("breakpoints") or [])
    ]
    if bp_rows:
        lines.append("\n#### Exact `0.033s` Tick Breakpoint Table")
        lines.append(render_table(["Ticks", "Effective CD", "Min CDR %", "Max Triggers/s"], bp_rows))
    return "\n".join(lines)


def _render_single_poedb_markdown(res: dict[str, Any]) -> str:
    from .formatters import render_table

    if res.get("error"):
        return f"### PoEDB: `{res.get('query', '-')}` — Error: {res['error']}"

    lines = [
        f"### PoEDB: {res.get('title')} (`{res.get('slug')}`) — {res.get('url')}",
    ]
    if res.get("stale"):
        lines.append("> [WARN] Serving stale cached PoEDB response due to network error.")
    if res.get("category"):
        lines.append(f"- **Category**: {res['category']}")
    for prop in res.get("properties") or []:
        lines.append(f"- {prop}")
    if res.get("implicits"):
        lines.append("\n#### Implicits")
        for ln in res["implicits"]:
            lines.append(f"- {ln}")
    if res.get("explicits"):
        lines.append("\n#### Explicits")
        for ln in res["explicits"]:
            lines.append(f"- {ln}")

    vh = res.get("versionHistory") or []
    if vh:
        lines.append("\n#### Version History")
        v_rows = [[v.get("version"), "; ".join(v.get("changes") or [])[:140]] for v in vh[:10]]
        lines.append(render_table(["Version", "Changes"], v_rows))

    mods = res.get("mods")
    if mods is not None:
        pool_slug = res.get("modPoolSlug") or res.get("slug")
        total_m = res.get("modCountTotal", len(mods))
        flt_s = f", filter=`{res['modFilter']}`" if res.get("modFilter") else ""
        lines.append(f"\n#### Mod Pool (`{pool_slug}`{flt_s} — showing {len(mods)} of {total_m} mods)")
        m_rows = []
        for m in mods:
            fam = ", ".join(m.get("family") or [])[:35] or "-"
            tags = ", ".join(m.get("fossilNo") or m.get("tags") or [])[:40] or "-"
            m_rows.append(
                [
                    m.get("side") or "-",
                    fam,
                    m.get("level") if m.get("level") is not None else "-",
                    (m.get("text") or "-")[:90],
                    m.get("dropChance") if m.get("dropChance") is not None else "-",
                    tags,
                ]
            )
        lines.append(render_table(["Side", "Family", "iLvl", "Mod Text", "Weight", "Tags (fossil_no)"], m_rows))

    return "\n".join(lines)


def _render_single_price_markdown(res: dict[str, Any], query: str) -> str:
    from .formatters import fmt_num, render_table

    if res.get("error"):
        return f"### poe.ninja Price Search: `{query}` — Error: {res['error']}"

    matches = res.get("matches", [])
    is_det = bool(res.get("detailed")) or any(
        m.get("gemLevel") is not None or m.get("corrupted") is not None for m in matches
    )
    div_rate = res.get("divineChaosRate")
    rate_str = f" | 1 Divine = `{fmt_num(div_rate, 1)}` Chaos" if div_rate else ""
    det_tag = " [Detailed]" if is_det else ""
    lines = [
        f"### poe.ninja Price Search{det_tag}: `{query}` (League: `{res.get('league')}`{rate_str}, {res.get('totalMatches', len(matches))} matches)"
    ]
    if res.get("stale"):
        lines.append("> [WARN] Serving stale cached poe.ninja price data due to network error.")

    if is_det:
        rows = []
        for m in matches:
            lv = m.get("gemLevel")
            ql = m.get("gemQuality")
            lv_q = f"{lv}/{ql or 0}" if lv is not None else "-"
            corr = "Yes" if m.get("corrupted") is True else ("No" if m.get("corrupted") is False else "-")
            links = f"{m['links']}L" if m.get("links") else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or "-",
                    lv_q,
                    corr,
                    links,
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        lines.append(
            render_table(
                ["Item / Currency", "Category", "Variant", "Lvl/Qual", "Corrupted", "Links", "Chaos", "Divine", "Listings"],
                rows,
            )
        )
    else:
        rows = []
        for m in matches:
            trend = m.get("trend24hPct")
            trend_str = f"{float(trend):+.1f}%" if trend is not None else "-"
            rows.append(
                [
                    m.get("name"),
                    m.get("type"),
                    m.get("variant") or (f"{m['links']}L" if m.get("links") else "-"),
                    fmt_num(m.get("chaosValue"), 1),
                    fmt_num(m.get("divineValue"), 2) if m.get("divineValue") is not None else "-",
                    trend_str,
                    m.get("listingCount") if m.get("listingCount") is not None else "-",
                ]
            )
        lines.append(
            render_table(["Item / Currency", "Category", "Variant / Links", "Chaos", "Divine", "24h Trend", "Listings"], rows)
        )
    return "\n".join(lines)


def _render_single_wiki_markdown(res: dict[str, Any], topic: str) -> str:
    if res.get("error"):
        return f"### PoE Wiki: `{topic}` — Error: {res['error']}"
    parts = [f"### PoE Wiki: {res.get('title') or topic} ({res.get('url') or '-'})"]
    orig_q = str(res.get("query") or topic).strip()
    resolved_q = str(res.get("resolvedQuery") or "").strip()
    if resolved_q and resolved_q.lower() != orig_q.lower():
        parts.append(f"- **Resolved Query**: `{orig_q}` -> `{resolved_q}`")
    if res.get("stale"):
        parts.append("> [WARN] Serving stale cached PoE Wiki response due to network error.")

    mech_secs = res.get("mechanicsSections") or []
    if mech_secs:
        summary_str = str(res.get("summary") or "").strip()
        lead_only = summary_str.split("\n\n## ", 1)[0].strip() if summary_str else ""
        if lead_only:
            parts.append(lead_only)
        mech_lines = ["#### Mechanics & Interactions"]
        for sec in mech_secs:
            if isinstance(sec, dict) and sec.get("text"):
                heading = str(sec.get("heading") or "Mechanics").strip()
                text = str(sec.get("text") or "").strip()
                mech_lines.append(f"##### {heading}\n{text}")
        if len(mech_lines) > 1:
            parts.append("\n\n".join(mech_lines))
    elif res.get("summary"):
        parts.append(str(res["summary"]))
    return "\n\n".join(parts)


_POEDB_ENTITY_CATS = frozenset({
    "Skill Gems", "Support Gems", "Unique", "Passive", "Ascendancy", "Bloodline", "Keywords",
    "Body Armours", "Shields", "Helmets", "Gloves", "Boots", "Rings", "Amulets", "Belts",
    "Jewels", "Cluster Jewel", "Timeless Jewel", "Two Hand Swords", "One Hand Swords",
    "Wands", "Bows", "Staves", "Daggers", "Sceptres", "Claws", "One Hand Axes", "Two Hand Axes",
    "One Hand Maces", "Two Hand Maces", "Quivers", "Life Flasks", "Mana Flasks", "Utility Flasks",
})


def _is_long_nl_research_query(query: str) -> bool:
    import re

    q = query.strip()
    words = re.findall(r"[A-Za-z0-9'\-]+", q)
    if len(words) >= 7:
        return True
    if len(words) >= 6 and (
        ":" in q
        or "?" in q
        or bool(re.search(r"\b(?:how|does|do|can|what|why|when|with|versus|vs|patch|changes?|values?|work|stack|trigger|hit|same)\b", q, re.I))
    ):
        return True
    return len(q) >= 24 and bool(re.search(r"[？?]|怎么|如何|能否|能不能|吃不吃|算不算|需要多少|有没有|为什么|配合|还是", q))


def _scan_poedb_labels_in_text(text: str, *, limit: int = 4) -> list[str]:
    import json
    import re

    from .updater.paths import get_shared_cache_dir

    try:
        idx_path = get_shared_cache_dir() / "poedb_autocomplete_index.json"
        if not idx_path.is_file():
            return []
        entries = json.loads(idx_path.read_text(encoding="utf-8")).get("entries") or []
    except Exception:
        return []

    low = text.lower()
    matches: list[tuple[int, int, str]] = []
    seen_us: set[str] = set()
    for ent in entries:
        if not isinstance(ent, dict) or ent.get("usDesc") not in _POEDB_ENTITY_CATS:
            continue
        us = str(ent.get("usLabel") or "").strip()
        cn = str(ent.get("cnLabel") or "").strip()
        if not us or us.lower() in seen_us:
            continue
        pos = -1
        mlen = 0
        if len(us) >= 4 and us.lower() in low:
            m = re.search(rf"(?<![A-Za-z0-9]){re.escape(us)}(?![A-Za-z0-9])", text, re.IGNORECASE)
            if m:
                pos, mlen = m.start(), len(us)
        if pos < 0 and len(cn) >= 2:
            cn_clean = re.sub(r"[（(]辅[）)]$", "", cn).strip()
            if len(cn_clean) >= 2 and cn_clean in text:
                pos, mlen = text.index(cn_clean), len(cn_clean)
        if pos >= 0:
            seen_us.add(us.lower())
            matches.append((pos, -mlen, us))

    matches.sort()
    chosen: list[tuple[int, int, str]] = []
    for pos, neg_len, lbl in matches:
        end = pos - neg_len
        if any(pos < (p2 - n2) and end > p2 for p2, n2, _ in chosen):
            continue
        chosen.append((pos, neg_len, lbl))
        if len(chosen) >= limit:
            break
    return [lbl for _, _, lbl in chosen]


def _expand_long_research_queries(raw_queries: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    from .guardrails import GuardrailScope, check_input, extract_research_entities, validate_url

    expanded: list[str] = []
    seen: set[str] = set()
    auto_splits: list[dict[str, Any]] = []

    def _add(term: str) -> None:
        key = term.strip().lower()
        if key and key not in seen:
            seen.add(key)
            expanded.append(term.strip())

    for q in raw_queries:
        if not _is_long_nl_research_query(q):
            _add(q)
            continue
        validate_url(q, expected_game="poe1", cli_subcommand="research")
        check_input(q, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
        extracted = _scan_poedb_labels_in_text(q, limit=4)
        if not extracted:
            try:
                from .game_data import ground_text_entities

                g = ground_text_entities(q, limit=4)
                for ent in g.get("entities") or ():
                    if isinstance(ent, dict):
                        nm = str(ent.get("display_name") or ent.get("name") or "").strip()
                        if nm and nm not in extracted:
                            extracted.append(nm)
            except Exception:
                pass
        if not extracted:
            extracted = extract_research_entities(q, limit=4)
        if extracted:
            auto_splits.append({"original": q, "entities": extracted[:4]})
            for ent in extracted[:4]:
                _add(ent)
        else:
            _add(q)
    return expanded, auto_splits


def run_dual_research(
    queries: list[str],
    *,
    include_mods: bool = False,
    mod_filter: str | None = None,
    lang: str = "cn",
    proxy: str | None = None,
    limit_mods: int = 40,
) -> dict[str, Any]:
    """Concurrently query poedb.tw + poewiki.net per topic with partial-success resilience."""
    import concurrent.futures

    from . import external_data as ext
    from . import poedb_client as pc
    from .errors import CrossGameHallucinationError, LegacyPatchTrapError

    raw_queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not raw_queries:
        raise ValueError("Research queries cannot be empty.")

    clean_queries, auto_splits = _expand_long_research_queries(raw_queries)

    def _run_poedb(q: str) -> dict[str, Any]:
        try:
            extra_kw: dict[str, Any] = {"mod_filter": mod_filter} if (mod_filter and mod_filter.strip()) else {}
            return pc.query_poedb(
                q, include_mods=include_mods, lang=lang, proxy=proxy, limit_mods=limit_mods, **extra_kw
            )
        except (CrossGameHallucinationError, LegacyPatchTrapError):
            raise
        except Exception as exc:
            return {"query": q, "error": str(exc)}

    def _run_wiki(q: str) -> dict[str, Any]:
        try:
            return ext.query_wiki(q, proxy=proxy)
        except (CrossGameHallucinationError, LegacyPatchTrapError):
            raise
        except Exception as exc:
            return {"query": q, "error": str(exc)}

    poedb_results: dict[int, dict[str, Any]] = {}
    wiki_results: dict[int, dict[str, Any]] = {}
    workers = min(4, max(1, len(clean_queries) * 2))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs: dict[concurrent.futures.Future[dict[str, Any]], tuple[str, int]] = {}
        for idx, q in enumerate(clean_queries):
            futs[pool.submit(_run_poedb, q)] = ("poedb", idx)
            futs[pool.submit(_run_wiki, q)] = ("wiki", idx)
        for fut in concurrent.futures.as_completed(futs):
            src, idx = futs[fut]
            data = fut.result()
            if src == "poedb":
                poedb_results[idx] = data
            else:
                wiki_results[idx] = data

    results: list[dict[str, Any]] = []
    for idx, q in enumerate(clean_queries):
        pdb = poedb_results[idx]
        wk = wiki_results[idx]
        if wk.get("error") and not pdb.get("error"):
            pdb_slug = str(pdb.get("slug") or "").replace("_", " ").strip()
            if pdb_slug and pdb_slug.lower() != q.lower():
                wk = _run_wiki(pdb_slug)
        ok_sources = [s for s, d in (("poedb", pdb), ("wiki", wk)) if not d.get("error")]
        errors = {s: str(d["error"]) for s, d in (("poedb", pdb), ("wiki", wk)) if d.get("error")}
        results.append(
            {
                "query": q,
                "poedb": pdb,
                "wiki": wk,
                "okSources": ok_sources,
                "errors": errors,
            }
        )

    out: dict[str, Any] = {
        "queries": clean_queries,
        "count": len(results),
        "includeMods": include_mods,
        "lang": lang,
        "results": results,
    }
    if mod_filter and mod_filter.strip():
        out["modFilter"] = mod_filter.strip()
    if auto_splits:
        out["autoSplits"] = auto_splits
    if any(bool((r["poedb"]).get("stale")) or bool((r["wiki"]).get("stale")) for r in results):
        out["stale"] = True
    return out


def _render_research_markdown(res: dict[str, Any]) -> str:
    items = res.get("results") or []
    if not items:
        return "### Dual-Source Research\n_(no results)_"

    prefix_lines: list[str] = []
    for sp in res.get("autoSplits") or ():
        if isinstance(sp, dict) and sp.get("entities"):
            ents = ", ".join(f"`{e}`" for e in sp["entities"])
            prefix_lines.append(
                f"> [AUTO-SPLIT] Detected long sentence query; split into PoE1 entity lookups: {ents} "
                "(Tip: pass 1-3 short official entity names directly for fastest results)."
            )

    blocks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        q = str(item.get("query") or "-")
        parts = [f"## Dual-Source Research: `{q}`"]
        ok_sources = item.get("okSources") or []
        errors = item.get("errors") or {}
        if errors and ok_sources:
            err_s = "; ".join(f"{k}: {v}" for k, v in errors.items())
            parts.append(f"> [PARTIAL] Succeeded: {', '.join(ok_sources)} | Errors: {err_s}")
        pdb = item.get("poedb") if isinstance(item.get("poedb"), dict) else {"query": q, "error": "missing"}
        wk = item.get("wiki") if isinstance(item.get("wiki"), dict) else {"query": q, "error": "missing"}
        parts.append(_render_single_poedb_markdown(pdb))
        parts.append(_render_single_wiki_markdown(wk, q))
        blocks.append("\n\n".join(parts))
    body = "\n\n---\n\n".join(blocks)
    return ("\n".join(prefix_lines) + "\n\n" + body) if prefix_lines else body
