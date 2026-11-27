"""Compatible `pob tree` Click command group (inspect, `change`, `set-class`, `prune`, and future `diff`)."""

from __future__ import annotations

import inspect
import sys
from typing import Any

import click

from .cli_common import (
    _SLOT_SLUG_TYPE,
    _default_target,
    _emit_json,
    _emit_text,
    _output_options,
    _target_option,
    invoke_daemon,
)


def _normalize_positional_slot(slot: str | None, target: str, target_explicit: bool) -> str:
    if not slot:
        return target
    s = slot.strip().lower()
    if s in ("current", "active") and not target_explicit:
        return _default_target()
    return _SLOT_SLUG_TYPE.convert(s, None, None)


def _invoke_tree_daemon(action: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Dispatch daemon call respecting legacy test monkeypatches on `pob_cli.cli_mutate.invoke_daemon`."""
    mut_mod = sys.modules.get("pob_cli.cli_mutate")
    if mut_mod is not None:
        mut_fn = getattr(mut_mod, "invoke_daemon", None)
        if callable(mut_fn) and mut_fn is not invoke_daemon:
            return mut_fn(action, params, **kwargs) if kwargs else mut_fn(action, params)
    return invoke_daemon(action, params, **kwargs)


def _format_mutation_report(title: str, res: dict[str, Any]) -> str:
    from .formatters import format_mutation_report

    return format_mutation_report(title, res)


class _TreeCompatGroup(click.Group):
    """Click Group dispatching registered subcommands (`change`, `set-class`, `prune`, `diff`)
    while preserving backward-compatible `pob tree [slot] [--full] [--json] ...` inspection."""

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args:
            first = args[0]
            if first in self.commands:
                return super().resolve_command(ctx, args)
            # Treat any non-subcommand positional token (e.g. `current`, `ref`) as `[slot]` for tree inspect.
            return "_inspect", _cmd_tree_inspect_pos, list(args)
        return super().resolve_command(ctx, args)


def _resolve_tree_subcommand_opts(
    ctx: click.Context,
    *,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> tuple[str, bool, bool, str | None]:
    """Propagate CLI-provided `pob tree` group options into subcommands when not overridden."""
    from click.core import ParameterSource

    parent_opts = (ctx.obj.get("tree_group_opts") if isinstance(ctx.obj, dict) else None) or {}
    if "target" in parent_opts and ctx.get_parameter_source("target") != ParameterSource.COMMANDLINE:
        target = parent_opts["target"]
    if "as_json" in parent_opts and ctx.get_parameter_source("as_json") != ParameterSource.COMMANDLINE:
        as_json = bool(parent_opts["as_json"])
    if "compact" in parent_opts and ctx.get_parameter_source("compact") != ParameterSource.COMMANDLINE:
        compact = bool(parent_opts["compact"])
    if "out_path" in parent_opts and ctx.get_parameter_source("out_path") != ParameterSource.COMMANDLINE:
        out_path = parent_opts["out_path"]
    return target, as_json, compact, out_path


def _dispatch_tree_inspect(
    *,
    searches: tuple[str, ...],
    node_types: tuple[str, ...],
    allocated: bool | None,
    max_cost: int | None,
    show_path: bool,
    masteries: bool,
    socket_radius: str | None,
    limit: int,
    full: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    from .cli_inspect_parts import _run_tree_inspect

    eff_limit = 0 if full else limit
    extra_kw = {"limit": eff_limit} if "limit" in inspect.signature(_run_tree_inspect).parameters else {}
    _run_tree_inspect(
        searches=searches,
        node_types=node_types,
        allocated=allocated,
        max_cost=max_cost,
        show_path=show_path,
        masteries=masteries,
        socket_radius=socket_radius,
        target=target,
        as_json=as_json,
        compact=compact,
        out_path=out_path,
        **extra_kw,
    )


@click.group("tree", cls=_TreeCompatGroup, invoke_without_command=True)
@click.option("--search", "searches", multiple=True, help="Search passive tree nodes by EN/ZH name, stat, ID, or '#1' (repeatable).")
@click.option("--type", "node_types", multiple=True, help="Filter by node type (Keystone, Notable, Mastery, Socket, Ascendancy; repeatable).")
@click.option("--allocated/--unallocated", "allocated", default=None, help="Filter to allocated or unallocated nodes.")
@click.option("--max-cost", "max_cost", type=int, default=None, help="Maximum shortest-path point cost.")
@click.option("--show-path", "show_path", is_flag=True, help="Include shortest allocation path node IDs.")
@click.option("--masteries", "masteries", is_flag=True, help="Include full masteryEffects IDs and text without truncation.")
@click.option("--socket-radius", "socket_radius", type=str, default=None, help="Inspect nodes within jewel radii of socket (ID, '#1', or name).")
@click.option("--limit", type=click.IntRange(min=0), default=100, show_default=True, help="Max nodes returned (0 = unlimited).")
@click.option("--full", "full", is_flag=True, help="Return full passive tree matches without truncation (limit=0).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def grp_tree(
    ctx: click.Context,
    searches: tuple[str, ...],
    node_types: tuple[str, ...],
    allocated: bool | None,
    max_cost: int | None,
    show_path: bool,
    masteries: bool,
    socket_radius: str | None,
    limit: int,
    full: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Inspect passive tree (`pob tree [slot] [--search ...]`), prune branches (`prune`), mutate nodes (`change`), or switch class (`set-class`)."""
    if ctx.invoked_subcommand is None:
        _dispatch_tree_inspect(
            searches=searches,
            node_types=node_types,
            allocated=allocated,
            max_cost=max_cost,
            show_path=show_path,
            masteries=masteries,
            socket_radius=socket_radius,
            limit=limit,
            full=full,
            target=target,
            as_json=as_json,
            compact=compact,
            out_path=out_path,
        )
        return

    from click.core import ParameterSource

    ctx.ensure_object(dict)
    tree_opts: dict[str, Any] = {}
    for pname, pval in (
        ("target", target),
        ("as_json", as_json),
        ("compact", compact),
        ("out_path", out_path),
        ("searches", searches),
        ("node_types", node_types),
        ("allocated", allocated),
        ("max_cost", max_cost),
        ("show_path", show_path),
        ("masteries", masteries),
        ("socket_radius", socket_radius),
        ("limit", limit),
        ("full", full),
    ):
        if ctx.get_parameter_source(pname) == ParameterSource.COMMANDLINE:
            tree_opts[pname] = pval
    ctx.obj["tree_group_opts"] = tree_opts


@click.command("_inspect", hidden=True)
@click.argument("slot_pos", required=False, default=None)
@click.option("--search", "searches", multiple=True)
@click.option("--type", "node_types", multiple=True)
@click.option("--allocated/--unallocated", "allocated", default=None)
@click.option("--max-cost", "max_cost", type=int, default=None)
@click.option("--show-path", "show_path", is_flag=True)
@click.option("--masteries", "masteries", is_flag=True)
@click.option("--socket-radius", "socket_radius", type=str, default=None)
@click.option("--limit", type=click.IntRange(min=0), default=100)
@click.option("--full", "full", is_flag=True)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True)
@_output_options
@click.pass_context
def _cmd_tree_inspect_pos(
    ctx: click.Context,
    slot_pos: str | None,
    searches: tuple[str, ...],
    node_types: tuple[str, ...],
    allocated: bool | None,
    max_cost: int | None,
    show_path: bool,
    masteries: bool,
    socket_radius: str | None,
    limit: int,
    full: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    from click.core import ParameterSource

    parent_opts = (ctx.obj.get("tree_group_opts") if isinstance(ctx.obj, dict) else None) or {}
    target_explicit = (
        ctx.get_parameter_source("target") == ParameterSource.COMMANDLINE or "target" in parent_opts
    )
    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    eff_target = _normalize_positional_slot(slot_pos, target, target_explicit)
    eff_searches = searches or parent_opts.get("searches") or ()
    eff_types = node_types or parent_opts.get("node_types") or ()
    eff_alloc = allocated if allocated is not None else parent_opts.get("allocated")
    eff_max_cost = max_cost if max_cost is not None else parent_opts.get("max_cost")
    eff_show_path = bool(show_path or parent_opts.get("show_path"))
    eff_masteries = bool(masteries or parent_opts.get("masteries"))
    eff_sock_rad = socket_radius if socket_radius is not None else parent_opts.get("socket_radius")
    eff_limit = limit if ctx.get_parameter_source("limit") == ParameterSource.COMMANDLINE else parent_opts.get("limit", limit)
    eff_full = bool(full or parent_opts.get("full"))
    _dispatch_tree_inspect(
        searches=eff_searches,
        node_types=eff_types,
        allocated=eff_alloc,
        max_cost=eff_max_cost,
        show_path=eff_show_path,
        masteries=eff_masteries,
        socket_radius=eff_sock_rad,
        limit=eff_limit,
        full=eff_full,
        target=eff_target,
        as_json=as_json,
        compact=compact,
        out_path=out_path,
    )


@grp_tree.command("prune")
@click.argument("slot_pos", required=False, default=None)
@click.option("--top", type=int, default=15, show_default=True, help="Maximum safe/unsafe branches to show.")
@click.option(
    "--max-branch-pts",
    "max_branch_pts",
    type=int,
    default=5,
    show_default=True,
    help="Maximum passive points in a candidate removable branch.",
)
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["dps", "ehp", "pts"], case_sensitive=False),
    default="dps",
    show_default=True,
    help="Sort safe branches by lowest ΔDPS%/pt (`dps`), lowest ΔEHP%/pt (`ehp`), or most points freed (`pts`).",
)
@click.option(
    "--include-unsafe",
    "include_unsafe",
    is_flag=True,
    default=False,
    help="Show unsafe branches that violate attribute/resist/reservation/cliff guardrails in Markdown.",
)
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_prune(
    ctx: click.Context,
    slot_pos: str | None,
    top: int,
    max_branch_pts: int,
    sort_by: str,
    include_unsafe: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Rank connectivity-preserving passive branches by lowest DPS/EHP loss per freed point (<0.5s)."""
    from click.core import ParameterSource

    from .formatters.tree_prune_md import format_tree_prune_markdown

    parent_opts = (ctx.obj.get("tree_group_opts") if isinstance(ctx.obj, dict) else None) or {}
    target_explicit = (
        ctx.get_parameter_source("target") == ParameterSource.COMMANDLINE or "target" in parent_opts
    )
    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    eff_target = _normalize_positional_slot(slot_pos, target, target_explicit)
    res = _invoke_tree_daemon(
        "tree_prune",
        {
            "target": eff_target,
            "top": top,
            "max_branch_pts": max_branch_pts,
            "sort": sort_by.lower(),
            "include_unsafe": include_unsafe,
        },
        timeout=90.0,
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_tree_prune_markdown(res, include_unsafe=include_unsafe), out_path=out_path)


@grp_tree.command("change")
@click.option(
    "--allocate",
    multiple=True,
    help="Node IDs, names, or Mastery `<nodeId>:<effectId>` to allocate (repeatable or comma-separated).",
)
@click.option(
    "--deallocate",
    multiple=True,
    help="Node IDs or names to deallocate (repeatable or comma-separated).",
)
@click.option(
    "--allow-cascade/--no-allow-cascade",
    "allow_cascade",
    default=True,
    show_default=True,
    help="Allow deallocations that cascade-orphan >3 downstream passive nodes.",
)
@click.option("--apply", is_flag=True, help="Persist passive tree changes into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_change(
    ctx: click.Context,
    allocate: tuple[str, ...] | str | None,
    deallocate: tuple[str, ...] | str | None,
    allow_cascade: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Allocate or deallocate passive tree nodes idempotently (R12)."""
    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    alloc_chunks = (allocate,) if isinstance(allocate, str) else (allocate or ())
    dealloc_chunks = (deallocate,) if isinstance(deallocate, str) else (deallocate or ())
    alloc_list = [x.strip() for chunk in alloc_chunks for x in chunk.split(",") if x.strip()]
    dealloc_list = [x.strip() for chunk in dealloc_chunks for x in chunk.split(",") if x.strip()]
    res = _invoke_tree_daemon(
        "tree_change",
        {
            "target": target,
            "allocate": alloc_list,
            "deallocate": dealloc_list,
            "allow_cascade": allow_cascade,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(_format_mutation_report("Passive Tree Change", res), out_path=out_path)


@grp_tree.command("set-class")
@click.argument("class_name", required=False)
@click.argument("ascendancy", required=False)
@click.option("--secondary-asc", "secondary_asc", help="Secondary / Bloodline ascendancy name (or 'None' to clear).")
@click.option("--apply", is_flag=True, help="Persist class/ascendancy change into build.")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_set_class(
    ctx: click.Context,
    class_name: str | None,
    ascendancy: str | None,
    secondary_asc: str | None,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Switch character class, primary ascendancy, and/or secondary (Bloodline) ascendancy."""
    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    if not class_name and not ascendancy and not secondary_asc:
        raise click.UsageError("Specify class_name, ascendancy, and/or --secondary-asc.")

    from .guardrails import check_poe2_class_or_ascendancy

    check_poe2_class_or_ascendancy(class_name, field="class")
    check_poe2_class_or_ascendancy(ascendancy, field="ascendancy")
    check_poe2_class_or_ascendancy(secondary_asc, field="secondary_ascendancy")

    res = _invoke_tree_daemon(
        "tree_set_class",
        {
            "target": target,
            "class_name": class_name,
            "ascendancy_name": ascendancy,
            "secondary_ascendancy_name": secondary_asc,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    detail = res.get("detail") or {}
    changes = detail.get("changes") or []
    title = f"Set Class / Ascendancy ({'; '.join(changes) or 'no-op'})"
    _emit_text(_format_mutation_report(title, res), out_path=out_path)


@grp_tree.command("diff")
@_target_option(allow_slot_alias=True)
@click.option(
    "--against",
    "against",
    type=_SLOT_SLUG_TYPE,
    default="ref",
    show_default=True,
    help="Reference build slot slug to diff against (ignored when --spec-a/--spec-b are passed).",
)
@click.option("--spec-a", "spec_a", default=None, help="First `<Spec>` index or title within `--target` for intra-build diff.")
@click.option("--spec-b", "spec_b", default=None, help="Second `<Spec>` index or title within `--target` for intra-build diff.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_diff(
    ctx: click.Context,
    target: str,
    against: str,
    spec_a: str | None,
    spec_b: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Structured passive tree diff (points, ascendancy, keystones, wheels, masteries, clusters & sockets)."""
    from .formatters.tree_diff_md import format_tree_diff_markdown

    base_slot, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    base_slot = _normalize_positional_slot(base_slot, _default_target(), False)  # `current` / `active`
    if bool(spec_a) != bool(spec_b):
        raise click.UsageError("Provide both `--spec-a` and `--spec-b` together for multi-spec diff.")

    params: dict[str, Any] = {"target": base_slot}
    if spec_a and spec_b:
        params["spec_a"] = spec_a.strip()
        params["spec_b"] = spec_b.strip()
    else:
        params["ref_target"] = against

    res = _invoke_tree_daemon("tree_diff", params, timeout=90.0)
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    _emit_text(format_tree_diff_markdown(res), out_path=out_path)


from .cli_tree_tattoo import register_tree_tattoo_commands as _register_tree_tattoo_commands  # noqa: E402
from .cli_tree_wheels import register_tree_wheel_commands as _register_tree_wheel_commands  # noqa: E402

_register_tree_tattoo_commands(grp_tree)
_register_tree_wheel_commands(grp_tree)
