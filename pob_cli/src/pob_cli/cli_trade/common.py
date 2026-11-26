"""Shared helpers for `pob trade` and `pob bench` CLI commands."""

from __future__ import annotations

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
from ..trade_client import (
    CookieRequired,
    NoResults,
    RateLimited,
    TradeError,
)
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


class TradeUsageGroup(click.Group):
    """`pob trade`: Click usage errors exit 1, since trade's exit 2 means "no results"."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as exc:
            exc.exit_code = 1
            raise

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.UsageError as exc:
            exc.exit_code = 1
            raise


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
