"""Pure-graph (<5ms) passive wheel package aggregation & Mastery Uniqueness Invariant."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any

from ..bridge_client import BridgeWorker
from ..game_data.tree_graph import PassiveGraph
from ..importers import share_code_to_xml
from .wheel_tags import BuildProfile, pick_best_mastery_effects, score_wheel_relevance

_DEFAULT_MAX_WHEELS = 36
_DEFAULT_MAX_PACKAGES = 56


def extract_used_mastery_effect_ids(
    worker: BridgeWorker,
    tree_state: dict[str, Any],
    nodes_db: dict[str, Any],
) -> set[int]:
    """Read all `effectId`s currently selected on allocated Mastery nodes (Mastery Uniqueness Invariant)."""
    used: set[int] = set()
    # Propagate BridgeError from RPC; only swallow XML decode/parse errors.
    exp = worker.call("export_code", {})
    if isinstance(exp, dict) and exp.get("code"):
        try:
            xml_txt = share_code_to_xml(str(exp["code"]))
            root = ET.fromstring(xml_txt.lstrip("\ufeff"))
            tree_el = root.find("Tree")
            if tree_el is not None:
                a_idx = max(1, int(tree_el.attrib.get("activeSpec", "1") or "1"))
                specs = tree_el.findall("Spec")
                if a_idx <= len(specs):
                    raw_me = specs[a_idx - 1].attrib.get("masteryEffects") or ""
                    for m in re.finditer(r"\{(\d+),(\d+)\}", raw_me):
                        used.add(int(m.group(2)))
        except (ValueError, KeyError, TypeError, ET.ParseError):
            pass

    overrides = tree_state.get("overrides") if isinstance(tree_state.get("overrides"), dict) else {}
    for nid in tree_state.get("allocatedNodes") or []:
        nd = nodes_db.get(str(nid)) or {}
        if nd.get("type") != "Mastery":
            continue
        ov = overrides.get(str(nid))
        if not isinstance(ov, dict):
            continue
        if ov.get("effectId") is not None:
            used.add(int(ov["effectId"]))
            continue
        ov_st = [" ".join(str(s).split()).lower() for s in (ov.get("stats") or [])]
        if ov_st:
            for eff in nd.get("masteryEffects") or []:
                if isinstance(eff, dict):
                    e_st = [" ".join(str(s).split()).lower() for s in (eff.get("stats") or [])]
                    if e_st == ov_st and eff.get("effect") is not None:
                        used.add(int(eff["effect"]))
                        break
    return used


def _bfs_from_connected_alloc(
    graph: PassiveGraph,
    alloc_set: set[int],
    *,
    max_dist: int,
) -> tuple[dict[int, int], dict[int, int | None]]:
    """Multi-source BFS from `ClassStart`-connected allocated main-tree nodes (respecting non-transit rules)."""
    main_alloc = {n for n in alloc_set if n in graph.nodes and not graph.is_ascendancy(n)}
    roots = [n for n in main_alloc if graph.node_type(n) == "ClassStart"]
    if roots:
        connected: set[int] = set(roots)
        qc: deque[int] = deque(roots)
        while qc:
            cur = qc.popleft()
            if graph.node_type(cur) == "Mastery":
                continue
            for nb in graph.adj.get(cur, ()):
                if nb in main_alloc and nb not in connected:
                    connected.add(nb)
                    if graph.node_type(nb) != "Mastery":
                        qc.append(nb)
    else:
        connected = main_alloc

    dist: dict[int, int] = {}
    parent: dict[int, int | None] = {}
    q: deque[int] = deque()
    for n in sorted(connected):
        if graph.node_type(n) == "Mastery":
            continue
        dist[n] = 0
        parent[n] = None
        q.append(n)

    while q:
        cur = q.popleft()
        d = dist[cur]
        if d >= max_dist:
            continue
        for nb in graph.adj.get(cur, ()):
            if nb in dist or graph.is_ascendancy(nb):
                continue
            ntype = graph.node_type(nb)
            if ntype in ("ClassStart", "AscendClassStart", "Mastery"):
                continue
            dist[nb] = d + 1
            parent[nb] = cur
            q.append(nb)
    return dist, parent


def _reconstruct_travel(target_id: int, parent: dict[int, int | None]) -> list[int]:
    path: list[int] = []
    cur: int | None = target_id
    while cur is not None and parent.get(cur) is not None:
        path.append(cur)
        cur = parent.get(cur)
    path.reverse()
    return path


def _shortest_path_in_wheel(
    graph: PassiveGraph,
    wheel_nodes: frozenset[int],
    seeds: set[int],
    goal: int,
) -> list[int] | None:
    if goal in seeds:
        return []
    dist: dict[int, int] = {s: 0 for s in seeds}
    prev: dict[int, int | None] = {s: None for s in seeds}
    q: deque[int] = deque(sorted(seeds))
    while q:
        cur = q.popleft()
        if cur == goal:
            out: list[int] = []
            c: int | None = goal
            while c is not None and prev.get(c) is not None:
                out.append(c)
                c = prev.get(c)
            out.reverse()
            return out
        for nb in graph.adj.get(cur, ()):
            if nb in wheel_nodes and nb not in dist and graph.node_type(nb) != "Mastery":
                dist[nb] = dist[cur] + 1
                prev[nb] = cur
                q.append(nb)
    return None


def _select_wheel_packages(
    entries: list[tuple[tuple[int, int, int, int, int, str], dict[str, Any] | None, list[dict[str, Any]]]],
    *,
    all_tags: bool,
    max_wheels: int | None,
    max_packages: int | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Apply relevance+distance pre-rank budget when `not all_tags`; `--all-tags` stays 100% uncapped."""
    if all_tags or (max_wheels is None and max_packages is None):
        all_pkgs: list[dict[str, Any]] = []
        for _, base_pkg, mastery_pkgs in entries:
            if base_pkg is not None:
                all_pkgs.append(base_pkg)
            all_pkgs.extend(mastery_pkgs)
        return all_pkgs, False

    total_pkgs = sum((1 if bp is not None else 0) + len(mp) for _, bp, mp in entries)
    ranked_entries = sorted(entries, key=lambda item: item[0])
    cap_applied = False
    if max_wheels is not None and len(ranked_entries) > max_wheels:
        ranked_entries = ranked_entries[:max_wheels]
        cap_applied = True

    packages: list[dict[str, Any]] = []
    extras_per_wheel: list[list[dict[str, Any]]] = []
    for _, base_pkg, mastery_pkgs in ranked_entries:
        primary = base_pkg if base_pkg is not None else mastery_pkgs[0]
        extras = mastery_pkgs if base_pkg is not None else mastery_pkgs[1:]
        if max_packages is not None and len(packages) >= max_packages:
            cap_applied = True
            break
        packages.append(primary)
        extras_per_wheel.append(extras)

    for m_idx in (0, 1):
        for extras in extras_per_wheel:
            if m_idx >= len(extras):
                continue
            if max_packages is not None and len(packages) >= max_packages:
                cap_applied = True
                break
            packages.append(extras[m_idx])

    return packages, cap_applied or (len(packages) < total_pkgs)


