"""Item text builder, tier extraction, and PoB clipboard text parser."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .models import (
    FRAME_RARITY,
    INFLUENCE_API_MAP,
    INFLUENCE_LINE_MAP,
    MOD_KIND_TAGS,
    NUMBER_RE,
    POB_TAG_RE,
    PROPERTY_KEYS,
    PROPERTY_RE,
    SEPARATOR_RE,
    SIMPLE_TAG_RE,
    STATE_LINES,
    TRAILING_KIND_RE,
    Item,
    Mod,
    escape_ggg_string,
    normalize_template,
    sanitize_text,
)

# ---------------------------------------------------------------------------
# Item text builder (PoE1 3.30 + legacy trade API JSON -> PoB clipboard text)
# ---------------------------------------------------------------------------


def _extract_mod_lines(entries: Any, extra_tag: str | None = None) -> list[str]:
    """Extract physical PoB mod lines from 3.30 dict entries or legacy strings.

    Splits multiline entries on ``\\n`` so every physical line receives its
    ``{tag}`` prefix without duplicating tags already present in the text.
    """
    if not isinstance(entries, list):
        return []

    out: list[str] = []
    extra = extra_tag.strip().lower() if extra_tag else None

    for entry in entries:
        raw_text = ""
        ordered_tags: list[str] = []
        if extra:
            ordered_tags.append(extra)

        if isinstance(entry, str):
            raw_text = entry
        elif isinstance(entry, dict):
            raw_text = str(entry.get("description") or entry.get("text") or "")
            flags = entry.get("flags")
            if isinstance(flags, dict):
                for flag_name, flag_val in flags.items():
                    if flag_val:
                        tag_key = str(flag_name).strip().lower()
                        if tag_key and tag_key not in ordered_tags:
                            ordered_tags.append(tag_key)
        else:
            continue

        if not raw_text:
            continue

        for part in raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            cleaned = escape_ggg_string(sanitize_text(part))
            if not cleaned:
                continue
            existing_tags = {tag.lower() for tag in SIMPLE_TAG_RE.findall(cleaned)}
            prefix = "".join(f"{{{tag}}}" for tag in ordered_tags if tag not in existing_tags)
            out.append(f"{prefix}{cleaned}")
    return out


def _format_sockets(sockets: Any) -> str:
    """Format trade API socket array into PoB ``R-G-B R-B W`` notation."""
    if not isinstance(sockets, list) or not sockets:
        return ""
    groups: dict[int, list[str]] = {}
    group_order: list[int] = []
    for socket in sockets:
        if not isinstance(socket, dict):
            continue
        colour = sanitize_text(socket.get("sColour"), 8).upper()
        if not colour:
            continue
        try:
            group_id = int(socket.get("group", 0))
        except (TypeError, ValueError):
            group_id = 0
        if group_id not in groups:
            groups[group_id] = []
            group_order.append(group_id)
        groups[group_id].append(colour)
    if not group_order:
        return ""
    return " ".join("-".join(groups[gid]) for gid in group_order)


def _format_property_line(prop: Any) -> str:
    """Format one trade API property dict into a PoB header line, or empty to skip."""
    if not isinstance(prop, dict):
        return ""
    pname = escape_ggg_string(sanitize_text(prop.get("name"), 120))
    if not pname:
        return ""
    values = prop.get("values")
    if not isinstance(values, list) or not values:
        return ""
    extracted: list[str] = []
    for pair in values:
        if isinstance(pair, (list, tuple)) and pair:
            val_text = escape_ggg_string(sanitize_text(pair[0], 120))
            if val_text:
                extracted.append(val_text)
    if not extracted:
        return ""
    return f"{pname}: {', '.join(extracted)}"


def build_item_text(raw_item: dict[str, Any]) -> str:
    """Build high-fidelity PoB clipboard text from a PoE1 trade fetch row or item dict."""
    if not isinstance(raw_item, dict):
        raise ValueError("trade item payload is not an object")
    item = raw_item.get("item") if isinstance(raw_item.get("item"), dict) else raw_item
    if not isinstance(item, dict):
        raise ValueError("trade item payload is not an object")

    raw_rarity = str(item.get("rarity") or "").strip().upper()
    rarity = raw_rarity if raw_rarity else FRAME_RARITY.get(item.get("frameType"), "RARE")

    name = escape_ggg_string(sanitize_text(item.get("name"), 120))
    type_line = escape_ggg_string(sanitize_text(item.get("typeLine") or item.get("baseType"), 120))
    if not type_line:
        raise ValueError("trade item has no base type")

    lines: list[str] = [f"Rarity: {rarity}"]
    if name:
        lines.append(name)
    lines.append(type_line)

    for prop in item.get("properties") or []:
        prop_line = _format_property_line(prop)
        if prop_line:
            lines.append(prop_line)

    ilvl = item.get("ilvl") or item.get("itemLevel")
    if ilvl is not None and ilvl != "":
        try:
            ilvl_int = int(ilvl)
        except (TypeError, ValueError):
            ilvl_int = 0
        if ilvl_int > 0:
            lines.append(f"Item Level: {ilvl_int}")

    socket_str = _format_sockets(item.get("sockets"))
    if socket_str:
        lines.append(f"Sockets: {socket_str}")

    enchant_lines = _extract_mod_lines(item.get("enchantMods"), extra_tag="enchant")
    scourge_lines = _extract_mod_lines(item.get("scourgeMods"), extra_tag="scourge")
    implicit_lines = _extract_mod_lines(item.get("implicitMods"))
    implicit_block = enchant_lines + scourge_lines + implicit_lines

    lines.append(f"Implicits: {len(implicit_block)}")
    lines.extend(implicit_block)

    explicit_lines = _extract_mod_lines(item.get("explicitMods"))
    lines.extend(explicit_lines)

    explicit_counts = Counter(explicit_lines)
    for field_name, tag in (
        ("fracturedMods", "fractured"),
        ("craftedMods", "crafted"),
        ("mutatedMods", "mutated"),
        ("veiledMods", "veiled"),
    ):
        for mod_line in _extract_mod_lines(item.get(field_name), extra_tag=tag):
            if explicit_counts.get(mod_line, 0) > 0:
                explicit_counts[mod_line] -= 1
                continue
            lines.append(mod_line)

    influences = item.get("influences") if isinstance(item.get("influences"), dict) else {}
    for api_key, display in INFLUENCE_API_MAP:
        if influences.get(api_key) or item.get(api_key):
            lines.append(f"{display} Item")

    if item.get("synthesised"):
        lines.append("Synthesised Item")
    if item.get("fractured"):
        lines.append("Fractured Item")
    if item.get("split"):
        lines.append("Split")
    if item.get("duplicated"):
        lines.append("Mirrored")
    if item.get("corrupted"):
        lines.append("Corrupted")
    if item.get("identified") is False:
        lines.append("Unidentified")

    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Tier extraction (3.30 explicitMods[].mods[] + legacy extended.mods.explicit)
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def _extract_tier_meta(
    mod_obj: Any,
    *,
    preferred_hash: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(mod_obj, dict):
        return None
    tier = sanitize_text(mod_obj.get("tier") or "", 40)
    name = sanitize_text(mod_obj.get("name") or "", 120)
    level = _to_int(mod_obj.get("level"))

    min_val = _to_float(mod_obj.get("min"))
    max_val = _to_float(mod_obj.get("max"))

    magnitudes = mod_obj.get("magnitudes")
    if (min_val is None or max_val is None) and isinstance(magnitudes, list):
        chosen = None
        if preferred_hash:
            for mag in magnitudes:
                if isinstance(mag, dict) and mag.get("hash") == preferred_hash:
                    chosen = mag
                    break
        if chosen is None:
            for mag in magnitudes:
                if isinstance(mag, dict) and (mag.get("min") is not None or mag.get("max") is not None):
                    chosen = mag
                    break
        if isinstance(chosen, dict):
            if min_val is None:
                min_val = _to_float(chosen.get("min"))
            if max_val is None:
                max_val = _to_float(chosen.get("max"))

    if not tier and not name and level is None and min_val is None and max_val is None:
        return None

    return {
        "tier": tier,
        "name": name,
        "level": level,
        "min": min_val,
        "max": max_val,
    }


def _values_fit_mod(values: tuple[float, ...], mod_obj: dict[str, Any]) -> bool:
    if not values:
        return False
    magnitudes = mod_obj.get("magnitudes")
    if not isinstance(magnitudes, list) or not magnitudes:
        return False
    for mag in magnitudes:
        if not isinstance(mag, dict):
            continue
        lo = _to_float(mag.get("min"))
        hi = _to_float(mag.get("max"))
        if lo is None or hi is None:
            continue
        low_b, high_b = (lo, hi) if lo <= hi else (hi, lo)
        if any(low_b <= v <= high_b for v in values):
            return True
    return False


def trade_mod_tiers(raw_item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map normalized explicit modifier templates to tier metadata dicts.

    Supports both PoE1 3.30 ``explicitMods[].mods[]`` and legacy
    ``extended.mods.explicit[]`` (matched via ``extended.hashes.explicit``,
    magnitude range matching, or positional fallback).
    """
    if not isinstance(raw_item, dict):
        return {}
    item = raw_item.get("item") if isinstance(raw_item.get("item"), dict) else raw_item
    if not isinstance(item, dict):
        return {}

    tiers: dict[str, dict[str, Any]] = {}
    explicit_entries = item.get("explicitMods") or []

    # 1. PoE1 3.30 structured explicitMods[].mods[]
    if isinstance(explicit_entries, list):
        for entry in explicit_entries:
            if not isinstance(entry, dict):
                continue
            raw_desc = str(entry.get("description") or entry.get("text") or "")
            mods_list = entry.get("mods")
            if not raw_desc or not isinstance(mods_list, list):
                continue
            meta = None
            for mod_obj in mods_list:
                meta = _extract_tier_meta(mod_obj)
                if meta is not None:
                    break
            if meta is None:
                continue
            for part in raw_desc.replace("\r\n", "\n").split("\n"):
                template = normalize_template(part)
                if template:
                    tiers.setdefault(template, meta)

    # 2. Legacy extended.mods.explicit[] + extended.hashes.explicit[]
    extended = item.get("extended")
    if not isinstance(extended, dict):
        return tiers
    ext_mods_root = extended.get("mods")
    ext_explicit = ext_mods_root.get("explicit") if isinstance(ext_mods_root, dict) else None
    if not isinstance(ext_explicit, list) or not ext_explicit:
        return tiers

    plain_lines: list[str] = []
    if isinstance(explicit_entries, list):
        for entry in explicit_entries:
            if isinstance(entry, str):
                plain_lines.append(entry)
            elif isinstance(entry, dict):
                plain_lines.append(str(entry.get("description") or entry.get("text") or ""))

    ext_hashes_root = extended.get("hashes")
    ext_hashes = ext_hashes_root.get("explicit") if isinstance(ext_hashes_root, dict) else None

    if isinstance(ext_hashes, list) and ext_hashes:
        for idx, hash_entry in enumerate(ext_hashes):
            if idx >= len(plain_lines):
                break
            template = normalize_template(plain_lines[idx])
            if not template or template in tiers:
                continue
            if not isinstance(hash_entry, (list, tuple)) or len(hash_entry) < 2:
                continue
            stat_hash = str(hash_entry[0]) if hash_entry[0] is not None else None
            mod_indices = hash_entry[1]
            if not isinstance(mod_indices, list):
                continue
            for mod_idx in mod_indices:
                if isinstance(mod_idx, int) and 0 <= mod_idx < len(ext_explicit):
                    meta = _extract_tier_meta(ext_explicit[mod_idx], preferred_hash=stat_hash)
                    if meta is not None:
                        tiers.setdefault(template, meta)
                        break
        return tiers

    # Fallback without hashes: match by magnitude range first, then positional if lengths match
    claimed: set[int] = set()
    for line in plain_lines:
        template = normalize_template(line)
        if not template or template in tiers:
            continue
        clean = escape_ggg_string(line).replace("\u2212", "-")
        vals = tuple(float(v) for v in NUMBER_RE.findall(clean))
        for mod_idx, mod_obj in enumerate(ext_explicit):
            if mod_idx in claimed or not isinstance(mod_obj, dict):
                continue
            if _values_fit_mod(vals, mod_obj):
                meta = _extract_tier_meta(mod_obj)
                if meta is not None:
                    tiers[template] = meta
                    claimed.add(mod_idx)
                    break

    if len(plain_lines) == len(ext_explicit):
        for idx, line in enumerate(plain_lines):
            template = normalize_template(line)
            if not template or template in tiers or idx in claimed:
                continue
            meta = _extract_tier_meta(ext_explicit[idx])
            if meta is not None:
                tiers[template] = meta
                claimed.add(idx)

    return tiers


