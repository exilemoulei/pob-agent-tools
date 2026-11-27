"""`pob trade cluster` — 8-passive 2-socket Front-2 Back-1 Large Cluster Jewel trade query & live solver."""

from __future__ import annotations

import re
from typing import Any

import click

from ..cli_common import _emit_json, _emit_text, _output_options
from ..formatters import render_table
from ..guardrails import GuardrailScope, check_input, validate_url
from ..locale_cfg import current_realm
from ..trade_client import NoResults, TradeError, extract_listing_trade_meta, seller_url
from ..trade_eval.shopping_specials import encode_trade_search_url
from ..trade_query import (
    VALID_STATUSES,
    ClusterNotable,
    ClusterSolution,
    load_local_trade_stats,
    resolve_option_stat,
    resolve_stat,
    solve_front2_back1,
)
from .common import _err_utf8, _trade_error
from .other_cmds import trade_group

_ENCHANT_PASSIVES_ID = "enchant.stat_3086156145"
_ENCHANT_SOCKETS_ID = "enchant.stat_4079888060"
_ENCHANT_GRANT_ID = "enchant.stat_3948993189"
_NOTABLE_LINE_RE = re.compile(r"^1 Added Passive Skill is\s+(.+)$", re.IGNORECASE)
_WIKI_LINK_RE = re.compile(r"\[[^|\]]+\|([^\]]+)\]")
_NOTABLE_SPLIT_RE = re.compile(r"[,，;/|]+")


def _parse_two_notables(raw: str) -> tuple[str, str]:
    parts = [p.strip() for p in _NOTABLE_SPLIT_RE.split(raw or "") if p.strip()]
    if len(parts) != 2:
        raise ValueError(
            f"--notables requires exactly 2 comma-separated notables (got {len(parts)}: {raw!r})."
        )
    return parts[0], parts[1]


def _resolve_notable_stat_id(notable: ClusterNotable, explicit_entries: list[dict[str, Any]]) -> str:
    if explicit_entries:
        m = resolve_stat(f"1 Added Passive Skill is {notable.name}", explicit_entries, threshold=0.88)
        if m is not None and m.id.startswith("explicit."):
            return m.id
    if notable.stat_id_hint:
        return notable.stat_id_hint
    raise ValueError(f"Could not resolve trade stat ID for cluster notable {notable.name!r}.")


def _resolve_enchant_option_id(
    solution: ClusterSolution,
    stat_entries: list[dict[str, Any]],
) -> int | str:
    enchant = solution.enchant
    if stat_entries:
        probes = [*enchant.stats, enchant.name]
        for probe in probes:
            opt = resolve_option_stat(_ENCHANT_GRANT_ID, stat_entries, option_value=probe, domain="enchant")
            if opt is not None:
                return opt.option_id
    if enchant.trade_option_id is not None:
        return enchant.trade_option_id
    raise ValueError(f"Could not resolve trade enchant option ID for {enchant.name!r}.")


def _resolve_socket_stat_id(stat_entries: list[dict[str, Any]]) -> str:
    if stat_entries:
        enchant_entries = [e for e in stat_entries if str(e.get("id") or "").startswith("enchant.")]
        m = resolve_stat("# Added Passive Skills are Jewel Sockets", enchant_entries or stat_entries, threshold=0.90)
        if m is not None:
            return m.id
    return _ENCHANT_SOCKETS_ID


