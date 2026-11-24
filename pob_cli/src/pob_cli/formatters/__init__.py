"""Output formatting, RFC-8259 JSON emission, and PoB color escape stripping."""

from __future__ import annotations

from .build_md import (
    _extract_metrics_block,
    _format_calc_cell,
    _safe_min_ele_max_hit,
    format_compare_markdown,
    format_matrix_markdown,
    format_sweep_markdown,
)
from .diagnose_compare_md import (
    format_diagnose_compare_markdown,
    format_ground_markdown,
)
from .diagnose_md import format_diagnose_markdown
from .gamedata_md import format_game_data
from .mutation_md import format_cautions, format_mutation_report
from .primitives import (
    LARGE_JSON_STDOUT_BYTES,
    POB_ESCAPE_RE,
    RAW_PRESERVE_KEYS,
    _get_tabulate,
    _parse_numeric_or_none,
    _sanitize_nonfinite_json,
    emit_json,
    fmt_delta,
    fmt_num,
    fmt_signed_pct,
    format_recency_stamp,
    keep_rejected_warning,
    render_table,
    render_update_warning,
    restore_warning,
    sanitize_for_output,
    strip_pob_escapes,
    to_json_str,
)
from .tree_md import (
    _format_mastery_effects_lines,
    _format_path,
    format_tree_markdown,
)

__all__ = [
    "LARGE_JSON_STDOUT_BYTES",
    "POB_ESCAPE_RE",
    "RAW_PRESERVE_KEYS",
    "_extract_metrics_block",
    "_format_calc_cell",
    "_format_mastery_effects_lines",
    "_format_path",
    "_get_tabulate",
    "_parse_numeric_or_none",
    "_safe_min_ele_max_hit",
    "_sanitize_nonfinite_json",
    "emit_json",
    "fmt_delta",
    "fmt_num",
    "fmt_signed_pct",
    "format_cautions",
    "format_compare_markdown",
    "format_diagnose_compare_markdown",
    "format_diagnose_markdown",
    "format_game_data",
    "format_ground_markdown",
    "format_matrix_markdown",
    "format_mutation_report",
    "format_recency_stamp",
    "format_sweep_markdown",
    "format_tree_markdown",
    "keep_rejected_warning",
    "render_table",
    "render_update_warning",
    "restore_warning",
    "sanitize_for_output",
    "strip_pob_escapes",
    "to_json_str",
]
