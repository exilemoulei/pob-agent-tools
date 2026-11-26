"""Tests for Layer 1: passive tree topology graph, wheel grouping, Tarjan branch pruning,
mastery `nodeId:effectId` + ZH localization in `node_power`, and `eval_node_paths` CoW evaluator."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine, has_zh_data

from pob_cli.bridge_client import BridgeWorker
from pob_cli.cli import main
from pob_cli.comparator.ranking import rank_nodes
from pob_cli.game_data.tree_graph import (
    _connected_from_roots,
    build_passive_graph,
    find_prunable_branches,
)
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


def _sample_nodes_db() -> dict[str, dict[str, Any]]:
    """Synthetic passive tree with highway, 2 wheels (one with mastery), empty/occupied sockets, proxy, charm, ascendancy."""
    return {
        "1": {"id": 1, "type": "ClassStart", "name": "Witch", "links": [2, 10]},
        # Highway travel
        "2": {"id": 2, "type": "Normal", "name": "Intelligence", "stats": ["+10 to Intelligence"], "group": 100, "links": [1, 3, 20, 30, 40]},
        # Dead-end Wheel A (group 200): Small(3) -> Notable(4) + Mastery(5)
        "3": {"id": 3, "type": "Normal", "name": "Spell Damage", "stats": ["10% increased Spell Damage"], "group": 200, "links": [2, 4]},
        "4": {"id": 4, "type": "Notable", "name": "Arcane Dominion", "nameZh": "奥术主宰", "stats": ["20% increased Spell Damage"], "group": 200, "links": [3]},
        "5": {"id": 5, "type": "Mastery", "name": "Caster Mastery", "nameZh": "法术专精", "group": 200, "links": []},
        # Ring Wheel B (group 300) bridged back to 10 so removing one notable stays connected:
        # 1 - 10 - 11(Small) - 12(Notable1) - 13(Notable2) - 2
        "10": {"id": 10, "type": "Normal", "name": "Intelligence", "stats": ["+10 to Intelligence"], "group": 101, "links": [1, 11]},
        "11": {"id": 11, "type": "Normal", "name": "Lightning Damage", "stats": ["12% increased Lightning Damage"], "group": 300, "links": [10, 12]},
        "12": {"id": 12, "type": "Notable", "name": "Heart of Thunder", "nameZh": "雷霆之心", "stats": ["25% increased Lightning Damage"], "group": 300, "links": [11, 13]},
        "13": {"id": 13, "type": "Notable", "name": "Crackling Speed", "nameZh": "裂空之速", "stats": ["8% increased Cast Speed"], "group": 300, "links": [12, 2]},
        "14": {"id": 14, "type": "Mastery", "name": "Lightning Mastery", "group": 300, "links": []},
        # Empty allocated socket twig: 2 -> 20(empty socket)
        "20": {"id": 20, "type": "Socket", "name": "Jewel Socket", "links": [2]},
        # Occupied socket twig: 2 -> 30(occupied socket)
        "30": {"id": 30, "type": "Socket", "name": "Jewel Socket", "links": [2]},
        # Excluded proxy, charm socket, and ascendancy node
        "40": {"id": 40, "type": "Normal", "name": "Proxy", "isProxy": True, "links": [2, 41]},
        "41": {"id": 41, "type": "Normal", "name": "BehindProxy", "links": [40]},
        "50": {"id": 50, "type": "Socket", "name": "Charm Socket", "charmSocket": True, "links": [2]},
        "60": {"id": 60, "type": "Notable", "name": "Shaper of Flames", "asc": "Elementalist", "links": [2, 61]},
        "61": {"id": 61, "type": "Notable", "name": "Other Asc", "asc": "Occultist", "links": [60]},
    }


def test_build_passive_graph_excludes_proxy_charm_cross_asc_and_groups_wheels() -> None:
    graph = build_passive_graph(_sample_nodes_db())
    assert 40 not in graph.nodes
    assert 50 not in graph.nodes
    # Cross-ascendancy and main-to-ascendancy edges are blocked.
    assert 60 not in graph.adj[2]
    assert 61 not in graph.adj[60]
    # Mastery links to its wheel's notables even when raw links is empty.
    assert graph.mastery_to_notables[5] == (4,)
    assert graph.mastery_to_notables[14] == (12, 13)
    assert 4 in graph.adj[5]
    # Highway travel nodes separated from wheels.
    assert 2 in graph.highway_nodes
    assert 10 in graph.highway_nodes
    assert 4 in graph.node_to_wheel
    assert 12 in graph.node_to_wheel
    w_a = graph.wheels[graph.node_to_wheel[4]]
    assert "Arcane Dominion" in w_a.name
    assert 3 in w_a.node_ids and 4 in w_a.node_ids and 5 in w_a.node_ids
    assert 2 not in w_a.node_ids


def test_find_prunable_branches_preserves_connectivity_mastery_and_occupied_sockets() -> None:
    graph = build_passive_graph(_sample_nodes_db())
    allocated = {1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 20, 30}
    branches = find_prunable_branches(
        graph,
        allocated,
        occupied_socket_ids={30},
        empty_socket_ids={20},
        max_branch_pts=5,
        include_occupied_sockets=False,
    )
    branch_sets = {frozenset(b.node_ids): b for b in branches}

    # 1. Empty allocated socket sorts at the top and is extracted cleanly.
    assert branches[0].node_ids == (20,)
    assert branches[0].kind == "empty_socket"

    # 2. Occupied socket (30) is excluded by default.
    assert all(30 not in b.node_ids for b in branches)

    # 3. Mastery invariant: removing Notable 4 MUST bundle Mastery 5!
    assert frozenset({4}) not in branch_sets
    assert frozenset({4, 5}) in branch_sets
    assert branch_sets[frozenset({4, 5})].includes_mastery_ids == (5,)
    # Whole twig {3, 4, 5} is also prunable.
    assert frozenset({3, 4, 5}) in branch_sets
    # Removing ONLY Mastery 5 is valid on its own.
    assert frozenset({5}) in branch_sets
    assert branch_sets[frozenset({5})].kind == "mastery"

    # 4. On ring wheel B (two notables 12 & 13 + mastery 14):
    # Removing single notable 12 leaves notable 13 allocated, so mastery 14 does NOT need bundling.
    assert frozenset({12}) in branch_sets
    assert branch_sets[frozenset({12})].includes_mastery_ids == ()
    # Removing whole wheel B {11, 12, 13, 14} leaves 1-10 and 1-2 both connected!
    assert frozenset({11, 12, 13, 14}) in branch_sets

    # 5. EVERY returned branch MUST keep remaining allocated nodes 100% connected to ClassStart(1)
    # AND preserve the Mastery invariant!
    for br in branches:
        remaining = allocated - set(br.node_ids)
        reachable = _connected_from_roots(graph, remaining, [1])
        assert reachable == remaining, f"Branch {br.node_ids} disconnected remaining nodes: {remaining - reachable}"
        for mid, nots in graph.mastery_to_notables.items():
            if mid in remaining:
                assert any(n in remaining for n in nots), f"Branch {br.node_ids} orphaned mastery {mid}"

    # 6. Including occupied sockets exposes socket 30 when explicitly requested.
    branches_with_occ = find_prunable_branches(
        graph,
        allocated,
        occupied_socket_ids={30},
        empty_socket_ids={20},
        max_branch_pts=5,
        include_occupied_sockets=True,
    )
    assert any(b.node_ids == (30,) and b.includes_occupied_socket_ids == (30,) for b in branches_with_occ)


def test_rank_nodes_and_cli_surface_alloc_spec_for_masteries(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeWorker:
        def __init__(self) -> None:
            import threading

            self.lock = threading.RLock()

        def call(self, method: str, _params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            if method == "node_power":
                return {
                    "stat": "CombinedDPS",
                    "stats": ["CombinedDPS"],
                    "report": [
                        {
                            "id": 34723,
                            "effectId": 61097,
                            "allocSpec": "34723:61097",
                            "name": "Caster Mastery: +1 to Level of all Spell Skill Gems",
                            "nameZh": "法术专精: 所有法术技能宝石等级 +1",
                            "type": "Mastery",
                            "allocated": False,
                            "power": 0.12,
                            "powerStr": "+12.0%",
                            "pathPower": 0.12,
                            "pathPowerStr": "+12.0%",
                            "pathDist": 1,
                            "sd": ["+1 to Level of all Spell Skill Gems"],
                            "sdZh": ["所有法术技能宝石等级 +1"],
                        },
                        {
                            "id": 12345,
                            "name": "Arcane Will",
                            "type": "Notable",
                            "allocated": False,
                            "power": 0.08,
                            "powerStr": "+8.0%",
                            "pathPower": 0.04,
                            "pathPowerStr": "+4.0%",
                            "pathDist": 2,
                        },
                    ],
                }
            if method == "list_items":
                return {"slots": [], "items": []}
            return {}

    res = rank_nodes(_FakeWorker(), stat="CombinedDPS", max_dist=5, top=5)  # type: ignore[arg-type]
    assert res["byPerPoint"][0]["allocSpec"] == "34723:61097"
    assert res["byPerPoint"][1]["allocSpec"] == "12345"

    monkeypatch.setattr("pob_cli.cli._daemon_call", lambda action, _params=None, **_kw: res)
    runner = CliRunner()
    cli_res = runner.invoke(main, ["rank-nodes"])
    assert cli_res.exit_code == 0, cli_res.output
    assert "34723:61097" in cli_res.output
    assert "法术专精" in cli_res.output


def test_lua_tree_eval_kernel_mounted_in_headless_runner() -> None:
    lua_dir = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "lua"
    tek = lua_dir / "tree_eval_kernel.lua"
    hr = lua_dir / "headless_runner.lua"
    npk = lua_dir / "node_power_kernel.lua"
    assert tek.is_file()
    assert 'load_ext("tree_eval_kernel.lua")' in hr.read_text(encoding="utf-8")
    assert "eval_node_paths" in tek.read_text(encoding="utf-8")
    npk_txt = npk.read_text(encoding="utf-8")
    assert "nameZh = r_name_zh" in npk_txt
    assert "sdZh = r_sd_zh" in npk_txt


@pytest.mark.skipif(_ENGINE_DIR is None, reason="No runnable PoB engine found")
def test_real_engine_mastery_alloc_spec_zh_eval_node_paths_and_tarjan_graph() -> None:
    assert _ENGINE_DIR is not None
    # Witch 3_30 build with Spell Damage wheel (notable 55373 / Heart and Soul 38048 / Deep Wisdom 55906)
    # and empty basic socket 6230.
    alloc_nodes = "58833,55373,38048,55906,6230,15144,918,28221,24643,33545,18033,46092,51923,48778,37671,27415,32710,49605"
    xml_build = f"""<?xml version="1.0" encoding="UTF-8"?>
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
        assert w.caps.get("evalNodePaths") is True
        if has_zh_data(_ENGINE_DIR):
            w.call("set_locale", {"lang": "zh-CN"})
        w.call("import_code", {"code": xml_to_share_code(xml_build), "mode": "replace"})

        # 1. Verify node_power mastery entries have effectId, allocSpec, sd, and ZH fields.
        np_res = w.call("node_power", {"stat": "CombinedDPS", "maxDepth": 4})
        masteries = [r for r in np_res.get("report", []) if r.get("type") == "Mastery" and not r.get("allocated")]
        assert masteries, "Expected at least one reachable unallocated Mastery in node_power"
        sample_m = next((m for m in masteries if m.get("effectId") is not None), None)
        assert sample_m is not None
        assert isinstance(sample_m["effectId"], int)
        assert sample_m["allocSpec"] == f"{sample_m['id']}:{sample_m['effectId']}"
        assert isinstance(sample_m.get("sd"), list) and len(sample_m["sd"]) > 0
        if has_zh_data(_ENGINE_DIR):
            assert isinstance(sample_m.get("nameZh"), str) and len(sample_m["nameZh"]) > 0
            assert isinstance(sample_m.get("sdZh"), list) and len(sample_m["sdZh"]) > 0
        non_mastery = next((r for r in np_res.get("report", []) if r.get("type") == "Notable"), None)
        assert non_mastery is not None
        assert non_mastery["allocSpec"] == str(non_mastery["id"])

        # 2. Verify build_passive_graph + Tarjan find_prunable_branches on real 3.30 tree.
        ts_before = w.call("get_tree_state", {})
        stats_before = w.call("get_stats", {}).get("stats", {})
        nodes_db = w.get_cached_tree_data(ts_before.get("treeVersion")).get("nodes", {})
        alloc_before = {int(x) for x in ts_before.get("allocatedNodes", [])}

        t0 = time.perf_counter()
        graph = build_passive_graph(nodes_db, ts_before.get("dynamicNodes"), ts_before.get("overrides"))
        branches = find_prunable_branches(graph, alloc_before, empty_socket_ids={6230}, max_branch_pts=4)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        slack = float(os.environ.get("POB_TEST_PERF_SLACK", "1") or "1")
        assert elapsed_ms < 50.0 * slack, f"Graph build + Tarjan too slow: {elapsed_ms:.2f}ms"
        assert branches, "Expected prunable branches on real Witch build"
        assert any(b.node_ids == (6230,) and b.kind == "empty_socket" for b in branches)
        main_alloc = {n for n in alloc_before if n in graph.nodes and not graph.is_ascendancy(n)}
        roots = [n for n in main_alloc if graph.node_type(n) == "ClassStart"]
        for br in branches:
            rem = main_alloc - set(br.node_ids)
            assert _connected_from_roots(graph, rem, roots) == rem

        # 3. Verify eval_node_paths batch add (with temporary mastery) & remove with ZERO state mutation.
        rm_target = next(b for b in branches if b.node_ids != (6230,))
        eval_res = w.call(
            "eval_node_paths",
            {
                "candidates": [
                    {"key": "rm_branch", "mode": "remove", "nodeIds": list(rm_target.node_ids)},
                    {"key": "rm_empty_sock", "mode": "remove", "nodeIds": [6230]},
                    {
                        "key": "add_mastery",
                        "mode": "add",
                        "nodeIds": [int(sample_m["id"])],
                        "masteryEffects": {str(sample_m["id"]): int(sample_m["effectId"])},
                    },
                ]
            },
        )
        assert eval_res["evaluatedCount"] == 3
        assert eval_res["baseStats"]["CombinedDPS"] > 0
        by_key = {r["key"]: r for r in eval_res["results"]}
        assert "error" not in by_key["rm_branch"]
        assert "error" not in by_key["rm_empty_sock"]
        assert "error" not in by_key["add_mastery"]
        assert by_key["rm_empty_sock"]["deltas"]["CombinedDPS"] == pytest.approx(0.0, abs=1e-6)
        assert "Life" in by_key["rm_branch"]["outputStats"]
        assert "ManaUnreserved" in by_key["rm_branch"]["outputStats"]
        assert "Str" in by_key["rm_branch"]["outputStats"]

        # Verify 100% ZERO mutation residue on spec.allocNodes, overrides/masteries, and mainOutput!
        ts_after = w.call("get_tree_state", {})
        stats_after = w.call("get_stats", {}).get("stats", {})
        assert ts_after["allocatedNodes"] == ts_before["allocatedNodes"]
        assert ts_after.get("overrides") == ts_before.get("overrides")
        for key in ("CombinedDPS", "TotalEHP", "Life", "Mana", "Str", "Dex", "Int"):
            assert stats_after.get(key) == stats_before.get(key)
    finally:
        w.stop()
