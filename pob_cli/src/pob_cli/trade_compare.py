"""Compare trade candidates with the equipped item, rank them and report.

No DPS is calculated here.  The comparison is deliberately limited to what both
sources can prove: modifier templates, their numeric values, and the asking
price.  Real DPS/EHP only comes from ``pob item try`` (the local headless PoB)
and is marked ``-`` when it was not verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .daemon import call_daemon
from .formatters import fmt_num, render_table
from .poe_item import Conversion, Item, mods_by_template, sanitize_text

DEFAULT_WEIGHTS: dict[str, float] = {
    "life": 1.0,
    "mana": 0.35,
    "energy_shield": 0.55,
    "armour": 0.35,
    "evasion": 0.35,
    "resist": 0.8,
    "attributes": 0.4,
    "spirit": 0.6,
    "damage": 1.0,
    "speed": 0.6,
    "crit": 0.5,
    "skill_level": 1.2,
    "accuracy": 0.2,
    "defense": 0.4,
    "rarity": 0.05,
    "penalty": -0.5,
    "other": 0.1,
}

PREFERRED_BONUS = 1.6
POB_EHP_WEIGHT = 0.25


@dataclass
class ModDelta:
    template: str
    category: str
    current: float
    candidate: float
    delta: float
    contribution: float
    example: str
    kind: str

    @property
    def is_gain(self) -> bool:
        return self.contribution > 0


@dataclass
class PobEval:
    dps_key: str
    dps_before: float
    dps_after: float
    ehp_before: float
    ehp_after: float
    life_before: float
    life_after: float
    warnings: tuple[str, ...] = ()

    @property
    def dps_pct(self) -> float:
        return _pct(self.dps_before, self.dps_after)

    @property
    def ehp_pct(self) -> float:
        return _pct(self.ehp_before, self.ehp_after)

    @property
    def life_delta(self) -> float:
        return self.life_after - self.life_before

    @property
    def combined_pct(self) -> float:
        return self.dps_pct + POB_EHP_WEIGHT * self.ehp_pct


@dataclass
class Candidate:
    raw: dict[str, Any]
    item: Item
    item_text: str
    price_amount: float | None = None
    price_currency: str | None = None
    score: float = 0.0
    deltas: list[ModDelta] = field(default_factory=list)
    conversions: list[Conversion] = field(default_factory=list)
    url: str = ""
    listing_id: str | None = None
    pob: PobEval | None = None
    pob_error: str | None = None

    @property
    def display_name(self) -> str:
        return self.item.display_name

    @property
    def price_text(self) -> str:
        if self.price_amount is None or not self.price_currency:
            return "no price"
        return f"{self.price_amount:g} {self.price_currency}"

    @property
    def gains(self) -> list[ModDelta]:
        return sorted((d for d in self.deltas if d.contribution > 0), key=lambda d: -d.contribution)

    @property
    def losses(self) -> list[ModDelta]:
        return sorted((d for d in self.deltas if d.contribution < 0), key=lambda d: d.contribution)


def _pct(before: float, after: float) -> float:
    if abs(before) < 1e-9:
        return 0.0
    return (after - before) / abs(before) * 100.0


def parse_weights(values: Iterable[str] | None) -> dict[str, float]:
    weights = dict(DEFAULT_WEIGHTS)
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"--weight needs category=value: {value}")
        category, raw = value.split("=", 1)
        category = category.strip().lower()
        if category not in weights:
            raise ValueError("Unknown weight category: " + category)
        weights[category] = float(raw)
    return weights


def compare_items(
    current: Item | None,
    candidate: Item,
    *,
    weights: dict[str, float] | None = None,
    preferred: set[str] | None = None,
) -> list[ModDelta]:
    weights = weights or DEFAULT_WEIGHTS
    preferred = preferred or set()

    current_totals = mods_by_template(current)
    candidate_totals = mods_by_template(candidate)
    current_examples = {mod.template: mod for mod in (current.mods if current else [])}
    candidate_examples = {mod.template: mod for mod in candidate.mods}

    deltas: list[ModDelta] = []
    for template in sorted(set(current_totals) | set(candidate_totals)):
        before = current_totals.get(template, 0.0)
        after = candidate_totals.get(template, 0.0)
        delta = after - before
        if abs(delta) < 1e-9:
            continue
        sample = candidate_examples.get(template) or current_examples.get(template)
        category = sample.category if sample else "other"
        contribution = delta * weights.get(category, weights.get("other", 0.1))
        if template in preferred:
            contribution *= PREFERRED_BONUS
        deltas.append(
            ModDelta(
                template=template,
                category=category,
                current=before,
                candidate=after,
                delta=delta,
                contribution=contribution,
                example=sample.raw if sample else template,
                kind=sample.kind if sample else "explicit",
            )
        )
    deltas.sort(key=lambda item: item.contribution, reverse=True)
    return deltas


def score_deltas(deltas: list[ModDelta]) -> float:
    return round(sum(delta.contribution for delta in deltas), 2)


def verify_candidate(
    candidate: Candidate,
    *,
    slot: str,
    target: str = "main",
    item_try: Callable[..., dict[str, Any]] = call_daemon,
) -> PobEval:
    """Run ``pob item try`` on the candidate text (what-if, never applied)."""
    payload = item_try(
        "item_try",
        {
            "target": target,
            "slot": slot,
            "raw": candidate.item_text,
            "tooltip_only": False,
            "apply": False,
        },
    )
    mutation = payload.get("mutation") or {}
    before = mutation.get("before") or {}
    after = mutation.get("after") or {}
    diff = mutation.get("diff") or {}
    return PobEval(
        dps_key=str(after.get("primaryDpsKey") or before.get("primaryDpsKey") or "CombinedDPS"),
        dps_before=float(before.get("primaryDps") or 0.0),
        dps_after=float(after.get("primaryDps") or 0.0),
        ehp_before=float(before.get("totalEhp") or 0.0),
        ehp_after=float(after.get("totalEhp") or 0.0),
        life_before=float(before.get("life") or 0.0),
        life_after=float(after.get("life") or 0.0),
        warnings=tuple(str(item) for item in (diff.get("newWarnings") or [])[:8]),
    )


def sort_candidates(candidates: list[Candidate], sort: str = "score") -> list[Candidate]:
    if sort == "price":
        return sorted(
            candidates,
            key=lambda c: (c.price_amount is None, c.price_amount or 0.0, -c.score),
        )
    if sort == "pob":
        return sorted(
            candidates,
            key=lambda c: (
                c.pob is None,
                -(c.pob.combined_pct if c.pob else 0.0),
                -c.score,
            ),
        )
    return sorted(
        candidates,
        key=lambda c: (-c.score, c.price_amount is None, c.price_amount or 0.0),
    )


def _escape(value: str) -> str:
    return sanitize_text(value).replace("|", "\\|")


def _format_delta(delta: ModDelta) -> str:
    return _escape(delta.example)


def format_report(
    *,
    build_label: str,
    slot: str,
    current: Item | None,
    candidates: list[Candidate],
    league: str,
    budget: float | None,
    currency: str,
    search_url: str,
    total: int,
    fetched: int,
    conversion_note: str = "",
    open_affixes: dict[str, bool] | None = None,
    bench_options: list[dict[str, Any]] | None = None,
) -> str:
    lines: list[str] = ["# Gear upgrade candidates (PoE1 official trade)", ""]
    lines.append(f"- Build: {build_label or 'unknown'}")
    lines.append(f"- Slot: {slot}")
    lines.append(f"- Current item: {current.display_name if current else 'empty'}")
    lines.append(f"- League: {league}")
    lines.append(f"- Budget: {f'{budget:g} {currency}' if budget else 'unlimited'}")
    lines.append(f"- Results: {total} matching, {fetched} fetched")
    if search_url:
        lines.append(f"- Trade link: {search_url}")
    if conversion_note:
        lines.append(f"- Conversion: {conversion_note}")
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

    if not candidates:
        lines.append("No analysable candidates.")
        return "\n".join(lines)

    has_pob = any(candidate.pob is not None for candidate in candidates)
    if has_pob:
        lines.append("| # | Item | Price | Score | PoB DPS | PoB EHP | Gains | Losses | Conversion |")
        lines.append("|---|------|-------|-------|---------|---------|-------|--------|------------|")
    else:
        lines.append("| # | Item | Price | Score | Gains | Losses | Conversion |")
        lines.append("|---|------|-------|-------|-------|--------|------------|")

    for index, candidate in enumerate(candidates, start=1):
        gains = "; ".join(_format_delta(delta) for delta in candidate.gains[:2]) or "-"
        losses = "; ".join(_format_delta(delta) for delta in candidate.losses[:2]) or "-"
        conversion = (
            ", ".join(
                f"{conversion.element}->{conversion.target}" for conversion in candidate.conversions
            )
            or "-"
        )
        if has_pob:
            dps = f"{candidate.pob.dps_pct:+.1f}%" if candidate.pob else "-"
            ehp = f"{candidate.pob.ehp_pct:+.1f}%" if candidate.pob else "-"
            lines.append(
                f"| {index} | {_escape(candidate.display_name)} | {_escape(candidate.price_text)} | "
                f"{candidate.score:g} | {dps} | {ehp} | {gains} | {losses} | {conversion} |"
            )
        else:
            lines.append(
                f"| {index} | {_escape(candidate.display_name)} | {_escape(candidate.price_text)} | "
                f"{candidate.score:g} | {gains} | {losses} | {conversion} |"
            )
    lines.append("")

    if bench_options:
        lines.append(f"## Crafting bench options for {slot or 'this slot'}")
        for recipe in bench_options:
            affix = str(recipe.get("affix") or "unknown")
            text = str(recipe.get("text") or "")
            text_zh = str(recipe.get("textZh") or "")
            values = recipe.get("values") or []
            rolls = []
            for value in values:
                low = value.get("min")
                high = value.get("max")
                rolls.append(f"{low:g}" if low == high else f"{low:g}-{high:g}")
            label = text + (f" / {text_zh}" if text_zh and text_zh != text else "")
            lines.append(
                f"- [{affix}] {label} ({', '.join(rolls) or '-'}; {recipe.get('cost') or '-'})"
            )
        lines.append("")
        lines.append(
            "> Bench modifiers roll below high-tier natural mods and need the matching "
            "open prefix/suffix. Buy the base, then craft; re-check in PoB with the crafted line."
        )
        lines.append("")

    for index, candidate in enumerate(candidates[:3], start=1):
        lines.append(f"## {index}. {candidate.display_name} - {candidate.price_text}")
        if candidate.url:
            lines.append(f"[Trade listing]({candidate.url})")
        lines.append("")
        if candidate.pob is not None:
            pob = candidate.pob
            lines.append(
                f"PoB: {pob.dps_key} {fmt_num(pob.dps_before)} -> {fmt_num(pob.dps_after)} "
                f"({pob.dps_pct:+.1f}%), EHP {fmt_num(pob.ehp_before)} -> {fmt_num(pob.ehp_after)} "
                f"({pob.ehp_pct:+.1f}%), life {pob.life_delta:+g}"
            )
            if pob.warnings:
                lines.append("PoB warnings: " + "; ".join(_escape(warning) for warning in pob.warnings))
            lines.append("")
        elif candidate.pob_error:
            lines.append(f"PoB verification failed: {_escape(candidate.pob_error)}")
            lines.append("")
        if candidate.conversions:
            lines.append("Required in-game conversion:")
            for conversion in candidate.conversions:
                tier = f"tier {conversion.tier}; " if conversion.tier else ""
                lines.append(
                    f"- {conversion.template} -> {conversion.converted_template} "
                    f"(cost: {conversion.cost}; {tier}{conversion.patch} rules; same tier, "
                    "roll re-rolled inside the tier - the listed value is a best case)"
                )
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

    lines.append(
        "> Mod score is a weighted template delta, not PoB DPS. `-` means the candidate "
        "was not verified, not 0 upgrade. PoB text is rebuilt from the listing and has no "
        "quality/socket details - re-check in PoB before buying."
    )
    return "\n".join(lines)


def report_json(candidates: list[Candidate]) -> list[dict[str, Any]]:
    return [
        {
            "name": candidate.display_name,
            "base_type": candidate.item.base_type,
            "rarity": candidate.item.rarity,
            "listing_id": candidate.listing_id,
            "price": {
                "amount": candidate.price_amount,
                "currency": candidate.price_currency,
            },
            "score": candidate.score,
            "url": candidate.url,
            "conversions": [conversion.as_dict() for conversion in candidate.conversions],
            "pob": (
                {
                    "dps_key": candidate.pob.dps_key,
                    "dps_before": candidate.pob.dps_before,
                    "dps_after": candidate.pob.dps_after,
                    "dps_pct": candidate.pob.dps_pct,
                    "ehp_before": candidate.pob.ehp_before,
                    "ehp_after": candidate.pob.ehp_after,
                    "ehp_pct": candidate.pob.ehp_pct,
                    "life_delta": candidate.pob.life_delta,
                    "warnings": list(candidate.pob.warnings),
                }
                if candidate.pob is not None
                else None
            ),
            "pob_error": candidate.pob_error,
            "gains": [
                {
                    "mod": delta.example,
                    "template": delta.template,
                    "current": delta.current,
                    "candidate": delta.candidate,
                    "delta": delta.delta,
                }
                for delta in candidate.gains
            ],
            "losses": [
                {
                    "mod": delta.example,
                    "template": delta.template,
                    "current": delta.current,
                    "candidate": delta.candidate,
                    "delta": delta.delta,
                }
                for delta in candidate.losses
            ],
            "item_text": candidate.item_text,
        }
        for candidate in candidates
    ]


def preferred_templates(values: Iterable[str] | None) -> set[str]:
    return {value.strip().lower() for value in values or [] if value.strip()}


def deltas_for_example(current: Item | None, candidate: Item) -> list[ModDelta]:
    """Small helper kept for tests and callers that only need the delta list."""
    return compare_items(current, candidate)
