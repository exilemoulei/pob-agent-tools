"""Passive tree conquered 0-stat dead-end and redundant cycle node audit (`Price of Glory`)."""

from __future__ import annotations

from collections import deque
from typing import Any

from ..formatters import strip_pob_escapes
from ..snapshot import _call_batch


def _parse_depends_ids(raw_deps: Any) -> set[int] | None:
    if not isinstance(raw_deps, list) or not raw_deps:
        return None
    out: set[int] = set()
    for item in raw_deps:
        try:
            if isinstance(item, dict) and "id" in item:
                out.add(int(item["id"]))
            else:
                out.add(int(item))
        except (TypeError, ValueError):
            return None
    return out if out else None


def _peel_allocated_dead_ends(
    allocated_set: set[int],
    removable_ids: set[int],
    nodes_db: dict[str, Any],
    dynamic_map: dict[str, Any],
) -> tuple[list[int], list[int]]:
    """Peel degree-<=1 removable nodes on the allocated subgraph; classify remaining as loop/parallel."""
    alloc_adj: dict[int, set[int]] = {aid: set() for aid in allocated_set}
    start_nodes: set[int] = set()

    for aid in allocated_set:
        sid = str(aid)
        for src_map in (nodes_db, dynamic_map):
            raw_node = src_map.get(sid)
            if not isinstance(raw_node, dict):
                continue
            if raw_node.get("type") in ("ClassStart", "AscendClassStart"):
                start_nodes.add(aid)
            for edge_key in ("linked", "links", "out", "in"):
                edges = raw_node.get(edge_key)
                if not isinstance(edges, (list, tuple)):
                    continue
                for raw_nb in edges:
                    try:
                        nb = int(raw_nb)
                    except (TypeError, ValueError):
                        continue
                    if nb != aid and nb in allocated_set:
                        alloc_adj[aid].add(nb)
                        alloc_adj[nb].add(aid)

    peeled: set[int] = set()
    # No edge known for a node (the tree data did not load): it cannot be called a dead end, and
    # is left to the one-at-a-time list, where PoB's own depends answer is all that is claimed.
    peelable = {nid for nid in removable_ids if alloc_adj.get(nid)}
    queue: deque[int] = deque(
        nid
        for nid in sorted(peelable)
        if nid not in start_nodes and len(alloc_adj.get(nid, ())) <= 1
    )
    while queue:
        cur = queue.popleft()
        if cur in peeled or cur in start_nodes or cur not in peelable:
            continue
        if len(alloc_adj.get(cur, ())) > 1:
            continue
        peeled.add(cur)
        neighbors = list(alloc_adj.get(cur, ()))
        alloc_adj[cur].clear()
        for nb in neighbors:
            alloc_adj[nb].discard(cur)
            if (
                nb in peelable
                and nb not in peeled
                and nb not in start_nodes
                and len(alloc_adj[nb]) <= 1
            ):
                queue.append(nb)

    dead_end_ids = sorted(peeled)
    redundant_loop_ids = sorted(removable_ids - peeled - start_nodes)
    return dead_end_ids, redundant_loop_ids


