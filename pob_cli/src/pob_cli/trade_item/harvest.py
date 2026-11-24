"""PoE1 3.30 single-element Harvest conversion rules and template matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

ELEMENTS: tuple[str, ...] = ("fire", "cold", "lightning")

ELEMENT_LABELS: dict[str, str] = {
    "fire": "Fire",
    "cold": "Cold",
    "lightning": "Lightning",
}


@dataclass(frozen=True)
class HarvestGroup:
    """One convertible Harvest modifier family (resistance or elemental damage)."""

    key: str
    kind: str
    elements: tuple[str, ...]
    craft: str
    cost: str
    patch: str
    note: str

    def craft_text(self, src: str, dst: str) -> str:
        return self.craft.format(
            src=ELEMENT_LABELS.get(src, src.title()),
            dst=ELEMENT_LABELS.get(dst, dst.title()),
        )

    def as_dict(self, src: str, dst: str) -> dict[str, Any]:
        return {
            "group": self.key,
            "kind": self.kind,
            "from": src,
            "to": dst,
            "craft": self.craft_text(src, dst),
            "cost": self.cost,
            "patch": self.patch,
            "rerolled": True,
            "note": self.note,
        }


RESISTANCE = HarvestGroup(
    key="resistance",
    kind="resistance",
    elements=ELEMENTS,
    craft=(
        "Change a modifier that grants {src} Resistance into a similar-tier "
        "modifier that grants {dst} Resistance"
    ),
    cost="300 Lifeforce (Primal/Vivid/Wild matching target)",
    patch="3.30",
    note="Same tier is kept, the value is re-rolled inside that tier's range.",
)

ELEMENTAL_DAMAGE = HarvestGroup(
    key="elemental_damage",
    kind="damage",
    elements=ELEMENTS,
    craft=(
        "Change a modifier that grants {src} Damage into a similar-tier "
        "modifier that grants {dst} Damage"
    ),
    cost="300 Lifeforce + 1 Crystallised Rancour",
    patch="3.30",
    note="Same tier is kept, the value is re-rolled inside that tier's range.",
)

HARVEST_GROUPS: tuple[HarvestGroup, ...] = (RESISTANCE, ELEMENTAL_DAMAGE)

_HARVEST_PATTERNS: tuple[tuple[re.Pattern[str], HarvestGroup, str], ...] = (
    (re.compile(r"^#% to (fire|cold|lightning) resistance$"), RESISTANCE, "resistance"),
    (re.compile(r"^adds # to # (fire|cold|lightning) damage$"), ELEMENTAL_DAMAGE, "added_damage_local"),
    (
        re.compile(r"^adds # to # (fire|cold|lightning) damage to attacks$"),
        ELEMENTAL_DAMAGE,
        "added_damage_attacks",
    ),
    (
        re.compile(r"^adds # to # (fire|cold|lightning) damage to spells$"),
        ELEMENTAL_DAMAGE,
        "added_damage_spells",
    ),
    (
        re.compile(r"^#% increased (fire|cold|lightning) damage$"),
        ELEMENTAL_DAMAGE,
        "increased_damage",
    ),
)


@dataclass(frozen=True)
class HarvestMatch:
    """A normalized modifier template that belongs to a convertible Harvest group."""

    group: HarvestGroup
    element: str
    subkind: str

    @property
    def element_label(self) -> str:
        return ELEMENT_LABELS.get(self.element, self.element.title())


def match_harvest(template: str) -> HarvestMatch | None:
    """Return the Harvest conversion group, element, and subkind for a template."""
    value = (template or "").strip().lower()
    if not value:
        return None
    for pattern, group, subkind in _HARVEST_PATTERNS:
        found = pattern.match(value)
        if found:
            return HarvestMatch(group=group, element=found.group(1), subkind=subkind)
    return None


def is_convertible_template(template: str) -> bool:
    """Return True if ``template`` is eligible for PoE1 3.30 single-element Harvest swap."""
    return match_harvest(template) is not None


def convert_template(template: str, target: str) -> str | None:
    """Rewrite a convertible template to ``target`` keeping its exact shape."""
    found = match_harvest(template)
    if found is None:
        return None
    if target not in found.group.elements:
        raise ValueError(f"Unsupported conversion target: {target!r}")
    normalized = (template or "").strip().lower()
    return re.sub(r"\b(fire|cold|lightning)\b", target, normalized, count=1)


def conversion_note(template: str, target: str) -> str:
    """Human/agent-facing warning for one converted modifier."""
    found = match_harvest(template)
    if found is None:
        return ""
    if target not in found.group.elements:
        raise ValueError(f"Unsupported conversion target: {target!r}")
    src = found.element_label
    dst = ELEMENT_LABELS.get(target, target.title())
    group = found.group
    return (
        f"{src} -> {dst}: {group.craft_text(found.element, target)} "
        f"({group.patch}); cost: {group.cost}. "
        "Tier is kept but the roll is randomized inside the tier - the listed "
        "value is a best case, re-check the item in PoB before buying."
    )
