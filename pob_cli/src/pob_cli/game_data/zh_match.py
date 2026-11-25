"""Resolve a Chinese entity name to its English canonical name across both `zh-rCN` and `zh-rTW`.

`zh-rCN` and `zh-rTW` often word the same entity differently (e.g. `Mageblood` is `法师之血` vs `魔血`,
`Brutal Restraint` is `残酷的约束` vs `殘酷的紀律`), so character folding alone is not enough. Worse,
a Simplified name can fold onto a different entity's Traditional name (`术士手套` is Sorcerer Gloves
in zh-rCN, while `術士手套` is Warlock Gloves in zh-rTW). So lookups try an exact, unfolded match in
both dictionaries before any folded match, and the order follows the query's script, not `pob lang`:
a query already in Simplified form checks `zh-rCN` first, anything else checks `zh-rTW` first.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..locale_cfg import current_lang
from .constants import _CJK_RE, ZH_NON_NAME_KEY_RE
from .paths import list_available_zh_dirs, resolve_pob_dir
from .tw_norm import _fold_search_text, fold_han

_ENTITY_JSON_FILES: tuple[str, ...] = (
    "uniques.json",
    "items.json",
    "gems.json",
    "gem_names.json",
    "gem_names_support.json",
    "gem_names_nonactive.json",
    "passives.json",
    "tree_dn.json",
    "anoint.json",
    "legion.json",
    "tattoo.json",
    "Z-S28.json",
    "Z-S29.json",
    "Z-S30.json",
)
_GEM_SUP_STRIP_RE = re.compile(r"(?:[（(]辅[)）]|辅助|輔助)$")
_EntityMaps = tuple[dict[str, str], dict[str, str]]
_ZH_ENTITY_MAP_CACHE: dict[tuple[str, float], _EntityMaps] = {}


def _normalize_entity_lookup_key(text: str) -> str:
    s = _fold_search_text(text.replace("．", "·").replace("・", "·")).strip()
    return re.sub(r"\s+", " ", s)


def _exact_entity_lookup_key(text: str) -> str:
    s = text.replace("．", "·").replace("・", "·").strip().lower()
    return re.sub(r"\s+", " ", s)


def _put(target: dict[str, str], key: str, en: str) -> None:
    if key and key not in target:
        target[key] = en


def _load_zh_dir_entity_map(zh_dir: Path) -> _EntityMaps:
    """Return `(exact, folded)` reverse maps (Chinese name -> English) for one locale dir."""
    meta = zh_dir / "meta.json"
    try:
        mtime = meta.stat().st_mtime if meta.is_file() else zh_dir.stat().st_mtime
        cache_key = (str(zh_dir.resolve()), mtime)
    except OSError:
        return {}, {}
    cached = _ZH_ENTITY_MAP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    exact_map: dict[str, str] = {}
    fold_map: dict[str, str] = {}
    for fname in _ENTITY_JSON_FILES:
        fpath = zh_dir / fname
        if not fpath.is_file():
            continue
        try:
            payload = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            continue
        is_gem = fname in ("gems.json", "gem_names.json", "gem_names_support.json", "gem_names_nonactive.json")
        for raw_en, raw_zh in entries.items():
            if not isinstance(raw_en, str) or not isinstance(raw_zh, str):
                continue
            en = raw_en.strip()
            zh = raw_zh.strip()
            if (
                not en
                or not zh
                or len(en) > 60
                or len(zh) > 60
                or "{" in en
                or "#" in en
                or "\n" in en
                or "\n" in zh
                or _CJK_RE.search(en)
                or ZH_NON_NAME_KEY_RE.match(en)
            ):
                continue
            _put(exact_map, _exact_entity_lookup_key(zh), en)
            _put(fold_map, _normalize_entity_lookup_key(zh), en)
            if is_gem and "强辅" not in zh and "強輔" not in zh and not en.lower().startswith("awakened "):
                stem_zh = _GEM_SUP_STRIP_RE.sub("", zh).strip()
                if stem_zh and stem_zh != zh:
                    _put(exact_map, _exact_entity_lookup_key(stem_zh), en)
                    _put(fold_map, _normalize_entity_lookup_key(stem_zh), en)

    _ZH_ENTITY_MAP_CACHE[cache_key] = (exact_map, fold_map)
    return exact_map, fold_map


def _locale_order_for(text: str) -> tuple[str, str]:
    # A query with no Traditional-only characters is read as Simplified, so zh-rCN wins ties.
    return ("zh-rCN", "zh-rTW") if text == fold_han(text) else ("zh-rTW", "zh-rCN")


def resolve_zh_entity_across_locales(
    text: str | None,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
    folded: bool = True,
) -> str | None:
    """Resolve a Chinese entity name: exact match in both locale dirs first, then (if `folded`) folded."""
    q = str(text or "").strip()
    if not q or not _CJK_RE.search(q):
        return None
    try:
        resolved_pob = resolve_pob_dir(pob_dir, runtime_dir)
        avail = {d.name: d for d in list_available_zh_dirs(resolved_pob)}
    except Exception:
        return None
    maps = [_load_zh_dir_entity_map(avail[sub]) for sub in _locale_order_for(q) if sub in avail]
    passes = ((0, _exact_entity_lookup_key(q)), (1, _normalize_entity_lookup_key(q)))
    for idx, key in passes if folded else passes[:1]:
        for pair in maps:
            hit = pair[idx].get(key)
            if hit and not _CJK_RE.search(hit):
                return hit
    return None


@lru_cache(maxsize=512)
def _english_for_zh_cached(
    text: str, pob_dir: str | None, runtime_dir: str | None, active_lang: str
) -> str | None:
    from .queries import query_local_data

    resolved_arg = Path(pob_dir) if pob_dir else None
    rt_arg = Path(runtime_dir) if runtime_dir else None
    # The index below holds only the active display locale and matches folded text, so a
    # Simplified name could resolve to a zh-rTW entity that merely shares its characters.
    entity_en = resolve_zh_entity_across_locales(text, pob_dir=resolved_arg, runtime_dir=rt_arg)
    if entity_en:
        return entity_en
    try:
        res = query_local_data("translate", text, pob_dir=resolved_arg, runtime_dir=rt_arg, limit=3)
    except Exception:
        res = {}
    for row in res.get("results") or ():
        if isinstance(row, dict) and row.get("match_type") == "exact":
            en = str(row.get("en") or "").strip()
            if en and not _CJK_RE.search(en) and not ZH_NON_NAME_KEY_RE.match(en):
                return en
    return None


def english_for_zh(
    text: str | None, *, pob_dir: Path | None = None, runtime_dir: Path | None = None
) -> str | None:
    """Exact English name for a Chinese (Simplified or Traditional) entity name, else None."""
    q = str(text or "").strip()
    if not q or not _CJK_RE.search(q):
        return None
    return _english_for_zh_cached(
        q,
        str(pob_dir) if pob_dir else None,
        str(runtime_dir) if runtime_dir else None,
        current_lang(runtime_dir),
    )
