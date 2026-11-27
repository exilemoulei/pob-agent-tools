"""`pob trade shopping-list` (L0 zero-HTTP) and `pob trade appraise` (L1 3-tier valuation) CLI commands."""

from __future__ import annotations

from typing import Any

import click

from ..cli_common import _emit_json, _emit_text, _output_options
from ..errors import BootstrapMissingError, CrossGameHallucinationError
from ..formatters.shopping_md import format_appraise_markdown, format_shopping_list_markdown
from ..locale_cfg import current_realm
from ..trade_client import TradeError
from ..trade_eval.appraise import appraise_build
from ..trade_eval.shopping_list import build_shopping_list
from ..trade_query import VALID_STATUSES, load_local_trade_stats
from ..updater import format_stderr_update_alert
from .common import (
    _TARGET_SLOT_TYPE,
    _default_target_slot,
    _err_utf8,
    _prepend_warning,
    _trade_error,
)
from .other_cmds import trade_group


def _load_build_snapshot(ctx: click.Context, target: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    import pob_cli.cli_trade as _ct

    try:
        items_res = _ct._daemon_call_safe("items", {"target": target, "allRaw": True})
        skills_res = _ct._daemon_call_safe("skills", {"target": target})
    except CrossGameHallucinationError:
        raise
    except BootstrapMissingError as exc:
        _err_utf8(format_stderr_update_alert(exc.update_status) or str(exc))
        ctx.exit(5)
        return None
    except Exception as exc:
        _err_utf8(f"Error: {exc}")
        ctx.exit(5)
        return None
    return items_res, skills_res


def _run_appraise_and_emit(
    ctx: click.Context,
    *,
    target: str,
    league: str | None,
    realm: str | None,
    status: str,
    include_gems: bool,
    max_live_queries: int,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    import pob_cli.cli_trade as _ct

    loaded = _load_build_snapshot(ctx, target)
    if loaded is None:
        return
    items_res, skills_res = loaded
    eff_realm = current_realm(cli_realm=realm)
    stat_entries = load_local_trade_stats() or []
    offline_league = max_live_queries <= 0

    if max_live_queries > 0:
        with _ct.TradeClient(proxy=proxy, realm=eff_realm) as client:
            try:
                league_name, league_warn = _ct.resolve_trade_league(
                    league,
                    offline_only=False,
                    proxy=proxy,
                    client=client,
                    realm=eff_realm,
                )
            except TradeError as exc:
                _trade_error(ctx, exc)
                return
            if league_warn:
                _err_utf8(f"Warning: {league_warn}")
            result = appraise_build(
                items_res,
                skills_res,
                stat_entries,
                league=league_name,
                realm=eff_realm,
                status=status,
                include_gems=include_gems,
                max_live_queries=max_live_queries,
                client=client,
                proxy=proxy,
            )
    else:
        try:
            league_name, league_warn = _ct.resolve_trade_league(
                league,
                offline_only=offline_league,
                proxy=proxy,
                realm=eff_realm,
            )
        except TradeError as exc:
            _trade_error(ctx, exc)
            return
        if league_warn:
            _err_utf8(f"Warning: {league_warn}")
        result = appraise_build(
            items_res,
            skills_res,
            stat_entries,
            league=league_name,
            realm=eff_realm,
            status=status,
            include_gems=include_gems,
            max_live_queries=0,
            client=None,
            proxy=proxy,
        )

    update_warn = items_res.get("updateWarning") if isinstance(items_res, dict) else None
    if update_warn:
        result["updateWarning"] = update_warn
    if as_json:
        _emit_json(result, compact=compact, out_path=out_path, sanitize=True)
        return
    _emit_text(_prepend_warning(format_appraise_markdown(result), update_warn), out_path=out_path)


@trade_group.command("shopping-list")
@click.option("--target", type=_TARGET_SLOT_TYPE, default=_default_target_slot, help="Build slot slug.")
@click.option("--league", help="PoE1 trade league (defaults to cached/default league with 0 HTTP calls).")
@click.option(
    "--realm",
    type=click.Choice(["global", "tw"], case_sensitive=False),
    default=None,
    help="Trade server realm (`global` pathofexile.com or `tw` pathofexile.tw).",
)
@click.option(
    "--status",
    type=click.Choice(sorted(VALID_STATUSES)),
    default="securable",
    show_default=True,
    help="Default trade listing status in generated links (`securable` = Faustus Instant Buyout).",
)
@click.option(
    "--include-gems/--no-gems",
    default=True,
    show_default=True,
    help="Include non-vendor high-value gems (21/20, Awakened, Transfigured, Exceptional).",
)
@click.option("--live", is_flag=True, help="Upgrade to L1 3-tier appraisal (poe.ninja + bounded live P25 pricing).")
@click.option(
    "--max-live-queries",
    type=click.IntRange(min=0, max=20),
    default=8,
    show_default=True,
    help="Maximum Tier B live Trade API queries when --live is enabled.",
)
@click.option("--proxy", help="HTTP/SOCKS5 proxy URL (used only with --live).")
@click.option("--json", "as_json", is_flag=True, help="Output RFC-8259 JSON.")
@_output_options
def cmd_trade_shopping_list(
    target: str,
    league: str | None,
    realm: str | None,
    status: str,
    include_gems: bool,
    live: bool,
    max_live_queries: int,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Generate zero-HTTP official Trade links for every equipped item, flask, jewel, and key gem."""
    import pob_cli.cli_trade as _ct

    ctx = click.get_current_context()
    if live:
        _run_appraise_and_emit(
            ctx,
            target=target,
            league=league,
            realm=realm,
            status=status,
            include_gems=include_gems,
            max_live_queries=max_live_queries,
            proxy=proxy,
            as_json=as_json,
            compact=compact,
            out_path=out_path,
        )
        return

    loaded = _load_build_snapshot(ctx, target)
    if loaded is None:
        return
    items_res, skills_res = loaded
    eff_realm = current_realm(cli_realm=realm)
    league_name, _ = _ct.resolve_trade_league(league, offline_only=True, realm=eff_realm)
    stat_entries = load_local_trade_stats() or []
    result = build_shopping_list(
        items_res,
        skills_res,
        stat_entries,
        league=league_name,
        realm=eff_realm,
        status=status,
        include_gems=include_gems,
    )
    update_warn = items_res.get("updateWarning") if isinstance(items_res, dict) else None
    if update_warn:
        result["updateWarning"] = update_warn

    if as_json:
        _emit_json(result, compact=compact, out_path=out_path, sanitize=True)
        return
    _emit_text(_prepend_warning(format_shopping_list_markdown(result), update_warn), out_path=out_path)


@trade_group.command("appraise")
@click.option("--target", type=_TARGET_SLOT_TYPE, default=_default_target_slot, help="Build slot slug.")
@click.option("--league", help="PoE1 trade league (defaults to current primary challenge league).")
@click.option(
    "--realm",
    type=click.Choice(["global", "tw"], case_sensitive=False),
    default=None,
    help="Trade server realm (`global` pathofexile.com or `tw` pathofexile.tw).",
)
@click.option(
    "--status",
    type=click.Choice(sorted(VALID_STATUSES)),
    default="securable",
    show_default=True,
    help="Listing status for Tier B live checks and L0 links (`securable` = Instant Buyout).",
)
@click.option(
    "--max-live-queries",
    type=click.IntRange(min=0, max=20),
    default=8,
    show_default=True,
    help="Maximum Tier B live Trade API queries (0 = poe.ninja-only; no live trade queries).",
)
@click.option(
    "--include-gems/--no-gems",
    default=True,
    show_default=True,
    help="Include non-vendor high-value gems in valuation.",
)
@click.option("--proxy", help="HTTP/SOCKS5 proxy URL (overrides POB_PROXY).")
@click.option("--json", "as_json", is_flag=True, help="Output RFC-8259 JSON.")
@_output_options
def cmd_trade_appraise(
    target: str,
    league: str | None,
    realm: str | None,
    status: str,
    max_live_queries: int,
    include_gems: bool,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """3-tier PoE1 build valuation (Tier A poe.ninja floor, Tier B live P25, Tier C crafted rares)."""
    ctx = click.get_current_context()
    _run_appraise_and_emit(
        ctx,
        target=target,
        league=league,
        realm=realm,
        status=status,
        include_gems=include_gems,
        max_live_queries=max_live_queries,
        proxy=proxy,
        as_json=as_json,
        compact=compact,
        out_path=out_path,
    )
