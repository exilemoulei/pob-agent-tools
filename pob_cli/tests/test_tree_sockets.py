"""Tests for item 73 Layer 1: jewel/cluster socket classification, BFS reachability,
N1-N4 noise-reduction rules, `pob tree` Markdown socket panel, and `node_power_kernel.lua`."""

from __future__ import annotations

from pathlib import Path

from pob_cli.analyzer.tree_sockets import (
    classify_socket,
    enrich_tree_sockets,
    filter_sockets_for_view,
    sector_label_for_coords,
)
from pob_cli.formatters.tree_md import format_tree_markdown


def test_socket_classification_and_six_outer_sectors() -> None:
    assert classify_socket({"nodeId": 1, "expansion": False}) == "basic"
    assert classify_socket({"nodeId": 2, "expansion": True, "expansionSize": 2}) == "large_cluster_outer"
    assert classify_socket({"nodeId": 3, "expansion": True, "expansionSize": 1}) == "medium_cluster_inner"
    assert classify_socket({"nodeId": 4, "expansion": True, "expansionSize": 0}) == "small_cluster_inner"
    assert classify_socket({"nodeId": 5, "charm": True}) is None
    assert classify_socket({"nodeId": 6, "expansion": False}, {"ascendancyName": "Primalist"}) is None

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
