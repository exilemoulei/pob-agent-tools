"""Special-item extractors for PoB shopping lists: anoints, jewels, timeless seeds, and key gems."""

from __future__ import annotations

import base64
import copy
import gzip
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..locale_cfg import normalize_realm, trade_base_url_for_realm
from ..trade_client import localize_query_for_tw, map_tw_league, unmapped_tw_ascii_fields
from ..trade_item import Item

_CJK_RESIDUE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_DEFINING_JEWEL_KINDS: frozenset[str] = frozenset({"watchers_eye", "forbidden_jewel", "radius_jewel"})

_CLUSTER_PASSIVES_RE = re.compile(r"^Adds\s+(\d+)\s+Passive Skills$", re.IGNORECASE)
_CLUSTER_GRANT_RE = re.compile(r"^Added Small Passive Skills grant:\s*(.+)$", re.IGNORECASE)
_CLUSTER_NOTABLE_RE = re.compile(r"^1 Added Passive Skill is\s+(.+)$", re.IGNORECASE)
_TIMELESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"bathed in the blood of (\d+) sacrificed in the name of ([a-z]+)", re.IGNORECASE),
    re.compile(r"carved to glorify (\d+) new faithful converted by high templar ([a-z]+)", re.IGNORECASE),
    re.compile(r"commanded leadership over (\d+) warriors under ([a-z]+)", re.IGNORECASE),
    re.compile(r"commissioned (\d+) coins to commemorate ([a-z]+)", re.IGNORECASE),
    re.compile(r"denoted service of (\d+) dekhara in the akhara of ([a-z]+)", re.IGNORECASE),
    re.compile(r"remembrancing (\d+) songworthy deeds by the line of ([a-z]+)", re.IGNORECASE),
    re.compile(r"subjugating (\d+) souls in the thrall of ([a-z]+)", re.IGNORECASE),
    re.compile(r"binding (\d+) souls to phylacteries to sustain ([a-z]+)", re.IGNORECASE),
)
_TIMELESS_JEWEL_NAMES: frozenset[str] = frozenset(
    {"glorious vanity", "lethal pride", "brutal restraint", "militant faith", "elegant hubris"}
)
_STANDARD_OF_GEMS: frozenset[str] = frozenset(
    {
        "herald of ash",
        "herald of ice",
        "herald of thunder",
        "herald of purity",
        "herald of agony",
        "purity of elements",
        "purity of fire",
        "purity of ice",
        "purity of lightning",
        "orb of storms",
        "sigil of power",
        "wave of conviction",
        "rain of arrows",
        "eye of winter",
        "bane of condemnation",
        "mark of submission",
        "wrath",
        "aspect of the avian",
        "aspect of the cat",
        "aspect of the crab",
        "aspect of the spider",
    }
)
_EXCEPTIONAL_SUPPORTS: frozenset[str] = frozenset(
    {
        "empower",
        "empower support",
        "enlighten",
        "enlighten support",
        "enhance",
        "enhance support",
        "awakened empower support",
        "awakened enlighten support",
        "awakened enhance support",
    }
)

_ANOINT_CACHE: dict[str, dict[str, list[str]]] = {}
_ANOINT_LOCK = threading.Lock()
_TREE_NODE_NAME_RE = re.compile(r'\["name"\]\s*=\s*"([^"]+)"')
_TREE_OIL_RE = re.compile(r'"([A-Za-z]+Oil)"')


@dataclass
class ShoppingEntry:
    section: str  # gear | flask | jewel | gem | anoint
    slot: str
    kind: str  # unique | rare | magic_flask | cluster_jewel | watchers_eye | forbidden_jewel | radius_jewel | timeless_jewel | rare_jewel | gem | anoint
    name: str
    base_type: str = ""
    rarity: str = "UNIQUE"
    links: int = 0
    corrupted: bool = False
    foulborn: bool = False
    gem_level: int | None = None
    gem_quality: int | None = None
    quantity: int = 1
    is_support: bool = False
    is_awakened: bool = False
    is_transfigured: bool = False
    is_exceptional: bool = False
    raw_text: str = ""
    parsed: Item | None = None
    cluster_passives: int | None = None
    cluster_grant: str | None = None
    cluster_notables: list[str] = field(default_factory=list)
    timeless_seed: int | None = None
    timeless_conqueror: str | None = None
    anoint_notable: str | None = None
    anoint_oils: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _format_oil_name(raw_oil: str) -> str:
    return re.sub(r"([a-z])([A-Z])", r"\1 \2", raw_oil.strip())


