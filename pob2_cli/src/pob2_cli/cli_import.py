# SPDX-License-Identifier: GPL-3.0-only
"""Click CLI subcommand `pob2 import` for multi-source PoE2 URLs, WeGame shares, and PoB2 codes/XML (L4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from .cli_common import daemon_call_cli, resolve_target_slot
from .compat import ensure_utf8
from .game_profile import stamp_game
from .importers import resolve_poe2_build_source


def _echo(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def _emit_json(payload: dict[str, Any], *, compact: bool, out_path: str | None) -> None:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    )
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8", newline="\n")
        return
    _echo(text)


@click.command("import")
@click.argument("source", required=True)
@click.option(
    "--out",
    "xml_out",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write imported PoB2 XML to a UTF-8 file.",
)
@click.option(
    "--data-dir",
    "data_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional Data/poe2 localization directory (zh-rCN / zh-rTW).",
)
@click.option("--load", "load_slot", is_flag=True, default=False, help="Also load the imported build into the active PoB2 daemon slot.")
@click.option("--target", type=str, default=None, help="Target daemon slot when --load is set (default: main).")
@click.option("--timeout", type=float, default=15.0, show_default=True, help="HTTP timeout per endpoint (s).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON result.")
@click.pass_context
def cmd_import(
    ctx: click.Context,
    source: str,
    xml_out: str | None,
    data_dir: Path | None,
    load_slot: bool,
    target: str | None,
    timeout: float,
    as_json: bool,
) -> None:
    """Import a PoE2 build from pobb.in, poe2db.tw, poe.ninja/poe2, maxroll.gg/poe2, WeGame, XML, or PoB2 code."""
    root_obj = (ctx.find_root().obj or {}) if ctx is not None else {}
    compact = bool(root_obj.get("compact"))
    effective_out = xml_out or root_obj.get("out")

    resolved = resolve_poe2_build_source(source=source, data_dir=data_dir, timeout=timeout)
    summary: dict[str, Any] = {**resolved.summary, "warnings": list(resolved.warnings), "stats": dict(resolved.stats)}

    if load_slot:
        load_res = daemon_call_cli("load", {"xml_code": resolved.code, "target": resolve_target_slot(target)})
        summary["loaded"] = load_res

    _deliver_import_result(
        resolved.xml,
        summary=summary,
        source_kind=resolved.source_kind,
        as_json=as_json,
        compact=compact,
        xml_out=effective_out,
    )


def _deliver_import_result(
    xml: str,
    *,
    summary: dict[str, Any],
    source_kind: str,
    as_json: bool,
    compact: bool,
    xml_out: str | None,
) -> None:
    if xml_out and not as_json:
        target = Path(xml_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(xml, encoding="utf-8", newline="\n")
        _echo(f"Wrote PoB2 XML ({len(xml)} bytes) to {target}")
        for w in summary.get("warnings") or []:
            _echo(f"[import] {w}", err=True)
        return

    if as_json:
        written_path: str | None = None
        if xml_out:
            target = Path(xml_out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(xml, encoding="utf-8", newline="\n")
            written_path = str(target)
        payload = stamp_game(
            {
                "ok": True,
                "sourceKind": source_kind,
                "xmlBytes": len(xml.encode("utf-8")),
                "xmlPath": written_path,
                **summary,
                **({} if written_path else {"xml": xml}),
            }
        )
        _emit_json(payload, compact=compact, out_path=None)
        return

    _echo(xml, err=False)
    for w in summary.get("warnings") or []:
        _echo(f"[import] {w}", err=True)


__all__ = ["cmd_import"]
