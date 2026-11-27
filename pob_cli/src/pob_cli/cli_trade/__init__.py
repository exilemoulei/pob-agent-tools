"""`pob trade` & `pob bench` — official read-only PoE1 trade search and crafting bench CLI."""

from __future__ import annotations

from ..daemon import call_daemon
from ..external_data import fetch_currency_chaos_rates, resolve_current_trade_league
from ..trade_client import TradeClient, seller_url
from ..trade_query import load_trade_stats
from .common import (
    _TARGET_SLOT_TYPE,
    SLOT_HINT,
    _build_label_from_info,
    _cached_league_offline,
    _conversion_payload,
    _daemon_call_safe,
    _default_target_slot,
    _echo_utf8,
    _err_utf8,
    _is_known_slot,
    _is_securable_row,
    _listing_trade_meta,
    _prepend_warning,
    _price_of,
    _read_current_item,
    _resolve_league,
    _TargetSlotType,
    _trade_error,
    _weighted_sum_of,
)
from .fresh_bargain import (
    _chaos_rates,
    _fetch_fresh,
    _market_chaos,
    _price_chaos,
    _search_rows,
)
from .other_cmds import (
    bench_group,
    cmd_bench_list,
    cmd_bench_suggest,
    cmd_trade_convert,
    cmd_trade_leagues,
    cmd_trade_stats,
    trade_group,
)
from .plan_cmd import cmd_trade_plan
from .search_cmd import cmd_trade_search

__all__ = [
    "SLOT_HINT",
    "_TARGET_SLOT_TYPE",
    "TradeClient",
    "_TargetSlotType",
    "_build_label_from_info",
    "_cached_league_offline",
    "_chaos_rates",
    "_conversion_payload",
    "_daemon_call_safe",
    "_default_target_slot",
    "_echo_utf8",
    "_err_utf8",
    "_fetch_fresh",
    "_is_known_slot",
    "_is_securable_row",
    "_listing_trade_meta",
    "_market_chaos",
    "_prepend_warning",
    "_price_chaos",
    "_price_of",
    "_read_current_item",
    "_resolve_league",
    "_search_rows",
    "_trade_error",
    "_weighted_sum_of",
    "bench_group",
    "call_daemon",
    "cmd_bench_list",
    "cmd_bench_suggest",
    "cmd_trade_convert",
    "cmd_trade_leagues",
    "cmd_trade_plan",
    "cmd_trade_search",
    "cmd_trade_stats",
    "fetch_currency_chaos_rates",
    "load_trade_stats",
    "resolve_current_trade_league",
    "seller_url",
    "trade_group",
]
