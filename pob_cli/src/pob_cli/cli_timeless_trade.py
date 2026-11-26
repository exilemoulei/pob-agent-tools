"""L4 `--trade` CLI orchestration for `pob timeless search --trade` (isolated from `cli_timeless`)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from .cli_common import _echo_stderr_utf8
from .formatters.timeless_trade_md import format_timeless_trade_markdown
from .locale_cfg import current_realm
from .timeless.trade_live import (
    build_urls_only_trade_report,
    execute_timeless_trade_plan,
    resolve_timeless_trade_league,
)
from .timeless.trade_sharder import build_timeless_trade_plan
from .trade_client import CookieRequired, RateLimited, TradeError

_TRADE_ONLY_OPTION_NAMES: tuple[str, ...] = (
    "budget",
    "currency",
    "status",
    "securable",
    "max_per_seller",
    "max_trade_batches",
    "trade_limit",
    "league",
    "realm",
    "proxy",
)


def validate_timeless_trade_options(
    ctx: click.Context,
    *,
    trade: bool,
    budget: float | None,
    status: str,
    securable: bool,
    max_per_seller: int,
    max_trade_batches: int,
    trade_limit: int,
) -> str:
    """Validate `--trade` CLI options before spawning PoB search workers; return effective `status`."""
    if not trade:
        supplied = [
            f"--{name.replace('_', '-')}"
            for name in _TRADE_ONLY_OPTION_NAMES
            if ctx.get_parameter_source(name) not in (None, click.core.ParameterSource.DEFAULT)
        ]
        if supplied:
            raise click.UsageError(f"Trade option(s) {', '.join(supplied)} require --trade.")
        return status.strip().lower()

    eff_status = status.strip().lower()
    if securable:
        src = ctx.get_parameter_source("status")
        if src != click.core.ParameterSource.DEFAULT and eff_status != "securable":
            raise click.ClickException(
                f"--securable conflicts with --status {status!r}; omit --status or pass --status securable."
            )
        eff_status = "securable"
    if budget is not None and budget <= 0:
        raise click.ClickException("--budget must be > 0.")
    if max_per_seller < 0:
        raise click.ClickException("--max-per-seller must be >= 0.")
    if max_trade_batches < 0:
        raise click.ClickException("--max-trade-batches must be >= 0.")
    if trade_limit < 1:
        raise click.ClickException("--trade-limit must be >= 1.")
    return eff_status


def _trade_error_exit_code(err: TradeError) -> int:
    if isinstance(err, CookieRequired):
        return 3
    if isinstance(err, RateLimited):
        return 4
    return 5


def run_timeless_trade_overlay(
    search_result: dict[str, Any],
    *,
    conqueror_override: str | None = None,
    budget: float | None = None,
    currency: str = "divine",
    status: str = "securable",
    max_per_seller: int = 2,
    max_trade_batches: int = 3,
    trade_limit: int = 5,
    league: str | None = None,
    realm: str | None = None,
    proxy: str | None = None,
    pob_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], str, int, str | None]:
    """Shard PoB timeless results, run Early-Stop Trade lookups, and return `(trade_res, md, exit_code, err_msg)`."""
    eff_realm = current_realm(cli_realm=realm)
    offline_env = os.environ.get("POB_OFFLINE", "").strip() == "1"
    offline_fallback = offline_env and max_trade_batches > 0
    eff_batches = 0 if offline_fallback else max_trade_batches
    if offline_fallback:
        _echo_stderr_utf8("[timeless] Warning: POB_OFFLINE=1 is set; degrading --trade to urls_only (0 HTTP calls).")

    stat_key = str(search_result.get("stat") or "CombinedDPS")
    pob_path = Path(pob_dir) if pob_dir else None
    max_shards = None if eff_batches == 0 else max(eff_batches + 2, 5)

    league_warning: str | None = None
    try:
        eff_league, league_warning = resolve_timeless_trade_league(
            league, realm=eff_realm, offline_only=(eff_batches == 0), proxy=proxy
        )
    except TradeError as exc:
        code = _trade_error_exit_code(exc)
        fallback_league = "標準模式" if eff_realm == "tw" else "Standard"
        plan = build_timeless_trade_plan(
            search_result, league=fallback_league, realm=eff_realm, conqueror=conqueror_override,
            status=status, budget=budget, currency=currency, max_shards=None, pob_dir=pob_path,
        )
        trade_res = build_urls_only_trade_report(
            plan, max_trade_batches=0, trade_limit=trade_limit, max_per_seller=max_per_seller,
            budget=budget, currency=currency, reason="trade_error_fallback",
        )
        trade_res["error"] = {"type": type(exc).__name__, "message": str(exc), "exitCode": code}
        return trade_res, format_timeless_trade_markdown(trade_res, stat=stat_key), code, str(exc)

    if league_warning:
        _echo_stderr_utf8(f"[timeless] Warning: {league_warning}")

    try:
        plan = build_timeless_trade_plan(
            search_result, league=eff_league, realm=eff_realm, conqueror=conqueror_override,
            status=status, budget=budget, currency=currency, max_shards=max_shards, pob_dir=pob_path,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    unmatched_dev = plan.get("unmatchedDevotionMods") or []
    if unmatched_dev:
        _echo_stderr_utf8(
            f"[timeless] Warning: unrecognized Militant Faith devotion --mod filter(s) omitted from Trade query: "
            f"{', '.join(unmatched_dev)}"
        )
    unmapped_tw = plan.get("unmappedTwFields") or []
    if unmapped_tw:
        pairs = ", ".join(f"{e['field']}={e['value']!r}" for e in unmapped_tw)
        _echo_stderr_utf8(f"[timeless] Warning: could not localize TW trade field(s) to zh-rTW: {pairs}")

    try:
        trade_res = execute_timeless_trade_plan(
            plan, max_trade_batches=eff_batches, trade_limit=trade_limit, max_per_seller=max_per_seller,
            budget=budget, currency=currency, proxy=proxy, progress=progress,
            offline_fallback=offline_fallback, league_warning=league_warning,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    except TradeError as exc:
        code = _trade_error_exit_code(exc)
        partial = getattr(exc, "partial_trade_result", None)
        if not isinstance(partial, dict):
            partial = build_urls_only_trade_report(
                plan, max_trade_batches=0, trade_limit=trade_limit, max_per_seller=max_per_seller,
                budget=budget, currency=currency, league_warning=league_warning, reason="trade_error_fallback",
            )
        partial["error"] = {"type": type(exc).__name__, "message": str(exc), "exitCode": code}
        return partial, format_timeless_trade_markdown(partial, stat=stat_key), code, str(exc)

    return trade_res, format_timeless_trade_markdown(trade_res, stat=stat_key), 0, None
