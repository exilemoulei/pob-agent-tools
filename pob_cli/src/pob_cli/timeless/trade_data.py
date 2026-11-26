"""Static PoE1 3.30 timeless jewel trade specs, devotion stat IDs, mod regexes, and conqueror resolvers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..game_data import fold_han
from .lut import _JEWEL_ZH_ALIASES, JEWEL_TYPE_IDS, JEWEL_ZH_TW_NAMES

# PoE 3.30 renamed Glorious Vanity's Ahuana -> Mazcotl while GGG's Trade stat ID stayed `ahuana`.
CONQUEROR_TRADE_SLUG_OVERRIDES: dict[str, str] = {"mazcotl": "ahuana"}
TRADE_SLUG_TO_POB_CONQUEROR: dict[str, str] = {"ahuana": "Mazcotl"}

# All 11 PoE1 3.30 Historic timeless jewels (verified against PoB LegionTradeIds.lua + Uniques/jewel.lua).
JEWEL_TRADE_SPECS: dict[str, dict[str, Any]] = {
    "Glorious Vanity": {
        "id": JEWEL_TYPE_IDS["Glorious Vanity"],
        "base": "Timeless Jewel",
        "conquerors": ("Doryani", "Xibaqua", "Mazcotl"),
        "legacyConquerors": ("Zerphi",),
    },
    "Lethal Pride": {
        "id": JEWEL_TYPE_IDS["Lethal Pride"],
        "base": "Timeless Jewel",
        "conquerors": ("Kaom", "Rakiata", "Akoya"),
        "legacyConquerors": ("Kiloava",),
    },
    "Brutal Restraint": {
        "id": JEWEL_TYPE_IDS["Brutal Restraint"],
        "base": "Timeless Jewel",
        "conquerors": ("Asenath", "Nasima", "Balbala"),
        "legacyConquerors": ("Deshret",),
    },
    "Militant Faith": {
        "id": JEWEL_TYPE_IDS["Militant Faith"],
        "base": "Timeless Jewel",
        "conquerors": ("Avarius", "Dominus", "Maxarius"),
        "legacyConquerors": ("Venarius",),
    },
    "Elegant Hubris": {
        "id": JEWEL_TYPE_IDS["Elegant Hubris"],
        "base": "Timeless Jewel",
        "conquerors": ("Cadiro", "Victario", "Caspiro"),
        "legacyConquerors": ("Chitus",),
    },
    "Heroic Tragedy": {
        "id": JEWEL_TYPE_IDS["Heroic Tragedy"],
        "base": "Timeless Jewel",
        "conquerors": ("Vorana", "Uhtred", "Medved"),
        "legacyConquerors": (),
    },
    "Festering Vengeance": {
        "id": JEWEL_TYPE_IDS["Festering Vengeance"],
        "base": "Murderous Eye Jewel",
        "conquerors": ("Tecrod",),
        "legacyConquerors": (),
    },
    "Extinguishing Grasp": {
        "id": JEWEL_TYPE_IDS["Extinguishing Grasp"],
        "base": "Searching Eye Jewel",
        "conquerors": ("Ulaman",),
        "legacyConquerors": (),
    },
    "Baleful Dominion": {
        "id": JEWEL_TYPE_IDS["Baleful Dominion"],
        "base": "Hypnotic Eye Jewel",
        "conquerors": ("Kurgal",),
        "legacyConquerors": (),
    },
    "Destructive Aspiration": {
        "id": JEWEL_TYPE_IDS["Destructive Aspiration"],
        "base": "Ghastly Eye Jewel",
        "conquerors": ("Amanamu",),
        "legacyConquerors": (),
    },
    "Reclaimed Malevolence": {
        "id": JEWEL_TYPE_IDS["Reclaimed Malevolence"],
        "base": "Assembled Eye Jewel",
        "conquerors": ("Zorath",),
        "legacyConquerors": (),
    },
}

# Verified against PoB Data/TimelessJewelData/LegionTradeIds.lua ([4].devotion) and Data/Uniques/jewel.lua.
MILITANT_FAITH_DEVOTION_STATS: tuple[tuple[str, str, str], ...] = (
    ("explicit.stat_2566390555", "Totem Damage", "4% increased Totem Damage per 10 Devotion"),
    ("explicit.stat_2697019412", "Brand Damage", "4% increased Brand Damage per 10 Devotion"),
    ("explicit.stat_970844066", "Channelling Damage", "Channelling Skills deal 4% increased Damage per 10 Devotion"),
    ("explicit.stat_1724614884", "Area Damage", "4% increased Area Damage per 10 Devotion"),
    ("explicit.stat_3103189267", "Elemental Damage", "4% increased Elemental Damage per 10 Devotion"),
    ("explicit.stat_1910205563", "Elemental Resistances", "+2% to all Elemental Resistances per 10 Devotion"),
    ("explicit.stat_1810368194", "Effect of non-Damaging Ailments", "3% increased Effect of non-Damaging Ailments on Enemies per 10 Devotion"),
    ("explicit.stat_730530528", "Elemental Ailment Duration", "4% reduced Elemental Ailment Duration on you per 10 Devotion"),
    ("explicit.stat_4235333770", "Duration of Curses", "4% reduced Duration of Curses on you per 10 Devotion"),
    ("explicit.stat_3808469650", "Minion Attack and Cast Speed", "1% increased Minion Attack and Cast Speed per 10 Devotion"),
    ("explicit.stat_2830135449", "Minions Accuracy Rating", "Minions have +60 to Accuracy Rating per 10 Devotion"),
    ("explicit.stat_2042813020", "Mana Regen", "Regenerate 0.6 Mana per Second per 10 Devotion"),
    ("explicit.stat_3293275880", "Skill Cost", "1% reduced Mana Cost of Skills per 10 Devotion"),
    ("explicit.stat_2585926696", "Non-Curse Aura Effect", "1% increased effect of Non-Curse Auras per 10 Devotion"),
    ("explicit.stat_2803981661", "Defences from Shield", "3% increased Defences from Equipped Shield per 10 Devotion"),
    ("explicit.stat_1355915086", "Mana Cost Efficiency", "3% increased Mana Cost Efficiency per 10 Devotion"),
)

JEWEL_BASE_ZH_TW: dict[str, str] = {
    "Timeless Jewel": "永恆珠寶",
    "Murderous Eye Jewel": "殺戮之眼珠寶",
    "Searching Eye Jewel": "勘查之眼珠寶",
    "Hypnotic Eye Jewel": "催眠之眼珠寶",
    "Ghastly Eye Jewel": "極懼之眼珠寶",
    "Assembled Eye Jewel": "拼湊之眼珠寶",
}

# Verified against official zh-rTW GGPK timeless jewel mod text.
ZH_CONQUEROR_TO_POB: dict[str, str] = {
    "多里亞尼": "Doryani", "賽巴昆": "Xibaqua", "阿呼阿娜": "Mazcotl", "澤佛伊": "Zerphi",
    "岡姆": "Kaom", "萊基亞塔": "Rakiata", "艾古亞": "Akoya", "基洛阿瓦": "Kiloava",
    "安賽娜絲": "Asenath", "納西瑪": "Nasima", "貝爾巴拉": "Balbala", "迪虛瑞特": "Deshret",
    "伊爾莉斯": "Avarius", "神主": "Dominus", "瑪薩里歐斯": "Maxarius", "維那利斯": "Venarius",
    "卡迪羅": "Cadiro", "維多里奧": "Victario", "卡斯皮羅": "Caspiro", "切特斯": "Chitus",
    "沃拉娜": "Vorana", "烏特雷": "Uhtred", "梅德偉": "Medved",
    "特克羅德": "Tecrod", "烏拉曼": "Ulaman", "柯戈": "Kurgal", "阿姆那姆": "Amanamu", "澤洛斯": "Zorath",
}

# Exact 8 timeless jewel explicit mod patterns from PoB Classes/TradeHelpers.lua (L188-195).
EN_TIMELESS_MOD_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"bathed in the blood of (\d+) sacrificed in the name of ([A-Za-z]+)",
        r"carved to glorify (\d+) new faithful converted by high templar ([A-Za-z]+)",
        r"commanded leadership over (\d+) warriors under ([A-Za-z]+)",
        r"commissioned (\d+) coins to commemorate ([A-Za-z]+)",
        r"denoted service of (\d+) dekhara in the akhara of ([A-Za-z]+)",
        r"remembrancing (\d+) songworthy deeds by the line of ([A-Za-z]+)",
        r"subjugating (\d+) souls in the thrall of ([A-Za-z]+)",
        r"binding (\d+) souls to phylacteries to sustain ([A-Za-z]+)",
    )
)
PAREN_HINT_RE = re.compile(r"[\(（][^\)）]*[\)）]")
SEED_DIGITS_RE = re.compile(r"(?<!\d)(\d{2,6})(?!\d)")
ZH_CONQUEROR_ENTRIES: tuple[tuple[str, str, str], ...] = tuple(
    sorted(
        ((zh, fold_han(zh), pob) for zh, pob in ZH_CONQUEROR_TO_POB.items()),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

_ALL_KNOWN_POB_CONQUERORS: dict[str, str] = {
    c.lower(): c
    for spec in JEWEL_TRADE_SPECS.values()
    for c in (*spec["conquerors"], *spec["legacyConquerors"])
}
_ALL_KNOWN_POB_CONQUERORS.setdefault("ahuana", "Mazcotl")
_FOLDED_ZH_CONQUERORS: dict[str, str] = {fold_han(k): v for k, v in ZH_CONQUEROR_TO_POB.items()}
_PAREN_STRIP_RE = re.compile(r"\s*[\(（][^\)）]*[\)）]")
_PSEUDO_STAT_PREFIX = "explicit.pseudo_timeless_jewel_"


def canonical_conqueror_key(conqueror: str) -> str:
    """Return lowercase Trade API slug for a conqueror (`Mazcotl` and `Ahuana` both -> `'ahuana'`)."""
    cleaned = _PAREN_STRIP_RE.sub("", str(conqueror or "")).strip()
    if not cleaned:
        return ""
    if cleaned in ZH_CONQUEROR_TO_POB:
        cleaned = ZH_CONQUEROR_TO_POB[cleaned]
    elif fold_han(cleaned) in _FOLDED_ZH_CONQUERORS:
        cleaned = _FOLDED_ZH_CONQUERORS[fold_han(cleaned)]
    low = cleaned.lower()
    if low.startswith(_PSEUDO_STAT_PREFIX):
        low = low[len(_PSEUDO_STAT_PREFIX) :]
    return CONQUEROR_TRADE_SLUG_OVERRIDES.get(low, low)


def pob_conqueror_name(conqueror: str) -> str:
    """Reverse-map a Trade API slug/stat ID or legacy/ZH conqueror name to canonical PoB 3.30 name."""
    slug = canonical_conqueror_key(conqueror)
    if not slug:
        return ""
    if slug in TRADE_SLUG_TO_POB_CONQUEROR:
        return TRADE_SLUG_TO_POB_CONQUEROR[slug]
    return _ALL_KNOWN_POB_CONQUERORS.get(slug, slug.capitalize())


def conqueror_trade_stat_id(conqueror: str) -> str:
    """Resolve a PoB conqueror name (including 3.30 `Mazcotl` -> `ahuana`) to its GGG Trade stat ID."""
    slug = canonical_conqueror_key(conqueror)
    if not slug:
        raise ValueError("Conqueror name cannot be empty.")
    return f"{_PSEUDO_STAT_PREFIX}{slug}"


def resolve_jewel_spec(jewel_name: str) -> tuple[str, dict[str, Any]]:
    """Resolve English or Chinese timeless jewel name to `(canonical_english_name, spec)`."""
    cleaned = str(jewel_name or "").strip()
    if not cleaned:
        raise ValueError("Timeless jewel name cannot be empty.")
    low = cleaned.lower()
    for name, spec in JEWEL_TRADE_SPECS.items():
        if name.lower() == low:
            return name, spec
    folded = fold_han(cleaned)
    for en_name, tw_name in JEWEL_ZH_TW_NAMES.items():
        if (cleaned == tw_name or folded == fold_han(tw_name)) and en_name in JEWEL_TRADE_SPECS:
            return en_name, JEWEL_TRADE_SPECS[en_name]
    mapped = _JEWEL_ZH_ALIASES.get(cleaned) or _JEWEL_ZH_ALIASES.get(folded)
    if mapped and mapped in JEWEL_TRADE_SPECS:
        return mapped, JEWEL_TRADE_SPECS[mapped]
    raise ValueError(f"Unsupported timeless jewel for trade search: {jewel_name!r}")


def resolve_conqueror_stat_ids(jewel_name: str, conqueror: str | None) -> tuple[list[str], list[str]]:
    """Return `(pob_conqueror_names, trade_stat_ids)` strictly scoped to `jewel_name`'s own conquerors."""
    canon_jewel, spec = resolve_jewel_spec(jewel_name)
    conquerors: tuple[str, ...] = spec["conquerors"]
    legacy: tuple[str, ...] = spec.get("legacyConquerors") or ()
    if not conquerors:
        if conqueror and str(conqueror).strip().lower() not in ("any", "*"):
            raise ValueError(f"{canon_jewel} has no conquerors (got {conqueror!r}).")
        return [], []
    if not conqueror or str(conqueror).strip().lower() in ("any", "*"):
        names = list(conquerors)
        return names, [conqueror_trade_stat_id(c) for c in names]
    req_slug = canonical_conqueror_key(conqueror)
    for c in (*conquerors, *legacy):
        if canonical_conqueror_key(c) == req_slug:
            return [c], [conqueror_trade_stat_id(c)]
    raise ValueError(f"Unknown conqueror {conqueror!r} for {canon_jewel}; valid: {', '.join(conquerors)}, any")


