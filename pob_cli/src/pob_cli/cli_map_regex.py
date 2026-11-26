"""Click CLI subcommand `pob map-regex` (daemon-backed or `--preset-only` offline)."""

from __future__ import annotations

import sys
from typing import Any

import click

from .cli_common import (
    _echo_stderr_utf8,
    _emit_json,
    _emit_text,
    _output_options,
    _prepare_load_source,
    _prepend_warning,
    _target_option,
    invoke_daemon,
)
from .cli_inspect_parts import _apply_loop_guard
from .daemon.read_cache import compute_source_hash
from .formatters import render_table
from .locale_cfg import current_lang, normalize_lang
from .map_regex import build_map_regex_result, load_corpus, resolve_preset_ids


def _format_map_regex_markdown(res: dict[str, Any]) -> str:
    regex = str(res.get("regex") or "")
    length = int(res.get("length") or len(regex))
    max_len = int(res.get("maxLength") or 250)
    lang = str(res.get("clientLang") or "en")
    fp = int(res.get("falsePositiveCollisions") or 0)
    tier = str(res.get("tier") or "both")
    mode = str(res.get("mode") or "safe")
    corpus_ver = str(res.get("corpusVersion") or "3.30")
    lang_overlay = str(res.get("langOverlay") or ("zh-rCN" if res.get("cnOverlayActive") else "none"))

    lines = [
        f"### Map Regex (`tier={tier}`, `mode={mode}`)",
        f"`{length} / {max_len} chars`, `lang={lang}`, `corpus={corpus_ver}`, `overlay={lang_overlay}`, `{fp} false-positive collisions verified`",
        "",
        "```text",
        regex or "(no excluded mods)",
        "```",
    ]

    overflow = res.get("overflowRegex") or []
    if overflow:
        lines.append(f"\n#### Overflow Regex ({len(overflow)} extra filter{'s' if len(overflow) > 1 else ''})")
        for idx, chunk in enumerate(overflow, 1):
            lines.append(f"- **Part {idx + 1}** (`{len(chunk)} / {max_len} chars`):")
            lines.append("```text")
            lines.append(str(chunk))
            lines.append("```")

    excluded = res.get("excludedMods") or []
    lines.append("\n#### Excluded Map Mods")
    if excluded:
        rows = [
            [
                f"[{str(m.get('severity') or 'avoid').upper()}]",
                str(m.get("id") or "-"),
                f"`{m.get('token') or '-'}`",
                str(m.get("reason") or "-"),
            ]
            for m in excluded
        ]
        lines.append(render_table(["Severity", "Preset", "Token", "Reason"], rows))
    elif res.get("presetOnly"):
        lines.append("- None (no modifiers excluded).")
    else:
        lines.append("- None (build can run all evaluated map modifiers).")

    exempted = [
        m for m in (res.get("exemptedMods") or [])
        if str(m.get("reason") or "").startswith(("SAFE:", "Manually allowed"))
        and "toggleable T17 preset" not in str(m.get("reason") or "")
        and "no critical build conflict" not in str(m.get("reason") or "")
    ]
    if exempted:
        brief = "; ".join(
            f"`{m['id']}` ({str(m['reason']).removeprefix('SAFE: ').strip()})" for m in exempted[:10]
        )
        lines.append(f"\n- **Auto-Exempted (`SAFE`)**: {brief}")

    return "\n".join(lines)


