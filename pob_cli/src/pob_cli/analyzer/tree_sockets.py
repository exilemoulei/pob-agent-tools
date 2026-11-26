"""Jewel & cluster socket classification, BFS reachability fallback, and N1-N4 noise-reduction rules."""

from __future__ import annotations

import math
from collections import deque
from typing import Any

_KIND_PRIORITY: dict[str, int] = {
    "medium_cluster_inner": 0,
    "basic": 1,
    "large_cluster_outer": 2,
    "small_cluster_inner": 3,
}

_KIND_LABELS: dict[str, str] = {
    "basic": "Jewel Socket",
    "large_cluster_outer": "Outer Large Cluster",
    "medium_cluster_inner": "Inner Medium Cluster",
    "small_cluster_inner": "Inner Small Cluster",
}


def sector_label_for_coords(x: float | None, y: float | None) -> str | None:
    """Map PoE1 tree coordinates (y downward) to the 6 outer cluster socket sectors."""
    if x is None or y is None:
        return None
    try:
        deg = math.degrees(math.atan2(float(y), float(x)))
    except (TypeError, ValueError):
        return None
    if -150.0 <= deg < -90.0:
        return "NW (Witch/Templar)"
    if -90.0 <= deg < -30.0:
        return "NE (Witch/Shadow)"
    if -30.0 <= deg < 30.0:
        return "E (Shadow/Ranger)"
    if 30.0 <= deg < 90.0:
        return "SE (Ranger/Duelist)"
    if 90.0 <= deg < 150.0:
        return "SW (Duelist/Marauder)"
    return "W (Marauder/Templar)"


def classify_socket(sock: dict[str, Any], node_meta: dict[str, Any] | None = None) -> str | None:
    """Return socket kind (`basic`, `large_cluster_outer`, `medium_cluster_inner`, `small_cluster_inner`) or None for Charm sockets."""
    if sock.get("charm"):
        return None
    if isinstance(node_meta, dict):
        if node_meta.get("charmSocket") or node_meta.get("asc") or node_meta.get("ascendancyName"):
            return None
        nm = str(node_meta.get("name") or "")
        if nm == "Charm Socket":
            return None
    if not sock.get("expansion"):
        return "basic"
    exp_size = sock.get("expansionSize")
    if exp_size == 2:
        return "large_cluster_outer"
    if exp_size == 1:
        return "medium_cluster_inner"
    return "small_cluster_inner"


def socket_display_kind(entry: dict[str, Any]) -> str:
    kind = str(entry.get("kind") or "basic")
    label = _KIND_LABELS.get(kind, kind)
    sector = entry.get("sector")
    return f"{label} ({sector})" if sector else label


