"""Pure-Python PoB `<PathOfBuilding>` XML generator from English GGG character JSON (L1).

Modeled on MIT `cn-poe-community/cn-poe-utils` (`src/building/`), enabling cross-platform
(Windows/macOS/Linux) import for CN server (`poe.game.qq.com`) translated JSON and
international private-profile JSON without patching upstream `bridge.lua`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_POE1_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Scion", ("Ascendant",)),
    ("Marauder", ("Juggernaut", "Berserker", "Chieftain")),
    ("Ranger", ("Warden", "Deadeye", "Pathfinder")),
    ("Witch", ("Occultist", "Elementalist", "Necromancer")),
    ("Duelist", ("Slayer", "Gladiator", "Champion")),
    ("Templar", ("Inquisitor", "Hierophant", "Guardian")),
    ("Shadow", ("Assassin", "Saboteur", "Trickster")),
)

_CLASS_NAME_LOOKUP: dict[str, tuple[int, int, str, str]] = {}
for _cid, (_cname, _ascs) in enumerate(_POE1_CLASSES):
    _CLASS_NAME_LOOKUP[_cname.lower()] = (_cid, 0, _cname, "")
    for _aid, _aname in enumerate(_ascs, start=1):
        _CLASS_NAME_LOOKUP[_aname.lower()] = (_cid, _aid, _cname, _aname)
# Legacy Raider maps cleanly to Ranger slot 1 if encountered on Standard
_CLASS_NAME_LOOKUP["raider"] = (2, 1, "Ranger", "Warden")

_RARITY_MAP: dict[int, str] = {0: "NORMAL", 1: "MAGIC", 2: "RARE", 3: "UNIQUE", 9: "RELIC"}

_SLOT_MAP: dict[str, str] = {
    "Weapon": "Weapon 1",
    "Offhand": "Weapon 2",
    "Weapon2": "Weapon 1 Swap",
    "Offhand2": "Weapon 2 Swap",
    "Helm": "Helmet",
    "BodyArmour": "Body Armour",
    "Gloves": "Gloves",
    "Boots": "Boots",
    "Amulet": "Amulet",
    "Ring": "Ring 1",
    "Ring2": "Ring 2",
    "Ring3": "Ring 3",
    "Belt": "Belt",
}

_INFLUENCE_LINES: tuple[tuple[str, str], ...] = (
    ("shaper", "Shaper Item"),
    ("elder", "Elder Item"),
    ("warlord", "Warlord Item"),
    ("hunter", "Hunter Item"),
    ("crusader", "Crusader Item"),
    ("redeemer", "Redeemer Item"),
    ("searing", "Searing Exarch Item"),
    ("tangled", "Eater of Worlds Item"),
)

_ASCII_NAME_MAP: dict[str, str] = {
    "Doppelgänger Guise": "Doppelganger Guise",
    "Mjölner": "Mjolner",
}
_ASCII_BASE_MAP: dict[str, str] = {
    "Maelström Staff": "Maelstrom Staff",
    "Energy Blade": "Energy Blade One Handed",
}

_FALLBACK_JEWEL_SLOTS_3_30: tuple[int, ...] = (
    26725, 36634, 33989, 41263, 60735, 61834, 31683, 28475, 6230, 48768,
    34483, 7960, 46882, 55190, 61419, 2491, 54127, 32763, 26196, 33631,
    21984, 29712, 48679, 9408, 12613,
)
_JEWEL_SLOTS_CACHE: dict[str, list[int]] = {}
_BUILTIN_SUP_RE = re.compile(r"Supported by Level\s+(\d+)\s+(.+)", re.IGNORECASE)


def _load_jewel_slots(pob_root: Path | None, tree_version: str) -> list[int]:
    if pob_root is None:
        return list(_FALLBACK_JEWEL_SLOTS_3_30)
    tree_lua = Path(pob_root) / "PoE1" / "TreeData" / tree_version / "tree.lua"
    key = str(tree_lua)
    if key in _JEWEL_SLOTS_CACHE:
        return _JEWEL_SLOTS_CACHE[key]
    if tree_lua.is_file():
        try:
            txt = tree_lua.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'\["jewelSlots"\]\s*=\s*\{([^}]+)\}', txt)
            if m:
                nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
                if nums:
                    _JEWEL_SLOTS_CACHE[key] = nums
                    return nums
        except OSError:
            pass
    return list(_FALLBACK_JEWEL_SLOTS_3_30)


def _parse_int(val: Any, default: int = 0) -> int:
    m = re.search(r"-?\d+", str(val or ""))
    return int(m.group(0)) if m else default


def _resolve_class_and_asc(char_meta: dict[str, Any], passives: dict[str, Any]) -> tuple[int, int, str, str]:
    raw_class = str(char_meta.get("class") or "").strip()
    if raw_class.lower() in _CLASS_NAME_LOOKUP:
        return _CLASS_NAME_LOOKUP[raw_class.lower()]
    cid = passives.get("character")
    aid = passives.get("ascendancy")
    if isinstance(cid, int) and 0 <= cid < len(_POE1_CLASSES):
        cname, ascs = _POE1_CLASSES[cid]
        if isinstance(aid, int) and 1 <= aid <= len(ascs):
            return (cid, aid, cname, ascs[aid - 1])
        return (cid, 0, cname, "")
    return (0, 0, "Scion", "")


def _format_sockets(sockets: list[dict[str, Any]] | None) -> tuple[str, int]:
    if not sockets:
        return "", 0
    parts: list[str] = []
    abyss_count = 0
    prev_group: int | None = None
    for s in sockets:
        if not isinstance(s, dict):
            continue
        col = str(s.get("sColour") or "W")
        if col == "A":
            abyss_count += 1
        grp = s.get("group")
        if prev_group is None:
            parts.append(col)
        elif grp == prev_group:
            parts.append(f"-{col}")
        else:
            parts.append(f" {col}")
        prev_group = grp if isinstance(grp, int) else prev_group
    return "".join(parts), abyss_count


def _format_pob_item_text(item: dict[str, Any]) -> str:
    """Format a GGG item JSON object into PoB `<Item>` raw text."""
    frame_type = int(item.get("frameType") or 0)
    rarity = _RARITY_MAP.get(frame_type, "RARE")
    name = _ASCII_NAME_MAP.get(str(item.get("name") or "").strip(), str(item.get("name") or "").strip())
    raw_base = str(item.get("baseType") or item.get("typeLine") or "").strip()
    base_type = _ASCII_BASE_MAP.get(raw_base, raw_base)
    type_line = str(item.get("typeLine") or base_type).strip()
    for src, dst in _ASCII_BASE_MAP.items():
        type_line = type_line.replace(src, dst)

    lines: list[str] = [f"Rarity: {rarity}"]
    if name:
        lines.append(name)
        lines.append(base_type or type_line)
    else:
        lines.append(type_line or base_type)

    props: dict[str, str] = {}
    for p in item.get("properties") or []:
        if isinstance(p, dict) and p.get("name"):
            vals = p.get("values") or []
            v0 = str(vals[0][0]) if vals and isinstance(vals[0], list) and vals[0] else ""
            props[str(p["name"]).strip()] = v0

    for prop_key, label in (
        ("Armour", "Armour"),
        ("Evasion Rating", "Evasion"),
        ("Energy Shield", "Energy Shield"),
        ("Ward", "Ward"),
    ):
        if props.get(prop_key):
            lines.append(f"{label}: {_parse_int(props[prop_key])}")

    if item.get("id"):
        lines.append(f"Unique ID: {item['id']}")

    influences = item.get("influences") or {}
    if isinstance(influences, dict):
        for inf_key, inf_label in _INFLUENCE_LINES:
            if influences.get(inf_key):
                lines.append(inf_label)
    if item.get("searing") and not (isinstance(influences, dict) and influences.get("searing")):
        lines.append("Searing Exarch Item")
    if item.get("tangled") and not (isinstance(influences, dict) and influences.get("tangled")):
        lines.append("Eater of Worlds Item")

    if item.get("ilvl"):
        lines.append(f"Item Level: {int(item['ilvl'])}")
    if props.get("Quality"):
        lines.append(f"Quality: {_parse_int(props['Quality'])}")

    sock_str, abyss_count = _format_sockets(item.get("sockets"))
    if sock_str:
        lines.append(f"Sockets: {sock_str}")
    if props.get("Radius"):
        lines.append(f"Radius: {props['Radius']}")
    if props.get("Limited to"):
        lines.append(f"Limited to: {props['Limited to']}")

    for req in item.get("requirements") or []:
        if isinstance(req, dict) and str(req.get("name") or "").rstrip(":") == "Class":
            vals = req.get("values") or []
            if vals and isinstance(vals[0], list) and vals[0]:
                lines.append(f"Requires Class {vals[0][0]}")

    def _flatten(key: str) -> list[str]:
        raw = item.get(key)
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, str):
                out.extend(s.strip() for s in entry.split("\n") if s.strip())
        return out

    enchants = _flatten("enchantMods")
    implicits = _flatten("implicitMods")
    explicits = _flatten("explicitMods")
    mutated = _flatten("mutatedMods")
    fractured = _flatten("fracturedMods")
    crafted = _flatten("craftedMods")
    crucible = _flatten("crucibleMods")

    if base_type == "Energy Blade One Handed":
        implicits = []
        explicits = [f"Has {abyss_count} Abyssal Sockets"] if abyss_count > 0 else []

    lines.append(f"Implicits: {len(enchants) + len(implicits)}")
    for m in enchants:
        lines.append(f"{{crafted}}{m}")
    lines.extend(implicits)
    lines.extend(explicits)
    for m in mutated:
        lines.append(f"{{mutated}}{m}")
    for m in fractured:
        lines.append(f"{{fractured}}{m}")
    for m in crafted:
        lines.append(f"{{crafted}}{m}")
    for m in crucible:
        lines.append(f"{{crucible}}{m}")
    if item.get("corrupted"):
        lines.append("Corrupted")
    return "\n".join(lines)


def _gem_name_spec(gem: dict[str, Any]) -> str:
    base = str(gem.get("baseType") or gem.get("typeLine") or "").strip()
    hybrid = gem.get("hybrid")
    if isinstance(hybrid, dict) and hybrid.get("isVaalGem") and hybrid.get("baseTypeName"):
        hname = str(hybrid["baseTypeName"]).strip()
        if " of " in hname and not hname.startswith("Vaal "):
            return f"Vaal {hname}"
    return base[:-8] if base.endswith(" Support") else base


def _gem_level_quality(gem: dict[str, Any]) -> tuple[int, int]:
    lvl, qual = 20, 0
    for p in gem.get("properties") or []:
        if not isinstance(p, dict):
            continue
        pname = str(p.get("name") or "").strip()
        vals = p.get("values") or []
        v0 = str(vals[0][0]) if vals and isinstance(vals[0], list) and vals[0] else ""
        if pname == "Level":
            lvl = _parse_int(v0, 20)
        elif pname == "Quality":
            qual = _parse_int(v0, 0)
    return lvl, qual


def _item_slot_name(item: dict[str, Any]) -> str | None:
    inv = str(item.get("inventoryId") or "").strip()
    if inv == "Flask":
        return f"Flask {int(item.get('x') or 0) + 1}"
    return _SLOT_MAP.get(inv)


def character_bundle_to_pob_xml(
    bundle: dict[str, Any],
    *,
    tree_version: str = "3_30",
    pob_root: Path | None = None,
) -> str:
    """Convert an English GGG character bundle (`char`, `items`, `passiveSkills`) into `<PathOfBuilding>` XML."""
    char_meta = bundle.get("char") if isinstance(bundle.get("char"), dict) else {}
    items_payload = bundle.get("items") if isinstance(bundle.get("items"), dict) else {}
    passives = bundle.get("passiveSkills") if isinstance(bundle.get("passiveSkills"), dict) else {}
    if isinstance(items_payload.get("character"), dict):
        char_meta = {**items_payload["character"], **char_meta}

    class_id, asc_id, class_name, asc_name = _resolve_class_and_asc(char_meta, passives)
    level = int(char_meta.get("level") or 1)
    sec_asc_id = int(passives.get("alternate_ascendancy") or 0) if isinstance(passives.get("alternate_ascendancy"), int) else 0

    root = ET.Element("PathOfBuilding")
    ET.SubElement(
        root,
        "Build",
        {
            "level": str(level),
            "className": class_name,
            "ascendClassName": asc_name,
            "targetVersion": "3_0",
            "mainSocketGroup": "1",
            "viewMode": "ITEMS",
        },
    )

    skills_el = ET.SubElement(root, "Skills", {"activeSkillSet": "1"})
    skill_set_el = ET.SubElement(skills_el, "SkillSet", {"id": "1"})

    items_el = ET.SubElement(root, "Items", {"activeItemSet": "1"})
    item_set_el = ET.SubElement(items_el, "ItemSet", {"id": "1", "useSecondWeaponSet": "false"})

    next_item_id = 1
    for item in items_payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        slot_name = _item_slot_name(item)
        if not slot_name:
            continue
        item_id = next_item_id
        next_item_id += 1
        it_el = ET.SubElement(items_el, "Item", {"id": str(item_id)})
        it_el.text = "\n" + _format_pob_item_text(item) + "\n"

        slot_attrs: dict[str, str] = {"itemPbURL": "", "name": slot_name, "itemId": str(item_id)}
        if slot_name.startswith("Flask "):
            slot_attrs["active"] = "true"
        ET.SubElement(item_set_el, "Slot", slot_attrs)

        # Socketed abyssal jewels & gems grouped by link group
        sockets = item.get("sockets") or []
        abyss_idx = 0
        groups: dict[int, list[dict[str, Any]]] = {}
        for si in item.get("socketedItems") or []:
            if not isinstance(si, dict):
                continue
            if si.get("abyssJewel"):
                abyss_idx += 1
                j_id = next_item_id
                next_item_id += 1
                j_el = ET.SubElement(items_el, "Item", {"id": str(j_id)})
                j_el.text = "\n" + _format_pob_item_text(si) + "\n"
                ET.SubElement(
                    item_set_el,
                    "Slot",
                    {"itemPbURL": "", "name": f"{slot_name} Abyssal Socket {abyss_idx}", "itemId": str(j_id)},
                )
            else:
                s_idx = int(si.get("socket") or 0)
                grp = int(sockets[s_idx].get("group") or 0) if 0 <= s_idx < len(sockets) and isinstance(sockets[s_idx], dict) else 0
                groups.setdefault(grp, []).append(si)

        for grp_gems in groups.values():
            skill_attrs: dict[str, str] = {"enabled": "true", "slot": slot_name, "mainActiveSkill": "nil"}
            for g in grp_gems:
                bis = str(g.get("builtInSupport") or "")
                m_sup = _BUILTIN_SUP_RE.search(bis)
                if m_sup:
                    skill_attrs["imbuedSupport"] = m_sup.group(2).strip()
                    break
            sk_el = ET.SubElement(skill_set_el, "Skill", skill_attrs)
            for g in grp_gems:
                name_spec = _gem_name_spec(g)
                glvl, gqual = _gem_level_quality(g)
                is_vaal = name_spec.startswith("Vaal ")
                ET.SubElement(
                    sk_el,
                    "Gem",
                    {
                        "level": str(glvl),
                        "qualityId": "Default",
                        "quality": str(gqual),
                        "nameSpec": name_spec,
                        "enabled": "true",
                        "enableGlobal1": "false" if is_vaal else "true",
                        "enableGlobal2": "true" if is_vaal else "false",
                    },
                )

    # Passive tree, masteries, overrides, and socketed tree jewels
    hashes = [str(int(h)) for h in (passives.get("hashes") or []) if isinstance(h, int)]
    mastery_raw = passives.get("mastery_effects")
    mastery_parts: list[str] = []
    if isinstance(mastery_raw, dict):
        for nid, eid in mastery_raw.items():
            mastery_parts.append(f"{{{nid},{eid}}}")

    tree_el = ET.SubElement(root, "Tree", {"activeSpec": "1"})
    spec_el = ET.SubElement(
        tree_el,
        "Spec",
        {
            "treeVersion": tree_version,
            "classId": str(class_id),
            "ascendClassId": str(asc_id),
            "secondaryAscendClassId": str(sec_asc_id),
            "masteryEffects": ",".join(mastery_parts),
            "nodes": ",".join(hashes),
        },
    )
    sockets_el = ET.SubElement(spec_el, "Sockets")
    jewel_slots = _load_jewel_slots(pob_root, tree_version)
    for j_item in passives.get("items") or []:
        if not isinstance(j_item, dict):
            continue
        x_idx = j_item.get("x")
        if not isinstance(x_idx, int) or not (0 <= x_idx < len(jewel_slots)):
            continue
        j_id = next_item_id
        next_item_id += 1
        j_el = ET.SubElement(items_el, "Item", {"id": str(j_id)})
        j_el.text = "\n" + _format_pob_item_text(j_item) + "\n"
        ET.SubElement(sockets_el, "Socket", {"nodeId": str(jewel_slots[x_idx]), "itemId": str(j_id)})

    overrides = passives.get("skill_overrides")
    if isinstance(overrides, dict) and overrides:
        ov_el = ET.SubElement(spec_el, "Overrides")
        for nid, ov in overrides.items():
            if isinstance(ov, dict) and ov.get("name"):
                ET.SubElement(ov_el, "Override", {"dn": str(ov["name"]), "nodeId": str(nid)})

    # Keep ItemSet as the final child of <Items> matching PoB XML convention
    items_el.remove(item_set_el)
    items_el.append(item_set_el)

    cfg_el = ET.SubElement(root, "Config", {"activeConfigSet": "1"})
    cfg_set = ET.SubElement(cfg_el, "ConfigSet", {"id": "1", "title": "Default"})
    ET.SubElement(cfg_set, "Input", {"name": "enemyIsBoss", "string": "Pinnacle"})

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
