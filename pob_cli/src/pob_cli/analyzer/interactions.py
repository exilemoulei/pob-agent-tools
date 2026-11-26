"""ModParser mechanics interaction analyzer and PoB-faithful flag compatibility classifier."""

from __future__ import annotations

from typing import Any

from ..bridge_client import BridgeWorker
from ..bridge_protocol import get_worker_caps
from ..errors import DaemonRequestError
from .inspectors import resolve_slot_name
from .interactions_rules import (
    CONDITION_TAG_TYPES,
    evaluate_mod_against_skill,
    unwrap_nested_mod,
)
from .interactions_rules import (
    KEYWORD_FLAG_BITS as KEYWORD_FLAG_BITS,
)
from .interactions_rules import (
    KEYWORD_FLAG_MATCH_ALL as KEYWORD_FLAG_MATCH_ALL,
)
from .interactions_rules import (
    MOD_FLAG_BITS as MOD_FLAG_BITS,
)
from .interactions_rules import (
    decode_bitmask as decode_bitmask,
)
from .interactions_rules import (
    format_tag_premise as format_tag_premise,
)
from .interactions_rules import (
    keyword_flags_match as keyword_flags_match,
)
from .interactions_rules import (
    mod_flags_match as mod_flags_match,
)


def classify_mod_entry(entry: dict[str, Any], main_skill: dict[str, Any]) -> dict[str, Any]:
    """Classify a single parsed mod line against the active main skill using PoB ModDB/Global semantics."""
    raw_line = str(entry.get("rawLine") or "")
    if entry.get("unsupported"):
        return {
            "rawLine": raw_line,
            "section": entry.get("section"),
            "source": entry.get("source"),
            "verdict": "UNSUPPORTED_BY_POB",
            "unsupported": True,
            "extra": entry.get("extra") or raw_line,
            "mismatchedFlags": [],
            "premises": [],
            "mods": [],
        }

    mods = [m for m in (entry.get("mods") or []) if isinstance(m, dict)]
    if not mods:
        return {
            "rawLine": raw_line,
            "section": entry.get("section"),
            "source": entry.get("source"),
            "verdict": "DEFENCE_OR_GLOBAL",
            "unsupported": False,
            "mismatchedFlags": [],
            "premises": [],
            "mods": [],
        }

    sub_verdicts: list[str] = []
    all_mismatches: list[str] = []
    all_premises: list[str] = []
    enriched_mods: list[dict[str, Any]] = []

    for m in mods:
        mismatches, conv_notes, offensive = evaluate_mod_against_skill(m, main_skill)
        unwrapped, _is_min, _outer = unwrap_nested_mod(m)
        cond_premises = [
            format_tag_premise(t)
            for t in (unwrapped.get("tags") or [])
            if isinstance(t, dict) and str(t.get("type") or "") in CONDITION_TAG_TYPES
        ]
        premises = [*cond_premises, *conv_notes]

        if mismatches:
            sv = "FLAG_MISMATCH"
        elif cond_premises:
            sv = "CONDITIONAL_PREMISE"
        elif offensive:
            sv = "SCALES_MAIN_SKILL"
        else:
            sv = "DEFENCE_OR_GLOBAL"

        sub_verdicts.append(sv)
        for r in mismatches:
            if r not in all_mismatches:
                all_mismatches.append(r)
        for p in premises:
            if p not in all_premises:
                all_premises.append(p)
        enriched_mods.append({**m, "verdict": sv, "mismatchedFlags": mismatches, "premises": premises})

    # Line-level verdict: if any sub-mod directly scales main skill (e.g. "Attack and Cast Speed" on Spell),
    # do not falsely condemn the whole line as FLAG_MISMATCH.
    if "SCALES_MAIN_SKILL" in sub_verdicts:
        verdict = "SCALES_MAIN_SKILL"
    elif "CONDITIONAL_PREMISE" in sub_verdicts:
        verdict = "CONDITIONAL_PREMISE"
    elif "DEFENCE_OR_GLOBAL" in sub_verdicts:
        verdict = "DEFENCE_OR_GLOBAL"
    elif "FLAG_MISMATCH" in sub_verdicts:
        verdict = "FLAG_MISMATCH"
    else:
        verdict = "DEFENCE_OR_GLOBAL"

    return {
        "rawLine": raw_line,
        "section": entry.get("section"),
        "source": entry.get("source"),
        "verdict": verdict,
        "unsupported": False,
        "mismatchedFlags": all_mismatches if verdict == "FLAG_MISMATCH" else [],
        "partialMismatches": all_mismatches if (verdict != "FLAG_MISMATCH" and all_mismatches) else [],
        "premises": all_premises,
        "mods": enriched_mods,
    }


