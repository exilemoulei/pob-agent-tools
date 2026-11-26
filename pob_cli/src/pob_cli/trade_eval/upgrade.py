"""`pob trade upgrade`: pick the gear slots worth searching first, from PoB-verified unequip probes."""

from __future__ import annotations

from typing import Any

from ..scoring import pct_change, stat_value, weighted_score

# PoB slot name -> `pob trade search --slot` name.
UPGRADE_SLOTS: dict[str, str] = {
    "Weapon 1": "weapon1",
    "Body Armour": "body_armour",
    "Helmet": "helmet",
    "Gloves": "gloves",
    "Boots": "boots",
    "Amulet": "amulet",
    "Ring 1": "ring1",
    "Ring 2": "ring2",
    "Belt": "belt",
}
_STAT_FOR = {"dps": "primaryDps", "ehp": "totalEhp"}


def unequip_loss(res: dict[str, Any], score: dict[str, float]) -> float | None:
    """Score lost when the slot is emptied (positive = the item contributes); None if unverified."""
    before, after = res.get("before") or {}, res.get("after") or {}
    pcts: dict[str, float | None] = {}
    for key in score:
        stat = _STAT_FOR.get(key, key)
        b, a = stat_value(before, stat), stat_value(after, stat)
        pcts[key] = pct_change(b, None if b is None or a is None else a - b)
    gain = weighted_score(pcts, score)
    return None if gain is None else -gain


def rank_weak_slots(items_res: dict[str, Any], losses: dict[str, float | None], top: int) -> list[dict[str, Any]]:
    """Empty slots first, then the lowest-contribution slots; unverified losses last."""
    empty: list[dict[str, Any]] = []
    filled: list[dict[str, Any]] = []
    # The daemon's `items` RPC (inspect_items) shape: equippedSlots[].slotName / itemId.
    for s in items_res.get("equippedSlots") or []:
        name = s.get("slotName")
        if name not in UPGRADE_SLOTS or s.get("inactive"):
            continue
        row = {"slot": name, "tradeSlot": UPGRADE_SLOTS[name]}
        if not s.get("itemId"):
            empty.append({**row, "reason": "empty", "loss": None})
        else:
            filled.append({**row, "reason": "lowest contribution", "loss": losses.get(name)})
    filled.sort(key=lambda r: (r["loss"] is None, r["loss"] or 0.0))
    return (empty + filled)[: max(0, top)]
