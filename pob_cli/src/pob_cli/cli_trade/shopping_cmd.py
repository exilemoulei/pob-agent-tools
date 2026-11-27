"""`pob trade shopping-list` (L0 zero-HTTP full-build links) Click CLI command."""

from __future__ import annotations

import click

from ..cli_common import _emit_json, _emit_text, _output_options
from ..errors import BootstrapMissingError, CrossGameHallucinationError
from ..formatters.shopping_md import format_shopping_list_markdown
from ..locale_cfg import current_realm
from ..trade_eval.shopping_list import build_shopping_list
from ..trade_query import VALID_STATUSES, load_local_trade_stats
from ..updater import format_stderr_update_alert
from .common import (
    _TARGET_SLOT_TYPE,
    _default_target_slot,
    _err_utf8,
    _prepend_warning,
)
from .other_cmds import trade_group


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
@click.option("--json", "as_json", is_flag=True, help="Output RFC-8259 JSON.")
@_output_options
def cmd_trade_shopping_list(
    target: str,
    league: str | None,
    realm: str | None,
    status: str,
    include_gems: bool,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Generate zero-HTTP official Trade links for every equipped item, flask, jewel, and key gem."""
    import pob_cli.cli_trade as _ct

    ctx = click.get_current_context()
    eff_realm = current_realm(cli_realm=realm)
    league_name, _ = _ct.resolve_trade_league(league, offline_only=True, realm=eff_realm)

    try:
        items_res = _ct._daemon_call_safe("items", {"target": target, "allRaw": True})
        skills_res = _ct._daemon_call_safe("skills", {"target": target})
    except CrossGameHallucinationError:
        raise
    except BootstrapMissingError as exc:
        _err_utf8(format_stderr_update_alert(exc.update_status) or str(exc))
        ctx.exit(5)
        return
    except Exception as exc:
        _err_utf8(f"Error: {exc}")
        ctx.exit(5)
        return

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
