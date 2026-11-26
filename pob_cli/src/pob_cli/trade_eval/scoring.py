"""Candidate modifier scoring, numeric safety helpers, and evaluation data models."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..formatters import _parse_numeric_or_none, fmt_delta, fmt_num
from ..trade_item import (
    Conversion,
    Item,
    mods_by_template,
    normalize_template,
)
from .surrogate import (
    MIN_SURROGATE_PREFILTER_K as MIN_SURROGATE_PREFILTER_K,
)
from .surrogate import (
    _weight_scalar as _weight_scalar,
)
from .surrogate import (
    compute_grad_weights as compute_grad_weights,
)
from .surrogate import (
    extract_item_surrogate_features as extract_item_surrogate_features,
)
from .surrogate import (
    prefilter_candidates_with_surrogate as prefilter_candidates_with_surrogate,
)
from .surrogate import (
    score_candidate_surrogate as score_candidate_surrogate,
)

DEFAULT_WEIGHTS: dict[str, float] = {
    "life": 1.0,
    "mana": 0.35,
    "energy_shield": 0.55,
    "armour": 0.35,
    "evasion": 0.35,
    "resist": 0.8,
    "attributes": 0.4,
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


# ---------------------------------------------------------------------------
# Numeric & JSON safety helpers (hardened against PoB 'inf' / '-inf' / 'nan')
# ---------------------------------------------------------------------------


def _safe_pct(before: Any, after: Any) -> float | None:
    """Compute rounded percentage change safely; returns None for non-finite or zero-base inputs."""
    if before is None or after is None:
        return None
    if isinstance(before, str) and not before.strip():
        return None
    if isinstance(after, str) and not after.strip():
        return None
    bf = _parse_numeric_or_none(before)
    af = _parse_numeric_or_none(after)
    if bf is None or af is None:
        return None
    if not (math.isfinite(bf) and math.isfinite(af)):
        return None
    if abs(bf) < 1e-9:
        return 0.0 if abs(af) < 1e-9 else None
    return round((af - bf) / abs(bf) * 100.0, 2)


def _finite_float(val: Any, default: float = 0.0) -> float:
    """Parse any value to a strictly finite float (never NaN/Inf)."""
    if val is None:
        return default
    if isinstance(val, str) and not val.strip():
        return default
    parsed = _parse_numeric_or_none(val)
    if parsed is None or not math.isfinite(parsed):
        return default
    return float(parsed)


def _json_safe_optional_float(val: Any) -> float | None:
    """Return a finite float or None (for RFC-8259 JSON fields like price_amount)."""
    if val is None:
        return None
    parsed = _parse_numeric_or_none(val)
    if parsed is None or not math.isfinite(parsed):
        return None
    return float(parsed)


def _json_safe_stat(val: Any) -> float | str:
    """Convert a PoB stat value into a finite float or sentinel string ('inf', '-inf', 'nan')."""
    if val is None:
        return 0.0
    if isinstance(val, bool):
        return float(int(val))
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else fmt_num(f)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0.0
        parsed = _parse_numeric_or_none(s)
        if parsed is None:
            return s
        return parsed if math.isfinite(parsed) else fmt_num(parsed)
    return str(val)


def _is_pos_inf(val: Any) -> bool:
    if val is None or (isinstance(val, str) and not val.strip()):
        return False
    parsed = _parse_numeric_or_none(val)
    return parsed is not None and math.isinf(parsed) and parsed > 0


def _finite_sort_key(val: float | None, default: float = 0.0) -> float:
    if val is None or not math.isfinite(val):
        return default
    return float(val)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


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
    dps_before: float | str
    dps_after: float | str
    ehp_before: float | str
    ehp_after: float | str
    life_before: float
    life_after: float
    warnings: tuple[str, ...] = ()
    resolved_warnings: tuple[str, ...] = ()
    listed_quality: int | None = None
    evaluated_quality: int | None = None
    quality_normalized: bool = False

    @property
    def dps_pct(self) -> float | None:
        return _safe_pct(self.dps_before, self.dps_after)

    @property
    def ehp_pct(self) -> float | None:
        return _safe_pct(self.ehp_before, self.ehp_after)

    @property
    def life_delta(self) -> float:
        bf = _parse_numeric_or_none(self.life_before)
        af = _parse_numeric_or_none(self.life_after)
        if bf is None or af is None or not (math.isfinite(bf) and math.isfinite(af)):
            return 0.0
        return round(af - bf, 4)

    @property
    def combined_pct(self) -> float:
        dps = self.dps_pct if (self.dps_pct is not None and math.isfinite(self.dps_pct)) else 0.0
        ehp = self.ehp_pct if (self.ehp_pct is not None and math.isfinite(self.ehp_pct)) else 0.0
        return round(dps + POB_EHP_WEIGHT * ehp, 4)

    @property
    def dps_pct_str(self) -> str:
        pct = self.dps_pct
        if pct is not None:
            return f"{pct:+.1f}%"
        if self.dps_before is None or self.dps_after is None:
            return "-"
        return fmt_delta(self.dps_before, self.dps_after)

    @property
    def ehp_pct_str(self) -> str:
        pct = self.ehp_pct
        if pct is not None:
            return f"{pct:+.1f}%"
        if _is_pos_inf(self.ehp_before) and _is_pos_inf(self.ehp_after):
            return "0% (inf)"
        if self.ehp_before is None or self.ehp_after is None:
            return "-"
        return fmt_delta(self.ehp_before, self.ehp_after)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dps_key": self.dps_key,
            "dps_before": _json_safe_stat(self.dps_before),
            "dps_after": _json_safe_stat(self.dps_after),
            "dps_pct": self.dps_pct,
            "ehp_before": _json_safe_stat(self.ehp_before),
            "ehp_after": _json_safe_stat(self.ehp_after),
            "ehp_pct": self.ehp_pct,
            "life_before": _json_safe_stat(self.life_before),
            "life_after": _json_safe_stat(self.life_after),
            "life_delta": self.life_delta,
            "combined_pct": self.combined_pct,
            "warnings": list(self.warnings),
            "resolved_warnings": list(self.resolved_warnings),
            "listedQuality": self.listed_quality,
            "evaluatedQuality": self.evaluated_quality,
            "qualityNormalized": self.quality_normalized,
        }


def listing_age_hours(row: dict[str, Any], now: datetime | None = None) -> float | None:
    """Hours since the trade site indexed a fetched listing row (None when unknown)."""
    listing = row.get("listing") if isinstance(row, dict) else None
    try:
        then = datetime.fromisoformat(str((listing or {}).get("indexed")))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(UTC)) - then).total_seconds() / 3600)


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
    conversion_warnings: list[str] = field(default_factory=list)
    url: str = ""
    listing_id: str | None = None
    weighted_sum: float | None = None
    price_chaos: float | None = None  # price in chaos when the currency rate is known
    vs_market_pct: float | None = None  # price vs the going price; negative = cheaper
    bargain: bool = False
    trade_mode: str = "in_person"
    gold_fee: int | None = None
    price_type: str | None = None
    seller_duplicates_collapsed: int = 0
    pob: PobEval | None = None
    pob_error: str | None = None
    bench_craft: dict[str, Any] | None = None

    @property
    def display_name(self) -> str:
        return self.item.display_name

    @property
    def seller(self) -> str | None:
        listing = self.raw.get("listing") if isinstance(self.raw, dict) else None
        account = (listing or {}).get("account")
        name = account.get("name") if isinstance(account, dict) else None
        return str(name) if name else None

    @property
    def age_hours(self) -> float | None:
        return listing_age_hours(self.raw)

    @property
    def price_text(self) -> str:
        if self.price_amount is None or not self.price_currency:
            return "no price"
        if not math.isfinite(self.price_amount):
            return "no price"
        return f"{self.price_amount:g} {self.price_currency}"

    @property
    def gains(self) -> list[ModDelta]:
        return sorted((d for d in self.deltas if d.contribution > 0), key=lambda d: -d.contribution)

    @property
    def losses(self) -> list[ModDelta]:
        return sorted((d for d in self.deltas if d.contribution < 0), key=lambda d: d.contribution)


# ---------------------------------------------------------------------------
# Weights & modifier comparison
# ---------------------------------------------------------------------------


def parse_weights(values: Iterable[str] | None) -> dict[str, float]:
    """Parse ``category=value`` CLI overrides on top of :data:`DEFAULT_WEIGHTS`."""
    weights = dict(DEFAULT_WEIGHTS)
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"--weight needs category=value: {value}")
        category, raw = value.split("=", 1)
        category = category.strip().lower()
        if category not in weights:
            raise ValueError(f"Unknown weight category: {category}")
        parsed = float(raw)
        if not math.isfinite(parsed):
            raise ValueError(f"Weight for {category} must be finite: {raw}")
        weights[category] = parsed
    return weights


def preferred_templates(values: Iterable[str] | None) -> set[str]:
    """Normalize ``--want`` preferences into lowercase templates/substrings."""
    result: set[str] = set()
    for value in values or []:
        if not value or not str(value).strip():
            continue
        norm = normalize_template(str(value))
        if norm:
            result.add(norm)
    return result


def _matches_preferred(template: str, preferred: set[str]) -> bool:
    if not preferred:
        return False
    if template in preferred:
        return True
    return any(pref and pref in template for pref in preferred)


def compare_items(
    current: Item | None,
    candidate: Item,
    *,
    weights: dict[str, float] | None = None,
    preferred: set[str] | None = None,
) -> list[ModDelta]:
    """Compare normalized modifier templates between ``current`` and ``candidate``."""
    weights = weights or DEFAULT_WEIGHTS
    norm_preferred = {
        normalize_template(p)
        for p in (preferred or ())
        if p and str(p).strip()
    }
    norm_preferred.discard("")

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
        if _matches_preferred(template, norm_preferred):
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
    """Sum weighted template delta contributions into a finite rounded score."""
    total = sum(d.contribution for d in deltas if math.isfinite(d.contribution))
    return round(total, 2)


def deltas_for_example(current: Item | None, candidate: Item) -> list[ModDelta]:
    """Small helper kept for tests and callers that only need the delta list."""
    return compare_items(current, candidate)


def _clone_dedupe_key(candidate: Candidate) -> tuple[Any, ...] | None:
    seller = (candidate.seller or "").strip().lower()
    if not seller:
        return None
    mod_sig = tuple(sorted((m.kind, m.template, m.values) for m in candidate.item.mods))
    price_amt = _json_safe_optional_float(candidate.price_amount)
    price_cur = (candidate.price_currency or "").strip().lower()
    return (seller, candidate.item.base_type.strip().lower(), mod_sig, price_amt, price_cur)


def dedupe_candidates_with_stats(
    candidates: list[Candidate],
    *,
    max_per_seller: int = 2,
) -> tuple[list[Candidate], int]:
    """Collapse exact seller clones, then cap retained candidates per seller before PoB eval.

    Returns ``(retained_candidates, seller_capped_dropped)`` where ``seller_capped_dropped``
    counts distinct listings hidden by ``max_per_seller`` (excluding exact clones collapsed
    into ``seller_duplicates_collapsed``).
    """
    # Step A: exact clone collapse
    collapsed: list[Candidate] = []
    seen_clones: dict[tuple[Any, ...], Candidate] = {}
    for cand in candidates:
        key = _clone_dedupe_key(cand)
        if key is not None and key in seen_clones:
            seen_clones[key].seller_duplicates_collapsed += 1
            continue
        if key is not None:
            seen_clones[key] = cand
        collapsed.append(cand)

    if max_per_seller <= 0:
        return collapsed, 0

    # Step B: per-seller cap
    capped: list[Candidate] = []
    seller_counts: dict[str, int] = {}
    seller_capped_dropped = 0
    for cand in collapsed:
        seller = (cand.seller or "").strip().lower()
        if not seller:
            capped.append(cand)
            continue
        count = seller_counts.get(seller, 0)
        if count >= max_per_seller:
            seller_capped_dropped += 1
            continue
        seller_counts[seller] = count + 1
        capped.append(cand)
    return capped, seller_capped_dropped


def dedupe_candidates_by_seller(
    candidates: list[Candidate],
    *,
    max_per_seller: int = 2,
) -> list[Candidate]:
    """Collapse exact seller clones, then cap retained candidates per seller before PoB eval."""
    return dedupe_candidates_with_stats(candidates, max_per_seller=max_per_seller)[0]