def _node_neighbors(node: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for key in ("linked", "links", "out", "in"):
        vals = node.get(key)
        if not isinstance(vals, list):
            continue
        for v in vals:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
    return out


def _build_socket_bfs(
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
    allocated_set: set[int],
    *,
    max_depth: int,
) -> tuple[dict[int, int], dict[int, int | None]]:
    """Multi-source BFS from connected allocated passives (ignoring Mastery/ClassStart transit and isolated leap nodes)."""
    if not allocated_set or max_depth <= 0:
        return {}, {}

    def _get(nid: int) -> dict[str, Any] | None:
        sid = str(nid)
        return dynamic_map.get(sid) or nodes_db.get(sid)

    adj: dict[int, list[int]] = {}

    def _neighbors(nid: int) -> list[int]:
        cached = adj.get(nid)
        if cached is not None:
            return cached
        nd = _get(nid)
        nb = _node_neighbors(nd) if isinstance(nd, dict) else []
        adj[nid] = nb
        return nb

    # Restrict BFS roots to the connected component(s) containing a ClassStart/AscendClassStart
    # when present so Thread of Hope / Impossible Escape / Anoint isolated nodes never act as roots.
    start_roots = [
        nid
        for nid in allocated_set
        if isinstance(_get(nid), dict) and _get(nid).get("type") in ("ClassStart", "AscendClassStart")
    ]
    if start_roots:
        connected: set[int] = set(start_roots)
        q_conn: deque[int] = deque(start_roots)
        while q_conn:
            cur = q_conn.popleft()
            for nb in _neighbors(cur):
                if nb in allocated_set and nb not in connected:
                    connected.add(nb)
                    q_conn.append(nb)
        roots = connected
    else:
        roots = set(allocated_set)

    dist: dict[int, int] = {}
    parent: dict[int, int | None] = {}
    q: deque[int] = deque()
    for nid in sorted(roots):
        nd = _get(nid)
        if isinstance(nd, dict) and nd.get("type") == "Mastery":
            continue
        dist[nid] = 0
        parent[nid] = None
        q.append(nid)

    while q:
        cur = q.popleft()
        d = dist[cur]
        if d >= max_depth:
            continue
        for nb in _neighbors(cur):
            if nb in dist:
                continue
            nb_node = _get(nb)
            if not isinstance(nb_node, dict):
                continue
            ntype = nb_node.get("type")
            if ntype in ("ClassStart", "AscendClassStart", "Mastery"):
                continue
            if nb_node.get("asc") or nb_node.get("ascendancyName"):
                continue
            dist[nb] = d + 1
            parent[nb] = cur
            q.append(nb)
    return dist, parent


def _reconstruct_path_and_stats(
    target_id: int,
    parent: dict[int, int | None],
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
) -> tuple[list[int], list[str]]:
    path: list[int] = []
    cur: int | None = target_id
    while cur is not None and parent.get(cur) is not None:
        path.append(cur)
        cur = parent.get(cur)
    path.reverse()
    travel_stats: list[str] = []
    seen: set[str] = set()
    for nid in path:
        if nid == target_id:
            continue
        nd = dynamic_map.get(str(nid)) or nodes_db.get(str(nid)) or {}
        for ln in nd.get("stats") or []:
            clean = " ".join(str(ln).split())
            if clean and clean not in seen and len(travel_stats) < 4:
                seen.add(clean)
                travel_stats.append(clean)
    return path, travel_stats


def enrich_tree_sockets(
    tree_state: dict[str, Any],
    items_res: dict[str, Any],
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
    allocated_set: set[int],
    *,
    max_basic_dist: int = 3,
    max_outer_dist: int = 4,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Enrich `jewelSockets` with kind/sector/occupied/cost and compute `socketSummary` + `nearbySockets`."""
    socket_index_by_node: dict[int, int] = {}
    occupied_by_slot: dict[int, int] = {}
    for slot in items_res.get("slots", []):
        if slot.get("nodeId"):
            nid = int(slot["nodeId"])
            if slot.get("socketIndex"):
                socket_index_by_node[nid] = int(slot["socketIndex"])
            if slot.get("selItemId"):
                occupied_by_slot[nid] = int(slot["selItemId"])

    bfs_dist, bfs_parent = _build_socket_bfs(
        nodes_db, dynamic_map, allocated_set, max_depth=max(max_basic_dist, max_outer_dist)
    )

    jewel_sockets: list[dict[str, Any]] = []
    allocated_empty_ids: list[int] = []
    allocated_total = 0
    allocated_occupied = 0
    active_outer_clusters = 0
    nearby_sockets: list[dict[str, Any]] = []

    for sock in tree_state.get("sockets", []):
        nid = int(sock["nodeId"])
        meta = dynamic_map.get(str(nid)) or nodes_db.get(str(nid)) or {}
        kind = classify_socket(sock, meta)
        alloc = nid in allocated_set
        item_id = sock.get("itemId") or occupied_by_slot.get(nid)
        occupied = bool(item_id)
        sector = sector_label_for_coords(meta.get("x"), meta.get("y")) if kind == "large_cluster_outer" else None

        enriched: dict[str, Any] = {
            **sock,
            "socketIndex": socket_index_by_node.get(nid),
            "allocated": alloc,
            "occupied": occupied,
            "kind": kind or "charm",
        }
        if sector:
            enriched["sector"] = sector
        if not alloc and nid in bfs_dist:
            enriched["cost"] = bfs_dist[nid]
        jewel_sockets.append(enriched)

        if kind is None:
            continue
        if alloc:
            allocated_total += 1
            if occupied:
                allocated_occupied += 1
                if kind == "large_cluster_outer":
                    active_outer_clusters += 1
            else:
                allocated_empty_ids.append(nid)
                nearby_sockets.append(
                    {
                        "nodeId": nid,
                        "socketIndex": socket_index_by_node.get(nid),
                        "kind": kind,
                        "sector": sector,
                        "allocated": True,
                        "occupied": False,
                        "cost": 0,
                        "pathIds": [],
                        "travelStats": [],
                    }
                )
        else:
            d = bfs_dist.get(nid)
            lim = max_outer_dist if kind == "large_cluster_outer" else max_basic_dist
            if d is not None and 1 <= d <= lim:
                path_ids, travel_stats = _reconstruct_path_and_stats(nid, bfs_parent, nodes_db, dynamic_map)
                nearby_sockets.append(
                    {
                        "nodeId": nid,
                        "socketIndex": socket_index_by_node.get(nid),
                        "kind": kind,
                        "sector": sector,
                        "allocated": False,
                        "occupied": False,
                        "cost": d,
                        "pathIds": path_ids,
                        "travelStats": travel_stats,
                    }
                )

    allocated_empty_ids.sort()
    nearby_sockets.sort(key=lambda s: (int(s.get("cost") or 0), int(s.get("nodeId") or 0)))
    summary = {
        "allocatedTotal": allocated_total,
        "allocatedOccupied": allocated_occupied,
        "allocatedEmptyIds": allocated_empty_ids,
        "activeOuterClusters": active_outer_clusters,
    }
    return jewel_sockets, summary, nearby_sockets


def filter_sockets_for_view(
    nearby_sockets: list[dict[str, Any]],
    socket_summary: dict[str, Any] | None,
    *,
    mode: str = "rank_nodes",
    max_rows: int = 5,
    level: int | None = None,
    unspent_points: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Apply N1-N3 noise-reduction and priority sorting for Markdown views (`rank_nodes` / `diagnose`)."""
    summary = socket_summary or {}
    empty_ids = list(summary.get("allocatedEmptyIds") or [])
    active_outer = int(summary.get("activeOuterClusters") or 0)
    alloc_occ = int(summary.get("allocatedOccupied") or 0)
    unalloc = [s for s in (nearby_sockets or []) if not s.get("allocated")]

    # N1: in diagnose Markdown, >=2 empty allocated sockets fold new unallocated socket hints.
    if mode == "diagnose" and len(empty_ids) >= 2:
        ids_str = ", ".join(str(x) for x in empty_ids)
        return [], f"Fill existing {len(empty_ids)} empty allocated jewel sockets first (nodeId: {ids_str}) before opening new sockets."

    # N2: maxed & cluster-saturated builds only show <=2pt respec candidates in diagnose.
    if (
        mode == "diagnose"
        and unspent_points == 0
        and level is not None
        and level >= 98
        and (active_outer >= 2 or alloc_occ >= 5)
    ):
        unalloc = [s for s in unalloc if int(s.get("cost") or 99) <= 2]

    boost_outer = level is not None and level >= 75 and active_outer == 0
    boosted_id: int | None = None
    if boost_outer:
        outer_cands = [
            s for s in unalloc if s.get("kind") == "large_cluster_outer" and int(s.get("cost") or 99) <= 3
        ]
        if outer_cands:
            outer_cands.sort(key=lambda s: (int(s.get("cost") or 99), int(s.get("nodeId") or 0)))
            boosted_id = int(outer_cands[0]["nodeId"])

    ordered = sorted(
        unalloc,
        key=lambda s: (
            0 if boosted_id is not None and int(s.get("nodeId") or 0) == boosted_id else 1,
            int(s.get("cost") or 99),
            _KIND_PRIORITY.get(str(s.get("kind") or "basic"), 9),
            int(s.get("nodeId") or 0),
        ),
    )
    return ordered[:max_rows], None