def _parse_tree_anoints(tree_path: Path) -> dict[str, list[str]]:
    recipes: dict[str, list[str]] = {}
    try:
        text = tree_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return recipes
    cur_name: str | None = None
    in_recipe = False
    cur_oils: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not in_recipe:
            m_name = _TREE_NODE_NAME_RE.search(s)
            if m_name:
                cur_name = m_name.group(1).strip().lower()
                continue
            if s.startswith('["recipe"] = {'):
                in_recipe = True
                cur_oils = []
        else:
            if s.startswith("}"):
                if cur_name and cur_oils:
                    recipes[cur_name] = [_format_oil_name(o) for o in cur_oils]
                in_recipe = False
                cur_oils = []
            else:
                m_oil = _TREE_OIL_RE.search(s)
                if m_oil:
                    cur_oils.append(m_oil.group(1))
    return recipes


def lookup_anoint_oils(notable_name: str, pob_dir: Path | None = None) -> list[str]:
    """Look up the 3-oil anoint recipe for a passive notable from PoB's local ``tree.lua``."""
    target = str(notable_name or "").strip().lower()
    if not target:
        return []
    import pob_cli.trade_query as _tq

    from ..game_data import _find_latest_tree_lua

    try:
        root = Path(pob_dir) if pob_dir is not None else _tq.resolve_engine_root()
        _, tree_path = _find_latest_tree_lua(root / "PoE1")
    except Exception:
        return []
    cache_key = str(tree_path.resolve())
    with _ANOINT_LOCK:
        table = _ANOINT_CACHE.get(cache_key)
    if table is None:
        table = _parse_tree_anoints(tree_path)
        with _ANOINT_LOCK:
            _ANOINT_CACHE[cache_key] = table
    return list(table.get(target) or ())


def _extract_timeless(item: Item) -> tuple[int | None, str | None]:
    for mod in item.mods:
        for pat in _TIMELESS_PATTERNS:
            m = pat.search(mod.raw)
            if m:
                seed = int(m.group(1))
                conq = m.group(2).strip().lower()
                # PoB 3.30 renamed Ahuana -> Mazcotl in item text, while GGG's Trade API stat ID
                # stays `explicit.pseudo_timeless_jewel_ahuana` (PoE1/Classes/TreeTab.lua:2273-2277, LegionTradeIds.lua).
                if conq == "mazcotl":
                    conq = "ahuana"
                return seed, conq
    return None, None


