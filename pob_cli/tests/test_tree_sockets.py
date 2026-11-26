"""Tests for item 73 Layer 1: jewel/cluster socket classification, BFS reachability,
N1-N4 noise-reduction rules, `pob tree` Markdown socket panel, and `node_power_kernel.lua`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from engine_paths import find_runnable_engine

from pob_cli.analyzer.tree_sockets import (
    classify_socket,
    enrich_tree_sockets,
    filter_sockets_for_view,
    sector_label_for_coords,
)
from pob_cli.bridge_client import BridgeWorker
from pob_cli.formatters.tree_md import format_tree_markdown
from pob_cli.importers import xml_to_share_code
from pob_cli.updater import is_valid_engine_dir


def _find_engine() -> Path | None:
    eng = find_runnable_engine()
    if eng is not None:
        return eng
    rt_env = os.environ.get("POB_RUNTIME_DIR", "").strip()
    if rt_env:
        cand = Path(rt_env) / "engine"
        if (cand / "PoE1" / "Data" / "Gems.lua").is_file() and is_valid_engine_dir(cand):
            return cand
    return None


_ENGINE_DIR = _find_engine()


def test_socket_classification_and_six_outer_sectors() -> None:
    assert classify_socket({"nodeId": 1, "expansion": False}) == "basic"
    assert classify_socket({"nodeId": 2, "expansion": True, "expansionSize": 2}) == "large_cluster_outer"
    assert classify_socket({"nodeId": 3, "expansion": True, "expansionSize": 1}) == "medium_cluster_inner"
    assert classify_socket({"nodeId": 4, "expansion": True, "expansionSize": 0}) == "small_cluster_inner"
    assert classify_socket({"nodeId": 5, "charm": True}) is None
    assert classify_socket({"nodeId": 6, "expansion": False}, {"ascendancyName": "Primalist"}) is None
    assert classify_socket({"nodeId": 7, "expansion": False}, {"dn": "Charm Socket"}) is None
    assert classify_socket({"nodeId": 8, "name": "Charm Socket"}) is None
    assert classify_socket({"nodeId": 9, "asc": True}) is None

    assert sector_label_for_coords(-5000, -8660) == "NW (Witch/Templar)"
    assert sector_label_for_coords(5000, -8660) == "NE (Witch/Shadow)"
    assert sector_label_for_coords(10000, 0) == "E (Shadow/Ranger)"
    assert sector_label_for_coords(5000, 8660) == "SE (Ranger/Duelist)"
    assert sector_label_for_coords(-5000, 8660) == "SW (Duelist/Marauder)"
    assert sector_label_for_coords(-10000, 0) == "W (Marauder/Templar)"


def test_enrich_tree_sockets_bfs_ignores_isolated_leap_nodes_and_mastery_transit() -> None:
    # Tree: ClassStart(1) - Normal(2) - Normal(3) - OuterCluster(7960, NE)
    #       Normal(2) - Mastery(9) - BasicSocket(10)  [must NOT pass through Mastery(9)!]
    #       IsolatedLeap(99, allocated) - BasicSocket(11) [must NOT route from isolated leap node!]
    #       Normal(2) - EmptyAllocSocket(20, allocated)
    #       Normal(2) - OccAllocSocket(21, allocated, itemId=5)
    nodes_db = {
        "1": {"id": 1, "type": "ClassStart", "links": [2]},
        "2": {"id": 2, "type": "Normal", "stats": ["+10 to Intelligence"], "links": [1, 3, 9, 20, 21]},
        "3": {"id": 3, "type": "Normal", "stats": ["+10 to Intelligence"], "links": [2, 7960]},
        "9": {"id": 9, "type": "Mastery", "links": [2, 10]},
        "10": {"id": 10, "type": "Socket", "links": [9]},
        "99": {"id": 99, "type": "Notable", "links": [11]},
        "11": {"id": 11, "type": "Socket", "links": [99]},
        "20": {"id": 20, "type": "Socket", "links": [2]},
        "21": {"id": 21, "type": "Socket", "links": [2]},
        "7960": {"id": 7960, "type": "Socket", "x": 5000.0, "y": -8660.0, "links": [3]},
        "18060": {"id": 18060, "type": "Socket", "charmSocket": True, "links": [2]},
    }
    tree_state = {
        "sockets": [
            {"nodeId": 20, "expansion": False},
            {"nodeId": 21, "expansion": False, "itemId": 5, "name": "Watcher's Eye"},
            {"nodeId": 7960, "expansion": True, "expansionSize": 2},
            {"nodeId": 10, "expansion": False},
            {"nodeId": 11, "expansion": False},
            {"nodeId": 18060, "expansion": False, "charm": True},
        ]
    }
    items_res = {"slots": [{"nodeId": 20, "socketIndex": 1, "selItemId": 0}, {"nodeId": 21, "socketIndex": 2, "selItemId": 5}]}
    allocated = {1, 2, 20, 21, 99}

    sockets, summary, nearby = enrich_tree_sockets(tree_state, items_res, nodes_db, {}, allocated)
    assert summary == {
        "allocatedTotal": 2,
        "allocatedOccupied": 1,
        "allocatedEmptyIds": [20],
        "activeOuterClusters": 0,
    }
    nearby_ids = [s["nodeId"] for s in nearby]
    assert nearby_ids == [20, 7960]
    outer = next(s for s in nearby if s["nodeId"] == 7960)
    assert outer["cost"] == 2
    assert outer["pathIds"] == [3, 7960]
    assert outer["travelStats"] == ["+10 to Intelligence"]
    assert outer["sector"] == "NE (Witch/Shadow)"
    # Charm socket excluded from nearbySockets
    assert all(s["nodeId"] != 18060 for s in nearby)
    assert any(s["nodeId"] == 21 and s["occupied"] is True for s in sockets)


def test_filter_sockets_n1_n2_n3_rules() -> None:
    sample = [
        {"nodeId": 100, "kind": "basic", "allocated": False, "cost": 2},
        {"nodeId": 200, "kind": "large_cluster_outer", "sector": "NE (Witch/Shadow)", "allocated": False, "cost": 3},
        {"nodeId": 300, "kind": "medium_cluster_inner", "allocated": False, "cost": 2},
    ]
    # N1: >=2 empty allocated sockets fold unallocated sockets in diagnose, not in rank_nodes
    rows_diag, fold_note = filter_sockets_for_view(sample, {"allocatedEmptyIds": [10, 11]}, mode="diagnose", max_rows=3)
    assert rows_diag == []
    assert fold_note and "10, 11" in fold_note
    rows_rank, fold_rank = filter_sockets_for_view(sample, {"allocatedEmptyIds": [10, 11]}, mode="rank_nodes", max_rows=5)
    assert len(rows_rank) == 3 and fold_rank is None

    # N2: maxed Lv98+ 0-unspent 2-cluster build hides cost>=3 in diagnose
    rows_n2, _ = filter_sockets_for_view(
        sample,
        {"allocatedEmptyIds": [], "activeOuterClusters": 2, "allocatedOccupied": 4},
        mode="diagnose",
        max_rows=3,
        level=99,
        unspent_points=0,
    )
    assert [r["nodeId"] for r in rows_n2] == [300, 100]

    # N3: level>=75 with 0 outer clusters boosts nearest <=3pt outer cluster to top
    rows_n3, _ = filter_sockets_for_view(
        sample,
        {"allocatedEmptyIds": [], "activeOuterClusters": 0, "allocatedOccupied": 2},
        mode="diagnose",
        max_rows=3,
        level=85,
        unspent_points=3,
    )
    assert rows_n3[0]["nodeId"] == 200


def test_format_tree_markdown_renders_jewel_sockets_panel() -> None:
    md = format_tree_markdown(
        {
            "points": {"used": 100, "usedMax": 120, "ascUsed": 8, "ascMax": 8},
            "keystones": [],
            "ascendancyNodes": [],
            "masteries": [],
            "notables": [],
            "jewelSockets": [
                {"nodeId": 20, "socketIndex": 1, "kind": "basic", "allocated": True, "occupied": False},
                {"nodeId": 21, "socketIndex": 2, "kind": "basic", "allocated": True, "occupied": True, "name": "Watcher's Eye"},
            ],
            "socketSummary": {"allocatedTotal": 2, "allocatedOccupied": 1, "allocatedEmptyIds": [20], "activeOuterClusters": 0},
            "nearbySockets": [
                {"nodeId": 20, "kind": "basic", "allocated": True, "occupied": False, "cost": 0},
                {"nodeId": 7960, "kind": "large_cluster_outer", "sector": "NE (Witch/Shadow)", "allocated": False, "cost": 2, "travelStats": ["+10 to Intelligence"]},
            ],
        }
    )
    assert "#### Jewel Sockets (Allocated: 2, Empty: 1 | Nearby <=3pt: 1)" in md
    assert "[EMPTY]" in md
    assert "Watcher's Eye" in md
    assert "7960" in md and "NE (Witch/Shadow)" in md


def test_node_power_kernel_lua_budget_and_contract() -> None:
    lua_dir = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "lua"
    npk = lua_dir / "node_power_kernel.lua"
    uk = lua_dir / "upstream_kernels.lua"
    assert npk.is_file() and uk.is_file()
    npk_txt = npk.read_text(encoding="utf-8")
    uk_txt = uk.read_text(encoding="utf-8")
    assert "collect_nearby_sockets" in npk_txt
    assert "nearbySockets = nearby_sockets" in npk_txt
    assert 'require("node_power_kernel")' in uk_txt
    assert "node_power = handle_node_power" in uk_txt
    assert "function handle_node_power" not in uk_txt
    assert "collect_nearby_sockets" not in uk_txt


def test_undirected_bfs_overrides_and_non_cluster_outer_occupant() -> None:
    # Node 2 does NOT list 3 in its `links` (one-sided edge on 3 -> [2, 7960]),
    # and node 3 has an override replacing its raw Intelligence travel stat.
    nodes_db = {
        "1": {"id": 1, "type": "ClassStart", "links": [2]},
        "2": {"id": 2, "type": "Normal", "links": [1]},
        "3": {"id": 3, "type": "Normal", "stats": ["+10 to Intelligence"], "links": [2, 7960]},
        "7960": {"id": 7960, "type": "Socket", "x": 5000.0, "y": -8660.0, "links": [3]},
        "21984": {"id": 21984, "type": "Socket", "x": 5000.0, "y": -8660.0, "links": [2]},
        "46882": {"id": 46882, "type": "Socket", "x": -5000.0, "y": -8660.0, "links": [2]},
    }
    tree_state = {
        "overrides": {"3": {"why": "tattoo", "stats": ["+4% to Chaos Resistance"]}},
        "sockets": [
            {"nodeId": 7960, "expansion": True, "expansionSize": 2},
            # Regular unique jewel in an outer Large Jewel Socket -> NOT an active cluster!
            {"nodeId": 21984, "expansion": True, "expansionSize": 2, "itemId": 1, "name": "Watcher's Eye", "baseName": "Prismatic Jewel"},
            # Actual Large Cluster Jewel in an outer socket -> activeOuterClusters += 1
            {"nodeId": 46882, "expansion": True, "expansionSize": 2, "itemId": 2, "name": "Cluster", "baseName": "Large Cluster Jewel"},
        ],
    }
    items_res = {
        "slots": [
            {"nodeId": 21984, "socketIndex": 1, "selItemId": 1},
            {"nodeId": 46882, "socketIndex": 2, "selItemId": 2},
        ]
    }
    _, summary, nearby = enrich_tree_sockets(tree_state, items_res, nodes_db, {}, {1, 2, 21984, 46882})
    assert summary["allocatedOccupied"] == 2
    assert summary["activeOuterClusters"] == 1
    outer = next(s for s in nearby if s["nodeId"] == 7960)
    assert outer["cost"] == 2
    assert outer["travelStats"] == ["+4% to Chaos Resistance"]


@pytest.mark.skipif(_ENGINE_DIR is None, reason="No runnable PoB engine found")
def test_real_engine_lua_and_python_socket_parity() -> None:
    assert _ENGINE_DIR is not None
    # Witch 3_30 path:
    # - 6230 allocated & empty basic socket
    # - 48768 unallocated basic socket at cost=1 (adjacent to 28221)
    # - 21984 unallocated NE outer Large Cluster socket at cost=3 ([22618, 21033, 21984] from 49605)
    # - 26725 isolated allocated node (must NOT create illegal nearby socket paths)
    alloc_nodes = "58833,55373,38048,55906,6230,15144,918,28221,24643,33545,18033,46092,51923,48778,37671,27415,32710,49605,26725"
    xml_base = f"""<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="{alloc_nodes}"/></Tree>
  <Skills activeSkillSet="1"><SkillSet id="1"><Skill mainActiveSkill="1" enabled="true" label="Main"><Gem nameSpec="Arc" level="20" quality="20" enabled="true"/></Skill></SkillSet></Skills>
  <Items activeItemSet="1"><ItemSet id="1"/></Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>"""

    w = BridgeWorker(_ENGINE_DIR)
    w.start()
    try:
        w.call("import_code", {"code": xml_to_share_code(xml_base), "mode": "replace"})
        np_res = w.call("node_power", {"stat": "CombinedDPS", "maxDepth": 4})
        ts = w.call("get_tree_state", {})
        it_res = w.call("list_items", {})
        nodes_db = w.get_cached_tree_data(ts.get("treeVersion")).get("nodes", {})
        dyn_map = {str(n["id"]): n for n in ts.get("dynamicNodes", [])}
        alloc_set = {int(x) for x in ts.get("allocatedNodes", [])}
        _, py_summary, py_nearby = enrich_tree_sockets(
            ts, it_res, nodes_db, dyn_map, alloc_set, max_basic_dist=4, max_outer_dist=4
        )

        assert np_res["socketSummary"] == py_summary
        lua_tuples = [
            (s["nodeId"], s["kind"], s.get("sector"), s["allocated"], s["occupied"], s["cost"])
            for s in np_res["nearbySockets"]
        ]
        py_tuples = [
            (s["nodeId"], s["kind"], s.get("sector"), s["allocated"], s["occupied"], s["cost"])
            for s in py_nearby
        ]
        assert lua_tuples == py_tuples
        assert (6230, "basic", None, True, False, 0) in lua_tuples
        assert (48768, "basic", None, False, False, 1) in lua_tuples
        assert (21984, "large_cluster_outer", "NE (Witch/Shadow)", False, False, 3) in lua_tuples

        # Allocate 21984 and socket a regular Cobalt Jewel -> activeOuterClusters MUST stay 0 on both Lua & Python!
        alloc_with_outer = f"{alloc_nodes},22618,21033,21984"
        xml_cobalt = f"""<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1">
    <Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="{alloc_with_outer}">
      <Sockets><Socket nodeId="21984" itemId="1"/></Sockets>
    </Spec>
  </Tree>
  <Skills activeSkillSet="1"><SkillSet id="1"><Skill mainActiveSkill="1" enabled="true" label="Main"><Gem nameSpec="Arc" level="20" quality="20" enabled="true"/></Skill></SkillSet></Skills>
  <Items activeItemSet="1">
    <Item id="1">Rarity: RARE
