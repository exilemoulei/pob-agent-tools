"""Slot-exact Mageblood Magic Utility Flask exemption filter matching PoB CalcPerform.lua (L2)."""

from __future__ import annotations

import re
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from .summary import _active_item_lines, _has_mageblood_equipped, _is_tincture_item

_MAGEBLOOD_SPEC_RE = re.compile(
    r"\b(leftmost|rightmost)\s+(?:(\d+)|\((\d+)\s*-\s*(\d+)\))\s+magic\s+utility\s+flasks?\b",
    re.IGNORECASE,
)
_MAGEBLOOD_ZH_RE = re.compile(
    r"(最左侧|最右侧|最左側|最右側)\s*(?:(\d+)|\((\d+)\s*-\s*(\d+)\))\s*瓶?魔法(?:功能|效用)?(?:药剂|藥劑)",
)
_MOD_RANGE_RE = re.compile(r"\{range:([0-9.]+)\}")
_MOD_TAG_RE = re.compile(r"\{[^}]+\}")
_FLASK_NUM_RE = re.compile(r"flask\s*(\d+)", re.IGNORECASE)
_NON_UTILITY_FLASK_TOKENS = (
    "life flask",
    "mana flask",
    "hybrid flask",
    "生命药剂",
    "魔力药剂",
    "复合药剂",
    "生命藥劑",
    "魔力藥劑",
    "複合藥劑",
)


def _resolve_range_count(line: str, lit: str | None, lo_s: str | None, hi_s: str | None) -> int:
    if lit is not None:
        return max(1, min(5, int(lit)))
    lo, hi = int(lo_s or 2), int(hi_s or 4)
    mr = _MOD_RANGE_RE.search(line)
    val = round(lo + float(mr.group(1)) * (hi - lo)) if mr else hi
    return max(1, min(5, val))


def parse_mageblood_spec(belt_raw: str | None) -> tuple[str, int]:
    """Parse active Mageblood direction ('left'|'right') and flask count (1..5, default 4)."""
    if belt_raw:
        for ln in _active_item_lines(belt_raw):
            clean_ln = _MOD_TAG_RE.sub("", ln)
            m = _MAGEBLOOD_SPEC_RE.search(clean_ln)
            if m:
                direction = "right" if m.group(1).lower() == "rightmost" else "left"
                return direction, _resolve_range_count(ln, m.group(2), m.group(3), m.group(4))
            mz = _MAGEBLOOD_ZH_RE.search(clean_ln)
            if mz:
                direction = "right" if mz.group(1) in ("最右侧", "最右側") else "left"
                return direction, _resolve_range_count(ln, mz.group(2), mz.group(3), mz.group(4))
    return "left", 4


def is_magic_utility_flask(
    item: dict[str, Any] | None,
    *,
    allow_unknown_rarity: bool = False,
) -> bool:
    """True when `item` is a Magic Utility Flask (matching PoB CalcPerform.lua:1625)."""
    if not isinstance(item, dict) or not item:
        return False
    if _is_tincture_item(item):
        return False
    text = " ".join(
        strip_pob_escapes(str(item.get(k) or ""))
        for k in ("baseName", "baseNameZh", "name", "nameZh", "title", "titleZh", "type")
    ).lower()
    if any(tok in text for tok in _NON_UTILITY_FLASK_TOKENS):
        return False
    rarity = str(item.get("rarity") or "").strip().upper()
    if rarity:
        return rarity == "MAGIC"
    return allow_unknown_rarity


def _flask_slot_order(slot_name: str, fallback_idx: int) -> int:
    m = _FLASK_NUM_RE.search(slot_name)
    return int(m.group(1)) if m else (100 + fallback_idx)


def mageblood_exempt_flask_slots(
    worker: BridgeWorker,
    items_res: dict[str, Any],
    *,
    equipped_slots_raw: list[dict[str, Any]] | None = None,
) -> tuple[set[str], str]:
    """Return `(exempt_slot_names, spec_label)` for equipped Mageblood, or `(set(), '')`."""
    if not isinstance(items_res, dict) or not _has_mageblood_equipped(items_res):
        return set(), ""

    items_by_id = {
        it["id"]: it
        for it in (items_res.get("items") or [])
        if isinstance(it, dict) and "id" in it
    }
    belt_raw = ""
    for entry in equipped_slots_raw or []:
        if isinstance(entry, dict) and str(entry.get("slotName") or "") == "Belt" and entry.get("raw"):
            belt_raw = str(entry["raw"])
            break
    if not belt_raw:
        for slot in items_res.get("slots") or []:
            if isinstance(slot, dict) and slot.get("name") == "Belt" and slot.get("selItemId"):
                b_item = items_by_id.get(slot["selItemId"]) or {}
                belt_raw = str(b_item.get("raw") or "")
                if not belt_raw and callable(getattr(worker, "get_cached_tree_data", None)):
                    try:
                        res = worker.call("item_raw", {"id": slot["selItemId"]})
                        if isinstance(res, dict) and res.get("raw"):
                            belt_raw = str(res["raw"])
                    except Exception:
                        pass
                break

    direction, count = parse_mageblood_spec(belt_raw)
    flask_slots: list[tuple[int, str, dict[str, Any]]] = []
    for pos, slot in enumerate(items_res.get("slots") or []):
        if not isinstance(slot, dict) or not slot.get("isFlask"):
            continue
        sname = str(slot.get("name") or "")
        item = items_by_id.get(slot.get("selItemId") or 0) or {}
        if sname and item:
            flask_slots.append((_flask_slot_order(sname, pos), sname, item))
    flask_slots.sort(key=lambda tup: tup[0])

    any_rarity_known = any(bool(str(it.get("rarity") or "").strip()) for _, _, it in flask_slots)
    magic_utility_names = [
        sname
        for _, sname, it in flask_slots
        if is_magic_utility_flask(it, allow_unknown_rarity=not any_rarity_known)
    ]
    exempt_list = magic_utility_names[-count:] if direction == "right" else magic_utility_names[:count]
    dir_lbl = "Rightmost" if direction == "right" else "Leftmost"
    return set(exempt_list), f"Mageblood {dir_lbl} {count} Magic Utility Flask"
