"""Build digests, checkpoints, slot history and the transactions every build edit runs in."""

from __future__ import annotations

from .checkpoint import (
    BuildSnapshot,
    drop_snapshot,
    fall_back_to_code,
    restore_snapshot,
    supports_cow_snapshot,
    supports_direct_calc,
    take_snapshot,
    worker_caps,
)
from .commands import load_build, redo_build, run_mutation, undo_build
from .digest import (
    capture_digest,
    capture_direct_digest,
    compact_digest_summary,
    diff_digest,
    digest_from_stats,
)
from .helpers import (
    CORE_STAT_KEYS,
    _call_batch,
    _extract_points_used,
    _finite_float,
    _json_safe_stat,
    _safe_min_ele_max_hit,
    _stat_float,
)
from .history import SlotHistory, history_of, rebuild_if_stale
from .transaction import BuildScope, BuildTransaction
from .verify import stat_drift, verify_build, verify_cow_restored

__all__ = [
    "CORE_STAT_KEYS",
    "BuildScope",
    "BuildSnapshot",
    "BuildTransaction",
    "SlotHistory",
    "_call_batch",
    "_extract_points_used",
    "_finite_float",
    "_json_safe_stat",
    "_safe_min_ele_max_hit",
    "_stat_float",
    "capture_digest",
    "capture_direct_digest",
    "compact_digest_summary",
    "diff_digest",
    "digest_from_stats",
    "drop_snapshot",
    "fall_back_to_code",
    "history_of",
    "load_build",
    "rebuild_if_stale",
    "redo_build",
    "restore_snapshot",
    "run_mutation",
    "stat_drift",
    "supports_cow_snapshot",
    "supports_direct_calc",
    "take_snapshot",
    "undo_build",
    "verify_build",
    "verify_cow_restored",
    "worker_caps",
]
