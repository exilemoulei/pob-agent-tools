"""Trade query argument parsing, Harvest conversion planning, and JSON query builder."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..trade_item import ELEMENTS, convert_template, match_harvest
from .aliases import INDEXED_WINDOWS, VALID_STATUSES
from .stats_index import StatMatch, resolve_stat, suggest_stats

_SUM_MIN_SUFFIX_RE = re.compile(r">=\s*([+-]?\d+(?:\.\d+)?)\s*$")
_WEIGHT_SUFFIX_RE = re.compile(r"\*\s*([+-]?\d+(?:\.\d+)?)\s*$")


@dataclass
class StatRequest:
    text: str
    min_value: float = 1.0
    match: StatMatch | None = None


@dataclass
class ConversionPlan:
    """One ``--stat`` expanded into a count group over convertible elements."""

    group: str
    target: str
    min_value: float
    stat_ids: dict[str, str] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    cost: str = ""
    patch: str = ""

    @property
    def elements(self) -> list[str]:
        return list(self.stat_ids)


@dataclass
class WeightedStatEntry:
    """One modifier entry inside an official PoE1 ``type: "weight"`` (Weighted Sum) group."""

    text: str
    weight: float = 1.0
    min_value: float | None = None
    match: StatMatch | None = None


@dataclass
class WeightedGroup:
    """An official PoE1 ``type: "weight"`` stat group whose values sum into ``Sum: XX``."""

    entries: list[WeightedStatEntry] = field(default_factory=list)
    min_sum: float | None = None


@dataclass
class StatPlan:
    plain: list[StatRequest] = field(default_factory=list)
    conversions: list[ConversionPlan] = field(default_factory=list)
    weighted_groups: list[WeightedGroup] = field(default_factory=list)


def resolve_requests(
    requests: Iterable[StatRequest],
    entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
) -> None:
    """Resolve every ``StatRequest`` in place, raising ``ValueError`` with suggestions on miss."""
    for request in requests:
        match = resolve_stat(request.text, entries, slot=slot)
        if match is None:
            lines = [f"Could not map this modifier to a trade filter: {request.text}"]
            for suggestion in suggest_stats(request.text, entries, slot=slot):
                lines.append(f"  - {suggestion.text}  ({suggestion.id})")
            lines.append("Rewrite the text, or use --want (scoring only, no filter).")
            raise ValueError("\n".join(lines))
        request.match = match


def parse_stat_argument(value: str) -> StatRequest:
    """Parse ``"to Cold Resistance=40"``, ``"生命>=70"``, or ``"冰霜抗性=40"`` into a ``StatRequest``."""
    text = (value or "").strip()
    minimum = 1.0
    if "=" in text:
        candidate_text, candidate_min = text.rsplit("=", 1)
        try:
            minimum = float(candidate_min.strip())
            text = candidate_text.rstrip(">").strip()
        except ValueError:
            pass
    if not text:
        raise ValueError("--stat cannot be empty")
    return StatRequest(text=text, min_value=minimum)


def parse_sum_group_argument(value: str) -> WeightedGroup:
    """Parse a Weighted Sum group argument into a :class:`WeightedGroup`.

    Format: ``"stat1[*w1][=min1]|stat2[*w2][=min2]|...[>=min_sum]"`` (``;`` also accepted
    as separator). Example:
    ``"+#% to Global Critical Strike Multiplier|+#% to Critical Strike Multiplier for Spell Damage>=25"``.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("--sum cannot be empty")

    min_sum: float | None = None
    m_min = _SUM_MIN_SUFFIX_RE.search(raw)
    if m_min:
        parsed_min = float(m_min.group(1))
        if not math.isfinite(parsed_min):
            raise ValueError(f"Invalid --sum minimum: {m_min.group(1)}")
        min_sum = parsed_min
        raw = raw[: m_min.start()].strip()

    parts = [part.strip() for part in re.split(r"[|;]", raw) if part.strip()]
    if not parts:
        raise ValueError("--sum must include at least one modifier")

    entries: list[WeightedStatEntry] = []
    for part in parts:
        text = part
        weight = 1.0
        entry_min: float | None = None

        m_weight = _WEIGHT_SUFFIX_RE.search(text)
        if m_weight:
            parsed_w = float(m_weight.group(1))
            if not math.isfinite(parsed_w):
                raise ValueError(f"Invalid --sum weight: {m_weight.group(1)}")
            weight = parsed_w
            text = text[: m_weight.start()].strip()

        if "=" in text:
            candidate_text, candidate_min = text.rsplit("=", 1)
            try:
                parsed_entry_min = float(candidate_min.strip())
            except ValueError:
                parsed_entry_min = None
            if parsed_entry_min is not None:
                if not math.isfinite(parsed_entry_min):
                    raise ValueError(f"Invalid --sum stat min: {candidate_min.strip()}")
                entry_min = parsed_entry_min
                text = candidate_text.strip()

        if not text:
            raise ValueError(f"Empty modifier entry in --sum: {value!r}")
        entries.append(WeightedStatEntry(text=text, weight=weight, min_value=entry_min))

    return WeightedGroup(entries=entries, min_sum=min_sum)


