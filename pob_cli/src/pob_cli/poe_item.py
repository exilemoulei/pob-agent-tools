"""Parse, rebuild and Harvest-convert PoB item text for trade evaluation.

Two sources are normalised into the same model:

* PoB clipboard text (``pob items --slot X --raw``), including ``{crafted}`` /
  ``{fractured}`` tags and the ``Implicits: N`` block.
* The official trade API item JSON (``item`` / ``typeLine`` / ``explicitMods``).

Modifiers are reduced to a template (``#% to cold resistance``) plus numeric
values, which is enough for a deterministic before/after comparison and keeps
the tool honest: it compares what the two texts actually say, not what an
optimiser hopes they say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import equivalences

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
TAG_RE = re.compile(r"\{([a-zA-Z]+)\}")
SPACE_RE = re.compile(r"\s+")
SEPARATOR_RE = re.compile(r"^-{3,}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PROPERTY_RE = re.compile(r"^([A-Za-z][A-Za-z '/]+):")

NEGATIVE_KEYWORDS = ("reduced", "less", "slower", "decreased", "lower")

PROPERTY_KEYS = {
    "item class",
    "rarity",
    "quality",
    "item level",
    "unique id",
    "levelreq",
    "level req",
    "sockets",
    "implicits",
    "requirements",
    "requires",
    "armour",
    "evasion",
    "energy shield",
    "ward",
    "physical damage",
    "elemental damage",
    "chaos damage",
    "attack speed",
    "critical strike chance",
    "spirit",
    "stack size",
    "note",
}

# Category order matters: check the most specific phrases first.
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("penalty", ("requirement", "mana cost", "reservation", "cost of")),
    ("defense", ("damage taken", "block chance", "chance to block", "stun threshold")),
    ("resist", ("resistance",)),
    ("life", ("maximum life", "life regeneration", "life leech", "life recoup")),
    ("mana", ("maximum mana", "mana regeneration", "mana leech")),
    ("energy_shield", ("energy shield",)),
    ("armour", ("armour",)),
    ("evasion", ("evasion",)),
    ("attributes", ("strength", "dexterity", "intelligence", "all attributes", "attributes")),
    ("skill_level", ("level of all", "level of")),
    (
        "speed",
        ("attack speed", "cast speed", "movement speed", "skill speed", "reload speed"),
    ),
    ("crit", ("critical",)),
    ("accuracy", ("accuracy",)),
    ("damage", ("damage", "penetration", "ailment", "ignite", "bleed", "poison")),
    ("rarity", ("rarity",)),
)

FRAME_RARITY = {0: "NORMAL", 1: "MAGIC", 2: "RARE", 3: "UNIQUE", 4: "GEM", 5: "CURRENCY"}

TRADE_MOD_KEYS: tuple[tuple[str, str], ...] = (
    ("implicitMods", "implicit"),
    ("explicitMods", "explicit"),
    ("craftedMods", "crafted"),
    ("fracturedMods", "fractured"),
    ("enchantMods", "enchant"),
)

CORRUPTED_TAGS = {"corrupted"}


def sanitize_text(text: object, limit: int = 300) -> str:
    """Strip control characters from untrusted trade text before displaying it."""
    value = CONTROL_RE.sub(" ", str(text or ""))
    value = SPACE_RE.sub(" ", value).strip()
    return value[:limit]


def normalize_template(text: str) -> str:
    """``+42% to Cold Resistance`` -> ``#% to cold resistance``."""
    value = TAG_RE.sub("", str(text or "")).strip().lower()
    value = value.replace("\u2013", "-").replace("\u2014", "-")
    value = NUMBER_RE.sub("#", value)
    value = value.replace("+", "")
    value = SPACE_RE.sub(" ", value).strip(" .,")
    return value


def classify(template: str) -> str:
    for category, keywords in CATEGORY_RULES:
        if any(keyword in template for keyword in keywords):
            return category
    return "other"


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
        return classify(self.template)

    @property
    def signed_value(self) -> float:
        """Value with negative mods flipped so bigger is always better."""
        if self.template.startswith("adds ") and len(self.values) == 2:
            total = sum(self.values) / 2
        else:
            total = sum(self.values)
        if any(keyword in self.template for keyword in NEGATIVE_KEYWORDS):
            total = -total
        return total


@dataclass
class Item:
    """A parsed item: header fields plus the exact lines it was read from."""

    rarity: str = "RARE"
    name: str = ""
    base_type: str = ""
    item_level: int = 0
    corrupted: bool = False
    lines: list[str] = field(default_factory=list)
    mods: list[Mod] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        if self.name and self.base_type and self.name.lower() != self.base_type.lower():
            return f"{self.name} ({self.base_type})"
        return self.base_type or self.name or "(unknown item)"


