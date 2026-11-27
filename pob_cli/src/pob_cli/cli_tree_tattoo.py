"""Click subcommand `pob tree tattoo` (mounted onto `grp_tree` in `cli_tree.py`)."""

from __future__ import annotations

from typing import Any

import click

from .cli_common import _emit_json, _emit_text, _output_options, _target_option
from .formatters.primitives import render_table, strip_pob_escapes


def _format_tattoo_inspect_md(res: dict[str, Any]) -> str:
    nid = res.get("nodeId")
    base_nm = strip_pob_escapes(str(res.get("baseNodeName") or "-"))
    base_ty = strip_pob_escapes(str(res.get("baseTargetType") or "-"))
    alloc_str = "Allocated" if res.get("allocated") else "Unallocated"
    lines = [f"### Passive Node Tattoo Options: `{nid}` ({base_nm} | {base_ty} | {alloc_str})"]
    cur_ov = res.get("currentOverride")
    if isinstance(cur_ov, dict) and cur_ov.get("name"):
        lines.append(f"- **Current Override**: `{cur_ov.get('name')}` (`why={cur_ov.get('why')}`)")
    if res.get("count"):
        lines.append(f"- **Engine Count**: {res['count']}")
    opts = res.get("options") or []
    if not res.get("allowed") or not opts:
        lines.append("\nNo tattoos or runegrafts are eligible for this passive node.")
        return "\n".join(lines)
    rows = [
        [
            o.get("id") or o.get("name"),
            strip_pob_escapes(str(o.get("nameZh") or "-")),
            "; ".join(strip_pob_escapes(str(s)) for s in (o.get("lines") or ())[:2])[:90],
        ]
        for o in opts
    ]
    lines.append("\n" + render_table(["Tattoo / Runegraft", "ZH", "Stats"], rows))
    return "\n".join(lines)


@click.command("tattoo")
@click.option("--node", "node", required=True, type=str, help="Allocated passive node ID or name.")
@click.option(
    "--tattoo",
    "tattoo",
    type=str,
    default=None,
    help=(
        "Tattoo/Runegraft EN/ZH name or id, or 'clear' to remove; omit to list eligible options. "
        "Connectivity guards count allocated adjacent passives (stricter than PoB's static linkedId)."
    ),
)
@click.option("--show-legacy", "show_legacy", is_flag=True, default=False, help="Allow retired 3.22 legacy tattoos.")
@click.option(
    "--strict-req",
    "strict_req",
    is_flag=True,
    default=False,
    help="Fail with exit 1 if applying the tattoo bricks or worsens Str/Dex/Int/Omni requirements.",
)
@click.option("--apply", is_flag=True, default=False, help="Persist tattoo override into build (default: 0-residue what-if).")
@_target_option(allow_slot_alias=True)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
@click.pass_context
def cmd_tree_tattoo(
    ctx: click.Context,
    node: str,
    tattoo: str | None,
    show_legacy: bool,
    strict_req: bool,
    apply: bool,
    target: str,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Inspect, apply, or clear a PoE1 Tattoo/Runegraft on an allocated passive node.

    Connectivity guards (`MinimumConnected` / `MaximumConnected`) count allocated adjacent passives
    rather than PoB's static `linkedId` degree.
    """
    from .cli_tree import _format_mutation_report, _invoke_tree_daemon, _resolve_tree_subcommand_opts

    target, as_json, compact, out_path = _resolve_tree_subcommand_opts(
        ctx, target=target, as_json=as_json, compact=compact, out_path=out_path
    )
    res = _invoke_tree_daemon(
        "tree_tattoo",
        {
            "target": target,
            "node": node,
            "tattoo": tattoo,
            "show_legacy": show_legacy,
            "strict_req": strict_req,
            "apply": apply,
        },
    )
    if as_json:
        _emit_json(res, compact=compact, out_path=out_path)
        return
    if res.get("mode") == "inspect":
        _emit_text(_format_tattoo_inspect_md(res), out_path=out_path)
        return
    detail = res.get("detail") or {}
    title = (
        f"Clear Tattoo on Node {detail.get('nodeId', node)}"
        if detail.get("action") == "clear"
        else f"Tattoo Node {detail.get('nodeId', node)}: {detail.get('tattoo') or tattoo}"
    )
    report = _format_mutation_report(title, res)
    req_warns = res.get("reqWarnings") or []
    if req_warns:
        report += "\n\n" + "\n".join(f"> **Warning**: {w}" for w in req_warns)
    _emit_text(report, out_path=out_path)


def register_tree_tattoo_commands(tree_group: click.Group) -> None:
    """Attach `tattoo` subcommand onto `pob tree`."""
    tree_group.add_command(cmd_tree_tattoo)
