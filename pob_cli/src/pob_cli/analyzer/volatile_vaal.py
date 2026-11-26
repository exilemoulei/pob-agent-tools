"""Zero-noise PoE1 Volatile Vaal Orb diagnostic opportunity hint (zero-RPC)."""

from __future__ import annotations

import re
from typing import Any

from ..formatters import format_bilingual, strip_pob_escapes

VOLATILE_VAAL_CORE_UNIQUES: frozenset[str] = frozenset(
    {
        "the ivory tower",
        "ephemeral edge",
        "mageblood",
        "aegis aurora",
        "crown of the inward eye",
        "rathpith globe",
        "astramentis",
        "shavronne's wrappings",
    }
)

_CORRUPTED_LINE_RE = re.compile(r"^\s*(?:corrupted|已腐化|已汙染|已污染)\s*$", re.IGNORECASE | re.MULTILINE)


def _is_slot_item_corrupted(slot_entry: dict[str, Any], item: dict[str, Any]) -> bool:
    if item.get("corrupted") is True:
        return True
    for raw_src in (slot_entry.get("raw"), item.get("raw")):
        if isinstance(raw_src, str) and raw_src and _CORRUPTED_LINE_RE.search(raw_src):
            return True
    return False


def _has_reliquarian_corrupted_synergy(
    build_info: dict[str, Any] | None,
    ascendancy_nodes: list[dict[str, Any]] | None,
    alloc_node_stats: list[tuple[str, list[str]]] | None,
    equipped_slots_raw: list[dict[str, Any]] | None,
) -> bool:
    asc = strip_pob_escapes((build_info or {}).get("ascendClassName")).strip().lower()
    if asc != "reliquarian":
        asc_from_nodes = any(
            strip_pob_escapes(str(n.get("asc") or "")).strip().lower() == "reliquarian"
            for n in (ascendancy_nodes or [])
            if isinstance(n, dict)
        )
        if not asc_from_nodes:
            return False

    for node in ascendancy_nodes or []:
        if not isinstance(node, dict):
            continue
        blob = " ".join(
            [
                str(node.get("name") or ""),
                *(str(s) for s in (node.get("stats") or [])),
            ]
        ).lower()
        if "shadowstitch" in blob or "corrupted item" in blob or "equipped corrupted" in blob:
            return True

    for lbl, stat_lines in alloc_node_stats or []:
        blob = f"{lbl} {' '.join(stat_lines)}".lower()
        if "shadowstitch" in blob or "corrupted item" in blob or "equipped corrupted" in blob:
            return True

    for entry in equipped_slots_raw or []:
        if not isinstance(entry, dict) or "Swap" in str(entry.get("slotName") or ""):
            continue
        it = entry.get("item")
        if isinstance(it, dict):
            nm = strip_pob_escapes(it.get("title") or it.get("name")).strip().lower()
            if nm == "shadowstitch":
                return True
    return False


def detect_volatile_vaal_opportunity(
    *,
    equipped_slots_raw: list[dict[str, Any]] | None,
    build_info: dict[str, Any] | None = None,
    ascendancy_nodes: list[dict[str, Any]] | None = None,
    alloc_node_stats: list[tuple[str, list[str]]] | None = None,
) -> dict[str, Any] | None:
    """Return a concise zero-RPC `[VOLATILE-VAAL]` hint ONLY when eligible uncorrupted uniques are worn.

    Never regex-multiplies literal mod numbers (preserving fixed denominators such as
    `per 100 Reserved Life` per R5). Returns `None` on Rare/Magic items, already-corrupted
    uniques, and ordinary builds.
    """
    if not equipped_slots_raw:
        return None

    reliquarian_synergy = _has_reliquarian_corrupted_synergy(
        build_info, ascendancy_nodes, alloc_node_stats, equipped_slots_raw
    )

    candidates: list[dict[str, Any]] = []
    seen_slots: set[str] = set()
    for entry in equipped_slots_raw:
        if not isinstance(entry, dict):
            continue
        slot_name = str(entry.get("slotName") or "").strip()
        if not slot_name or "Swap" in slot_name or slot_name in seen_slots:
            continue
        if entry.get("isFlask") or entry.get("nodeId") is not None or slot_name.startswith("Jewel"):
            continue
        item = entry.get("item")
        if not isinstance(item, dict):
            continue
        rarity = str(item.get("rarity") or "").strip().upper()
        if rarity not in ("UNIQUE", "RELIC"):
            continue
        if _is_slot_item_corrupted(entry, item):
            continue

        name_en = strip_pob_escapes(item.get("title") or item.get("name")).strip()
        if not name_en:
            continue
        name_zh = strip_pob_escapes(item.get("titleZh") or item.get("nameZh")).strip() or None
        is_core = name_en.lower() in VOLATILE_VAAL_CORE_UNIQUES
        if not is_core and not reliquarian_synergy:
            continue

        seen_slots.add(slot_name)
        candidates.append(
            {
                "slot": slot_name,
                "name": name_en,
                "nameZh": name_zh,
                "reason": "core_unique" if is_core else "reliquarian_corrupted_synergy",
            }
        )

    if not candidates:
        return None

    item_labels = [
        f"{format_bilingual(c['name'], c.get('nameZh'), kind='unique')} (`{c['slot']}`)"
        for c in candidates[:6]
    ]
    synergy_note = (
        " + Reliquarian Shadowstitch corrupted-item scaling"
        if reliquarian_synergy
        else ""
    )
    message = (
        f"Uncorrupted unique(s) eligible for Volatile Vaal Orb{synergy_note}: "
        f"{', '.join(item_labels)}. "
        "50% destroy risk (in-game preview shows new values before confirming); surviving unique "
        "becomes Corrupted with each rollable mod independently scaled 0.78x-1.22x in 0.01 steps "
        "(values 1 and 2 cannot change due to rounding; downsides scale too). "
        "Test exact roll ceiling in PoB via `pob item edit` / `pob batch` (never multiply fixed "
        "denominators like 'per 100 Reserved Life') and check live corrupted supply on official trade."
    )
    return {
        "tag": "[VOLATILE-VAAL]",
        "reliquarianCorruptedSynergy": reliquarian_synergy,
        "candidates": candidates,
        "message": message,
    }