def _classify_jewel(
    parsed: Item,
    clean_name: str,
    slot_label: str,
    raw_text: str,
    foulborn: bool,
) -> ShoppingEntry:
    name_low = clean_name.lower()
    base_low = (parsed.base_type or "").lower()

    if "cluster jewel" in base_low:
        passives: int | None = None
        grant: str | None = None
        notables: list[str] = []
        for mod in parsed.mods:
            if m_p := _CLUSTER_PASSIVES_RE.match(mod.raw):
                passives = int(m_p.group(1))
            elif m_g := _CLUSTER_GRANT_RE.match(mod.raw):
                grant = m_g.group(1).strip()
            elif m_n := _CLUSTER_NOTABLE_RE.match(mod.raw):
                notables.append(m_n.group(1).strip())
        return ShoppingEntry(
            section="jewel",
            slot=slot_label,
            kind="cluster_jewel",
            name=clean_name or parsed.base_type,
            base_type=parsed.base_type,
            rarity=parsed.rarity,
            corrupted=parsed.corrupted,
            raw_text=raw_text,
            parsed=parsed,
            cluster_passives=passives,
            cluster_grant=grant,
            cluster_notables=notables,
        )

    if name_low == "watcher's eye":
        return ShoppingEntry(
            section="jewel",
            slot=slot_label,
            kind="watchers_eye",
            name="Watcher's Eye",
            base_type=parsed.base_type or "Prismatic Jewel",
            rarity="UNIQUE",
            corrupted=parsed.corrupted,
            raw_text=raw_text,
            parsed=parsed,
        )

    if name_low in ("forbidden flesh", "forbidden flame"):
        return ShoppingEntry(
            section="jewel",
            slot=slot_label,
            kind="forbidden_jewel",
            name="Forbidden Flesh" if name_low == "forbidden flesh" else "Forbidden Flame",
            base_type=parsed.base_type,
            rarity="UNIQUE",
            corrupted=parsed.corrupted,
            raw_text=raw_text,
            parsed=parsed,
        )

    if name_low in ("thread of hope", "impossible escape", "sublime vision", "the light of meaning"):
        return ShoppingEntry(
            section="jewel",
            slot=slot_label,
            kind="radius_jewel",
            name=clean_name,
            base_type=parsed.base_type,
            rarity="UNIQUE",
            corrupted=parsed.corrupted,
            raw_text=raw_text,
            parsed=parsed,
        )

    seed, conq = _extract_timeless(parsed)
    if name_low in _TIMELESS_JEWEL_NAMES or (seed is not None and conq is not None):
        return ShoppingEntry(
            section="jewel",
            slot=slot_label,
            kind="timeless_jewel",
            name=clean_name or "Timeless Jewel",
            base_type=parsed.base_type or "Timeless Jewel",
            rarity="UNIQUE",
            corrupted=parsed.corrupted,
            raw_text=raw_text,
            parsed=parsed,
            timeless_seed=seed,
            timeless_conqueror=conq,
        )

    if parsed.rarity in ("UNIQUE", "RELIC"):
        return ShoppingEntry(
            section="jewel",
            slot=slot_label,
            kind="unique",
            name=clean_name,
            base_type=parsed.base_type,
            rarity=parsed.rarity,
            corrupted=parsed.corrupted,
            foulborn=foulborn,
            raw_text=raw_text,
            parsed=parsed,
        )

    return ShoppingEntry(
        section="jewel",
        slot=slot_label,
        kind="rare_jewel",
        name=clean_name or parsed.base_type,
        base_type=parsed.base_type,
        rarity=parsed.rarity,
        corrupted=parsed.corrupted,
        raw_text=raw_text,
        parsed=parsed,
    )


def _is_transfigured_gem_name(name: str) -> bool:
    low = (name or "").strip().lower()
    if " of " not in low or low in _STANDARD_OF_GEMS:
        return False
    if low.startswith("awakened "):
        return False
    return True


def _extract_key_gems(skills_res: dict[str, Any] | None) -> list[ShoppingEntry]:
    if not isinstance(skills_res, dict):
        return []
    seen: dict[tuple[str, int, int, bool], ShoppingEntry] = {}
    for grp in skills_res.get("groups") or []:
        if not isinstance(grp, dict) or grp.get("enabled") is False or grp.get("source"):
            continue
        slot_lbl = str(grp.get("slot") or grp.get("displayLabel") or "Skill")
        for gem in grp.get("gems") or []:
            if not isinstance(gem, dict) or gem.get("enabled") is False:
                continue
            name = str(gem.get("nameSpec") or gem.get("name") or "").strip()
            if not name:
                continue
            level = int(gem.get("level") or 1)
            quality = int(gem.get("quality") or 0)
            is_support = (
                bool(gem.get("isSupport") or gem.get("support"))
                or name.lower().endswith(" support")
                or str(gem.get("skillId") or "").startswith("Support")
            )
            low = name.lower()
            is_awakened = low.startswith("awakened ")
            is_trans = _is_transfigured_gem_name(name)
            is_exc = low in _EXCEPTIONAL_SUPPORTS or (
                is_support and any(low.startswith(k) for k in ("empower", "enlighten", "enhance"))
            )
            corrupted = bool(
                level >= 21
                or quality > 20
                or (is_exc and not is_awakened and level >= 4)
                or (is_awakened and level >= 6)
                or (is_exc and is_awakened and level >= 5)
            )
            is_high_value = bool(
                is_awakened
                or is_trans
                or is_exc
                or level >= 21
                or quality >= 21
                or (quality >= 20 and corrupted)
            )
            if not is_high_value:
                continue
            canon_name = name
            if is_support and not low.endswith(" support"):
                canon_name = f"{name} Support"
            key = (canon_name.lower(), level, quality, corrupted)
            if key in seen:
                seen[key].quantity += 1
                continue
            seen[key] = ShoppingEntry(
                section="gem",
                slot=slot_lbl,
                kind="gem",
                name=canon_name,
                base_type=canon_name,
                rarity="GEM",
                corrupted=corrupted,
                gem_level=level,
                gem_quality=quality,
                quantity=1,
                is_support=is_support,
                is_awakened=is_awakened,
                is_transfigured=is_trans,
                is_exceptional=is_exc,
            )
    return list(seen.values())


