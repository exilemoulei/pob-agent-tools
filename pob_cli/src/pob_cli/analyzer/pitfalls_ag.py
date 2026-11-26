"""Animate Guardian secondary ItemSet inspection: Exposure conflicts and team-buff dependencies (L2)."""

from __future__ import annotations

import re
from typing import Any

from ..formatters import strip_pob_escapes
from ..snapshot import _call_batch
from .summary import _active_item_lines

_EXPOSURE_ELEM_RE = re.compile(r"\b(fire|cold|lightning)\s+exposure\b", re.IGNORECASE)
_AG_KEY_UNIQUES: tuple[tuple[str, str], ...] = (
    ("kingmaker", "Kingmaker (+50% Crit Multi, +10 Fortification, Culling Strike)"),
    ("garb of the ephemeral", "Garb of the Ephemeral (Action Speed floor, nearby enemies cannot Crit)"),
    ("crown of the tyrant", "Crown of the Tyrant (-10% res / flat damage aura)"),
    ("dying breath", "Dying Breath (18% increased Damage / Aura Effect)"),
    ("leer cast", "Leer Cast (50% increased Damage aura)"),
)


def fetch_minion_itemsets(worker: Any) -> dict[str, Any]:
    """Fetch secondary minion item sets (e.g. Animate Guardian gear) with FakeWorker/legacy fallback."""
    if worker is None or not callable(getattr(worker, "get_cached_tree_data", None)):
        caps = getattr(worker, "caps", None)
        if not (isinstance(caps, dict) and caps.get("minionItemSets")):
            return {}
    try:
        res = _call_batch(worker, [("get_minion_itemsets", {})], raise_on_error=False)
    except Exception:
        return {}
    if not res or not isinstance(res[0], dict) or res[0].get("_error") or res[0].get("ok") is False:
        return {}
    return res[0]


def extract_ag_on_hit_mod_lines(minion_itemsets: dict[str, Any] | None) -> list[str]:
    """Collect active item mod lines from enabled Animate Guardian bound ItemSet(s)."""
    if not isinstance(minion_itemsets, dict):
        return []
    sets_map = minion_itemsets.get("minionItemSets")
    if not isinstance(sets_map, dict):
        return []
    lines: list[str] = []
    seen_sets: set[str] = set()
    for bnd in minion_itemsets.get("bindings") or []:
        if not isinstance(bnd, dict) or not bnd.get("enabled", True) or "Swap" in str(bnd.get("slot") or ""):
            continue
        nm_low = f"{bnd.get('name') or ''} {bnd.get('skillId') or ''}".lower()
        if "animate guardian" not in nm_low and "animateguardian" not in nm_low:
            continue
        set_id = bnd.get("skillMinionItemSet")
        if set_id is None:
            continue
        sid_key = str(set_id)
        if sid_key in seen_sets:
            continue
        seen_sets.add(sid_key)
        iset = sets_map.get(sid_key) or sets_map.get(set_id)
        if not isinstance(iset, dict):
            continue
        for sl in iset.get("slots") or []:
            if not isinstance(sl, dict) or "Swap" in str(sl.get("slotName") or ""):
                continue
            raw = str(sl.get("raw") or "")
            if raw:
                lines.extend(strip_pob_escapes(ln) for ln in _active_item_lines(raw))
    return lines


