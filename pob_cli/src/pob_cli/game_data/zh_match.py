"""Resolve a Chinese entity name to its English canonical name across both `zh-rCN` and `zh-rTW`.

`zh-rCN` and `zh-rTW` often word the same entity differently (e.g. `Mageblood` is `法师之血` vs `魔血`,
`Brutal Restraint` is `残酷的约束` vs `殘酷的紀律`), so character folding alone is not enough.
`english_for_zh` checks the active locale first and falls back to the sibling Chinese locale on disk
so both Simplified and Traditional names resolve to canonical English regardless of `pob lang`.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..locale_cfg import current_lang
from .constants import _CJK_RE
from .paths import list_available_zh_dirs, resolve_pob_dir
from .tw_norm import _fold_search_text

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
_ZH_ENTITY_MAP_CACHE: dict[tuple[str, float], dict[str, str]] = {}


def _normalize_entity_lookup_key(text: str) -> str:
    s = _fold_search_text(text.replace("．", "·").replace("・", "·")).strip()
    return re.sub(r"\s+", " ", s)


def _load_zh_dir_entity_map(zh_dir: Path) -> dict[str, str]:
    meta = zh_dir / "meta.json"
    try:
        mtime = meta.stat().st_mtime if meta.is_file() else zh_dir.stat().st_mtime
        cache_key = (str(zh_dir.resolve()), mtime)
    except OSError:
        return {}
    cached = _ZH_ENTITY_MAP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    rev_map: dict[str, str] = {}
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
            ):
                continue
            raw_k = zh.lower()
            fold_k = _normalize_entity_lookup_key(zh)
            if raw_k and raw_k not in rev_map:
                rev_map[raw_k] = en
            if fold_k and fold_k not in rev_map:
                rev_map[fold_k] = en
            if is_gem and "强辅" not in zh and "強輔" not in zh and not en.lower().startswith("awakened "):
                stem_zh = _GEM_SUP_STRIP_RE.sub("", zh).strip()
                if stem_zh and stem_zh != zh:
                    stem_fold = _normalize_entity_lookup_key(stem_zh)
                    if stem_fold and stem_fold not in rev_map:
                        rev_map[stem_fold] = en

    _ZH_ENTITY_MAP_CACHE[cache_key] = rev_map
    return rev_map


def resolve_zh_entity_across_locales(
    text: str | None,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> str | None:
    """Resolve exact Chinese entity name via active locale dir first, then sibling (`zh-rCN` <-> `zh-rTW`)."""
    q = str(text or "").strip()
    if not q or not _CJK_RE.search(q):
        return None
    try:
        resolved_pob = resolve_pob_dir(pob_dir, runtime_dir)
        avail = {d.name: d for d in list_available_zh_dirs(resolved_pob)}
    except Exception:
        return None
    if not avail:
        return None

    lang = current_lang(runtime_dir)
    order = ("zh-rTW", "zh-rCN") if lang == "zh-TW" else ("zh-rCN", "zh-rTW")
    raw_k = q.lower()
    fold_k = _normalize_entity_lookup_key(q)
    for sub in order:
        zh_dir = avail.get(sub)
        if zh_dir is None:
            continue
        rev = _load_zh_dir_entity_map(zh_dir)
        hit = rev.get(raw_k) or rev.get(fold_k)
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
    try:
        res = query_local_data("translate", text, pob_dir=resolved_arg, runtime_dir=rt_arg, limit=3)
    except Exception:
        res = {}
    for row in res.get("results") or ():
        if isinstance(row, dict) and row.get("match_type") == "exact":
            en = str(row.get("en") or "").strip()
            if en and not _CJK_RE.search(en):
                return en
    return resolve_zh_entity_across_locales(text, pob_dir=resolved_arg, runtime_dir=rt_arg)


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
