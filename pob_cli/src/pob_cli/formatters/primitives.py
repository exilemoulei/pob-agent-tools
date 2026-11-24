"""Core formatting primitives, RFC-8259 JSON emission, and PoB color escape stripping."""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from ..compat import ensure_utf8

_tabulate_checked = False
_tabulate_fn: Any = None

POB_ESCAPE_RE = re.compile(r"\^x[0-9A-Fa-f]{6}|\^\d")

RAW_PRESERVE_KEYS = frozenset({
    "raw",
    "beforeRaw",
    "afterRaw",
    "xml",
    "code",
    "notes",
    "text",
    "lhsRaw",
    "rhsRaw",
    "last_good_code",
    "updateWarning",
    "customModsText",
})

LARGE_JSON_STDOUT_BYTES = 16384


def _get_tabulate() -> Any:
    global _tabulate_checked, _tabulate_fn
    if not _tabulate_checked:
        _tabulate_checked = True
        try:
            from tabulate import tabulate as _tab

            _tabulate_fn = _tab
        except ImportError:  # pragma: no cover
            _tabulate_fn = None
    return _tabulate_fn


def strip_pob_escapes(text: str | None) -> str:
    """Strip PoB color codes (^xRRGGBB and ^0..^9) from display strings."""
    if not text:
        return ""
    return POB_ESCAPE_RE.sub("", str(text))


def sanitize_for_output(obj: Any, *, preserve_raw: bool = True) -> Any:
    """Recursively strip PoB color codes from strings while preserving raw payload keys."""
    if isinstance(obj, str):
        return strip_pob_escapes(obj)
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_output(item, preserve_raw=preserve_raw) for item in obj]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if preserve_raw and k in RAW_PRESERVE_KEYS:
                out[k] = v
            else:
                out[k] = sanitize_for_output(v, preserve_raw=preserve_raw)
        return out
    return obj


def _parse_numeric_or_none(val: Any) -> float | None:
    """Parse numeric values and PoB string sentinels ('inf', '-inf', 'nan') to float, or None."""
    if val is None:
        return 0.0
    if isinstance(val, bool):
        return float(int(val))
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0.0
        s_low = s.lower()
        if s_low in ("inf", "+inf", "infinity", "+infinity"):
            return math.inf
        if s_low in ("-inf", "-infinity"):
            return -math.inf
        if s_low == "nan":
            return math.nan
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return None
    return None


def fmt_num(val: float | int | str | None, decimals: int = 1) -> str:
    """Format a number concisely for human/LLM tables, safely handling PoB sentinels ('inf', '-inf', 'nan')."""
    if val is None:
        return "-"
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return "-"
        s_low = s.lower()
        if s_low in ("inf", "+inf", "infinity", "+infinity"):
            return "inf"
        if s_low in ("-inf", "-infinity"):
            return "-inf"
        if s_low == "nan":
            return "nan"
        # Preserve non-numeric sentinel strings (or pre-formatted text) verbatim
        return val
    if isinstance(val, float):
        if math.isnan(val):
            return "nan"
        if math.isinf(val):
            return "inf" if val > 0 else "-inf"
        if val.is_integer():
            return f"{int(val):,}"
        if abs(val) >= 100:
            return f"{val:,.0f}"
        return f"{val:,.{decimals}f}"
    if isinstance(val, int):
        return f"{val:,}"
    return str(val)


def fmt_delta(before: float | int | str | None, after: float | int | str | None, unit: str = "") -> str:
    """Format a before -> after delta with absolute and percentage change, hardened against inf/nan/strings."""
    b = _parse_numeric_or_none(before)
    a = _parse_numeric_or_none(after)
    if b is None or a is None:
        b_disp = fmt_num(before)
        a_disp = fmt_num(after)
        if b_disp == a_disp:
            return "0"
        return f"{b_disp}{unit} -> {a_disp}{unit}"

    if math.isnan(b) or math.isnan(a):
        if math.isnan(b) and math.isnan(a):
            return "0"
        return f"{fmt_num(b)}{unit} -> {fmt_num(a)}{unit}"

    if math.isinf(b) or math.isinf(a):
        if b == a:
            return "0"
        return f"{fmt_num(b)}{unit} -> {fmt_num(a)}{unit}"

    diff = a - b
    if abs(diff) < 1e-6:
        return "0"
    sign = "+" if diff > 0 else ""
    diff_str = fmt_num(diff)
    if abs(b) > 1e-6:
        pct = (diff / abs(b)) * 100.0
        pct_sign = "+" if pct > 0 else ""
        return f"{sign}{diff_str}{unit} ({pct_sign}{pct:.1f}%)"
    return f"{sign}{diff_str}{unit}"


