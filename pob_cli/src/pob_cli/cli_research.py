"""PoEDB, PoE Wiki, and dual-source `research` Click commands & helpers for `pob` CLI."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from typing import Any

import click

from .cli_common import _call_with_supported_kwargs, _emit_json, _emit_text, _output_options
from .cli_data_render import _expand_long_research_queries, _render_single_poedb_markdown


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


def run_dual_research(
    queries: list[str],
    *,
    include_mods: bool = False,
    mod_filter: str | None = None,
    lang: str = "cn",
    proxy: str | None = None,
    limit_mods: int = 150,
    max_chars: int = 12000,
) -> dict[str, Any]:
    """Concurrently query poedb.tw + poewiki.net per topic with partial-success resilience."""
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
            return _call_with_supported_kwargs(ext.query_wiki, q, proxy=proxy, max_chars=max_chars)
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


def _render_multi_sections(
    multi_res: Any,
    q_list: list[str],
    render_one: Callable[[dict[str, Any], str], str],
) -> str:
    sections: list[str] = []
    res_map = multi_res.get("results") if isinstance(multi_res, dict) else multi_res
    resp_queries = (
        [str(x) for x in multi_res["queries"]]
        if isinstance(multi_res, dict) and isinstance(multi_res.get("queries"), list)
        else q_list
    )
    if isinstance(res_map, dict):
        for q in resp_queries:
            q_key = q.strip()
            sub = res_map[q] if q in res_map else (res_map[q_key] if q_key in res_map else {"query": q_key, "error": "missing"})
            sections.append(render_one(sub or {"query": q_key, "error": "missing"}, q_key))
    elif isinstance(res_map, list):
        for idx, item in enumerate(res_map):
            sub_dict = dict(item) if isinstance(item, dict) else {"error": "missing"}
            lbl = str(sub_dict.get("query") or (resp_queries[idx] if idx < len(resp_queries) else "-"))
            sub_dict.setdefault("query", lbl)
            sections.append(render_one(sub_dict, lbl))
    return "\n\n".join(sections)


@click.command("poedb")
@click.argument("queries", nargs=-1, required=True)
@click.option("--mods", "include_mods", is_flag=True, help="Fetch item/base mod pool with DropChance weights & fossil tags.")
@click.option("--filter", "mod_filter", type=str, default=None, help="Filter mod pool rows by keyword(s) (case-insensitive, '|' for OR).")
@click.option("--lang", type=click.Choice(["cn", "us", "en"], case_sensitive=False), default="cn", show_default=True, help="PoEDB language.")
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--limit", type=click.IntRange(min=0), default=150, show_default=True, help="Maximum mod pool rows to return (0 = all).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_poedb(
    queries: tuple[str, ...],
    include_mods: bool,
    mod_filter: str | None,
    lang: str,
    proxy: str | None,
    limit: int,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query poedb.tw CN/US autocomplete index, properties, version history, and mod pool weights (cached)."""
    from . import poedb_client as pc

    q_list = [q.strip() for q in queries if q and q.strip()]
    if not q_list:
        raise click.UsageError("Provide at least one non-empty query.")
    extra_kw: dict[str, Any] = {"mod_filter": mod_filter} if mod_filter else {}
    if len(q_list) == 1:
        res = _call_with_supported_kwargs(
            pc.query_poedb, q_list[0], include_mods=include_mods, lang=lang, proxy=proxy, limit_mods=limit, **extra_kw
        )
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_single_poedb_markdown(res), out_path=out_path)
        return

    if hasattr(pc, "query_poedb_multi"):
        multi_res = _call_with_supported_kwargs(
            pc.query_poedb_multi, q_list, include_mods=include_mods, lang=lang, proxy=proxy, limit_mods=limit, **extra_kw
        )
    else:
        grouped = {
            q: _call_with_supported_kwargs(
                pc.query_poedb, q, include_mods=include_mods, lang=lang, proxy=proxy, limit_mods=limit, **extra_kw
            )
            for q in q_list
        }
        multi_res = {"queries": q_list, "results": grouped}

    if as_json:
        _emit_json(multi_res, compact=compact, out_path=out_path, sanitize=False)
        return
    _emit_text(_render_multi_sections(multi_res, q_list, lambda sub, _lbl: _render_single_poedb_markdown(sub)), out_path=out_path)


@click.command("wiki")
@click.argument("queries", nargs=-1, required=True)
@click.option("--max-chars", "max_chars", type=click.IntRange(min=0), default=12000, show_default=True, help="Maximum summary characters per page (0 = unlimited).")
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_wiki(
    queries: tuple[str, ...],
    max_chars: int,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query poewiki.net MediaWiki API for mechanics, uniques, and keystones (24h local cache)."""
    from . import external_data as ext

    q_list = [q.strip() for q in queries if q and q.strip()]
    if not q_list:
        raise click.UsageError("Provide at least one non-empty query.")
    if len(q_list) == 1:
        res = _call_with_supported_kwargs(ext.query_wiki, q_list[0], proxy=proxy, max_chars=max_chars)
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_single_wiki_markdown(res, q_list[0]), out_path=out_path)
        return

    if hasattr(ext, "query_wiki_multi"):
        multi_res = _call_with_supported_kwargs(ext.query_wiki_multi, q_list, proxy=proxy, max_chars=max_chars)
    else:
        grouped = {q: _call_with_supported_kwargs(ext.query_wiki, q, proxy=proxy, max_chars=max_chars) for q in q_list}
        multi_res = {"queries": q_list, "results": grouped}

    if as_json:
        _emit_json(multi_res, compact=compact, out_path=out_path, sanitize=False)
        return
    _emit_text(_render_multi_sections(multi_res, q_list, _render_single_wiki_markdown), out_path=out_path)


@click.command("research")
@click.argument("queries", nargs=-1, required=True)
@click.option("--mods", "include_mods", is_flag=True, help="Also fetch PoEDB mod pool weights & fossil tags.")
@click.option("--filter", "mod_filter", type=str, default=None, help="Filter PoEDB mod pool rows by keyword(s) (case-insensitive, '|' for OR).")
@click.option("--lang", type=click.Choice(["cn", "us", "en"], case_sensitive=False), default="cn", show_default=True, help="PoEDB language.")
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--limit", type=click.IntRange(min=0), default=150, show_default=True, help="Maximum PoEDB mod pool rows (0 = all).")
@click.option("--max-chars", "max_chars", type=click.IntRange(min=0), default=12000, show_default=True, help="Maximum PoE Wiki summary characters per topic (0 = unlimited).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_research(
    queries: tuple[str, ...],
    include_mods: bool,
    mod_filter: str | None,
    lang: str,
    proxy: str | None,
    limit: int,
    max_chars: int,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Concurrent dual-source research via poedb.tw + poewiki.net (partial-success resilient)."""
    q_list = [q.strip() for q in queries if q and q.strip()]
    if not q_list:
        raise click.UsageError("Provide at least one non-empty query.")
    res = run_dual_research(
        q_list,
        include_mods=include_mods,
        mod_filter=mod_filter,
        lang=lang,
        proxy=proxy,
        limit_mods=limit,
        max_chars=max_chars,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
        return
    _emit_text(_render_research_markdown(res), out_path=out_path)


def register_research_commands(group: click.Group) -> None:
    for cmd in (cmd_poedb, cmd_wiki, cmd_research):
        group.add_command(cmd)
