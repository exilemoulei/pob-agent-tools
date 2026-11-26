"""Shared helpers for `pob trade` and `pob bench` CLI commands."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import click

from ..cli_common import _default_target as _default_target_slot  # noqa: F401  (re-exported for trade cmds)
from ..cli_common import _SlotSlugType as _TargetSlotType
from ..compat import ensure_utf8
from ..daemon import BootstrapMissingError
from ..errors import CrossGameHallucinationError
from ..formatters import render_update_warning
from ..importers import _get_cache_dir
from ..trade_client import CookieRequired, NoResults, RateLimited, TradeClient, TradeError
from ..trade_item import parse_item_text, sanitize_text
from ..trade_query import CATEGORY_BY_SLOT, pob_slot_for_slot
from ..updater import format_stderr_update_alert

SLOT_HINT = (
    "helmet, body_armour, gloves, boots, shield, quiver, amulet, belt, "
    "ring1, ring2, ring3, weapon1, weapon2, bow, wand, claw, dagger, "
    "sceptre, staff, flask1..5, jewel"
)

_PSEUDO_SUM_RE = re.compile(r"^\s*Sum:\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)


_TARGET_SLOT_TYPE = _TargetSlotType()


def _echo_utf8(text: str) -> None:
    """Write UTF-8 safely to stdout on Windows consoles."""
    ensure_utf8(sys.stdout)
    click.echo(text)


def _err_utf8(text: str) -> None:
    """Write UTF-8 safely to stderr on Windows consoles."""
    ensure_utf8(sys.stderr)
    click.echo(text, err=True)


def _trade_error(ctx: click.Context, error: TradeError) -> None:
    _err_utf8(str(error))
    if isinstance(error, CookieRequired):
        ctx.exit(3)
    if isinstance(error, RateLimited):
        ctx.exit(4)
    if isinstance(error, NoResults):
        ctx.exit(2)
    ctx.exit(5)


def _is_known_slot(slot: str | None) -> bool:
    if not slot:
        return False
    norm = slot.strip().lower().replace("-", "_").replace(" ", "_")
    compact = norm.replace("_", "")
    return (
        norm in CATEGORY_BY_SLOT
        or compact in CATEGORY_BY_SLOT
        or pob_slot_for_slot(slot) is not None
    )


def _cached_league_offline() -> str | None:
    cache_file = _get_cache_dir() / "poe_trade_league.json"
    if not cache_file.is_file():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        league = str(data.get("league") or "").strip()
        return league or None
    except Exception:
        return None


def _resolve_league(
    explicit: str | None,
    *,
    offline_only: bool = False,
    proxy: str | None = None,
    client: TradeClient | None = None,
) -> str:
    import pob_cli.cli_trade as _ct

    if explicit and explicit.strip():
        return explicit.strip()
    if offline_only:
        return _cached_league_offline() or "Standard"
    try:
        league = _ct.resolve_current_trade_league(proxy=proxy)
        if league:
            return league
    except Exception:
        pass
    try:
        if client is not None:
            return client.default_league()
        with _ct.TradeClient(proxy=proxy) as fallback_client:
            return fallback_client.default_league()
    except TradeError:
        return "Standard"


def _daemon_call_safe(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 150.0,
) -> dict[str, Any]:
    """Call local PoB daemon, surfacing BootstrapMissingError cleanly."""
    import pob_cli.cli_trade as _ct

    return _ct.call_daemon(action, params, auto_start=auto_start, timeout=timeout)


def _build_label_from_info(info: dict[str, Any] | None, target: str) -> str:
    if isinstance(info, dict) and info:
        parts = [
            str(info.get("classNameZh") or info.get("className") or "").strip(),
            str(info.get("ascendClassNameZh") or info.get("ascendClassName") or "").strip(),
        ]
        label = " ".join(part for part in parts if part)
        level = info.get("level")
        if level:
            label = f"{label} (level {level})" if label else f"level {level}"
        if label:
            return label
    return f"slot:{target}"


def _read_current_item(slot: str, target: str) -> tuple[Any, str | None, str | None, str | None]:
    import pob_cli.cli_trade as _ct

    try:
        payload = _ct._daemon_call_safe("items", {"target": target, "slot": slot, "raw": True})
    except CrossGameHallucinationError:
        raise
    except BootstrapMissingError as exc:
        alert = format_stderr_update_alert(exc.update_status) or str(exc)
        return None, sanitize_text(alert, 300), None, None
    except Exception as exc:
        return None, sanitize_text(exc, 300), None, None
    warn = payload.get("updateWarning") if isinstance(payload, dict) else None
    label = _build_label_from_info(payload.get("buildInfo") if isinstance(payload, dict) else None, target)
    raw = payload.get("raw") if isinstance(payload, dict) else None
    if not raw and isinstance(payload, dict) and isinstance(payload.get("equippedSlots"), list):
        slot_low = slot.strip().lower()
        sock_idx = int(slot_low[1:]) if slot_low.startswith("#") and slot_low[1:].isdigit() else None
        for entry in payload["equippedSlots"]:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("slotName") or "").strip().lower() == slot_low or (
                sock_idx is not None and entry.get("socketIndex") == sock_idx
            ):
                raw = entry.get("raw")
                break
    if not raw:
        return None, None, warn, label
    try:
        return parse_item_text(raw), None, warn, label
    except ValueError as exc:
        return None, sanitize_text(exc, 300), warn, label


def _price_of(row: dict[str, Any]) -> tuple[float | None, str | None]:
    price = (row.get("listing") or {}).get("price") or {}
    amount = price.get("amount")
    try:
        parsed_amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        parsed_amount = None
    currency = price.get("currency")
    return parsed_amount, (str(currency) if currency else None)


def _listing_trade_meta(row: dict[str, Any]) -> tuple[str, int | None, str | None]:
    """Extract cookie-invariant `(trade_mode, gold_fee, price_type)` from a trade fetch row.

    Anonymous responses strip `hideout_token` and online Merchant's Tab sellers may still
    carry `whisper`, whereas traditional in-person listings never carry a Gold `fee`.
    """
    listing = row.get("listing") if isinstance(row, dict) else None
    listing = listing if isinstance(listing, dict) else {}
    raw_fee = listing.get("fee")
    gold_fee = int(raw_fee) if isinstance(raw_fee, (int, float)) and not isinstance(raw_fee, bool) else None
    is_securable = bool(listing.get("hideout_token")) or (
        gold_fee is not None and (gold_fee > 0 or not listing.get("whisper"))
    )
    trade_mode = "securable" if is_securable else "in_person"
    if not is_securable:
        gold_fee = None
    price_obj = listing.get("price")
    price_type = str(price_obj.get("type") or "") or None if isinstance(price_obj, dict) else None
    return trade_mode, gold_fee, price_type


def _is_securable_row(row: dict[str, Any]) -> bool:
    return _listing_trade_meta(row)[0] == "securable"


def _weighted_sum_of(raw_item: dict[str, Any]) -> float | None:
    """Extract the official PoE1 Trade weighted sum (``pseudoMods: ["Sum: 32"]``) if present."""
    pseudo_mods = raw_item.get("pseudoMods")
    if not isinstance(pseudo_mods, list):
        return None
    for entry in pseudo_mods:
        text = entry.get("description") if isinstance(entry, dict) else str(entry)
        if not text:
            continue
        m = _PSEUDO_SUM_RE.match(text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _conversion_payload(plan: Any) -> dict[str, Any]:
    if not plan.conversions:
        return {"enabled": False}
    return {
        "enabled": True,
        "targets": [
            {
                "group": conversion.group,
                "target": conversion.target,
                "elements": conversion.elements,
                "cost": conversion.cost,
                "patch": conversion.patch,
                "rerolled": True,
            }
            for conversion in plan.conversions
        ],
        "note": (
            "Harvest keeps the tier but re-rolls the value inside the tier; "
            "the listing value is a best case, not a guarantee."
        ),
    }


def _prepend_warning(body: str, update_warning: str | None) -> str:
    banner = render_update_warning(update_warning)
    return f"{banner}{body}" if banner else body