Mind Spark
Cobalt Jewel
+10 to Intelligence</Item>
    <ItemSet id="1"><SocketIdURL nodeId="21984" itemPbURL="" name="Jewel 21984" itemId="1"/></ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>"""
        w.call("import_code", {"code": xml_to_share_code(xml_cobalt), "mode": "replace"})
        np_cob = w.call("node_power", {"stat": "CombinedDPS", "maxDepth": 4})
        ts_cob = w.call("get_tree_state", {})
        it_cob = w.call("list_items", {})
        _, py_sum_cob, _ = enrich_tree_sockets(
            ts_cob, it_cob, nodes_db, {str(n["id"]): n for n in ts_cob.get("dynamicNodes", [])}, {int(x) for x in ts_cob.get("allocatedNodes", [])}
        )
        assert np_cob["socketSummary"]["activeOuterClusters"] == 0
        assert py_sum_cob["activeOuterClusters"] == 0

        # Socket a real Large Cluster Jewel into 21984 -> activeOuterClusters == 1 on both Lua & Python!
        xml_cluster = xml_cobalt.replace(
            "Cobalt Jewel\n+10 to Intelligence",
            "Large Cluster Jewel\nImplicits: 2\n{crafted}Adds 8 Passive Skills\n{crafted}2 Added Passive Skills are Jewel Sockets\n{crafted}Added Small Passive Skills grant: 12% increased Lightning Damage",
        )
        w.call("import_code", {"code": xml_to_share_code(xml_cluster), "mode": "replace"})
        np_clu = w.call("node_power", {"stat": "CombinedDPS", "maxDepth": 4})
        ts_clu = w.call("get_tree_state", {})
        it_clu = w.call("list_items", {})
        _, py_sum_clu, _ = enrich_tree_sockets(
            ts_clu, it_clu, nodes_db, {str(n["id"]): n for n in ts_clu.get("dynamicNodes", [])}, {int(x) for x in ts_clu.get("allocatedNodes", [])}
        )
        assert np_clu["socketSummary"]["activeOuterClusters"] == 1
        assert py_sum_clu["activeOuterClusters"] == 1
    finally:
        w.stop()