def fmt_signed_pct(delta: Any, pct: Any = None) -> str:
    """Render '+1,234 (+4.2%)' for a PoB-measured delta; '-' when unavailable/non-finite, '0' when unchanged."""
    if delta is None or isinstance(delta, bool):
        return "-"
    try:
        d = float(delta)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(d):
        return "-"
    if d == 0:
        return "0"
    sign = "+" if d > 0 else ""
    out = f"{sign}{fmt_num(d)}"
    if pct is not None and not isinstance(pct, bool):
        try:
            p = float(pct)
        except (TypeError, ValueError):
            p = math.nan
        if math.isfinite(p):
            pct_sign = "+" if (p > 0 or (p == 0 and d > 0)) else ""
            out += f" ({pct_sign}{p:.1f}%)"
    return out


def keep_rejected_warning(stripped: dict[str, Any] | list[str] | tuple[str, ...] | None) -> list[str]:
    """Loud warning lines for --keep tokens that matched nothing (those items were NOT kept)."""
    if stripped is None:
        return []
    if isinstance(stripped, dict):
        raw_rejected = stripped.get("keepRejected") or []
    elif isinstance(stripped, (list, tuple)):
        raw_rejected = stripped
    else:
        return []
    rejected = [str(r).strip() for r in raw_rejected if str(r).strip()]
    if not rejected:
        return []
    return ["> **WARNING --keep not matched (item was NOT kept)**: " + "; ".join(rejected)]


def restore_warning(check: dict[str, Any] | None) -> str | None:
    """Warning when the build could not be verified as restored after a what-if run."""
    if not isinstance(check, dict) or not check:
        return None
    chk = (
        check.get("restoreCheck")
        if "restoreCheck" in check and isinstance(check.get("restoreCheck"), dict)
        else check
    )
    if not isinstance(chk, dict) or not chk:
        return None
    if not chk.get("verified", True):
        detail = chk.get("detail") or "build state differs from before the run"
        return (
            "> **WARNING build not verified as restored**: "
            f"{detail}. Reload the build (`pob load ...`) before trusting later results."
        )
    return None


def render_update_warning(warning: str | None) -> str:
    """Render a top-level Markdown warning banner for degraded engine update states."""
    if not warning or not str(warning).strip():
        return ""
    return f"> [WARN] {str(warning).strip()}\n\n"


def render_table(headers: list[str], rows: list[list[Any]]) -> str:
    """Render a compact GitHub-flavored Markdown table."""
    if not rows:
        return "_(empty)_"
    cleaned_rows = [[strip_pob_escapes(str(cell)) if cell is not None else "-" for cell in row] for row in rows]
    tab_fn = _get_tabulate()
    if tab_fn is not None:
        return tab_fn(cleaned_rows, headers=headers, tablefmt="github", disable_numparse=True)
    str_headers = [str(h) for h in headers]
    widths = [
        max(len(str_headers[i]), *(len(r[i]) if i < len(r) else 0 for r in cleaned_rows), 3)
        for i in range(len(str_headers))
    ]
    hdr_line = "| " + " | ".join(str_headers[i].ljust(widths[i]) for i in range(len(str_headers))) + " |"
    sep_line = "|" + "|".join("-" * (widths[i] + 2) for i in range(len(str_headers))) + "|"
    body_lines = [
        "| " + " | ".join((r[i] if i < len(r) else "-").ljust(widths[i]) for i in range(len(str_headers))) + " |"
        for r in cleaned_rows
    ]
    return "\n".join([hdr_line, sep_line, *body_lines])


