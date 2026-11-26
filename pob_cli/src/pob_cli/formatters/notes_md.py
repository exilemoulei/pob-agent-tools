"""Markdown renderer for PoB Author Notes (`pob load` and `pob diagnose`)."""

from __future__ import annotations

from typing import Any

DEFAULT_NOTES_MD_MAX_LINES = 25


def format_notes_markdown(
    res: dict[str, Any],
    *,
    max_lines: int = DEFAULT_NOTES_MD_MAX_LINES,
    heading: str = "#### Author Notes (作者备注)",
) -> str:
    """Render compact Author Notes Markdown section; returns `""` when notes are absent."""
    if not isinstance(res, dict):
        return ""
    notes = str(res.get("notes") or "")
    meta = res.get("notesMeta") if isinstance(res.get("notesMeta"), dict) else {}
    if not notes or meta.get("hasNotes") is False:
        return ""

    all_lines = notes.splitlines()
    total_lines = int(meta.get("lineCount") or len(all_lines))
    non_empty_total = sum(1 for ln in all_lines if ln.strip())

    if max_lines > 0 and non_empty_total > max_lines:
        kept: list[str] = []
        non_empty_seen = 0
        for ln in all_lines:
            kept.append(ln)
            if ln.strip():
                non_empty_seen += 1
                if non_empty_seen >= max_lines:
                    break
        body = "\n".join(kept).rstrip()
        footer = (
            f"\n\n_... (showing first {max_lines} non-empty lines of {total_lines} total lines; "
            "pass `--json` for complete author notes)_"
        )
        return f"{heading}\n\n{body}{footer}"

    return f"{heading}\n\n{notes}"
