"""Timeless jewel seed search (`pob timeless`).

Every reported number comes from headless PoB (R5): the seed is socketed and the build recalculated.
Three things keep that fast:

1. For the jewels whose lookup table is one byte per (notable, seed), seeds with identical bytes on the
   notables this build uses must produce the same build, so one seed per such group is calculated.
2. When there are still many groups, a few hundred are calculated (every table byte of every node at least
   twice), the value of each single node change is fitted from them, and that estimate only picks which
   further seeds to calculate. It stops once no uncalculated seed is estimated - plus the largest estimate
   error seen - to reach the requested top list. That is an estimate, not a proof; `exact` skips it.
3. Calculations are spread over several throwaway PoB engines running at once, with RPC pipelining via
   `call_batch` and immediate `abort()` cleanup on interrupts or worker failures.

Jewel names, base types, seed ranges and conquerors are read from PoB's own `Data/Uniques/jewel.lua`,
so new timeless jewels work without a code change.
"""

from __future__ import annotations

from ..bridge_client import BridgeWorker
from .engine import search
from .lut import (
    _ALLOCATES_RE as _ALLOCATES_RE,
)
from .lut import (
    _CONQUEROR_ALIASES as _CONQUEROR_ALIASES,
)
from .lut import (
    _JEWEL_ZH_ALIASES as _JEWEL_ZH_ALIASES,
)
from .lut import (
    _SEED_LINE as _SEED_LINE,
)
from .lut import (
    FLAT_TABLES,
    JEWEL_ZH_NAMES,
    MIN_SAMPLE,
    MIN_SEEN,
    RESET_EVERY,
    SEED_STEP,
    SELF_CHECK,
    STAT_KEYS,
    VERIFY_BATCH,
    _resolve_game_dir,
    default_workers,
    fit_pieces,
    group_seeds,
    jewel_text,
    load_jewels,
    load_node_index,
    load_table,
    narrow_down,
    set_idle_priority,
)
from .lut import (
    _active_tree_spec as _active_tree_spec,
)
from .lut import (
    _extract_allocated_notable_names as _extract_allocated_notable_names,
)
from .lut import (
    _extract_xml_allocated_nodes as _extract_xml_allocated_nodes,
)
from .lut import (
    _is_historic_socket_jewel as _is_historic_socket_jewel,
)
from .lut import (
    _json_safe_stat as _json_safe_stat,
)
from .lut import (
    _local_jewel_name as _local_jewel_name,
)
from .lut import (
    _parse_stat as _parse_stat,
)
from .lut import (
    _resolve_conqueror as _resolve_conqueror,
)
from .lut import (
    _resolve_jewel_static as _resolve_jewel_static,
)
from .lut import (
    _stat_rank_key as _stat_rank_key,
)
from .lut import (
    _worker_batch as _worker_batch,
)
from .worker import (
    Engine,
    describe,
    describe_worker,
)
from .worker import (
    _resolve_jewel as _resolve_jewel,
)
from .worker import (
    _seed_nodes as _seed_nodes,
)

__all__ = [
    "FLAT_TABLES",
    "JEWEL_ZH_NAMES",
    "MIN_SAMPLE",
    "MIN_SEEN",
    "RESET_EVERY",
    "SEED_STEP",
    "SELF_CHECK",
    "STAT_KEYS",
    "VERIFY_BATCH",
    "BridgeWorker",
    "Engine",
    "_resolve_game_dir",
    "default_workers",
    "describe",
    "describe_worker",
    "fit_pieces",
    "group_seeds",
    "jewel_text",
    "load_jewels",
    "load_node_index",
    "load_table",
    "narrow_down",
    "search",
    "set_idle_priority",
]