def _conversion_plan(
    request: StatRequest,
    entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
) -> ConversionPlan | None:
    """Expand one convertible request into sibling-element stat IDs (slot-aware)."""
    if request.match is None:
        return None
    found = match_harvest(request.match.template)
    if found is None:
        return None

    stat_ids: dict[str, str] = {}
    templates: dict[str, str] = {}
    for element in found.group.elements:
        if element not in ELEMENTS:
            continue
        template = request.match.template
        if element != found.element:
            template = convert_template(template, element) or ""
        if not template:
            continue
        resolved = resolve_stat(template, entries, slot=slot)
        if resolved is None:
            continue
        stat_ids[element] = resolved.id
        templates[element] = resolved.template
    if len(stat_ids) < 2:
        return None
    return ConversionPlan(
        group=found.group.key,
        target=found.element,
        min_value=request.min_value,
        stat_ids=stat_ids,
        templates=templates,
        cost=found.group.cost,
        patch=found.group.patch,
    )


def plan_stats(
    requests: list[StatRequest],
    entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
    allow_conversion: bool = False,
    convert_to: str | None = None,
    rarity: str | None = "rare",
    weighted_groups: list[WeightedGroup] | None = None,
) -> StatPlan:
    """Resolve requests and optionally expand Harvest-convertible ones into count groups."""
    if allow_conversion and (rarity or "").strip().lower() == "unique":
        raise ValueError(
            "--allow-conversion cannot be combined with --rarity unique "
            "(uniques cannot use Harvest conversion)."
        )

    resolve_requests(requests, entries, slot=slot)
    plan = StatPlan()
    target_override = convert_to.strip().lower() if convert_to else None

    for request in requests:
        conversion = _conversion_plan(request, entries, slot=slot) if allow_conversion else None
        if conversion is None:
            plan.plain.append(request)
            continue
        if target_override:
            if target_override not in conversion.stat_ids:
                raise ValueError(
                    f"Cannot convert {request.match.text if request.match else request.text} "
                    f"to {target_override!r}; available: {', '.join(conversion.elements)}"
                )
            conversion.target = target_override
        plan.conversions.append(conversion)

    for group in weighted_groups or []:
        resolved_entries: list[WeightedStatEntry] = []
        for item in group.entries:
            match = resolve_stat(item.text, entries, slot=slot)
            if match is None:
                lines = [f"Could not map this --sum modifier to a trade filter: {item.text}"]
                for suggestion in suggest_stats(item.text, entries, slot=slot):
                    lines.append(f"  - {suggestion.text}  ({suggestion.id})")
                raise ValueError("\n".join(lines))
            resolved_entries.append(
                WeightedStatEntry(
                    text=item.text,
                    weight=item.weight,
                    min_value=item.min_value,
                    match=match,
                )
            )
        if resolved_entries:
            plan.weighted_groups.append(
                WeightedGroup(entries=resolved_entries, min_sum=group.min_sum)
            )

    return plan


