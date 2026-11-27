"""Markdown renderers for offline game data, PoEDB, poe.ninja prices, and PoE Wiki CLI outputs."""

from __future__ import annotations

from typing import Any

from .formatters.price_md import render_single_price_markdown as _render_single_price_markdown  # noqa: F401


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
        or bool(
            re.search(
                r"\b(?:how|does|do|can|what|why|when|with|versus|vs|patch|changes?|values?|work|stack|trigger|hit|same)\b",
                q,
                re.I,
            )
        )
    ):
        return True
    return len(q) >= 24 and bool(
        re.search(r"[？?]|怎么|如何|能否|能不能|吃不吃|算不算|需要多少|有没有|为什么|配合|还是", q)
    )


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
        pos, mlen = -1, 0
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


_LAZY_RESEARCH_EXPORTS = frozenset(
    {
        "_render_research_markdown",
        "_render_single_wiki_markdown",
        "run_dual_research",
    }
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_RESEARCH_EXPORTS:
        from . import cli_research

        return getattr(cli_research, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
