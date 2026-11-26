"""Tests for Layer 4: `pob tree rank-wheels` wheel bundling, tag pre-filtering,
Mastery Uniqueness Invariant, hard vetoes, strictly isolated `[Proxy Benchmark]`, and real-engine latency."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

from pob_cli.bridge_client import BridgeError, BridgeWorker
from pob_cli.cli import main
from pob_cli.comparator.wheel_packages import build_wheel_packages
from pob_cli.comparator.wheel_ranking import check_wheel_veto_and_notes, rank_wheels
from pob_cli.comparator.wheel_tags import BuildProfile, extract_build_profile, is_wheel_relevant_for_build
from pob_cli.formatters.wheel_rank_md import format_wheel_rank_markdown
from pob_cli.game_data.tree_graph import build_passive_graph
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


def _synthetic_nodes_db() -> dict[str, Any]:
    """Build a small deterministic passive graph with:
    - ClassStart (1) -> small travel (2)
    - Wheel A (group 10): Lightning Spell wheel (101 small, 102 Notable, 103 Mastery with 2 effects)
    - Wheel B (group 20): Bow Attack wheel (201 small, 202 Notable) — filtered out for Lightning Spell
    - Wheel C (group 30): Universal Life wheel (301 small, 302 Notable) — always kept
    - Wheel D (group 40): Broken CoC/Mana wheel (401 small, 402 Notable) — triggers hard veto
    - Unallocated Basic Socket (501) at 2 hops (2 -> 500 -> 501)
    - Unallocated Outer Large Cluster Socket (601) at 3 hops (2 -> 600 -> 602 -> 601)
    """
    return {
        "1": {"name": "Witch", "type": "ClassStart", "group": 1, "out": [2], "in": [], "stats": []},
        "2": {"name": "Intelligence", "type": "Normal", "group": 1, "out": [1, 101, 201, 301, 401, 500, 600], "in": [], "stats": ["+10 to Intelligence"]},
        # Wheel A: Lightning Spell + Mastery (effects 9001 already used elsewhere, 9002 unused)
        "101": {"name": "Lightning Damage", "type": "Normal", "group": 10, "out": [2, 102], "in": [], "stats": ["12% increased Lightning Damage"]},
        "102": {"name": "Crackling Speed", "type": "Notable", "group": 10, "out": [101], "in": [], "stats": ["24% increased Lightning Damage", "8% increased Cast Speed"]},
        "103": {
            "name": "Lightning Mastery",
            "type": "Mastery",
            "group": 10,
            "out": [],
            "in": [],
            "masteryEffects": [
                {"effect": 9001, "stats": ["Lightning Damage with Non-Critical Strikes is Lucky"]},
                {"effect": 9002, "stats": ["+15% to Critical Strike Multiplier with Lightning Skills"]},
            ],
        },
        # Wheel B: Bow Attack (unrelated to Lightning Spell)
        "201": {"name": "Bow Damage", "type": "Normal", "group": 20, "out": [2, 202], "in": [], "stats": ["12% increased Damage with Bows"]},
        "202": {"name": "Heavy Draw", "type": "Notable", "group": 20, "out": [201], "in": [], "stats": ["30% increased Physical Damage with Bows"]},
        # Wheel C: Universal Life
        "301": {"name": "Life", "type": "Normal", "group": 30, "out": [2, 302], "in": [], "stats": ["5% increased maximum Life"]},
        "302": {"name": "Cruel Preparation", "type": "Notable", "group": 30, "out": [301], "in": [], "stats": ["10% increased maximum Life", "+5% to all Elemental Resistances"]},
        # Wheel D: Reservation/Trigger trap wheel (universal mana tag so it is evaluated and vetoed)
        "401": {"name": "Mana", "type": "Normal", "group": 40, "out": [2, 402], "in": [], "stats": ["8% increased maximum Mana"]},
        "402": {"name": "Overclocked Reserve", "type": "Notable", "group": 40, "out": [401], "in": [], "stats": ["10% increased maximum Mana"]},
        # Already-allocated other Mastery node holding effectId 9001
        "90": {"name": "Allocated Lightning Mastery", "type": "Mastery", "group": 9, "out": [], "in": [], "masteryEffects": [{"effect": 9001, "stats": ["Lightning Damage with Non-Critical Strikes is Lucky"]}]},
        # Sockets
        "500": {"name": "Intelligence", "type": "Normal", "group": 50, "out": [2, 501], "in": [], "stats": ["+10 to Intelligence"]},
        "501": {"name": "Jewel Socket", "type": "Socket", "group": 50, "out": [500], "in": [], "stats": []},
        "600": {"name": "Intelligence", "type": "Normal", "group": 60, "out": [2, 602], "in": [], "stats": ["+10 to Intelligence"]},
        "602": {"name": "Intelligence", "type": "Normal", "group": 60, "out": [600, 601], "in": [], "stats": ["+10 to Intelligence"]},
        "601": {"name": "Jewel Socket", "type": "Socket", "group": 60, "out": [602], "in": [], "stats": [], "expansionJewel": {"size": 2}, "x": 0.0, "y": -5000.0},
    }


class _FakeWheelWorker:
    """Offline fake BridgeWorker for deterministic unit tests of `rank_wheels`."""

    def __init__(self, eval_map: dict[str, dict[str, Any]], *, extra_ks: list[int] | None = None) -> None:
        self.lock = threading.RLock()
        self._nodes = _synthetic_nodes_db()
        self._eval_map = eval_map
        self._alloc = [1, 2, 90, *(extra_ks or [])]
        self.eval_batches: list[list[dict[str, Any]]] = []

    def get_cached_tree_data(self, _tv: str | None = None) -> dict[str, Any]:
        return {"nodes": self._nodes}

    def call_batch(self, requests: list[tuple[str, dict[str, Any]]], **_kw: Any) -> list[dict[str, Any]]:
        return [self.call(m, p) for m, p in requests]

    def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        if method == "get_tree_state":
            return {
                "treeVersion": "3_30",
                "allocatedNodes": list(self._alloc),
                "overrides": {"90": {"effectId": 9001, "stats": ["Lightning Damage with Non-Critical Strikes is Lucky"]}},
                "dynamicNodes": [],
                "sockets": [
                    {"nodeId": 501, "allocated": False, "itemId": 0},
                    {"nodeId": 601, "allocated": False, "itemId": 0, "expansionJewel": {"size": 2}},
                ],
            }
        if method == "list_items":
            return {"items": [], "slots": []}
        if method == "list_skills":
            return {
                "mainSocketGroup": 1,
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "enabled": True,
                        "gems": [{"name": "Arc", "enabled": True}, {"name": "Increased Critical Strikes Support", "enabled": True}],
                    }
                ],
            }
        if method == "get_stats":
            return {
                "stats": {
                    "CombinedDPS": 100000.0,
                    "TotalEHP": 20000.0,
                    "Life": 4000.0,
                    "EnergyShield": 300.0,
                    "CritChance": 45.0,
                    "LightningDPS": 95000.0,
                    "ManaUnreserved": 120.0,
                    "SpellSuppressionChance": 85.0,
                }
            }
        if method == "export_code":
            return {"code": ""}
        if method == "eval_node_paths":
            cands = list((params or {}).get("candidates") or [])
            self.eval_batches.append(cands)
            base_stats = {
                "CombinedDPS": 100000.0,
                "TotalEHP": 20000.0,
                "ManaUnreserved": 120.0,
                "SpellSuppressionChance": 85.0,
            }
            results = []
            for c in cands:
                key = str(c["key"])
                st = dict(base_stats)
                st.update(self._eval_map.get(key, {}))
                results.append({"key": key, "mode": c["mode"], "outputStats": st})
            return {"baseStats": base_stats, "results": results, "elapsedMs": 1.5}
        raise AssertionError(f"Unexpected RPC: {method}")


def test_tag_prefilter_reduces_candidates_keeps_universal_and_all_tags_restores() -> None:
    graph = build_passive_graph(_synthetic_nodes_db(), [], {})
    profile = extract_build_profile(
        {"Life": 4000, "EnergyShield": 200, "LightningDPS": 90000, "CritChance": 40},
        skills_res={"mainSocketGroup": 1, "groups": [{"index": 1, "isMain": True, "gems": [{"name": "Arc", "enabled": True}]}]},
    )
    assert "lightning" in profile.tags and "spell" in profile.tags and "crit" in profile.tags

    pkgs_filtered, total_w, filt_w = build_wheel_packages(graph, {1, 2, 90}, {9001}, profile, all_tags=False)
    wheel_ids_filt = {p["wheelId"] for p in pkgs_filtered}
    assert total_w == 4
    assert filt_w < total_w  # Bow wheel (g:20) filtered out!
    assert "g:10" in wheel_ids_filt  # Lightning Spell wheel kept
    assert "g:30" in wheel_ids_filt  # Universal Life wheel kept
    assert "g:20" not in wheel_ids_filt  # Bow wheel filtered out

    pkgs_all, total_all, filt_all = build_wheel_packages(graph, {1, 2, 90}, {9001}, profile, all_tags=True)
    wheel_ids_all = {p["wheelId"] for p in pkgs_all}
    assert filt_all == total_all == 4
    assert "g:20" in wheel_ids_all


def test_mastery_uniqueness_invariant_and_best_roi_package_selection() -> None:
    # Mastery 103 has 9001 (ALREADY USED on node 90) and 9002 (unused).
    # Make Notable+Mastery (3 pts -> +21% DPS = +7%/pt) beat Notable-only (2 pts -> +10% DPS = +5%/pt).
    worker = _FakeWheelWorker(
        {
            "wheel:g:10:base": {"CombinedDPS": 110000.0, "TotalEHP": 20000.0},
            "wheel:g:10:m:103:9002": {"CombinedDPS": 121000.0, "TotalEHP": 20000.0},
            "wheel:g:30:base": {"CombinedDPS": 100000.0, "TotalEHP": 22000.0, "SpellSuppressionChance": 100.0},
            "wheel:g:40:base": {"CombinedDPS": 105000.0, "TotalEHP": 20000.0, "ManaUnreserved": -25.0},
            "proxy_sock:501": {"CombinedDPS": 108000.0, "TotalEHP": 21200.0},
            "proxy_sock:601": {"CombinedDPS": 128000.0, "TotalEHP": 20000.0},
        }
    )

    res = rank_wheels(worker, top=10, sort_by="dps", include_proxy_sockets=True)  # type: ignore[arg-type]

    # Verify duplicate mastery effect 9001 was NEVER submitted to eval_node_paths!
    submitted_keys = {c["key"] for batch in worker.eval_batches for c in batch}
    assert "wheel:g:10:m:103:9001" not in submitted_keys
    assert "wheel:g:10:m:103:9002" in submitted_keys

    # Best wheel is g:10 with Mastery 103:9002 (+7%/pt > +5%/pt for Notable-only)
    assert res["wheels"], "Expected ranked wheels"
    top_w = res["wheels"][0]
    assert top_w["wheelId"] == "g:10"
    assert top_w["mastery"] is not None
    assert top_w["mastery"]["effectId"] == 9002
    assert top_w["mastery"]["allocSpec"] == "103:9002"
    assert "103:9002" in top_w["allocateSpec"]
    assert top_w["dpsPctPerPt"] == pytest.approx(7.0, abs=0.01)

    # Hard veto caught ManaUnreserved < 0 for g:40 and excluded it from positive wheels!
    assert all(w["wheelId"] != "g:40" for w in res["wheels"])
    assert len(res["vetoedWheels"]) == 1
    assert res["vetoedWheels"][0]["wheelId"] == "g:40"
    assert "[VETO: ManaUnreserved < 0" in res["vetoedWheels"][0]["vetoReason"]

    # Verify NO composite score field exists anywhere in output!
    forbidden_score_keys = {"score", "compositeScore", "weightedScore", "alpha", "beta", "combinedScore"}
    for rec in res["wheels"] + res["vetoedWheels"] + res["proxySocketBenchmarks"]:
        assert forbidden_score_keys.isdisjoint(rec.keys())


def test_hard_veto_coc_overspeed_and_agnostic_es() -> None:
    graph = build_passive_graph(_synthetic_nodes_db(), [], {})
    prof_std = BuildProfile(is_spell=True, is_trigger=True)
    # CoC overspeed cliff (trigger rate dropped >10% or source rate > cap)
    veto_coc, _ = check_wheel_veto_and_notes(
        {"SkillTriggerRate": 7.57, "EffectiveSourceRate": 7.40, "TriggerRateCap": 7.57},
        {"SkillTriggerRate": 5.05, "EffectiveSourceRate": 7.90, "TriggerRateCap": 7.57},
        {"nodeIds": [101, 102]},
        graph,
        prof_std,
        dps_pct=-15.0,
        sort_by="dps",
    )
    assert veto_coc is not None and "CoC overspeed / negative DPS" in veto_coc

    # Agnostic ES-only wheel
    nodes_es = _synthetic_nodes_db()
    nodes_es["701"] = {"name": "Energy Shield", "type": "Notable", "group": 70, "out": [2], "in": [], "stats": ["20% increased maximum Energy Shield"]}
    g_es = build_passive_graph(nodes_es, [], {})
    prof_ag = BuildProfile(is_spell=True, is_agnostic=True)
    veto_ag, _ = check_wheel_veto_and_notes(
        {"ManaUnreserved": 200.0},
        {"ManaUnreserved": 200.0},
        {"nodeIds": [701]},
        g_es,
        prof_ag,
        dps_pct=0.0,
        sort_by="ehp",
    )
    assert veto_ag == "[VETO: Agnostic ES > 0]"


def test_proxy_benchmark_strict_isolation_and_cli_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _FakeWheelWorker(
        {
            "wheel:g:10:base": {"CombinedDPS": 110000.0, "TotalEHP": 20000.0},
            "wheel:g:10:m:103:9002": {"CombinedDPS": 121000.0, "TotalEHP": 20000.0},
            "wheel:g:30:base": {"CombinedDPS": 100000.0, "TotalEHP": 22000.0},
            "proxy_sock:501": {"CombinedDPS": 108000.0, "TotalEHP": 21200.0},
            "proxy_sock:601": {"CombinedDPS": 128000.0, "TotalEHP": 20000.0},
        }
    )
    res_with = rank_wheels(worker, top=5, sort_by="dps", include_proxy_sockets=True)  # type: ignore[arg-type]
    assert "proxySocketBenchmarks" in res_with
    proxies = res_with["proxySocketBenchmarks"]
    assert len(proxies) == 2
    assert all(p["proxyBenchmark"] is True and p["templateSummary"] for p in proxies)
    # Main wheels table never contains proxy socket entries
    assert all(not w.get("proxyBenchmark") for w in res_with["wheels"])

    md_with = format_wheel_rank_markdown(res_with)
    real_idx = md_with.index("### Real Build Wheel Ranking")
    proxy_idx = md_with.index("### Reachable Socket Reference ROI [Proxy Benchmark]")
    assert real_idx < proxy_idx
    real_section = md_with[real_idx:proxy_idx]
    proxy_section = md_with[proxy_idx:]
    assert "[Proxy Benchmark]" not in real_section
    assert "[Proxy Benchmark] `501`" in proxy_section
    assert "[Proxy Benchmark] `601`" in proxy_section

    # `--no-proxy-sockets` omits proxySocketBenchmarks and proxy Markdown section cleanly
    res_no = rank_wheels(worker, top=5, sort_by="dps", include_proxy_sockets=False)  # type: ignore[arg-type]
    assert "proxySocketBenchmarks" not in res_no
    md_no = format_wheel_rank_markdown(res_no)
    assert "### Reachable Socket Reference ROI [Proxy Benchmark]" not in md_no
    assert "[Proxy Benchmark]" not in md_no

    # CLI routing check via Click CliRunner
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        calls.append((action, dict(params or {})))
        return res_no if not (params or {}).get("include_proxy_sockets", True) else res_with

    monkeypatch.setattr("pob_cli.cli._daemon_call", _fake_daemon)
    runner = CliRunner()

    r1 = runner.invoke(main, ["tree", "rank-wheels", "ref", "--top", "8", "--max-dist", "10", "--max-pts", "8", "--sort", "ehp", "--all-tags", "--no-proxy-sockets", "--json"])
    assert r1.exit_code == 0, r1.output
    assert calls[-1][0] == "tree_rank_wheels"
    assert calls[-1][1]["target"] == "ref"
    assert calls[-1][1]["top"] == 8
    assert calls[-1][1]["max_dist"] == 10
    assert calls[-1][1]["max_pts"] == 8
    assert calls[-1][1]["sort"] == "ehp"
    assert calls[-1][1]["all_tags"] is True
    assert calls[-1][1]["include_proxy_sockets"] is False
    parsed = json.loads(r1.output)
    assert "proxySocketBenchmarks" not in parsed

    r2 = runner.invoke(main, ["tree", "rank-wheels"])
    assert r2.exit_code == 0, r2.output
    assert "### Real Build Wheel Ranking" in r2.output
    assert "### Reachable Socket Reference ROI [Proxy Benchmark]" in r2.output


@pytest.mark.skipif(_ENGINE_DIR is None, reason="No runnable PoB engine found")
def test_real_engine_rank_wheels_latency_mastery_and_zero_residue() -> None:
    assert _ENGINE_DIR is not None
    # Connected Witch Elementalist Arc tree near basic socket 48768 & outer cluster socket 21984.
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
        w.call("import_code", {"code": xml_to_share_code(xml_build), "mode": "replace"})
        # Warm tree data cache once (just like a warm daemon slot).
        w.get_cached_tree_data("3_30")
        ts_before = w.call("get_tree_state", {})
        items_before = w.call("list_items", {})
        stats_before = w.call("get_stats", {}).get("stats", {})

        slack = float(os.environ.get("POB_TEST_PERF_SLACK", "1") or "1")
        t0 = time.perf_counter()
        res = rank_wheels(w, top=10, max_dist=12, max_pts=10, sort_by="dps", include_proxy_sockets=True)
        elapsed_sec = time.perf_counter() - t0

        assert elapsed_sec < 2.0 * slack, (
            f"rank_wheels exceeded 2.0s budget: {elapsed_sec:.3f}s "
            f"(agg={res['aggregationMs']}ms, lua={res['luaEvalMs']}ms, pkgs={res['evaluatedPackages']})"
        )
        assert res["totalWheelsScanned"] > 0
        assert res["tagFilteredWheels"] < res["totalWheelsScanned"], "Expected tag pre-filter to prune unrelated wheels"
        assert res["wheels"], "Expected positive ranked wheels on real Witch Arc build"
        assert res["wheels"][0]["dpsPctPerPt"] > 0.0
        assert res["wheels"][0]["allocateCommand"].startswith("pob tree change --allocate ")

        # Proxy benchmark strictly separated and calculated via real PoB Lua mod parser.
        proxies = res.get("proxySocketBenchmarks") or []
        assert proxies, "Expected reachable unallocated socket proxy benchmarks"
        assert all(p["proxyBenchmark"] is True and p["templateSummary"] for p in proxies)
        assert any(p["dpsPctPerPt"] > 0.0 or p["ehpPctPerPt"] > 0.0 for p in proxies)

        # Markdown strictly separates Real Build Wheels from [Proxy Benchmark].
        md = format_wheel_rank_markdown(res)
        assert "### Real Build Wheel Ranking" in md
        assert "### Reachable Socket Reference ROI [Proxy Benchmark]" in md

        # `--no-proxy-sockets` omits proxySocketBenchmarks cleanly and `--sort ehp` picks real Mastery packages.
        res_ehp = rank_wheels(w, top=5, max_dist=12, max_pts=10, sort_by="ehp", include_proxy_sockets=False)
        assert "proxySocketBenchmarks" not in res_ehp
        assert any(
            isinstance(wh.get("mastery"), dict) and ":" in str(wh["mastery"].get("allocSpec", ""))
            for wh in res_ehp["wheels"]
        ), "Expected at least one top EHP wheel to bundle a real <nodeId>:<effectId> Mastery"

        # 100% zero mutation residue on tree nodes, overrides, items, and output stats!
        ts_after = w.call("get_tree_state", {})
        items_after = w.call("list_items", {})
        stats_after = w.call("get_stats", {}).get("stats", {})
        assert ts_after["allocatedNodes"] == ts_before["allocatedNodes"]
        assert ts_after.get("overrides") == ts_before.get("overrides")
        assert items_after.get("items") == items_before.get("items")
        assert stats_after.get("CombinedDPS") == pytest.approx(stats_before.get("CombinedDPS"))
        assert stats_after.get("TotalEHP") == pytest.approx(stats_before.get("TotalEHP"))
    finally:
        w.stop()


def test_bow_retains_es_wheels_and_minion_retains_minion_attack_melee_wheels() -> None:
    """P1-1 & P1-2 regression: Bow retains Energy Shield wheels; Minion retains Minion Attack/Melee wheels."""
    nodes = {
        "1": {"name": "Start", "type": "ClassStart", "group": 1, "out": [2], "in": [], "stats": []},
        "2": {"name": "Travel", "type": "Normal", "group": 1, "out": [1, 10, 20, 30, 40, 50], "in": [], "stats": []},
        # Pure Energy Shield wheel (must NOT trigger equipped-shield weapon filter!)
        "10": {"name": "Unnatural Calm", "type": "Notable", "group": 10, "out": [2], "in": [], "stats": ["20% increased maximum Energy Shield"]},
        # Hybrid Life + Energy Shield wheel
        "20": {"name": "Melding", "type": "Notable", "group": 20, "out": [2], "in": [], "stats": ["10% increased maximum Life", "12% increased maximum Energy Shield"]},
        # Actual equipped-shield weapon wheel (MUST be filtered out for Bow!)
        "30": {"name": "Command of Steel", "type": "Notable", "group": 30, "out": [2], "in": [], "stats": ["30% increased Attack Damage while holding a Shield"]},
        # Minion Attack Speed wheel (MUST be retained for Minion build even though is_attack=False!)
        "40": {"name": "Spiritual Command", "type": "Notable", "group": 40, "out": [2], "in": [], "stats": ["Minions have 8% increased Attack Speed"]},
        # Minion Melee Damage wheel (MUST be retained for Minion build!)
        "50": {"name": "Ravenous Horde", "type": "Notable", "group": 50, "out": [2], "in": [], "stats": ["Minions deal 20% increased Melee Damage"]},
    }
    graph = build_passive_graph(nodes, [], {})

    bow_prof = BuildProfile(is_attack=True, is_projectile=True, weapon_types={"bow"}, damage_types={"lightning"})
    assert is_wheel_relevant_for_build(graph, graph.wheels["g:10"], bow_prof) is True
    assert is_wheel_relevant_for_build(graph, graph.wheels["g:20"], bow_prof) is True
    assert is_wheel_relevant_for_build(graph, graph.wheels["g:30"], bow_prof) is False
    assert is_wheel_relevant_for_build(graph, graph.wheels["g:40"], bow_prof) is False

    minion_prof = BuildProfile(is_minion=True, is_attack=False, is_spell=False, damage_types={"fire"})
    assert is_wheel_relevant_for_build(graph, graph.wheels["g:40"], minion_prof) is True
    assert is_wheel_relevant_for_build(graph, graph.wheels["g:50"], minion_prof) is True
    assert is_wheel_relevant_for_build(graph, graph.wheels["g:30"], minion_prof) is False


def test_bridge_error_propagates_target_slot_and_eval_error_count() -> None:
    """Major #86-1 & #86-2: BridgeError propagates, --target slot included, evalErrorCount surfaced."""
    w_err = _FakeWheelWorker({})

    def _boom_call(method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        if method == "export_code":
            raise BridgeError("worker crashed")
        return _FakeWheelWorker.call(w_err, method, params, **kw)

    w_err.call = _boom_call  # type: ignore[method-assign]
    with pytest.raises(BridgeError, match="worker crashed"):
        rank_wheels(w_err, top=5)  # type: ignore[arg-type]

    # Target slot propagation + evalErrorCount tracking
    w_ref = _FakeWheelWorker(
        {
            "wheel:g:10:base": {"CombinedDPS": 115000.0, "TotalEHP": 20000.0},
            "proxy_sock:501": {"CombinedDPS": 108000.0, "TotalEHP": 21200.0},
        }
    )
    orig_call = w_ref.call

    def _with_one_err(method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        res = orig_call(method, params, **kw)
        if method == "eval_node_paths":
            for r in res["results"]:
                if r["key"] == "wheel:g:30:base":
                    r["error"] = "simulated eval error"
        return res

    w_ref.call = _with_one_err  # type: ignore[method-assign]
    res = rank_wheels(w_ref, top=5, target_slot="ref", include_proxy_sockets=True)  # type: ignore[arg-type]
    assert res["evalErrorCount"] == 1
    assert "--target ref --allocate " in res["wheels"][0]["allocateCommand"]
    assert "--target ref --allocate " in res["proxySocketBenchmarks"][0]["pathCommand"]
    md = format_wheel_rank_markdown(res)
    assert "pob tree change --target ref --allocate " in md


@pytest.mark.skipif(_ENGINE_DIR is None, reason="No runnable PoB engine found")
def test_real_engine_rank_wheels_retains_es_for_bow_and_minion_attack_wheels() -> None:
    """Real-engine proof: Bow+Quiver keeps ES wheels and SRS Minion keeps Minion Attack/Melee wheels."""
    assert _ENGINE_DIR is not None

    bow_xml = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447,57264,33296,1957,739,18866"/></Tree>
  <Skills activeSkillSet="1"><SkillSet id="1"><Skill mainActiveSkill="1" enabled="true" label="Main"><Gem nameSpec="Lightning Arrow" level="20" quality="20" enabled="true"/></Skill></SkillSet></Skills>
  <Items activeItemSet="1">
    <Item id="1">Rarity: RARE
Tempest Thicket
Thicket Bow
Implicits: 0
Adds 20 to 80 Lightning Damage</Item>
    <Item id="2">Rarity: RARE
Feather Arrow
Broadhead Arrow Quiver
Implicits: 0
+40 to maximum Life</Item>
    <ItemSet id="1"><Slot name="Weapon 1" itemId="1"/><Slot name="Weapon 2" itemId="2"/></ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>"""

    minion_xml = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Necromancer" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="1" nodes="54447,57264,33296,1957,739,18866"/></Tree>
  <Skills activeSkillSet="1"><SkillSet id="1"><Skill mainActiveSkill="1" enabled="true" label="Main"><Gem nameSpec="Summon Raging Spirit" level="20" quality="20" enabled="true"/></Skill></SkillSet></Skills>
  <Items activeItemSet="1"><ItemSet id="1"/></Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>"""

    w = BridgeWorker(_ENGINE_DIR)
    w.start()
    try:
        nodes_db = w.get_cached_tree_data("3_30").get("nodes", {})

        # 1. Bow + Quiver build must retain Energy Shield defense wheels!
        w.call("import_code", {"code": xml_to_share_code(bow_xml), "mode": "replace"})
        ts_b = w.call("get_tree_state", {})
        it_b = w.call("list_items", {})
        sk_b = w.call("list_skills", {})
        st_b = (w.call("get_stats", {}) or {}).get("stats") or {}
        prof_b = extract_build_profile(st_b, skills_res=sk_b, items_res=it_b)
        assert "bow" in prof_b.weapon_types
        g_b = build_passive_graph(nodes_db, ts_b.get("dynamicNodes"), ts_b.get("overrides"))
        pkgs_b, _, _ = build_wheel_packages(
            g_b, {int(x) for x in ts_b.get("allocatedNodes", [])}, set(), prof_b, max_dist=18, max_pts=12
        )
        es_kept = [
            p["wheel"].name
            for p in pkgs_b
            if any(
                "energy shield" in str(s).lower()
                for nid in p["wheel"].node_ids
                for s in ((g_b.nodes.get(nid) or {}).get("stats") or [])
            )
        ]
        assert es_kept, "Expected Bow+Quiver build to retain reachable Energy Shield defense wheels"

        # 2. Summon Raging Spirit Minion build must retain Minion Attack Speed / Melee wheels!
        w.call("import_code", {"code": xml_to_share_code(minion_xml), "mode": "replace"})
        ts_m = w.call("get_tree_state", {})
        it_m = w.call("list_items", {})
        sk_m = w.call("list_skills", {})
        st_m = (w.call("get_stats", {}) or {}).get("stats") or {}
        prof_m = extract_build_profile(st_m, skills_res=sk_m, items_res=it_m)
        assert prof_m.is_minion is True and prof_m.is_attack is False
        g_m = build_passive_graph(nodes_db, ts_m.get("dynamicNodes"), ts_m.get("overrides"))
        pkgs_m, _, _ = build_wheel_packages(
            g_m, {int(x) for x in ts_m.get("allocatedNodes", [])}, set(), prof_m, max_dist=18, max_pts=12
        )
        minion_atk_kept = [
            p["wheel"].name
            for p in pkgs_m
            if any(
                "minion" in str(s).lower() and ("attack speed" in str(s).lower() or "melee" in str(s).lower() or "attack" in str(s).lower())
                for nid in p["wheel"].node_ids
                for s in ((g_m.nodes.get(nid) or {}).get("stats") or [])
            )
        ]
        assert minion_atk_kept, "Expected Minion build to retain reachable Minion Attack/Melee wheels"
    finally:
        w.stop()