def build_cluster_trade_query(
    solution: ClusterSolution,
    stat_entries: list[dict[str, Any]],
    *,
    status: str = "securable",
    budget: float | None = None,
    currency: str = "chaos_equiv",
    include_back_count_group: bool = True,
) -> dict[str, Any]:
    """Build the official PoE1 `/api/trade/search` JSON body for a Front-2 Back-1 Large Cluster Jewel."""
    explicit_entries = [e for e in stat_entries if str(e.get("id") or "").startswith("explicit.")]
    opt_id = _resolve_enchant_option_id(solution, stat_entries)
    socket_stat_id = _resolve_socket_stat_id(stat_entries)
    stat_a = _resolve_notable_stat_id(solution.notable_a, explicit_entries)
    stat_b = _resolve_notable_stat_id(solution.notable_b, explicit_entries)

    if solution.sockets < 1:
        raise ValueError(f"Large Cluster Jewels require 1 or 2 jewel sockets (got {solution.sockets}).")

    and_filters: list[dict[str, Any]] = [
        {
            "id": _ENCHANT_PASSIVES_ID,
            "value": {"min": solution.passives, "max": solution.passives},
            "disabled": False,
        },
        {
            "id": socket_stat_id,
            "value": {"min": solution.sockets, "max": solution.sockets},
            "disabled": False,
        },
    ]
    and_filters.append({"id": _ENCHANT_GRANT_ID, "value": {"option": opt_id}, "disabled": False})
    and_filters.append({"id": stat_a, "value": {}, "disabled": False})
    and_filters.append({"id": stat_b, "value": {}, "disabled": False})

    stat_groups: list[dict[str, Any]] = [{"type": "and", "filters": and_filters}]
    if include_back_count_group and solution.valid_back_notables:
        back_filters = [
            {"id": _resolve_notable_stat_id(c, explicit_entries), "value": {}, "disabled": False}
            for c in solution.valid_back_notables
        ]
        stat_groups.append({"type": "count", "value": {"min": 1}, "filters": back_filters})

    type_filters: dict[str, Any] = {
        "category": {"option": "jewel.cluster"},
        "rarity": {"option": "rare" if include_back_count_group else "nonunique"},
    }
    filters_blk: dict[str, Any] = {"type_filters": {"filters": type_filters}}
    if solution.ilvl_max is not None:
        filters_blk["misc_filters"] = {"filters": {"ilvl": {"max": solution.ilvl_max}}}
    if budget is not None and budget > 0:
        curr_norm = (currency or "").strip().lower()
        price_flt: dict[str, Any] = (
            {"max": budget} if curr_norm in ("", "chaos_equiv", "equiv") else {"option": curr_norm, "max": budget}
        )
        filters_blk["trade_filters"] = {"filters": {"price": price_flt}}

    # Omit localized `type` ("Large Cluster Jewel"): `category=jewel.cluster` + Large enchant enum
    # + exact passives count uniquely identifies Large Cluster Jewels across both global and tw realms.
    return {
        "query": {
            "status": {"option": status},
            "stats": stat_groups,
            "filters": filters_blk,
        },
        "sort": {"price": "asc"},
    }


def _collect_item_explicit_hashes(raw_item: dict[str, Any]) -> list[str]:
    hashes: list[str] = []
    ext = raw_item.get("extended")
    if isinstance(ext, dict):
        ext_hashes = ext.get("hashes")
        if isinstance(ext_hashes, dict):
            for entry in ext_hashes.get("explicit") or ():
                if isinstance(entry, (list, tuple)) and entry and isinstance(entry[0], str):
                    hashes.append(entry[0].removeprefix("stat.").strip())
                elif isinstance(entry, str):
                    hashes.append(entry.removeprefix("stat.").strip())
    for mod in raw_item.get("explicitMods") or ():
        if isinstance(mod, dict) and isinstance(mod.get("hash"), str):
            hashes.append(mod["hash"].removeprefix("stat.").strip())
    return hashes


def _extract_back_notable_from_listing(
    raw_item: dict[str, Any],
    front_names: set[str],
    valid_back_names: set[str],
    valid_back_stat_ids: dict[str, str] | None = None,
) -> str | None:
    if valid_back_stat_ids:
        for stat_id in _collect_item_explicit_hashes(raw_item):
            matched = valid_back_stat_ids.get(stat_id)
            if matched:
                return matched

    mods = raw_item.get("explicitMods") or []
    found_notables: list[str] = []
    for entry in mods:
        if isinstance(entry, dict):
            text = entry.get("description") if isinstance(entry.get("description"), str) else ""
        elif isinstance(entry, str):
            text = entry
        else:
            continue
        cleaned = _WIKI_LINK_RE.sub(r"\1", text).strip()
        m = _NOTABLE_LINE_RE.match(cleaned)
        if m:
            found_notables.append(m.group(1).strip())
    for nm in found_notables:
        if nm in valid_back_names:
            return nm
    for nm in found_notables:
        if nm not in front_names:
            return nm
    return None


