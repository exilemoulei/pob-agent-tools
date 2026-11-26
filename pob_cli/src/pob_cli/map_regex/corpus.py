"""Map modifier corpus loader, runtime Tencent zh-CN overlay, and bilingual preset resolver."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..game_data import fold_han
from ..locale_cfg import normalize_lang
from ..updater import resolve_engine_root

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "map_mods_3_30.json"
_PLACEHOLDER_RE = re.compile(r"\{[0-9]+\}|#")
_WS_RE = re.compile(r"\s+")
_CN_STATS_CACHE: dict[tuple[str, float], dict[str, str]] = {}


def _norm_tpl(text: str) -> str:
    return _WS_RE.sub(" ", _PLACEHOLDER_RE.sub("#", text.replace("\n", " "))).strip().lower()


@lru_cache(maxsize=1)
def _load_raw_corpus() -> dict[str, Any]:
    return json.loads(_DATA_PATH.read_text(encoding="utf-8"))


def _safe_engine_root(engine_root: Path | None) -> Path | None:
    if engine_root is not None:
        return Path(engine_root)
    try:
        return resolve_engine_root()
    except Exception:
        return None


def _load_cn_stats_overlay(engine_root: Path | None) -> dict[str, str]:
    root = _safe_engine_root(engine_root)
    if root is None:
        return {}
    stats_path = root / "Data" / "poe1" / "zh-rCN" / "stats.json"
    if not stats_path.is_file():
        return {}
    try:
        mtime = stats_path.stat().st_mtime
    except OSError:
        return {}
    cache_key = (str(stats_path), mtime)
    cached = _CN_STATS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        raw = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    mapping: dict[str, str] = {}
    for en_key, cn_val in entries.items():
        if isinstance(en_key, str) and isinstance(cn_val, str) and cn_val.strip():
            tpl = _norm_tpl(en_key)
            val = _PLACEHOLDER_RE.sub("#", cn_val).strip()
            mapping.setdefault(tpl, val)
    _CN_STATS_CACHE.clear()
    _CN_STATS_CACHE[cache_key] = mapping
    return mapping


def load_corpus(
    client_lang: str = "en",
    *,
    engine_root: Path | None = None,
    use_cn_overlay: bool = True,
) -> dict[str, Any]:
    """Load map mod corpus for `client_lang` (`en`, `zh-CN`, `zh-TW`), with live CN stats overlay."""
    lang = normalize_lang(client_lang)
    raw = _load_raw_corpus()
    lang_key = "en" if lang == "en" else ("zh_tw" if lang == "zh-TW" else "zh_cn")
    cn_map = _load_cn_stats_overlay(engine_root) if (lang == "zh-CN" and use_cn_overlay) else {}

    mods: list[dict[str, Any]] = []
    mods_by_id: dict[str, dict[str, Any]] = {}
    for entry in raw.get("mods") or ():
        en_lines = list(entry.get("en") or ())
        tw_lines = list(entry.get("zh_tw") or ())
        cn_fb = list(entry.get("zh_cn_fallback") or ())
        cn_lines: list[str] = []
        for idx, eline in enumerate(en_lines):
            overlaid = cn_map.get(_norm_tpl(eline))
            if overlaid:
                cn_lines.append(overlaid)
            elif idx < len(cn_fb):
                cn_lines.append(cn_fb[idx])
            elif idx < len(tw_lines):
                cn_lines.append(fold_han(tw_lines[idx]))
        active_lines = en_lines if lang == "en" else (tw_lines if lang == "zh-TW" else cn_lines)
        item = {
            "id": str(entry["id"]),
            "tiers": list(entry.get("tiers") or ()),
            "en": en_lines,
            "zh_tw": tw_lines,
            "zh_cn": cn_lines,
            "lines": active_lines,
            "aliases": list(entry.get("aliases") or ()),
        }
        mods.append(item)
        mods_by_id[item["id"]] = item

    safe_raw = raw.get("safeCorpus") or {}
    other_raw = safe_raw.get("otherMods") or {}
    other_en = list(other_raw.get("en") or ())
    other_tw = list(other_raw.get("zh_tw") or ())
    if lang == "en":
        other_lines = other_en
    elif lang == "zh-TW":
        other_lines = other_tw
    else:
        other_cn_base = list(other_raw.get("zh_cn") or ()) or [fold_han(x) for x in other_tw]
        if cn_map:
            merged_other: list[str] = []
            seen_other: set[str] = set()
            for eline in other_en:
                cn_hit = cn_map.get(_norm_tpl(eline))
                if cn_hit and cn_hit not in seen_other:
                    seen_other.add(cn_hit)
                    merged_other.append(cn_hit)
            for line in other_cn_base:
                if line not in seen_other:
                    seen_other.add(line)
                    merged_other.append(line)
            other_lines = merged_other
        else:
            other_lines = other_cn_base

    names_raw = safe_raw.get("mapNames") or {}
    if lang == "zh-CN":
        map_names = list(names_raw.get("zh_cn") or ()) or sorted({fold_han(x) for x in (names_raw.get("zh_tw") or ())})
    else:
        map_names = list(names_raw.get(lang_key) or ())
    headers = list((safe_raw.get("headers") or {}).get(lang_key) or ())
    meta = dict(raw.get("meta") or {})
    overlay_active = bool(cn_map)
    return {
        "clientLang": lang,
        "corpusVersion": str(meta.get("patch") or "3.30"),
        "langOverlay": "zh-rCN" if overlay_active else "none",
        "cnOverlayActive": overlay_active,
        "meta": meta,
        "mods": mods,
        "modsById": mods_by_id,
        "otherMods": other_lines,
        "mapNames": map_names,
        "headers": headers,
    }


def resolve_preset_ids(raw: str | list[str] | tuple[str, ...] | None, corpus: dict[str, Any]) -> list[str]:
    """Resolve comma-separated preset IDs or bilingual keywords to canonical preset IDs."""
    if not raw:
        return []
    tokens: list[str] = []
    items = [raw] if isinstance(raw, str) else list(raw)
    for chunk in items:
        for part in str(chunk).replace("，", ",").split(","):
            cleaned = part.strip()
            if cleaned:
                tokens.append(cleaned)
    if not tokens:
        return []

    mods: list[dict[str, Any]] = corpus["mods"]
    mods_by_id: dict[str, dict[str, Any]] = corpus["modsById"]
    alias_exact: dict[str, str] = {}
    for m in mods:
        mid = m["id"]
        alias_exact[mid.lower()] = mid
        for al in m.get("aliases") or ():
            alias_exact[fold_han(al).strip().lower()] = mid

    resolved: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        key = fold_han(tok).strip().lower()
        hit = alias_exact.get(key)
        if hit is None:
            matches = [
                m["id"]
                for m in mods
                if key in m["id"].lower()
                or any(key in fold_han(al).lower() for al in m.get("aliases") or ())
                or any(key in fold_han(ln).lower() for ln in (*m["en"], *m["zh_tw"], *m["zh_cn"]))
            ]
            if len(matches) == 1:
                hit = matches[0]
            elif len(matches) > 1:
                raise ValueError(
                    f"Ambiguous map mod preset {tok!r} (matched: {', '.join(matches)}). "
                    f"Specify an exact preset ID."
                )
        if hit is None:
            avail = ", ".join(sorted(mods_by_id))
            raise ValueError(f"Unknown map mod preset {tok!r}. Available presets: {avail}")
        if hit not in seen:
            seen.add(hit)
            resolved.append(hit)
    return resolved
