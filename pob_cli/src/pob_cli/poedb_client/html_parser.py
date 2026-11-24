"""HTML page parser and ModsView/card mod-pool extractors for PoEDB (poedb.tw)."""

from __future__ import annotations

import html as html_lib
import json
import re
from html.parser import HTMLParser
from typing import Any

_ATTR_TAG_MAP: tuple[tuple[str, str], ...] = (
    ("str_dex_int_armour", "str_dex_int"),
    ("str_int_armour", "str_int"),
    ("str_dex_armour", "str_dex"),
    ("dex_int_armour", "dex_int"),
    ("str_armour", "str"),
    ("dex_armour", "dex"),
    ("int_armour", "int"),
)

_ATTR_SPLIT_CLASSES = frozenset({"Helmets", "Body_Armours", "Gloves", "Boots", "Shields"})

_SPECIAL_BASE_MOD_POOLS = frozenset(
    {
        "Bone_Ring",
        "Bone_Spirit_Shield",
        "Burst_Band",
        "Convoking_Wand",
        "Iron_Flask",
        "Runic_Crown",
        "Runic_Gauntlets",
        "Runic_Sabatons",
        "Silver_Flask",
        "Silkweave_Sole",
        "Steel_Bracers",
        "Unset_Ring",
        "Cobalt_Jewel",
        "Crimson_Jewel",
        "Viridian_Jewel",
        "Prismatic_Jewel",
        "Murderous_Eye_Jewel",
        "Searching_Eye_Jewel",
        "Hypnotic_Eye_Jewel",
        "Ghastly_Eye_Jewel",
        "Timeless_Jewel",
    }
)

_SKIP_CARD_KEYS = frozenset(
    {
        "市集",
        "Trade",
        "Icon",
        "Mods.stat_description_list",
        "Mods.mod_stat_description_list",
        "Sockets.socket_info",
    }
)


def _clean_html_fragment(fragment: str) -> str:
    if not fragment:
        return ""
    txt = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html_lib.unescape(txt)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in txt.splitlines()]
    return " / ".join(ln for ln in lines if ln)


