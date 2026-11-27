"""Exact single-item trade primitives, bilingual name/base resolution, and 3.30 Foulborn guard."""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ..trade_item import normalize_template
from .aliases import _CJK_RE, infer_weapon_category_from_base

_FOULBORN_EN_RE = re.compile(r"^\s*foulborn\s+", re.IGNORECASE)
_FOULBORN_ZH_RE = re.compile(r"^\s*(?:秽生|穢生)[\s·・]*")
_LUA_KEY_RE = re.compile(r'^\s*\["([^"]+)"\]\s*=\s*\{\s*$')
_LUA_STR_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*,?\s*$')
_MUTATED_TAG_RE = re.compile(r"\{mutated\}|\(mutated\)", re.IGNORECASE)

_FOULBORN_MAP_CACHE: dict[str, dict[str, tuple[str, ...]]] = {}
_FOULBORN_MAP_LOCK = threading.Lock()


def detect_and_strip_foulborn(name: str | None) -> tuple[str | None, bool]:
    """Strip ``Foulborn `` / ``秽生`` prefix from an item name and report whether it was Foulborn."""
    if not name or not str(name).strip():
        return None, False
    raw = str(name).strip()
    if _FOULBORN_EN_RE.match(raw):
        cleaned = _FOULBORN_EN_RE.sub("", raw).strip()
        return cleaned or None, True
    if _FOULBORN_ZH_RE.match(raw):
        cleaned = _FOULBORN_ZH_RE.sub("", raw).strip()
        return cleaned or None, True
    return raw, False


