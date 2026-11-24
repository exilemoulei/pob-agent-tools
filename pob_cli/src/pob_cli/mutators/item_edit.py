"""In-place raw item mod editing (`edit_item_mods`)."""

from __future__ import annotations

import re
from typing import Any

from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from ..formatters import strip_pob_escapes
from ..guardrails import GuardrailScope, check_input
from ..snapshot import run_mutation
from .common import _BatchCache, _is_jewel_slot_name, _resolve_slot_from_items

_MOD_TAG_RE = re.compile(r"\{[^}]*\}")
_IMPLICITS_HDR_RE = re.compile(r"^Implicits:\s*(\d+)\s*$", re.IGNORECASE)
_ITEM_TRAILER_RE = re.compile(
    r"^(?:Split|Mirrored|Fractured Item|Corrupted|Unidentified|Synthesised Item|Foil Unique(?:\s*\(.*\))?)$",
    re.IGNORECASE,
)
_ITEM_HEADER_KEY_RE = re.compile(
    r"^(?:Rarity|Armour|Evasion|Energy Shield|Ward|ArmourBasePercentile|EvasionBasePercentile|"
    r"EnergyShieldBasePercentile|WardBasePercentile|Intangibility|Unique ID|League|Unreleased|"
    r"Crafted|Prefix|Suffix|Catalyst|CatalystQuality|Cluster Jewel Skill|Cluster Jewel Node Count|"
    r"Talisman Tier|Item Level|Memory Strands|Version|Selected Version|Variant|Selected Variant|"
    r"Selected Variant Group|Has Alt Variant.*|Selected Alt Variant.*|Allow Duplicate Variants|"
    r"Quality|Sockets|LevelReq|Radius|Limited to|Requires Class|Shaper Item|Elder Item|"
    r"Crusader Item|Redeemer Item|Hunter Item|Warlord Item|Searing Exarch Item|Eater of Worlds Item|"
    r"Synthesised Item)\b",
    re.IGNORECASE,
)


def _mod_line_matches(line: str, pattern: str) -> bool:
    pat = pattern.strip()
    if not pat:
        return False
    clean = strip_pob_escapes(_MOD_TAG_RE.sub("", line)).strip()
    pat_low = pat.lower()
    if pat_low in clean.lower() or pat_low in line.lower():
        return True
    try:
        if re.search(pat, clean, re.IGNORECASE) or re.search(pat, line, re.IGNORECASE):
            return True
    except re.error:
        return False
    return False


