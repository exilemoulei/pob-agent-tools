"""Refresh the offline crafting-bench dataset from poedb + the PoE wiki.

Usage (from the repository root):

    python pob_cli/scripts/update_crafting_bench.py

Sources
-------
* https://poedb.tw/cn/Crafting_Bench  (Chinese labels, item classes, cost, unlock)
* https://poedb.tw/us/Crafting_Bench  (English labels; rows align 1:1 with /cn/)
* https://www.poewiki.net/wiki/Crafting_bench  (prefix/suffix section headers)

The generated file is ``pob_cli/src/pob_cli/data/crafting_bench.json``.  It is
committed so the CLI never scrapes at runtime; re-run this script when a patch
changes the bench.

The data is informational: values are the bench roll ranges, which are lower
than high-tier natural modifiers.  The tool reports the *minimum* as the safe
floor and never pretends a bench craft equals a natural T2 roll.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "pob_cli" / "data" / "crafting_bench.json"
USER_AGENT = "pob-cli/0.1.0 (+https://github.com/exilemoulei/pob-agent-tools)"

POEDB_CN = "https://poedb.tw/cn/Crafting_Bench"
POEDB_US = "https://poedb.tw/us/Crafting_Bench"
WIKI = "https://www.poewiki.net/wiki/Crafting_bench"

# poedb item-class labels -> slot keys used by `pob trade --slot`.
CLASS_TO_SLOTS: dict[str, tuple[str, ...]] = {
    "Body Armour": ("body_armour",),
    "Gloves": ("gloves",),
    "Boots": ("boots",),
    "Helmet": ("helmet",),
    "Shield": ("shield",),
    "Quiver": ("quiver",),
    "Ring": ("ring1", "ring2"),
    "Amulet": ("amulet",),
    "Belt": ("belt",),
    "One Hand Melee": ("weapon1", "weapon2"),
    "Two Hand Melee": ("weapon1",),
    "One Hand Ranged": ("weapon1", "weapon2"),
    "Two Hand Ranged": ("weapon1",),
}

# Rows that are socket/link/map operations, not stat modifiers.
NON_MOD_HINTS = (
    "socket",
    "sockets",
    "link",
    "links",
    "map ",
    "maps",
    "atlas",
    "remove crafted",
    "removes crafted",
    "cannot be changed",
    "cannot be modified",
    "can have up to",
    "tier",
    "rarity",
)

MOD_KEYWORDS = (
    "adds",
    "gain",
    "gains",
    "hits",
    "cannot",
    "can't",
    "you ",
    "your ",
    "nearby",
    "minions",
    "minion",
    "skills",
    "attacks",
    "spells",
    "trigger",
    "curse",
    "regenerate",
    "recover",
    "leech",
    "enemies",
    "projectiles",
    "melee",
    "bow",
    "warcry",
    "totems",
    "traps",
    "mines",
    "movement",
    "chance",
    "has ",
    "immune",
    "allocates",
    "grants",
    "converts",
    "converted",
    "avoid",
    "block",
    "stun",
    "frozen",
    "freeze",
    "ignite",
    "shock",
    "bleed",
    "poison",
    "impale",
    "blind",
    "suppress",
    "resist",
    "armour",
    "evasion",
    "energy shield",
    "life",
    "mana",
    "attribute",
    "strength",
    "dexterity",
    "intelligence",
    "damage",
    "speed",
    "accuracy",
    "critical",
    "quality",
    "level",
    "radius",
    "area",
    "duration",
    "penetrate",
    "penetration",
    "leech",
    "regen",
    "ward",
    "spirit",
)

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


class _TableParser(HTMLParser):
    """Collect tables (rows/cells) plus the heading that precedes each table."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[dict] = []
        self._table_depth = 0
        self._current: dict | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._heading: str | None = None
        self._heading_buf: list[str] | None = None
        self._pending_heading: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"h2", "h3", "h4"}:
            self._heading_buf = []
        elif tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._current = {"heading": self._pending_heading or "", "rows": []}
                self.tables.append(self._current)
        elif self._table_depth == 1 and tag == "tr" and self._current is not None:
            self._row = []
            self._current["rows"].append(self._row)
        elif self._table_depth == 1 and tag in {"td", "th"}:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h2", "h3", "h4"} and self._heading_buf is not None:
            self._heading = " ".join("".join(self._heading_buf).split())
            self._pending_heading = self._heading
            self._heading_buf = None
        elif tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._heading_buf is not None:
            self._heading_buf.append(data)
        elif self._cell is not None:
            self._cell.append(data)


