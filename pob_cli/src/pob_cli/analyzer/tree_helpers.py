"""Helper routines for passive tree overrides, socket radius resolution, and search mismatch hints."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from typing import Any

from ..formatters import strip_pob_escapes
from .constants import JEWEL_RADII_315, JEWEL_RADII_316
from .tattoo_slots import _classify_raw_base_bucket

_HYBRID_ATTR_RE = re.compile(r"\b(?:strength|dexterity|intelligence|all attributes)\b", re.IGNORECASE)


def classify_base_target_type(raw_base: dict[str, Any] | None) -> str | None:
    """Classify un-overridden `nodes_db` entry into its readable base target type."""
    if not isinstance(raw_base, dict) or not raw_base:
        return None
    bucket = _classify_raw_base_bucket(raw_base)
    if bucket is not None:
        return bucket
    ntype = str(raw_base.get("type") or "Normal")
    if ntype in ("Notable", "Keystone", "Mastery", "Socket"):
        return ntype
    if ntype == "Normal":
        stats_blob = " ".join(str(s) for s in (raw_base.get("stats") or ()) if s)
        if _HYBRID_ATTR_RE.search(stats_blob):
            return "Small Attribute"
    return ntype

_TREE_TYPE_RANK = {"Keystone": 0, "Notable": 1, "Mastery": 2, "Socket": 3, "Normal": 4}

_RADIUS_TEXT_EN_RE = re.compile(
    r"(?:^|\n)\s*Radius\s*:\s*(Very\s+Large|Massive|Large|Medium|Small)\b",
    re.IGNORECASE,
)
_RADIUS_TEXT_ZH_RE = re.compile(
    r"(?:^|\n)\s*(?:半径|範圍|范围)\s*[:：]\s*(极大|極大|很大|巨大|大|中|小)",
)
_ZH_RADIUS_MAP = {
    "小": "Small",
    "中": "Medium",
    "大": "Large",
    "极大": "Very Large",
    "極大": "Very Large",
    "很大": "Very Large",
    "巨大": "Massive",
}


def apply_node_override(
    base: dict[str, Any],
    ov: dict[str, Any],
    raw_base: dict[str, Any] | None = None,
) -> None:
    """Apply PoB node override while preserving underlying base identity and zeroing conquered stats."""
    orig_src = raw_base if isinstance(raw_base, dict) and raw_base else base
    if "baseNodeName" not in base and orig_src.get("name") is not None:
        base["baseNodeName"] = orig_src.get("name")
    if "baseTargetType" not in base:
        btype = classify_base_target_type(orig_src)
        if btype is not None:
            base["baseTargetType"] = btype
    base["overrideWhy"] = ov.get("why")
    for fld in ("name", "nameZh", "stats", "statsZh"):
        if fld in ov and ov[fld] is not None:
            base[fld] = ov[fld]
    if "stats" in ov and ov["stats"] is not None and not ov.get("statsZh"):
        base["statsZh"] = []
        if ov["stats"] == []:
            base["stats"] = []
    if (
        raw_base is not None
        and "name" in ov
        and ov["name"] is not None
        and ov["name"] != raw_base.get("name")
        and not ov.get("nameZh")
    ):
        base.pop("nameZh", None)


def _jewel_radii_for_version(tree_version: str | None) -> list[tuple[str, float]]:
    tv = str(tree_version or "").strip()
    if tv.startswith(("3_15", "3_14", "3_13", "3_12", "3_11", "3_10", "3_9", "3_8")):
        return JEWEL_RADII_315
    return JEWEL_RADII_316


def _normalize_query_list(search: str | None, queries: Iterable[str] | str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    raw_items: list[str] = []
    if search is not None and str(search).strip():
        raw_items.append(str(search).strip())
    if isinstance(queries, str):
        if queries.strip():
            raw_items.append(queries.strip())
    elif queries is not None:
        for q in queries:
            s = str(q or "").strip()
            if s:
                raw_items.append(s)
    for item in raw_items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _normalize_node_types(node_types: Iterable[str] | str | None) -> set[str]:
    if node_types is None:
        return set()
    items = [node_types] if isinstance(node_types, str) else list(node_types)
    out: set[str] = set()
    for it in items:
        for part in str(it or "").split(","):
            s = part.strip().lower()
            if not s:
                continue
            if s in ("jewel", "jewelsocket", "jewel_socket"):
                s = "socket"
            elif s in ("asc", "ascend", "ascendancy"):
                s = "ascendancy"
            out.add(s)
    return out


def _matches_node_types(meta: dict[str, Any], allowed_types: set[str]) -> bool:
    if not allowed_types:
        return True
    ntype = str(meta.get("type") or "Normal").lower()
    is_asc = bool(meta.get("asc"))
    if "ascendancy" in allowed_types and is_asc:
        return True
    if ntype in allowed_types:
        return True
    if "mastery" in allowed_types and meta.get("overrideWhy") == "mastery":
        return True
    return False


def _match_radius_label(raw_label: str, radii: list[tuple[str, float]]) -> tuple[str, float] | None:
    norm = " ".join(raw_label.strip().lower().replace("_", " ").replace("-", " ").split())
    if not norm:
        return None
    mapped_zh = _ZH_RADIUS_MAP.get(raw_label.strip())
    if mapped_zh:
        norm = mapped_zh.lower()
    for lbl, val in radii:
        if lbl.lower() == norm:
            return lbl, val
    return None


def _extract_radius_from_text(text: str, radii: list[tuple[str, float]]) -> tuple[str, float] | None:
    cleaned = strip_pob_escapes(text)
    m_en = _RADIUS_TEXT_EN_RE.search(cleaned)
    if m_en:
        hit = _match_radius_label(m_en.group(1), radii)
        if hit is not None:
            return hit
    m_zh = _RADIUS_TEXT_ZH_RE.search(cleaned)
    if m_zh:
        hit = _match_radius_label(m_zh.group(1), radii)
        if hit is not None:
            return hit
    return None


def _resolve_equipped_socket_jewel(
    sock_nid: int,
    radii: list[tuple[str, float]],
    items_res: dict[str, Any],
    tree_state: dict[str, Any],
) -> tuple[str | None, str | None, float | None]:
    """Return `(equipped_jewel_name, equipped_radius_label, equipped_max_radius)` for `sock_nid`."""
    items_by_id: dict[Any, dict[str, Any]] = {}
    for it in (items_res or {}).get("items", []):
        if isinstance(it, dict) and "id" in it:
            items_by_id[it["id"]] = it
            items_by_id[str(it["id"])] = it

    matched_slot: dict[str, Any] | None = None
    for slot in (items_res or {}).get("slots", []):
        if not isinstance(slot, dict) or slot.get("inactive"):
            continue
        if slot.get("nodeId") is not None and int(slot["nodeId"]) == sock_nid:
            matched_slot = slot
            break

    sock_entry: dict[str, Any] | None = None
    for sock in (tree_state or {}).get("sockets", []):
        if isinstance(sock, dict) and sock.get("nodeId") is not None and int(sock["nodeId"]) == sock_nid:
            sock_entry = sock
            break

    item: dict[str, Any] | None = None
    if matched_slot is not None:
        if isinstance(matched_slot.get("item"), dict) and matched_slot["item"]:
            item = matched_slot["item"]
        else:
            iid = matched_slot.get("selItemId") or matched_slot.get("itemId")
            if iid not in (None, 0, "0", "", False):
                item = items_by_id.get(iid) or items_by_id.get(str(iid))
    if item is None and sock_entry is not None:
        if isinstance(sock_entry.get("item"), dict) and sock_entry["item"]:
            item = sock_entry["item"]
        else:
            iid = sock_entry.get("selItemId") or sock_entry.get("itemId")
            if iid not in (None, 0, "0", "", False):
                item = items_by_id.get(iid) or items_by_id.get(str(iid))

    if not isinstance(item, dict) or not item:
        return None, None, None

    disp = strip_pob_escapes(
        item.get("nameZh")
        or item.get("titleZh")
        or item.get("title")
        or item.get("name")
        or item.get("baseName")
        or (matched_slot or {}).get("itemName")
        or ""
    ).strip()
    if not disp and item.get("raw"):
        for ln in strip_pob_escapes(str(item["raw"])).splitlines():
            s_ln = ln.strip()
            if s_ln and not s_ln.lower().startswith("rarity:") and not s_ln.startswith("稀有度:"):
                disp = s_ln
                break

    sources = [d for d in (item, matched_slot, sock_entry) if isinstance(d, dict)]
    for src in sources:
        for key in ("radiusLabel", "radius", "activeRadius", "jewelRadius"):
            val = src.get(key)
            if val is None or isinstance(val, bool):
                continue
            if isinstance(val, (int, float)) and float(val) > 0:
                fval = float(val)
                for lbl, r_val in radii:
                    if abs(fval - float(r_val)) < 1e-6:
                        return disp or None, lbl, r_val
                return disp or None, f"Custom({fval:g})", fval
            if isinstance(val, str) and val.strip():
                direct = _match_radius_label(val, radii) or _extract_radius_from_text(val, radii)
                if direct is not None:
                    return disp or None, direct[0], direct[1]

    text_blobs: list[str] = []
    for key in ("implicitLines", "explicitLines", "propLines", "properties", "mods", "stats"):
        lines_val = item.get(key)
        if isinstance(lines_val, (list, tuple)):
            text_blobs.extend(str(x) for x in lines_val if x)
    for src in sources:
        for raw_key in ("raw", "text"):
            if src.get(raw_key):
                text_blobs.append(str(src[raw_key]))

    for blob in text_blobs:
        hit = _extract_radius_from_text(blob, radii)
        if hit is not None:
            return disp or None, hit[0], hit[1]

    return disp or None, None, None


def _resolve_socket_radius_spec(
    socket_radius: str | int,
    radii: list[tuple[str, float]],
    socket_index_by_node: dict[int, int],
    items_res: dict[str, Any],
    tree_state: dict[str, Any],
    nodes_db: dict[str, Any],
) -> tuple[int, float, str]:
    """Resolve `socket_radius` ('#1', '26725', '#1:Large', 'Jewel 26725') to `(socket_node_id, max_radius, label)`."""
    raw = str(socket_radius).strip()
    if not raw:
        raise ValueError("socket_radius cannot be empty.")

    sock_part = raw
    max_radius_label = radii[-1][0]
    max_radius = radii[-1][1]

    if ":" in raw:
        left, right = raw.rsplit(":", 1)
        matched_r = _match_radius_label(right, radii)
        if matched_r is not None:
            sock_part = left.strip()
            max_radius_label, max_radius = matched_r
        else:
            try:
                num_r = float(right.strip())
                if num_r > 0:
                    sock_part = left.strip()
                    max_radius = num_r
                    max_radius_label = f"Custom({num_r:g})"
            except ValueError:
                pass

    m_hash = re.match(r"^#(\d+)$", sock_part)
    if m_hash:
        target_idx = int(m_hash.group(1))
        for nid, s_idx in socket_index_by_node.items():
            if s_idx == target_idx:
                return nid, max_radius, max_radius_label
        raise ValueError(f"Jewel socket #{target_idx} not found in build slots.")

    if sock_part.isdigit():
        num = int(sock_part)
        if str(num) in nodes_db:
            return num, max_radius, max_radius_label
        for nid, s_idx in socket_index_by_node.items():
            if s_idx == num:
                return nid, max_radius, max_radius_label
        raise ValueError(f"Jewel socket '{sock_part}' not found by node ID or socket index.")

    q_low = sock_part.lower()
    for slot in items_res.get("slots", []):
        if slot.get("nodeId") is not None:
            cand_text = " ".join(str(slot.get(k) or "") for k in ("name", "label", "labelZh")).lower()
            if q_low == str(slot.get("name") or "").lower() or q_low in cand_text:
                return int(slot["nodeId"]), max_radius, max_radius_label

    for sock in tree_state.get("sockets", []):
        nid = int(sock["nodeId"])
        meta = nodes_db.get(str(nid)) or {}
        if q_low in str(meta.get("name") or "").lower() or q_low in str(meta.get("nameZh") or "").lower():
            return nid, max_radius, max_radius_label

    raise ValueError(f"Jewel socket '{socket_radius}' not found.")


def _build_socket_radius_matches(
    socket_radius: str | int,
    tree_version: str | None,
    socket_index_by_node: dict[int, int],
    items_res: dict[str, Any],
    tree_state: dict[str, Any],
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
    get_node_meta: Callable[[int], dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    radii = _jewel_radii_for_version(tree_version)
    sock_nid, max_r, max_r_label = _resolve_socket_radius_spec(
        socket_radius, radii, socket_index_by_node, items_res, tree_state, nodes_db
    )
    sock_node = get_node_meta(sock_nid)
    sx, sy = sock_node.get("x"), sock_node.get("y")
    if not isinstance(sx, (int, float)) or not isinstance(sy, (int, float)):
        raise ValueError(f"Jewel socket node {sock_nid} has no tree coordinates.")

    eq_jewel, eq_r_label, eq_max_r = _resolve_equipped_socket_jewel(sock_nid, radii, items_res, tree_state)

    counts_by_radius: dict[str, int] = {lbl: 0 for lbl, _ in radii}
    matches: list[dict[str, Any]] = []
    all_node_ids = set(nodes_db.keys()) | set(dynamic_map.keys())
    for sid in all_node_ids:
        nid = int(sid)
        if nid == sock_nid:
            continue
        meta = get_node_meta(nid)
        if meta.get("asc") or meta.get("proxy") or meta.get("expansion"):
            continue
        if meta.get("blighted") or meta.get("isBlighted"):
            continue
        if meta.get("type") in ("ClassStart", "AscendClassStart"):
            continue
        nx, ny = meta.get("x"), meta.get("y")
        if not isinstance(nx, (int, float)) or not isinstance(ny, (int, float)):
            continue
        dist = math.hypot(float(nx) - float(sx), float(ny) - float(sy))
        if dist > max_r:
            continue
        tier_label = max_r_label
        for lbl, r_outer in radii:
            if dist <= r_outer:
                tier_label = lbl
                break
        for lbl, r_outer in radii:
            if dist <= r_outer:
                counts_by_radius[lbl] += 1
        meta["distance"] = round(dist, 1)
        meta["radiusTier"] = tier_label
        meta["withinEquippedRadius"] = bool(dist <= eq_max_r) if eq_max_r is not None else None
        matches.append(meta)

    socket_radius_info = {
        "id": sock_nid,
        "nodeId": sock_nid,
        "socketNodeId": sock_nid,
        "name": sock_node.get("name"),
        "nameZh": sock_node.get("nameZh"),
        "query": str(socket_radius),
        "socketIndex": socket_index_by_node.get(sock_nid),
        "x": sx,
        "y": sy,
        "maxRadius": max_r,
        "maxRadiusLabel": max_r_label,
        "equippedJewel": eq_jewel,
        "equippedRadiusLabel": eq_r_label,
        "equippedMaxRadius": eq_max_r,
        "radiusThresholds": {lbl: r for lbl, r in radii},
        "countsByRadius": counts_by_radius,
        "radii": {lbl: [] for lbl, _ in radii},
    }
    return matches, socket_radius_info


def _build_allocation_mismatch_hint(
    pre_alloc_matches: list[dict[str, Any]],
    want_allocated: bool,
) -> str | None:
    """Build a hint when `--allocated`/`--unallocated` search has 0 hits while opposite state matches."""
    opposite = [m for m in pre_alloc_matches if bool(m.get("allocated")) is (not want_allocated)]
    if not opposite:
        return None
    opposite.sort(
        key=lambda m: (
            _TREE_TYPE_RANK.get(m.get("type", "Normal"), 5),
            int(m.get("id", 0)),
        )
    )
    labels: list[str] = []
    for m in opposite[:5]:
        nm = strip_pob_escapes(m.get("nameZh") or m.get("name") or "").strip()
        labels.append(f"{m['id']} ({nm})" if nm else str(m["id"]))
    items_str = ", ".join(labels)
    if want_allocated:
        return f"0 allocated matches; matching unallocated node(s) exist: {items_str}. Omit --allocated to inspect."
    return f"0 unallocated matches; matching allocated node(s) exist: {items_str}. Omit --unallocated to inspect."
