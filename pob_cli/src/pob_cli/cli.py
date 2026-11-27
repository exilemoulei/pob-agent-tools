"""Click CLI entry point for `pob` (lazy-loaded for fast cold startup)."""

from __future__ import annotations

import os
import sys

if len(sys.argv) > 1 and sys.argv[1] == "_lua-host":
    from .updater.win_lua_host import main as _lua_host_main

    raise SystemExit(_lua_host_main(sys.argv[2:]))

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

_LAZY_SUBCOMMANDS: dict[str, tuple[str, str, str]] = {
    "serve": (
        ".cli_lifecycle",
        "cmd_serve",
        "Run the PoB daemon persistently in the foreground (ideal for IsDaemon:true agent shells).",
    ),
    "bootstrap": (
        ".cli_lifecycle",
        "cmd_bootstrap",
        "Bootstrap or offline-import the managed headless PoB engine.",
    ),
    "update": (
        ".cli_lifecycle",
        "cmd_update",
        "Check or incrementally update managed PoB & PobTools-zh engine (preserving loaded builds if daemon is alive).",
    ),
    "status": (
        ".cli_lifecycle",
        "cmd_status",
        "Show daemon status, engine versions, update state, and loaded builds across slots.",
    ),
    "lang": (
        ".cli_lifecycle",
        "cmd_lang",
        "Get or set display language (`zh-CN` default Simplified, `zh-TW` fixed Traditional, `en` English).",
    ),
    "stop": (
        ".cli_lifecycle",
        "cmd_stop",
        "Stop the background PoB daemon and release headless processes.",
    ),
    "profile": (
        ".cli_build_io",
        "cmd_profile",
        "List characters on an official PoE1 profile URL or Account#1234 (0 engine startup; supports Intl & CN).",
    ),
    "new": (
        ".cli_build_io",
        "cmd_new",
        "Create a fresh empty build in the target slot with optional class, ascendancy, and level.",
    ),
    "load": (
        ".cli_build_io",
        "cmd_load",
        "Load a build from local .xml/.txt file (or @file), share/profile URL, or Base64 code.",
    ),
    "save": (
        ".cli_build_io",
        "cmd_save",
        "Save the current build to a local .xml file (written by CLI, R11).",
    ),
    "export-code": (
        ".cli_build_io",
        "cmd_export_code",
        "Export current build as a URL-safe Base64 PoB share code.",
    ),
    "switch-main": (
        ".cli_build_io",
        "cmd_switch_main",
        "Switch active passive tree spec, main skill group/part, item/skill/config set, or loadout.",
    ),
    "undo": (
        ".cli_build_io",
        "cmd_undo",
        "Undo the last `--apply` mutation.",
    ),
    "redo": (
        ".cli_build_io",
        "cmd_redo",
        "Redo the last undone mutation.",
    ),
    "summary": (
        ".cli_inspect",
        "cmd_summary",
        "Display comprehensive build identity, offence, minion offence, defence, and warnings.",
    ),
    "diagnose": (
        ".cli_inspect",
        "cmd_diagnose",
        "One-shot comprehensive build diagnosis (and optional dual-build --compare & --ask question grounding).",
    ),
    "ground": (
        ".cli_inspect",
        "cmd_ground",
        "Fast (<15ms, daemon-free) entity & PoE2/legacy-patch trap grounder for arbitrary player prompts.",
    ),
    "audit": (
        ".cli_inspect",
        "cmd_audit",
        "Run build health check and non-destructive 4-part PoB inflation ('water') audit.",
    ),
    "items": (
        ".cli_inspect_parts",
        "cmd_items",
        "List equipped items, inspect a single slot, or dump all equipped items & jewels (`--all --raw`).",
    ),
    "skills": (
        ".cli_inspect_parts",
        "cmd_skills",
        "List all skill groups or inspect gems and per-gem DPS tooltips in a group.",
    ),
    "calc": (
        ".cli_inspect_parts",
        "cmd_calc",
        "Search PoB Calcs breakdown tables and formulas by one or more English/Chinese keywords.",
    ),
    "interactions": (
        ".cli_interactions",
        "cmd_interactions",
        "Inspect ModParser flag compatibility, conditional premises, and PoB uncomputed red mods.",
    ),
    "item": (
        ".cli_mutate_items",
        "grp_item",
        "Inspect, evaluate, edit, or mutate equipped items, flasks, and anoints.",
    ),
    "skill": (
        ".cli_mutate_skills",
        "skill",
        "Modify, add, or replace skill groups and support/active gems.",
    ),
    "tree": (
        ".cli_tree",
        "grp_tree",
        "Inspect passive tree (`pob tree [slot]`), prune (`prune`), diff (`diff`), mutate (`change`), or switch class (`set-class`).",
    ),
    "config": (
        ".cli_mutate",
        "grp_config",
        "Inspect or modify PoB ConfigTab parameters and customMods.",
    ),
    "batch": (
        ".cli_optimize",
        "cmd_batch",
        "Execute multiple item/skill/tree/config/build mutations in a single snapshot transaction.",
    ),
    "sweep": (
        ".cli_optimize",
        "cmd_sweep",
        "Evaluate N named build variants in one warm daemon worker with fault isolation and 0-residue rollback.",
    ),
    "matrix": (
        ".cli_optimize",
        "cmd_matrix",
        "Run multi-scenario what-if comparison in one atomic 0-residue transaction (default: Original/Pinnacle/Uber/FlasksOff).",
    ),
    "rank-nodes": (
        ".cli_optimize",
        "cmd_rank_nodes",
        "Rank unallocated passive tree nodes by ROI per point (`pathPower`) and total gain (`power`).",
    ),
    "rank-gems": (
        ".cli_optimize",
        "cmd_rank_gems",
        "Rank support/active candidate gems for a skill group by DPS gain.",
    ),
    "compare": (
        ".cli_optimize",
        "cmd_compare",
        "Compare target build vs reference build across 3 baselines + tree/skill/equipment diffs.",
    ),
    "borrow": (
        ".cli_optimize",
        "cmd_borrow",
        "Borrow items and/or a skill group from reference slot into target slot and compute exact delta.",
    ),
    "data": (
        ".cli_data",
        "cmd_data",
        "Query local 0ms PoB Lua game/tree data & 0.033s tick calculator (`gem|unique|mod|base|translate|tree|tick`, daemon-free).",
    ),
    "poedb": (
        ".cli_research",
        "cmd_poedb",
        "Query poedb.tw CN/US autocomplete index, properties, version history, and mod pool weights (cached).",
    ),
    "price": (
        ".cli_data",
        "cmd_price",
        "Query poe.ninja economy prices (15m local cache, 52 overview categories, optional --detailed/--variant).",
    ),
    "meta": (
        ".cli_data",
        "cmd_meta",
        "Query poe.ninja league meta snapshot (total builds, ascendancy & main skill share).",
    ),
    "wiki": (
        ".cli_research",
        "cmd_wiki",
        "Query poewiki.net MediaWiki API for mechanics, uniques, and keystones (24h local cache).",
    ),
    "research": (
        ".cli_research",
        "cmd_research",
        "Concurrent dual-source research via poedb.tw + poewiki.net (partial-success resilient).",
    ),
    "self-update": (
        ".cli_self_update",
        "cmd_self_update",
        "Update pob itself to the latest release (standalone builds; others get their update command).",
    ),
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
    "map-regex": (
        ".cli_map_regex",
        "cmd_map_regex",
        "Generate <=250-char 0-false-positive T16/T17 map filter regex tailored to your PoB build.",
    ),
    "help": (
        ".cli_help",
        "cmd_help",
        "Show compact 1-screen CLI cheat-sheet (`--brief`) or help for a subcommand.",
    ),
}

_HIDDEN_LAZY_SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "_daemon": (".cli_lifecycle", "cmd_internal_daemon"),
    "_release-check": (".cli_self_update", "cmd_release_check"),
    "rune": (".cli_mutate_items", "cmd_rune_trap"),
    "instill": (".cli_mutate_items", "cmd_instill_trap"),
}


class _RootGroup(GuardrailGroup):
    """GuardrailGroup that also resolves hidden internal/trap commands lazily without listing them in --help."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        spec = _HIDDEN_LAZY_SUBCOMMANDS.get(cmd_name)
        if spec is None:
            return None
        import importlib

        mod_path, attr_name = spec
        mod = importlib.import_module(mod_path, __package__)
        loaded: click.Command = getattr(mod, attr_name)
        self.commands[cmd_name] = loaded
        return loaded


@click.group(
    cls=_RootGroup,
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
    from .cli_self_update import maybe_show_update_notice

    maybe_show_update_notice(ctx)


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
