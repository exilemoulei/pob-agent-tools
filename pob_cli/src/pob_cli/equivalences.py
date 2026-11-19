"""PoE1 Harvest same-tier conversion rules used by trade candidate evaluation.

Harvest offers ``Change a modifier that grants X Resistance into a similar-tier
modifier that grants Y Resistance`` (and the damage equivalent).  The craft
keeps the modifier **tier** but re-rolls the value inside that tier's range, so
a listed ``+42% Fire Resistance`` is only *tier-equivalent* to a Cold
Resistance modifier - it is never guaranteed to keep the 42.

This module only answers "may this modifier be converted, at what cost, and to
which elements".  It never claims the numeric value survives; callers must show
the re-roll caveat (``conversion_note``) and let PoB re-evaluate the item.

Sources
-------
- https://www.poewiki.net/wiki/Harvest_crafting
- https://www.poewiki.net/wiki/List_of_harvest_crafting_options (3.30 costs)
- https://www.pathofexile.com/forum/view-thread/3025078
  ("It means the same tier. The exact roll within the tier will be randomized
  however.")
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ELEMENTS: tuple[str, ...] = ("fire", "cold", "lightning")

ELEMENT_LABELS: dict[str, str] = {
    "fire": "Fire",
    "cold": "Cold",
    "lightning": "Lightning",
}


@dataclass(frozen=True)
class Group:
    """One convertible modifier family (resistance or elemental damage)."""

    key: str
    kind: str
    elements: tuple[str, ...]
    craft: str
    cost: str
    patch: str
    note: str

    def craft_text(self, source: str, target: str) -> str:
        return self.craft.format(
            src=ELEMENT_LABELS.get(source, source.title()),
            dst=ELEMENT_LABELS.get(target, target.title()),
        )

    def as_dict(self, source: str, target: str) -> dict[str, str]:
        return {
            "group": self.key,
            "kind": self.kind,
            "from": source,
            "to": target,
            "craft": self.craft_text(source, target),
            "cost": self.cost,
            "patch": self.patch,
            "rerolled": "true",
            "note": self.note,
        }


RESISTANCE = Group(
    key="resistance",
    kind="resistance",
    elements=ELEMENTS,
    craft=(
        "Change a modifier that grants {src} Resistance into a similar-tier "
        "modifier that grants {dst} Resistance"
    ),
    cost="300 Lifeforce of two different types",
    patch="3.30",
    note="Same tier is kept, the value is re-rolled inside that tier's range.",
)

ELEMENTAL_DAMAGE = Group(
    key="elemental_damage",
    kind="damage",
    elements=ELEMENTS,
    craft=(
        "Change a modifier that grants {src} Damage into a similar-tier "
        "modifier that grants {dst} Damage"
    ),
    cost="300 Lifeforce of two different types + 1 Crystallised Rancour",
    patch="3.30",
    note="Same tier is kept, the value is re-rolled inside that tier's range.",
)

GROUPS: tuple[Group, ...] = (RESISTANCE, ELEMENTAL_DAMAGE)


# Templates are matched after normalisation (lowercase, numbers -> "#", no "+").
_RESISTANCE_RE = re.compile(r"^#% to (fire|cold|lightning) resistance$")
_ADDED_DAMAGE_RE = re.compile(
    r"^adds # to # (fire|cold|lightning) damage(?: to (?:attacks|spells))?$"
)
_INCREASED_DAMAGE_RE = re.compile(r"^#% increased (fire|cold|lightning) damage$")


@dataclass(frozen=True)
class Match:
    """A modifier template that belong to a convertible group."""

    group: Group
    element: str

    @property
    def element_label(self) -> str:
        return ELEMENT_LABELS.get(self.element, self.element.title())


def match(template: str) -> Match | None:
    """Return the conversion group/element for a normalised modifier template."""
    value = (template or "").strip().lower()
    if not value:
        return None
    found = _RESISTANCE_RE.match(value)
    if found:
        return Match(RESISTANCE, found.group(1))
    found = _ADDED_DAMAGE_RE.match(value)
    if found:
        return Match(ELEMENTAL_DAMAGE, found.group(1))
    found = _INCREASED_DAMAGE_RE.match(value)
    if found:
        return Match(ELEMENTAL_DAMAGE, found.group(1))
    return None


def is_convertible_template(template: str) -> bool:
    return match(template) is not None


def convert_template(template: str, target: str) -> str | None:
    """Rewrite a convertible template to ``target`` keeping its exact shape.

    ``#% to cold resistance`` -> ``#% to fire resistance`` keeps "to ... resistance";
    ``adds # to # fire damage to attacks`` keeps the "to attacks" suffix.
    """
    found = match(template)
    if found is None:
        return None
    if target not in found.group.elements:
        raise ValueError(f"Unsupported conversion target: {target!r}")
    return re.sub(
        r"\b(fire|cold|lightning)\b",
        target,
        template,
        count=1,
    )


def group_for(template: str) -> Group | None:
    found = match(template)
    return found.group if found else None


def conversion_note(template: str, target: str) -> str:
    """Human/agent-facing warning for one converted modifier."""
    found = match(template)
    if found is None:
        return ""
    src = found.element_label
    dst = ELEMENT_LABELS.get(target, target.title())
    group = found.group
    return (
        f"{src} -> {dst}: {group.craft_text(found.element, target)} "
        f"({group.patch}); cost: {group.cost}. "
        "Tier is kept but the roll is randomized inside the tier - the listed "
        "value is a best case, re-check the item in PoB before buying."
    )
