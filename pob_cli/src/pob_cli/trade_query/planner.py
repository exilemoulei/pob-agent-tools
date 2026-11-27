"""Trade query argument parsing, Harvest conversion planning, and JSON query builder."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..trade_item import ELEMENTS, convert_template, match_harvest
from .aliases import INDEXED_WINDOWS, VALID_STATUSES
from .gem_filters import apply_exact_item_filters
from .option_stats import resolve_option_stat, split_pipe_stat_id
from .stats_index import StatMatch, resolve_stat, suggest_stats

_SUM_MIN_SUFFIX_RE = re.compile(r">=\s*([+-]?\d+(?:\.\d+)?)\s*$")
_WEIGHT_SUFFIX_RE = re.compile(r"\*\s*([+-]?\d+(?:\.\d+)?)\s*$")
_STAT_DOMAIN_PREFIX_RE = re.compile(
    r"^(implicit|enchant|fractured|crafted|explicit|pseudo)\s*:\s*(.+)$",
    re.IGNORECASE,
)
_RANGE_BOUNDS_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)?\s*\.\.\s*([+-]?\d+(?:\.\d+)?)?\s*$")


@dataclass
class StatRequest:
    text: str
    min_value: float | None = 1.0
    match: StatMatch | None = None
    max_value: float | None = None
    domain: str | None = None
    option_value: str | None = None
    option_id: int | str | None = None


@dataclass
class ConversionPlan:
    """One ``--stat`` expanded into a count group over convertible elements."""

    group: str
    target: str
    min_value: float | None
    stat_ids: dict[str, str] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    cost: str = ""
    patch: str = ""
    max_value: float | None = None

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


def _reject_if_ambiguous_aura(raw_text: str, match: StatMatch) -> None:
    if not (match.is_ambiguous_bare_aura and len(match.disambiguation_candidates) > 1):
        return
    aura_lbl = match.disambiguation_aura or "aura"
    lines = [
        f"Ambiguous bare aura modifier ({aura_lbl}): {raw_text!r} matches multiple Watcher's Eye stats; "
        "specify the exact effect line:"
    ]
    for cand in match.disambiguation_candidates:
        lines.append(f"  - {cand['text']}  ({cand['id']})")
    raise ValueError("\n".join(lines))


def _scope_entries_by_domain(entries: list[dict[str, Any]], domain: str | None) -> list[dict[str, Any]]:
    if not domain:
        return entries
    prefix = f"{domain.strip().lower()}."
    return [e for e in entries if str(e.get("id") or "").lower().startswith(prefix)]


def _plain_stat_filter(request: StatRequest) -> dict[str, Any]:
    assert request.match is not None
    base_id, pipe_opt = split_pipe_stat_id(request.match.id)
    opt_val = request.option_id if request.option_id is not None else pipe_opt
    if opt_val is not None:
        return {"id": base_id, "value": {"option": opt_val}, "disabled": False}
    val: dict[str, Any] = {}
    if request.min_value is not None:
        val["min"] = request.min_value
    if request.max_value is not None:
        val["max"] = request.max_value
    return {"id": base_id, "value": val, "disabled": False}


def resolve_requests(
    requests: Iterable[StatRequest],
    entries: list[dict[str, Any]],
    *,
    slot: str | None = None,
) -> None:
    """Resolve every ``StatRequest`` in place, raising ``ValueError`` with suggestions on miss."""
    for request in requests:
        scoped = _scope_entries_by_domain(entries, request.domain)
        opt_match = resolve_option_stat(
            request.text,
            scoped,
            option_value=request.option_value,
            domain=request.domain,
        )
        if opt_match is not None:
            request.option_id = opt_match.option_id
            request.match = StatMatch(
                id=opt_match.id,
                text=opt_match.text,
                score=opt_match.score,
                template=opt_match.text.lower(),
            )
            continue
        if request.option_value is not None:
            raise ValueError(
                f"Could not map enumerated option stat: {request.text}={request.option_value}"
            )
        match = resolve_stat(request.text, scoped, slot=slot)
        if match is None:
            lines = [f"Could not map this modifier to a trade filter: {request.text}"]
            for suggestion in suggest_stats(request.text, scoped, slot=slot):
                lines.append(f"  - {suggestion.text}  ({suggestion.id})")
            lines.append("Rewrite the text, or use --want (scoring only, no filter).")
            raise ValueError("\n".join(lines))
        _reject_if_ambiguous_aura(request.text, match)
        base_id, pipe_opt = split_pipe_stat_id(match.id)
        if pipe_opt is not None:
            request.option_id = pipe_opt
            match = StatMatch(
                id=base_id,
                text=match.text,
                score=match.score,
                template=match.template,
                disambiguation_candidates=match.disambiguation_candidates,
                disambiguation_aura=match.disambiguation_aura,
                is_ambiguous_bare_aura=match.is_ambiguous_bare_aura,
            )
        request.match = match


def parse_stat_argument(value: str) -> StatRequest:
    """Parse ``"[domain:]<stat>[=<min>|<min>..<max>|..<max>|<option>]"`` into a ``StatRequest``."""
    text = (value or "").strip()
    domain: str | None = None
    m_dom = _STAT_DOMAIN_PREFIX_RE.match(text)
    if m_dom:
        domain = m_dom.group(1).lower()
        text = m_dom.group(2).strip()

    minimum: float | None = 1.0
    maximum: float | None = None
    option_value: str | None = None
    if "=" in text:
        candidate_text, candidate_rhs = text.rsplit("=", 1)
        rhs = candidate_rhs.strip()
        m_range = _RANGE_BOUNDS_RE.match(rhs)
        if m_range and (m_range.group(1) is not None or m_range.group(2) is not None):
            minimum = float(m_range.group(1)) if m_range.group(1) is not None else None
            maximum = float(m_range.group(2)) if m_range.group(2) is not None else None
            text = candidate_text.rstrip(">").strip()
        else:
            try:
                minimum = float(rhs)
                text = candidate_text.rstrip(">").strip()
            except ValueError:
                if rhs:
                    option_value = rhs
                    minimum = None
                    text = candidate_text.rstrip(">").strip()
    if not text:
        raise ValueError("--stat cannot be empty")
    return StatRequest(
        text=text,
        min_value=minimum,
        max_value=maximum,
        domain=domain,
        option_value=option_value,
    )


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
        max_value=request.max_value,
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
            _reject_if_ambiguous_aura(item.text, match)
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
    name: str | None = None,
    item_type: str | None = None,
    corrupted: str | None = None,
    links: int | None = None,
    foulborn: bool = False,
    gem_level: int | None = None,
    gem_quality: int | None = None,
    infer_gem_category: bool = True,
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
                    _plain_stat_filter(request)
                    for request in plan.plain
                    if request.match is not None
                ],
            }
        )
    if pseudo_filters:
        stat_groups.append({"type": "and", "filters": list(pseudo_filters)})
    for conversion in plan.conversions:
        # PoE1 trade API rejects "type": "or"; a count group with min=1 is the supported OR form.
        conv_val: dict[str, Any] = {}
        if conversion.min_value is not None:
            conv_val["min"] = conversion.min_value
        if conversion.max_value is not None:
            conv_val["max"] = conversion.max_value
        stat_groups.append(
            {
                "type": "count",
                "value": {"min": 1},
                "filters": [
                    {
                        "id": stat_id,
                        "value": dict(conv_val),
                        "disabled": False,
                    }
                    for stat_id in conversion.stat_ids.values()
                ],
            }
        )

    query: dict[str, Any] = {"status": {"option": status_norm}}
    if name and name.strip():
        query["name"] = name.strip()
    if item_type and item_type.strip():
        query["type"] = item_type.strip()
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

    cor_norm = (corrupted or "").strip().lower()
    has_explicit_cor = cor_norm in ("yes", "true", "1", "no", "false", "0", "any")
    should_exclude_corrupted = (
        (bool(plan.conversions) and not allow_corrupted and not has_explicit_cor)
        if exclude_corrupted_mirrored is None
        else (bool(exclude_corrupted_mirrored) and not has_explicit_cor)
    )
    should_exclude_mirrored = (
        ((bool(plan.conversions) or has_explicit_cor) and not allow_corrupted)
        if exclude_corrupted_mirrored is None
        else bool(exclude_corrupted_mirrored)
    )
    misc_filters: dict[str, Any] = {}
    if should_exclude_corrupted:
        misc_filters["corrupted"] = {"option": "false"}
    if should_exclude_mirrored:
        misc_filters["mirrored"] = {"option": "false"}
    if cor_norm in ("yes", "true", "1"):
        misc_filters["corrupted"] = {"option": "true"}
    elif cor_norm in ("no", "false", "0"):
        misc_filters["corrupted"] = {"option": "false"}
    if misc_filters:
        filter_block["misc_filters"] = {"filters": misc_filters}

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

    body = {"query": query, "sort": sort_block}
    if links is not None or foulborn or gem_level is not None or gem_quality is not None:
        apply_exact_item_filters(
            body,
            links=links,
            foulborn=foulborn,
            gem_level=gem_level,
            gem_quality=gem_quality,
            infer_gem_category=infer_gem_category,
        )
    return body
