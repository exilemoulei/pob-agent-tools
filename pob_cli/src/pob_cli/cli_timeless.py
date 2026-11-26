"""`pob timeless` — timeless jewel seed search CLI (every reported number verified by headless PoB)."""

from __future__ import annotations

import math
import zlib
from typing import Any

import click

from . import cli_common as _cli_mod
from .bridge_client import BridgeError
from .cli_common import (
    _daemon_call,
    _echo_stderr_utf8,
    _emit_json,
    _emit_text,
    _output_options,
    _resolve_output_flags,
    _target_option,
)
from .errors import CrossGameHallucinationError, DaemonRequestError
from .formatters import fmt_num, render_table
from .game_data import fold_han
from .guardrails import GuardrailScope, check_input, check_poe2_timeless_jewel
from .timeless import _JEWEL_ZH_ALIASES, STAT_KEYS, search, set_idle_priority
from .timeless.multi_conqueror import search_multi_conqueror
from .timeless.worker import _check_putrid_seed


def parse_socket_spec(raw: str | int) -> int | list[int] | str:
    """Parse `--socket` into a single `int`, a `list[int]` (`'61419,6230'`), or `'all'`."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise ValueError("--socket cannot be empty; pass a socket node ID, comma-separated IDs, or 'all'.")
    if text.lower() == "all":
        return "all"
    if "," not in text:
        try:
            return int(text)
        except ValueError as exc:
            raise ValueError(
                f"Invalid --socket value {raw!r}; expected an integer socket ID, comma-separated IDs, or 'all'."
            ) from exc
    parts = [p.strip() for p in text.split(",")]
    if any(not p for p in parts):
        raise ValueError(f"Invalid --socket list {raw!r}; empty entry in comma-separated socket IDs.")
    parsed: list[int] = []
    for p in parts:
        try:
            val = int(p)
        except ValueError as exc:
            raise ValueError(
                f"Invalid socket ID {p!r} in {raw!r}; expected integer socket IDs or 'all'."
            ) from exc
        if val not in parsed:
            parsed.append(val)
    return parsed


class _SocketOptionType(click.ParamType):
    name = "socket"

    def convert(self, value: Any, param: Any, ctx: Any) -> int | list[int] | str:
        if isinstance(value, list):
            return value
        try:
            return parse_socket_spec(value)
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


_SOCKET_OPTION_TYPE = _SocketOptionType()

_ABYSS_TIMELESS_JEWELS: frozenset[str] = frozenset(
    {
        "festering vengeance", "extinguishing grasp", "baleful dominion",
        "destructive aspiration", "reclaimed malevolence",
    }
)


def _is_abyss_timeless_jewel(jewel: str) -> bool:
    cleaned = str(jewel or "").strip()
    mapped = _JEWEL_ZH_ALIASES.get(cleaned) or _JEWEL_ZH_ALIASES.get(fold_han(cleaned)) or cleaned
    return mapped.strip().lower() in _ABYSS_TIMELESS_JEWELS


def _daemon_exit_code() -> int:
    return int(getattr(_cli_mod, "DAEMON_FAILURE_EXIT_CODE", 5))


def _err_text(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


def _raise_engine_failure(exc: BaseException) -> None:
    err = click.ClickException(f"PoB timeless engine failure: {_err_text(exc)}")
    err.exit_code = _daemon_exit_code()
    raise err from exc


def _pct(new: Any, old: Any) -> str:
    """Format finite percentage delta between two PoB stats, or '-' when non-finite/unavailable."""
    if isinstance(new, bool) or isinstance(old, bool):
        return "-"
    if not isinstance(new, (int, float)) or not isinstance(old, (int, float)):
        return "-"
    if not (math.isfinite(new) and math.isfinite(old)) or old == 0:
        return "-"
    return f"{((new - old) / abs(old)) * 100.0:+.1f}%"


def _seed_preview(seeds: list[int], limit: int = 6, conqueror: str | None = None) -> str:
    if not seeds:
        return "-"
    head = ", ".join(str(s) for s in seeds[:limit])
    preview = f"{head} ..." if len(seeds) > limit else head
    return f"[{conqueror}] {preview}" if conqueror else preview


def _notable_preview(entries: list[dict[str, Any]], limit: int = 6) -> str:
    if not entries:
        return "none"
    parts = [f"{n.get('nameZh') or n.get('name')} ({n.get('id')})" for n in entries[:limit]]
    return f"{', '.join(parts)} ..." if len(entries) > limit else ", ".join(parts)


def _format_radius_summary_line(res: dict[str, Any]) -> str | None:
    radius = res.get("radiusSummary")
    if not isinstance(radius, dict):
        return None
    alloc = list(radius.get("allocatedNotables") or [])
    unalloc = list(radius.get("unallocatedNotables") or [])
    if not alloc and not unalloc:
        return None
    return (
        f"> Radius notables — allocated (0 extra points, {len(alloc)}): {_notable_preview(alloc)} | "
        f"unallocated in radius ({len(unalloc)}): {_notable_preview(unalloc)}"
    )


def _format_list_markdown(info: dict[str, Any], target: str) -> str:
    jewels = info.get("jewels") or []
    sockets = info.get("sockets") or []
    lines = [
        "### Timeless Jewels",
        render_table(
            ["Jewel", "Local Name", "Seeds", "Conquerors", "Search"],
            [
                [
                    j.get("name"),
                    j.get("nameLocal"),
                    fmt_num(j.get("seeds")),
                    ", ".join(j.get("conquerors") or []),
                    "grouped" if j.get("grouped") else "every seed",
                ]
                for j in jewels
            ],
        ),
        f"\n### Allocated Jewel Sockets (`{target}`)",
        render_table(
            ["--socket", "Near", "Local Name"],
            [[s.get("nodeId"), s.get("label"), s.get("labelLocal")] for s in sockets],
        ),
    ]
    return "\n".join(lines)


def _format_multi_search_markdown(res: dict[str, Any], stat: str, top: int) -> str:
    base = res.get("baseline") or {}
    top_seeds = (res.get("topSeeds") or [])[:top]
    extra_stat = stat not in ("CombinedDPS", "TotalEHP", "Life")
    if extra_stat:
        headers = ["#", "Socket", stat, "vs now", "Combined DPS", "vs now", "Total EHP", "vs now", "Life", "Seeds", "Seed numbers"]
        rows: list[list[Any]] = [
            ["now", "-", fmt_num(base.get(stat)), "", fmt_num(base.get("CombinedDPS")), "", fmt_num(base.get("TotalEHP")), "", fmt_num(base.get("Life")), "-", "build as loaded"]
        ]
        for idx, item in enumerate(top_seeds, 1):
            s = item.get("stats") or {}
            seeds = item.get("seeds") or []
            sock_txt = f"{item.get('socket')} ({item['socketLabel']})" if item.get("socketLabel") else str(item.get("socket"))
            rows.append([
                idx, sock_txt, fmt_num(s.get(stat)), _pct(s.get(stat), base.get(stat)),
                fmt_num(s.get("CombinedDPS")), _pct(s.get("CombinedDPS"), base.get("CombinedDPS")),
                fmt_num(s.get("TotalEHP")), _pct(s.get("TotalEHP"), base.get("TotalEHP")),
                fmt_num(s.get("Life")), len(seeds), _seed_preview(seeds),
            ])
    else:
        headers = ["#", "Socket", "Combined DPS", "vs now", "Total EHP", "vs now", "Life", "Seeds", "Seed numbers"]
        rows = [
            ["now", "-", fmt_num(base.get("CombinedDPS")), "", fmt_num(base.get("TotalEHP")), "", fmt_num(base.get("Life")), "-", "build as loaded"]
        ]
        for idx, item in enumerate(top_seeds, 1):
            s = item.get("stats") or {}
            seeds = item.get("seeds") or []
            sock_txt = f"{item.get('socket')} ({item['socketLabel']})" if item.get("socketLabel") else str(item.get("socket"))
            rows.append([
                idx, sock_txt, fmt_num(s.get("CombinedDPS")), _pct(s.get("CombinedDPS"), base.get("CombinedDPS")),
                fmt_num(s.get("TotalEHP")), _pct(s.get("TotalEHP"), base.get("TotalEHP")),
                fmt_num(s.get("Life")), len(seeds), _seed_preview(seeds),
            ])

    sockets = res.get("sockets") or []
    sock_str = ", ".join(str(s) for s in sockets)
    lines = [
        f"### Timeless Multi-Socket Search: `{res.get('jewel')}` ({res.get('conqueror')}) across sockets "
        f"`[{sock_str}]` - {int(res.get('calculatedTotal') or 0):,} groups calculated on "
        f"{res.get('workers') or 1} engines in {res.get('seconds')}s, ranked by `{stat}`",
        render_table(headers, rows),
    ]
    for sock_res in res.get("socketResults") or []:
        lines.append("\n" + _format_search_markdown(sock_res, stat, min(top, 5)))
    return "\n".join(lines)


def _format_search_markdown(res: dict[str, Any], stat: str, top: int) -> str:
    if "socketResults" in res:
        return _format_multi_search_markdown(res, stat, top)

    base = res.get("baseline") or {}
    outcomes = res.get("outcomes") or []
    est = res.get("estimate")
    shown = outcomes if est else outcomes[:top]
    extra_stat = stat not in ("CombinedDPS", "TotalEHP", "Life")

    if extra_stat:
        headers = ["#", stat, "vs now", "Combined DPS", "vs now", "Total EHP", "vs now", "Life", "Seeds", "Seed numbers"]
        rows: list[list[Any]] = [[
            "now", fmt_num(base.get(stat)), "", fmt_num(base.get("CombinedDPS")), "",
            fmt_num(base.get("TotalEHP")), "", fmt_num(base.get("Life")), "-", "socket as loaded",
        ]]
        for idx, outcome in enumerate(shown, 1):
            s = outcome.get("stats") or {}
            seeds = outcome.get("seeds") or []
            rows.append([
                idx, fmt_num(s.get(stat)), _pct(s.get(stat), base.get(stat)),
                fmt_num(s.get("CombinedDPS")), _pct(s.get("CombinedDPS"), base.get("CombinedDPS")),
                fmt_num(s.get("TotalEHP")), _pct(s.get("TotalEHP"), base.get("TotalEHP")),
                fmt_num(s.get("Life")), len(seeds), _seed_preview(seeds, conqueror=outcome.get("conqueror")),
            ])
    else:
        headers = ["#", "Combined DPS", "vs now", "Total EHP", "vs now", "Life", "Seeds", "Seed numbers"]
        rows = [[
            "now", fmt_num(base.get("CombinedDPS")), "", fmt_num(base.get("TotalEHP")), "",
            fmt_num(base.get("Life")), "-", "socket as loaded",
        ]]
        for idx, outcome in enumerate(shown, 1):
            s = outcome.get("stats") or {}
            seeds = outcome.get("seeds") or []
            rows.append([
                idx, fmt_num(s.get("CombinedDPS")), _pct(s.get("CombinedDPS"), base.get("CombinedDPS")),
                fmt_num(s.get("TotalEHP")), _pct(s.get("TotalEHP"), base.get("TotalEHP")),
                fmt_num(s.get("Life")), len(seeds), _seed_preview(seeds, conqueror=outcome.get("conqueror")),
            ])

    seed_count = int(res.get("seedCount") or 0)
    group_count = int(res.get("groupCount") or 0)
    calculated = int(res.get("calculated") or 0)
    workers = res.get("workers") or 1
    seconds = res.get("seconds")

    lines = [
        f"### Timeless Search: `{res.get('jewel')}` ({res.get('conqueror')}) at socket `{res.get('socket')}` - "
        f"{seed_count:,} seeds in {group_count:,} groups, {calculated:,} calculated on "
        f"{workers} engines in {seconds}s, ranked by `{stat}`",
    ]
    radius_line = _format_radius_summary_line(res)
    if radius_line:
        lines.append(radius_line)
    lines.append(render_table(headers, rows))

    for other in res.get("removed") or []:
        lines.append(
            f"\n> `{other.get('jewel')}` at socket `{other.get('socket')}` was taken out for this search: "
            "the game allows one Historic jewel."
        )

    if est and est.get("method") == "weightShortlist":
        unique_groups = int(est.get("groupsTotal") or group_count)
        lines.append(
            f"\n> Ranked top {calculated} of {unique_groups} PRNG groups via weight shortlist "
            "(pass --exact only for exhaustive scan)."
        )
    elif est:
        max_err = est.get("maxError")
        if isinstance(max_err, (int, float)) and not isinstance(max_err, bool) and math.isfinite(max_err):
            err_text = f"{max_err * 100:.2f}%" if est.get("relative") else f"{max_err:.3g}"
        else:
            err_text = fmt_num(max_err)
        lines.append(
            f"\n> Rows are real PoB results. The other {max(0, group_count - calculated):,} groups were not "
            f"calculated: an estimate fitted on the calculated ones (error allowed for: {err_text}) puts none of them "
            f"in the top {top}. That is an estimate, not a proof; `--exact` calculates everything."
        )
    else:
        lines.append(
            f"\n> Exact search: all {calculated:,} of {group_count:,} seed groups were calculated by headless PoB."
        )

    check = res.get("selfCheck") or {}
    mismatches = check.get("mismatches") or []
    if mismatches:
        lines.append(
            f"\n> [WARN] Seeds {mismatches} differ from their group; please report this build and jewel."
        )

    return "\n".join(lines)


@click.group("timeless")
def timeless_group() -> None:
    """Find optimal timeless jewel seeds verified by headless PoB."""


@timeless_group.command("list")
@_target_option(allow_slot_alias=True)
@_output_options
@click.option("--json", "as_json", is_flag=True, help="Output structured JSON.")
def cmd_timeless_list(
    target: str,
    compact: bool,
    out_path: str | None,
    as_json: bool,
) -> None:
    """List timeless jewel types and the build's allocated jewel sockets."""
    try:
        info = _daemon_call("timeless_list", {"target": target})
    except CrossGameHallucinationError:
        raise
    except (DaemonRequestError, ValueError) as exc:
        raise click.ClickException(_err_text(exc)) from exc
    except (BridgeError, RuntimeError, OSError, zlib.error) as exc:
        _raise_engine_failure(exc)

    if as_json:
        _emit_json(info, compact=compact, out_path=out_path)
        return
    _emit_text(_format_list_markdown(info, target), out_path=out_path)


