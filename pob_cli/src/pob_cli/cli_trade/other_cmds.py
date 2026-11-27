"""`pob trade stats`, `pob trade convert`, `pob bench list`, and `pob bench suggest` commands."""

from __future__ import annotations

from pathlib import Path

import click

from .. import bench as bench_mod
from ..cli_common import _emit_json, _emit_text, _output_options, _resolve_output_flags
from ..formatters import emit_json, render_table
from ..guardrails import GuardrailScope, check_input, check_input_item_raw, validate_url
from ..locale_cfg import current_realm, trade_base_url_for_realm
from ..trade_client import TradeError
from ..trade_item import convert_item_text
from ..trade_query import (
    aura_disambiguation_for_query,
    get_stat_polarity_info,
    resolve_stat,
    suggest_stats,
)
from .common import SLOT_HINT, TradeUsageGroup, _echo_utf8, _err_utf8, _trade_error


def _polarity_json(stat_id: str) -> dict[str, object]:
    better, better_src = get_stat_polarity_info(stat_id)
    out: dict[str, object] = {"better": better}
    if better_src:
        out["betterSource"] = better_src
    return out


def _fmt_polarity_md(stat_id: str) -> str:
    better, better_src = get_stat_polarity_info(stat_id)
    tag = f" ({better_src.upper()})" if better_src else ""
    if better < 0:
        return f"↓ lower-raw{tag}"
    if better == 0:
        return f"· neutral{tag}"
    return f"↑{tag}"


@click.group("trade", cls=TradeUsageGroup)
def trade_group() -> None:
    """Search official PoE1 trade (read-only), preview Harvest swaps, and verify in PoB."""


