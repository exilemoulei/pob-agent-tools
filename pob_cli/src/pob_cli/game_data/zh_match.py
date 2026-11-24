"""Resolve a Chinese entity name to its English canonical name for engine-facing lookups.

pob-zh.exe shows Traditional (zh-rTW) names by default, and the Traditional and Simplified
translations often word the same entity differently (Mageblood is 魔血 vs 法师之血), so folding
characters is not enough. The engine always carries the English name as well, so an exact
dictionary hit in English matches regardless of which Chinese locale the engine displays.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .constants import _CJK_RE


@lru_cache(maxsize=512)
def _english_for_zh_cached(text: str, pob_dir: str | None) -> str | None:
    from .queries import query_local_data

    try:
        res = query_local_data("translate", text, pob_dir=Path(pob_dir) if pob_dir else None, limit=3)
    except Exception:
        # No index or no dictionary on this engine: callers fall back to the original text.
        return None
    for row in res.get("results") or ():
        if isinstance(row, dict) and row.get("match_type") == "exact":
            en = str(row.get("en") or "").strip()
            if en and not _CJK_RE.search(en):
                return en
    return None


def english_for_zh(text: str | None, *, pob_dir: Path | None = None) -> str | None:
    """Exact English name for a Chinese (Simplified or Traditional) entity name, else None."""
    q = str(text or "").strip()
    if not q or not _CJK_RE.search(q):
        return None
    return _english_for_zh_cached(q, str(pob_dir) if pob_dir else None)