def build_query(
    *,
    plan: StatPlan,
    category: str | None,
    rarity: str | None,
    item_level_min: int = 0,
    status: str = "available",
    currency: str | None = "chaos_equiv",
    budget: float | None = None,
    search_sort: str = "asc",
    pseudo_filters: list[dict[str, Any]] | None = None,
    exclude_corrupted_mirrored: bool | None = None,
    allow_corrupted: bool = False,
    max_age_hours: float | None = None,
) -> dict[str, Any]:
    """Build an official PoE1 ``/api/trade/search`` JSON request body."""
    status_norm = (status or "available").strip().lower()
    if status_norm not in VALID_STATUSES:
        raise ValueError(
            f"Invalid trade status {status!r}; expected one of: {', '.join(VALID_STATUSES)}"
        )

    stat_groups: list[dict[str, Any]] = []

    # Place weighted sum groups first (index 0) matching PoB TradeQueryRequests.lua,
    # so `statgroup.0` cleanly targets the primary weighted sum on the trade site.
    first_weight_idx: int | None = None
    for wgroup in plan.weighted_groups:
        wfilters: list[dict[str, Any]] = []
        for entry in wgroup.entries:
            if entry.match is None:
                continue
            val: dict[str, Any] = {"weight": entry.weight}
            if entry.min_value is not None:
                val["min"] = entry.min_value
            wfilters.append(
                {
                    "id": entry.match.id,
                    "value": val,
                    "disabled": False,
                }
            )
        if wfilters:
            if first_weight_idx is None:
                first_weight_idx = len(stat_groups)
            group_obj: dict[str, Any] = {
                "type": "weight",
                "filters": wfilters,
            }
            if wgroup.min_sum is not None:
                group_obj["value"] = {"min": wgroup.min_sum}
            stat_groups.append(group_obj)

    if plan.plain:
        stat_groups.append(
            {
                "type": "and",
                "filters": [
                    {
                        "id": request.match.id,
                        "value": {"min": request.min_value},
                        "disabled": False,
                    }
                    for request in plan.plain
                    if request.match is not None
                ],
            }
        )
    if pseudo_filters:
        stat_groups.append({"type": "and", "filters": list(pseudo_filters)})
    for conversion in plan.conversions:
        # PoE1 trade API rejects "type": "or"; a count group with min=1 is the supported OR form.
        stat_groups.append(
            {
                "type": "count",
                "value": {"min": 1},
                "filters": [
                    {
                        "id": stat_id,
                        "value": {"min": conversion.min_value},
                        "disabled": False,
                    }
                    for stat_id in conversion.stat_ids.values()
                ],
            }
        )

    query: dict[str, Any] = {"status": {"option": status_norm}}
    if stat_groups:
        query["stats"] = stat_groups

    filter_block: dict[str, Any] = {}
    type_filters: dict[str, Any] = {}
    if category:
        type_filters["category"] = {"option": category}
    if rarity and rarity.strip().lower() != "any":
        type_filters["rarity"] = {"option": rarity.strip().lower()}
    if item_level_min > 0:
        type_filters["ilvl"] = {"min": item_level_min}
    if type_filters:
        filter_block["type_filters"] = {"filters": type_filters}

    trade_filters: dict[str, Any] = {}
    if budget is not None and budget > 0:
        curr_norm = (currency or "").strip().lower()
        if curr_norm in ("", "chaos_equiv", "equiv"):
            price_filter: dict[str, Any] = {"max": budget}
        else:
            price_filter = {"option": curr_norm, "max": budget}
        trade_filters["price"] = price_filter
    if max_age_hours is not None and max_age_hours > 0:
        # The site only offers fixed windows: take the smallest one that covers the
        # limit; the caller trims the remainder from each listing's `indexed` time.
        window = next((opt for hours, opt in INDEXED_WINDOWS if max_age_hours <= hours), None)
        if window:
            trade_filters["indexed"] = {"option": window}
    if trade_filters:
        filter_block["trade_filters"] = {"filters": trade_filters}

    should_exclude = (
        (bool(plan.conversions) and not allow_corrupted)
        if exclude_corrupted_mirrored is None
        else bool(exclude_corrupted_mirrored)
    )
    if should_exclude:
        filter_block["misc_filters"] = {
            "filters": {
                "corrupted": {"option": "false"},
                "mirrored": {"option": "false"},
            }
        }

    if filter_block:
        query["filters"] = filter_block

    sort_norm = (search_sort or "asc").strip().lower()
    if sort_norm in ("sum", "weight"):
        if first_weight_idx is None:
            raise ValueError("--search-sort sum requires at least one --sum weighted group.")
        sort_block: dict[str, str] = {f"statgroup.{first_weight_idx}": "desc"}
    elif sort_norm in ("asc", "desc"):
        sort_block = {"price": sort_norm}
    else:
        raise ValueError(f"Invalid search_sort {search_sort!r}; expected asc, desc, or sum.")

    return {"query": query, "sort": sort_block}