@timeless_group.command("search")
@click.option("--jewel", required=True, help="Timeless jewel name (English or Chinese).")
@click.option(
    "--socket",
    required=True,
    type=_SOCKET_OPTION_TYPE,
    help="Allocated jewel socket node ID, comma-separated IDs (e.g. 61419,6230), or 'all'.",
)
@click.option("--conqueror", default=None, help="Conqueror / devotion variant (defaults to first).")
@click.option(
    "--stat",
    default="CombinedDPS",
    show_default=True,
    type=click.Choice(STAT_KEYS, case_sensitive=True),
    help="Stat to rank seeds by.",
)
@click.option("--mod", "mods", multiple=True, help="Extra explicit line on the jewel (repeatable).")
@click.option("--top", default=15, show_default=True, type=int, help="Number of top seed groups to return.")
@click.option(
    "--workers",
    default=None,
    type=int,
    help="Parallel headless PoB engines (default: half cores, max 4).",
)
@click.option("--exact", is_flag=True, help="Evaluate every seed group without estimation.")
@click.option("--idle", is_flag=True, help="Lower CPU priority for worker processes.")
@click.option("--trade", is_flag=True, help="Lookup live in-stock Trade buyout listings for top ranked seeds.")
@click.option("--budget", type=float, default=None, help="Maximum Trade listing price (used with --trade).")
@click.option(
    "--currency",
    type=click.Choice(["divine", "chaos"], case_sensitive=False),
    default="divine",
    show_default=True,
    help="Currency for --budget (used with --trade).",
)
@click.option(
    "--status",
    type=click.Choice(["securable", "available", "online", "any"], case_sensitive=False),
    default="securable",
    show_default=True,
    help="Trade listing status filter (used with --trade).",
)
@click.option("--securable", is_flag=True, help="Shortcut for --status securable (used with --trade).")
@click.option(
    "--max-per-seller",
    type=int,
    default=2,
    show_default=True,
    help="Max Trade listings retained per seller account (0 = unlimited; used with --trade).",
)
@click.option(
    "--max-trade-batches",
    type=int,
    default=3,
    show_default=True,
    help="Max Trade API shard batches to execute (0 = offline pre-sharded URLs only).",
)
@click.option(
    "--trade-limit",
    type=int,
    default=5,
    show_default=True,
    help="Max in-stock matches to return and early-stop target across batches.",
)
@click.option("--league", default=None, help="PoE1 trade league (defaults to active challenge league).")
@click.option(
    "--realm",
    type=click.Choice(["global", "tw"], case_sensitive=False),
    default=None,
    help="Trade realm (`global` pathofexile.com or `tw` pathofexile.tw).",
)
@click.option("--proxy", default=None, help="HTTP/SOCKS5 proxy URL for Trade API requests.")
@_target_option(allow_slot_alias=False)
@_output_options
@click.option("--json", "as_json", is_flag=True, help="Output structured JSON.")
def cmd_timeless_search(
    jewel: str,
    socket: int | list[int] | str,
    conqueror: str | None,
    stat: str,
    mods: tuple[str, ...],
    top: int,
    workers: int | None,
    exact: bool,
    idle: bool,
    trade: bool,
    budget: float | None,
    currency: str,
    status: str,
    securable: bool,
    max_per_seller: int,
    max_trade_batches: int,
    trade_limit: int,
    league: str | None,
    realm: str | None,
    proxy: str | None,
    target: str,
    compact: bool,
    out_path: str | None,
    as_json: bool,
) -> None:
    """Find the best seeds of a timeless jewel at one or more allocated jewel sockets."""
    if top < 1:
        raise click.ClickException("--top must be >= 1.")
    if workers is not None and workers < 1:
        raise click.ClickException("--workers must be >= 1.")

    ctx = click.get_current_context()
    from .cli_timeless_trade import validate_timeless_trade_options

    eff_status = validate_timeless_trade_options(
        ctx,
        trade=trade,
        budget=budget,
        status=status,
        securable=securable,
        max_per_seller=max_per_seller,
        max_trade_batches=max_trade_batches,
        trade_limit=trade_limit,
    )

    try:
        _check_putrid_seed(jewel)
    except ValueError as exc:
        raise click.ClickException(_err_text(exc)) from exc
    check_poe2_timeless_jewel(jewel, expected_game="poe1")
    check_input(jewel, scope=GuardrailScope.DATA_QUERY, expected_game="poe1")
    for mod in mods:
        check_input(mod, scope=GuardrailScope.ITEM_RAW, expected_game="poe1")

    try:
        exported = _daemon_call("export", {"target": target, "include_xml": True})
    except CrossGameHallucinationError:
        raise
    except (DaemonRequestError, ValueError) as exc:
        raise click.ClickException(_err_text(exc)) from exc
    except (BridgeError, RuntimeError, OSError, zlib.error) as exc:
        _raise_engine_failure(exc)

    code = str(exported.get("code") or "")
    if not code:
        raise click.ClickException(f"No build loaded in target slot '{target}'. Load a build first (`pob load ...`).")

    if idle:
        try:
            set_idle_priority()
        except OSError as exc:
            _echo_stderr_utf8(f"[timeless] Warning: could not lower CPU priority: {exc}")

    _, eff_out = _resolve_output_flags(compact=compact, out_path=out_path)
    log = None if (as_json or eff_out is not None) else (lambda msg: _echo_stderr_utf8(f"[timeless] {msg}"))

    if _is_abyss_timeless_jewel(jewel):
        click.echo(
            "[timeless] Note: Abyss timeless jewels evaluate ~7,900 ungrouped seeds and may take ~1-3 minutes "
            "(recommend specifying a single --socket <id>).",
            err=True,
        )

    is_any_conq = bool(conqueror and conqueror.strip().lower() in ("any", "*"))
    eval_top = max(top, 30) if (trade and is_any_conq) else top

    try:
        res = search_multi_conqueror(
            code,
            exported.get("xml") or "",
            socket,
            jewel,
            conqueror=conqueror,
            mods=mods,
            stat=stat,
            top=eval_top,
            workers=workers,
            exact=exact,
            progress=log,
            pob_dir=exported.get("pobDir"),
            search_fn=search,
        )
    except CrossGameHallucinationError:
        raise
    except (DaemonRequestError, ValueError) as exc:
        raise click.ClickException(_err_text(exc)) from exc
    except (BridgeError, RuntimeError, OSError, zlib.error) as exc:
        _raise_engine_failure(exc)

    trade_md: str | None = None
    trade_exit = 0
    trade_err: str | None = None
    if trade:
        from .cli_timeless_trade import run_timeless_trade_overlay

        trade_res, trade_md, trade_exit, trade_err = run_timeless_trade_overlay(
            res,
            conqueror_override=conqueror if is_any_conq else None,
            budget=budget,
            currency=currency,
            status=eff_status,
            max_per_seller=max_per_seller,
            max_trade_batches=max_trade_batches,
            trade_limit=trade_limit,
            league=league,
            realm=realm,
            proxy=proxy,
            pob_dir=exported.get("pobDir"),
            progress=log,
        )
        res = {**res, "trade": trade_res}

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
    else:
        body = _format_search_markdown(res, stat, top)
        if trade_md:
            body = f"{body}\n\n{trade_md}"
        _emit_text(body, out_path=out_path)

    if trade_exit != 0:
        if trade_err:
            _echo_stderr_utf8(trade_err)
        ctx.exit(trade_exit)