def _run_live_cluster_search(
    ctx: click.Context,
    *,
    query_body: dict[str, Any],
    solution: ClusterSolution,
    stat_entries: list[dict[str, Any]],
    league: str | None,
    realm: str,
    proxy: str | None,
    max_results: int,
) -> dict[str, Any] | None:
    import pob_cli.cli_trade as _ct

    with _ct.TradeClient(proxy=proxy, realm=realm) as client:
        try:
            league_name, league_warn = _ct.resolve_trade_league(
                league, offline_only=False, proxy=proxy, client=client, realm=realm
            )
            if league_warn:
                _err_utf8(f"Warning: {league_warn}")
            search_res = client.search(league_name, query_body)
            ids = list(search_res.get("result") or [])
            total = int(search_res.get("total") or 0)
            search_id = str(search_res.get("id") or "")
            if not ids:
                raise NoResults(status=(query_body.get("query") or {}).get("status", {}).get("option"))
            limit = max(1, max_results)
            rows = client.fetch_many(ids[:limit], search_id, limit=limit)
        except TradeError as exc:
            _trade_error(ctx, exc)
            return None

    front_names = {solution.notable_a.name, solution.notable_b.name}
    valid_back_names = {n.name for n in solution.valid_back_notables}
    explicit_entries = [e for e in stat_entries if str(e.get("id") or "").startswith("explicit.")]
    valid_back_stat_ids: dict[str, str] = {}
    for cand in solution.valid_back_notables:
        if cand.stat_id_hint:
            valid_back_stat_ids[cand.stat_id_hint] = cand.name
        try:
            valid_back_stat_ids[_resolve_notable_stat_id(cand, explicit_entries)] = cand.name
        except ValueError:
            pass

    listings: list[dict[str, Any]] = []
    for row in rows:
        meta = extract_listing_trade_meta(row)
        raw_item = row.get("item") if isinstance(row.get("item"), dict) else {}
        back_c = _extract_back_notable_from_listing(
            raw_item,
            front_names,
            valid_back_names,
            valid_back_stat_ids=valid_back_stat_ids,
        )
        seller = meta["seller"]
        direct_link = (
            seller_url(league_name, query_body["query"], seller, realm=realm)
            if seller
            else f"{client.base_url}/trade/search/{league_name}/{search_id}"
        )
        listings.append(
            {
                "listingId": meta["listingId"],
                "priceAmount": meta["priceAmount"],
                "priceCurrency": meta["priceCurrency"],
                "tradeMode": meta["tradeMode"],
                "goldFee": meta["goldFee"],
                "seller": seller,
                "ilvl": raw_item.get("ilvl"),
                "backNotable": back_c,
                "whisper": meta["whisper"],
                "url": direct_link,
            }
        )
    return {
        "league": league_name,
        "leagueWarning": league_warn,
        "searchId": search_id,
        "total": total,
        "fetched": len(listings),
        "listings": listings,
    }


