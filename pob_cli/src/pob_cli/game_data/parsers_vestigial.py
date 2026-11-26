"""PoB's Vestigial unique implicits: the mods, their donor uniques and the item type each may go on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import LUA_SKIP_KEYS_MOD
from .lua_parser import MOD_TRADEHASH_STRIP_RE, _as_seq, _LuaParser, _parse_lua_return, _tokenize_lua
from .zh_dict import _ZhDictionary

# PoB (`AddImplicitPopup.lua`, `data.vestigialUniqueBaseTypes`): a Vestigial implicit goes only on a
# unique of its donor's item type, and only these five types take one. A quiver never does.
VESTIGIAL_ITEM_TYPES: dict[str, str] = {
    "helmet": "Helmet",
    "body armour": "Body Armour",
    "gloves": "Gloves",
    "boots": "Boots",
    "shield": "Shield",
}


def _pob_title_key(name: Any) -> str:
    """A unique's title as PoB compares it: its `sanitiseText` writes a-umlaut and o-umlaut plain."""
    return str(name).replace("\u00e4", "a").replace("\u00f6", "o").lower()


def _assign_vestigial_slots(mods: list[dict[str, Any]], uniques: list[dict[str, Any]]) -> None:
    """Give each Vestigial mod its donor's item type; a mod PoB cannot place gets no slot at all."""
    type_by_title = {
        _pob_title_key(u["name"]): VESTIGIAL_ITEM_TYPES.get(str(u.get("item_class") or "").lower()) for u in uniques
    }
    for m in mods:
        if m.get("source") != "Vestigial":
            continue
        donors = m.get("vestigialDonors") or ()
        item_type = type_by_title.get(_pob_title_key(donors[0])) if donors else None
        m["applicable_types"] = [item_type] if item_type else []


def _load_vestigial_mods(data_dir: Path, zh: _ZhDictionary) -> list[dict[str, Any]]:
    excl_path = data_dir / "ModItemExclusive.lua"
    if not excl_path.is_file():
        return []

    vest_donors_by_id: dict[str, list[str]] = {}
    vest_path = data_dir / "Vestigial.lua"
    if vest_path.is_file():
        parsed_vest = _parse_lua_return(vest_path)
        if isinstance(parsed_vest, dict):
            for mid, donors in parsed_vest.items():
                d_list = [str(d).strip() for d in _as_seq(donors) if isinstance(d, str) and str(d).strip()]
                if d_list:
                    vest_donors_by_id[str(mid)] = d_list

    raw_txt = excl_path.read_text(encoding="utf-8", errors="ignore")
    parsed_excl: Any = None
    if len(raw_txt) > 200_000:
        div_lines = [
            ln
            for ln in raw_txt.splitlines()
            if ("Divergent" in ln or "divergentunique" in ln) and ln.lstrip().startswith('["')
        ]
        if div_lines:
            snippet = MOD_TRADEHASH_STRIP_RE.sub(" }", "return {\n" + "\n".join(div_lines) + "\n}")
            try:
                parsed_excl = _LuaParser(_tokenize_lua(snippet), skip_keys=LUA_SKIP_KEYS_MOD).parse_return_table()
            except Exception:
                parsed_excl = None
    if not isinstance(parsed_excl, dict):
        parsed_excl = _parse_lua_return(
            excl_path,
            skip_keys=LUA_SKIP_KEYS_MOD,
            prestrip_re=MOD_TRADEHASH_STRIP_RE,
            prestrip_repl=" }",
        )
    if not isinstance(parsed_excl, dict):
        return []

    out: list[dict[str, Any]] = []
    for mod_id, entry in parsed_excl.items():
        if not isinstance(entry, dict):
            continue
        mid_s = str(mod_id)
        mod_tags = [str(t) for t in _as_seq(entry.get("modTags")) if isinstance(t, str)]
        if not (mid_s.startswith("Divergent") or "divergentunique" in mod_tags):
            continue
        stat_lines = [str(s) for s in _as_seq(entry) if isinstance(s, str) and s.strip()]
        if not stat_lines:
            continue
        stats_zh = [zh.translate_stat_line(ln) for ln in stat_lines] if zh.available else []
        out.append(
            {
                "id": mid_s,
                "source": "Vestigial",
                "generation_type": str(entry.get("type") or "") or "Vestigial",
                "affix": str(entry.get("affix") or ""),
                "group": str(entry.get("group") or ""),
                "level": int(entry.get("level") or 0),
                "stats": stat_lines,
                "stats_zh": stats_zh,
                "mod_tags": mod_tags,
                "weights": {},
                "zero_weights": [],
                "applicable_types": [],  # set by _assign_vestigial_slots once the uniques are known
                "bench_info": None,
                "vestigialDonors": vest_donors_by_id.get(mid_s, []),
            }
        )
    return out


def _vestigial_donor_map_from_mods(mods: list[dict[str, Any]]) -> dict[str, list[str]]:
    by_donor: dict[str, list[str]] = {}
    for m in mods:
        if m.get("source") != "Vestigial":
            continue
        donors = m.get("vestigialDonors") or ()
        if not donors:
            continue
        joined = "; ".join(str(s) for s in (m.get("stats") or ()) if s)
        if not joined:
            continue
        for donor in donors:
            bucket = by_donor.setdefault(_pob_title_key(donor), [])
            if joined not in bucket:
                bucket.append(joined)
    return by_donor
