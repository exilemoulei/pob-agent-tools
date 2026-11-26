# SPDX-License-Identifier: GPL-3.0-only
"""CLI build I/O commands: load, export, notes, undo, redo (L4)."""

from __future__ import annotations

from pathlib import Path

import click

from .cli_common import daemon_call_cli, echo_utf8, emit_output, resolve_target_slot
from .formatters import format_notes_md


@click.command("load")
@click.argument("source", required=True)
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_load(ctx: click.Context, source: str, target: str | None, as_json: bool) -> None:
    """Load a PoE2 build (code, XML file, pobb.in, poe2db.tw, poe.ninja/poe2, maxroll, WeGame) into a slot."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("load", {"source": source, "target": slot})
    info = res.get("buildInfo") or {}
    lines = [
        f"Loaded PoE2 build into slot `{slot}` ({res.get('sourceKind') or 'build'}): "
        f"{info.get('className') or '?'} / {info.get('ascendClassName') or 'None'} Lv{info.get('level') or '?'}"
    ]
    dropped_nodes = int(res.get("disconnectedNodesDropped") or 0)
    if dropped_nodes > 0:
        req_cnt = res.get("requestedNodeCount", "?")
        ld_cnt = res.get("loadedNodeCount", "?")
        warn_txt = (
            f"Warning: {dropped_nodes} passive node(s) from input XML were dropped on load "
            f"({req_cnt} requested -> {ld_cnt} allocated; disconnected from class start or invalid for treeVersion)."
        )
        echo_utf8(warn_txt, err=True)
        lines.append(f"- [tree-disconnected] {warn_txt}")
    for w in res.get("fixupWarnings") or []:
        lines.append(f"- {w}")
    for w in res.get("importWarnings") or []:
        lines.append(f"- [import] {w}")
    for d in res.get("droppedItems") or []:
        lines.append(f"- [{d.get('issue')}] {d.get('message')}")
    notes = res.get("notes") or {}
    if notes.get("hasNotes"):
        lines.append(f"- Author Notes ({notes.get('lineCount', 0)} lines) available via `pob2 notes`.")
    emit_output(ctx, res, as_json=as_json, markdown_text="\n".join(lines))


@click.command("export")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--xml", "include_xml", is_flag=True, default=False, help="Output decoded XML instead of share code.")
@click.option("--out", "out_file", type=click.Path(dir_okay=False), default=None, help="Write exported code/XML to file.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_export(
    ctx: click.Context,
    target: str | None,
    include_xml: bool,
    out_file: str | None,
    as_json: bool,
) -> None:
    """Export the current PoE2 slot build as a PoB2 share code or XML."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("export", {"target": slot, "include_xml": include_xml})
    if as_json:
        emit_output(ctx, res, as_json=True, out_path=out_file)
        return
    body = str(res.get("xml") if include_xml else res.get("code") or "")
    if out_file:
        p = Path(out_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body + "\n", encoding="utf-8", newline="\n")
        echo_utf8(f"Exported to {p}")
        return
    echo_utf8(body)


@click.command("notes")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_notes(ctx: click.Context, target: str | None, as_json: bool) -> None:
    """Display author build Notes with PoB colour escape codes stripped."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("notes", {"target": slot})
    emit_output(ctx, res, as_json=as_json, markdown_text=format_notes_md(res))


@click.command("undo")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_undo(ctx: click.Context, target: str | None, as_json: bool) -> None:
    """Undo the last committed edit on the target PoE2 build slot."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("undo", {"target": slot})
    md = f"Undone slot `{slot}` (undoDepth={res.get('undoDepth')}, redoDepth={res.get('redoDepth')})."
    emit_output(ctx, res, as_json=as_json, markdown_text=md)


@click.command("redo")
@click.option("--target", type=str, default=None, help="Target build slot (default: main).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON output.")
@click.pass_context
def cmd_redo(ctx: click.Context, target: str | None, as_json: bool) -> None:
    """Redo the previously undone edit on the target PoE2 build slot."""
    slot = resolve_target_slot(target)
    res = daemon_call_cli("redo", {"target": slot})
    md = f"Redone slot `{slot}` (undoDepth={res.get('undoDepth')}, redoDepth={res.get('redoDepth')})."
    emit_output(ctx, res, as_json=as_json, markdown_text=md)


__all__ = ["cmd_export", "cmd_load", "cmd_notes", "cmd_redo", "cmd_undo"]
