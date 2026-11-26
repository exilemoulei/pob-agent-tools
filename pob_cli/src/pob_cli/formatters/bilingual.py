"""Locale-aware `<Localized> (<English>)` formatting backed by `Data/poe1/zh-rCN` & `zh-rTW`."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..game_data.paths import list_available_zh_dirs, resolve_pob_dir
from ..locale_cfg import current_lang, lang_to_data_subdir, normalize_lang, resolve_runtime_dir_l0
from .primitives import strip_pob_escapes

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ALREADY_BILINGUAL_RE = re.compile(r"^.+?\s*\(([A-Za-z0-9][A-Za-z0-9'’:,./+\- ]*)\)\s*$")

_GEM_FILES: tuple[str, ...] = (
    "gems.json",
    "gem_names.json",
    "gem_names_support.json",
    "gem_names_nonactive.json",
)
_ASC_FILES: tuple[str, ...] = (
    "ascendancy_by_id.json",
    "ui.json",
    "passives.json",
    "tree_dn.json",
)

# Cache key: (resolved_zh_dir, mtime) -> {category: (exact_map, lower_map)}
_CategoryMaps = dict[str, tuple[dict[str, str], dict[str, str]]]
_LOCALE_DICT_CACHE: dict[tuple[str, float], _CategoryMaps] = {}
# Cache key: (lang_arg, rt_dir, pob_dir, POB_LANG, POB_RUNTIME_DIR, POB_ROOT, cfg_mtime) -> (eff_lang, zh_dir)
_LocaleCtxKey = tuple[str | None, str, str, str, str, str, float]
_LOCALE_CTX_CACHE: dict[_LocaleCtxKey, tuple[str, Path | None]] = {}


def clear_bilingual_cache() -> None:
    """Clear in-process dictionary and locale-context caches (used by tests switching synthetic engine dirs)."""
    _LOCALE_DICT_CACHE.clear()
    _LOCALE_CTX_CACHE.clear()


def _read_entries(fpath: Path) -> dict[str, str]:
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


def _build_category_map(zh_dir: Path, files: tuple[str, ...]) -> tuple[dict[str, str], dict[str, str]]:
    exact: dict[str, str] = {}
    lower: dict[str, str] = {}
    for fname in files:
        for en, zh in _read_entries(zh_dir / fname).items():
            exact.setdefault(en, zh)
            lower.setdefault(en.lower(), zh)
    return exact, lower


def _resolve_zh_dir_for_lang(
    lang: str,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> Path | None:
    sub = lang_to_data_subdir(lang)
    if not sub:
        return None
    try:
        resolved = resolve_pob_dir(pob_dir, runtime_dir)
        avail = {d.name: d for d in list_available_zh_dirs(resolved)}
    except Exception:
        return None
    return avail.get(sub)


def _resolve_locale_context(
    lang: str | None,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> tuple[str, Path | None]:
    rt_dir = resolve_runtime_dir_l0(runtime_dir)
    cfg_path = rt_dir / "config.json"
    try:
        cfg_mtime = cfg_path.stat().st_mtime if cfg_path.is_file() else -1.0
    except OSError:
        cfg_mtime = -1.0
    ctx_key: _LocaleCtxKey = (
        lang,
        str(rt_dir),
        str(pob_dir or ""),
        os.environ.get("POB_LANG", ""),
        os.environ.get("POB_RUNTIME_DIR", ""),
        os.environ.get("POB_ROOT", ""),
        cfg_mtime,
    )
    cached = _LOCALE_CTX_CACHE.get(ctx_key)
    if cached is not None:
        return cached

    eff_lang = normalize_lang(lang) if lang is not None else current_lang(rt_dir)
    zh_dir = (
        None
        if eff_lang == "en"
        else _resolve_zh_dir_for_lang(eff_lang, pob_dir=pob_dir, runtime_dir=rt_dir)
    )
    result = (eff_lang, zh_dir)
    _LOCALE_CTX_CACHE[ctx_key] = result
    return result


def _load_locale_maps_for_dir(zh_dir: Path | None) -> _CategoryMaps:
    if zh_dir is None:
        return {}
    meta = zh_dir / "meta.json"
    try:
        mtime = meta.stat().st_mtime if meta.is_file() else zh_dir.stat().st_mtime
        cache_key = (str(zh_dir.resolve()), mtime)
    except OSError:
        return {}
    cached = _LOCALE_DICT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    maps: _CategoryMaps = {
        "gem": _build_category_map(zh_dir, _GEM_FILES),
        "unique": _build_category_map(zh_dir, ("uniques.json",)),
        "base": _build_category_map(zh_dir, ("items.json",)),
        "asc": _build_category_map(zh_dir, _ASC_FILES),
    }
    _LOCALE_DICT_CACHE[cache_key] = maps
    return maps


def _load_locale_maps(
    lang: str,
    *,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> _CategoryMaps:
    _eff_lang, zh_dir = _resolve_locale_context(lang, pob_dir=pob_dir, runtime_dir=runtime_dir)
    return _load_locale_maps_for_dir(zh_dir)


def _probe_map(pair: tuple[dict[str, str], dict[str, str]], key: str) -> str | None:
    exact, lower = pair
    return exact.get(key) or lower.get(key.lower())


def _lookup_gem(pair: tuple[dict[str, str], dict[str, str]], name: str, lang: str) -> str | None:
    cands: list[str] = [name]
    low = name.lower()
    if low.endswith(" support"):
        cands.append(name[:-8].strip())
    else:
        cands.append(f"{name} Support")
    for cand in cands:
        hit = _probe_map(pair, cand)
        if hit:
            return hit
    if low.startswith("vaal "):
        base_en = name[5:].strip()
        if base_en:
            base_zh = _probe_map(pair, base_en)
            if base_zh:
                prefix = "瓦爾．" if lang == "zh-TW" else "瓦尔："
                return f"{prefix}{base_zh}"
    return None


def _lookup_in_maps(en: str, kind: str, eff_lang: str, maps: _CategoryMaps) -> str | None:
    k = (kind or "any").strip().lower()
    order_map: dict[str, tuple[str, ...]] = {
        "gem": ("gem",),
        "skill": ("gem",),
        "unique": ("unique",),
        "base": ("base",),
        "item": ("unique", "base"),
        "asc": ("asc",),
        "class": ("asc",),
        "any": ("unique", "gem", "base", "asc"),
    }
    for cat in order_map.get(k, ("unique", "gem", "base", "asc")):
        pair = maps.get(cat)
        if not pair:
            continue
        hit = _lookup_gem(pair, en, eff_lang) if cat == "gem" else _probe_map(pair, en)
        if hit:
            return hit
    return None


def lookup_localized_name(
    en_name: str | None,
    *,
    kind: str = "any",
    lang: str | None = None,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> str | None:
    """Look up localized `zh-CN` or `zh-TW` name for an English PoE1 entity, or None."""
    en = strip_pob_escapes(en_name).strip()
    if not en or _CJK_RE.search(en):
        return None
    eff_lang, zh_dir = _resolve_locale_context(lang, pob_dir=pob_dir, runtime_dir=runtime_dir)
    if eff_lang == "en":
        return None
    maps = _load_locale_maps_for_dir(zh_dir)
    if not maps:
        return None
    return _lookup_in_maps(en, kind, eff_lang, maps)


def _is_already_bilingual(text: str, en_hint: str = "") -> bool:
    if en_hint and f"({en_hint})" in text:
        return True
    return bool(_CJK_RE.search(text) and _ALREADY_BILINGUAL_RE.match(text))


def format_bilingual(
    en_name: Any,
    zh_name: Any = None,
    *,
    kind: str = "any",
    lang: str | None = None,
    pob_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> str:
    """Render an entity as `<Localized> (<English>)` in `zh-CN`/`zh-TW`, or `<English>` in `en`."""
    en = strip_pob_escapes(en_name).strip()
    zh = strip_pob_escapes(zh_name).strip()
    if not en and not zh:
        return ""
    eff_lang, zh_dir = _resolve_locale_context(lang, pob_dir=pob_dir, runtime_dir=runtime_dir)
    if eff_lang == "en":
        return en or zh
    if en and _is_already_bilingual(en):
        return en
    if not en:
        return zh
    if zh and _is_already_bilingual(zh, en):
        return zh

    dict_zh = None
    if not _CJK_RE.search(en):
        maps = _load_locale_maps_for_dir(zh_dir)
        if maps:
            dict_zh = _lookup_in_maps(en, kind, eff_lang, maps)
    loc = dict_zh or (zh if (zh and zh != en and _CJK_RE.search(zh)) else None)
    if not loc or loc == en:
        return en
    return f"{loc} ({en})"