@trade_group.command("leagues")
@click.option(
    "--realm",
    type=click.Choice(["global", "tw"], case_sensitive=False),
    default=None,
    help="Trade server realm (`global` pathofexile.com or `tw` pathofexile.tw).",
)
@click.option("--proxy", help="HTTP/SOCKS5 proxy URL (overrides POB_PROXY).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_trade_leagues(
    realm: str | None,
    proxy: str | None,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """List official PoE1 trade leagues and default PC challenge league for global or tw."""
    import pob_cli.cli_trade as _ct

    ctx = click.get_current_context()
    eff_realm = current_realm(cli_realm=realm)
    with _ct.TradeClient(proxy=proxy, realm=eff_realm) as client:
        try:
            leagues = client.leagues()
            default_lg = client.default_league()
        except TradeError as exc:
            _trade_error(ctx, exc)
            return
    if as_json:
        _emit_json(
            {
                "realm": eff_realm,
                "tradeBaseUrl": trade_base_url_for_realm(eff_realm),
                "defaultLeague": default_lg,
                "leagues": leagues,
            },
            compact=compact,
            out_path=out_path,
            sanitize=True,
        )
        return
    rows = [[str(lg.get("id") or "-"), str(lg.get("realm") or "pc"), str(lg.get("text") or "-")] for lg in leagues]
    header = (
        f"### PoE1 Trade Leagues (`{eff_realm}`, `{trade_base_url_for_realm(eff_realm)}`, "
        f"default: `{default_lg}`)\n"
    )
    _emit_text(header + render_table(["ID", "Realm", "Label"], rows), out_path=out_path)


@trade_group.command("stats")
@click.argument("text")
@click.option("--slot", help=f"Optional slot for (Local)/(Shields) routing ({SLOT_HINT}).")
@click.option("--limit", default=5, show_default=True, help="Maximum suggestions to show.")
@click.option(
    "--realm",
    type=click.Choice(["global", "tw"], case_sensitive=False),
    default=None,
    help="Trade server realm (`global` or `tw`).",
)
@click.option("--proxy", help="HTTP/SOCKS5 proxy URL (overrides POB_PROXY).")
@click.option("--refresh", is_flag=True, help="Force refresh remote trade stats cache.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_trade_stats(
    text: str,
    slot: str | None,
    limit: int,
    realm: str | None,
    proxy: str | None,
    refresh: bool,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Resolve an English or Chinese modifier to an official PoE1 trade stat ID."""
    import pob_cli.cli_trade as _ct

    ctx = click.get_current_context()
    validate_url(text, expected_game="poe1", cli_subcommand="trade stats")
    if slot:
        check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(text, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    eff_realm = current_realm(cli_realm=realm)
    with _ct.TradeClient(proxy=proxy, realm=eff_realm) as client:
        try:
            entries, source = _ct.load_trade_stats(
                remote_fetcher=lambda: client.stats(force_refresh=refresh),
                force_refresh=refresh,
            )
        except TradeError as exc:
            _trade_error(ctx, exc)
            return
        except Exception as exc:
            _err_utf8(str(exc))
            ctx.exit(5)
            return

    match = resolve_stat(text, entries, slot=slot)
    suggestions = suggest_stats(text, entries, limit=limit, slot=slot)
    disambig_cands: list[dict[str, object]] = list(match.disambiguation_candidates) if (match and match.disambiguation_candidates) else []
    disambig_aura: str | None = match.disambiguation_aura if (match and match.disambiguation_candidates) else None
    if match is None and not disambig_cands:
        fb_aura, fb_cands = aura_disambiguation_for_query(text, entries)
        if fb_cands:
            disambig_aura = fb_aura
            disambig_cands = list(fb_cands)
    if as_json:
        match_obj: dict[str, object] | None = None
        if match:
            match_obj = {
                "id": match.id,
                "text": match.text,
                "score": match.score,
                **_polarity_json(match.id),
            }
            if match.polarity_inverted:
                match_obj["polarityInverted"] = True
            if disambig_cands:
                match_obj["disambiguationCandidates"] = disambig_cands
        payload: dict[str, object] = {
            "query": text,
            "slot": slot,
            "source": source,
            "match": match_obj,
            "suggestions": [
                {
                    "id": item.id,
                    "text": item.text,
                    "score": item.score,
                    **_polarity_json(item.id),
                }
                for item in suggestions
            ],
        }
        if disambig_cands:
            payload["disambiguationCandidates"] = disambig_cands
            payload["disambiguationWarning"] = (
                f"Ambiguous aura modifier ({disambig_aura}); "
                "specify which sibling stat you want from disambiguationCandidates."
            )
        _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
    else:
        from ..guardrails import get_active_warnings

        lines: list[str] = [w.format_markdown() for w in get_active_warnings()]
        if match:
            inv_note = ", bounds inverted (negated)" if match.polarity_inverted else ""
            lines.append(
                f"Resolved (`{source}`): `{match.text}` -> `{match.id}` "
                f"(score {match.score:.3f}, polarity {_fmt_polarity_md(match.id)}{inv_note})"
            )
        else:
            lines.append(f"No confident match for `{text}` (source: `{source}`).")
        if disambig_cands:
            sibs = "; ".join(f"`{c['text']}` ({c['id']})" for c in disambig_cands[:5])
            lines.append(
                f"> [DISAMBIGUATE] Aura query (`{disambig_aura}`); sibling stats: {sibs}"
            )
        if suggestions:
            lines.append(
                render_table(
                    ["Text", "Stat ID", "Score", "Polarity"],
                    [
                        [item.text, item.id, f"{item.score:.3f}", _fmt_polarity_md(item.id)]
                        for item in suggestions
                    ],
                )
            )
        _emit_text("\n".join(lines), out_path=out_path)
    if match is None:
        ctx.exit(1)


@trade_group.command("convert")
@click.option("--file", "file_path", type=str, help="Item text file.")
@click.option("--text", "raw_text", help="Item text (PoB clipboard format).")
@click.option(
    "--to",
    "target",
    required=True,
    type=click.Choice(["fire", "cold", "lightning"]),
    help="Target element for Harvest conversion.",
)
@click.option("--out", "out_path", type=click.Path(dir_okay=False), help="Write converted text to file.")
@click.option(
    "--force",
    is_flag=True,
    help="Bypass unique/corrupted/mirrored item-level guards (what-if preview only).",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.option(
    "--compact",
    is_flag=True,
    default=False,
    help="Emit compact single-line JSON (when --json is used).",
)
def cmd_trade_convert(
    file_path: str | None,
    raw_text: str | None,
    target: str,
    out_path: str | None,
    force: bool,
    as_json: bool,
    compact: bool,
) -> None:
    """Preview PoE1 3.30 Harvest same-tier element conversion on item text."""
    ctx = click.get_current_context()
    if file_path:
        from ..cli_common import enforce_safe_input_path

        enforce_safe_input_path(file_path)
        p = Path(file_path)
        if not p.is_file():
            _err_utf8(f"Item file not found: {file_path}")
            ctx.exit(1)
            return
        raw_text = p.read_text(encoding="utf-8", errors="replace")
    if not raw_text:
        _err_utf8("Provide --text or --file.")
        ctx.exit(1)
        return
    check_input_item_raw(raw_text, expected_game="poe1")

    try:
        converted = convert_item_text(raw_text, target, force=force)
    except ValueError as exc:
        _err_utf8(str(exc))
        ctx.exit(1)
        return

    if out_path:
        out_file = Path(out_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(converted.text + "\n", encoding="utf-8", newline="\n")

    if as_json:
        payload = {
            "target": target,
            "text": converted.text,
            "conversions": [c.as_dict() for c in converted.conversions],
            "warnings": list(converted.warnings),
            "skipped": list(converted.skipped),
        }
        eff_compact, root_out = _resolve_output_flags(compact=compact, out_path=None)
        json_out = None if out_path else root_out
        text = emit_json(payload, compact=eff_compact, out=json_out, sanitize=True)
        if json_out is not None:
            _echo_utf8(f"Saved JSON to `{json_out}` ({len(text.encode('utf-8')):,} bytes).")
        return

    lines = [f"### Converted item text (target: {target})", "```text", converted.text, "```"]
    if converted.conversions:
        lines.append("\nConverted modifiers:")
        for c in converted.conversions:
            tier_info = f"tier {c.tier}; " if c.tier else ""
            lines.append(
                f"- {c.template} -> {c.converted_template} "
                f"(cost: {c.cost}; {tier_info}{c.patch} rules; same tier, roll re-rolled)"
            )
    else:
        lines.append("\nNo convertible modifiers were found.")
    if converted.warnings:
        lines.append("\nWarnings:")
        for w in converted.warnings:
            lines.append(f"- {w}")
    if converted.skipped:
        lines.append("\nSkipped:")
        for reason in converted.skipped:
            lines.append(f"- {reason}")
    lines.append(
        "\n> Harvest keeps the tier but re-rolls the value inside that tier. "
        "Listed values are best-case references; verify with `pob item try --slot <slot>`."
    )
    rendered = "\n".join(lines)
    if out_path:
        _echo_utf8(rendered)
    else:
        _emit_text(rendered)


@click.group("bench")
def bench_group() -> None:
    """Look up PoE1 crafting bench modifiers (ModMaster.lua authoritative + cost overlay)."""


@bench_group.command("list")
@click.option("--slot", required=True, help=f"Equipment slot ({SLOT_HINT}).")
@click.option("--search", "text", help="Filter by English or Chinese modifier text.")
@click.option("--affix", type=click.Choice(["prefix", "suffix"]), help="Filter by affix type.")
@click.option("--limit", default=25, show_default=True, help="Maximum rows to show.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_bench_list(
    slot: str,
    text: str | None,
    affix: str | None,
    limit: int,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """List crafting bench modifiers available for a slot."""
    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    if text:
        check_input(text, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
        results = bench_mod.search(slot, text, limit=limit, affix=affix)
        items = [r.recipe for r in results]
    else:
        items = bench_mod.list_for_slot(slot, affix=affix)[: max(0, limit)]

    meta = bench_mod.source_metadata()
    if as_json:
        payload = {
            "slot": slot,
            "slotKey": bench_mod.slot_key(slot),
            "count": len(items),
            "source": meta,
            "recipes": [recipe.as_dict() for recipe in items],
        }
        _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
        return

    if not items:
        _emit_text(f"No bench modifiers matched for `{slot}`.", out_path=out_path)
        return

    rows = [
        [recipe.affix, recipe.text, recipe.text_zh or "-", recipe.roll_text or "-", recipe.cost, recipe.unlock or "-"]
        for recipe in items
    ]
    _emit_text(render_table(["Affix", "Modifier", "中文", "Roll", "Cost", "Unlock"], rows), out_path=out_path)


@bench_group.command("suggest")
@click.option("--slot", required=True, help=f"Equipment slot ({SLOT_HINT}).")
@click.option("--stat", "text", required=True, help="Desired modifier (EN or 中文).")
@click.option("--affix", type=click.Choice(["prefix", "suffix"]), help="Filter by affix type.")
@click.option("--limit", default=5, show_default=True, help="Maximum suggestions.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@_output_options
def cmd_bench_suggest(
    slot: str,
    text: str,
    affix: str | None,
    limit: int,
    as_json: bool,
    compact: bool,
    out_path: str | None,
) -> None:
    """Suggest the strongest crafting bench craft for a desired modifier."""
    ctx = click.get_current_context()
    check_input(slot, scope=GuardrailScope.SLOT, expected_game="poe1")
    check_input(text, scope=GuardrailScope.STAT_QUERY, expected_game="poe1")
    results = bench_mod.search(slot, text, limit=limit, affix=affix)
    if not results:
        _emit_text(f"No bench modifier for `{slot}` matched `{text}`.", out_path=out_path)
        ctx.exit(1)
        return

    best_recipe = results[0].recipe
    meta = bench_mod.source_metadata()
    if as_json:
        payload = {
            "slot": slot,
            "slotKey": bench_mod.slot_key(slot),
            "query": text,
            "best": best_recipe.as_dict(),
            "alternatives": [r.recipe.as_dict() for r in results[1:]],
            "source": meta,
        }
        _emit_json(payload, compact=compact, out_path=out_path, sanitize=True)
        return

    zh_part = f" ({best_recipe.text_zh})" if best_recipe.text_zh and best_recipe.text_zh != best_recipe.text else ""
    lines = [
        f"Best bench craft for `{slot}` / `{text}` (source: `{meta.get('mode', 'unknown')}`):",
        f"- affix: {best_recipe.affix}",
        f"- modifier: {best_recipe.text}{zh_part}",
        f"- roll: {best_recipe.roll_text or '-'} (minimum {best_recipe.min_value:g} is the safe floor)",
        f"- crafted line (min): `{' | '.join(best_recipe.crafted_lines(use_min=True))}`",
        f"- cost: {best_recipe.cost}",
    ]
    if best_recipe.unlock:
        lines.append(f"- unlock: {best_recipe.unlock}")
    lines.append(
        f"- note: bench mods roll lower than high-tier natural mods; requires an open {best_recipe.affix}. "
        "Append the `{crafted}` line and verify with `pob item try`."
    )
    if len(results) > 1:
        lines.append("\nAlternatives:")
        for r in results[1:]:
            rec = r.recipe
            lines.append(f"- [{rec.affix}] {rec.text} ({rec.roll_text or '-'}; {rec.cost})")
    _emit_text("\n".join(lines), out_path=out_path)