@dataclass(frozen=True)
class Conversion:
    """One modifier rewritten to another element."""

    template: str
    converted_template: str
    element: str
    target: str
    group: str
    values: tuple[float, ...]
    cost: str
    patch: str
    line: str
    tier: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "from": self.template,
            "to": self.converted_template,
            "element": self.element,
            "target": self.target,
            "group": self.group,
            "values": list(self.values),
            "cost": self.cost,
            "patch": self.patch,
            "tier": self.tier,
            "rerolled": True,
            "line": self.line,
        }


@dataclass
class ConvertedItem:
    text: str
    item: Item
    conversions: list[Conversion] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _first_int(value: str) -> int:
    match = NUMBER_RE.search(str(value).replace("+", ""))
    try:
        return int(float(match.group(0))) if match else 0
    except ValueError:
        return 0


def _kind_from(tags: tuple[str, ...], implicit_remaining: int) -> str:
    for tag in tags:
        if tag in {"crafted", "fractured", "enchant", "implicit", "rune"}:
            return tag
    return "implicit" if implicit_remaining > 0 else "explicit"


def make_mod(
    raw: str,
    *,
    kind: str = "explicit",
    tags: tuple[str, ...] = (),
    line_index: int = -1,
) -> Mod:
    clean = TAG_RE.sub("", raw).strip()
    template = normalize_template(clean)
    values = tuple(float(value) for value in NUMBER_RE.findall(clean))
    return Mod(
        raw=clean,
        template=template,
        values=values,
        kind=kind,
        tags=tags,
        line_index=line_index,
    )


def _is_property_line(line: str) -> bool:
    match = PROPERTY_RE.match(line)
    if not match:
        return False
    return match.group(1).strip().lower() in PROPERTY_KEYS


def parse_item_text(text: str) -> Item:
    """Parse PoB clipboard text (or text produced by :func:`build_item_text`)."""
    raw_lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    lines = [line for line in raw_lines if line and not SEPARATOR_RE.match(line)]
    if not lines:
        raise ValueError("item text is empty")

    item = Item(lines=lines)
    index = 0
    if lines[index].lower().startswith("item class:"):
        index += 1

    if index < len(lines) and lines[index].lower().startswith("rarity:"):
        item.rarity = lines[index].split(":", 1)[1].strip().upper()
        index += 1

    if item.rarity in {"RARE", "UNIQUE", "MAGIC"}:
        if index < len(lines):
            item.name = sanitize_text(lines[index], 120)
            index += 1
        if index < len(lines):
            item.base_type = sanitize_text(lines[index], 120)
            index += 1
    else:
        if index < len(lines):
            item.base_type = sanitize_text(lines[index], 120)
            index += 1

    implicit_remaining = 0
    for line_index in range(index, len(lines)):
        line = lines[line_index]
        lowered = line.lower()
        if lowered.startswith("implicits:"):
            implicit_remaining = _first_int(line.split(":", 1)[1])
            continue
        if lowered.startswith("item level:"):
            item.item_level = _first_int(line.split(":", 1)[1])
            continue
        if lowered in CORRUPTED_TAGS or lowered.startswith("corrupted"):
            item.corrupted = True
            continue
        if _is_property_line(line):
            continue

        tags = tuple(tag.lower() for tag in TAG_RE.findall(line))
        clean = TAG_RE.sub("", line).strip()
        if not clean:
            continue
        kind = _kind_from(tags, implicit_remaining)
        if implicit_remaining > 0:
            implicit_remaining -= 1
        item.mods.append(make_mod(clean, kind=kind, tags=tags, line_index=line_index))
    return item


def _mod_line_text(entry: object) -> str:
    if isinstance(entry, str):
        return sanitize_text(entry)
    if isinstance(entry, dict):
        return sanitize_text(entry.get("description") or entry.get("text") or "")
    return ""


def build_item_text(raw_item: dict) -> str:
    """Build PoB clipboard text from a trade API item (or a fetch row)."""
    item = raw_item.get("item") if isinstance(raw_item.get("item"), dict) else raw_item
    if not isinstance(item, dict):
        raise ValueError("trade item payload is not an object")

    rarity = FRAME_RARITY.get(item.get("frameType"), "RARE")
    name = sanitize_text(item.get("name"), 120)
    base = sanitize_text(item.get("typeLine") or item.get("baseType"), 120)
    if not base:
        raise ValueError("trade item has no base type")

    implicit = [_mod_line_text(entry) for entry in item.get("implicitMods") or []]
    explicit = [_mod_line_text(entry) for entry in item.get("explicitMods") or []]
    crafted = [_mod_line_text(entry) for entry in item.get("craftedMods") or []]
    fractured = [_mod_line_text(entry) for entry in item.get("fracturedMods") or []]
    enchants = [_mod_line_text(entry) for entry in item.get("enchantMods") or []]

    implicit_block = [line for line in implicit + enchants if line]
    lines = [f"Rarity: {rarity}"]
    if rarity in {"RARE", "UNIQUE", "MAGIC"} and name:
        lines.append(name)
    lines.append(base)
    item_level = item.get("ilvl") or item.get("itemLevel")
    if item_level:
        lines.append(f"Item Level: {int(item_level)}")
    lines.append(f"Implicits: {len(implicit_block)}")
    lines.extend(implicit_block)
    lines.extend(line for line in explicit if line)
    lines.extend(f"{{crafted}}{line}" if line else "" for line in crafted)
    lines.extend(f"{{fractured}}{line}" if line else "" for line in fractured)
    if item.get("corrupted"):
        lines.append("Corrupted")
    return "\n".join(line for line in lines if line)


