"""Extract equipped gear, flasks, active jewels, key gems, and anoints from a loaded PoB build."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..trade_item import parse_item_text
from ..trade_query.gem_filters import detect_and_strip_foulborn, strip_foulborn_replaced_mods
from .shopping_specials import (
    ShoppingEntry,
    _classify_jewel,
    _extract_key_gems,
    lookup_anoint_oils,
)

__all__ = [
    "ShoppingEntry",
    "extract_build_shopping_entries",
    "lookup_anoint_oils",
]

_GEAR_SLOTS: tuple[str, ...] = (
    "Weapon 1",
    "Weapon 2",
    "Weapon 1 Swap",
    "Weapon 2 Swap",
    "Helmet",
    "Body Armour",
    "Gloves",
    "Boots",
    "Amulet",
    "Ring 1",
    "Ring 2",
    "Ring 3",
    "Belt",
)
_GEAR_SLOT_SET: frozenset[str] = frozenset(_GEAR_SLOTS)

_RARITY_HEADER_RE = re.compile(r"(?m)^Rarity:\s*", re.IGNORECASE)
_SOCKETS_LINE_RE = re.compile(r"^Sockets:\s*(.+)$", re.IGNORECASE)
_SELECTED_VARIANT_RE = re.compile(r"^Selected(?:\s+Alt)?(?:\s+Two)?\s+Variant:\s*(\d+)\s*$", re.IGNORECASE)
_LINE_VARIANT_TAG_RE = re.compile(r"\{variant:([0-9,]+)\}", re.IGNORECASE)
_ANOINT_LINE_RE = re.compile(r"^Allocates\s+(.+?)$", re.IGNORECASE)
_FLASK_BASE_RE = re.compile(
    r"\b((?:Divine|Eternal|Hallowed|Sanctified|Colossal|Sacred|Giant|Greater|Large|Medium|Small)\s+(?:Life|Mana|Hybrid)\s+Flask|"
    r"(?:Diamond|Ruby|Sapphire|Topaz|Granite|Quicksilver|Amethyst|Quartz|Jade|Basalt|Aquamarine|"
    r"Stibnite|Sulphur|Silver|Bismuth|Gold|Corundum|Iron)\s+Flask)\b",
    re.IGNORECASE,
)


def _filter_active_variant_lines(raw_text: str) -> str:
    """Drop inactive ``{variant:N}`` mod lines when PoB ``Selected Variant: M`` is present."""
    lines = (raw_text or "").splitlines()
    selected: set[int] = set()
    for line in lines:
        m = _SELECTED_VARIANT_RE.match(line.strip())
        if m:
            selected.add(int(m.group(1)))
    if not selected:
        return raw_text
    kept: list[str] = []
    for line in lines:
        m_v = _LINE_VARIANT_TAG_RE.search(line)
        if m_v:
            allowed = {int(tok) for tok in m_v.group(1).split(",") if tok.isdigit()}
            if allowed and not (allowed & selected):
                continue
        kept.append(line)
    return "\n".join(kept)


def _max_links_from_raw(raw_text: str) -> int:
    best = 0
    for line in (raw_text or "").splitlines():
        m = _SOCKETS_LINE_RE.match(line.strip())
        if not m:
            continue
        for grp in m.group(1).strip().split():
            cnt = len([c for c in grp.split("-") if c])
            if cnt > best:
                best = cnt
    return best


def _slot_required_links(skills_res: dict[str, Any] | None) -> dict[str, int]:
    req: dict[str, int] = {}
    if not isinstance(skills_res, dict):
        return req
    for grp in skills_res.get("groups") or []:
        if not isinstance(grp, dict) or grp.get("enabled") is False:
            continue
        slot_nm = str(grp.get("slot") or "").strip()
        if not slot_nm:
            continue
        enabled_gems = [g for g in (grp.get("gems") or []) if isinstance(g, dict) and g.get("enabled", True)]
        if len(enabled_gems) > req.get(slot_nm, 0):
            req[slot_nm] = len(enabled_gems)
    return req


def _clean_flask_base(base_type: str) -> str:
    m = _FLASK_BASE_RE.search(base_type or "")
    return m.group(1) if m else (base_type or "").strip()


def _slot_weapon_set(slot_obj: dict[str, Any], slot_name: str) -> int | None:
    raw_ws = slot_obj.get("weaponSet")
    if raw_ws is not None:
        try:
            return int(raw_ws)
        except (TypeError, ValueError):
            pass
    if slot_name.startswith(("Weapon 1", "Weapon 2")):
        return 2 if "Swap" in slot_name else 1
    return None


def extract_build_shopping_entries(
    items_res: dict[str, Any],
    skills_res: dict[str, Any] | None = None,
    *,
    include_gems: bool = True,
    pob_dir: Path | None = None,
    warnings_out: list[str] | None = None,
) -> list[ShoppingEntry]:
    """Extract all buyable gear, flasks, active jewels, key gems, and anoints from a PoB build."""
    entries: list[ShoppingEntry] = []
    req_links_by_slot = _slot_required_links(skills_res)
    equipped = items_res.get("equippedSlots") if isinstance(items_res, dict) else None
    active_weapon_set = 2 if (isinstance(items_res, dict) and bool(items_res.get("useSecondWeaponSet"))) else 1
    anoint_entry: ShoppingEntry | None = None

    for slot_obj in equipped or ():
        if not isinstance(slot_obj, dict) or slot_obj.get("inactive"):
            continue
        raw_text = str(slot_obj.get("raw") or "").strip()
        if not raw_text:
            continue
        slot_name = str(slot_obj.get("slotName") or "").strip()
        slot_ws = _slot_weapon_set(slot_obj, slot_name)
        if slot_ws is not None and slot_ws != active_weapon_set:
            continue
        node_id = slot_obj.get("nodeId")
        if node_id is not None and slot_obj.get("allocated") is False:
            continue
        if not _RARITY_HEADER_RE.search(raw_text):
            if warnings_out is not None:
                warnings_out.append(
                    f"Skipped {slot_name or 'slot'}: raw item text lacks English 'Rarity:' header "
                    "(localized/non-English item text cannot be safely converted into Trade filters)."
                )
            continue
        try:
            parsed = parse_item_text(_filter_active_variant_lines(raw_text))
        except ValueError:
            if warnings_out is not None:
                warnings_out.append(f"Skipped {slot_name or 'slot'}: could not parse raw item text.")
            continue

        item_meta = slot_obj.get("item") if isinstance(slot_obj.get("item"), dict) else {}
        raw_name = parsed.name or str(item_meta.get("name") or "").strip()
        clean_name, fb_prefix = detect_and_strip_foulborn(raw_name)
        has_mutated_tag = any("mutated" in m.tags for m in parsed.mods)
        is_foulborn = bool(fb_prefix or has_mutated_tag)
        if is_foulborn and clean_name:
            # Preserve `{mutated}` tag from `Mod.tags` so replaced-mod stripping works even
            # when `ModFoulbornMap.lua` is unavailable.
            tagged_lines = [f"{{mutated}}{m.raw}" if "mutated" in m.tags else m.raw for m in parsed.mods]
            _, stripped = strip_foulborn_replaced_mods(clean_name, tagged_lines, pob_dir=pob_dir)
            if stripped:
                stripped_set = {s.removeprefix("{mutated}") for s in stripped}
                parsed.mods = [m for m in parsed.mods if ("mutated" in m.tags) or (m.raw not in stripped_set)]

        if slot_obj.get("isFlask") or slot_name.startswith("Flask "):
            is_uniq = parsed.rarity in ("UNIQUE", "RELIC")
            base_clean = parsed.base_type if is_uniq else _clean_flask_base(parsed.base_type)
            entries.append(
                ShoppingEntry(
                    section="flask",
                    slot=slot_name,
                    kind="unique" if is_uniq else "magic_flask",
                    name=(clean_name or base_clean) if is_uniq else base_clean,
                    base_type=base_clean,
                    rarity=parsed.rarity,
                    corrupted=parsed.corrupted,
                    foulborn=is_foulborn,
                    raw_text=raw_text,
                    parsed=parsed,
                )
            )
            continue

        is_jewel_slot = (
            node_id is not None
            or slot_name.startswith("Jewel ")
            or "Abyssal Socket" in slot_name
        )
        if is_jewel_slot:
            sock_idx = slot_obj.get("socketIndex")
            j_label = f"Jewel #{sock_idx}" if sock_idx is not None else slot_name
            entries.append(_classify_jewel(parsed, clean_name or "", j_label, raw_text, is_foulborn))
            continue

        if slot_name not in _GEAR_SLOT_SET:
            continue

        raw_links = _max_links_from_raw(raw_text)
        skill_links = req_links_by_slot.get(slot_name, 0)
        eff_links = max(raw_links, skill_links) if max(raw_links, skill_links) >= 5 else 0
        is_uniq = parsed.rarity in ("UNIQUE", "RELIC")

        if slot_name == "Amulet":
            for mod in parsed.mods:
                if mod.kind == "enchant":
                    m_an = _ANOINT_LINE_RE.match(mod.raw)
                    if m_an:
                        notable = m_an.group(1).strip()
                        oils = lookup_anoint_oils(notable, pob_dir=pob_dir)
                        anoint_entry = ShoppingEntry(
                            section="anoint",
                            slot="Amulet Anoint",
                            kind="anoint",
                            name=notable,
                            rarity="NORMAL",
                            anoint_notable=notable,
                            anoint_oils=oils,
                        )
                        break

        entries.append(
            ShoppingEntry(
                section="gear",
                slot=slot_name,
                kind="unique" if is_uniq else "rare",
                name=(clean_name or parsed.base_type) if is_uniq else parsed.base_type,
                base_type=parsed.base_type,
                rarity=parsed.rarity,
                links=eff_links,
                corrupted=parsed.corrupted,
                foulborn=is_foulborn,
                raw_text=raw_text,
                parsed=parsed,
            )
        )

    if include_gems:
        entries.extend(_extract_key_gems(skills_res))
    if anoint_entry is not None:
        entries.append(anoint_entry)
    return entries