def iter_enabled_ag_itemsets(ctx: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return `(binding, itemset)` pairs for enabled non-Swap Animate Guardian gems."""
    msets = ctx.get("minion_itemsets") or {}
    sets_map = msets.get("minionItemSets") if isinstance(msets, dict) else None
    if not isinstance(sets_map, dict):
        return []
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for bnd in msets.get("bindings") or []:
        if not isinstance(bnd, dict) or not bnd.get("enabled", True) or "Swap" in str(bnd.get("slot") or ""):
            continue
        nm_low = f"{bnd.get('name') or ''} {bnd.get('skillId') or ''}".lower()
        if "animate guardian" not in nm_low and "animateguardian" not in nm_low:
            continue
        set_id = bnd.get("skillMinionItemSet")
        if set_id is None:
            continue
        iset = sets_map.get(str(set_id)) or sets_map.get(set_id)
        if isinstance(iset, dict):
            out.append((bnd, iset))
    return out


def find_ag_exposure_conflicts(ctx: dict[str, Any], invert_sources: list[str]) -> list[str]:
    """Find enabled Animate Guardian ItemSet items inflicting Exposure matching inverted elements."""
    src_blob = " ".join(invert_sources).lower()
    if "invert the rules" in src_blob or "rakiata" in src_blob or "elemental resistance" in src_blob:
        inv_elems = {"fire", "cold", "lightning"}
    else:
        inv_elems = {e for e in ("fire", "cold", "lightning") if e in src_blob} or {"fire", "cold", "lightning"}

    hits: list[str] = []
    for _bnd, iset in iter_enabled_ag_itemsets(ctx):
        for sl in iset.get("slots") or []:
            if not isinstance(sl, dict) or "Swap" in str(sl.get("slotName") or ""):
                continue
            for ln in _active_item_lines(str(sl.get("raw") or "")):
                m_exp = _EXPOSURE_ELEM_RE.search(ln)
                if m_exp and m_exp.group(1).lower() in inv_elems:
                    it_nm = strip_pob_escapes(str(sl.get("title") or sl.get("name") or sl.get("slotName") or ""))
                    hits.append(
                        f"Animate Guardian [{iset.get('title')}] {sl.get('slotName')} ({it_nm}): {strip_pob_escapes(ln)}"
                    )
                    break
    return hits


def _check_animate_guardian_dependencies(
    ctx: dict[str, Any],
    gaps: list[dict[str, str]],
    mechanics: dict[str, Any],
) -> None:
    ag_sets = iter_enabled_ag_itemsets(ctx)
    if not ag_sets:
        return
    ag_entries: list[dict[str, Any]] = []
    for bnd, iset in ag_sets:
        m_id = bnd.get("skillMinionItemSet")
        c_id = bnd.get("skillMinionItemSetCalcs")
        mismatch = m_id is not None and c_id is not None and str(m_id) != str(c_id)
        if mismatch:
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "MEDIUM",
                    "message": (
                        f"Animate Guardian (Group #{bnd.get('group')}) ItemSet mismatch: Main uses "
                        f"ItemSet #{m_id} ({iset.get('title')}) but Calcs uses ItemSet #{c_id}."
                    ),
                }
            )
        found_uniques: list[str] = []
        slot_items: list[dict[str, str]] = []
        for sl in iset.get("slots") or []:
            if not isinstance(sl, dict) or "Swap" in str(sl.get("slotName") or ""):
                continue
            title = strip_pob_escapes(str(sl.get("title") or sl.get("name") or "")).strip()
            slot_items.append({"slot": str(sl.get("slotName") or ""), "name": title})
            t_low = title.lower()
            for key, desc in _AG_KEY_UNIQUES:
                if key in t_low and desc not in found_uniques:
                    found_uniques.append(desc)
        sup_lows = [strip_pob_escapes(str(s)).lower() for s in (bnd.get("supportNames") or [])]
        if any("companionship" in s for s in sup_lows):
            found_uniques.append("Companionship Support (damage-sharing / max-hit dependency)")
        ag_entries.append(
            {
                "group": bnd.get("group"),
                "itemSetId": m_id,
                "itemSetTitle": iset.get("title"),
                "itemSetMismatch": mismatch,
                "keyDependencies": found_uniques,
                "items": slot_items,
            }
        )
        if found_uniques:
            gaps.append(
                {
                    "category": "Mechanic",
                    "severity": "INFO",
                    "message": (
                        f"Animate Guardian (Group #{bnd.get('group')}, ItemSet '{iset.get('title')}') "
                        f"carries critical buffs: {'; '.join(found_uniques)} — AG death or out-of-range "
                        "causes an immediate cliff in DPS/EHP."
                    ),
                }
            )
    mechanics["animateGuardian"] = ag_entries[0] if len(ag_entries) == 1 else ag_entries
