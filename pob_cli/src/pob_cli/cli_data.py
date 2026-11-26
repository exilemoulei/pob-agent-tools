"""Offline game data and poe.ninja economy/meta Click commands for `pob` CLI."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import _call_with_supported_kwargs, _emit_json, _emit_text, _output_options
from .cli_data_render import (
    _render_gem_quality_breakpoints_markdown,
    _render_single_data_markdown,
    _render_single_price_markdown,
    _render_tick_data_markdown,
    _render_tree_data_markdown,
)
from .cli_research import cmd_poedb, cmd_research, cmd_wiki


@click.command("data")
@click.argument(
    "kind",
    type=click.Choice(
        ["gem", "unique", "mod", "base", "translate", "tattoo", "runegraft", "tree", "tick"],
        case_sensitive=False,
    ),
)
@click.argument("queries", nargs=-1, required=False)
@click.option("--slot", help="Filter mods/uniques/bases by slot, or tattoos by target passive type (e.g. 'body_armour', 'Small Strength', 'Mastery').")
@click.option("--ilvl", type=int, help="Maximum item level / required level filter.")
@click.option("--mod", "mod_filter", type=str, default=None, help="[unique/tattoo] Filter uniques or tattoos/runegrafts by mod/stat substring (EN or ZH).")
@click.option(
    "--kind",
    "tattoo_kind",
    type=click.Choice(["all", "tattoo", "runegraft"], case_sensitive=False),
    default="all",
    show_default=True,
    help="[tattoo] Filter passive overrides by kind (all, tattoo, runegraft).",
)
@click.option("--legacy", "include_legacy", is_flag=True, help="[tattoo, runegraft] Include retired 3.22 legacy tattoos.")
@click.option("--type", "node_type", type=str, default=None, help="[tree only] Filter passive tree nodes by type (Keystone, Notable, Mastery, Ascendancy, Bloodline, Socket).")
@click.option("--asc", "asc_filter", type=str, default=None, help="[tree only] Filter passive tree nodes by Ascendancy or Bloodline name (EN or ZH, e.g. Warden, Trickster, 守望者).")
@click.option("--cdr", "cdr_pct", type=float, default=0.0, show_default=True, help="[tick only] Total Cooldown Recovery Rate % (e.g. 14, 52).")
@click.option("--aps", "aps_rate", type=float, default=None, help="[tick only] Trigger attack/action rate per second for CoC overspeed check.")
@click.option("--spells", "triggered_spells", type=int, default=1, show_default=True, help="[tick only] Number of distinct triggered skills sharing the trigger.")
@click.option("--limit", type=click.IntRange(min=0), default=25, show_default=True, help="Maximum number of results per query (0 = unlimited).")
@click.option("--quality-breakpoints", "quality_breakpoints", is_flag=True, help="[gem only] Scan 3.30 gems for 20%->40%+ quality breakpoints (integer steps & more multipliers).")
@click.option("--search-quality", "search_quality", type=str, default=None, help="[gem only] Search gems by quality stat keyword (EN or ZH, e.g. 'projectile', '连锁', 'more').")
@click.option("--active-only", "active_only", is_flag=True, help="[gem quality scan] Restrict to Active Skill Gems only (matching '+% to Quality of all Skill Gems').")
@click.option("--rebuild", is_flag=True, help="Force rebuild the local SQLite game/tree data index.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_data(
    kind: str,
    queries: tuple[str, ...],
    slot: str | None,
    ilvl: int | None,
    mod_filter: str | None,
    tattoo_kind: str,
    include_legacy: bool,
    node_type: str | None,
    asc_filter: str | None,
    cdr_pct: float,
    aps_rate: float | None,
    triggered_spells: int,
    limit: int,
    quality_breakpoints: bool,
    search_quality: str | None,
    active_only: bool,
    rebuild: bool,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query local 0ms PoB Lua game/tree data & 0.033s tick calculator (`gem|unique|mod|base|translate|tattoo|runegraft|tree|tick`, daemon-free)."""
    from . import game_data as gd

    kind_low = kind.lower()
    if rebuild:
        if kind_low == "tree":
            gd.ensure_treedata_index(force_rebuild=True)
        elif kind_low != "tick":
            gd.ensure_gamedata_index(force_rebuild=True)

    q_list = [q.strip() for q in queries if q and q.strip()]

    if kind_low == "tick":
        if not q_list:
            raise click.UsageError(
                "Provide base cooldown seconds or trigger preset (e.g. `pob data tick 0.15 --cdr 14 --aps 8.5` or `pob data tick coc --cdr 52`)."
            )
        try:
            res = gd.query_tick_breakpoints(
                q_list[0],
                cdr_pct=cdr_pct,
                aps=aps_rate,
                triggered_spells=triggered_spells,
            )
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_tick_data_markdown(res), out_path=out_path)
        return

    if kind_low == "tree":
        if not q_list and not node_type and not asc_filter:
            raise click.UsageError(
                "Provide at least one node query or `--type` / `--asc` filter (e.g. `pob data tree --asc Warden` or `pob data tree Polymath`)."
            )
        res = gd.query_tree_data(
            q_list or None,
            node_type=node_type,
            asc=asc_filter,
            limit=limit,
        )
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_tree_data_markdown(res), out_path=out_path)
        return

    if mod_filter is not None and kind_low not in ("unique", "tattoo", "runegraft"):
        raise click.UsageError("--mod is only supported for `pob data unique` and `pob data tattoo`.")
    if (tattoo_kind.lower() != "all" or include_legacy) and kind_low not in ("tattoo", "runegraft"):
        raise click.UsageError("--kind and --legacy are only supported for `pob data tattoo` and `pob data runegraft`.")
    if kind_low == "runegraft" and tattoo_kind.lower() == "tattoo":
        raise click.UsageError("`pob data runegraft` lists runegrafts only; use `pob data tattoo --kind tattoo`.")

    if quality_breakpoints or search_quality is not None:
        if kind_low != "gem":
            raise click.UsageError("--quality-breakpoints and --search-quality are only supported for `pob data gem`.")
        eff_q = search_quality if search_quality is not None else (" ".join(q_list) or None)
        res = gd.query_gem_quality_breakpoints(
            eff_q,
            active_only=active_only,
            limit=limit,
        )
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_gem_quality_breakpoints_markdown(res), out_path=out_path)
        return

    extra_kw: dict[str, Any] = {}
    if kind_low in ("tattoo", "runegraft"):
        extra_kw = {"tattoo_kind": tattoo_kind.lower(), "include_legacy": include_legacy}

    if not q_list:
        if kind_low in ("tattoo", "runegraft"):
            res = gd.query_local_data(kind, "", slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit, **extra_kw)
            if as_json:
                _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
                return
            lbl = f"--mod {mod_filter}" if mod_filter else (slot or tattoo_kind)
            _emit_text(_render_single_data_markdown(res, kind, lbl), out_path=out_path)
            return
        if kind_low == "unique" and mod_filter and mod_filter.strip():
            res = gd.query_local_data(kind, "", slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit)
            if as_json:
                _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
                return
            _emit_text(_render_single_data_markdown(res, kind, f"--mod {mod_filter}"), out_path=out_path)
            return
        raise click.UsageError("Provide at least one non-empty query.")
    if len(q_list) == 1:
        res = gd.query_local_data(kind, q_list[0], slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit, **extra_kw)
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        q_disp = f"{q_list[0]} (--mod {mod_filter})" if mod_filter else q_list[0]
        _emit_text(_render_single_data_markdown(res, kind, q_disp), out_path=out_path)
        return

    multi_res: dict[str, Any]
    if hasattr(gd, "query_local_data_multi"):
        multi_res = _call_with_supported_kwargs(
            gd.query_local_data_multi, kind, q_list, slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit, **extra_kw
        )
    elif hasattr(gd, "query_multi"):
        import inspect

        sig = inspect.signature(gd.query_multi)
        first_param = next(iter(sig.parameters.keys()), "")
        if first_param in ("db", "conn", "db_path"):
            db_path = gd.ensure_gamedata_index()
            multi_res = _call_with_supported_kwargs(
                gd.query_multi, db_path, kind, q_list, slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit, **extra_kw
            )
        else:
            multi_res = _call_with_supported_kwargs(
                gd.query_multi, kind, q_list, slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit, **extra_kw
            )
    else:
        grouped = {
            q: gd.query_local_data(kind, q, slot=slot, ilvl=ilvl, mod_filter=mod_filter, limit=limit, **extra_kw)
            for q in q_list
        }
        multi_res = {"kind": kind.lower(), "queries": q_list, "results": grouped}

    if as_json:
        _emit_json(multi_res, compact=compact, out_path=out_path, sanitize=False)
        return

    sections: list[str] = []
    res_map = multi_res.get("results")
    resp_queries = (
        [str(x) for x in multi_res["queries"]]
        if isinstance(multi_res, dict) and isinstance(multi_res.get("queries"), list)
        else q_list
    )
    if isinstance(res_map, dict):
        for q in resp_queries:
            q_key = q.strip()
            sub = res_map[q] if q in res_map else (res_map[q_key] if q_key in res_map else {})
            sections.append(_render_single_data_markdown(sub or {}, kind, q_key))
    elif isinstance(res_map, list):
        for idx, sub in enumerate(res_map):
            sub_dict = sub if isinstance(sub, dict) else {}
            lbl = str(sub_dict.get("query") or (resp_queries[idx] if idx < len(resp_queries) else "-"))
            sections.append(_render_single_data_markdown(sub_dict, kind, lbl))
    _emit_text("\n\n".join(sections), out_path=out_path)


