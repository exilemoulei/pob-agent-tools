"""Candidate sorting, Markdown upgrade report rendering, and RFC-8259 JSON serialization."""

from __future__ import annotations

import math
import re
from typing import Any

from ..formatters import fmt_num, render_table
from ..locale_cfg import current_lang
from ..trade_item import Item, sanitize_text
from .evaluator import (
    Candidate,
    ModDelta,
    _finite_float,
    _finite_sort_key,
    _json_safe_optional_float,
)

_DASH_RE = re.compile(r"[\u2013\u2014\u2212]")
_BENCH_RANGE_RE = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*\)")


def _age_text(hours: float | None) -> str:
    if hours is None:
        return "-"
    if hours < 1:
        return f"{hours * 60:.0f}m"
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.0f}d"


def _price_key(candidate: Candidate) -> tuple[bool, float]:
    # shortcut: compares raw amounts across currencies when no rate is known (poe.ninja
    # unreachable or an exotic currency); rank by the API's own order if that ever matters.
    value = candidate.price_chaos if candidate.price_chaos is not None else candidate.price_amount
    return (value is None or not math.isfinite(value), _finite_sort_key(value, 0.0))


def _vs_market_text(candidate: Candidate) -> str:
    if candidate.vs_market_pct is None:
        return "-"
    pct = round(candidate.vs_market_pct)
    return f"{pct:+d}%" + (" BARGAIN" if candidate.bargain else "")


def _buyout_tag(candidate: Candidate, lang: str) -> str:
    if candidate.trade_mode == "securable":
        label = "一口价" if lang == "zh-CN" else ("一口價" if lang == "zh-TW" else "Instant")
        if candidate.gold_fee is not None and candidate.gold_fee > 0:
            return f"[{label} · {candidate.gold_fee:,}G]"
        return f"[{label}]"
    label = "私聊" if lang in ("zh-CN", "zh-TW") else "Whisper"
    return f"[{label}]"


def _format_price_with_mode(candidate: Candidate, lang: str) -> str:
    if candidate.price_amount is None or not candidate.price_currency or not math.isfinite(candidate.price_amount):
        base = "no price"
    else:
        cur = candidate.price_currency.strip().lower()
        if cur == "chaos":
            base = f"{candidate.price_amount:g}c"
        elif cur == "divine":
            base = f"{candidate.price_amount:g}d"
        else:
            base = f"{candidate.price_amount:g} {candidate.price_currency}"
    if base == "no price" and candidate.trade_mode != "securable" and not candidate.raw.get("listing"):
        return base
    return f"{base} {_buyout_tag(candidate, lang)}"


def _seller_text(candidate: Candidate) -> str:
    base = candidate.seller or "-"
    if candidate.seller_duplicates_collapsed > 0:
        return f"{base} (+{candidate.seller_duplicates_collapsed} dup)"
    return base


def _candidate_chaos_floor(candidate: Candidate) -> float | None:
    val = candidate.price_chaos
    if val is None and (candidate.price_currency or "").strip().lower() == "chaos":
        val = candidate.price_amount
    if val is None or not math.isfinite(val) or val <= 0:
        return None
    return float(val)


def compute_price_floor_note(candidates: list[Candidate], *, status: str) -> str | None:
    """Summarize price-floor divergence on ``available`` searches when whisper floor < 70% of instant floor."""
    if status != "available":
        return None
    securable = [
        (chaos, cand)
        for cand in candidates
        if cand.trade_mode == "securable" and (chaos := _candidate_chaos_floor(cand)) is not None
    ]
    in_person = [
        (chaos, cand)
        for cand in candidates
        if cand.trade_mode == "in_person" and (chaos := _candidate_chaos_floor(cand)) is not None
    ]
    if not securable or not in_person:
        return None
    min_sec_chaos, min_sec_cand = min(securable, key=lambda pair: pair[0])
    min_inp_chaos, _ = min(in_person, key=lambda pair: pair[0])
    if min_inp_chaos < 0.7 * min_sec_chaos:
        fee_part = f" (+{min_sec_cand.gold_fee:,}G)" if (min_sec_cand.gold_fee and min_sec_cand.gold_fee > 0) else ""
        return f"Instant Buyout floor: {min_sec_chaos:g}c{fee_part} vs Whisper-only floor: {min_inp_chaos:g}c"
    return None