@click.command("map-regex")
@click.argument("source", required=False)
@click.option(
    "--tier",
    type=click.Choice(["t16", "t17", "both"], case_sensitive=False),
    default="both",
    show_default=True,
    help="Map modifier tier pool to evaluate.",
)
@click.option(
    "--mode",
    type=click.Choice(["brick", "safe"], case_sensitive=False),
    default="safe",
    show_default=True,
    help="Exclusion strictness (`brick`: hard bricks only; `safe`: bricks + dangers).",
)
@click.option(
    "--avoid",
    multiple=True,
    help="Extra comma-separated preset IDs or bilingual keywords to exclude (repeatable).",
)
@click.option(
    "--allow",
    multiple=True,
    help="Comma-separated preset IDs or bilingual keywords to force-allow (repeatable).",
)
@click.option(
    "--preset-only",
    is_flag=True,
    default=False,
    help="Compile `--avoid` presets directly without starting or querying the PoB daemon.",
)
@click.option("--min-quant", type=int, default=None, help="Require minimum Item Quantity % (e.g. 80).")
@click.option("--min-pack", type=int, default=None, help="Require minimum Monster Pack Size % (e.g. 30).")
@click.option(
    "--client-lang",
    type=click.Choice(["auto", "en", "zh-CN", "zh-TW"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="PoE client language for generated regex tokens.",
)
@click.option("--max-len", type=int, default=250, show_default=True, help="Maximum regex string length.")
@_target_option(allow_slot_alias=True)
@click.option("--char", "char_name", default=None, help="Character name when SOURCE is an official profile URL.")
@click.option("--league", "league", default=None, help="Optional league filter for official profile URL import.")
@click.option(
    "--realm",
    type=click.Choice(["pc", "xbox", "sony", "cn"], case_sensitive=False),
    default=None,
    help="Official profile realm (`pc`, `xbox`, `sony`, or `cn`).",
)
@click.option("--json", "as_json", is_flag=True, help="Output structured English JSON.")
@_output_options
def cmd_map_regex(
    source: str | None,
    tier: str,
    mode: str,
    avoid: tuple[str, ...],
    allow: tuple[str, ...],
    preset_only: bool,
    min_quant: int | None,
    min_pack: int | None,
    client_lang: str,
    max_len: int,
    target: str,
    char_name: str | None,
    league: str | None,
    realm: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Generate <=250-char 0-false-positive T16/T17 map regex tailored to your PoB build."""
    if max_len < 10:
        _echo_stderr_utf8("Error: --max-len must be at least 10.")
        sys.exit(1)

    eff_lang = current_lang() if client_lang.lower() == "auto" else normalize_lang(client_lang)
    try:
        corpus = load_corpus(eff_lang)
        avoid_ids = resolve_preset_ids(list(avoid), corpus)
        allow_ids = resolve_preset_ids(list(allow), corpus)
    except ValueError as exc:
        _echo_stderr_utf8(f"Error: {exc}")
        sys.exit(1)

    if preset_only:
        if not avoid_ids and min_quant is None and min_pack is None:
            _echo_stderr_utf8("Error: --preset-only requires --avoid, --min-quant, or --min-pack.")
            sys.exit(1)
        res = build_map_regex_result(
            None,
            tier=tier.lower(),
            mode=mode.lower(),
            avoid_ids=avoid_ids,
            allow_ids=allow_ids,
            preset_only=True,
            client_lang=eff_lang,
            max_len=max_len,
            min_quant=min_quant,
            min_pack=min_pack,
            corpus=corpus,
        )
        if as_json:
            _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
            return
        _emit_text(_format_map_regex_markdown(res), out_path=out_path)
        return

    params: dict[str, Any] = {
        "target": target,
        "tier": tier.lower(),
        "mode": mode.lower(),
        "client_lang": eff_lang,
        "max_len": max_len,
    }
    if avoid_ids:
        params["avoid"] = avoid_ids
    if allow_ids:
        params["allow"] = allow_ids
    if min_quant is not None:
        params["min_quant"] = min_quant
    if min_pack is not None:
        params["min_pack"] = min_pack
    if char_name and char_name.strip():
        params["char_name"] = char_name.strip()
    if league and league.strip():
        params["league"] = league.strip()
    if realm and realm.strip():
        params["realm"] = realm.strip().lower()
    if source:
        resolved_src, resolved_xml_code = _prepare_load_source(source)
        if resolved_xml_code is not None:
            params["xml_code"] = resolved_xml_code
        else:
            params["source"] = resolved_src
        src_hash = compute_source_hash(params)
        if src_hash:
            params["source_hash"] = src_hash

    res = invoke_daemon("map_regex", params, timeout=180.0)
    if as_json:
        _apply_loop_guard(res)
        _emit_json(res, compact=compact, out_path=out_path, sanitize=False)
        return
    _emit_text(_apply_loop_guard(res, _prepend_warning(_format_map_regex_markdown(res), res)), out_path=out_path)
