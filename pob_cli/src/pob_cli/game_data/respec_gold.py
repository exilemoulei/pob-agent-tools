"""PoE1 3.30 Gold passive-tree respec cost lookup (from PoB `Data/Misc.lua` & `Classes/TreeTab.lua`)."""

from __future__ import annotations

from typing import Any

# From PoB `PoE1/Data/Misc.lua` (`data.goldRespecPrices`, VillageBalancePerLevelShared.dat), levels 1..100.
GOLD_RESPEC_PRICES: tuple[int, ...] = (
    4, 4, 4, 5, 5, 6, 6, 7, 8, 8,
    9, 11, 12, 14, 15, 17, 18, 20, 22, 24,
    26, 28, 31, 34, 36, 39, 43, 46, 50, 54,
    58, 62, 67, 72, 83, 90, 97, 105, 113, 121,
    130, 151, 161, 171, 182, 209, 222, 237, 252, 267,
    295, 313, 333, 354, 376, 427, 452, 482, 513, 546,
    618, 656, 694, 733, 773, 816, 860, 892, 921, 1055,
    1203, 1365, 1541, 1748, 1974, 2221, 2490, 2770, 3073, 3400,
    3752, 4131, 4538, 4976, 5444, 5967, 6526, 7126, 7766, 8450,
    9851, 11380, 13042, 14847, 16801, 18914, 21192, 23647, 26286, 29119,
)

# From PoB `PoE1/Data/Misc.lua` (`AscendancyRespecCost = 5`) and `Classes/TreeTab.lua`.
ASCENDANCY_RESPEC_MULTIPLIER = 5


def gold_respec_per_point(level: int | None) -> int | None:
    """Return PoE1 gold cost to refund one regular passive node at `level` (1..100), or None if unknown."""
    if level is None or isinstance(level, bool):
        return None
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return None
    if 1 <= lv <= len(GOLD_RESPEC_PRICES):
        return GOLD_RESPEC_PRICES[lv - 1]
    return None


def estimate_respec_gold(
    level: int | None,
    *,
    regular_nodes: int,
    ascendancy_nodes: int = 0,
) -> dict[str, Any]:
    """Estimate total Gold respec cost matching PoB `TreeTab.lua` (`reg * gold + asc * gold * 5`)."""
    reg = max(0, int(regular_nodes))
    asc = max(0, int(ascendancy_nodes))
    per_pt = gold_respec_per_point(level)
    total = (reg * per_pt + asc * per_pt * ASCENDANCY_RESPEC_MULTIPLIER) if per_pt is not None else None
    return {
        "characterLevel": int(level) if per_pt is not None and level is not None else None,
        "goldPerPoint": per_pt,
        "ascendancyMultiplier": ASCENDANCY_RESPEC_MULTIPLIER,
        "deallocatedRegularCount": reg,
        "deallocatedAscendancyCount": asc,
        "estimatedGoldRespecTotal": total,
    }
