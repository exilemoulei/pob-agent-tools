"""`pob trade` — official PoE1 trade search with Harvest-aware conversion.

The command is read-only: it searches the official trade API, compares the
listings with the equipped item, optionally verifies the top candidates through
the local headless PoB (`pob item try`), and prints a report.  It never buys,
whispers, or writes anything into the build.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from . import bench as bench_mod
from .daemon import call_daemon
from .external_data import resolve_current_trade_league
from .formatters import render_table, to_json_str
from .poe_item import (
    build_item_text,
    convert_item_text,
    parse_item_text,
    sanitize_text,
    trade_mod_tiers,
)
from .trade_api import (
    CookieRequired,
    NoResults,
    RateLimited,
    TradeClient,
    TradeError,
)
from .trade_compare import (
    Candidate,
    compare_items,
    format_report,
    parse_weights,
    preferred_templates,
    report_json,
    score_deltas,
    sort_candidates,
    verify_candidate,
)
from .trade_query import (
    build_query,
    category_for_slot,
    flatten_stats,
    parse_stat_argument,
    plan_stats,
    pob_slot_for_slot,
    resolve_stat,
    suggest_stats,
)

SLOT_CHOICES = (
    "helmet, body_armour, gloves, boots, shield, amulet, belt, ring1, ring2, weapon1, weapon2"
)

OPEN_PREFIX_ID = "pseudo.pseudo_number_of_empty_prefix_mods"
OPEN_SUFFIX_ID = "pseudo.pseudo_number_of_empty_suffix_mods"
OPEN_AFFIX_ID = "pseudo.pseudo_number_of_empty_affix_mods"


def _echo(text: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    click.echo(text)


def _err(text: str) -> None:
    click.echo(text, err=True)


def _exit(ctx: click.Context, code: int) -> None:
    ctx.exit(code)


def _trade_error(ctx: click.Context, error: TradeError) -> None:
    _err(str(error))
    if isinstance(error, CookieRequired):
        _exit(ctx, 3)
    if isinstance(error, RateLimited):
        _exit(ctx, 4)
    if isinstance(error, NoResults):
        _exit(ctx, 2)
    _exit(ctx, 5)


def _resolve_league(explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        league = resolve_current_trade_league()
        if league:
            return league
    except Exception:
        pass
    try:
        return TradeClient().default_league()
    except TradeError:
        return "Standard"


def _build_label() -> str:
    try:
        status = call_daemon("status")
    except Exception:
        return "unknown"
    info = (status.get("main") or {}).get("buildInfo") or {}
    parts = [
        str(info.get("classNameZh") or info.get("className") or "").strip(),
        str(info.get("ascendClassNameZh") or info.get("ascendClassName") or "").strip(),
    ]
    label = " ".join(part for part in parts if part)
    level = info.get("level")
    if level:
        label = f"{label} (level {level})" if label else f"level {level}"
    return label or "unknown"


def _read_current_item(slot: str, target: str) -> tuple[Any, str | None]:
    try:
        payload = call_daemon("items", {"target": target, "slot": slot, "raw": True})
    except Exception as error:  # noqa: BLE001 - daemon/PoB may be unavailable
        return None, sanitize_text(error, 300)
    raw = payload.get("raw")
    if not raw:
        return None, None
    try:
        return parse_item_text(raw), None
    except ValueError as error:
        return None, sanitize_text(error, 300)


def _price_of(row: dict[str, Any]) -> tuple[float | None, str | None]:
    price = ((row.get("listing") or {}).get("price") or {})
    amount = price.get("amount")
    return (float(amount) if amount is not None else None, price.get("currency"))


def _conversion_payload(plan) -> dict[str, Any]:
    if not plan.conversions:
        return {"enabled": False}
    return {
        "enabled": True,
        "targets": [
            {
                "group": conversion.group,
                "target": conversion.target,
                "elements": conversion.elements,
                "cost": conversion.cost,
                "patch": conversion.patch,
                "rerolled": True,
            }
            for conversion in plan.conversions
        ],
        "note": (
            "Harvest keeps the tier but re-rolls the value inside the tier; "
            "the listing value is a best case, not a guarantee."
        ),
    }


def _fetch_rows(client: TradeClient, ids: list[str], query_id: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    capped = ids[: max(0, limit)]
    for start in range(0, len(capped), 10):
        rows.extend(client.fetch(capped[start : start + 10], query_id))
    return rows


@click.group("trade")
def trade() -> None:
    """Search the official PoE1 trade site (read-only) and verify with PoB."""
    pass


@trade.command("stats")
@click.argument("text")
@click.option("--limit", default=5, show_default=True, help="How many suggestions to show.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_stats(text: str, limit: int, as_json: bool) -> None:
    """Resolve a modifier text to a trade stat id (diagnostic)."""
    ctx = click.get_current_context()
    try:
        entries = flatten_stats(TradeClient().stats())
    except TradeError as error:
        _trade_error(ctx, error)
        return

    match = resolve_stat(text, entries)
    suggestions = suggest_stats(text, entries, limit)
    if as_json:
        payload = {
            "query": text,
            "match": (
                {"id": match.id, "text": match.text, "score": match.score} if match else None
            ),
            "suggestions": [
                {"id": item.id, "text": item.text, "score": item.score} for item in suggestions
            ],
        }
        _echo(to_json_str(payload))
    else:
        if match:
            _echo(f"Resolved: `{match.text}` -> `{match.id}` (score {match.score})")
        else:
            _echo(f"No confident match for `{text}`.")
        if suggestions:
            _echo(
                render_table(
                    ["Text", "Stat id", "Score"],
                    [[item.text, item.id, f"{item.score:.3f}"] for item in suggestions],
                )
            )
    if match is None:
        _exit(ctx, 1)


@trade.command("search")
@click.option("--slot", help=f"Slot to upgrade ({SLOT_CHOICES}).")
@click.option("--stat", "stats", multiple=True, help='Hard filter, e.g. "to Cold Resistance=40".')
@click.option("--want", "wants", multiple=True, help="Soft preference (scoring only).")
@click.option("--weight", "weights", multiple=True, help="Override a scoring weight, e.g. resist=1.2.")
@click.option("--budget", type=float, help="Maximum price.")
@click.option("--currency", default="chaos", show_default=True, help="Budget currency.")
@click.option("--league", help="League name (defaults to the current softcore trade league).")
@click.option("--rarity", default="rare", show_default=True, help="Rarity filter (any to disable).")
@click.option("--ilvl-min", type=int, default=0, show_default=True, help="Minimum item level.")
@click.option("--category", help="Override the trade category, e.g. armour.gloves.")
@click.option("--no-category", is_flag=True, help="Do not filter by item category.")
@click.option("--allow-conversion", is_flag=True, help="Include Harvest-convertible elements in the search.")
@click.option(
    "--convert-to",
    type=click.Choice(["fire", "cold", "lightning"]),
    help="Target element when --allow-conversion is used.",
)
@click.option("--search-sort", type=click.Choice(["asc", "desc"]), default="asc", show_default=True)
@click.option("--sort", "sort_by", type=click.Choice(["score", "price", "pob"]), default="score", show_default=True)
@click.option("--max-results", type=int, default=8, show_default=True, help="Rows in the report.")
@click.option("--fetch-limit", type=int, default=10, show_default=True, help="Listings to fetch (max 20).")
@click.option("--open-prefix", is_flag=True, help="Only listings with at least one open prefix.")
@click.option("--open-suffix", is_flag=True, help="Only listings with at least one open suffix.")
@click.option("--open-affix", is_flag=True, help="Only listings with an open prefix or suffix.")
@click.option("--bench-hint/--no-bench-hint", default=True, show_default=True, help="Show crafting-bench options for the slot.")
@click.option("--pob", "pob_mode", type=click.Choice(["auto", "on", "off"]), default="auto", show_default=True)
@click.option("--pob-limit", type=int, default=5, show_default=True, help="Candidates to verify in PoB.")
@click.option("--target", type=click.Choice(["main", "ref"]), default="main")
@click.option("--current-item-file", type=click.Path(exists=True, dir_okay=False), help="Current item text (skips the daemon).")
@click.option("--no-compare", is_flag=True, help="Do not load the equipped item.")
@click.option("--dump-query", is_flag=True, help="Print the trade query and exit (no network search).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.option("--verbose", is_flag=True, help="Print HTTP request lines (never the cookie).")
def cmd_search(
    slot: str | None,
    stats: tuple[str, ...],
    wants: tuple[str, ...],
    weights: tuple[str, ...],
    budget: float | None,
    currency: str,
    league: str | None,
    rarity: str,
    ilvl_min: int,
    category: str | None,
    no_category: bool,
    allow_conversion: bool,
    convert_to: str | None,
    search_sort: str,
    sort_by: str,
    max_results: int,
    fetch_limit: int,
    open_prefix: bool,
    open_suffix: bool,
    open_affix: bool,
    bench_hint: bool,
    pob_mode: str,
    pob_limit: int,
    target: str,
    current_item_file: str | None,
    no_compare: bool,
    dump_query: bool,
    as_json: bool,
    verbose: bool,
) -> None:
    """Search listings, compare them with the equipped item and rank the upgrades."""
    ctx = click.get_current_context()
    if not slot and not category and not no_category:
        _err("Provide --slot (or --category / --no-category).")
        _exit(ctx, 1)

    resolved_category = None
    if not no_category:
        resolved_category = category or category_for_slot(slot or "")
        if not resolved_category:
            _err(f"Unknown slot {slot!r}; pass --category or --no-category.")
            _exit(ctx, 1)

    try:
        weight_map = parse_weights(weights)
        preferred = preferred_templates(wants)
        requests = [parse_stat_argument(value) for value in stats]
    except ValueError as error:
        _err(str(error))
        _exit(ctx, 1)
        return

    client = TradeClient(logger=(lambda line: _err(line)) if verbose else None)
    league_name = _resolve_league(league)

    try:
        entries = flatten_stats(client.stats())
    except TradeError as error:
        _trade_error(ctx, error)
        return

    pseudo_filters: list[dict[str, Any]] = []
    if open_prefix:
        pseudo_filters.append(
            {"id": OPEN_PREFIX_ID, "value": {"min": 1}, "disabled": False}
        )
    if open_suffix:
        pseudo_filters.append(
            {"id": OPEN_SUFFIX_ID, "value": {"min": 1}, "disabled": False}
        )
    if open_affix and not (open_prefix or open_suffix):
        pseudo_filters.append(
            {"id": OPEN_AFFIX_ID, "value": {"min": 1}, "disabled": False}
        )
    open_flags = {
        "prefix": bool(open_prefix),
        "suffix": bool(open_suffix),
        "any": bool(open_affix and not (open_prefix or open_suffix)),
    }

    bench_options: list[dict[str, Any]] = []
    if bench_hint and slot:
        seen: set[str] = set()
        for text in [request.text for request in requests] + list(wants):
            for result in bench_mod.search(slot, text, limit=2):
                recipe = result.recipe
                if recipe.text in seen:
                    continue
                seen.add(recipe.text)
                bench_options.append(recipe.as_dict())

    try:
        plan = plan_stats(
            requests,
            entries,
            allow_conversion=allow_conversion,
            convert_to=convert_to,
        )
        query = build_query(
            plan=plan,
            category=resolved_category,
            rarity=rarity,
            item_level_min=ilvl_min,
            currency=currency,
            budget=budget,
            search_sort=search_sort,
            pseudo_filters=pseudo_filters or None,
        )
    except ValueError as error:
        _err(str(error))
        _exit(ctx, 1)
        return

    if dump_query:
        payload = {
            "league": league_name,
            "slot": slot,
            "category": resolved_category,
            "conversion": _conversion_payload(plan),
            "openAffixes": open_flags,
            "benchOptions": bench_options,
            "query": query,
        }
        _echo(to_json_str(payload, sanitize=False))
        return

    try:
        payload = client.search(league_name, query)
    except TradeError as error:
        _trade_error(ctx, error)
        return

    query_id = str(payload.get("id") or "")
    ids = [str(item) for item in payload.get("result") or []]
    total = int(payload.get("total") or len(ids))

    try:
        rows = _fetch_rows(client, ids, query_id, min(fetch_limit, 20))
    except TradeError as error:
        _trade_error(ctx, error)
        return

    current = None
    current_error = None
    if current_item_file:
        current = parse_item_text(Path(current_item_file).read_text(encoding="utf-8", errors="replace"))
    elif not no_compare:
        pob_slot = pob_slot_for_slot(slot or "")
        if pob_slot:
            current, current_error = _read_current_item(pob_slot, target)
        else:
            current_error = f"no PoB slot mapping for {slot!r}"

    candidates: list[Candidate] = []
    for row in rows:
        raw_item = row.get("item") or {}
        try:
            text = build_item_text(raw_item)
        except ValueError:
            continue
        conversions = []
        if plan.conversions:
            tiers = trade_mod_tiers(raw_item)
            for conversion in plan.conversions:
                converted = convert_item_text(
                    text,
                    conversion.target,
                    group=conversion.group,
                    tiers=tiers,
                )
                text = converted.text
                conversions.extend(converted.conversions)
        try:
            item = parse_item_text(text)
        except ValueError:
            continue
        deltas = compare_items(current, item, weights=weight_map, preferred=preferred)
        amount, price_currency = _price_of(row)
        candidates.append(
            Candidate(
                raw=row,
                item=item,
                item_text=text,
                price_amount=amount,
                price_currency=price_currency,
                score=score_deltas(deltas),
                deltas=deltas,
                conversions=conversions,
                url=client.search_url(league_name, query_id) if query_id else "",
                listing_id=row.get("id"),
            )
        )

    candidates = sort_candidates(candidates, "score")
    pob_slot = pob_slot_for_slot(slot or "")

    if pob_mode != "off" and candidates:
        if not pob_slot:
            if pob_mode == "on":
                _err(f"PoB verification needs a supported --slot, got {slot!r}.")
                _exit(ctx, 6)
                return
            _err("PoB verification skipped: no PoB slot for this search.")
        else:
            for candidate in candidates[: max(0, pob_limit)]:
                try:
                    candidate.pob = verify_candidate(candidate, slot=pob_slot, target=target)
                except Exception as error:  # noqa: BLE001 - surface PoB failures verbatim
                    candidate.pob_error = sanitize_text(error, 300)
                    if pob_mode == "on":
                        _err(f"PoB verification failed: {candidate.pob_error}")
                        _exit(ctx, 6)
                        return
            if pob_mode == "auto" and all(candidate.pob is None for candidate in candidates):
                _err(
                    "PoB verification unavailable (daemon/PoB not usable); "
                    "the report only shows modifier deltas, not DPS."
                )

    ordered = sort_candidates(candidates, sort_by)[: max(0, max_results)]
    conversion_note = ""
    if plan.conversions:
        parts = []
        for conversion in plan.conversions:
            elements = "/".join(conversion.elements)
            parts.append(
                f"harvest {elements} -> {conversion.target} "
                f"(cost: {conversion.cost}; {conversion.patch} rules; "
                "tier kept, roll re-rolled)"
            )
        conversion_note = "; ".join(parts)

    search_url = client.search_url(league_name, query_id) if query_id else ""
    if as_json:
        payload = {
            "league": league_name,
            "slot": slot,
            "category": resolved_category,
            "query": query,
            "searchUrl": search_url,
            "total": total,
            "fetched": len(rows),
            "conversion": _conversion_payload(plan),
            "openAffixes": open_flags,
            "benchOptions": bench_options,
            "current": {
                "found": current is not None,
                "error": current_error,
                "name": current.display_name if current else None,
            },
            "candidates": report_json(ordered),
        }
        _echo(to_json_str(payload, sanitize=False))
        return

    _echo(
        format_report(
            build_label=_build_label() if (current is not None or not no_compare) else "unknown",
            slot=slot or resolved_category or "",
            current=current,
            candidates=ordered,
            league=league_name,
            budget=budget,
            currency=currency,
            search_url=search_url,
            total=total,
            fetched=len(rows),
            conversion_note=conversion_note,
            open_affixes=open_flags,
            bench_options=bench_options,
        )
    )
    if current_error:
        _err(f"Equipped item could not be read: {current_error}")


@trade.command("convert")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False), help="Item text file.")
@click.option("--text", "raw_text", help="Item text (PoB clipboard format).")
@click.option(
    "--to",
    "target",
    required=True,
    type=click.Choice(["fire", "cold", "lightning"]),
    help="Element to convert resistance/damage modifiers to.",
)
@click.option("--out", "out_path", type=click.Path(dir_okay=False), help="Write the converted text to a file.")
@click.option("--force", is_flag=True, help="Allow unique/corrupted items (not a real game action).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def cmd_convert(
    file_path: str | None,
    raw_text: str | None,
    target: str,
    out_path: str | None,
    force: bool,
    as_json: bool,
) -> None:
    """Rewrite Harvest-convertible modifiers to another element and show the cost."""
    ctx = click.get_current_context()
    if file_path:
        raw_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    if not raw_text:
        _err("Provide --text or --file.")
        _exit(ctx, 1)
        return

    converted = convert_item_text(raw_text, target, force=force)
    if out_path:
        Path(out_path).write_text(converted.text + "\n", encoding="utf-8")

    if as_json:
        payload = {
            "target": target,
            "text": converted.text,
            "conversions": [conversion.as_dict() for conversion in converted.conversions],
            "skipped": converted.skipped,
        }
        _echo(to_json_str(payload, sanitize=False))
        return

    _echo(f"### Converted item text (target: {target})")
    _echo("```text")
    _echo(converted.text)
    _echo("```")
    if converted.conversions:
        _echo("")
        _echo("Converted modifiers:")
        for conversion in converted.conversions:
            _echo(
                f"- {conversion.template} -> {conversion.converted_template} "
                f"(cost: {conversion.cost}; {conversion.patch} rules; tier kept, roll re-rolled)"
            )
    else:
        _echo("")
        _echo("No convertible modifiers were found.")
    if converted.skipped:
        _echo("")
        _echo("Skipped:")
        for reason in converted.skipped:
            _echo(f"- {reason}")
    _echo("")
    _echo(
        "> Harvest keeps the tier but re-rolls the value inside the tier. "
        "The text above keeps the listed value as a best case - verify with "
        "`pob item try --slot <slot> --raw <text>` before buying."
    )