def _sanitize_nonfinite_json(obj: Any) -> Any:
    """Convert non-finite floats (NaN/Inf/-Inf) into RFC-8259 JSON-safe values."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return None
        if math.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        return obj
    if isinstance(obj, (list, tuple)):
        return [_sanitize_nonfinite_json(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_nonfinite_json(v) for k, v in obj.items()}
    return obj


def to_json_str(data: Any, *, sanitize: bool = True, compact: bool = False) -> str:
    """Serialize output data to strict RFC-8259 JSON (no NaN/Infinity tokens)."""
    payload = sanitize_for_output(data) if sanitize else data
    payload = _sanitize_nonfinite_json(payload)
    if compact:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)


def emit_json(
    data: Any,
    *,
    compact: bool = False,
    out: str | Path | None = None,
    sanitize: bool = True,
    bloat_guard: bool = True,
) -> str:
    """Serialize RFC-8259 JSON safely and write either to a UTF-8 no-BOM file (`out`) or stdout.

    When writing to stdout (`out is None`) and payload exceeds 16 KB, auto-compacts to single-line
    JSON and emits a concise stderr hint so shell/LLM callers don't flood long conversation contexts.
    """
    text = to_json_str(data, sanitize=sanitize, compact=compact)
    if out is not None:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8", newline="\n")
        return text

    if bloat_guard:
        orig_bytes = len(text.encode("utf-8"))
        if orig_bytes > LARGE_JSON_STDOUT_BYTES:
            orig_kb = orig_bytes / 1024.0
            if not compact:
                text = to_json_str(data, sanitize=sanitize, compact=True)
                new_kb = len(text.encode("utf-8")) / 1024.0
                hint = (
                    f"[pob hint] Large JSON output ({orig_kb:.1f} KB > 16 KB) auto-compacted to single line ({new_kb:.1f} KB). "
                    "Prefer default Markdown (omit --json) or pass --out <file> / --compact to keep context lean.\n"
                )
            else:
                hint = (
                    f"[pob hint] Large JSON output ({orig_kb:.1f} KB > 16 KB). "
                    "Prefer default Markdown (omit --json) or pass --out <file> to keep context lean.\n"
                )
            ensure_utf8(sys.stderr)
            sys.stderr.write(hint)
            sys.stderr.flush()

    ensure_utf8(sys.stdout)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return text


def format_recency_stamp(
    res: dict[str, Any] | None = None,
    *,
    slot: str | None = None,
    patch: str | None = None,
    pob_version: str | None = None,
    data_version: str | None = None,
    tree_version: str | None = None,
    rev: int | None = None,
) -> str:
    """Build a compact 1-line recency/game anchor banner for multi-turn anti-hallucination."""
    res_d = res if isinstance(res, dict) else {}
    env_d = res_d.get("env") if isinstance(res_d.get("env"), dict) else {}
    info_d = res_d.get("buildInfo") if isinstance(res_d.get("buildInfo"), dict) else {}

    eff_tree = tree_version or env_d.get("treeVersion") or info_d.get("treeVersion")
    eff_patch = patch or env_d.get("patch")
    if not eff_patch:
        tv_s = str(eff_tree or "")
        eff_patch = tv_s.replace("_", ".") if tv_s.startswith("3_") else "3.30"
    eff_pob = pob_version or env_d.get("pobVersion")
    eff_data = data_version or env_d.get("dataVersion")
    eff_slot = slot or env_d.get("slot") or res_d.get("targetSlot") or "main"

    eff_rev = rev
    if eff_rev is None:
        cand_rev = env_d.get("rev") if "rev" in env_d else res_d.get("rev")
        if isinstance(cand_rev, int) and not isinstance(cand_rev, bool):
            eff_rev = cand_rev

    parts = [f"Env: PoE1 {eff_patch} (NOT PoE2)"]
    if eff_pob and str(eff_pob) not in ("-", "unknown"):
        parts.append(f"PoB {eff_pob}")
    if eff_data and str(eff_data) not in ("-", "unknown"):
        parts.append(f"Data {eff_data}")
    if eff_tree and str(eff_tree) not in ("-", "unknown"):
        parts.append(f"Tree {eff_tree}")
    slot_part = f"Slot: {eff_slot} (rev={eff_rev})" if isinstance(eff_rev, int) and not isinstance(eff_rev, bool) else f"Slot: {eff_slot}"
    parts.append(slot_part)
    return "[" + " | ".join(parts) + "]"
