"""Click subcommand `pob tree rank-wheels` (mounted onto `grp_tree` in `cli_tree.py`)."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import _default_target, _emit_json, _emit_text, _output_options, _target_option


@click.command("rank-wheels")
@click.argument("slot_pos", required=False, default=None)
@click.option("--top", type=int, default=12, show_default=True, help="Maximum top wheels to display.")
@click.option("--max-dist", "max_dist", type=int, default=12, show_default=True, help="Maximum BFS travel hops from current allocated tree.")
@click.option("--max-pts", "max_pts", type=int, default=10, show_default=True, help="Maximum total passive points (travel + wheel + mastery).")
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["dps", "ehp"], case_sensitive=False),
    default="dps",
    show_default=True,
    help="Sort real wheels by per-point ΔDPS%/pt (`dps`) or ΔEHP%/pt (`ehp`).",
)
@click.option(
    "--all-tags",
    "all_tags",
    is_flag=True,
    default=False,
    help="Bypass static main-skill tag pre-filtering and evaluate all reachable wheels.",
)
@click.option(
    "--no-proxy-sockets",
    "no_proxy_sockets",
    is_flag=True,
    default=False,
    help="Omit the isolated `[Proxy Benchmark]` socket & outer cluster ROI section.",
)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_rank_wheels(
    ctx: click.Context,
    slot_pos: str | None,
    top: int,
    max_dist: int,
    max_pts: int,
    sort_by: str,
    all_tags: bool,
    no_proxy_sockets: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Rank reachable passive wheels (travel + notable + best unused mastery) by per-point ROI (<2s)."""
    from click.core import ParameterSource

    from .cli_tree import (
        _invoke_tree_daemon,
        _normalize_positional_slot,
        _resolve_tree_subcommand_opts,
    )
    from .formatters.wheel_rank_md import format_wheel_rank_markdown

    parent_opts = (ctx.obj.get("tree_group_opts") if isinstance(ctx.obj, dict) else None) or {}
    target_explicit = (
        ctx.get_parameter_source("target") == ParameterSource.COMMANDLINE or "target" in parent_opts
    )
    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    eff_target = _normalize_positional_slot(slot_pos, target or _default_target(), target_explicit)
    res: dict[str, Any] = _invoke_tree_daemon(
        "tree_rank_wheels",
        {
            "target": eff_target,
            "top": top,
            "max_dist": max_dist,
            "max_pts": max_pts,
            "sort": sort_by.lower(),
            "all_tags": all_tags,
            "include_proxy_sockets": not no_proxy_sockets,
        },
        timeout=90.0,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_wheel_rank_markdown(res), out_path=out_path)


def register_tree_wheel_commands(tree_group: click.Group) -> None:
    """Attach `rank-wheels` subcommand onto `pob tree`."""
    tree_group.add_command(cmd_tree_rank_wheels)