# ---------------------------------------------------------------------------
# Item text parser
# ---------------------------------------------------------------------------


def _first_int(value: str) -> int:
    match = NUMBER_RE.search(str(value).replace("+", ""))
    if not match:
        return 0
    try:
        return int(float(match.group(0)))
    except ValueError:
        return 0


def _is_property_line(line: str) -> bool:
    match = PROPERTY_RE.match(line)
    if not match:
        return False
    key = match.group(1).strip().lower()
    if key in PROPERTY_KEYS:
        return True
    if key.startswith("quality (") and key.endswith(")"):
        return True
    if key.endswith("basepercentile"):
        return True
    if key.startswith(("selected ", "has alt variant")):
        return True
    return False


def _is_meta_or_property_line(line: str) -> bool:
    lowered = line.strip().lower()
    if not lowered:
        return True
    if lowered in STATE_LINES:
        return True
    if lowered.startswith(("implicits:", "item level:", "rarity:", "item class:")):
        return True
    if lowered.startswith("foil unique"):
        return True
    return _is_property_line(line)


def _kind_from(tags: tuple[str, ...], implicit_remaining: int) -> str:
    for tag in tags:
        if tag in MOD_KIND_TAGS:
            return tag
    return "implicit" if implicit_remaining > 0 else "explicit"


