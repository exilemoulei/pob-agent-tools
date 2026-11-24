"""Strict PoE1 3.30 Harvest conversion of parsed item text."""

from __future__ import annotations

import re
from typing import Any

from .harvest import (
    ELEMENT_LABELS,
    ELEMENTS,
    HarvestMatch,
    convert_template,
    match_harvest,
)
from .models import NON_CONVERTIBLE_KINDS, Conversion, ConvertedItem, Mod
from .parser import _to_float, parse_item_text


def _rewrite_element(line: str, target: str) -> str:
    return re.sub(
        r"\b(fire|cold|lightning)\b",
        ELEMENT_LABELS.get(target, target.capitalize()),
        line,
        count=1,
        flags=re.IGNORECASE,
    )


def _fmt_roll(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _resolve_tier_info(
    tiers: dict[str, Any],
    template: str,
) -> tuple[str, str]:
    entry = tiers.get(template)
    if isinstance(entry, str):
        return entry, ""
    if isinstance(entry, dict):
        tier_label = str(entry.get("tier") or "")
        lo = _to_float(entry.get("min"))
        hi = _to_float(entry.get("max"))
        if lo is not None and hi is not None:
            return tier_label, f"{_fmt_roll(lo)}-{_fmt_roll(hi)}"
        return tier_label, ""
    return "", ""


def convert_item_text(
    text: str,
    target: str,
    *,
    force: bool = False,
    group: str | None = None,
    tiers: dict[str, Any] | None = None,
) -> ConvertedItem:
    """Rewrite convertible explicit modifiers to ``target`` (``fire``/``cold``/``lightning``).

    Enforces PoE1 3.30 Harvest rules:
    * Refuses unidentified, corrupted, mirrored, and non-MAGIC/RARE items unless ``force=True``.
    * Refuses non-explicit modifiers (implicit, enchant, fractured, crafted,
      scourge, crucible, mutated, veiled, rune).
    * Enforces same-group conflict checks: skips if an explicit/fractured target
      modifier already exists in the same ``(group, subkind)``; warns (but allows
      converting the natural explicit) if a crafted target modifier exists; and
      converts at most ONE modifier per ``(group, subkind)``, choosing the highest
      ``signed_value`` candidate first.
    """
    if target not in ELEMENTS:
        raise ValueError(f"Unsupported conversion target: {target!r}")

    tier_map = tiers or {}
    item = parse_item_text(text)

    if not force:
        blockers: list[str] = []
        if item.unidentified:
            blockers.append("Unidentified items cannot use Harvest conversion.")
        if item.corrupted:
            blockers.append("Corrupted items cannot be modified with Harvest conversion.")
        if item.mirrored:
            blockers.append("Mirrored items cannot be modified with Harvest conversion.")
        if item.rarity not in {"MAGIC", "RARE"}:
            blockers.append(
                f"{item.rarity.title()} items cannot use Harvest conversion (magic/rare only)."
            )
        if blockers:
            return ConvertedItem(text=text, item=item, skipped=blockers)

    existing_locked: set[tuple[str, str, str]] = set()
    existing_crafted: set[tuple[str, str, str]] = set()
    for mod in item.mods:
        found = match_harvest(mod.template)
        if found is None:
            continue
        key = (found.group.key, found.subkind, found.element)
        if mod.kind in {"explicit", "fractured"}:
            existing_locked.add(key)
        elif mod.kind == "crafted":
            existing_crafted.add(key)

    skipped: list[str] = []
    warnings: list[str] = []
    candidates: list[tuple[Mod, HarvestMatch]] = []

    for mod in item.mods:
        found = match_harvest(mod.template)
        if found is None:
            continue
        if group is not None and found.group.key != group:
            continue
        if mod.kind in NON_CONVERTIBLE_KINDS:
            if found.element != target:
                skipped.append(f"{mod.raw}: {mod.kind} modifiers cannot be converted")
            continue
        if found.element == target:
            continue
        candidates.append((mod, found))

    # Choose the strongest tier / highest signed_value candidate first within each group.
    candidates.sort(key=lambda pair: (-pair[0].signed_value, pair[0].line_index))

    lines = list(item.lines)
    conversions: list[Conversion] = []
    converted_groups: set[tuple[str, str]] = set()
    warned_crafted_groups: set[tuple[str, str]] = set()

    for mod, found in candidates:
        group_sub = (found.group.key, found.subkind)
        if (found.group.key, found.subkind, target) in existing_locked:
            skipped.append(
                f"{mod.raw}: item already has target {target} modifier in the same group"
            )
            continue
        if group_sub in converted_groups:
            skipped.append(
                f"{mod.raw}: another higher-value modifier in the same group was already converted to {target}"
            )
            continue

        new_line = _rewrite_element(mod.raw, target)
        if new_line == mod.raw or mod.line_index < 0:
            skipped.append(f"{mod.raw}: could not rewrite the element in place")
            continue

        if (
            (found.group.key, found.subkind, target) in existing_crafted
            and group_sub not in warned_crafted_groups
        ):
            warnings.append(
                "Item has a crafted target modifier; remove it at the crafting bench before Harvest conversion."
            )
            warned_crafted_groups.add(group_sub)

        lines[mod.line_index] = new_line
        converted_groups.add(group_sub)
        tier_label, tier_range = _resolve_tier_info(tier_map, mod.template)
        conversions.append(
            Conversion(
                template=mod.template,
                converted_template=convert_template(mod.template, target) or "",
                element=found.element,
                target=target,
                group=found.group.key,
                subkind=found.subkind,
                values=mod.values,
                cost=found.group.cost,
                patch=found.group.patch,
                line=new_line,
                tier=tier_label,
                tier_range=tier_range,
            )
        )

    converted_text = "\n".join(lines)
    return ConvertedItem(
        text=converted_text,
        item=parse_item_text(converted_text),
        conversions=conversions,
        skipped=skipped,
        warnings=warnings,
    )


def conversion_summary(converted: ConvertedItem) -> str:
    """Short human/agent-facing summary of a Harvest conversion run."""
    if not converted.conversions:
        return "no convertible modifiers found"
    parts: list[str] = []
    for conv in converted.conversions:
        tier_part = ""
        if conv.tier and conv.tier_range:
            tier_part = f" [{conv.tier} {conv.tier_range}]"
        elif conv.tier:
            tier_part = f" [{conv.tier}]"
        parts.append(
            f"{conv.template} -> {conv.converted_template}{tier_part} "
            f"(cost: {conv.cost}; tier kept, roll re-rolled)"
        )
    return "; ".join(parts)