def _format_cluster_markdown(payload: dict[str, Any]) -> str:
    sol = payload["solution"]
    ench = sol["enchant"]
    front = sol["frontNotables"]
    data_source = str(sol.get("dataSource") or "unknown")
    lines: list[str] = [
        f"### Large Cluster Jewel Solver (`{ench['name']}`, {sol['passives']}p / {sol['sockets']}s)",
        f"- **Front Notables**: `{front[0]['name']}` ({front[0]['affixType']}, order `{front[0]['sortOrder']}`, ilvl `{front[0]['minIlvl']}+`) + "
        f"`{front[1]['name']}` ({front[1]['affixType']}, order `{front[1]['sortOrder']}`, ilvl `{front[1]['minIlvl']}+`)",
        f"- **League / Realm**: `{payload['league']}` (`{payload['realm']}`), status=`{payload['status']}`, dataSource=`{data_source}`",
    ]
    if sol["possible"]:
        lines.append(f"- **Verdict**: POSSIBLE — {sol['explanation']}")
        if sol["minIlvlOverall"] is not None:
            lines.append(f"- **Minimum ilvl**: `{sol['minIlvlOverall']}+` (A+B floor: `{sol['minIlvlAB']}+`)")
        back_rows = [
            [n["name"], n["affixType"], str(n["sortOrder"]), f"ilvl {n['minIlvl']}+", str(n["weight"]), n["statId"] or "-"]
            for n in sol["validBackNotables"]
        ]
        lines.append("\n#### Valid Back-Seat Notables (`COUNT >= 1`)")
        lines.append(render_table(["Back Notable (C)", "Affix", "SortOrder", "Min ilvl", "Weight", "Stat ID"], back_rows))
        lines.append(f"\n- **Zero-HTTP Trade Link**: {payload['url']}")
    else:
        lines.append(f"- **Verdict**: IMPOSSIBLE (`{sol['reason']}`) — {sol['explanation']}")
        if sol["rejectedSandwich"]:
            rej_str = ", ".join(f"{r['name']} ({r['affixType']}: {r['rejectReason']})" for r in sol["rejectedSandwich"][:6])
            lines.append(f"- **Rejected Sandwich Notables**: {rej_str}")
        if sol["alternativeEnchants"]:
            alt_str = "; ".join(
                f"{a['name']} ({a['validBackCount']} fillers: {', '.join(a['validBackNames'][:3])})"
                for a in sol["alternativeEnchants"][:3]
            )
            lines.append(f"- **Alternative Large Enchants**: {alt_str}")
        if payload.get("fallbackUrl"):
            lines.append(f"- **2-Notable Fallback Link**: {payload['fallbackUrl']}")

    if data_source != "local_engine":
        lines.append(
            "> [WARN] Using limited offline_fallback cluster dataset (4/17 enchants); "
            "configure PoB engine for full 300-notable pool"
        )
    for warn in sol.get("warnings") or ():
        lines.append(f"> [WARN] {warn}")

    live = payload.get("live")
    if isinstance(live, dict):
        lines.append(f"\n#### Live Listings (`{live['total']}` total, showing `{live['fetched']}`)")
        rows_md: list[list[str]] = []
        for idx, item in enumerate(live.get("listings") or (), 1):
            price_s = (
                f"{item['priceAmount']:g} {item['priceCurrency']}"
                if item.get("priceAmount") is not None and item.get("priceCurrency")
                else "-"
            )
            mode_s = (
                f"instant ({item['goldFee']}g)"
                if item.get("tradeMode") == "securable" and item.get("goldFee") is not None
                else str(item.get("tradeMode") or "-")
            )
            rows_md.append(
                [
                    str(idx),
                    price_s,
                    mode_s,
                    str(item.get("backNotable") or "-"),
                    str(item.get("ilvl") or "-"),
                    str(item.get("seller") or "-"),
                    str(item.get("url") or "-"),
                ]
            )
        lines.append(render_table(["#", "Price", "Mode / Gold", "Back Notable (C)", "ilvl", "Seller", "Link"], rows_md))
    return "\n".join(lines)


