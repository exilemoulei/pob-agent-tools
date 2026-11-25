"""Passive tree inspection, multi-query search, mastery lookup, and jewel socket radius queries."""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from ..bridge_client import BridgeWorker
from ..snapshot import _call_batch
from .constants import JEWEL_RADII_315, JEWEL_RADII_316

_TREE_TYPE_RANK = {"Keystone": 0, "Notable": 1, "Mastery": 2, "Socket": 3, "Normal": 4}


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
        r_query = right.strip().lower().replace("_", " ").replace("-", " ")
        matched_r = None
        for lbl, val in radii:
            if lbl.lower() == r_query:
                matched_r = (lbl, val)
                break
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
        "radiusThresholds": {lbl: r for lbl, r in radii},
        "countsByRadius": counts_by_radius,
        "radii": {lbl: [] for lbl, _ in radii},
    }
    return matches, socket_radius_info


def _filter_tree_matches_by_queries(
    matches: list[dict[str, Any]],
    query_list: list[str],
    force_path_ids: set[int],
) -> list[dict[str, Any]]:
    socket_queries: dict[int, str] = {}
    text_queries: list[tuple[str, str]] = []
    for q in query_list:
        m_sock = re.match(r"^#(\d+)$", q)
        if m_sock:
            socket_queries[int(m_sock.group(1))] = q
        else:
            text_queries.append((q, q.lower()))

    filtered: list[dict[str, Any]] = []
    for meta in matches:
        hit_queries: list[str] = []
        s_idx = meta.get("socketIndex")
        if s_idx is not None and int(s_idx) in socket_queries:
            hit_queries.append(socket_queries[int(s_idx)])
            force_path_ids.add(int(meta["id"]))

        if text_queries:
            hay_parts = [
                str(meta.get("id") or ""),
                str(meta.get("name") or ""),
                str(meta.get("nameZh") or ""),
                " ".join(str(s) for s in (meta.get("stats") or [])),
                " ".join(str(s) for s in (meta.get("statsZh") or [])),
            ]
            for eff in meta.get("masteryEffects") or []:
                if isinstance(eff, dict):
                    hay_parts.append(str(eff.get("effect") or ""))
                    hay_parts.append(" ".join(str(s) for s in (eff.get("stats") or [])))
                    hay_parts.append(" ".join(str(s) for s in (eff.get("statsZh") or [])))
            haystack = " ".join(hay_parts).lower()
            for orig_q, low_q in text_queries:
                if low_q in haystack:
                    hit_queries.append(orig_q)

        if hit_queries:
            if len(query_list) > 1:
                meta["matchedQueries"] = hit_queries
            filtered.append(meta)
    return filtered


def _sort_tree_matches_by_proximity(
    matches: list[dict[str, Any]],
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
    allocated_set: set[int],
) -> dict[int, int]:
    adj: dict[int, set[int]] = {}
    group_notables: dict[Any, list[int]] = {}
    mastery_groups: list[tuple[int, Any]] = []
    for src_map in (nodes_db, dynamic_map):
        for sid, raw_node in src_map.items():
            if not isinstance(raw_node, dict):
                continue
            try:
                uid = int(raw_node.get("id", sid))
            except (TypeError, ValueError):
                continue
            for edge_key in ("linked", "links", "out", "in"):
                edges = raw_node.get(edge_key)
                if not isinstance(edges, (list, tuple)):
                    continue
                for raw_nb in edges:
                    try:
                        vid = int(raw_nb)
                    except (TypeError, ValueError):
                        continue
                    if vid == uid:
                        continue
                    adj.setdefault(uid, set()).add(vid)
                    adj.setdefault(vid, set()).add(uid)
            grp_id = raw_node.get("group")
            if grp_id is not None:
                ntype = raw_node.get("type")
                if ntype == "Notable":
                    group_notables.setdefault(grp_id, []).append(uid)
                elif ntype == "Mastery":
                    mastery_groups.append((uid, grp_id))
    for mid, grp_id in mastery_groups:
        if not adj.get(mid):
            for nid in group_notables.get(grp_id, ()):
                adj.setdefault(mid, set()).add(nid)
                adj.setdefault(nid, set()).add(mid)

    bfs_dist: dict[int, int] = {}
    if allocated_set and adj:
        bfs_dist = {nid: 0 for nid in allocated_set}
        queue = deque(allocated_set)
        while queue:
            cur = queue.popleft()
            next_d = bfs_dist[cur] + 1
            for nb in adj.get(cur, ()):
                if nb not in bfs_dist:
                    bfs_dist[nb] = next_d
                    queue.append(nb)

    alloc_coords: list[tuple[float, float]] = []
    for aid in allocated_set:
        ameta = dynamic_map.get(str(aid)) or nodes_db.get(str(aid)) or {}
        ax, ay = ameta.get("x"), ameta.get("y")
        if isinstance(ax, (int, float)) and isinstance(ay, (int, float)):
            alloc_coords.append((float(ax), float(ay)))

    def _min_euclid_sq(meta: dict[str, Any]) -> float:
        nx, ny = meta.get("x"), meta.get("y")
        if not alloc_coords or not isinstance(nx, (int, float)) or not isinstance(ny, (int, float)):
            return float("inf")
        fx, fy = float(nx), float(ny)
        return min((fx - ax) * (fx - ax) + (fy - ay) * (fy - ay) for ax, ay in alloc_coords)

    def _proximity_sort_key(m: dict[str, Any]) -> tuple[int, int, float, int, int]:
        nid = int(m["id"])
        trank = _TREE_TYPE_RANK.get(m.get("type", "Normal"), 5)
        if m.get("allocated"):
            return (0, 0, 0.0, trank, nid)
        hop = bfs_dist.get(nid)
        if hop is not None:
            return (1, hop, 0.0, trank, nid)
        return (2, 999999, _min_euclid_sq(m), trank, nid)

    matches.sort(key=_proximity_sort_key)
    return bfs_dist


