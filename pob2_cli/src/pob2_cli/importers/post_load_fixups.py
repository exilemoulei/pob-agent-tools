# SPDX-License-Identifier: GPL-3.0-only
"""Post-load PoE2 auto-fixups (#2401 false-negative Spirit, #2536 main skill / statSet, Notes strip) (L1)."""

from __future__ import annotations

import re
from typing import Any

_POB_COLOR_RE = re.compile(r"\^(?:x[0-9A-Fa-f]{6}|[0-9])")


def strip_pob_color_codes(text: str | None) -> str:
    """Strip PoB colour escape codes (`^xRRGGBB`, `^0`..`^9`) while preserving line breaks."""
    if not isinstance(text, str) or not text:
        return ""
    cleaned = _POB_COLOR_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return cleaned.strip()


def _detect_and_fix_2401_spirit(worker: Any, *, auto_fix: bool) -> tuple[list[dict[str, Any]], list[str]]:
    """Detect (#2401) false-negative Spirit caused by inactive weapon-set aura/persistent groups."""
    fixups: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        stats = (worker.call("get_stats", {}) or {}).get("stats") or {}
        ws_state = worker.call("get_weapon_set_state", {}) or {}
        skills = worker.call("list_skills", {}) or {}
    except Exception:
        return fixups, warnings

    spirit_total = float(stats.get("Spirit") or 0)
    spirit_unres = float(stats.get("SpiritUnreserved") if stats.get("SpiritUnreserved") is not None else spirit_total)
    if spirit_unres >= 0:
        return fixups, warnings

    active_ws = 2 if ws_state.get("useSecondWeaponSet") else 1
    ws_groups = ws_state.get("skillWeaponSets") or []
    groups = skills.get("groups") or []
    off_set_indices: list[dict[str, Any]] = []

    for idx, g_ws in enumerate(ws_groups, 1):
        if not isinstance(g_ws, dict) or not g_ws.get("enabled"):
            continue
        grp_info = groups[idx - 1] if idx - 1 < len(groups) else {}
        slot_name = str(grp_info.get("slot") or "")
        set1 = g_ws.get("set1")
        set2 = g_ws.get("set2")
        eg1 = g_ws.get("gem1EnableGlobal1")
        eg2 = g_ws.get("gem1EnableGlobal2")

        is_off_slot = (active_ws == 1 and "Swap" in slot_name) or (
            active_ws == 2 and slot_name in ("Weapon 1", "Weapon 2")
        )
        is_off_binding = (
            (active_ws == 1 and set1 is False and set2 is True)
            or (active_ws == 2 and set2 is False and set1 is True)
            or (active_ws == 1 and eg1 is False and eg2 is True)
            or (active_ws == 2 and eg2 is False and eg1 is True)
        )
        if is_off_slot or is_off_binding:
            off_set_indices.append(
                {
                    "index": idx,
                    "label": str(g_ws.get("label") or grp_info.get("label") or f"Group #{idx}"),
                    "slot": slot_name,
                    "reason": "off_weapon_set_slot" if is_off_slot else "off_weapon_set_binding",
                }
            )

    if not off_set_indices:
        return fixups, warnings

    disabled_groups: list[dict[str, Any]] = []
    after_unres = spirit_unres
    if auto_fix:
        for cand in off_set_indices:
            try:
                worker.call("set_group", {"index": cand["index"], "enabled": False})
                disabled_groups.append(cand)
            except Exception:
                continue
        if disabled_groups:
            after_stats = (worker.call("get_stats", {}) or {}).get("stats") or {}
            after_unres = float(after_stats.get("SpiritUnreserved") or after_unres)

    fixups.append(
        {
            "issue": "#2401",
            "kind": "false_negative_spirit_offset_aura",
            "activeWeaponSet": active_ws,
            "spiritUnreservedBefore": spirit_unres,
            "spiritUnreservedAfter": after_unres,
            "detectedGroups": off_set_indices,
            "autoFixed": bool(disabled_groups),
            "disabledGroups": disabled_groups,
        }
    )
    if disabled_groups:
        warnings.append(
            f"[#2401] Disabled {len(disabled_groups)} inactive weapon-set aura/persistent group(s) "
            f"(SpiritUnreserved {spirit_unres:.0f} -> {after_unres:.0f})."
        )
    else:
        warnings.append(
            f"[#2401] Detected {len(off_set_indices)} inactive weapon-set aura/persistent group(s) "
            f"causing negative SpiritUnreserved ({spirit_unres:.0f})."
        )
    return fixups, warnings


def _detect_and_fix_2536_main_skill(worker: Any, *, auto_fix: bool) -> tuple[list[dict[str, Any]], list[str]]:
    """Detect (#2536) 0-DPS main skill group or wrong statSetIndex (e.g. aura group #1 or Tempest Bell statSets[1])."""
    fixups: list[dict[str, Any]] = []
    warnings: list[str] = []
    caps = getattr(worker, "caps", None) or {}
    if not caps.get("deepAuditProbe"):
        return fixups, warnings
    try:
        res = worker.call("select_best_main_skill", {"apply": bool(auto_fix)}) or {}
    except Exception:
        return fixups, warnings
    if res.get("changed") or res.get("recommended"):
        fixups.append(
            {
                "issue": "#2536",
                "kind": "smart_main_skill_stat_set",
                "applied": bool(res.get("applied")),
                "beforeGroup": res.get("beforeGroup"),
                "afterGroup": res.get("afterGroup"),
                "beforeStatSet": res.get("beforeStatSet"),
                "afterStatSet": res.get("afterStatSet"),
                "skillName": res.get("skillName"),
                "statSetLabel": res.get("statSetLabel"),
                "beforeDps": res.get("beforeDps", 0),
                "afterDps": res.get("afterDps", 0),
            }
        )
        warnings.append(
            f"[#2536] Main skill calibrated to group #{res.get('afterGroup')} "
            f"`{res.get('skillName')}` (statSet={res.get('afterStatSet')}, "
            f"DPS {res.get('beforeDps', 0):.0f} -> {res.get('afterDps', 0):.0f})."
        )
    return fixups, warnings


def fetch_clean_notes(worker: Any) -> dict[str, Any]:
    """Read author Notes from PoB2 and strip PoB colour codes (`^xRRGGBB`, `^0..^9`)."""
    try:
        res = worker.call("get_notes", {}) or {}
    except Exception:
        return {"text": "", "rawLength": 0, "hasNotes": False}
    raw_text = str(res.get("text") or "")
    clean = strip_pob_color_codes(raw_text)
    lines = [ln for ln in clean.splitlines() if ln.strip()]
    return {
        "text": clean,
        "preview": "\n".join(lines[:12]),
        "lineCount": len(lines),
        "rawLength": len(raw_text),
        "hasNotes": bool(clean),
    }


def apply_post_load_fixups(worker: Any, *, auto_fix: bool = True) -> dict[str, Any]:
    """Run #2401 Spirit check, #2536 main-skill/statSet calibration, and clean Notes extraction."""
    fixups: list[dict[str, Any]] = []
    warnings: list[str] = []

    f1, w1 = _detect_and_fix_2401_spirit(worker, auto_fix=auto_fix)
    fixups.extend(f1)
    warnings.extend(w1)

    f2, w2 = _detect_and_fix_2536_main_skill(worker, auto_fix=auto_fix)
    fixups.extend(f2)
    warnings.extend(w2)

    notes = fetch_clean_notes(worker)
    return {"fixups": fixups, "warnings": warnings, "notes": notes}


__all__ = [
    "apply_post_load_fixups",
    "fetch_clean_notes",
    "strip_pob_color_codes",
]