@trade_group.command("cluster")
@click.option("--enchant", required=True, help='Large Cluster Jewel enchant (EN or ZH, e.g. "12% increased Lightning Damage" or "闪电伤害").')
@click.option("--notables", required=True, help='Two desired Front-seat notables separated by comma (EN or ZH, e.g. "Sadist,Snowstorm").')
@click.option("--passives", type=click.IntRange(8, 12), default=8, show_default=True, help="Added Passive Skills count (default 8).")
@click.option("--sockets", type=click.IntRange(0, 2), default=2, show_default=True, help="Added Jewel Sockets count (default 2).")
@click.option("--ilvl-max", "ilvl_max", type=click.IntRange(1, 100), default=None, help="Optional maximum item level filter.")
@click.option(
    "--realm",
    type=click.Choice(["global", "tw"], case_sensitive=False),
    default=None,
    help="Trade server realm (`global` pathofexile.com or `tw` pathofexile.tw).",
)
@click.option("--league", help="PoE1 trade league (defaults to active/cached league).")
@click.option("--live", is_flag=True, help="Query official Trade API live instead of zero-HTTP link only.")
@click.option("--budget", type=float, default=None, help="Optional maximum listing price.")
@click.option(
    "--currency",
    type=click.Choice(["chaos_equiv", "chaos", "divine"]),
    default="chaos_equiv",
    show_default=True,
    help="Price currency when --budget is set.",
)
@click.option(
    "--status",
    type=click.Choice(sorted(VALID_STATUSES)),
    default="securable",
    show_default=True,
    help="Trade listing status (`securable` = Faustus Instant Buyout).",
)
@click.option("--max-results", type=click.IntRange(1, 20), default=8, show_default=True, help="Max live listings to fetch.")
@click.option("--proxy", help="HTTP/SOCKS5 proxy URL (used only with --live).")
@click.option("--json", "as_json", is_flag=True, help="Output RFC-8259 JSON.")
@_output_options
def cmd_trade_cluster(
    enchant: str,
    notables: str,
    passives: int,
    sockets: int,
    ilvl_max: int | None,
    realm: str | None,
    league: str | None,
    live: bool,
    budget: float | None,
    currency: str,
    status: str,
    max_results: int,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Solve 8-passive 2-socket Front-2 Back-1 Large Cluster Jewel trade query and optional live buyouts."""
    import pob_cli.cli_trade as _ct

    ctx = click.get_current_context()
    validate_url(enchant, expected_game="poe1", cli_subcommand="trade cluster")
    validate_url(notables, expected_game="poe1", cli_subcommand="trade cluster")
    check_input(enchant, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    check_input(notables, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")

    try:
        notable_a, notable_b = _parse_two_notables(notables)
        solution = solve_front2_back1(
            enchant,
            notable_a,
            notable_b,
            passives=passives,
            sockets=sockets,
            ilvl_max=ilvl_max,
        )
    except ValueError as exc:
        _err_utf8(str(exc))
        ctx.exit(1)
        return

    eff_realm = current_realm(cli_realm=realm)
    league_name, _ = _ct.resolve_trade_league(league, offline_only=True, realm=eff_realm)
    stat_entries = load_local_trade_stats() or []

    query_body: dict[str, Any] | None = None
    direct_url: str | None = None
    encoded_id: str | None = None
    query_param_url: str | None = None
    fallback_query: dict[str, Any] | None = None
    fallback_url: str | None = None
    fallback_param_url: str | None = None

    try:
        if solution.possible:
            raw_q = build_cluster_trade_query(
                solution, stat_entries, status=status, budget=budget, currency=currency, include_back_count_group=True
            )
            direct_url, encoded_id, query_param_url, query_body = encode_trade_search_url(
                league_name, raw_q, realm=eff_realm
            )
        elif solution.reason not in ("two_suffix_notables_impossible", "same_mod_group"):
            raw_fb = build_cluster_trade_query(
                solution, stat_entries, status=status, budget=budget, currency=currency, include_back_count_group=False
            )
            fallback_url, encoded_id, fallback_param_url, fallback_query = encode_trade_search_url(
                league_name, raw_fb, realm=eff_realm
            )
    except ValueError as exc:
        _err_utf8(str(exc))
        ctx.exit(1)
        return

    live_payload: dict[str, Any] | None = None
    if live:
        active_query = query_body or fallback_query
        if active_query is not None:
            live_payload = _run_live_cluster_search(
                ctx,
                query_body=active_query,
                solution=solution,
                stat_entries=stat_entries,
                league=league,
                realm=eff_realm,
                proxy=proxy,
                max_results=max_results,
            )
            if live_payload is None:
                return
            live_league = live_payload["league"]
            if live_league != league_name:
                league_name = live_league
                if query_body is not None:
                    direct_url, encoded_id, query_param_url, query_body = encode_trade_search_url(
                        league_name, query_body, realm=eff_realm
                    )
                if fallback_query is not None:
                    fallback_url, encoded_id, fallback_param_url, fallback_query = encode_trade_search_url(
                        league_name, fallback_query, realm=eff_realm
                    )

    payload: dict[str, Any] = {
        "mode": "live" if live else "zero_http",
        "realm": eff_realm,
        "league": league_name,
        "status": status,
        "possible": solution.possible,
        "solution": solution.to_dict(),
        "query": query_body,
        "url": direct_url,
        "encodedQueryId": encoded_id,
        "queryParamUrl": query_param_url,
        "fallbackQuery": fallback_query,
        "fallbackUrl": fallback_url,
        "fallbackQueryParamUrl": fallback_param_url,
        "live": live_payload,
    }
    if as_json:
        _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
        return
    _emit_text(_format_cluster_markdown(payload), out_path=out_path)
