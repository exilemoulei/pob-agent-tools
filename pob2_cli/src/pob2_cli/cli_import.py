# SPDX-License-Identifier: GPL-3.0-only
"""Click CLI subcommand `pob2 import` for CN WeGame share links and PoB2 build validation (L4)."""

from __future__ import annotations

import base64
import json
import sys
import zlib
from pathlib import Path
from typing import Any

import click

from .compat import ensure_utf8
from .errors import DaemonRequestError
from .game_profile import stamp_game
from .guardrails import validate_build_xml, validate_url
from .importers import (
    convert_wegame_bundle_to_pob2_xml,
    fetch_wegame_share,
    is_wegame_share_url,
    parse_wegame_share_url,
)


def _echo(text: str, *, err: bool = False) -> None:
    ensure_utf8(sys.stderr if err else sys.stdout)
    click.echo(text, err=err)


def _try_decode_pob_code(raw: str) -> str | None:
    """Decode a base64url + zlib PoB/PoB2 share code into XML text if valid."""
    token = raw.strip()
    if len(token) < 24 or " " in token or "\n" in token:
        return None
    try:
        padded = token + "=" * ((4 - len(token) % 4) % 4)
        compressed = base64.urlsafe_b64decode(padded.encode("ascii"))
        xml = zlib.decompress(compressed).decode("utf-8")
        return xml if "<PathOfBuilding" in xml else None
    except Exception:
        return None


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
@click.option("--timeout", type=float, default=15.0, show_default=True, help="HTTP timeout per WeGame endpoint (s).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON result.")
@click.pass_context
def cmd_import(
    ctx: click.Context,
    source: str,
    xml_out: str | None,
    data_dir: Path | None,
    timeout: float,
    as_json: bool,
) -> None:
    """Import a PoE2 build from a CN WeGame share link, sanitized WeGame JSON bundle, or PoB2 XML/code."""
    src = source.strip()
    # Always run bidirectional URL gate first so PoE1 URLs exit with code 1.
    if "://" in src or src.lower().startswith(("pob:", "pob2:", "www.", "poe")):
        validate_url(src, expected_game="poe2", cli_subcommand="import")

    root_obj = (ctx.find_root().obj or {}) if ctx is not None else {}
    compact = bool(root_obj.get("compact"))
    effective_out = xml_out or root_obj.get("out")

    # 1. CN WeGame cookie-free share URL
    if is_wegame_share_url(src):
        parse_wegame_share_url(src)
        bundle = fetch_wegame_share(src, timeout=timeout)
        res = convert_wegame_bundle_to_pob2_xml(bundle, data_dir=data_dir)
        _deliver_import_result(
            res.xml,
            summary=res.to_summary_dict(),
            source_kind="wegame_url",
            as_json=as_json,
            compact=compact,
            xml_out=effective_out,
        )
        return

    # 2. Local file: sanitized WeGame JSON bundle or PoB2 XML
    cand_path = Path(src)
    if cand_path.is_file():
        raw_text = cand_path.read_text(encoding="utf-8")
        if cand_path.suffix.lower() == ".json" or raw_text.lstrip().startswith("{"):
            try:
                bundle = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise DaemonRequestError(f"Invalid JSON bundle `{cand_path}`: {exc}") from exc
            res = convert_wegame_bundle_to_pob2_xml(bundle, data_dir=data_dir)
            _deliver_import_result(
                res.xml,
                summary=res.to_summary_dict(),
                source_kind="wegame_bundle",
                as_json=as_json,
                compact=compact,
                xml_out=effective_out,
            )
            return
        validate_build_xml(raw_text, expected_game="poe2", source_hint=str(cand_path))
        _deliver_import_result(
            raw_text,
            summary={"warnings": [], "stats": {}},
            source_kind="pob2_xml_file",
            as_json=as_json,
            compact=compact,
            xml_out=effective_out,
        )
        return

    # 3. Direct PoB2 XML or base64/zlib PoB2 share code
    decoded_xml = src if src.lstrip().startswith("<") else _try_decode_pob_code(src)
    if decoded_xml is not None:
        validate_build_xml(decoded_xml, expected_game="poe2")
        _deliver_import_result(
            decoded_xml,
            summary={"warnings": [], "stats": {}},
            source_kind="pob2_code",
            as_json=as_json,
            compact=compact,
            xml_out=effective_out,
        )
        return

    raise DaemonRequestError(
        "Unsupported import source. Provide a CN WeGame PoE2 share URL "
        "(`https://www.wegame.com.cn/helper/poe2/#/share/<code>`), "
        "a sanitized WeGame JSON bundle, or a PoB2 XML file / share code."
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