def audit_conquered_zero_stat_nodes(
    worker: Any,
    tree_state: dict[str, Any] | None,
    nodes_db: dict[str, Any] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """Detect conquered 0-stat small passives (`Price of Glory`) that are dead-ends or redundant cycle links."""
    if worker is None or not isinstance(tree_state, dict) or tree_state.get("_error"):
        return [], None

    overrides = tree_state.get("overrides")
    if not isinstance(overrides, dict) or not overrides:
        return [], None

    if nodes_db is None:
        try:
            tv = tree_state.get("treeVersion")
            nodes_db = (worker.get_cached_tree_data(tv) or {}).get("nodes", {})
        except Exception:
            nodes_db = {}
    nodes_db = nodes_db if isinstance(nodes_db, dict) else {}

    dynamic_map = {
        str(n["id"]): n
        for n in (tree_state.get("dynamicNodes") or [])
        if isinstance(n, dict) and "id" in n
    }

    allocated_set: set[int] = set()
    for raw_nid in tree_state.get("allocatedNodes") or []:
        try:
            allocated_set.add(int(raw_nid))
        except (TypeError, ValueError):
            continue
    if not allocated_set:
        return [], None

    zero_stat_ids: set[int] = set()
    zero_stat_names: dict[int, str] = {}
    for nid in sorted(allocated_set):
        sid = str(nid)
        ov = overrides.get(sid)
        if not isinstance(ov, dict):
            continue
        if ov.get("why") != "conquered" or "stats" not in ov or ov.get("stats") != [] or ov.get("statsZh"):
            continue
        nd = dict(nodes_db.get(sid) or {})
        if sid in dynamic_map:
            nd.update(dynamic_map[sid])
        ntype = nd.get("type") or "Normal"
        if ntype != "Normal" or nd.get("asc") or nd.get("ascendancyName"):
            continue
        zero_stat_ids.add(nid)
        nm = strip_pob_escapes(str(ov.get("nameZh") or ov.get("name") or nd.get("name") or "Price of Glory")).strip()
        zero_stat_names[nid] = nm or "Price of Glory"

    if not zero_stat_ids:
        return [], None

    sorted_zero = sorted(zero_stat_ids)
    hover_calls = [("node_hover", {"id": nid}) for nid in sorted_zero]
    try:
        hover_results = _call_batch(worker, hover_calls, raise_on_error=False)
    except AttributeError:
        hover_results = []
        for method, params in hover_calls:
            try:
                hover_results.append(worker.call(method, params))
            except Exception:
                hover_results.append({})
    except Exception:
        return [], None

    if not isinstance(hover_results, list):
        return [], None

    removable_ids: set[int] = set()
    for nid, hover in zip(sorted_zero, hover_results, strict=False):
        if not isinstance(hover, dict) or hover.get("_error") or hover.get("ok") is False:
            continue
        dep_set = _parse_depends_ids(hover.get("depends"))
        if dep_set is not None and dep_set.issubset(zero_stat_ids):
            removable_ids.add(nid)

    if not removable_ids:
        return [], None

    dead_end_ids, redundant_loop_ids = _peel_allocated_dead_ends(
        allocated_set, removable_ids, nodes_db, dynamic_map
    )
    if not dead_end_ids and not redundant_loop_ids:
        return [], None

    seen_names: list[str] = []
    for nid in (*dead_end_ids, *redundant_loop_ids):
        nm = zero_stat_names.get(nid, "Price of Glory")
        if nm not in seen_names:
            seen_names.append(nm)
    names_str = ", ".join(seen_names) or "Price of Glory"

    severity = "HIGH" if len(dead_end_ids) >= 3 else "MEDIUM"
    if dead_end_ids and redundant_loop_ids:
        dead_csv = ",".join(str(x) for x in dead_end_ids)
        dead_disp = ", ".join(str(x) for x in dead_end_ids)
        loop_disp = ", ".join(str(x) for x in redundant_loop_ids)
        msg = (
            f"Conquered 0-stat small passive waste ({names_str}): "
            f"{len(dead_end_ids)} dead-end node(s) [{dead_disp}] grant 0 stats and disconnect no real passives "
            f"(reclaim via `pob tree change --deallocate {dead_csv}`); "
            f"plus {len(redundant_loop_ids)} node(s) [{loop_disp}] on a loop: any one of them can go, "
            "but not all together (several removed at once can cut the loop and disconnect real passives). "
            "Deallocate one, then run the audit again."
        )
    elif dead_end_ids:
        dead_csv = ",".join(str(x) for x in dead_end_ids)
        dead_disp = ", ".join(str(x) for x in dead_end_ids)
        msg = (
            f"Conquered 0-stat small passive waste ({names_str}): "
            f"{len(dead_end_ids)} dead-end node(s) [{dead_disp}] grant 0 stats and disconnect no real passives — "
            f"reclaim {len(dead_end_ids)} passive point(s) via `pob tree change --deallocate {dead_csv}`."
        )
    else:
        loop_disp = ", ".join(str(x) for x in redundant_loop_ids)
        first_id = redundant_loop_ids[0]
        msg = (
            f"Conquered 0-stat small passive waste ({names_str}): "
            f"{len(redundant_loop_ids)} node(s) [{loop_disp}] grant 0 stats on a loop: any one of them can go, "
            "but not all together (several removed at once can cut the loop and disconnect real passives). "
            f"Start with `pob tree change --deallocate {first_id}`, then run the audit again."
        )

    finding = {"category": "TreeWaste", "severity": severity, "message": msg}
    waste_info = {
        "zeroStatIds": sorted_zero,
        "deadEndIds": dead_end_ids,
        # Each is removable alone; the set is not removable as a whole.
        "redundantLoopIds": redundant_loop_ids,
    }
    return [finding], waste_info