def trade_mod_tiers(raw_item: dict) -> dict[str, str]:
    """Map explicit modifier templates to the trade API tier label (``S3``, ``P2``).

    The PoE1 trade API returns ``explicitMods`` as objects with ``mods[].tier``;
    the PoE2-style plain strings simply have no tier.  Harvest preserves the
    tier, so showing it is the honest way to talk about "same tier".
    """
    item = raw_item.get("item") if isinstance(raw_item.get("item"), dict) else raw_item
    if not isinstance(item, dict):
        return {}
    tiers: dict[str, str] = {}
    for entry in item.get("explicitMods") or []:
        if not isinstance(entry, dict):
            continue
        template = normalize_template(_mod_line_text(entry))
        if not template:
            continue
        for mod in entry.get("mods") or []:
            if isinstance(mod, dict) and mod.get("tier"):
                tiers.setdefault(template, str(mod["tier"]))
                break
    return tiers


def mods_by_template(item: Item | None) -> dict[str, float]:
    """Sum signed values per template so duplicate mods stack correctly."""
    result: dict[str, float] = {}
    if item is None:
        return result
    for mod in item.mods:
        result[mod.template] = result.get(mod.template, 0.0) + mod.signed_value
    return result


def _rewrite_element(line: str, target: str) -> str:
    return re.sub(
        r"\b(fire|cold|lightning)\b",
        target.capitalize(),
        line,
        count=1,
        flags=re.IGNORECASE,
    )


def convert_item_text(
    text: str,
    target: str,
    *,
    force: bool = False,
    group: str | None = None,
    tiers: dict[str, str] | None = None,
) -> ConvertedItem:
    """Rewrite convertible modifiers to ``target`` (``fire`` / ``cold`` / ``lightning``).

    Refuses unique/corrupted items unless ``force`` is set, and skips implicit,
    enchant and fractured lines (Harvest cannot change those tags).  ``group``
    limits the rewrite to one family (``resistance`` / ``elemental_damage``) so
    a resistance search never silently rewrites damage mods.  ``tiers`` maps a
    template to the trade API's tier label (``S3``) so the report can show the
    tier that Harvest preserves.  The value is kept verbatim in the produced
    text; the caller must still report that the real craft re-rolls inside the
    tier.
    """
    if target not in equivalences.ELEMENTS:
        raise ValueError(f"Unsupported conversion target: {target!r}")

    tiers = tiers or {}
    item = parse_item_text(text)
    skipped: list[str] = []
    if item.corrupted and not force:
        return ConvertedItem(
            text=text,
            item=item,
            skipped=["Corrupted items cannot be modified (remove the corruption first)."],
        )
    if item.rarity not in {"MAGIC", "RARE"} and not force:
        return ConvertedItem(
            text=text,
            item=item,
            skipped=[f"{item.rarity.title()} items cannot use Harvest conversion (rare/magic only)."],
        )

    lines = list(item.lines)
    conversions: list[Conversion] = []
    for mod in item.mods:
        found = equivalences.match(mod.template)
        if found is None:
            continue
        if group is not None and found.group.key != group:
            continue
        if mod.kind in {"fractured", "implicit", "enchant", "rune"}:
            skipped.append(f"{mod.raw}: {mod.kind} modifiers cannot be converted")
            continue
        if found.element == target:
            continue
        new_line = _rewrite_element(mod.raw, target)
        if new_line == mod.raw or mod.line_index < 0:
            skipped.append(f"{mod.raw}: could not rewrite the element in place")
            continue
        lines[mod.line_index] = new_line
        conversions.append(
            Conversion(
                template=mod.template,
                converted_template=equivalences.convert_template(mod.template, target) or "",
                element=found.element,
                target=target,
                group=found.group.key,
                values=mod.values,
                cost=found.group.cost,
                patch=found.group.patch,
                line=new_line,
                tier=tiers.get(mod.template, ""),
            )
        )

    converted_text = "\n".join(lines)
    return ConvertedItem(
        text=converted_text,
        item=parse_item_text(converted_text),
        conversions=conversions,
        skipped=skipped,
    )


def conversion_summary(converted: ConvertedItem) -> str:
    """Short human/agent-facing summary of a conversion run."""
    if not converted.conversions:
        return "no convertible modifiers found"
    parts = []
    for conversion in converted.conversions:
        parts.append(
            f"{conversion.template} -> {conversion.converted_template} "
            f"(cost: {conversion.cost}; tier kept, roll re-rolled)"
        )
    return "; ".join(parts)
