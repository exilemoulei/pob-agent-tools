"""Click CLI entry point for `pob` (lazy-loaded for fast cold startup)."""

from __future__ import annotations

import os

import click

from . import __version__
from .cli_common import (
    DAEMON_FAILURE_EXIT_CODE,
    GuardrailGroup,
    LazyGroup,
    _daemon_call,
    _default_target,
    _echo_stderr_utf8,
    _echo_utf8,
    _emit_json,
    _emit_text,
    _merge_keep_tokens,
    _output_options,
    _prepare_load_source,
    _prepend_warning,
    _resolve_output_flags,
    _target_option,
)
from .cli_data import register_data_commands
from .cli_inspect import register_inspect_commands
from .cli_lifecycle import register_lifecycle_commands
from .cli_mutate import register_mutate_commands
from .cli_mutate_items import register_item_commands
from .cli_optimize import register_optimize_commands
from .cli_self_update import maybe_show_update_notice, register_self_update_commands

_LAZY_SUBCOMMANDS: dict[str, tuple[str, str, str]] = {
    "trade": (
        ".cli_trade",
        "trade_group",
        "Search official PoE1 trade (read-only), preview Harvest swaps, and verify in PoB.",
    ),
    "bench": (
        ".cli_trade",
        "bench_group",
        "Look up PoE1 crafting bench modifiers (ModMaster.lua authoritative + cost overlay).",
    ),
    "timeless": (
        ".cli_timeless",
        "timeless_group",
        "Find optimal timeless jewel seeds verified by headless PoB.",
    ),
    "hook": (
        ".cli_hook",
        "hook_group",
        "Deterministic lifecycle hooks (pre-prompt, pre-tool, verify-stop) for agent harnesses.",
    ),
}


@click.group(
    cls=GuardrailGroup,
    lazy_subcommands=_LAZY_SUBCOMMANDS,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "-V", "--version", prog_name="pob")
@click.option("--offline", is_flag=True, help="Skip GitHub engine update checks (sets POB_OFFLINE=1).")
@click.option("--compact", is_flag=True, default=False, help="Emit compact single-line JSON for --json commands.")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write output to UTF-8 (no BOM) file.",
)
@click.pass_context
def main(ctx: click.Context, offline: bool, compact: bool, out_path: str | None) -> None:
    """Headless Path of Building (PoB) CLI for build inspection, what-if testing, and optimization."""
    if offline:
        os.environ["POB_OFFLINE"] = "1"
    ctx.ensure_object(dict)
    ctx.obj["compact"] = compact
    ctx.obj["out"] = out_path
    maybe_show_update_notice(ctx)


register_lifecycle_commands(main)
register_inspect_commands(main)
register_item_commands(main)
register_mutate_commands(main)
register_optimize_commands(main)
register_data_commands(main)
register_self_update_commands(main)

__all__ = [
    "DAEMON_FAILURE_EXIT_CODE",
    "_LAZY_SUBCOMMANDS",
    "GuardrailGroup",
    "LazyGroup",
    "_daemon_call",
    "_default_target",
    "_echo_stderr_utf8",
    "_echo_utf8",
    "_emit_json",
    "_emit_text",
    "_merge_keep_tokens",
    "_output_options",
    "_prepare_load_source",
    "_prepend_warning",
    "_resolve_output_flags",
    "_target_option",
    "main",
]


if __name__ == "__main__":
    main()
