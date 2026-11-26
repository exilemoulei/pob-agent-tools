"""Constants, text normalization, classification, and data models for PoE1 trade items."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
RANGE_RE = re.compile(r"\(\s*-?\d+(?:\.\d+)?\s*-\s*-?\d+(?:\.\d+)?\s*\)")
POB_TAG_RE = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_:.,-]*)\}")
SIMPLE_TAG_RE = re.compile(r"\{([a-zA-Z]+)\}")
TRAILING_KIND_RE = re.compile(
    r"\s*\((crafted|fractured|implicit|enchant|scourge|mutated|vestigial|crucible|veiled|rune)\)\s*$",
    re.IGNORECASE,
)
SPACE_RE = re.compile(r"\s+")
SEPARATOR_RE = re.compile(r"^(?:-{3,}|={3,})$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PROPERTY_RE = re.compile(r"^([A-Za-z][A-Za-z '/()-]+):")

_GGG_TAG_RE = re.compile(r"<[^>]+>\{([^}]*)\}")
_GGG_LINK_PIPE_RE = re.compile(r"\[[^|\]]+\|([^|\]]+)\]")
_GGG_LINK_PLAIN_RE = re.compile(r"\[([^|\]]+)\]")

NEGATIVE_KEYWORDS: tuple[str, ...] = ("reduced", "less", "slower", "decreased", "lower")
NEGATIVE_KEYWORD_RE = re.compile(r"\b(?:reduced|less|slower|decreased|lower)\b")

POSITIVE_REDUCED_PATTERNS: tuple[str, ...] = (
    "reduced mana cost",
    "reduced cost of",
    "reduced attribute requirements",
    "reduced enemy stun threshold",
    "reduced extra damage from critical",
    "reduced reflected",
    "reduced damage taken",
    "reduced effect of curses on you",
    "reduced effect of chill",
    "reduced effect of shock",
    "reduced duration of ailments",
    "reduced soul gain prevention",
    "less damage taken",
    "less mana cost",
)

PROPERTY_KEYS: frozenset[str] = frozenset(
    {
        "item class",
        "rarity",
        "quality",
        "item level",
        "unique id",
        "levelreq",
        "level req",
        "level",
        "sockets",
        "implicits",
        "requirements",
        "requires",
        "requires level",
        "requires class",
        "str",
        "strength",
        "dex",
        "dexterity",
        "int",
        "intelligence",
        "armour",
        "evasion",
        "evasion rating",
        "energy shield",
        "ward",
        "physical damage",
        "elemental damage",
        "chaos damage",
        "attack speed",
        "attacks per second",
        "critical strike chance",
        "critical strike range",
        "chance to block",
        "block chance",
        "weapon range",
        "radius",
        "limited to",
        "memory strands",
        "talisman tier",
        "catalyst",
        "catalystquality",
        "intangibility",
        "cluster jewel skill",
        "cluster jewel node count",
        "prefix",
        "suffix",
        "crafted",
        "variant",
        "selected variant",
        "version",
        "selected version",
        "allow duplicate variants",
        "league",
        "unreleased",
        "upgrade",
        "source",
        "stack size",
        "map tier",
        "item quantity",
        "item rarity",
        "monster pack size",
        "note",
    }
)

# Category order matters: check the most specific phrases first.
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("penalty", ("requirement", "mana cost", "reservation", "cost of")),
    (
        "defense",
        (
            "damage taken",
            "block chance",
            "chance to block",
            "stun threshold",
            "extra damage from critical",
            "reduced reflected",
            "effect of curses on you",
            "effect of chill",
            "effect of shock",
            "duration of ailments",
            "suppress spell damage",
            "spell suppression",
        ),
    ),
    ("resist", ("resistance",)),
    ("life", ("maximum life", "life regeneration", "life leech", "life recoup", "life gained")),
    (
        "mana",
        (
            "maximum mana",
            "mana regeneration",
            "mana leech",
            "mana cost",
            "cost of",
            "reservation efficiency",
        ),
    ),
    ("energy_shield", ("energy shield",)),
    ("armour", ("armour",)),
    ("evasion", ("evasion",)),
    (
        "attributes",
        ("strength", "dexterity", "intelligence", "all attributes", "attributes", "attribute requirements"),
    ),
    ("skill_level", ("level of all", "level of")),
    ("speed", ("attack speed", "cast speed", "movement speed", "skill speed", "action speed")),
    ("crit", ("critical",)),
    ("accuracy", ("accuracy",)),
    ("damage", ("damage", "penetration", "ailment", "ignite", "bleed", "poison")),
    ("rarity", ("rarity",)),
)

FRAME_RARITY: dict[int, str] = {
    0: "NORMAL",
    1: "MAGIC",
    2: "RARE",
    3: "UNIQUE",
    4: "GEM",
    5: "CURRENCY",
    9: "RELIC",
}

INFLUENCE_API_MAP: tuple[tuple[str, str], ...] = (
    ("shaper", "Shaper"),
    ("elder", "Elder"),
    ("warlord", "Warlord"),
    ("hunter", "Hunter"),
    ("crusader", "Crusader"),
    ("redeemer", "Redeemer"),
    ("searing", "Searing Exarch"),
    ("tangled", "Eater of Worlds"),
)

INFLUENCE_LINE_MAP: dict[str, str] = {
    **{f"{display.lower()} item": display for _, display in INFLUENCE_API_MAP},
    "vestigial item": "Vestigial",
    "vestigial": "Vestigial",
}

STATE_LINES: frozenset[str] = frozenset(
    {
        "corrupted",
        "mirrored",
        "unidentified",
        "synthesised item",
        "fractured item",
        "split",
    }
    | set(INFLUENCE_LINE_MAP)
)

MOD_KIND_TAGS: frozenset[str] = frozenset(
    {
        "crafted",
        "fractured",
        "enchant",
        "scourge",
        "mutated",
        "vestigial",
        "implicit",
        "crucible",
        "veiled",
        "rune",
    }
)

NON_CONVERTIBLE_KINDS: frozenset[str] = frozenset(
    {
        "implicit",
        "enchant",
        "fractured",
        "crafted",
        "scourge",
        "crucible",
        "mutated",
        "vestigial",
        "veiled",
        "rune",
    }
)


# ---------------------------------------------------------------------------
# Sanitization, GGG string cleaning, template normalization, classification
# ---------------------------------------------------------------------------


def sanitize_text(text: object, limit: int = 300) -> str:
    """Strip control characters and collapse whitespace from untrusted trade text."""
    raw = "" if text is None else str(text)
    value = CONTROL_RE.sub(" ", raw)
    value = SPACE_RE.sub(" ", value).strip()
    return value[:limit]


def escape_ggg_string(text: str) -> str:
    """Mirror PoB ``Common.lua`` ``escapeGGGString`` for keyword/tag markup.

    * ``<tag>{content}`` -> ``content`` (e.g. ``<augmented>{+42}`` -> ``+42``)
    * ``[Key|Display Text]`` -> ``Display Text``
    * ``[Key]`` -> ``Key``
    """
    value = "" if text is None else str(text)
    value = _GGG_TAG_RE.sub(r"\1", value)
    value = _GGG_LINK_PIPE_RE.sub(r"\1", value)
    value = _GGG_LINK_PLAIN_RE.sub(r"\1", value)
    return value


def normalize_template(text: str) -> str:
    """Normalise a modifier line into a deterministic numeric-free template.

    ``{crafted}+(35–45)% to [Cold|Cold] Resistance`` -> ``#% to cold resistance``
    """
    value = escape_ggg_string("" if text is None else str(text))
    value = POB_TAG_RE.sub("", value)
    value = TRAILING_KIND_RE.sub("", value)
    value = value.strip().lower()
    value = value.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    value = RANGE_RE.sub("#", value)
    value = NUMBER_RE.sub("#", value)
    value = value.replace("+", "")
    value = SPACE_RE.sub(" ", value).strip(" .,")
    return value


def _is_positive_reduced(template: str) -> bool:
    lowered = (template or "").strip().lower()
    return any(pattern in lowered for pattern in POSITIVE_REDUCED_PATTERNS)


def classify_mod(template: str) -> str:
    """Classify a normalised modifier template into a semantic bucket."""
    lowered = (template or "").strip().lower()
    if not lowered:
        return "other"

    positive_reduced = _is_positive_reduced(lowered)
    if positive_reduced:
        if "mana cost" in lowered or "cost of" in lowered:
            return "mana"
        if "attribute requirements" in lowered:
            return "attributes"

    is_reservation_efficiency = "reservation efficiency" in lowered

    for category, keywords in CATEGORY_RULES:
        if category == "penalty" and (positive_reduced or is_reservation_efficiency):
            continue
        if any(keyword in lowered for keyword in keywords):
            return category
    return "other"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mod:
    """One normalised modifier line."""

    raw: str
    template: str
    values: tuple[float, ...]
    kind: str = "explicit"
    tags: tuple[str, ...] = ()
    line_index: int = -1

    @property
    def category(self) -> str:
        return classify_mod(self.template)

    @property
    def signed_value(self) -> float:
        """Numeric magnitude with genuine negative mods flipped so bigger is better."""
        if self.template.startswith("adds ") and len(self.values) == 2:
            total = (self.values[0] + self.values[1]) / 2.0
        else:
            total = float(sum(self.values))
        if NEGATIVE_KEYWORD_RE.search(self.template) and not _is_positive_reduced(self.template):
            total = -total
        return total


@dataclass
class Item:
    """A parsed PoE1 item: header fields, flags, exact lines, and parsed mods."""

    rarity: str = "RARE"
    name: str = ""
    base_type: str = ""
    item_level: int = 0
    corrupted: bool = False
    mirrored: bool = False
    unidentified: bool = False
    synthesised: bool = False
    influences: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    mods: list[Mod] = field(default_factory=list)
    fractured: bool = False
    split: bool = False

    @property
    def display_name(self) -> str:
        if self.name and self.base_type and self.name.lower() != self.base_type.lower():
            return f"{self.name} ({self.base_type})"
        return self.base_type or self.name or "(unknown item)"


@dataclass(frozen=True)
class Conversion:
    """One modifier rewritten to another element via Harvest."""

    template: str
    converted_template: str
    element: str
    target: str
    group: str
    subkind: str
    values: tuple[float, ...]
    cost: str
    patch: str
    line: str
    tier: str = ""
    tier_range: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.template,
            "to": self.converted_template,
            "element": self.element,
            "target": self.target,
            "group": self.group,
            "subkind": self.subkind,
            "values": list(self.values),
            "cost": self.cost,
            "patch": self.patch,
            "tier": self.tier,
            "tier_range": self.tier_range,
            "rerolled": True,
            "line": self.line,
        }


@dataclass
class ConvertedItem:
    text: str
    item: Item
    conversions: list[Conversion] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