def _unescape_lua(raw: str) -> str:
    return raw.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def load_foulborn_map(pob_dir: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Load ``PoE1/Data/ModFoulbornMap.lua`` mapping ``unique_name -> tuple[mutated_mod_lines]``."""
    import pob_cli.trade_query as _tq

    try:
        root = Path(pob_dir) if pob_dir is not None else _tq.resolve_engine_root()
    except Exception:
        return {}
    lua_path = root / "PoE1" / "Data" / "ModFoulbornMap.lua"
    if not lua_path.is_file():
        return {}

    cache_key = str(lua_path.resolve())
    with _FOULBORN_MAP_LOCK:
        cached = _FOULBORN_MAP_CACHE.get(cache_key)
        if cached is not None:
            return cached

    mapping: dict[str, tuple[str, ...]] = {}
    cur_name: str | None = None
    cur_mods: list[str] = []
    try:
        text = lua_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    for line in text.splitlines():
        m_key = _LUA_KEY_RE.match(line)
        if m_key:
            if cur_name and cur_mods:
                mapping[cur_name] = tuple(cur_mods)
            cur_name = _unescape_lua(m_key.group(1))
            cur_mods = []
            continue
        if cur_name is not None:
            if line.strip().startswith("}"):
                if cur_mods:
                    mapping[cur_name] = tuple(cur_mods)
                cur_name = None
                cur_mods = []
                continue
            m_str = _LUA_STR_RE.match(line)
            if m_str:
                cur_mods.append(_unescape_lua(m_str.group(1)))

    with _FOULBORN_MAP_LOCK:
        _FOULBORN_MAP_CACHE[cache_key] = mapping
    return mapping


def _looks_replaced_by_foulborn(orig_tmpl: str, mutated_tmpls: list[str]) -> bool:
    if not orig_tmpl or not mutated_tmpls:
        return False
    for mut in mutated_tmpls:
        if orig_tmpl == mut:
            return False
        if SequenceMatcher(None, orig_tmpl, mut).ratio() >= 0.72:
            return True
    return False


def strip_foulborn_replaced_mods(
    item_name: str,
    raw_mod_lines: Sequence[str],
    pob_dir: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Exclude original unique mods replaced by a 3.30 Foulborn mutation to avoid 0-hit trade searches.

    Returns ``(kept_lines, stripped_lines)`` preserving the Foulborn ``{mutated}`` mod(s)
    and all untouched mods while dropping replaced original unique lines.
    """
    clean_name, _ = detect_and_strip_foulborn(item_name)
    if not clean_name:
        return list(raw_mod_lines), []

    fb_map = load_foulborn_map(pob_dir=pob_dir)
    known_fb_lines = fb_map.get(clean_name) or ()
    known_fb_tmpls = {normalize_template(ln) for ln in known_fb_lines if ln}

    present_mutated_tmpls: list[str] = []
    mutated_indices: set[int] = set()
    for idx, line in enumerate(raw_mod_lines):
        tmpl = normalize_template(line)
        if _MUTATED_TAG_RE.search(line) or (tmpl and tmpl in known_fb_tmpls):
            mutated_indices.add(idx)
            if tmpl:
                present_mutated_tmpls.append(tmpl)

    compare_tmpls = present_mutated_tmpls or sorted(known_fb_tmpls)
    if not compare_tmpls:
        return list(raw_mod_lines), []

    kept: list[str] = []
    stripped: list[str] = []
    for idx, line in enumerate(raw_mod_lines):
        if idx in mutated_indices:
            kept.append(line)
            continue
        tmpl = normalize_template(line)
        if _looks_replaced_by_foulborn(tmpl, compare_tmpls):
            stripped.append(line)
        else:
            kept.append(line)
    return kept, stripped


def _resolve_entity_en(raw: str | None, *, pob_dir: Path | None = None) -> tuple[str | None, str | None]:
    if not raw or not str(raw).strip():
        return None, None
    cleaned = str(raw).strip()
    if not _CJK_RE.search(cleaned):
        return cleaned, None
    from .. import game_data as gd

    q_tw = gd.normalize_tw_query(cleaned)
    slang = gd.resolve_player_slang(cleaned) or (gd.resolve_player_slang(q_tw) if q_tw != cleaned else None)
    if slang is not None:
        return slang[0], slang[1]
    try:
        en_name = gd.english_for_zh(cleaned, pob_dir=pob_dir) or (
            gd.english_for_zh(q_tw, pob_dir=pob_dir) if q_tw != cleaned else None
        )
    except Exception:
        en_name = None
    if en_name:
        return en_name, None
    ng = gd.resolve_non_gear_en(cleaned) or (gd.resolve_non_gear_en(q_tw) if q_tw != cleaned else None)
    if ng:
        return ng, None
    return cleaned, None


def resolve_trade_name_and_base(
    item_name: str | None,
    item_type: str | None,
    *,
    pob_dir: Path | None = None,
) -> tuple[str | None, str | None, bool, str | None]:
    """Resolve bilingual/slang ``--name`` and ``--base`` into ``(en_name, en_base, is_foulborn, inferred_category)``."""
    stripped_name, is_foulborn = detect_and_strip_foulborn(item_name)
    en_name, name_kind = _resolve_entity_en(stripped_name, pob_dir=pob_dir)
    if en_name:
        en_name, fb2 = detect_and_strip_foulborn(en_name)
        is_foulborn = is_foulborn or fb2

    en_base, base_kind = _resolve_entity_en(item_type, pob_dir=pob_dir)
    inferred_cat: str | None = None
    if base_kind == "gem" or (name_kind == "gem" and not en_base):
        inferred_cat = "gem"
        if name_kind == "gem" and en_name and not en_base:
            # Gems are queried via `query.type` on official PoE1 Trade, not `query.name`.
            en_base = en_name
            en_name = None
    elif en_base:
        low_base = en_base.lower()
        if "cluster jewel" in low_base:
            inferred_cat = "jewel.cluster"
        elif "flask" in low_base:
            inferred_cat = "flask"
        elif not en_name:
            inferred_cat = infer_weapon_category_from_base(en_base, pob_root=pob_dir)
    return en_name, en_base, is_foulborn, inferred_cat


def apply_exact_item_filters(
    query_body: dict[str, Any],
    *,
    links: int | None = None,
    foulborn: bool = False,
    gem_level: int | None = None,
    gem_quality: int | None = None,
    infer_gem_category: bool = True,
) -> dict[str, Any]:
    """Attach socket link, 3.30 Foulborn, and gem level/quality filters onto a trade query body."""
    inner = query_body.get("query") if isinstance(query_body.get("query"), dict) else query_body
    filters = inner.setdefault("filters", {})

    if links is not None:
        if not (1 <= int(links) <= 6):
            raise ValueError(f"--links must be between 1 and 6, got {links}")
        filters.setdefault("socket_filters", {}).setdefault("filters", {})["links"] = {"min": int(links)}

    if foulborn:
        filters.setdefault("misc_filters", {}).setdefault("filters", {})["foulborn_item"] = {"option": "true"}

    if gem_level is not None:
        filters.setdefault("misc_filters", {}).setdefault("filters", {})["gem_level"] = {"min": int(gem_level)}
    if gem_quality is not None:
        filters.setdefault("misc_filters", {}).setdefault("filters", {})["quality"] = {"min": int(gem_quality)}
    if (gem_level is not None or gem_quality is not None) and infer_gem_category:
        tf = filters.setdefault("type_filters", {}).setdefault("filters", {})
        tf.setdefault("category", {"option": "gem"})

    if not filters:
        inner.pop("filters", None)
    return query_body
