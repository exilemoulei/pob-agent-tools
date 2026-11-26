"""Structured passive tree diff (`pob tree diff`) between two slots or two `<Spec>` stages."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any

from ..bridge_client import BridgeWorker
from ..formatters import strip_pob_escapes
from ..game_data.respec_gold import estimate_respec_gold
from ..game_data.tree_graph import PassiveGraph, build_passive_graph
from ..importers import share_code_to_xml
from ..importers_stages import parse_xml_spec_snapshot
from ..snapshot import _call_batch
from .tree_sockets import enrich_tree_sockets

_CLUSTER_ENCHANT_RE = re.compile(r"Added Small Passive Skills grant:\s*(.+)", re.IGNORECASE)


def _extract_mastery_selections_from_state(
    tree_state: dict[str, Any],
    nodes_db: dict[str, Any],
    alloc_set: set[int],
    xml_masteries: dict[int, int] | None = None,
) -> dict[int, dict[str, Any]]:
    """Build `{nodeId: {nodeId, effectId, allocSpec, wheel, stats, statsZh}}` for allocated Masteries."""
    overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
    out: dict[int, dict[str, Any]] = {}
    for nid in sorted(alloc_set):
        nd = nodes_db.get(str(nid)) or {}
        if nd.get("type") != "Mastery":
            continue
        ov = overrides.get(str(nid)) if isinstance(overrides.get(str(nid)), dict) else {}
        eff_id = (xml_masteries or {}).get(nid)
        stats: list[str] = list(ov.get("stats") or [])
        stats_zh: list[str] = list(ov.get("statsZh") or [])

        # Match override stats against masteryEffects to recover effectId when not provided from XML.
        if eff_id is None and stats and isinstance(nd.get("masteryEffects"), list):
            norm_ov = [" ".join(str(s).split()).lower() for s in stats]
            for eff in nd["masteryEffects"]:
                if not isinstance(eff, dict):
                    continue
                e_st = [" ".join(str(s).split()).lower() for s in (eff.get("stats") or [])]
                if e_st and e_st == norm_ov:
                    eff_id = int(eff["effect"])
                    if not stats_zh and eff.get("statsZh"):
                        stats_zh = list(eff["statsZh"])
                    break
        elif eff_id is not None and not stats and isinstance(nd.get("masteryEffects"), list):
            for eff in nd["masteryEffects"]:
                if isinstance(eff, dict) and int(eff.get("effect") or -1) == eff_id:
                    stats = list(eff.get("stats") or [])
                    stats_zh = list(eff.get("statsZh") or [])
                    break

        alloc_spec = f"{nid}:{eff_id}" if eff_id is not None else str(nid)
        out[nid] = {
            "nodeId": nid,
            "effectId": eff_id,
            "allocSpec": alloc_spec,
            "name": strip_pob_escapes(str(nd.get("name") or f"Mastery {nid}")),
            "nameZh": strip_pob_escapes(str(nd.get("nameZh") or "")) or None,
            "stats": stats,
            "statsZh": stats_zh or None,
        }
    return out


def _hop_distances_from_alloc(graph: PassiveGraph, alloc_set: set[int]) -> dict[int, int]:
    """Multi-source BFS shortest hop distance from connected main-tree allocated nodes."""
    main_alloc = {n for n in alloc_set if n in graph.nodes and not graph.is_ascendancy(n)}
    if not main_alloc:
        return {}
    dist: dict[int, int] = {}
    q: deque[int] = deque()
    for n in sorted(main_alloc):
        if graph.node_type(n) == "Mastery":
            continue
        dist[n] = 0
        q.append(n)
    while q:
        cur = q.popleft()
        d = dist[cur]
        for nb in graph.adj.get(cur, ()):
            if nb in dist or graph.is_ascendancy(nb):
                continue
            if graph.node_type(nb) in ("ClassStart", "AscendClassStart"):
                continue
            dist[nb] = d + 1
            if graph.node_type(nb) != "Mastery":
                q.append(nb)
    return dist


def _cluster_notables_for_socket(
    socket_nid: int,
    graph: PassiveGraph,
    dyn_by_id: dict[int, dict[str, Any]],
    alloc_set: set[int],
) -> list[str]:
    """Trace connected cluster subgraph nodes (`id >= 65536` / dynamic) reachable from `socket_nid`."""
    if not dyn_by_id:
        return []
    visited: set[int] = {socket_nid}
    q: deque[int] = deque([socket_nid])
    found: list[str] = []
    while q:
        cur = q.popleft()
        for nb in graph.adj.get(cur, ()):
            if nb in visited:
                continue
            if nb not in dyn_by_id and nb < 65536:
                continue
            visited.add(nb)
            q.append(nb)
            dn = dyn_by_id.get(nb)
            if dn is not None and dn.get("type") == "Notable" and nb in alloc_set:
                found.append(strip_pob_escapes(str(dn.get("nameZh") or dn.get("name") or nb)))
    return found


def _extract_cluster_summary(
    jewel_sockets: list[dict[str, Any]],
    items_res: dict[str, Any],
    dynamic_nodes: list[dict[str, Any]],
    alloc_set: set[int],
    graph: PassiveGraph,
) -> list[dict[str, Any]]:
    items_by_id = {
        int(it["id"]): it
        for it in (items_res.get("items") or [])
        if isinstance(it, dict) and it.get("id") is not None
    }
    dyn_by_id = {
        int(dn["id"]): dn
        for dn in (dynamic_nodes or [])
        if isinstance(dn, dict) and dn.get("id") is not None
    }
    clusters: list[dict[str, Any]] = []
    for s in jewel_sockets:
        if s.get("kind") != "large_cluster_outer" or not s.get("allocated"):
            continue
        nid = int(s["nodeId"]) if s.get("nodeId") is not None else -1
        iid = s.get("itemId")
        item = items_by_id.get(int(iid)) if iid else None
        raw_txt = str((item or {}).get("raw") or "")
        enchant = None
        m_en = _CLUSTER_ENCHANT_RE.search(raw_txt)
        if m_en:
            enchant = m_en.group(1).strip()
        notables = _cluster_notables_for_socket(nid, graph, dyn_by_id, alloc_set) if (s.get("occupied") and nid > 0) else []
        clusters.append(
            {
                "nodeId": s.get("nodeId"),
                "sector": s.get("sector"),
                "occupied": bool(s.get("occupied")),
                "itemName": strip_pob_escapes(str((item or {}).get("title") or (item or {}).get("name") or s.get("name") or "")) or None,
                "enchant": enchant,
                "allocatedClusterNotables": notables,
            }
        )
    return clusters


def _node_record(nodes_db: dict[str, Any], nid: int) -> dict[str, Any]:
    nd = nodes_db.get(str(nid)) or {}
    return {
        "id": nid,
        "name": strip_pob_escapes(str(nd.get("name") or nid)),
        "nameZh": strip_pob_escapes(str(nd.get("nameZh") or "")) or None,
        "type": nd.get("type") or "Normal",
        "asc": nd.get("asc") or nd.get("ascendancyName"),
        "stats": list(nd.get("stats") or [])[:4],
        "statsZh": list(nd.get("statsZh") or [])[:4] or None,
    }


def _diff_snapshots(
    snap_a: dict[str, Any],
    snap_b: dict[str, Any],
    nodes_db: dict[str, Any],
    *,
    include_respec_gold: bool = True,
) -> dict[str, Any]:
    """Compute structured 5-section passive tree diff between snapshot A (`current`) and snapshot B (`ref`)."""
    alloc_a = {int(x) for x in snap_a.get("allocatedNodes", [])}
    alloc_b = {int(x) for x in snap_b.get("allocatedNodes", [])}

    graph_a = build_passive_graph(nodes_db, snap_a.get("dynamicNodes"), snap_a.get("overrides"))
    graph_b = build_passive_graph(nodes_db, snap_b.get("dynamicNodes"), snap_b.get("overrides"))
    base_graph = build_passive_graph(nodes_db)

    # 1. Point & Ascendancy summary
    asc_a = {n for n in alloc_a if n in base_graph.nodes and base_graph.is_ascendancy(n) and base_graph.node_type(n) not in ("AscendClassStart", "ClassStart")}
    asc_b = {n for n in alloc_b if n in base_graph.nodes and base_graph.is_ascendancy(n) and base_graph.node_type(n) not in ("AscendClassStart", "ClassStart")}
    main_a = {n for n in alloc_a if n not in asc_a and base_graph.node_type(n) not in ("ClassStart", "AscendClassStart")}
    main_b = {n for n in alloc_b if n not in asc_b and base_graph.node_type(n) not in ("ClassStart", "AscendClassStart")}

    summary = {
        "labelA": snap_a.get("label") or "current",
        "labelB": snap_b.get("label") or "ref",
        "pointsA": len(main_a),
        "pointsB": len(main_b),
        "pointsDelta": len(main_b) - len(main_a),
        "sharedPoints": len(main_a & main_b),
        "ascendancyAdded": [_node_record(nodes_db, n) for n in sorted(asc_b - asc_a)],
        "ascendancyRemoved": [_node_record(nodes_db, n) for n in sorted(asc_a - asc_b)],
    }
    respec_gold: dict[str, Any] | None = None
    if include_respec_gold:
        cls_a, cls_b = snap_a.get("classId"), snap_b.get("classId")
        same_class = (cls_a is None or cls_b is None or int(cls_a) == int(cls_b))
        if same_class:
            dealloc_reg = [n for n in (main_a - main_b) if n < 65536]
            dealloc_asc = [n for n in (asc_a - asc_b) if n < 65536]
            est = estimate_respec_gold(
                snap_a.get("characterLevel"),
                regular_nodes=len(dealloc_reg),
                ascendancy_nodes=len(dealloc_asc),
            )
            if est.get("estimatedGoldRespecTotal") is not None:
                respec_gold = est

    # 2. Keystones
    ks_a = {n for n in main_a if base_graph.node_type(n) == "Keystone"}
    ks_b = {n for n in main_b if base_graph.node_type(n) == "Keystone"}
    keystones = {
        "onlyInRef": [_node_record(nodes_db, n) for n in sorted(ks_b - ks_a)],
        "onlyInCurrent": [_node_record(nodes_db, n) for n in sorted(ks_a - ks_b)],
        "shared": [_node_record(nodes_db, n) for n in sorted(ks_a & ks_b)],
    }

    # 3. Core Wheels (main-tree wheels grouped via tree_graph + hop distance from A)
    hop_dist_a = _hop_distances_from_alloc(graph_a, alloc_a)
    only_ref_wheels: list[dict[str, Any]] = []
    only_cur_wheels: list[dict[str, Any]] = []
    partial_wheels: list[dict[str, Any]] = []

    for wid, winfo in sorted(base_graph.wheels.items()):
        in_a = sorted(winfo.node_ids & main_a)
        in_b = sorted(winfo.node_ids & main_b)
        if not in_a and not in_b:
            continue
        if in_a == in_b:
            continue
        nots_a = [strip_pob_escapes(str((nodes_db.get(str(n)) or {}).get("name") or n)) for n in in_a if base_graph.node_type(n) == "Notable"]
        nots_b = [strip_pob_escapes(str((nodes_db.get(str(n)) or {}).get("name") or n)) for n in in_b if base_graph.node_type(n) == "Notable"]
        if not in_a and in_b:
            dists = [hop_dist_a[n] for n in in_b if n in hop_dist_a]
            min_hop = min(dists) if dists else None
            only_ref_wheels.append(
                {
                    "wheelId": wid,
                    "name": winfo.name,
                    "nameZh": winfo.name_zh,
                    "points": len(in_b),
                    "nodeIds": in_b,
                    "notables": nots_b,
                    "shortestHopFromCurrent": min_hop,
                }
            )
        elif in_a and not in_b:
            only_cur_wheels.append(
                {
                    "wheelId": wid,
                    "name": winfo.name,
                    "nameZh": winfo.name_zh,
                    "points": len(in_a),
                    "nodeIds": in_a,
                    "notables": nots_a,
                }
            )
        else:
            added = sorted(set(in_b) - set(in_a))
            removed = sorted(set(in_a) - set(in_b))
            partial_wheels.append(
                {
                    "wheelId": wid,
                    "name": winfo.name,
                    "nameZh": winfo.name_zh,
                    "pointsA": len(in_a),
                    "pointsB": len(in_b),
                    "pointsDelta": len(in_b) - len(in_a),
                    "addedNodeIds": added,
                    "removedNodeIds": removed,
                    "addedNotables": [strip_pob_escapes(str((nodes_db.get(str(n)) or {}).get("name") or n)) for n in added if base_graph.node_type(n) == "Notable"],
                    "removedNotables": [strip_pob_escapes(str((nodes_db.get(str(n)) or {}).get("name") or n)) for n in removed if base_graph.node_type(n) == "Notable"],
                }
            )

    only_ref_wheels.sort(key=lambda w: (w["shortestHopFromCurrent"] if w["shortestHopFromCurrent"] is not None else 999, -w["points"], w["name"]))
    only_cur_wheels.sort(key=lambda w: (-w["points"], w["name"]))
    partial_wheels.sort(key=lambda w: (-abs(w["pointsDelta"]), w["name"]))

    hw_a = sorted(main_a & base_graph.highway_nodes)
    hw_b = sorted(main_b & base_graph.highway_nodes)
    wheels_diff = {
        "onlyInRef": only_ref_wheels,
        "onlyInCurrent": only_cur_wheels,
        "partialDeltas": partial_wheels,
        "highwayTravel": {
            "currentPoints": len(hw_a),
            "refPoints": len(hw_b),
            "delta": len(hw_b) - len(hw_a),
            "addedNodeIds": sorted(set(hw_b) - set(hw_a)),
            "removedNodeIds": sorted(set(hw_a) - set(hw_b)),
        },
    }

    # 4. Masteries
    m_a = _extract_mastery_selections_from_state(snap_a, nodes_db, alloc_a, snap_a.get("masterySelections"))
    m_b = _extract_mastery_selections_from_state(snap_b, nodes_db, alloc_b, snap_b.get("masterySelections"))
    for m_map in (m_a, m_b):
        for nid, entry in m_map.items():
            wid = base_graph.node_to_wheel.get(nid)
            winfo = base_graph.wheels.get(wid) if wid else None
            entry["wheel"] = winfo.name if winfo else None
            entry["wheelZh"] = winfo.name_zh if winfo else None

    m_only_ref = [m_b[n] for n in sorted(set(m_b) - set(m_a))]
    m_only_cur = [m_a[n] for n in sorted(set(m_a) - set(m_b))]
    m_changed = []
    for nid in sorted(set(m_a) & set(m_b)):
        ea, eb = m_a[nid], m_b[nid]
        diff_eff = (ea.get("effectId") != eb.get("effectId")) if (ea.get("effectId") is not None or eb.get("effectId") is not None) else (ea.get("stats") != eb.get("stats"))
        if diff_eff:
            m_changed.append({"nodeId": nid, "wheel": ea.get("wheel"), "current": ea, "ref": eb})
    masteries_diff = {
        "onlyInRef": m_only_ref,
        "onlyInCurrent": m_only_cur,
        "changedEffect": m_changed,
    }

    # 5. Cluster Jewels & Jewel Sockets
    dyn_a = {str(n["id"]): n for n in (snap_a.get("dynamicNodes") or []) if isinstance(n, dict) and "id" in n}
    dyn_b = {str(n["id"]): n for n in (snap_b.get("dynamicNodes") or []) if isinstance(n, dict) and "id" in n}
    socks_a, sum_a, _ = enrich_tree_sockets(snap_a, snap_a.get("itemsRes") or {}, nodes_db, dyn_a, alloc_a)
    socks_b, sum_b, _ = enrich_tree_sockets(snap_b, snap_b.get("itemsRes") or {}, nodes_db, dyn_b, alloc_b)
    alloc_sock_a = {int(s["nodeId"]) for s in socks_a if s.get("allocated") and s.get("kind") != "charm"}
    alloc_sock_b = {int(s["nodeId"]) for s in socks_b if s.get("allocated") and s.get("kind") != "charm"}
    sockets_diff = {
        "allocatedCountA": len(alloc_sock_a),
        "allocatedCountB": len(alloc_sock_b),
        "delta": len(alloc_sock_b) - len(alloc_sock_a),
        "onlyInRef": sorted(alloc_sock_b - alloc_sock_a),
        "onlyInCurrent": sorted(alloc_sock_a - alloc_sock_b),
        "summaryA": sum_a,
        "summaryB": sum_b,
        "outerClustersA": _extract_cluster_summary(
            socks_a, snap_a.get("itemsRes") or {}, snap_a.get("dynamicNodes") or [], alloc_a, graph_a
        ),
        "outerClustersB": _extract_cluster_summary(
            socks_b, snap_b.get("itemsRes") or {}, snap_b.get("dynamicNodes") or [], alloc_b, graph_b
        ),
    }

    return {
        "summary": summary,
        "respecGold": respec_gold,
        "keystones": keystones,
        "wheels": wheels_diff,
        "masteries": masteries_diff,
        "sockets": sockets_diff,
        "rawNodeDiff": {
            "onlyInRef": sorted(alloc_b - alloc_a),
            "onlyInCurrent": sorted(alloc_a - alloc_b),
            "sharedCount": len(alloc_a & alloc_b),
        },
    }


def _capture_worker_tree_snapshot(worker: BridgeWorker, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with worker.lock:
        ts, items_res = _call_batch(worker, [("get_tree_state", {}), ("list_items", {})])
        nodes_db = worker.get_cached_tree_data(ts.get("treeVersion")).get("nodes", {})
        # Extract masterySelections from XML if available for exact <nodeId>:<effectId>.
        # Propagate BridgeError from RPC; only swallow XML decode/parse failures.
        xml_masteries: dict[int, int] = {}
        char_level: int | None = None
        exp = worker.call("export_code", {})
        if isinstance(exp, dict) and exp.get("code"):
            try:
                xml_txt = share_code_to_xml(str(exp["code"]))
                root = ET.fromstring(xml_txt.lstrip("\ufeff"))
                build_el = root.find("Build")
                if build_el is not None and (build_el.attrib.get("level") or "").isdigit():
                    char_level = int(build_el.attrib["level"])
                tree_el = root.find("Tree")
                if tree_el is not None:
                    a_idx = max(1, int(tree_el.attrib.get("activeSpec", "1") or "1"))
                    specs = tree_el.findall("Spec")
                    if a_idx <= len(specs):
                        raw_me = specs[a_idx - 1].attrib.get("masteryEffects") or ""
                        for m in re.finditer(r"\{(\d+),(\d+)\}", raw_me):
                            xml_masteries[int(m.group(1))] = int(m.group(2))
            except (ValueError, KeyError, TypeError, ET.ParseError):
                pass
        if char_level is None:
            try:
                info = worker.call("get_build_info", {})
                if isinstance(info, dict) and info.get("level") is not None:
                    char_level = int(info["level"])
            except Exception:
                pass
        snap = {
            **ts,
            "label": label,
            "characterLevel": char_level,
            "itemsRes": items_res,
            "masterySelections": xml_masteries,
        }
        return snap, nodes_db


def diff_tree_slots(
    main_worker: BridgeWorker,
    ref_worker: BridgeWorker,
    *,
    label_a: str = "main",
    label_b: str = "ref",
) -> dict[str, Any]:
    """Diff passive trees of two loaded build slots (`current` vs `ref`)."""
    snap_a, nodes_db = _capture_worker_tree_snapshot(main_worker, label_a)
    snap_b, nodes_db_b = _capture_worker_tree_snapshot(ref_worker, label_b)
    merged_db = {**nodes_db_b, **nodes_db}
    res = _diff_snapshots(snap_a, snap_b, merged_db, include_respec_gold=False)
    res["mode"] = "slots"
    return res


def diff_tree_specs(
    worker: BridgeWorker,
    *,
    spec_a: str,
    spec_b: str,
) -> dict[str, Any]:
    """Diff two `<Spec>` stages within a single multi-stage build slot without mutating the slot."""
    with worker.lock:
        xml_str = ""
        try:
            xml_res = worker.call("save_xml", {})
            xml_str = str(xml_res.get("xml") or "")
        except Exception:
            xml_str = ""
        if not xml_str:
            code_res = worker.call("export_code", {})
            xml_str = share_code_to_xml(str(code_res.get("code") or ""))
        snap_a = parse_xml_spec_snapshot(xml_str, spec_a)
        snap_b = parse_xml_spec_snapshot(xml_str, spec_b)
        char_level: int | None = None
        try:
            b_el = ET.fromstring(xml_str.lstrip("\ufeff")).find("Build")
            if b_el is not None and (b_el.attrib.get("level") or "").isdigit():
                char_level = int(b_el.attrib["level"])
        except (ValueError, ET.ParseError):
            pass
        if char_level is None:
            try:
                info = worker.call("get_build_info", {})
                if isinstance(info, dict) and info.get("level") is not None:
                    char_level = int(info["level"])
            except Exception:
                pass
        snap_a["characterLevel"] = char_level
        snap_b["characterLevel"] = char_level
        tv = snap_a.get("treeVersion") or snap_b.get("treeVersion")
        nodes_db = worker.get_cached_tree_data(tv).get("nodes", {})
    # Populate default socket entries from nodes_db so enrich_tree_sockets classifies sockets accurately.
    for snap in (snap_a, snap_b):
        by_id = {int(s["nodeId"]): s for s in snap.get("sockets") or []}
        full_socks = []
        for sid, nd in nodes_db.items():
            if isinstance(nd, dict) and nd.get("type") == "Socket":
                nid = int(nd.get("id", sid))
                base_sk = by_id.get(nid) or {"nodeId": nid}
                full_socks.append(
                    {
                        **base_sk,
                        "expansion": bool(nd.get("expansion")),
                        "expansionSize": 2 if (nd.get("expansion") and not nd.get("expansionParent")) else 1,
                    }
                )
        snap["sockets"] = full_socks

    res = _diff_snapshots(snap_a, snap_b, nodes_db)
    res["mode"] = "specs"
    res["socketsNote"] = (
        "Intra-build Spec diff compares passive allocations only; "
        "equipped jewels/clusters reflect the slot's active ItemSet."
    )
    return res
