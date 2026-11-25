"""ROI rankings (rank-nodes, rank-gems), 3-baseline A/B build comparison, cross-build borrowing, and matrix."""

from __future__ import annotations

from .compare import (
    EXTERNAL_ENV_WHITELIST,
    TWO_HANDED_TYPES,
    _apply_env_config,
    _build_pair_table,
    _extract_env_config,
    _extract_env_config_from_cfg,
    _run_worker_pair,
    borrow_from_ref,
    compare_builds,
)
from .gem_caveats import (
    _check_support_hard_conflict,
    compute_sadism_per_ailment_pct,
    compute_sadism_stats,
)
from .gem_links import optimize_links
from .matrix import (
    DEFAULT_MATRIX_SCENARIOS,
    _apply_matrix_scenario_in_place,
    _parse_matrix_scenario,
    _plan_has_post_base_mutations,
    run_matrix,
)
from .rank_refund import DEFAULT_REFUND_SCORE, rank_refunds
from .rank_scored import rank_nodes_scored
from .ranking import (
    _classify_item_weapon_type,
    _detect_equipped_weapon_types,
    _gem_archetype_bucket,
    _gem_caveat,
    _is_legacy_awakened_gem_safe,
    _node_weapon_mastery_kind,
    _safe_dist,
    _safe_float,
    rank_gems,
    rank_nodes,
)
from .respec import plan_respec

__all__ = [
    "DEFAULT_MATRIX_SCENARIOS",
    "DEFAULT_REFUND_SCORE",
    "EXTERNAL_ENV_WHITELIST",
    "TWO_HANDED_TYPES",
    "_apply_env_config",
    "_apply_matrix_scenario_in_place",
    "_build_pair_table",
    "_check_support_hard_conflict",
    "_classify_item_weapon_type",
    "_detect_equipped_weapon_types",
    "_extract_env_config",
    "_extract_env_config_from_cfg",
    "_gem_archetype_bucket",
    "_gem_caveat",
    "_is_legacy_awakened_gem_safe",
    "_node_weapon_mastery_kind",
    "_parse_matrix_scenario",
    "_plan_has_post_base_mutations",
    "_run_worker_pair",
    "_safe_dist",
    "_safe_float",
    "borrow_from_ref",
    "compare_builds",
    "compute_sadism_per_ailment_pct",
    "compute_sadism_stats",
    "optimize_links",
    "plan_respec",
    "rank_gems",
    "rank_nodes",
    "rank_nodes_scored",
    "rank_refunds",
    "run_matrix",
]
