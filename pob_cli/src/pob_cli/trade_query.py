"""Turn a slot + desired modifiers into an official PoE1 trade query."""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

from . import equivalences
from .poe_item import normalize_template

CATEGORY_BY_SLOT = {
    "helmet": "armour.helmet",
    "body_armour": "armour.chest",
    "chest": "armour.chest",
    "gloves": "armour.gloves",
    "boots": "armour.boots",
    "shield": "armour.shield",
    "quiver": "armour.quiver",
    "amulet": "accessory.amulet",
    "belt": "accessory.belt",
    "ring": "accessory.ring",
    "ring1": "accessory.ring",
    "ring2": "accessory.ring",
    "weapon1": None,
    "weapon2": None,
    "flask": "flask",
    "jewel": "jewel",
}

# PoB slot names used by `pob items` / `pob item try`.
POB_SLOT_BY_SLOT = {
    "helmet": "Helmet",
    "body_armour": "Body Armour",
    "chest": "Body Armour",
    "gloves": "Gloves",
    "boots": "Boots",
    "shield": "Weapon 2",
    "amulet": "Amulet",
    "belt": "Belt",
    "ring": "Ring 1",
    "ring1": "Ring 1",
    "ring2": "Ring 2",
    "weapon1": "Weapon 1",
    "weapon2": "Weapon 2",
    "flask": "Flask 1",
}

STAT_TYPE_ORDER = {
    "explicit": 0,
    "crafted": 1,
    "fractured": 1,
    "enchant": 1,
    "implicit": 2,
    "pseudo": 3,
}


@dataclass
class StatMatch:
    id: str
    text: str
    score: float
    template: str


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
class StatPlan:
    plain: list[StatRequest] = field(default_factory=list)
    conversions: list[ConversionPlan] = field(default_factory=list)


def flatten_stats(groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for group in groups:
        for entry in group.get("entries") or []:
            if entry.get("id") and entry.get("text"):
                entries.append(entry)
    return entries


def _stat_priority(stat_id: str) -> int:
    prefix = stat_id.split(".", 1)[0]
    return STAT_TYPE_ORDER.get(prefix, 5)


def _similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def resolve_stat(
    text: str,
    entries: list[dict[str, Any]],
    threshold: float = 0.82,
) -> StatMatch | None:
    """Resolve ``to Cold Resistance`` to the trade stat id for that modifier."""
    target = normalize_template(text)
    if not target:
        return None

    best: tuple[float, int, int, dict[str, Any]] | None = None
    for entry in entries:
        entry_template = normalize_template(entry["text"])
        score = _similarity(target, entry_template)
        # A short request such as "to cold resistance" should still hit
        # "#% to Cold Resistance"; ratio alone is too strict for that.
        if target and (target in entry_template or entry_template in target):
            score = max(score, 0.9)
        if score < threshold:
            continue
        key = (score, -_stat_priority(entry["id"]), -len(entry_template), entry)
        if best is None or key[:3] > best[:3]:
            best = key

    if best is None:
        return None
    score, _, _, entry = best
    return StatMatch(
        id=entry["id"],
        text=entry["text"],
        score=round(score, 3),
        template=normalize_template(entry["text"]),
    )


def suggest_stats(text: str, entries: list[dict[str, Any]], limit: int = 5) -> list[StatMatch]:
    target = normalize_template(text)
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in entries:
        scored.append((_similarity(target, normalize_template(entry["text"])), entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        StatMatch(
            id=entry["id"],
            text=entry["text"],
            score=round(score, 3),
            template=normalize_template(entry["text"]),
        )
        for score, entry in scored[:limit]
    ]


def resolve_requests(requests: Iterable[StatRequest], entries: list[dict[str, Any]]) -> None:
    for request in requests:
        match = resolve_stat(request.text, entries)
        if match is None:
            lines = [f"Could not map this modifier to a trade filter: {request.text}"]
            for suggestion in suggest_stats(request.text, entries):
                lines.append(f"  - {suggestion.text}  ({suggestion.id})")
            lines.append("Rewrite the text, or use --want (scoring only, no filter).")
            raise ValueError("\n".join(lines))
        request.match = match


def _conversion_plan(request: StatRequest, entries: list[dict[str, Any]]) -> ConversionPlan | None:
    """Expand one convertible request into the sibling-element stat ids."""
    if request.match is None:
        return None
    found = equivalences.match(request.match.template)
    if found is None:
        return None

    stat_ids: dict[str, str] = {}
    templates: dict[str, str] = {}
    for element in found.group.elements:
        template = request.match.template
        if element != found.element:
            template = equivalences.convert_template(template, element) or ""
        if not template:
            continue
        resolved = resolve_stat(template, entries)
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
    allow_conversion: bool = False,
    convert_to: str | None = None,
) -> StatPlan:
    """Resolve requests and (optionally) expand convertible ones into count groups."""
    resolve_requests(requests, entries)
    plan = StatPlan()
    for request in requests:
        conversion = _conversion_plan(request, entries) if allow_conversion else None
        if conversion is None:
            plan.plain.append(request)
            continue
        if convert_to:
            if convert_to not in conversion.stat_ids:
                raise ValueError(
                    f"Cannot convert {request.match.text} to {convert_to!r}; "
                    f"available: {', '.join(conversion.elements)}"
                )
            conversion.target = convert_to
        plan.conversions.append(conversion)
    return plan


def build_query(
    *,
    plan: StatPlan,
    category: str | None,
    rarity: str | None,
    item_level_min: int = 0,
    status: str = "online",
    currency: str | None = None,
    budget: float | None = None,
    search_sort: str = "asc",
    pseudo_filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stat_groups: list[dict[str, Any]] = []

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
        # The PoE1 trade API rejects ``"type": "or"`` stat groups; a count group
        # with ``min: 1`` is the supported "any one of these elements" form.
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

    query: dict[str, Any] = {"status": {"option": status}}
    if stat_groups:
        query["stats"] = stat_groups

    filter_block: dict[str, Any] = {}
    type_filters: dict[str, Any] = {}
    if category:
        type_filters["category"] = {"option": category}
    if rarity and rarity != "any":
        type_filters["rarity"] = {"option": rarity}
    if item_level_min > 0:
        type_filters["ilvl"] = {"min": item_level_min}
    if type_filters:
        filter_block["type_filters"] = {"filters": type_filters}

    if budget is not None and budget > 0:
        filter_block["trade_filters"] = {
            "filters": {
                "price": {
                    "option": currency or "chaos",
                    "max": budget,
                }
            }
        }
    if filter_block:
        query["filters"] = filter_block

    return {"query": query, "sort": {"price": search_sort}}


def parse_stat_argument(value: str) -> StatRequest:
    """``"to Cold Resistance=40"`` -> text + minimum 40."""
    text = value.strip()
    minimum = 1.0
    if "=" in text:
        candidate_text, candidate_min = text.rsplit("=", 1)
        try:
            minimum = float(candidate_min)
            text = candidate_text.strip()
        except ValueError:
            pass
    if not text:
        raise ValueError("--stat cannot be empty")
    return StatRequest(text=text, min_value=minimum)


def category_for_slot(slot: str) -> str | None:
    return CATEGORY_BY_SLOT.get(slot.lower())


def pob_slot_for_slot(slot: str) -> str | None:
    return POB_SLOT_BY_SLOT.get(slot.lower())