def encode_trade_search_url(
    league: str,
    query_body: dict[str, Any],
    *,
    realm: str = "global",
    pob_dir: Path | None = None,
) -> tuple[str, str, str, dict[str, Any]]:
    """Encode an official PoE1 Trade search body locally with 0 HTTP requests.

    Returns ``(direct_url, encoded_query_id, query_param_url, localized_body)``.
    ``direct_url`` matches PoB ``CompareBuySimilar.lua`` & ``seller_url`` (``/<b64gzip>``).
    ``query_param_url`` uses ``?q=<urlencoded JSON>`` so browsers also accept direct JSON queries.
    """
    eff_realm = normalize_realm(realm)
    eff_league = map_tw_league(league) if eff_realm == "tw" else (league.strip() or "Standard")
    localized = localize_query_for_tw(query_body, pob_dir=pob_dir) if eff_realm == "tw" else copy.deepcopy(query_body)
    inner = localized.get("query") if isinstance(localized.get("query"), dict) else localized
    raw_json = json.dumps(inner, separators=(",", ":"), ensure_ascii=False)
    blob = gzip.compress(raw_json.encode("utf-8"), mtime=0)
    query_id = base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
    host = trade_base_url_for_realm(eff_realm).rstrip("/")
    league_q = quote(eff_league, safe="")
    direct_url = f"{host}/trade/search/{league_q}/{query_id}"
    q_param = quote(json.dumps({"query": inner}, separators=(",", ":"), ensure_ascii=False), safe="")
    query_param_url = f"{host}/trade/search/{league_q}?q={q_param}"
    return direct_url, query_id, query_param_url, localized


def has_defining_mods(entry: ShoppingEntry) -> bool:
    if entry.kind not in _DEFINING_JEWEL_KINDS or entry.parsed is None:
        return False
    if entry.kind == "watchers_eye":
        return any("while affected by" in m.template for m in entry.parsed.mods)
    return any(m.kind == "explicit" for m in entry.parsed.mods)


def collect_item_query_warnings(
    entry: ShoppingEntry,
    localized_body: dict[str, Any],
    *,
    eff_realm: str,
) -> tuple[list[str], bool]:
    """Return ``(warnings, defining_mods_unresolved)`` for one localized shopping-list query."""
    item_warnings: list[str] = []
    if eff_realm == "tw":
        for fld, val in unmapped_tw_ascii_fields(localized_body):
            item_warnings.append(f"unmapped TW {fld}={val!r}")
    else:
        q_inner = localized_body.get("query") if isinstance(localized_body.get("query"), dict) else {}
        for fld in ("name", "type"):
            v_str = str(q_inner.get(fld) or "")
            if v_str and _CJK_RESIDUE_RE.search(v_str):
                item_warnings.append(f"global query contains CJK {fld}={v_str!r}")
    stat_groups = (localized_body.get("query") or {}).get("stats") or []
    has_defining_filter = any(
        isinstance(flt, dict) and not str(flt.get("id") or "").lower().startswith("implicit.")
        for grp in stat_groups
        if isinstance(grp, dict)
        for flt in (grp.get("filters") or ())
    )
    defining_unresolved = bool(has_defining_mods(entry) and not has_defining_filter)
    if defining_unresolved:
        item_warnings.append("defining modifiers could not be resolved (stat catalog incomplete)")
    return item_warnings, defining_unresolved