def parse_devotion_mods(jewel_name: str, mods: Sequence[str] = ()) -> tuple[list[str], list[str]]:
    """Map Militant Faith `--mod` devotion lines/labels to `(resolved_stat_ids, unmatched_mods)`."""
    canon_jewel, _ = resolve_jewel_spec(jewel_name)
    if canon_jewel != "Militant Faith" or not mods:
        return [], []
    resolved: list[str] = []
    unmatched: list[str] = []
    for raw in mods:
        text = str(raw or "").strip()
        if not text:
            continue
        low = text.lower()
        if low.startswith("explicit.stat_"):
            if low not in resolved:
                resolved.append(low)
            continue
        norm = re.sub(r"\s+", " ", low)
        hit = None
        for stat_id, label, mod_line in MILITANT_FAITH_DEVOTION_STATS:
            if norm == label.lower() or norm == mod_line.lower() or label.lower() in norm:
                hit = stat_id
                break
        if hit is not None:
            if hit not in resolved:
                resolved.append(hit)
        elif text not in unmatched:
            unmatched.append(text)
    return resolved, unmatched


def resolve_devotion_stat_ids(jewel_name: str, mods: Sequence[str] = ()) -> list[str]:
    """Return resolved Militant Faith devotion Trade API `explicit.stat_*` IDs."""
    return parse_devotion_mods(jewel_name, mods)[0]


def parse_timeless_mod_line(line: str) -> tuple[int | None, str | None]:
    """Parse `(seed, canonical_pob_conqueror)` from a single EN or zh-TW/zh-CN timeless mod line."""
    text = " ".join(str(line or "").splitlines()).strip()
    if not text:
        return None, None
    for pattern in EN_TIMELESS_MOD_RES:
        m = pattern.search(text)
        if m:
            return int(m.group(1)), pob_conqueror_name(m.group(2))
    stripped = PAREN_HINT_RE.sub("", text)
    folded = fold_han(stripped)
    for zh_raw, zh_folded, pob_name in ZH_CONQUEROR_ENTRIES:
        if zh_raw in stripped or zh_folded in folded:
            dm = SEED_DIGITS_RE.search(stripped)
            if dm:
                return int(dm.group(1)), pob_name
    return None, None