def sort_candidates(candidates: list[Candidate], sort: str = "score") -> list[Candidate]:
    """Sort candidates by ``score``, ``price``, ``sum``, or ``pob`` (always NaN-safe)."""
    if sort == "sum":
        return sorted(
            candidates,
            key=lambda c: (
                c.weighted_sum is None or not math.isfinite(c.weighted_sum),
                -_finite_sort_key(c.weighted_sum, 0.0),
                *_price_key(c),
                -_finite_sort_key(c.score, 0.0),
            ),
        )
    if sort == "price":
        return sorted(candidates, key=lambda c: (*_price_key(c), -_finite_sort_key(c.score, 0.0)))
    if sort == "pob":
        return sorted(
            candidates,
            key=lambda c: (
                c.pob is None,
                -_finite_sort_key(c.pob.combined_pct if c.pob is not None else None, 0.0),
                -_finite_sort_key(c.score, 0.0),
                *_price_key(c),
            ),
        )
    return sorted(candidates, key=lambda c: (-_finite_sort_key(c.score, 0.0), *_price_key(c)))


def _escape(value: str) -> str:
    return sanitize_text(value).replace("|", "\\|")


def _format_delta(delta: ModDelta) -> str:
    return _escape(delta.example)


def _build_crafted_min_line(text: str, values: list[dict[str, Any]]) -> str:
    """Resolve a bench recipe text to its safe minimum-roll ``{crafted}`` PoB line."""
    clean = _DASH_RE.sub("-", sanitize_text(text))
    if not clean:
        return ""

    def _pick_min(match: re.Match[str]) -> str:
        s1, s2 = match.group(1), match.group(2)
        return s1 if float(s1) <= float(s2) else s2

    resolved = _BENCH_RANGE_RE.sub(_pick_min, clean)
    if "#" in resolved and values:
        for val in values:
            low = val.get("min")
            if low is None:
                continue
            low_str = str(int(low)) if float(low).is_integer() else f"{float(low):g}"
            resolved = resolved.replace("#", low_str, 1)
    if not resolved.startswith("{crafted}"):
        resolved = f"{{crafted}}{resolved}"
    return resolved


def _format_report_header(
    *,
    build_label: str,
    slot: str,
    current: Item | None,
    league: str,
    status: str,
    budget: float | None,
    currency: str,
    search_url: str,
    total: int,
    fetched: int,
    conversion_note: str,
    open_affixes: dict[str, bool] | None,
    listed_note: str = "",
    market_note: str = "",
    price_floor_note: str = "",
    seller_capped_dropped: int = 0,
    max_per_seller: int | None = None,
) -> list[str]:
    lines: list[str] = ["# Gear upgrade candidates (PoE1 official trade)", ""]
    lines.append(f"- Build: {sanitize_text(build_label) or 'unknown'}")
    lines.append(f"- Slot: {sanitize_text(slot) or 'unknown'}")
    lines.append(f"- Current item: {sanitize_text(current.display_name) if current else 'empty'}")
    lines.append(f"- League: {sanitize_text(league) or 'unknown'}")
    if status:
        lines.append(f"- Status: {sanitize_text(status)}")
    budget_str = f"{budget:g} {sanitize_text(currency)}" if (budget is not None and budget > 0) else "unlimited"
    lines.append(f"- Budget: {budget_str}")
    lines.append(f"- Results: {total} matching, {fetched} fetched")
    if search_url:
        lines.append(f"- Trade link: {search_url}")
    if conversion_note:
        lines.append(f"- Conversion: {sanitize_text(conversion_note)}")
    if listed_note:
        lines.append(f"- Listed: {sanitize_text(listed_note)}")
    if market_note:
        lines.append(f"- Market: {sanitize_text(market_note)}")
    if price_floor_note:
        lines.append(f"- Price floor: {sanitize_text(price_floor_note)}")
    if seller_capped_dropped > 0:
        cap_hint = (
            f" (cap: {max_per_seller}/seller; pass --max-per-seller 0 for unlimited)"
            if max_per_seller and max_per_seller > 0
            else ""
        )
        lines.append(f"- Seller cap: {seller_capped_dropped} additional listing(s) hidden{cap_hint}")
    if open_affixes and any(open_affixes.values()):
        wanted = [key for key, enabled in open_affixes.items() if enabled]
        lines.append(
            "- Open affixes: "
            + ", ".join(wanted)
            + " (bench-craft fallback; the craft needs an open "
            + "/".join(wanted)
            + ")"
        )
    lines.append("")
    return lines


