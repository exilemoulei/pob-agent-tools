"""Shared L2 league resolution (preserving RateLimited/CookieRequired) and listing metadata extraction."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from ..external_data import resolve_current_trade_league
from ..importers import _get_cache_dir
from ..json_io import atomic_write_json
from ..locale_cfg import current_realm, normalize_realm
from .client import TradeClient
from .tw_names import map_tw_league
from .utils import CookieRequired, RateLimited, TradeError


def _league_cache_file(realm: str) -> Path:
    fname = "poe_trade_league.json" if realm == "global" else f"poe_trade_league_{realm}.json"
    return _get_cache_dir() / fname


def _read_cached_league(realm: str, *, ttl_sec: float | None = None) -> str | None:
    path = _league_cache_file(realm)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if ttl_sec is not None and (time.time() - float(data.get("_ts") or 0.0)) >= ttl_sec:
            return None
        val = str(data.get("league") or "").strip()
        return val or None
    except Exception:
        return None


def resolve_trade_league(
    explicit: str | None,
    *,
    realm: str | None = None,
    offline_only: bool = False,
    proxy: str | None = None,
    client: TradeClient | None = None,
) -> tuple[str, str | None]:
    """Resolve active PoE1 trade league and optional warning, re-raising `RateLimited`/`CookieRequired`."""
    eff_realm = normalize_realm(realm) if realm is not None else current_realm()
    if explicit and explicit.strip():
        cleaned = explicit.strip()
        return (map_tw_league(cleaned) if eff_realm == "tw" else cleaned), None

    if eff_realm == "tw":
        if offline_only:
            return (_read_cached_league("tw") or "標準模式"), None
        cached = _read_cached_league("tw", ttl_sec=3600.0)
        if cached and cached != "標準模式":
            return cached, None
        try:
            if client is not None:
                resolved = client.default_league()
                warning = getattr(client, "league_warning", None)
            else:
                with TradeClient(proxy=proxy, realm="tw") as tw_cli:
                    resolved = tw_cli.default_league()
                    warning = tw_cli.league_warning
            try:
                atomic_write_json(_league_cache_file("tw"), {"_ts": time.time(), "league": resolved})
            except OSError:
                pass
            return resolved, warning
        except (RateLimited, CookieRequired):
            raise
        except TradeError as exc:
            return (cached or "標準模式"), f"League lookup failed ({exc}); using {cached or '標準模式'}. Pass --league to choose one."

    if offline_only:
        return (_read_cached_league("global") or "Standard"), None
    try:
        resolved = resolve_current_trade_league(proxy=proxy)
        if resolved:
            return resolved, None
    except Exception:
        pass
    if client is not None:
        return client.default_league(), getattr(client, "league_warning", None)
    with TradeClient(proxy=proxy, realm="global") as cli:
        return cli.default_league(), cli.league_warning


def extract_listing_trade_meta(row: dict[str, Any]) -> dict[str, Any]:
    """Extract cookie-invariant price, `securable`/`in_person` mode, gold fee, and seller info."""
    listing = row.get("listing") if isinstance(row, dict) else None
    listing = listing if isinstance(listing, dict) else {}
    price = listing.get("price") if isinstance(listing.get("price"), dict) else {}
    raw_amount = price.get("amount")
    try:
        amount = float(raw_amount) if raw_amount is not None else None
        if amount is not None and not math.isfinite(amount):
            amount = None
    except (TypeError, ValueError):
        amount = None
    currency = str(price.get("currency") or "").strip().lower() or None
    price_type = str(price.get("type") or "").strip() or None

    raw_fee = listing.get("fee")
    gold_fee = int(raw_fee) if isinstance(raw_fee, (int, float)) and not isinstance(raw_fee, bool) else None
    is_securable = bool(listing.get("hideout_token")) or (
        gold_fee is not None and (gold_fee > 0 or not listing.get("whisper"))
    )
    trade_mode = "securable" if is_securable else "in_person"
    if not is_securable:
        gold_fee = None

    account = listing.get("account") if isinstance(listing.get("account"), dict) else {}
    seller = str(account.get("name") or "").strip()
    return {
        "listingId": str(row.get("id") or ""),
        "priceAmount": amount,
        "priceCurrency": currency,
        "priceType": price_type,
        "tradeMode": trade_mode,
        "goldFee": gold_fee,
        "seller": seller,
        "indexed": listing.get("indexed"),
        "whisper": str(listing.get("whisper") or "").strip() or None,
    }