def _collect_keystone_and_override_summary(probe: dict[str, Any]) -> dict[str, Any]:
    keystones = [
        {"name": ks.get("name"), "stats": ks.get("stats") or [], "modCount": len(ks.get("mods") or [])}
        for ks in (probe.get("keystones") or [])
        if isinstance(ks, dict) and ks.get("name")
    ]
    overrides: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for s in probe.get("equippedSlots") or []:
        if not isinstance(s, dict) or not s.get("active", True):
            continue
        slot_nm = str(s.get("slot") or "")
        item_nm = str(s.get("itemName") or "")
        for ln in s.get("lines") or []:
            if not isinstance(ln, dict) or ln.get("unsupported"):
                continue
            for m in ln.get("mods") or []:
                if not isinstance(m, dict) or m.get("type") not in ("FLAG", "OVERRIDE"):
                    continue
                key = (slot_nm, str(m.get("name") or ""), str(m.get("type") or ""))
                if key in seen:
                    continue
                seen.add(key)
                overrides.append(
                    {
                        "slot": slot_nm,
                        "itemName": item_nm,
                        "rawLine": ln.get("rawLine"),
                        "modName": m.get("name"),
                        "modType": m.get("type"),
                        "value": m.get("value"),
                    }
                )
    return {"keystones": keystones, "overrides": overrides}


