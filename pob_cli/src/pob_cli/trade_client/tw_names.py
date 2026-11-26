"""Traditional Chinese (`zh-rTW`) `name` & `type` mapping for `pathofexile.tw` trade queries."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from ..game_data.paths import list_available_zh_dirs, resolve_pob_dir
from ..game_data.slang import resolve_player_slang
from ..game_data.zh_match import english_for_zh

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
_TW_LEAGUE_MAP: dict[str, str] = {
    "standard": "標準模式",
    "hardcore": "專家模式",
    "covens": "巫影密會",
}


def map_tw_league(league: str) -> str:
    """Map known English PoE1 league names to official `pathofexile.tw` league identifiers."""
    cleaned = str(league or "").strip()
    if not cleaned:
        return cleaned
    return _TW_LEAGUE_MAP.get(cleaned.lower(), cleaned)
_TW_GEM_FILES: tuple[str, ...] = (
    "gems.json",
    "gem_names.json",
    "gem_names_support.json",
    "gem_names_nonactive.json",
)

# Cache key: (resolved_zh_tw_dir, mtime) -> {cat: (en_exact, en_lower, tw_values)}
_TwMapEntry = tuple[dict[str, str], dict[str, str], set[str]]
_TW_NAME_CACHE: dict[tuple[str, float], dict[str, _TwMapEntry]] = {}


def clear_tw_name_cache() -> None:
    """Clear in-process `zh-rTW` trade name cache (used in tests)."""
    _TW_NAME_CACHE.clear()


def _read_dict(fpath: Path) -> dict[str, str]:
    if not fpath.is_file():
        return {}
    try:
        payload = json.loads(fpath.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw = payload.get("entries") if isinstance(payload, dict) and isinstance(payload.get("entries"), dict) else payload
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str):
            ks, vs = k.strip(), v.strip()
            if ks and vs and not _CJK_RE.search(ks) and "\n" not in ks and "\n" not in vs:
                out[ks] = vs
    return out


def _build_entry(zh_tw_dir: Path, files: tuple[str, ...]) -> _TwMapEntry:
    exact: dict[str, str] = {}
    lower: dict[str, str] = {}
    values: set[str] = set()
    for fname in files:
        for en, tw in _read_dict(zh_tw_dir / fname).items():
            exact.setdefault(en, tw)
            lower.setdefault(en.lower(), tw)
            values.add(tw)
    return exact, lower, values


def _load_tw_maps(
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, _TwMapEntry]:
    try:
        resolved = resolve_pob_dir(pob_dir, runtime_dir)
        avail = {d.name: d for d in list_available_zh_dirs(resolved)}
    except Exception:
        return {}
    tw_dir = avail.get("zh-rTW")
    if tw_dir is None:
        return {}
    meta = tw_dir / "meta.json"
    try:
        mtime = meta.stat().st_mtime if meta.is_file() else tw_dir.stat().st_mtime
        cache_key = (str(tw_dir.resolve()), mtime)
    except OSError:
        return {}
    cached = _TW_NAME_CACHE.get(cache_key)
    if cached is not None:
        return cached

    maps = {
        "unique": _build_entry(tw_dir, ("uniques.json",)),
        "base": _build_entry(tw_dir, ("items.json",)),
        "gem": _build_entry(tw_dir, _TW_GEM_FILES),
    }
    _TW_NAME_CACHE[cache_key] = maps
    return maps


def _probe_tw(entry: _TwMapEntry, key: str, *, is_gem: bool = False) -> str | None:
    exact, lower, _vals = entry
    cands = [key]
    if is_gem:
        low = key.lower()
        if low.endswith(" support"):
            cands.append(key[:-8].strip())
        else:
            cands.append(f"{key} Support")
    for cand in cands:
        hit = exact.get(cand) or lower.get(cand.lower())
        if hit:
            return hit
    return None


def to_tw_trade_name(
    name: str,
    *,
    kind: str = "any",
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> str:
    """Map an English, Simplified Chinese, or slang item/unique/base/gem name to official `zh-rTW`."""
    cleaned = str(name or "").strip()
    if not cleaned:
        return cleaned

    maps = _load_tw_maps(pob_dir=pob_dir, runtime_dir=runtime_dir)
    if not maps:
        return cleaned

    k = (kind or "any").strip().lower()
    order_map: dict[str, tuple[str, ...]] = {
        "unique": ("unique",),
        "name": ("unique",),
        "type": ("base", "gem"),
        "base": ("base",),
        "gem": ("gem",),
        "any": ("unique", "base", "gem"),
    }
    cats = order_map.get(k, ("unique", "base", "gem"))

    # Already an exact official zh-rTW trade name -> keep unchanged
    if _CJK_RE.search(cleaned):
        for cat in cats:
            if cleaned in maps[cat][2]:
                return cleaned
        # Bridge Simplified Chinese or player slang -> canonical English -> official zh-rTW
        en_bridged = english_for_zh(cleaned, pob_dir=pob_dir, runtime_dir=runtime_dir)
        if not en_bridged:
            slang_hit = resolve_player_slang(cleaned)
            if slang_hit:
                en_bridged = slang_hit[0]
        if not en_bridged:
            return cleaned
        lookup_en = en_bridged
    else:
        lookup_en = cleaned

    for cat in cats:
        hit = _probe_tw(maps[cat], lookup_en, is_gem=(cat == "gem"))
        if hit:
            return hit
    return cleaned


def localize_query_for_tw(
    query: dict[str, Any],
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a copy of a trade search body with `query.name` and `query.type` mapped to `zh-rTW`.

    Official stat IDs (`explicit.stat_*`, `pseudo.*`, etc.) are identical on `pathofexile.tw`
    and are preserved verbatim.
    """
    cloned = copy.deepcopy(query)
    inner = cloned.get("query") if isinstance(cloned, dict) else None
    if not isinstance(inner, dict):
        return cloned
    if isinstance(inner.get("name"), str) and inner["name"].strip():
        inner["name"] = to_tw_trade_name(
            inner["name"], kind="unique", pob_dir=pob_dir, runtime_dir=runtime_dir
        )
    if isinstance(inner.get("type"), str) and inner["type"].strip():
        inner["type"] = to_tw_trade_name(
            inner["type"], kind="type", pob_dir=pob_dir, runtime_dir=runtime_dir
        )
    return cloned


def unmapped_tw_ascii_fields(query: dict[str, Any]) -> list[tuple[str, str]]:
    """Return `[(field, value), ...]` for `name`/`type` left as pure ASCII after TW localization."""
    inner = query.get("query") if isinstance(query, dict) else None
    if not isinstance(inner, dict):
        return []
    out: list[tuple[str, str]] = []
    for field in ("name", "type"):
        val = inner.get(field)
        if isinstance(val, str):
            cleaned = val.strip()
            if cleaned and _ASCII_LETTER_RE.search(cleaned) and not _CJK_RE.search(cleaned):
                out.append((field, cleaned))
    return out
