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
from .guardrails import GuardrailScope, check_input, check_poe2_timeless_jewel
from .timeless import STAT_KEYS, search, set_idle_priority


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


def _seed_preview(seeds: list[int], limit: int = 6) -> str:
    if not seeds:
        return "-"
    head = ", ".join(str(s) for s in seeds[:limit])
    return f"{head} ..." if len(seeds) > limit else head


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


def _format_search_markdown(res: dict[str, Any], stat: str, top: int) -> str:
    base = res.get("baseline") or {}
    outcomes = res.get("outcomes") or []
    est = res.get("estimate")
    shown = outcomes if est else outcomes[:top]
    extra_stat = stat not in ("CombinedDPS", "TotalEHP", "Life")

    if extra_stat:
        headers = ["#", stat, "vs now", "Combined DPS", "vs now", "Total EHP", "vs now", "Life", "Seeds", "Seed numbers"]
        rows: list[list[Any]] = [[
            "now",
            fmt_num(base.get(stat)),
            "",
            fmt_num(base.get("CombinedDPS")),
            "",
            fmt_num(base.get("TotalEHP")),
            "",
            fmt_num(base.get("Life")),
            "-",
            "socket as loaded",
        ]]
        for idx, outcome in enumerate(shown, 1):
            s = outcome.get("stats") or {}
            seeds = outcome.get("seeds") or []
            rows.append([
                idx,
                fmt_num(s.get(stat)),
                _pct(s.get(stat), base.get(stat)),
                fmt_num(s.get("CombinedDPS")),
                _pct(s.get("CombinedDPS"), base.get("CombinedDPS")),
                fmt_num(s.get("TotalEHP")),
                _pct(s.get("TotalEHP"), base.get("TotalEHP")),
                fmt_num(s.get("Life")),
                len(seeds),
                _seed_preview(seeds),
            ])
    else:
        headers = ["#", "Combined DPS", "vs now", "Total EHP", "vs now", "Life", "Seeds", "Seed numbers"]
        rows = [[
            "now",
            fmt_num(base.get("CombinedDPS")),
            "",
            fmt_num(base.get("TotalEHP")),
            "",
            fmt_num(base.get("Life")),
            "-",
            "socket as loaded",
        ]]
        for idx, outcome in enumerate(shown, 1):
            s = outcome.get("stats") or {}
            seeds = outcome.get("seeds") or []
            rows.append([
                idx,
                fmt_num(s.get("CombinedDPS")),
                _pct(s.get("CombinedDPS"), base.get("CombinedDPS")),
                fmt_num(s.get("TotalEHP")),
                _pct(s.get("TotalEHP"), base.get("TotalEHP")),
                fmt_num(s.get("Life")),
                len(seeds),
                _seed_preview(seeds),
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
        render_table(headers, rows),
    ]

    for other in res.get("removed") or []:
        lines.append(
            f"\n> `{other.get('jewel')}` at socket `{other.get('socket')}` was taken out for this search: "
            "the game allows one Historic jewel."
        )

    if est and est.get("method") == "weightShortlist":
        lines.append(
            f"\n> Shortlist: {calculated:,} of {group_count:,} seed groups, picked by PoB's timeless weight finder, "
            "were calculated; the rest were not, so a better seed may exist. `--exact` calculates all."
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
    type=int,
    help="Allocated jewel socket node ID (from 'pob timeless list').",
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
@_target_option(allow_slot_alias=False)
@_output_options
@click.option("--json", "as_json", is_flag=True, help="Output structured JSON.")
def cmd_timeless_search(
    jewel: str,
    socket: int,
    conqueror: str | None,
    stat: str,
    mods: tuple[str, ...],
    top: int,
    workers: int | None,
    exact: bool,
    idle: bool,
    target: str,
    compact: bool,
    out_path: str | None,
    as_json: bool,
) -> None:
    """Find the best seeds of a timeless jewel at an allocated jewel socket."""
    if top < 1:
        raise click.ClickException("--top must be >= 1.")
    if workers is not None and workers < 1:
        raise click.ClickException("--workers must be >= 1.")

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

    try:
        res = search(
            code,
            exported.get("xml") or "",
            socket,
            jewel,
            conqueror=conqueror,
            mods=mods,
            stat=stat,
            top=top,
            workers=workers,
            exact=exact,
            progress=log,
            pob_dir=exported.get("pobDir"),
        )
    except CrossGameHallucinationError:
        raise
    except (DaemonRequestError, ValueError) as exc:
        raise click.ClickException(_err_text(exc)) from exc
    except (BridgeError, RuntimeError, OSError, zlib.error) as exc:
        _raise_engine_failure(exc)

    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(_format_search_markdown(res, stat, top), out_path=out_path)
