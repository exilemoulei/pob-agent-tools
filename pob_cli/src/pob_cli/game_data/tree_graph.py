"""Pure-Python passive tree topology, wheel grouping, and Tarjan branch-pruning graph (L1)."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

_HIGHWAY_STAT_RE = re.compile(
    r"^\+\d+\s+to\s+(?:Strength|Dexterity|Intelligence)(?:\s+and\s+(?:Strength|Dexterity|Intelligence))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WheelInfo:
    """Cohesive passive tree wheel (notables + smalls + optional mastery) separated from highway travel."""

    wheel_id: str
    name: str
    name_zh: str | None
    group_id: Any
    node_ids: frozenset[int]
    notable_ids: tuple[int, ...]
    mastery_ids: tuple[int, ...]
    keystone_ids: tuple[int, ...]
    is_cluster: bool = False


@dataclass(frozen=True)
class PrunableBranch:
    """Connectivity-preserving removable branch of allocated main-tree / cluster passives."""

    key: str
    node_ids: tuple[int, ...]
    points: int
    kind: str  # "empty_socket" | "mastery" | "twig" | "wheel_spur" | "wheel" | "leaf"
    wheel_name: str | None
    wheel_name_zh: str | None
    notable_names: tuple[str, ...]
    notable_names_zh: tuple[str, ...]
    includes_mastery_ids: tuple[int, ...]
    includes_empty_socket_ids: tuple[int, ...]
    includes_occupied_socket_ids: tuple[int, ...]


@dataclass
class PassiveGraph:
    """Undirected passive tree graph aligned with PoB PassiveSpec traversal rules."""

    nodes: dict[int, dict[str, Any]]
    adj: dict[int, tuple[int, ...]]
    mastery_to_notables: dict[int, tuple[int, ...]] = field(default_factory=dict)
    notable_to_masteries: dict[int, tuple[int, ...]] = field(default_factory=dict)
    wheels: dict[str, WheelInfo] = field(default_factory=dict)
    node_to_wheel: dict[int, str] = field(default_factory=dict)
    highway_nodes: set[int] = field(default_factory=set)

    def node_type(self, nid: int) -> str:
        nd = self.nodes.get(nid)
        return str((nd.get("type") if isinstance(nd, dict) else None) or "Normal")

    def is_non_transit(self, nid: int) -> bool:
        """Return True for nodes that cannot be traversed *through* (Mastery, ClassStart, AscendClassStart)."""
        return self.node_type(nid) in ("Mastery", "ClassStart", "AscendClassStart")

    def is_ascendancy(self, nid: int) -> bool:
        nd = self.nodes.get(nid)
        return bool(nd and (nd.get("asc") or nd.get("ascendancyName")))


def _is_excluded_node(nd: dict[str, Any]) -> bool:
    if nd.get("isProxy") or nd.get("proxy"):
        return True
    if nd.get("charm") or nd.get("charmSocket"):
        return True
    nm = str(nd.get("name") or nd.get("dn") or "")
    return nm == "Charm Socket"


def _iter_raw_neighbors(nd: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for key in ("linked", "links", "out", "in"):
        vals = nd.get(key)
        if not isinstance(vals, (list, tuple)):
            continue
        for v in vals:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
    return out


def _is_pure_attribute_travel(nd: dict[str, Any]) -> bool:
    if str(nd.get("type") or "Normal") != "Normal":
        return False
    stats = nd.get("stats") or []
    if not isinstance(stats, list) or not stats:
        return False
    cleaned = [" ".join(str(s).split()) for s in stats if str(s).strip()]
    return bool(cleaned) and all(_HIGHWAY_STAT_RE.match(s) for s in cleaned)


def build_passive_graph(
    nodes_db: dict[str, Any],
    dynamic_nodes: list[dict[str, Any]] | dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> PassiveGraph:
    """Build fast (<5ms) undirected passive graph over main-tree + active cluster subgraphs."""
    nodes: dict[int, dict[str, Any]] = {}
    ov_map = overrides if isinstance(overrides, dict) else {}

    for sid, raw in (nodes_db or {}).items():
        if not isinstance(raw, dict):
            continue
        try:
            nid = int(raw.get("id", sid))
        except (TypeError, ValueError):
            continue
        if _is_excluded_node(raw):
            continue
        eff = dict(raw)
        eff["id"] = nid
        ov = ov_map.get(str(nid))
        if isinstance(ov, dict):
            for k in ("name", "nameZh", "stats", "statsZh", "why"):
                if ov.get(k):
                    eff[k] = ov[k]
        nodes[nid] = eff

    dyn_iter: list[dict[str, Any]] = []
    if isinstance(dynamic_nodes, dict):
        dyn_iter = [v for v in dynamic_nodes.values() if isinstance(v, dict)]
    elif isinstance(dynamic_nodes, list):
        dyn_iter = [v for v in dynamic_nodes if isinstance(v, dict)]
    for raw in dyn_iter:
        if "id" not in raw or _is_excluded_node(raw):
            continue
        try:
            nid = int(raw["id"])
        except (TypeError, ValueError):
            continue
        eff = dict(nodes.get(nid) or {})
        eff.update(raw)
        eff["id"] = nid
        eff["_isDynamic"] = True
        nodes[nid] = eff

    adj_sets: dict[int, set[int]] = {nid: set() for nid in nodes}
    group_notables: dict[Any, list[int]] = {}
    mastery_ids: list[int] = []

    for nid, nd in nodes.items():
        ntype = str(nd.get("type") or "Normal")
        u_asc = nd.get("asc") or nd.get("ascendancyName")
        grp = nd.get("group")
        if grp is not None:
            if ntype == "Notable":
                group_notables.setdefault(grp, []).append(nid)
            elif ntype == "Mastery":
                mastery_ids.append(nid)
        for nb in _iter_raw_neighbors(nd):
            if nb == nid or nb not in nodes:
                continue
            nb_nd = nodes[nb]
            v_asc = nb_nd.get("asc") or nb_nd.get("ascendancyName")
            # Block cross-ascendancy edges and main-tree <-> ascendancy bridges.
            if (u_asc or v_asc) and u_asc != v_asc:
                continue
            adj_sets[nid].add(nb)
            adj_sets[nb].add(nid)

    # Ensure every Mastery links to its group's notables (PoB links Masteries by group membership).
    mastery_to_notables: dict[int, tuple[int, ...]] = {}
    notable_to_masteries_sets: dict[int, set[int]] = {}
    for mid in mastery_ids:
        grp = nodes[mid].get("group")
        nots = sorted(set(group_notables.get(grp, ())))
        if not nots:
            nots = sorted(nb for nb in adj_sets[mid] if str(nodes[nb].get("type") or "") == "Notable")
        for not_id in nots:
            adj_sets[mid].add(not_id)
            adj_sets[not_id].add(mid)
            notable_to_masteries_sets.setdefault(not_id, set()).add(mid)
        mastery_to_notables[mid] = tuple(nots)

    notable_to_masteries = {k: tuple(sorted(v)) for k, v in notable_to_masteries_sets.items()}
    adj = {nid: tuple(sorted(nbs)) for nid, nbs in adj_sets.items()}
    graph = PassiveGraph(
        nodes=nodes,
        adj=adj,
        mastery_to_notables=mastery_to_notables,
        notable_to_masteries=notable_to_masteries,
    )
    group_wheels(graph)
    return graph


def group_wheels(graph: PassiveGraph) -> dict[str, WheelInfo]:
    """Group main-tree and cluster passives into named Wheels, separating pure highway travel nodes."""
    by_group: dict[Any, list[int]] = {}
    highway_nodes: set[int] = set()

    for nid, nd in graph.nodes.items():
        if graph.is_ascendancy(nid):
            continue
        ntype = str(nd.get("type") or "Normal")
        if ntype in ("ClassStart", "AscendClassStart"):
            highway_nodes.add(nid)
            continue
        grp = nd.get("group")
        if grp is None:
            if _is_pure_attribute_travel(nd):
                highway_nodes.add(nid)
            continue
        by_group.setdefault(grp, []).append(nid)

    wheels: dict[str, WheelInfo] = {}
    node_to_wheel: dict[int, str] = {}

    for grp, members in by_group.items():
        member_set = set(members)
        notables = sorted(n for n in members if graph.node_type(n) == "Notable")
        masteries = sorted(n for n in members if graph.node_type(n) == "Mastery")
        keystones = sorted(n for n in members if graph.node_type(n) == "Keystone")
        is_cluster = any(bool(graph.nodes[n].get("_isDynamic")) for n in members)

        if not notables and not masteries and not keystones:
            # Travel-only / socket-only group -> mark non-socket normals as highway travel.
            for n in members:
                if graph.node_type(n) == "Normal":
                    highway_nodes.add(n)
            continue

        # Exclude pure attribute highway nodes that happen to share a group ID unless they connect
        # solely within this wheel's non-highway nodes.
        wheel_members: set[int] = set()
        for n in members:
            nd = graph.nodes[n]
            if _is_pure_attribute_travel(nd):
                ext_nbs = [nb for nb in graph.adj.get(n, ()) if nb not in member_set]
                if ext_nbs:
                    highway_nodes.add(n)
                    continue
            wheel_members.add(n)

        if not wheel_members:
            continue

        # Split disconnected components within the same group ID (if any).
        visited: set[int] = set()
        comp_idx = 0
        for seed in sorted(wheel_members):
            if seed in visited:
                continue
            comp: set[int] = set()
            q: deque[int] = deque([seed])
            visited.add(seed)
            while q:
                cur = q.popleft()
                comp.add(cur)
                for nb in graph.adj.get(cur, ()):
                    if nb in wheel_members and nb not in visited:
                        visited.add(nb)
                        q.append(nb)

            c_nots = tuple(sorted(n for n in comp if graph.node_type(n) == "Notable"))
            c_masts = tuple(sorted(n for n in comp if graph.node_type(n) == "Mastery"))
            c_keys = tuple(sorted(n for n in comp if graph.node_type(n) == "Keystone"))
            if not c_nots and not c_masts and not c_keys:
                for n in comp:
                    if graph.node_type(n) == "Normal":
                        highway_nodes.add(n)
                continue

            comp_idx += 1
            wid = f"cluster:{grp}" if is_cluster else (f"g:{grp}" if comp_idx == 1 else f"g:{grp}:{comp_idx}")
            primary_ids = c_nots or c_keys or c_masts
            names_en = [str(graph.nodes[n].get("name") or n) for n in primary_ids[:2]]
            names_zh = [str(graph.nodes[n].get("nameZh") or "") for n in primary_ids[:2] if graph.nodes[n].get("nameZh")]
            w_name = " / ".join(names_en)
            w_name_zh = " / ".join(names_zh) if names_zh else None
            if is_cluster:
                w_name = f"Cluster ({w_name})"
                if w_name_zh:
                    w_name_zh = f"星团 ({w_name_zh})"
            wheels[wid] = WheelInfo(
                wheel_id=wid,
                name=w_name,
                name_zh=w_name_zh,
                group_id=grp,
                node_ids=frozenset(comp),
                notable_ids=c_nots,
                mastery_ids=c_masts,
                keystone_ids=c_keys,
                is_cluster=is_cluster,
            )
            for n in comp:
                node_to_wheel[n] = wid

    graph.wheels = wheels
    graph.node_to_wheel = node_to_wheel
    graph.highway_nodes = highway_nodes
    return wheels


def _connected_from_roots(
    graph: PassiveGraph,
    active_nodes: set[int],
    roots: list[int],
) -> set[int]:
    """BFS reachability over `active_nodes` from `roots` obeying PoB non-transit rules (no transit through Mastery/ClassStart)."""
    visited: set[int] = set()
    q: deque[int] = deque()
    for r in roots:
        if r in active_nodes and r not in visited:
            visited.add(r)
            q.append(r)
    while q:
        cur = q.popleft()
        cur_type = graph.node_type(cur)
        # Can leave ClassStart when it is a root, but never traverse *through* Mastery or another ClassStart.
        if cur_type == "Mastery":
            continue
        if cur_type in ("ClassStart", "AscendClassStart") and cur not in roots:
            continue
        for nb in graph.adj.get(cur, ()):
            if nb not in active_nodes or nb in visited:
                continue
            visited.add(nb)
            if graph.node_type(nb) not in ("Mastery", "ClassStart", "AscendClassStart"):
                q.append(nb)
    return visited


def _expand_removal_closure(
    graph: PassiveGraph,
    main_connected: set[int],
    roots: list[int],
    seed_remove: set[int],
) -> set[int] | None:
    """Expand `seed_remove` so remaining nodes stay 100% connected to `roots` AND obey the Mastery invariant."""
    removed = set(seed_remove) & main_connected
    if not removed or any(r in removed for r in roots):
        return None

    for _ in range(8):
        remaining = main_connected - removed
        reachable = _connected_from_roots(graph, remaining, roots)
        orphans = remaining - reachable
        extra_masteries: set[int] = set()
        for nid in reachable:
            if graph.node_type(nid) != "Mastery":
                continue
            wheel_nots = graph.mastery_to_notables.get(nid, ())
            if wheel_nots and not any(n in reachable for n in wheel_nots):
                extra_masteries.add(nid)
        added = orphans | extra_masteries
        if not added:
            break
        removed |= added
        if any(r in removed for r in roots):
            return None

    return removed


def _tarjan_articulation_and_pendants(
    graph: PassiveGraph,
    main_connected: set[int],
    root: int,
) -> tuple[set[int], list[set[int]]]:
    """Iterative Tarjan DFS on structural (non-Mastery) nodes rooted at `root`."""
    # Exclude Mastery nodes from structural DFS: Masteries are non-transit leaves whose validity
    # is enforced by `_expand_removal_closure` (at least one notable in the wheel remains allocated),
    # so a shared Mastery must never mark one of two wheel notables as an articulation point.
    structural = {n for n in main_connected if graph.node_type(n) != "Mastery"}
    if root not in structural:
        return set(), []
    disc: dict[int, int] = {}
    low: dict[int, int] = {}
    parent: dict[int, int | None] = {root: None}
    art_points: set[int] = set()
    children_map: dict[int, list[int]] = {}
    pendant_roots: list[int] = []
    timer = 0
    root_children = 0

    def _neighbors_for_dfs(u: int) -> list[int]:
        return [nb for nb in graph.adj.get(u, ()) if nb in structural]

    disc[root] = low[root] = timer
    timer += 1
    stack: list[tuple[int, list[int], int]] = [(root, _neighbors_for_dfs(root), 0)]

    while stack:
        u, nbs, idx = stack[-1]
        if idx < len(nbs):
            v = nbs[idx]
            stack[-1] = (u, nbs, idx + 1)
            if v not in disc:
                parent[v] = u
                children_map.setdefault(u, []).append(v)
                if u == root:
                    root_children += 1
                disc[v] = low[v] = timer
                timer += 1
                stack.append((v, _neighbors_for_dfs(v), 0))
            elif v != parent.get(u):
                if disc[v] < low[u]:
                    low[u] = disc[v]
        else:
            stack.pop()
            p = parent.get(u)
            if p is not None:
                if low[u] < low[p]:
                    low[p] = low[u]
                if p == root:
                    if root_children > 1:
                        art_points.add(root)
                    pendant_roots.append(u)
                elif low[u] >= disc[p]:
                    art_points.add(p)
                    pendant_roots.append(u)

    # Collect DFS subtree for each pendant root.
    subtrees: list[set[int]] = []
    for pr in pendant_roots:
        sub: set[int] = set()
        q: deque[int] = deque([pr])
        while q:
            cur = q.popleft()
            if cur in sub:
                continue
            sub.add(cur)
            for ch in children_map.get(cur, ()):
                q.append(ch)
        if sub:
            subtrees.append(sub)
    return art_points, subtrees


def find_prunable_branches(
    graph: PassiveGraph,
    allocated_ids: set[int] | list[int],
    *,
    occupied_socket_ids: set[int] | list[int] | None = None,
    empty_socket_ids: set[int] | list[int] | None = None,
    max_branch_pts: int = 5,
    include_occupied_sockets: bool = False,
) -> list[PrunableBranch]:
    """Find all connectivity-preserving removable branches (`<= max_branch_pts`) via Tarjan articulation analysis."""
    alloc_set = {int(x) for x in allocated_ids if int(x) in graph.nodes and not graph.is_ascendancy(int(x))}
    if not alloc_set or max_branch_pts <= 0:
        return []

    occ_set = {int(x) for x in (occupied_socket_ids or ())}
    empty_set = {int(x) for x in (empty_socket_ids or ())}
    # Also mark any active cluster jewel parent socket as occupied.
    for nid in alloc_set:
        nd = graph.nodes[nid]
        if nd.get("_isDynamic"):
            for nb in graph.adj.get(nid, ()):
                if nb in alloc_set and graph.node_type(nb) == "Socket" and not graph.nodes[nb].get("_isDynamic"):
                    occ_set.add(nb)

    roots = sorted(n for n in alloc_set if graph.node_type(n) == "ClassStart")
    if not roots:
        # Synthetic graph without ClassStart: root at lowest ID in largest connected component.
        best_comp: set[int] = set()
        seen_all: set[int] = set()
        for n in sorted(alloc_set):
            if n in seen_all:
                continue
            comp = _connected_from_roots(graph, alloc_set, [n])
            seen_all |= comp
            if len(comp) > len(best_comp):
                best_comp = comp
        if not best_comp:
            return []
        roots = [min(best_comp)]

    main_connected = _connected_from_roots(graph, alloc_set, roots)
    if len(main_connected) <= len(roots):
        return []

    art_points, pendant_subtrees = _tarjan_articulation_and_pendants(graph, main_connected, roots[0])

    raw_candidates: list[tuple[set[int], str]] = []
    # 1. Empty allocated sockets (single node + minimal spur closure).
    for es in sorted(empty_set & main_connected):
        raw_candidates.append(({es}, "empty_socket"))
    # 2. Tarjan pendant subtrees (dead-end twigs and wheel spurs cut at articulation points).
    for sub in pendant_subtrees:
        if 1 <= len(sub) <= max_branch_pts:
            kind = "empty_socket" if (sub & empty_set and len(sub) == 1) else (
                "wheel_spur" if any(n in graph.node_to_wheel for n in sub) else "twig"
            )
            raw_candidates.append((sub, kind))
    # 3. Single non-articulation nodes (single extra notable, mastery, leaf, or socket on a ring).
    for nid in sorted(main_connected):
        if nid in roots or nid in art_points:
            continue
        ntype = graph.node_type(nid)
        if ntype == "Mastery":
            kind = "mastery"
        elif nid in empty_set:
            kind = "empty_socket"
        else:
            kind = "leaf"
        raw_candidates.append(({nid}, kind))
    # 4. Whole allocated wheels within max_branch_pts (e.g., pruning an entire wheel cleanly).
    wheel_alloc: dict[str, set[int]] = {}
    for nid in main_connected:
        wid = graph.node_to_wheel.get(nid)
        if wid:
            wheel_alloc.setdefault(wid, set()).add(nid)
    for _wid, w_nodes in wheel_alloc.items():
        if 1 <= len(w_nodes) <= max_branch_pts:
            raw_candidates.append((w_nodes, "wheel"))

    seen_sets: set[frozenset[int]] = set()
    branches: list[PrunableBranch] = []
    kind_prio = {"empty_socket": 0, "mastery": 1, "wheel": 2, "wheel_spur": 3, "twig": 4, "leaf": 5}
    raw_candidates.sort(key=lambda pair: (kind_prio.get(pair[1], 9), len(pair[0]), sorted(pair[0])))

    for seed_set, cand_kind in raw_candidates:
        closed = _expand_removal_closure(graph, main_connected, roots, seed_set)
        if not closed or len(closed) > max_branch_pts:
            continue
        occ_hit = tuple(sorted(closed & occ_set))
        if occ_hit and not include_occupied_sockets:
            continue
        key_set = frozenset(closed)
        if key_set in seen_sets:
            continue
        seen_sets.add(key_set)

        ordered_ids = tuple(sorted(closed))
        m_ids = tuple(n for n in ordered_ids if graph.node_type(n) == "Mastery")
        emp_ids = tuple(n for n in ordered_ids if n in empty_set)

        w_counts: dict[str, int] = {}
        for n in ordered_ids:
            wid = graph.node_to_wheel.get(n)
            if wid:
                w_counts[wid] = w_counts.get(wid, 0) + 1
        top_wid = max(w_counts, key=lambda k: (w_counts[k], k)) if w_counts else None
        w_info = graph.wheels.get(top_wid) if top_wid else None

        not_names: list[str] = []
        not_names_zh: list[str] = []
        for n in ordered_ids:
            if graph.node_type(n) in ("Notable", "Keystone", "Mastery"):
                nd = graph.nodes[n]
                not_names.append(str(nd.get("name") or n))
                if nd.get("nameZh"):
                    not_names_zh.append(str(nd["nameZh"]))

        eff_kind = cand_kind
        if emp_ids and len(ordered_ids) == 1:
            eff_kind = "empty_socket"
        elif m_ids and len(ordered_ids) == 1:
            eff_kind = "mastery"

        branches.append(
            PrunableBranch(
                key="rm:" + ",".join(str(x) for x in ordered_ids),
                node_ids=ordered_ids,
                points=len(ordered_ids),
                kind=eff_kind,
                wheel_name=w_info.name if w_info else None,
                wheel_name_zh=w_info.name_zh if w_info else None,
                notable_names=tuple(not_names),
                notable_names_zh=tuple(not_names_zh),
                includes_mastery_ids=m_ids,
                includes_empty_socket_ids=emp_ids,
                includes_occupied_socket_ids=occ_hit,
            )
        )

    branches.sort(key=lambda b: (0 if b.includes_empty_socket_ids else 1, b.points, b.node_ids))
    return branches
