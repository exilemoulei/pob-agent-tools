"""Compact 1-screen CLI reference (`pob help --brief`) auto-generated from Click commands."""

from __future__ import annotations

import sys
from typing import Any

import click

from .cli_common import SLOT_ALIAS_PARAM, _emit_json, _emit_text, _output_options

# Root-only `--version` / `-V` never reach a subcommand's params, so only per-command output noise is
# dropped here (`pob self-update --version X.Y.Z` stays visible).
_GLOBAL_NOISE_OPTS = frozenset({"--help", "-h", "--compact", "--out", "--offline"})
# Commands whose key options sit past the default cut-off (`--char/--realm/--json`, `--limit`, ...).
# They are listed in full: a fixed higher cut-off hides those options again as soon as one is added.
_BRIEF_UNCUT = frozenset({"diagnose", "data"})


def _param_token(param: click.Parameter) -> str | None:
    if getattr(param, "hidden", False) or param.name == SLOT_ALIAS_PARAM:
        return None
    if isinstance(param, click.Argument):
        label = (param.metavar or param.name or "ARG").upper()
        suffix = "..." if param.nargs == -1 else ""
        return f"<{label}{suffix}>" if param.required else f"[{label}{suffix}]"
    if isinstance(param, click.Option):
        long_opts = [o for o in param.opts if o.startswith("--")]
        opt = long_opts[0] if long_opts else (param.opts[0] if param.opts else "")
        if not opt or opt in _GLOBAL_NOISE_OPTS:
            return None
        if param.secondary_opts:
            sec_long = [o for o in param.secondary_opts if o.startswith("--")]
            if sec_long:
                return f"{opt}/{sec_long[0]}"
        if param.is_flag:
            return opt
        if param.metavar:
            return f"{opt} {param.metavar}"
        return opt
    return None


def _command_signature(cmd: click.Command, *, max_opts: int = 12) -> str:
    tokens: list[str] = []
    for p in cmd.params:
        tok = _param_token(p)
        if tok and tok not in tokens:
            tokens.append(tok)
    if len(tokens) > max_opts:
        return " ".join(tokens[:max_opts]) + f" (+{len(tokens) - max_opts})"
    return " ".join(tokens)


def build_brief_entries(root: click.Group, root_ctx: click.Context) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name in root.list_commands(root_ctx):
        cmd = root.get_command(root_ctx, name)
        if cmd is None or cmd.hidden:
            continue
        short = (cmd.get_short_help_str(limit=96) or "").strip()
        if isinstance(cmd, click.Group):
            sub_ctx = click.Context(cmd, info_name=name, parent=root_ctx)
            subs: list[dict[str, str]] = []
            if cmd.invoke_without_command:
                subs.append({"name": "(inspect)", "flags": _command_signature(cmd, max_opts=8)})
            for sub_name in cmd.list_commands(sub_ctx):
                sub_cmd = cmd.get_command(sub_ctx, sub_name)
                if sub_cmd is None or sub_cmd.hidden:
                    continue
                subs.append({"name": sub_name, "flags": _command_signature(sub_cmd, max_opts=8)})
            entries.append({"name": name, "kind": "group", "summary": short, "subcommands": subs})
        else:
            entries.append(
                {
                    "name": name,
                    "kind": "command",
                    "summary": short,
                    "flags": _command_signature(cmd, max_opts=sys.maxsize if name in _BRIEF_UNCUT else 10),
                }
            )
    return entries


def format_brief_help(root: click.Group, root_ctx: click.Context) -> str:
    entries = build_brief_entries(root, root_ctx)
    lines = [
        "pob — Headless PoE1 3.30 Path of Building CLI (run `pob <cmd> --help` for full details)",
        "Global: [--offline] [--compact] [--out FILE] | Build slot: [--target SLUG] (default POB_SLOT or 'main'); "
        "--slot below is an equipment/socket slot (items, item, trade, bench, data, rank-gems, skill new-group)",
    ]
    pad = max((len(e["name"]) for e in entries), default=10)
    for ent in entries:
        name = ent["name"].ljust(pad)
        if ent["kind"] == "group":
            parts = [
                f"{s['name']} [{s['flags']}]" if s["flags"] else s["name"]
                for s in ent["subcommands"]
            ]
            lines.append(f"  {name}  " + " | ".join(parts))
        else:
            fl = ent["flags"]
            lines.append(f"  {name}  {fl}" if fl else f"  {name}  # {ent['summary']}")
    return "\n".join(lines)


@click.command("help")
@click.argument("topic", nargs=-1)
@click.option("-b", "--brief", is_flag=True, default=False, help="Compact 1-screen listing of all subcommands and key options.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit structured CLI command metadata as JSON.")
@_output_options
@click.pass_context
def cmd_help(
    ctx: click.Context,
    topic: tuple[str, ...],
    brief: bool,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Show compact 1-screen CLI cheat-sheet (`--brief`) or help for a subcommand."""
    root_ctx = ctx.find_root()
    root_cmd = root_ctx.command
    if not isinstance(root_cmd, click.Group):
        _emit_text(root_ctx.get_help(), out_path=out_path)
        return

    if topic:
        cur_cmd: click.Command = root_cmd
        cur_ctx = root_ctx
        for part in topic:
            if not isinstance(cur_cmd, click.Group):
                raise click.UsageError(f"Command `{cur_ctx.command_path}` has no subcommands.")
            nxt = cur_cmd.get_command(cur_ctx, part)
            if nxt is None:
                raise click.UsageError(f"No such command {' '.join(topic)!r}.")
            cur_ctx = click.Context(nxt, info_name=part, parent=cur_ctx)
            cur_cmd = nxt
        cmd_label = " ".join(("pob", *topic))
        if as_json:
            _emit_json(
                {
                    "command": cmd_label,
                    "flags": _command_signature(cur_cmd, max_opts=50),
                    "help": cur_ctx.get_help(),
                },
                compact=compact,
                out_path=out_path,
            )
            return
        if brief:
            _emit_text(f"{cmd_label} {_command_signature(cur_cmd, max_opts=50)}".rstrip(), out_path=out_path)
            return
        _emit_text(cur_ctx.get_help(), out_path=out_path)
        return

    if as_json:
        _emit_json({"commands": build_brief_entries(root_cmd, root_ctx)}, compact=compact, out_path=out_path)
        return
    if brief:
        _emit_text(format_brief_help(root_cmd, root_ctx), out_path=out_path)
        return
    _emit_text(
        root_ctx.get_help() + "\n\nTip: run `pob help --brief` for a compact 1-screen option cheat-sheet.",
        out_path=out_path,
    )