def _edit_raw_item_mods(
    raw_text: str,
    *,
    replace_mods: list[tuple[str, str]] | None = None,
    add_mods: list[str] | None = None,
    remove_mods: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Safely edit only the mod lines of a PoB item raw string without mangling headers or trailers."""
    rep_list = list(replace_mods or [])
    add_list = [m.strip() for m in (add_mods or []) if str(m).strip()]
    rem_list = [p.strip() for p in (remove_mods or []) if str(p).strip()]
    if not rep_list and not add_list and not rem_list:
        raise ValueError("Specify at least one of replace_mods, add_mods, or remove_mods.")

    lines = raw_text.splitlines()
    has_implicits_hdr = False
    header_lines: list[str] = []
    implicit_lines: list[str] = []
    after_implicit: list[str] = []

    imp_idx: int | None = None
    imp_count = 0
    for idx, line in enumerate(lines):
        m = _IMPLICITS_HDR_RE.match(line.strip())
        if m:
            imp_idx = idx
            imp_count = int(m.group(1))
            break

    if imp_idx is not None:
        has_implicits_hdr = True
        header_lines = lines[:imp_idx]
        implicit_lines = lines[imp_idx + 1 : imp_idx + 1 + imp_count]
        after_implicit = lines[imp_idx + 1 + imp_count :]
    else:
        sep_indices = [i for i, ln in enumerate(lines) if ln.strip() == "--------"]
        if sep_indices:
            last_sep = sep_indices[-1]
            header_lines = lines[: last_sep + 1]
            after_implicit = lines[last_sep + 1 :]
        else:
            split_at = 0
            if lines and lines[0].strip().lower().startswith("rarity:"):
                rarity_val = lines[0].split(":", 1)[1].strip().upper()
                split_at = 3 if rarity_val in ("RARE", "UNIQUE") else 2
            while split_at < len(lines) and _ITEM_HEADER_KEY_RE.match(lines[split_at].strip()):
                split_at += 1
            header_lines = lines[:split_at]
            after_implicit = lines[split_at:]

    trailer_rev: list[str] = []
    explicit_lines = list(after_implicit)
    while explicit_lines:
        tail = explicit_lines[-1].strip()
        if not tail or _ITEM_TRAILER_RE.match(tail):
            trailer_rev.append(explicit_lines.pop())
        else:
            break
    trailer_lines = list(reversed(trailer_rev))

    changes: list[dict[str, Any]] = []

    for pattern, new_line in rep_list:
        pat = str(pattern).strip()
        repl = str(new_line).strip()
        if not pat:
            raise ValueError("replace_mods pattern cannot be empty.")
        matched = False
        for i, line in enumerate(explicit_lines):
            if _mod_line_matches(line, pat):
                changes.append({"action": "replace", "section": "explicit", "before": line, "after": repl})
                explicit_lines[i] = repl
                matched = True
        if not matched:
            for i, line in enumerate(implicit_lines):
                if _mod_line_matches(line, pat):
                    changes.append({"action": "replace", "section": "implicit", "before": line, "after": repl})
                    implicit_lines[i] = repl
                    matched = True
        if not matched:
            raise ValueError(f"Pattern '{pat}' did not match any mod line on the item.")

    for pat in rem_list:
        kept_explicit: list[str] = []
        removed_any = False
        for line in explicit_lines:
            if _mod_line_matches(line, pat):
                changes.append({"action": "remove", "section": "explicit", "before": line})
                removed_any = True
            else:
                kept_explicit.append(line)
        explicit_lines = kept_explicit

        if not removed_any:
            kept_implicit: list[str] = []
            for line in implicit_lines:
                if _mod_line_matches(line, pat):
                    changes.append({"action": "remove", "section": "implicit", "before": line})
                    removed_any = True
                else:
                    kept_implicit.append(line)
            implicit_lines = kept_implicit

        if not removed_any:
            raise ValueError(f"Pattern '{pat}' did not match any mod line to remove.")

    for new_mod in add_list:
        for sub_line in new_mod.splitlines():
            s = sub_line.strip()
            if s:
                explicit_lines.append(s)
                changes.append({"action": "add", "section": "explicit", "after": s})

    out_lines: list[str] = list(header_lines)
    if has_implicits_hdr:
        out_lines.append(f"Implicits: {len(implicit_lines)}")
    out_lines.extend(implicit_lines)
    out_lines.extend(explicit_lines)
    out_lines.extend(trailer_lines)

    return "\n".join(out_lines), changes


def _apply_edit_item_mods_in_place(
    w: BridgeWorker,
    *,
    slot_name: str,
    replace_mods: list[tuple[str, str]] | None = None,
    add_mods: list[str] | None = None,
    remove_mods: list[str] | None = None,
    cache: _BatchCache | None = None,
) -> dict[str, Any]:
    check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    for _, new_line in replace_mods or []:
        check_input(str(new_line), scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    for new_mod in add_mods or []:
        check_input(str(new_mod), scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    items_res = cache.get_items() if cache is not None else w.call("list_items", {})
    resolved_slot = _resolve_slot_from_items(items_res, slot_name)
    slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
    if not slot_obj or not slot_obj.get("selItemId"):
        raise DaemonRequestError(f"Slot '{resolved_slot}' has no equipped item to edit.")

    item_id = int(slot_obj["selItemId"])
    before_raw = w.call("item_raw", {"id": item_id}).get("raw") or ""
    after_raw, changes = _edit_raw_item_mods(
        before_raw, replace_mods=replace_mods, add_mods=add_mods, remove_mods=remove_mods
    )
    res = w.call("add_item", {"raw": after_raw, "slotName": resolved_slot, "equip": True})
    if cache is not None:
        cache.invalidate("items", "skills")
        if _is_jewel_slot_name(resolved_slot) or slot_obj.get("nodeId"):
            cache.invalidate("tree")
    return {
        "slotName": resolved_slot,
        "item": res.get("item"),
        "beforeRaw": before_raw,
        "afterRaw": after_raw,
        "changes": changes,
    }


def edit_item_mods(
    worker: BridgeWorker,
    *,
    slot_name: str,
    replace_mods: list[tuple[str, str]] | None = None,
    add_mods: list[str] | None = None,
    remove_mods: list[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Edit mod lines on an equipped item in place (replace/add/remove) with 0-residue what-if or --apply."""
    check_input(slot_name, scope=GuardrailScope.SLOT, expected_game="poe1")
    for _, new_line in replace_mods or []:
        check_input(str(new_line), scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    for new_mod in add_mods or []:
        check_input(str(new_mod), scope=GuardrailScope.ITEM_RAW, expected_game="poe1")
    with worker.lock:
        items_res = worker.call("list_items", {})
        resolved_slot = _resolve_slot_from_items(items_res, slot_name)
        slot_obj = next((s for s in items_res.get("slots", []) if s["name"] == resolved_slot), None)
        if not slot_obj or not slot_obj.get("selItemId"):
            raise DaemonRequestError(f"Slot '{resolved_slot}' has no equipped item to edit.")

        item_id = int(slot_obj["selItemId"])
        before_raw = worker.call("item_raw", {"id": item_id}).get("raw") or ""
        after_raw, changes = _edit_raw_item_mods(
            before_raw, replace_mods=replace_mods, add_mods=add_mods, remove_mods=remove_mods
        )

        def _do_edit(w: BridgeWorker) -> dict[str, Any]:
            res = w.call("add_item", {"raw": after_raw, "slotName": resolved_slot, "equip": True})
            return {"slotName": resolved_slot, "item": res.get("item"), "changes": changes}

        mutation_res = run_mutation(worker, _do_edit, apply=apply)
        return {
            "applied": apply,
            "mode": mutation_res["mode"],
            "slotName": resolved_slot,
            "beforeRaw": before_raw,
            "afterRaw": after_raw,
            "changes": changes,
            "mutation": mutation_res,
        }