def inspect_interactions(
    worker: BridgeWorker,
    *,
    slot_name: str | None = None,
    mod_lines: list[str] | None = None,
    unsupported_only: bool = False,
) -> dict[str, Any]:
    """Analyze ModParser mechanics, flag compatibility, and red uncomputed mods on the loaded build."""
    caps = get_worker_caps(worker)
    if not caps.get("interactionsProbe"):
        raise DaemonRequestError(
            "Mechanics interactions inspection (`pob interactions`) requires engine capability "
            "`interactionsProbe` (PoB 2.69+ / updated headless runner). Run `pob update` to upgrade."
        )

    cleaned_mods = [str(m).strip() for m in (mod_lines or []) if str(m).strip()]
    resolved_slot: str | None = None
    if slot_name:
        resolved_slot = resolve_slot_name(worker, slot_name)

    # Fast path: build-wide `--unsupported-only` calls `scan_unsupported_mods` directly.
    if unsupported_only and not resolved_slot and not cleaned_mods:
        scan = worker.call("scan_unsupported_mods", {})
        return {
            "mode": "unsupported_only",
            "unsupportedMods": {
                "items": list(scan.get("items") or []),
                "customMods": list(scan.get("customMods") or []),
                "totalCount": int(scan.get("totalCount") or 0),
            },
            "elapsedMs": scan.get("elapsedMs"),
        }

    rpc_params: dict[str, Any] = {}
    if resolved_slot:
        rpc_params["slot"] = resolved_slot
    if cleaned_mods:
        rpc_params["lines"] = cleaned_mods

    probe = worker.call("inspect_mod_interactions", rpc_params)
    main_skill = probe.get("mainSkill") or {}

    if resolved_slot or cleaned_mods:
        classified_lines = [classify_mod_entry(e, main_skill) for e in (probe.get("lines") or [])]
        classified_slot = [classify_mod_entry(e, main_skill) for e in (probe.get("slotLines") or [])]
        if unsupported_only:
            classified_lines = [c for c in classified_lines if c["verdict"] == "UNSUPPORTED_BY_POB"]
            classified_slot = [c for c in classified_slot if c["verdict"] == "UNSUPPORTED_BY_POB"]
        all_rows = [*classified_slot, *classified_lines]
        verdict_counts: dict[str, int] = {}
        for row in all_rows:
            v = row["verdict"]
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
        out: dict[str, Any] = {
            "mode": "targeted",
            "unsupportedOnly": unsupported_only,
            "mainSkill": main_skill,
            "verdictCounts": verdict_counts,
            "elapsedMs": probe.get("elapsedMs"),
        }
        if resolved_slot:
            out["slot"] = probe.get("slot") or resolved_slot
            out["itemName"] = probe.get("itemName") or ""
            out["baseName"] = probe.get("baseName") or ""
            out["slotLines"] = classified_slot
        if cleaned_mods:
            out["lines"] = classified_lines
        return out

    # Build-wide mode: scan all active equipped slots + customMods for UNSUPPORTED_BY_POB & FLAG_MISMATCH
    unsupported_items: list[dict[str, Any]] = []
    unsupported_custom: list[dict[str, Any]] = []
    flag_mismatches: list[dict[str, Any]] = []

    for s in probe.get("equippedSlots") or []:
        if not isinstance(s, dict):
            continue
        sn = str(s.get("slot") or "")
        inm = str(s.get("itemName") or "")
        active = bool(s.get("active", True))
        for raw_entry in s.get("lines") or []:
            if not isinstance(raw_entry, dict):
                continue
            cls = classify_mod_entry(raw_entry, main_skill)
            if cls["verdict"] == "UNSUPPORTED_BY_POB":
                unsupported_items.append(
                    {
                        "slot": sn,
                        "itemName": inm,
                        "section": raw_entry.get("section"),
                        "rawLine": cls["rawLine"],
                        "extra": cls.get("extra"),
                        "active": active,
                    }
                )
            elif active and cls["verdict"] == "FLAG_MISMATCH":
                flag_mismatches.append(
                    {
                        "slot": sn,
                        "itemName": inm,
                        "section": raw_entry.get("section"),
                        "rawLine": cls["rawLine"],
                        "mismatchedFlags": cls["mismatchedFlags"],
                    }
                )

    for cm in probe.get("customModLines") or []:
        if not isinstance(cm, dict):
            continue
        cls = classify_mod_entry(cm, main_skill)
        if cls["verdict"] == "UNSUPPORTED_BY_POB":
            unsupported_custom.append(
                {
                    "slot": "ConfigTab",
                    "blockTitle": cm.get("blockTitle") or "Default",
                    "rawLine": cls["rawLine"],
                    "extra": cls.get("extra"),
                }
            )
        elif cls["verdict"] == "FLAG_MISMATCH":
            flag_mismatches.append(
                {
                    "slot": "ConfigTab",
                    "itemName": f"Custom Modifiers ({cm.get('blockTitle') or 'Default'})",
                    "section": "customMods",
                    "rawLine": cls["rawLine"],
                    "mismatchedFlags": cls["mismatchedFlags"],
                }
            )

    ks_ov = _collect_keystone_and_override_summary(probe)
    return {
        "mode": "build_wide",
        "mainSkill": main_skill,
        "unsupportedMods": {
            "items": unsupported_items,
            "customMods": unsupported_custom,
            "totalCount": len(unsupported_items) + len(unsupported_custom),
        },
        "flagMismatches": flag_mismatches,
        "keystones": ks_ov["keystones"],
        "overrides": ks_ov["overrides"],
        "elapsedMs": probe.get("elapsedMs"),
    }