def _inspect_tree_filtered(
    worker: BridgeWorker,
    *,
    search: str | None,
    query_list: list[str],
    allowed_types: set[str],
    allocated: bool | None,
    max_cost: int | None,
    show_path: bool,
    masteries: bool,
    socket_radius: str | int | None,
    limit: int,
    tree_version: str | None,
    tree_state: dict[str, Any],
    items_res: dict[str, Any],
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
    allocated_set: set[int],
    socket_index_by_node: dict[int, int],
    get_node_meta: Callable[[int], dict[str, Any]],
) -> dict[str, Any]:
    socket_radius_info: dict[str, Any] | None = None
    force_path_ids: set[int] = set()

    if socket_radius is not None:
        matches, socket_radius_info = _build_socket_radius_matches(
            socket_radius,
            tree_version,
            socket_index_by_node,
            items_res,
            tree_state,
            nodes_db,
            dynamic_map,
            get_node_meta,
        )
    else:
        all_node_ids = set(nodes_db.keys()) | set(dynamic_map.keys())
        matches = [get_node_meta(int(sid)) for sid in all_node_ids]

    if query_list:
        matches = _filter_tree_matches_by_queries(matches, query_list, force_path_ids)
    if allowed_types:
        matches = [m for m in matches if _matches_node_types(m, allowed_types)]
    if allocated is not None:
        want_alloc = bool(allocated)
        matches = [m for m in matches if bool(m.get("allocated")) is want_alloc]

    bfs_dist: dict[int, int] = {}
    if socket_radius is not None:
        matches.sort(
            key=lambda m: (
                float(m.get("distance", 1e9)),
                _TREE_TYPE_RANK.get(m.get("type", "Normal"), 5),
                m.get("id", 0),
            )
        )
    elif max_cost is not None:
        bfs_dist = _sort_tree_matches_by_proximity(matches, nodes_db, dynamic_map, allocated_set)
    else:
        matches.sort(
            key=lambda m: (
                0 if m.get("allocated") else 1,
                _TREE_TYPE_RANK.get(m.get("type", "Normal"), 5),
                m.get("id", 0),
            )
        )

    if limit <= 0:
        hover_limit = len(matches)
    elif max_cost is not None:
        cost_cap = int(max_cost)
        within_hop = sum(
            1 for m in matches if m.get("allocated") or bfs_dist.get(int(m["id"]), 999999) <= cost_cap
        )
        hover_limit = max(limit * 2, 100, within_hop)
    else:
        hover_limit = max(limit * 2, 100)
    hover_targets = matches[:hover_limit]
    if hover_targets:
        hover_calls = [("node_hover", {"id": m["id"]}) for m in hover_targets]
        hover_results = _call_batch(worker, hover_calls, raise_on_error=False)
        for m, hover in zip(hover_targets, hover_results, strict=False):
            if isinstance(hover, dict) and not hover.get("_error") and hover.get("ok") is not False:
                m["cost"] = hover.get("cost")
                if show_path or int(m["id"]) in force_path_ids:
                    m["path"] = hover.get("path")
                    if m.get("allocated") and hover.get("depends") is not None:
                        m["depends"] = hover.get("depends")

    if max_cost is not None:
        cost_cap = int(max_cost)
        matches = [m for m in matches if m.get("cost") is not None and int(m["cost"]) <= cost_cap]
        if socket_radius is None:
            matches.sort(
                key=lambda m: (
                    0 if m.get("allocated") else 1,
                    int(m["cost"]),
                    _TREE_TYPE_RANK.get(m.get("type", "Normal"), 5),
                    m.get("id", 0),
                )
            )

    shown = matches if limit <= 0 else matches[:limit]
    out: dict[str, Any] = {
        "search": search if search is not None else (", ".join(query_list) if query_list else None),
        "queries": query_list,
        "masteries": bool(masteries),
        "showPath": bool(show_path),
        "totalMatches": len(matches),
        "matches": shown,
    }
    if socket_radius_info is not None:
        radii_groups: dict[str, list[dict[str, Any]]] = {
            lbl: [] for lbl in socket_radius_info["radii"].keys()
        }
        for m in shown:
            tier = m.get("radiusTier")
            if tier in radii_groups:
                radii_groups[tier].append(m)
            elif tier:
                radii_groups.setdefault(str(tier), []).append(m)
        socket_radius_info["radii"] = radii_groups
        out["socketRadius"] = socket_radius_info
    return out