def make_mod(
    raw: str,
    *,
    kind: str = "explicit",
    tags: tuple[str, ...] = (),
    line_index: int = -1,
) -> Mod:
    """Create a :class:`Mod` from a single raw modifier line."""
    unwrapped = escape_ggg_string("" if raw is None else str(raw))
    clean = POB_TAG_RE.sub("", unwrapped)
    clean = TRAILING_KIND_RE.sub("", clean).strip()
    template = normalize_template(clean)
    numeric_source = clean.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    values = tuple(float(value) for value in NUMBER_RE.findall(numeric_source))
    return Mod(
        raw=clean,
        template=template,
        values=values,
        kind=kind,
        tags=tags,
        line_index=line_index,
    )


def parse_item_text(text: str) -> Item:
    """Parse PoB clipboard text or text produced by :func:`build_item_text`."""
    raw_lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    lines = [line for line in raw_lines if line and not SEPARATOR_RE.match(line)]
    if not lines:
        raise ValueError("item text is empty")

    item = Item(lines=lines)
    index = 0
    if lines[index].lower().startswith("item class:"):
        index += 1

    if index < len(lines) and lines[index].lower().startswith("rarity:"):
        item.rarity = lines[index].split(":", 1)[1].strip().upper()
        index += 1

    if item.rarity in {"RARE", "UNIQUE", "RELIC"}:
        if index < len(lines):
            first = escape_ggg_string(sanitize_text(lines[index], 120))
            index += 1
            if index < len(lines) and not _is_meta_or_property_line(lines[index]):
                item.name = first
                item.base_type = escape_ggg_string(sanitize_text(lines[index], 120))
                index += 1
            else:
                item.name = ""
                item.base_type = first
    else:
        if index < len(lines):
            item.base_type = escape_ggg_string(sanitize_text(lines[index], 120))
            index += 1

    implicit_remaining = 0
    for line_index in range(index, len(lines)):
        line = lines[line_index]
        lowered = line.lower()

        if lowered.startswith("implicits:"):
            implicit_remaining = _first_int(line.split(":", 1)[1])
            continue
        if lowered.startswith("item level:"):
            item.item_level = _first_int(line.split(":", 1)[1])
            continue
        if lowered == "corrupted":
            item.corrupted = True
            continue
        if lowered == "mirrored":
            item.mirrored = True
            continue
        if lowered == "unidentified":
            item.unidentified = True
            continue
        if lowered == "synthesised item":
            item.synthesised = True
            continue
        if lowered == "fractured item":
            item.fractured = True
            continue
        if lowered == "split":
            item.split = True
            continue
        if lowered in INFLUENCE_LINE_MAP:
            display = INFLUENCE_LINE_MAP[lowered]
            if display not in item.influences:
                item.influences.append(display)
            continue
        if lowered.startswith("foil unique"):
            continue
        if _is_property_line(line):
            continue

        unwrapped = escape_ggg_string(line)
        tags_list = [tag.lower() for tag in SIMPLE_TAG_RE.findall(unwrapped)]
        trailing = TRAILING_KIND_RE.search(unwrapped)
        if trailing:
            trailing_tag = trailing.group(1).lower()
            if trailing_tag not in tags_list:
                tags_list.append(trailing_tag)
        tags = tuple(tags_list)

        clean = POB_TAG_RE.sub("", unwrapped)
        clean = TRAILING_KIND_RE.sub("", clean).strip()
        if not clean:
            continue

        kind = _kind_from(tags, implicit_remaining)
        if implicit_remaining > 0:
            implicit_remaining -= 1

        item.mods.append(make_mod(clean, kind=kind, tags=tags, line_index=line_index))

    return item


def mods_by_template(item: Item | None) -> dict[str, float]:
    """Sum signed values per template so duplicate mods stack correctly."""
    result: dict[str, float] = {}
    if item is None:
        return result
    for mod in item.mods:
        result[mod.template] = result.get(mod.template, 0.0) + mod.signed_value
    return result