def _format_candidates_table(candidates: list[Candidate], *, has_listing: bool, lang: str) -> list[str]:
    has_pob = any(candidate.pob is not None for candidate in candidates)
    has_sum = any(
        candidate.weighted_sum is not None and math.isfinite(candidate.weighted_sum)
        for candidate in candidates
    )
    has_market = any(candidate.vs_market_pct is not None for candidate in candidates)
    headers = ["#", "Item", "Price"]
    if has_market:
        headers.append("vs market")
    if has_listing:
        headers.extend(["Seller", "Listed"])
    if has_sum:
        headers.append("Sum")
    headers.append("Score")
    if has_pob:
        headers.extend(["PoB DPS", "PoB EHP"])
    headers.extend(["Gains", "Losses", "Conversion"])

    rows: list[list[Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        gains = "; ".join(_format_delta(delta) for delta in candidate.gains[:2]) or "-"
        losses = "; ".join(_format_delta(delta) for delta in candidate.losses[:2]) or "-"
        conversion = (
            ", ".join(f"{conv.element}->{conv.target}" for conv in candidate.conversions)
            or "-"
        )
        item_label = candidate.display_name
        if candidate.pob is not None and candidate.pob.quality_normalized:
            item_label += " [+20%Q]"
        if candidate.bench_craft:
            craft = candidate.bench_craft
            item_label += f" [bench: {' / '.join(craft['lines'])}; +{craft['cost']}]"
        score_str = f"{_finite_float(candidate.score):g}"
        row_cells: list[Any] = [
            index,
            _escape(item_label),
            _escape(_format_price_with_mode(candidate, lang)),
        ]
        if has_market:
            row_cells.append(_vs_market_text(candidate))
        if has_listing:
            row_cells.extend([_escape(_seller_text(candidate)), _age_text(candidate.age_hours)])
        if has_sum:
            sum_str = (
                f"{candidate.weighted_sum:g}"
                if (candidate.weighted_sum is not None and math.isfinite(candidate.weighted_sum))
                else "-"
            )
            row_cells.append(sum_str)
        row_cells.append(score_str)
        if has_pob:
            dps = candidate.pob.dps_pct_str if candidate.pob is not None else "-"
            ehp = candidate.pob.ehp_pct_str if candidate.pob is not None else "-"
            row_cells.extend([_escape(dps), _escape(ehp)])
        row_cells.extend([gains, losses, _escape(conversion)])
        rows.append(row_cells)

    return [render_table(headers, rows), ""]


def _format_bench_options(slot: str, bench_options: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = [f"## Crafting bench options for {sanitize_text(slot) or 'this slot'}"]
    for recipe in bench_options:
        affix = sanitize_text(recipe.get("affix") or "unknown")
        text = sanitize_text(recipe.get("text") or "")
        text_zh = sanitize_text(recipe.get("textZh") or "")
        values = recipe.get("values") or []
        rolls: list[str] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            low = value.get("min")
            high = value.get("max")
            if low is None and high is None:
                continue
            if low is None:
                rolls.append(f"{float(high):g}")
            elif high is None or low == high:
                rolls.append(f"{float(low):g}")
            else:
                rolls.append(f"{float(low):g}-{float(high):g}")
        label = text + (f" / {text_zh}" if text_zh and text_zh != text else "")
        cost = sanitize_text(recipe.get("cost") or "-")

        raw_crafted = recipe.get("craftedLinesMin") or recipe.get("crafted_lines_min") or []
        if isinstance(raw_crafted, str):
            raw_crafted = [raw_crafted]
        crafted_lines = [
            line if str(line).startswith("{crafted}") else f"{{crafted}}{line}"
            for line in raw_crafted
            if str(line).strip()
        ]
        if not crafted_lines and text:
            fallback_line = _build_crafted_min_line(text, values)
            if fallback_line:
                crafted_lines = [fallback_line]
        crafted_part = f" -> `{' / '.join(sanitize_text(c) for c in crafted_lines)}`" if crafted_lines else ""

        lines.append(
            f"- [{affix}] {label} ({', '.join(rolls) or '-'}; {cost}){crafted_part}"
        )
    lines.append("")
    lines.append(
        "> Bench modifiers roll below high-tier natural mods and need the matching "
        "open prefix/suffix. Hints show minimum-roll reference lines; `--bench-craft` "
        "simulates maximum rolls. Re-check the crafted result in PoB."
    )
    lines.append("")
    return lines


def _format_candidate_detail(
    index: int,
    candidate: Candidate,
    *,
    has_listing: bool = False,
    lang: str = "zh-CN",
) -> list[str]:
    lines: list[str] = [
        f"## {index}. {sanitize_text(candidate.display_name)} - {sanitize_text(_format_price_with_mode(candidate, lang))}"
    ]
    if candidate.url:
        lines.append(f"[Trade listing]({candidate.url})")
    if has_listing:
        lines.append(f"Seller: {_escape(_seller_text(candidate))} | Listed: {_age_text(candidate.age_hours)} ago")
    if candidate.vs_market_pct is not None:
        lines.append(f"Price vs market: {_vs_market_text(candidate)}")
    if candidate.bench_craft:
        craft = candidate.bench_craft
        lines.append(
            f"Bench ({craft['action']}, maximum roll): {' / '.join(craft['lines'])}; "
            f"cost: {craft['cost']}" + (" + remove existing craft" if craft["action"] == "replace_craft" else "")
        )
        if craft["action"] == "replace_craft":
            lines.append("Removed craft: " + "; ".join(_escape(line) for line in craft["removed_lines"]))
        lines.append("Maximum-roll target; repeated crafting/removal costs are not included.")
    lines.append("")
    if candidate.pob is not None:
        pob = candidate.pob
        q_tag = " [+20%Q]" if pob.quality_normalized else ""
        lines.append(
            f"PoB{q_tag}: {pob.dps_key} {fmt_num(pob.dps_before)} -> {fmt_num(pob.dps_after)} "
            f"({pob.dps_pct_str}), EHP {fmt_num(pob.ehp_before)} -> {fmt_num(pob.ehp_after)} "
            f"({pob.ehp_pct_str}), life {pob.life_delta:+g}"
        )
        if pob.warnings:
            lines.append("PoB warnings: " + "; ".join(_escape(warning) for warning in pob.warnings))
        if pob.resolved_warnings:
            lines.append(
                "PoB resolved warnings: "
                + "; ".join(_escape(warning) for warning in pob.resolved_warnings)
            )
        lines.append("")
    elif candidate.pob_error:
        lines.append(f"PoB verification failed: {_escape(candidate.pob_error)}")
        lines.append("")

    if candidate.conversions:
        lines.append("Required in-game conversion:")
        for conversion in candidate.conversions:
            tier_parts: list[str] = []
            if conversion.tier:
                tier_parts.append(f"tier {conversion.tier}")
            if conversion.tier_range:
                tier_parts.append(f"range {conversion.tier_range}")
            tier_prefix = ("; ".join(tier_parts) + "; ") if tier_parts else ""
            lines.append(
                f"- {conversion.template} -> {conversion.converted_template} "
                f"(cost: {conversion.cost}; {tier_prefix}{conversion.patch} rules; same tier, "
                "roll re-rolled inside the tier - the listed value is a best case)"
            )
        for warning in candidate.conversion_warnings:
            lines.append(f"- Warning: {sanitize_text(warning)}")
        lines.append("")
    elif candidate.conversion_warnings:
        lines.append("Conversion warnings:")
        for warning in candidate.conversion_warnings:
            lines.append(f"- {sanitize_text(warning)}")
        lines.append("")

    if candidate.gains:
        lines.append("Gains:")
        for delta in candidate.gains[:6]:
            lines.append(f"- {_format_delta(delta)} ({delta.current:g} -> {delta.candidate:g})")
        lines.append("")
    if candidate.losses:
        lines.append("Losses:")
        for delta in candidate.losses[:6]:
            lines.append(f"- {_format_delta(delta)} ({delta.current:g} -> {delta.candidate:g})")
        lines.append("")
    return lines


def format_report(
    *,
    build_label: str,
    slot: str,
    current: Item | None,
    candidates: list[Candidate],
    league: str,
    status: str = "",
    budget: float | None = None,
    currency: str = "chaos",
    search_url: str = "",
    total: int = 0,
    fetched: int = 0,
    conversion_note: str = "",
    open_affixes: dict[str, bool] | None = None,
    bench_options: list[dict[str, Any]] | None = None,
    listed_note: str = "",
    market_note: str = "",
    price_floor_note: str | None = None,
    seller_capped_dropped: int = 0,
    max_per_seller: int | None = None,
) -> str:
    """Render a high-SNR GitHub-flavored Markdown report for trade upgrade candidates."""
    lang = current_lang()
    resolved_floor = (
        price_floor_note
        if price_floor_note is not None
        else (compute_price_floor_note(candidates, status=status) or "")
    )
    lines = _format_report_header(
        build_label=build_label,
        slot=slot,
        current=current,
        league=league,
        status=status,
        budget=budget,
        currency=currency,
        search_url=search_url,
        total=total,
        fetched=fetched,
        conversion_note=conversion_note,
        open_affixes=open_affixes,
        listed_note=listed_note,
        market_note=market_note,
        price_floor_note=resolved_floor,
        seller_capped_dropped=seller_capped_dropped,
        max_per_seller=max_per_seller,
    )

    if not candidates:
        lines.append("No analysable candidates.")
        return "\n".join(lines)

    has_listing = any(candidate.seller or candidate.age_hours is not None for candidate in candidates)
    lines.extend(_format_candidates_table(candidates, has_listing=has_listing, lang=lang))

    if bench_options:
        lines.extend(_format_bench_options(slot, bench_options))

    for index, candidate in enumerate(candidates[:3], start=1):
        lines.extend(_format_candidate_detail(index, candidate, has_listing=has_listing, lang=lang))

    lines.append(
        "> Mod score is a weighted template delta, not PoB DPS. `-` means the candidate "
        "was not verified, not 0 upgrade. PoB text is rebuilt from the listing (and Harvest "
        "conversions re-roll within the same tier) - re-check in PoB before buying."
    )
    return "\n".join(lines)


def report_json(candidates: list[Candidate]) -> list[dict[str, Any]]:
    """Serialize candidates to strictly RFC-8259 compliant JSON dicts (zero NaN/Inf)."""
    return [
        {
            "name": candidate.display_name,
            "base_type": candidate.item.base_type,
            "rarity": candidate.item.rarity,
            "listing_id": candidate.listing_id,
            "price": {
                "amount": _json_safe_optional_float(candidate.price_amount),
                "currency": candidate.price_currency,
                "chaos": _json_safe_optional_float(candidate.price_chaos),
            },
            "tradeMode": candidate.trade_mode,
            "goldFee": candidate.gold_fee,
            "priceType": candidate.price_type,
            "sellerDuplicatesCollapsed": candidate.seller_duplicates_collapsed,
            "vs_market_pct": _json_safe_optional_float(candidate.vs_market_pct),
            "bargain": candidate.bargain,
            "seller": candidate.seller,
            "age_hours": _json_safe_optional_float(candidate.age_hours),
            "weighted_sum": _json_safe_optional_float(candidate.weighted_sum),
            "score": _finite_float(candidate.score),
            "url": candidate.url,
            "conversions": [conversion.as_dict() for conversion in candidate.conversions],
            "conversion_warnings": list(candidate.conversion_warnings),
            "pob": candidate.pob.as_dict() if candidate.pob is not None else None,
            "pob_error": candidate.pob_error,
            "bench_craft": candidate.bench_craft,
            "gains": [
                {
                    "mod": delta.example,
                    "template": delta.template,
                    "category": delta.category,
                    "current": _finite_float(delta.current),
                    "candidate": _finite_float(delta.candidate),
                    "delta": _finite_float(delta.delta),
                    "contribution": _finite_float(delta.contribution),
                }
                for delta in candidate.gains
            ],
            "losses": [
                {
                    "mod": delta.example,
                    "template": delta.template,
                    "category": delta.category,
                    "current": _finite_float(delta.current),
                    "candidate": _finite_float(delta.candidate),
                    "delta": _finite_float(delta.delta),
                    "contribution": _finite_float(delta.contribution),
                }
                for delta in candidate.losses
            ],
            "item_text": candidate.item_text,
        }
        for candidate in candidates
    ]
