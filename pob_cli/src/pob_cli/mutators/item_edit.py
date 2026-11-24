"""In-place raw item mod editing (`edit_item_mods`)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import item_inspect
from ..bridge_client import BridgeWorker
from ..errors import DaemonRequestError
from ..formatters import strip_pob_escapes
from ..guardrails import GuardrailScope, check_input
from ..snapshot import run_mutation
from ..trade_item import parse_item_text
from .common import _BatchCache, _is_jewel_slot_name, _resolve_slot_from_items

_MOD_TAG_RE = re.compile(r"\{[^}]*\}")
_IMPLICITS_HDR_RE = re.compile(r"^Implicits:[ \t]*(\d+)\s*$", re.IGNORECASE)
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


def _clean_mod_line(line: str) -> str:
    return strip_pob_escapes(_MOD_TAG_RE.sub("", line)).strip()


def _line_affix_label(line: str, report: dict[str, Any], global_types: dict[str, set[str]]) -> str:
    clean = _clean_mod_line(line)
    matched = [a for a in report.get("affixes", []) if clean in (a.get("lines") or [])]
    if matched:
        parts: list[str] = []
        for a in matched:
            tag = f"{a['type']}:{a['group']}"
            if tag not in parts:
                parts.append(tag)
        return ", ".join(parts)
    gtypes = sorted(global_types.get(clean, ()))
    if len(gtypes) == 1:
        return f"{gtypes[0]} (global mod pool)"
    if len(gtypes) > 1:
        return "prefix/suffix (ambiguous)"
    return "unknown"


def _can_rebalance_ambiguous(affixes: list[dict[str, Any]], counts: dict[str, int], cap: dict[str, int]) -> bool:
    """Return True if reassigning genuinely prefix/suffix-ambiguous affixes resolves the overflow."""
    p_cnt, s_cnt = counts["prefix"], counts["suffix"]
    p_cap, s_cap = cap["prefix"], cap["suffix"]
    if p_cnt > p_cap and s_cnt < s_cap:
        shiftable = sum(
            1 for a in affixes
            if a.get("type") == "prefix" and any(str(alt).startswith("suffix:") for alt in a.get("alternatives") or ())
        )
        need = p_cnt - p_cap
        if shiftable >= need and s_cnt + need <= s_cap:
            return True
    if s_cnt > s_cap and p_cnt < p_cap:
        shiftable = sum(
            1 for a in affixes
            if a.get("type") == "suffix" and any(str(alt).startswith("prefix:") for alt in a.get("alternatives") or ())
        )
        need = s_cnt - s_cap
        if shiftable >= need and p_cnt + need <= p_cap:
            return True
    return False


def _validate_item_edit_affixes(
    before_raw: str,
    after_raw: str,
    *,
    slot_name: str,
    changes: list[dict[str, Any]],
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> None:
    """Enforce PoE1 Rare affix capacity (3 prefixes / 3 suffixes; 2P/2S on jewels) and crafted mod limits."""
    touched_add_or_rep = [
        c for c in changes if c.get("section") == "explicit" and c.get("action") in ("add", "replace")
    ]
    if not touched_add_or_rep:
        return
    after_item = parse_item_text(after_raw)
    if after_item.rarity != "RARE" or after_item.unidentified:
        return

    has_add = any(c.get("action") == "add" for c in touched_add_or_rep)
    added_crafted = any(
        "{crafted}" in str(c.get("after") or "").lower() or "(crafted)" in str(c.get("after") or "").lower()
        for c in touched_add_or_rep
    )
    before_multimod = "can have up to 3 crafted modifiers" in before_raw.lower()
    item_label = f"'{slot_name}' [{after_item.display_name}]"
    has_multimod = any("can have up to 3 crafted modifiers" in m.raw.lower() for m in after_item.mods)
    max_crafted = 3 if has_multimod else 1
    check_crafted = added_crafted or (before_multimod and not has_multimod)

    try:
        after_rep = item_inspect.inspect_item(
            after_raw, slot=slot_name, pob_dir=pob_dir, runtime_dir=runtime_dir, include_suggestions=False
        )
    except (FileNotFoundError, RuntimeError):
        crafted_lines = [m.raw for m in after_item.mods if m.kind == "crafted"]
        if check_crafted and len(crafted_lines) > max_crafted:
            raise DaemonRequestError(
                f"Cannot edit Rare item on {item_label}: {len(crafted_lines)} crafted modifiers exceed limit ({max_crafted}). "
                "Remove or replace the existing {crafted} modifier first."
            ) from None
        return

    cap = after_rep["capacity"]
    after_affixes = after_rep["affixes"]
    after_unmatched = list(after_rep["unmatched"])

    global_types: dict[str, set[str]] = {}
    if after_unmatched:
        try:
            global_types = item_inspect.classify_mod_affix_types(
                after_unmatched, pob_dir=pob_dir, runtime_dir=runtime_dir
            )
        except (FileNotFoundError, RuntimeError):
            global_types = {}

    eff_counts = dict(after_rep["counts"])
    still_unmatched: list[str] = []
    for line in after_unmatched:
        sides = global_types.get(line, set())
        if len(sides) == 1:
            eff_counts[next(iter(sides))] += 1
        else:
            still_unmatched.append(line)

    p_cnt, s_cnt = eff_counts["prefix"], eff_counts["suffix"]
    p_cap, s_cap = cap["prefix"], cap["suffix"]
    total_after = p_cnt + s_cnt + len(still_unmatched)
    may_overflow = (p_cnt > p_cap) or (s_cnt > s_cap) or (total_after > p_cap + s_cap)

    before_rep: dict[str, Any] = {"affixes": [], "counts": {"prefix": 0, "suffix": 0}, "unmatched": []}
    before_cnt = {"prefix": 0, "suffix": 0}
    if may_overflow:
        try:
            before_rep = item_inspect.inspect_item(
                before_raw, slot=slot_name, pob_dir=pob_dir, runtime_dir=runtime_dir, include_suggestions=False
            )
            before_cnt = dict(before_rep["counts"])
            if before_rep["unmatched"]:
                b_global = item_inspect.classify_mod_affix_types(
                    list(before_rep["unmatched"]), pob_dir=pob_dir, runtime_dir=runtime_dir
                )
                for line in before_rep["unmatched"]:
                    sides = b_global.get(line, set())
                    if len(sides) == 1:
                        before_cnt[next(iter(sides))] += 1
        except (FileNotFoundError, RuntimeError):
            pass

    p_over = p_cnt > p_cap and (has_add or p_cnt > before_cnt["prefix"])
    s_over = s_cnt > s_cap and (has_add or s_cnt > before_cnt["suffix"])
    overflow_side = p_over or s_over
    if overflow_side and _can_rebalance_ambiguous(after_affixes, eff_counts, cap):
        overflow_side = False
    total_overflow = total_after > p_cap + s_cap and (has_add or total_after > sum(before_cnt.values()))

    if overflow_side or total_overflow:
        removed_lines = [
            _clean_mod_line(str(c.get("before") or ""))
            for c in changes
            if c.get("section") == "explicit" and c.get("action") in ("remove", "replace")
        ]
        added_lines = [_clean_mod_line(str(c.get("after") or "")) for c in touched_add_or_rep]
        if s_over:
            cur_s = f"{before_cnt['suffix']}/{s_cap} suffixes"
            head = (
                f"Cannot edit Rare item on {item_label}: item already has {cur_s} "
                f"(post-edit would be {p_cnt}/{p_cap} prefixes, {s_cnt}/{s_cap} suffixes)."
            )
            target_side = "suffix"
        elif p_over:
            cur_p = f"{before_cnt['prefix']}/{p_cap} prefixes"
            head = (
                f"Cannot edit Rare item on {item_label}: item already has {cur_p} "
                f"(post-edit would be {p_cnt}/{p_cap} prefixes, {s_cnt}/{s_cap} suffixes)."
            )
            target_side = "prefix"
        else:
            head = (
                f"Cannot edit Rare item on {item_label}: item already has "
                f"{before_cnt['prefix']}/{p_cap} prefixes and {before_cnt['suffix']}/{s_cap} suffixes "
                f"({total_after}/{p_cap + s_cap} explicit modifiers post-edit)."
            )
            target_side = "affix"
        detail_lines = [
            head,
            f"- Before edit: {before_cnt['prefix']}/{p_cap} prefixes, {before_cnt['suffix']}/{s_cap} suffixes.",
            "- PoE1 rule: Prefix and Suffix pools are independent (removing a prefix never frees a suffix slot, and vice versa).",
        ]
        if removed_lines:
            rem_desc = "; ".join(f"'{ln}' [{_line_affix_label(ln, before_rep, global_types)}]" for ln in removed_lines)
            detail_lines.append(f"- Removed: {rem_desc}")
        if added_lines:
            add_desc = "; ".join(f"'{ln}' [{_line_affix_label(ln, after_rep, global_types)}]" for ln in added_lines)
            detail_lines.append(f"- Added: {add_desc}")
        detail_lines.append(
            f"- Remediation: Remove or replace an existing {target_side} first "
            f"(inspect open affixes via `pob item inspect --slot \"{slot_name}\"`)."
        )
        raise DaemonRequestError("\n".join(detail_lines))

    crafted_affix_cnt = sum(1 for a in after_affixes if a.get("source") == "Bench")
    unmatched_crafted = [m.raw for m in after_item.mods if m.kind == "crafted" and m.raw in after_unmatched]
    if check_crafted and crafted_affix_cnt + len(unmatched_crafted) > max_crafted:
        raise DaemonRequestError(
            f"Cannot edit Rare item on {item_label}: {crafted_affix_cnt + len(unmatched_crafted)} crafted modifiers "
            f"exceed limit ({max_crafted}). Remove or replace the existing {{crafted}} modifier first "
            f"(inspect via `pob item inspect --slot \"{slot_name}\"`)."
        )


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
    _validate_item_edit_affixes(
        before_raw,
        after_raw,
        slot_name=resolved_slot,
        changes=changes,
        pob_dir=getattr(w, "pob_root", None) or getattr(w, "pob_dir", None),
        runtime_dir=getattr(w, "runtime_dir", None),
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
    allow_illegal_affixes: bool = False,
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
        if not allow_illegal_affixes:
            _validate_item_edit_affixes(
                before_raw,
                after_raw,
                slot_name=resolved_slot,
                changes=changes,
                pob_dir=getattr(worker, "pob_root", None) or getattr(worker, "pob_dir", None),
                runtime_dir=getattr(worker, "runtime_dir", None),
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