def _fetch(url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = httpx.get(
                url,
                timeout=90,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en,zh-CN;q=0.8"},
            )
            response.raise_for_status()
            return response.text
        except Exception as error:  # noqa: BLE001 - transient TLS/HTTP failures
            last_error = error
            time.sleep(3 + attempt * 3)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def _tables(html: str) -> list[dict]:
    parser = _TableParser()
    parser.feed(html)
    return parser.tables


def _first_big_table(html: str) -> list[list[str]]:
    tables = _tables(html)
    if not tables:
        raise RuntimeError("no tables found")
    biggest = max(tables, key=lambda table: len(table["rows"]))
    return [row for row in biggest["rows"] if row]


def normalize_template(text: str) -> str:
    value = str(text or "").lower()
    value = value.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    value = re.sub(r"\(?\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\)?", "#", value)
    value = NUMBER_RE.sub("#", value)
    value = value.replace("+", "")
    value = re.sub(r"\s+", " ", value).strip(" .,")
    return value


def _looks_like_mod(text: str, classes: str) -> bool:
    lowered = text.lower()
    if not text or text.lower() in {"mod", "词缀"}:
        return False
    if any(hint in lowered for hint in NON_MOD_HINTS):
        return False
    if not any(keyword in lowered for keyword in MOD_KEYWORDS) and not NUMBER_RE.search(lowered):
        return False
    return bool(classes.strip())


def _slots_for_classes(classes: str) -> tuple[str, ...]:
    slots: list[str] = []
    for label, mapped in CLASS_TO_SLOTS.items():
        if label in classes:
            slots.extend(mapped)
    # Weapons are represented by weapon1/weapon2 in the CLI.
    return tuple(dict.fromkeys(slots))


def _ranges(text: str) -> list[dict[str, float]]:
    """Extract roll ranges from ``+(15-25) to maximum Life`` style text."""
    value = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    values: list[dict[str, float]] = []
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", value):
        low, high = float(match.group(1)), float(match.group(2))
        values.append({"min": min(low, high), "max": max(low, high)})
    if values:
        return values
    for match in NUMBER_RE.finditer(value):
        number = float(match.group(0))
        values.append({"min": number, "max": number})
    return values


def _wiki_affixes(html: str) -> dict[str, str]:
    """Map normalised modifier templates to ``prefix`` / ``suffix`` from the wiki."""
    mapping: dict[str, str] = {}
    for table in _tables(html):
        heading = (table.get("heading") or "").lower()
        if "prefix" not in heading and "suffix" not in heading:
            continue
        affix = "prefix" if "prefix" in heading else "suffix"
        rows = [row for row in table["rows"] if row]
        if not rows:
            continue
        header = [cell.lower() for cell in rows[0]]
        if "modifier" not in header:
            continue
        mod_index = header.index("modifier")
        for row in rows[1:]:
            if len(row) <= mod_index:
                continue
            template = normalize_template(row[mod_index])
            if template:
                mapping.setdefault(template, affix)
    return mapping


def build() -> dict:
    cn_html = _fetch(POEDB_CN)
    us_html = _fetch(POEDB_US)
    wiki_html = _fetch(WIKI)

    cn_rows = _first_big_table(cn_html)
    us_rows = _first_big_table(us_html)
    if len(cn_rows) != len(us_rows):
        raise RuntimeError(f"poedb cn/us row mismatch: {len(cn_rows)} vs {len(us_rows)}")

    affixes = _wiki_affixes(wiki_html)
    recipes: list[dict] = []
    for cn_row, us_row in zip(cn_rows[1:], us_rows[1:]):
        if len(us_row) < 4 or len(cn_row) < 4:
            continue
        text_en = us_row[0].strip()
        text_cn = cn_row[0].strip()
        classes = us_row[2].strip()
        if not _looks_like_mod(text_en, classes):
            continue
        slots = _slots_for_classes(classes)
        if not slots:
            continue
        template = normalize_template(text_en)
        recipes.append(
            {
                "text": text_en,
                "textZh": text_cn,
                "template": template,
                "affix": affixes.get(template, "unknown"),
                "slots": list(slots),
                "classes": [part.strip() for part in classes.split("·") if part.strip()],
                "values": _ranges(text_en),
                "cost": us_row[1].strip(),
                "unlock": us_row[3].strip(),
            }
        )

    recipes.sort(key=lambda item: (item["template"], item["affix"], item["slots"]))
    return {
        "source": {
            "cn": POEDB_CN,
            "en": POEDB_US,
            "affix": WIKI,
            "fetched": date.today().isoformat(),
            "gameVersion": "PoE1 3.30 (Covens)",
            "note": (
                "Crafting-bench modifiers. Values are the bench roll ranges; they are "
                "lower than high-tier natural modifiers. `affix` comes from the PoE wiki "
                "prefix/suffix sections; `unknown` means the row was not matched there."
            ),
        },
        "count": len(recipes),
        "recipes": recipes,
    }


def main() -> int:
    payload = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    affixed = sum(1 for recipe in payload["recipes"] if recipe["affix"] != "unknown")
    print(f"wrote {OUT} ({payload['count']} recipes, {affixed} with prefix/suffix)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