def _clean_html_lines(fragment: str) -> list[str]:
    if not fragment:
        return []
    txt = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    txt = re.sub(r"</?(?:div|p|li|tr)[^>]*>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html_lib.unescape(txt)
    out: list[str] = []
    for ln in txt.splitlines():
        cleaned = re.sub(r"[ \t]+", " ", ln).strip()
        if cleaned:
            out.append(cleaned)
    return out


class _PoedbPageParser(HTMLParser):
    """Lightweight single-pass HTML parser extracting itembox, cards, and version history."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_text: str = ""
        self.h1_text: str = ""
        self.item_name_parts: list[str] = []
        self.properties: list[str] = []
        self.implicits: list[str] = []
        self.explicits: list[str] = []
        self.descriptions: list[str] = []
        self.card_pairs: list[tuple[str, str]] = []
        self.class_href: str | None = None
        self.tags_value: str | None = None
        self.version_history: list[dict[str, Any]] = []

        self._in_title = False
        self._in_h1 = False
        self._in_script_or_style = False

        # Track element stack of (tag, class_str)
        self._stack: list[tuple[str, str]] = []

        # Active capture targets
        self._capture_target: str | None = None
        self._capture_depth: int = 0
        self._capture_buf: list[str] = []

        # Card name-value tracking
        self._in_nv_card = False
        self._nv_depth = 0
        self._nv_key = ""
        self._nv_val = ""
        self._nv_sub: str | None = None
        self._nv_sub_depth = 0

        # Version history tracking
        self._saw_vh_heading = False
        self._vh_heading_buf: list[str] = []
        self._in_vh_h2 = False
        self._in_vh_table = False
        self._vh_table_depth = 0
        self._in_vh_tr = False
        self._vh_td_idx = 0
        self._vh_ver = ""
        self._vh_changes: list[str] = []
        self._vh_cur_li: list[str] = []
        self._in_vh_li = False
        self._in_vh_td = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        cls = attr_map.get("class", "")
        cls_tokens = set(cls.split())
        self._stack.append((tag, cls))

        if tag in ("script", "style"):
            self._in_script_or_style = True
            return

        if tag == "title" and not self.title_text:
            self._in_title = True
        elif tag == "h1" and not self.h1_text:
            self._in_h1 = True
        elif tag == "h2":
            self._in_vh_h2 = True
            self._vh_heading_buf.clear()

        if tag == "div" and "name-value" in cls_tokens and not self._in_nv_card:
            self._in_nv_card = True
            self._nv_depth = len(self._stack)
            self._nv_key = ""
            self._nv_val = ""
            self._nv_sub = None
        elif self._in_nv_card and tag == "div" and self._nv_sub is None:
            if "name" in cls_tokens:
                self._nv_sub = "name"
                self._nv_sub_depth = len(self._stack)
            elif "value" in cls_tokens:
                self._nv_sub = "value"
                self._nv_sub_depth = len(self._stack)

        if self._in_nv_card and self._nv_sub == "value" and tag == "a" and "ItemClasses" in cls_tokens:
            href = attr_map.get("href", "").strip()
            if href:
                self.class_href = href.rsplit("/", 1)[-1].split("#")[0]

        if self._capture_target is None:
            if "itemName" in cls_tokens:
                self._capture_target = "itemName"
                self._capture_depth = len(self._stack)
                self._capture_buf.clear()
            elif "property" in cls_tokens or "requirements" in cls_tokens:
                self._capture_target = "property"
                self._capture_depth = len(self._stack)
                self._capture_buf.clear()
            elif "implicitMod" in cls_tokens:
                self._capture_target = "implicit"
                self._capture_depth = len(self._stack)
                self._capture_buf.clear()
            elif "explicitMod" in cls_tokens or "mutatedMod" in cls_tokens:
                self._capture_target = "explicit"
                self._capture_depth = len(self._stack)
                self._capture_buf.clear()
            elif "FlavourText" in cls_tokens or "descrText" in cls_tokens or "secDescrText" in cls_tokens:
                self._capture_target = "desc"
                self._capture_depth = len(self._stack)
                self._capture_buf.clear()

        if tag == "br" and self._capture_target in ("implicit", "explicit", "property", "desc"):
            self._capture_buf.append("\n")

        if self._saw_vh_heading and tag == "table" and not self._in_vh_table:
            self._in_vh_table = True
            self._vh_table_depth = len(self._stack)
            self._saw_vh_heading = False
        elif self._in_vh_table:
            if tag == "tr":
                self._in_vh_tr = True
                self._vh_td_idx = 0
                self._vh_ver = ""
                self._vh_changes = []
            elif tag == "td" and self._in_vh_tr:
                self._in_vh_td = True
                self._vh_td_idx += 1
                self._vh_cur_li = []
            elif tag == "li" and self._in_vh_td and self._vh_td_idx == 2:
                if self._vh_cur_li:
                    txt = re.sub(r"\s+", " ", "".join(self._vh_cur_li)).strip()
                    if txt:
                        self._vh_changes.append(txt)
                    self._vh_cur_li.clear()
                self._in_vh_li = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._in_script_or_style = False

        cur_depth = len(self._stack)
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "h2" and self._in_vh_h2:
            self._in_vh_h2 = False
            heading = "".join(self._vh_heading_buf).strip().lower()
            if "version history" in heading or "版本历史" in heading:
                self._saw_vh_heading = True

        if self._capture_target is not None and cur_depth == self._capture_depth:
            raw_text = "".join(self._capture_buf)
            lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw_text.splitlines()]
            lines = [ln for ln in lines if ln]
            if self._capture_target == "itemName" and lines:
                self.item_name_parts.append(" ".join(lines))
            elif self._capture_target == "property":
                self.properties.extend(lines)
            elif self._capture_target == "implicit":
                self.implicits.extend(lines)
            elif self._capture_target == "explicit":
                self.explicits.extend(lines)
            elif self._capture_target == "desc" and lines:
                self.descriptions.append(" ".join(lines))
            self._capture_target = None
            self._capture_buf.clear()

        if self._in_nv_card:
            if self._nv_sub is not None and cur_depth == self._nv_sub_depth:
                self._nv_sub = None
            if cur_depth == self._nv_depth:
                k = re.sub(r"\s+", " ", self._nv_key).strip()
                v = re.sub(r"\s+", " ", self._nv_val).strip()
                if k and v:
                    self.card_pairs.append((k, v))
                    if k == "Tags":
                        self.tags_value = v
                self._in_nv_card = False

        if self._in_vh_table:
            if tag == "td" and self._in_vh_td:
                self._in_vh_td = False
                if self._vh_td_idx == 2 and self._vh_cur_li:
                    txt = re.sub(r"\s+", " ", "".join(self._vh_cur_li)).strip()
                    if txt:
                        self._vh_changes.append(txt)
                    self._vh_cur_li.clear()
                self._in_vh_li = False
            elif tag == "tr" and self._in_vh_tr:
                self._in_vh_tr = False
                ver = re.sub(r"\s+", " ", self._vh_ver).strip()
                if ver and self._vh_changes:
                    self.version_history.append({"version": ver, "changes": self._vh_changes[:12]})
            elif cur_depth == self._vh_table_depth:
                self._in_vh_table = False

        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_script_or_style:
            return
        if self._in_title:
            self.title_text += data
        if self._in_h1:
            self.h1_text += data
        if self._in_vh_h2:
            self._vh_heading_buf.append(data)
        if self._capture_target is not None:
            self._capture_buf.append(data)
        if self._in_nv_card:
            if self._nv_sub == "name":
                self._nv_key += data
            elif self._nv_sub == "value":
                self._nv_val += data
        if self._in_vh_td:
            if self._vh_td_idx == 1:
                self._vh_ver += data
            elif self._vh_td_idx == 2:
                self._vh_cur_li.append(data)


def _extract_mods_view_json(page_html: str) -> dict[str, Any] | None:
    """Extract embedded `new ModsView({...})` JSON object from a PoEDB page if present."""
    m = re.search(r"new\s+ModsView\s*\(", page_html)
    if not m:
        return None
    rest = page_html[m.end():].lstrip()
    if not rest.startswith("{"):
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(rest)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _derive_mod_pool_candidates(slug: str, parser: _PoedbPageParser) -> list[str]:
    """Derive candidate ModsView pool slugs (e.g. `Helmets_str_int`) for an item base/page."""
    candidates: list[str] = []
    if slug in _SPECIAL_BASE_MOD_POOLS:
        candidates.append(slug)

    class_slug = parser.class_href
    tags_str = (parser.tags_value or "").lower()
    if class_slug:
        if class_slug in _ATTR_SPLIT_CLASSES and tags_str:
            for tag_prefix, attr_suffix in _ATTR_TAG_MAP:
                if tag_prefix in tags_str:
                    candidates.append(f"{class_slug}_{attr_suffix}")
                    break
        candidates.append(class_slug)
        if not class_slug.endswith("s"):
            candidates.append(f"{class_slug}s")

    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _parse_int_or_raw(val: Any) -> int | str | None:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return str(val)


def _normalize_mod_side(bucket: str, gen_id: str) -> str:
    if bucket == "normal":
        if gen_id == "1":
            return "prefix"
        if gen_id == "2":
            return "suffix"
        return "normal"
    if bucket == "searing":
        return "eldritch_searing"
    if bucket == "eater":
        return "eldritch_eater"
    if bucket in ("elder", "shaper", "crusader", "redeemer", "hunter", "warlord"):
        suffix = "prefix" if gen_id == "1" else ("suffix" if gen_id == "2" else "")
        return f"influence_{bucket}_{suffix}" if suffix else f"influence_{bucket}"
    if bucket == "corrupted":
        return "corrupted"
    if bucket in ("delve", "veiled", "essence", "incursion", "enchant", "synthesis", "haunted", "infamous"):
        if gen_id == "1":
            return f"{bucket}_prefix"
        if gen_id == "2":
            return f"{bucket}_suffix"
        return bucket
    return bucket


_MOD_FILTER_ALIASES: dict[str, str] = {
    "能量护盾": "energy shield",
    "护盾": "energy shield",
    "最大生命": "maximum life",
    "生命": "life",
    "魔力": "mana",
    "移动速度": "movement speed",
    "抗性": "resistance",
    "压制": "suppress",
    "冷却": "cooldown",
    "暴击": "critical",
}


def _mod_matches_filter(mod: dict[str, Any], mod_filter: str) -> bool:
    terms: list[str] = []
    for raw in re.split(r"[|,，]", mod_filter):
        tok = raw.strip().lower()
        if not tok:
            continue
        terms.append(tok)
        alias = _MOD_FILTER_ALIASES.get(tok)
        if alias and alias not in terms:
            terms.append(alias)
    if not terms:
        return True
    hay = " ".join(
        [
            str(mod.get("text") or ""),
            str(mod.get("name") or ""),
            str(mod.get("side") or ""),
            str(mod.get("bucket") or ""),
            " ".join(str(x) for x in (mod.get("family") or ())),
            " ".join(str(x) for x in (mod.get("tags") or ())),
        ]
    ).lower()
    return any(t in hay for t in terms)


def _extract_mods_from_modsview(
    mv: dict[str, Any],
    *,
    limit_mods: int = 80,
    mod_filter: str | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """Extract normalized mod entries + summary counts from a PoEDB `ModsView` payload."""
    bucket_order = (
        "normal",
        "corrupted",
        "searing",
        "eater",
        "delve",
        "shaper",
        "elder",
        "crusader",
        "redeemer",
        "hunter",
        "warlord",
        "veiled",
        "essence",
        "incursion",
        "infamous",
        "haunted",
        "enchant",
        "synthesis",
    )

    all_parsed: list[dict[str, Any]] = []
    summary: dict[str, int] = {}

    for bucket in bucket_order:
        raw_bucket = mv.get(bucket)
        if not raw_bucket:
            continue
        if isinstance(raw_bucket, dict):
            rows = [v for v in raw_bucket.values() if isinstance(v, dict)]
        elif isinstance(raw_bucket, list):
            rows = [v for v in raw_bucket if isinstance(v, dict)]
        else:
            continue

        for row in rows:
            gen_id = str(row.get("ModGenerationTypeID") or "")
            side = _normalize_mod_side(bucket, gen_id)
            summary[side] = summary.get(side, 0) + 1

            family = [str(f) for f in (row.get("ModFamilyList") or []) if f]
            level_val = _parse_int_or_raw(row.get("Level"))
            drop_raw = row.get("DropChance")
            drop_val: int | float | None = None
            if drop_raw is not None and drop_raw != "":
                try:
                    num = float(drop_raw)
                    drop_val = int(num) if num.is_integer() else num
                except (TypeError, ValueError):
                    drop_val = None

            fossil_no = [str(t) for t in (row.get("fossil_no") or []) if t]
            mod_tags = list(fossil_no)
            for badge_html in row.get("mod_no") or []:
                m_tag = re.search(r'data-tag=["\']([^"\']+)["\']', str(badge_html))
                if m_tag and m_tag.group(1) not in mod_tags:
                    mod_tags.append(m_tag.group(1))

            all_parsed.append(
                {
                    "side": side,
                    "bucket": bucket,
                    "family": family,
                    "level": level_val,
                    "name": _clean_html_fragment(str(row.get("Name") or "")),
                    "text": _clean_html_fragment(str(row.get("str") or "")),
                    "dropChance": drop_val,
                    "fossilNo": fossil_no,
                    "tags": mod_tags,
                }
            )

    def _sort_key(mod: dict[str, Any]) -> tuple[int, int, int]:
        is_normal = 1 if mod.get("bucket") == "normal" else 0
        has_weight = 1 if (mod.get("dropChance") or 0) > 0 else 0
        lv = mod.get("level")
        lv_num = int(lv) if isinstance(lv, int) else 0
        return (is_normal, has_weight, lv_num)

    if mod_filter and mod_filter.strip():
        matched = [m for m in all_parsed if _mod_matches_filter(m, mod_filter)]
        matched.sort(key=_sort_key, reverse=True)
        sliced = matched[:limit_mods] if limit_mods > 0 else matched
        return sliced, len(matched), summary

    total_count = len(all_parsed)
    if limit_mods <= 0 or total_count <= limit_mods:
        return all_parsed, total_count, summary

    # Group best representative per (side, tuple(family))
    best_per_family: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    others: list[dict[str, Any]] = []
    for mod in all_parsed:
        key = (mod["side"], tuple(mod["family"]) or (mod["text"],))
        cur_best = best_per_family.get(key)
        if cur_best is None:
            best_per_family[key] = mod
        elif _sort_key(mod) > _sort_key(cur_best):
            others.append(cur_best)
            best_per_family[key] = mod
        else:
            others.append(mod)

    by_side: dict[str, list[dict[str, Any]]] = {}
    for mod in best_per_family.values():
        by_side.setdefault(mod["side"], []).append(mod)
    for lst in by_side.values():
        lst.sort(key=_sort_key, reverse=True)

    selected: list[dict[str, Any]] = []
    # Prioritize core `normal` prefix/suffix first (up to >=75% of limit) before special pools.
    core_sides = [s for s in ("prefix", "suffix") if s in by_side]
    core_quota = max((limit_mods * 3) // 4, limit_mods - 8)
    core_idx = {s: 0 for s in core_sides}
    while len(selected) < min(limit_mods, core_quota) and core_sides:
        progressed = False
        for s in core_sides:
            lst = by_side[s]
            i = core_idx[s]
            if i < len(lst):
                selected.append(lst[i])
                core_idx[s] = i + 1
                progressed = True
                if len(selected) >= min(limit_mods, core_quota):
                    break
        if not progressed:
            break

    side_priority = [
        "prefix",
        "suffix",
        "corrupted",
        "eldritch_searing",
        "eldritch_eater",
        "delve",
        "essence",
        "veiled",
    ]
    ordered_sides = [s for s in side_priority if s in by_side] + [s for s in by_side if s not in side_priority]
    idx_map = {s: core_idx.get(s, 0) for s in ordered_sides}
    while len(selected) < limit_mods:
        progressed = False
        for s in ordered_sides:
            lst = by_side[s]
            i = idx_map[s]
            if i < len(lst):
                selected.append(lst[i])
                idx_map[s] = i + 1
                progressed = True
                if len(selected) >= limit_mods:
                    break
        if not progressed:
            break

    if len(selected) < limit_mods:
        others.sort(key=_sort_key, reverse=True)
        selected.extend(others[: limit_mods - len(selected)])

    return selected, total_count, summary


def _extract_inline_mod_cards(page_html: str) -> list[dict[str, Any]]:
    """Extract inline PoEDB mod definition cards (e.g. implicit/entity mod tables)."""
    cards: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<h5 class="card-header">(.*?)</h5>\s*<table[^>]*>(.*?)</table>',
        re.DOTALL | re.IGNORECASE,
    )
    for hdr_raw, tbl_raw in pattern.findall(page_html):
        if "Family" not in tbl_raw and "GenerationType" not in tbl_raw:
            continue
        text = _clean_html_fragment(hdr_raw)
        rows: dict[str, str] = {}
        for k_raw, v_raw in re.findall(r"<tr>\s*<th>(.*?)<td>(.*?)(?=<tr>|$)", tbl_raw, re.DOTALL | re.IGNORECASE):
            k = _clean_html_fragment(k_raw)
            v = _clean_html_fragment(v_raw)
            if k:
                rows[k] = v
        if text or rows:
            cards.append(
                {
                    "side": rows.get("GenerationType", "entity"),
                    "bucket": "card",
                    "family": [x.strip() for x in rows.get("Family", "").split(",") if x.strip()],
                    "level": _parse_int_or_raw((rows.get("Req. level", "").split() or [""])[0]),
                    "name": rows.get("Family", ""),
                    "text": text,
                    "dropChance": None,
                    "fossilNo": [],
                    "tags": [rows.get("Domains", "")] if rows.get("Domains") else [],
                    "stats": rows.get("Stats", ""),
                }
            )
    return cards