def build_wheel_packages(
    graph: PassiveGraph,
    alloc_set: set[int],
    used_mastery_effects: set[int],
    profile: BuildProfile,
    *,
    max_dist: int = 12,
    max_pts: int = 10,
    all_tags: bool = False,
    sort_by: str = "dps",
    max_wheels: int | None = _DEFAULT_MAX_WHEELS,
    max_packages: int | None = _DEFAULT_MAX_PACKAGES,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    """Bundle reachable unallocated/partial wheels into `(packages, total_wheels, filtered_wheels, cap_applied)` (<5ms)."""
    dist, parent = _bfs_from_connected_alloc(graph, alloc_set, max_dist=max_dist)
    total_wheels = 0
    filtered_wheels = 0
    wheel_entries: list[tuple[tuple[int, int, int, int, int, str], dict[str, Any] | None, list[dict[str, Any]]]] = []

    for wid, winfo in sorted(graph.wheels.items()):
        if winfo.is_cluster:
            continue
        unalloc_notables = [n for n in winfo.notable_ids if n not in alloc_set]
        unalloc_keystones = [n for n in winfo.keystone_ids if n not in alloc_set]
        unalloc_masteries = [n for n in winfo.mastery_ids if n not in alloc_set]
        core_targets = unalloc_notables or unalloc_keystones
        if not core_targets and not unalloc_masteries:
            continue
        alloc_in_wheel = set(winfo.node_ids) & alloc_set
        travel_ids: list[int] = []
        if alloc_in_wheel:
            seeds = {n for n in alloc_in_wheel if graph.node_type(n) != "Mastery"}
        else:
            non_m = [n for n in winfo.node_ids if graph.node_type(n) != "Mastery" and n in dist]
            if not non_m:
                continue
            entry = min(non_m, key=lambda n: (dist[n], n))
            if dist[entry] > max_dist:
                continue
            full_path = _reconstruct_travel(entry, parent)
            travel_ids = [n for n in full_path if n not in winfo.node_ids]
            seeds = {entry}

        total_wheels += 1
        rel_score = 1 if all_tags else score_wheel_relevance(graph, winfo, profile, sort_by=sort_by)
        if not all_tags and rel_score <= 0:
            continue
        filtered_wheels += 1

        chosen_wheel_nodes: list[int] = []
        chosen_set: set[int] = set(seeds)
        if not alloc_in_wheel:
            chosen_wheel_nodes.extend(sorted(seeds))

        remaining_targets = list(core_targets)
        while remaining_targets:
            best_t: int | None = None
            best_seg: list[int] | None = None
            for t in remaining_targets:
                seg = _shortest_path_in_wheel(graph, winfo.node_ids, chosen_set, t)
                if seg is None:
                    continue
                if best_seg is None or len(seg) < len(best_seg):
                    best_t, best_seg = t, seg
            if best_t is None or best_seg is None:
                break
            new_pts = len(travel_ids) + len(chosen_wheel_nodes) + len([x for x in best_seg if x not in chosen_set])
            if new_pts > max_pts:
                break
            for x in best_seg:
                if x not in chosen_set:
                    chosen_set.add(x)
                    chosen_wheel_nodes.append(x)
            remaining_targets.remove(best_t)

        base_node_ids = travel_ids + [n for n in chosen_wheel_nodes if n not in alloc_set]
        if (not base_node_ids and not unalloc_masteries) or len(base_node_ids) > max_pts:
            continue

        covered_notables = [n for n in base_node_ids if graph.node_type(n) in ("Notable", "Keystone")]
        has_notable_after = bool(covered_notables) or any(n in alloc_set for n in winfo.notable_ids)

        base_pkg: dict[str, Any] | None = None
        if base_node_ids:
            base_pkg = {
                "wheelId": wid,
                "wheel": winfo,
                "key": f"wheel:{wid}:base",
                "travelIds": list(travel_ids),
                "wheelNodeIds": [n for n in chosen_wheel_nodes if n not in alloc_set],
                "nodeIds": list(base_node_ids),
                "masteryId": None,
                "masteryEffect": None,
                "travelCost": len(travel_ids),
                "wheelCost": len(base_node_ids) - len(travel_ids),
                "totalPoints": len(base_node_ids),
            }

        mastery_pkgs: list[dict[str, Any]] = []
        if unalloc_masteries and has_notable_after and (len(base_node_ids) + 1) <= max_pts:
            mid = unalloc_masteries[0]
            m_node = graph.nodes.get(mid) or {}
            best_effs = pick_best_mastery_effects(m_node, used_mastery_effects, profile, max_effects=2)
            for eff in best_effs:
                eid = int(eff["effectId"])
                mastery_pkgs.append(
                    {
                        "wheelId": wid,
                        "wheel": winfo,
                        "key": f"wheel:{wid}:m:{mid}:{eid}",
                        "travelIds": list(travel_ids),
                        "wheelNodeIds": [n for n in chosen_wheel_nodes if n not in alloc_set],
                        "nodeIds": [*base_node_ids, mid],
                        "masteryId": mid,
                        "masteryEffect": eff,
                        "masteryEffects": {str(mid): eid},
                        "travelCost": len(travel_ids),
                        "wheelCost": (len(base_node_ids) - len(travel_ids)) + 1,
                        "totalPoints": len(base_node_ids) + 1,
                    }
                )

        if base_pkg is None and not mastery_pkgs:
            continue

        travel_cost = len(travel_ids)
        priority_score = rel_score - travel_cost * 3
        near_tier = 0 if travel_cost <= 4 else 1
        priority_key = (-priority_score, near_tier, travel_cost, -rel_score, len(base_node_ids) or 1, wid)
        wheel_entries.append((priority_key, base_pkg, mastery_pkgs))

    packages, cap_applied = _select_wheel_packages(
        wheel_entries,
        all_tags=all_tags,
        max_wheels=max_wheels,
        max_packages=max_packages,
    )
    return packages, total_wheels, filtered_wheels, cap_applied