@click.command("price")
@click.argument("queries", nargs=-1, required=True)
@click.option("--league", help="League name (defaults to current primary softcore trade league).")
@click.option("--category", default="all", show_default=True, help="Category (all, unique, gem, imbuedgem, currency, card, cluster, base, flask, scarab, runegraft, wombgift, etc.).")
@click.option("--variant", help="Filter matches by variant substring (e.g. 'Necromancer', '8 passives', '21/20').")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True, help="Maximum number of matches to return.")
@click.option("--detailed", is_flag=True, help="Use detailed stash overview API (gem level/quality, corrupted, links, cluster passives).")
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_price(
    queries: tuple[str, ...],
    league: str | None,
    category: str,
    variant: str | None,
    limit: int,
    detailed: bool,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query poe.ninja economy prices (15m local cache, 52 overview categories, optional --detailed/--variant)."""
    from . import external_data as ext

    q_list = [q.strip() for q in queries if q and q.strip()]
    if not q_list:
        raise click.UsageError("Provide at least one non-empty query.")
    if len(q_list) == 1:
        res = _call_with_supported_kwargs(
            ext.query_price,
            q_list[0],
            league=league,
            category=category,
            limit=limit,
            variant=variant,
            detailed=detailed,
            proxy=proxy,
        )
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_render_single_price_markdown(res, q_list[0]), out_path=out_path)
        return

    if hasattr(ext, "query_price_multi"):
        multi_res = _call_with_supported_kwargs(
            ext.query_price_multi,
            q_list,
            league=league,
            category=category,
            limit=limit,
            variant=variant,
            detailed=detailed,
            proxy=proxy,
        )
    else:
        grouped = {
            q: _call_with_supported_kwargs(
                ext.query_price,
                q,
                league=league,
                category=category,
                limit=limit,
                variant=variant,
                detailed=detailed,
                proxy=proxy,
            )
            for q in q_list
        }
        multi_res = {"queries": q_list, "results": grouped}

    if as_json:
        _emit_json(multi_res, compact=compact, out_path=out_path, sanitize=False)
        return

    sections = []
    res_map = multi_res.get("results") if isinstance(multi_res, dict) else multi_res
    resp_queries = (
        [str(x) for x in multi_res["queries"]]
        if isinstance(multi_res, dict) and isinstance(multi_res.get("queries"), list)
        else q_list
    )
    if isinstance(res_map, dict):
        for q in resp_queries:
            q_key = q.strip()
            sub = res_map[q] if q in res_map else (res_map[q_key] if q_key in res_map else {"error": "missing"})
            sections.append(_render_single_price_markdown(sub or {"error": "missing"}, q_key))
    elif isinstance(res_map, list):
        for idx, sub in enumerate(res_map):
            sub_dict = sub if isinstance(sub, dict) else {"error": "missing"}
            lbl = str(sub_dict.get("query") or (resp_queries[idx] if idx < len(resp_queries) else "-"))
            sections.append(_render_single_price_markdown(sub_dict, lbl))
    _emit_text("\n\n".join(sections), out_path=out_path)


@click.command("meta")
@click.option("--league", help="League name or slug (defaults to current primary challenge league).")
@click.option("--skill", help="Filter by main skill (EN substring, ZH/TW name, or slang like 正火).")
@click.option("--class", "class_name", help="Filter by ascendancy/class (EN substring or ZH/TW like 酋长/圣宗).")
@click.option("--top", type=int, default=15, show_default=True, help="Number of top entries to show.")
@click.option("--proxy", help="Optional HTTP/SOCKS proxy URL.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_meta(
    league: str | None,
    skill: str | None,
    class_name: str | None,
    top: int,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Query poe.ninja league meta snapshot (total builds, ascendancy & main skill share)."""
    from . import external_data as ext
    from .formatters.gamedata_md import format_meta_markdown

    res = _call_with_supported_kwargs(
        ext.query_meta,
        league=league,
        skill=skill,
        class_name=class_name,
        top=top,
        proxy=proxy,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
        return
    _emit_text(format_meta_markdown(res), out_path=out_path)


def register_data_commands(group: click.Group) -> None:
    for cmd in (cmd_data, cmd_poedb, cmd_price, cmd_meta, cmd_wiki, cmd_research):
        group.add_command(cmd)