def inspect_tree(
    worker: BridgeWorker,
    search: str | None = None,
    *,
    queries: Iterable[str] | str | None = None,
    node_types: Iterable[str] | str | None = None,
    allocated: bool | None = None,
    max_cost: int | None = None,
    show_path: bool = False,
    masteries: bool = False,
    socket_radius: str | int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Summarize allocated passive tree nodes or search/filter nodes with pipelined `node_hover` RPCs."""
    query_list = _normalize_query_list(search, queries)
    allowed_types = _normalize_node_types(node_types)
    if masteries and not query_list and not allowed_types and socket_radius is None:
        allowed_types = {"mastery"}

    is_filtered_mode = bool(
        query_list
        or allowed_types
        or allocated is not None
        or max_cost is not None
        or masteries
        or socket_radius is not None
    )

    with worker.lock:
        tree_state, items_res = _call_batch(
            worker,
            [("get_tree_state", {}), ("list_items", {})],
        )
        tree_version = tree_state.get("treeVersion")
        nodes_db = worker.get_cached_tree_data(tree_version).get("nodes", {})

        overrides = tree_state.get("overrides", {})
        dynamic_map = {str(n["id"]): n for n in tree_state.get("dynamicNodes", [])}
        allocated_ids = [int(nid) for nid in tree_state.get("allocatedNodes", [])]
        allocated_set = set(allocated_ids)

        socket_index_by_node: dict[int, int] = {}
        for slot in items_res.get("slots", []):
            if slot.get("nodeId") and slot.get("socketIndex"):
                socket_index_by_node[int(slot["nodeId"])] = int(slot["socketIndex"])

        def _get_node_meta(nid: int) -> dict[str, Any]:
            sid = str(nid)
            base = dict(nodes_db.get(sid) or {})
            if sid in dynamic_map:
                base.update(dynamic_map[sid])
            if sid in overrides:
                ov = overrides[sid]
                base["overrideWhy"] = ov.get("why")
                for fld in ("name", "nameZh", "stats", "statsZh"):
                    if ov.get(fld):
                        base[fld] = ov[fld]
            base["id"] = nid
            base["allocated"] = nid in allocated_set
            if nid in socket_index_by_node:
                base["socketIndex"] = socket_index_by_node[nid]
            raw_base = nodes_db.get(sid) or {}
            if raw_base.get("masteryEffects") and not base.get("masteryEffects"):
                base["masteryEffects"] = raw_base["masteryEffects"]
            return base

        if is_filtered_mode:
            return _inspect_tree_filtered(
                worker,
                search=search,
                query_list=query_list,
                allowed_types=allowed_types,
                allocated=allocated,
                max_cost=max_cost,
                show_path=show_path,
                masteries=masteries,
                socket_radius=socket_radius,
                limit=int(limit),
                tree_version=tree_version,
                tree_state=tree_state,
                items_res=items_res,
                nodes_db=nodes_db,
                dynamic_map=dynamic_map,
                allocated_set=allocated_set,
                socket_index_by_node=socket_index_by_node,
                get_node_meta=_get_node_meta,
            )

        keystones: list[dict[str, Any]] = []
        notables: list[dict[str, Any]] = []
        masteries_list: list[dict[str, Any]] = []
        ascendancy_nodes: list[dict[str, Any]] = []

        for nid in sorted(allocated_ids):
            meta = _get_node_meta(nid)
            ntype = meta.get("type")
            if meta.get("asc") or meta.get("ascendancyName"):
                if ntype not in ("AscendClassStart", "ClassStart"):
                    ascendancy_nodes.append(meta)
            elif ntype == "Keystone":
                keystones.append(meta)
            elif ntype == "Notable":
                notables.append(meta)
            elif ntype == "Mastery" or meta.get("overrideWhy") == "mastery":
                masteries_list.append(meta)

        jewel_sockets = [
            {
                **sock,
                "socketIndex": socket_index_by_node.get(int(sock["nodeId"])),
                "allocated": int(sock["nodeId"]) in allocated_set,
            }
            for sock in tree_state.get("sockets", [])
        ]

        return {
            "treeVersion": tree_version,
            "className": tree_state.get("className"),
            "ascendClassName": tree_state.get("ascendClassName"),
            "points": tree_state.get("points"),
            "allocatedCount": len(allocated_ids),
            "keystones": keystones,
            "ascendancyNodes": ascendancy_nodes,
            "masteries": masteries_list,
            "notables": notables,
            "jewelSockets": jewel_sockets,
        }
