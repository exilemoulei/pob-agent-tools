"""Tests for Layer 2: compatible `pob tree` subcommand group, `pob tree prune`,
safety guardrails, and zero-RPC `<20ms` threshold gap probes."""

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

from pob_cli.analyzer.threshold_probes import (
    compute_mre_needed_for_aura,
    compute_threshold_probes,
)
from pob_cli.analyzer.tree_prune import evaluate_prune_guardrails, prune_tree
from pob_cli.bridge_client import BridgeWorker
from pob_cli.cli import main
from pob_cli.formatters.diagnose_md import format_diagnose_markdown
from pob_cli.formatters.tree_prune_md import (
    format_threshold_probes_markdown,
    format_tree_prune_markdown,
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


def test_pob_tree_compat_group_routes_positional_slot_and_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        calls.append((action, dict(params or {})))
        if action == "tree":
            return {
                "points": {"used": 50, "usedMax": 120, "ascUsed": 8, "ascMax": 8},
                "keystones": [],
                "ascendancyNodes": [],
                "masteries": [],
                "notables": [],
            }
        if action == "tree_prune":
            return {
                "dpsKey": "CombinedDPS",
                "baseDps": 1000000.0,
                "baseEhp": 50000.0,
                "sort": (params or {}).get("sort", "dps"),
                "maxBranchPts": (params or {}).get("max_branch_pts", 5),
                "evaluatedCount": 1,
                "safeCount": 1,
                "unsafeCount": 0,
                "safeBranches": [
                    {
                        "key": "rm:20",
                        "kind": "empty_socket",
                        "nodeIds": [20],
                        "points": 1,
                        "label": "Empty Jewel Socket (20)",
                        "dpsPct": 0.0,
                        "dpsPctPerPt": 0.0,
                        "ehpPct": 0.0,
                        "ehpPctPerPt": 0.0,
                        "safe": True,
                        "guardrails": {"safe": True, "violations": [], "details": {}},
                        "deallocCommand": "pob tree change --deallocate 20",
                    }
                ],
                "unsafeBranches": [],
                "candidates": [],
            }
        if action == "tree_change":
            return {"applied": False, "mode": "WHAT-IF", "before": {}, "after": {}, "diff": {"deltas": []}}
        return {}

    monkeypatch.setattr("pob_cli.cli._daemon_call", _fake_daemon)
    runner = CliRunner()

    r1 = runner.invoke(main, ["tree"])
    assert r1.exit_code == 0, r1.output
    assert calls[-1][0] == "tree" and calls[-1][1]["target"] == "main"

    r2 = runner.invoke(main, ["tree", "current"])
    assert r2.exit_code == 0, r2.output
    assert calls[-1][0] == "tree" and calls[-1][1]["target"] == "main"

    r3 = runner.invoke(main, ["tree", "ref", "--full", "--json"])
    assert r3.exit_code == 0, r3.output
    assert calls[-1][0] == "tree"
    assert calls[-1][1]["target"] == "ref"
    assert calls[-1][1]["limit"] == 0
    assert json.loads(r3.output)["points"]["used"] == 50

    r4 = runner.invoke(main, ["tree", "prune", "ref", "--top", "5", "--sort", "ehp", "--json"])
    assert r4.exit_code == 0, r4.output
    assert calls[-1][0] == "tree_prune"
    assert calls[-1][1]["target"] == "ref"
    assert calls[-1][1]["sort"] == "ehp"
    assert calls[-1][1]["top"] == 5

    r5 = runner.invoke(main, ["tree", "prune"])
    assert r5.exit_code == 0, r5.output
    assert "Empty Jewel Socket (20)" in r5.output
    assert "pob tree change --deallocate 20" in r5.output

    r6 = runner.invoke(main, ["tree", "change", "--allocate", "12345"])
    assert r6.exit_code == 0, r6.output
    assert calls[-1][0] == "tree_change"


def test_prune_guardrails_detect_attr_resist_mana_and_trigger_breaks() -> None:
    base = {
        "Str": 120,
        "Dex": 160,
        "Int": 200,
        "ReqStr": 110,
        "ReqDex": 155,
        "ReqInt": 180,
        "FireResist": 75,
        "FireResistMax": 75,
        "ColdResist": 80,
        "ColdResistMax": 80,
        "LightningResist": 75,
        "LightningResistMax": 75,
        "ManaUnreserved": 45,
        "ManaCost": 30,
        "LifeUnreserved": 4000,
        "HitChance": 100.0,
        "SkillTriggerRate": 7.50,
    }
    # Safe candidate
    ok = evaluate_prune_guardrails(base, {**base, "Dex": 155, "ManaUnreserved": 32})
    assert ok["safe"] is True and ok["violations"] == []

    # Dex deficit + FireResist drop + ManaUnreserved < 0 + HitChance drop + Trigger cliff
    bad = evaluate_prune_guardrails(
        base,
        {
            **base,
            "Dex": 143,
            "FireResist": 68,
            "ManaUnreserved": -5,
            "HitChance": 95.0,
            "SkillTriggerRate": 5.0,
        },
    )
    assert bad["safe"] is False
    joined = " | ".join(bad["violations"])
    assert "Dex -12 deficit" in joined
    assert "FireRes 68%<75%" in joined
    assert "ManaUnreserved -5<0" in joined
    assert "HitChance 100.0%->95.0%" in joined
    assert "TriggerRate cliff" in joined

    # Increasing ManaCost guardrail: base_cost=10 -> cand_cost=25 with ManaUnreserved=20 MUST flag!
    cost_inc = evaluate_prune_guardrails(
        {**base, "ManaUnreserved": 20, "ManaCost": 10},
        {**base, "ManaUnreserved": 20, "ManaCost": 25},
    )
    assert cost_inc["safe"] is False
    assert any("ManaUnreserved 20<Cost 25" in v for v in cost_inc["violations"])

    # Explicit cand ManaCost=0.0 must NOT fall back to base ManaCost=30!
    cost_zero = evaluate_prune_guardrails(
        {**base, "ManaUnreserved": 45, "ManaCost": 30},
        {**base, "ManaUnreserved": 10, "ManaCost": 0.0},
    )
    assert cost_zero["safe"] is True


def test_prune_tree_sorts_net_gain_and_empty_socket_first_and_separates_unsafe() -> None:
    nodes_db = {
        "1": {"id": 1, "type": "ClassStart", "name": "Witch", "links": [2]},
        "2": {"id": 2, "type": "Normal", "name": "Int", "stats": ["+10 to Intelligence"], "links": [1, 3, 4, 20]},
        "3": {"id": 3, "type": "Notable", "name": "Weak Node", "group": 10, "links": [2]},
        "4": {"id": 4, "type": "Notable", "name": "Dex Load Bearing", "group": 11, "links": [2]},
        "20": {"id": 20, "type": "Socket", "name": "Jewel Socket", "links": [2]},
    }

    class _FakePruneWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()

        def get_cached_tree_data(self, _ver: Any = None) -> dict[str, Any]:
            return {"nodes": nodes_db}

        def call(self, method: str, params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            if method == "get_tree_state":
                return {
                    "treeVersion": "3_30",
                    "allocatedNodes": [1, 2, 3, 4, 20],
                    "dynamicNodes": [],
                    "overrides": {},
                    "sockets": [{"nodeId": 20, "expansion": False}],
                }
            if method == "list_items":
                return {"slots": [{"nodeId": 20, "socketIndex": 1, "selItemId": 0}], "items": []}
            if method == "eval_node_paths":
                base = {"CombinedDPS": 1000.0, "TotalEHP": 10000.0, "Dex": 160.0, "ReqDex": 155.0}
                results = []
                for c in params["candidates"]:
                    nids = set(c["nodeIds"])
                    if nids == {20}:
                        st = dict(base)
                    elif nids == {3}:
                        st = {**base, "CombinedDPS": 980.0, "TotalEHP": 9900.0}
                    elif nids == {4}:
                        st = {**base, "CombinedDPS": 990.0, "Dex": 143.0}
                    else:
                        st = dict(base)
                    results.append({"key": c["key"], "mode": "remove", "nodeCount": len(nids), "outputStats": st, "deltas": {}})
                return {"useFullDPS": False, "baseStats": base, "results": results, "elapsedMs": 3.0}
            return {}

    res = prune_tree(_FakePruneWorker(), top=10, max_branch_pts=3)  # type: ignore[arg-type]
    assert res["safeCount"] == 2
    assert res["unsafeCount"] == 1
    assert res["safeBranches"][0]["nodeIds"] == [20]
    assert res["safeBranches"][0]["dpsPctPerPt"] == 0.0
    assert res["safeBranches"][1]["nodeIds"] == [3]
    assert res["safeBranches"][1]["dpsPctPerPt"] == pytest.approx(-2.0)
    assert res["unsafeBranches"][0]["nodeIds"] == [4]
    assert "Dex -12 deficit" in res["unsafeBranches"][0]["guardrails"]["violations"]

    res_ref = prune_tree(_FakePruneWorker(), top=10, max_branch_pts=3, target_slot="ref")  # type: ignore[arg-type]
    assert res_ref["safeBranches"][0]["deallocCommand"] == "pob tree change --target ref --deallocate 20"

    md_default = format_tree_prune_markdown(res, include_unsafe=False)
    assert "Unsafe Branches" not in md_default
    assert "--include-unsafe" in md_default
    md_unsafe = format_tree_prune_markdown(res, include_unsafe=True)
    assert "Unsafe Branches" in md_unsafe
    assert "[UNSAFE: Dex -12 deficit]" in md_unsafe


def test_threshold_probes_mre_coc_suppression_accuracy_and_speed() -> None:
    # 1. Exact MRE 25% & 50% aura math with PoE1 integer rounding.
    # 1000 Mana, currently 65% reserved (650 mana, 0% MRE), ManaCost 20 -> need >=20 unreserved.
    # Adding a 25% aura gives 90% base reserved (900 mana -> 100 unreserved >= 20 -> fitsNow=True, +0% MRE!).
    m25 = compute_mre_needed_for_aura(
        max_mana=1000,
        mana_unreserved=350,
        mana_reserved_pct=65.0,
        current_mre_pct=0.0,
        active_mana_cost=20.0,
        extra_aura_base_pct=25.0,
    )
    assert m25["fitsNow"] is True
    assert m25["additionalMreNeededPct"] == 0

    # Adding a 50% aura gives 115% base reserved -> needs MRE so ceil(1000 * round(11500/(1+mre/100))/10000) <= 980.
    # At +18% MRE: 65/1.18=55.08% (551) + 50/1.18=42.37% (424) = 975 -> 25 unreserved >= 20!
    # At +17% MRE: 65/1.17=55.56% (556) + 50/1.17=42.74% (428) = 984 -> 16 unreserved < 20!
    m50 = compute_mre_needed_for_aura(
        max_mana=1000,
        mana_unreserved=350,
        mana_reserved_pct=65.0,
        current_mre_pct=0.0,
        active_mana_cost=20.0,
        extra_aura_base_pct=50.0,
    )
    assert m50["fitsNow"] is False
    assert m50["additionalMreNeededPct"] == 18
    assert m50["unreservedAfter"] >= 20

    # 2. Full threshold_probes with CoC 0.033s tick cliff + Suppression gap + Accuracy + Attributes.
    stats_coc = {
        "SpellSuppressionChance": 86,
        "EffectiveSpellSuppressionChance": 86,
        "Mana": 1000,
        "ManaUnreserved": 120,
        "ManaReservedPercent": 88.0,
        "ManaReservationEfficiency": 20.0,
        "ManaCost": 25,
        "CooldownRecovery": 14.0,  # CoC 0.15s / 1.14 = 0.13158s -> 4 ticks = 0.132s = 7.58/s
        "Speed": 7.80,  # Exceeds 7.58/s -> OVERSPEED CLIFF!
        "HitChance": 97.5,
        "Str": 115,
        "ReqStr": 111,
        "Dex": 140,
        "ReqDex": 155,
        "Int": 220,
        "ReqInt": 155,
    }
    skills_coc = {
        "groups": [
            {
                "index": 1,
                "enabled": True,
                "gems": [
                    {"name": "Cyclone", "enabled": True},
                    {"name": "Cast On Critical Strike Support", "enabled": True},
                    {"name": "Ice Nova", "enabled": True},
                ],
            }
        ]
    }
    t0 = time.perf_counter()
    probes = compute_threshold_probes(stats_coc, skills_res=skills_coc)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 20.0
    assert probes["spellSuppression"]["gapTo100Pct"] == 14.0
    assert probes["triggerBreakpoint"]["ticks"] == 4
    assert probes["triggerBreakpoint"]["effectiveCdSec"] == pytest.approx(0.132)
    assert probes["triggerBreakpoint"]["overspeedCliff"] is True
    assert probes["accuracy"]["gapTo100Pct"] == pytest.approx(2.5)
    assert probes["attributes"]["Dex"]["surplus"] == -15
    assert probes["attributes"]["Dex"]["status"] == "deficit"
    assert probes["attributes"]["Str"]["status"] == "tight"

    md_lines = format_threshold_probes_markdown(probes)
    joined = "\n".join(md_lines)
    assert "Spell Suppression" in joined and "-14%" in joined
    assert "[OVERSPEED CLIFF]" in joined
    assert "Accuracy `97.5%`" in joined
    assert "Dex `140/155` (-15 [DEFICIT])" in joined

    diag_md = format_diagnose_markdown({"thresholdProbes": probes, "healthGaps": []})
    assert "Threshold Gap Probes" in diag_md
    assert "[OVERSPEED CLIFF]" in diag_md
    assert probes["manaReservation"]["estimated"] is False

    # Missing ManaReservationEfficiency marks probe as estimated=True.
    probes_est = compute_threshold_probes({"Mana": 1000, "ManaUnreserved": 350, "ManaReservedPercent": 65.0, "ManaCost": 20})
    assert probes_est["manaReservation"]["estimated"] is True
    assert probes_est["manaReservation"]["extra50Aura"]["estimated"] is True


@pytest.mark.skipif(_ENGINE_DIR is None, reason="No runnable PoB engine found")
def test_real_engine_tree_prune_latency_guardrails_and_threshold_probes() -> None:
    assert _ENGINE_DIR is not None
    # Witch build with empty basic socket 6230 + multiple prunable branches + Int-bearing twigs.
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

        slack = float(os.environ.get("POB_TEST_PERF_SLACK", "1") or "1")
        t0 = time.perf_counter()
        prune_res = prune_tree(w, top=10, max_branch_pts=4, sort_by="dps")
        prune_sec = time.perf_counter() - t0
        assert prune_sec < 0.5 * slack, f"prune_tree exceeded 0.5s budget: {prune_sec:.3f}s"
        assert prune_res["evaluatedCount"] > 0
        assert prune_res["safeBranches"], "Expected safe prunable branches"
        # Empty allocated socket 6230 has 0% DPS/EHP loss and sorts right at the top.
        assert prune_res["safeBranches"][0]["nodeIds"] == [6230]
        assert prune_res["safeBranches"][0]["dpsPctPerPt"] == pytest.approx(0.0, abs=1e-6)
        assert prune_res["safeBranches"][0]["deallocCommand"] == "pob tree change --deallocate 6230"

        # Zero mutation residue check.
        ts_after = w.call("get_tree_state", {})
        assert ts_after["allocatedNodes"] == ts_before["allocatedNodes"]

        # Threshold probes <20ms on real PoB stats.
        stats = w.call("get_stats", {}).get("stats", {})
        t1 = time.perf_counter()
        probes = compute_threshold_probes(stats)
        probe_ms = (time.perf_counter() - t1) * 1000.0
        assert probe_ms < 20.0 * slack, f"threshold_probes exceeded 20ms budget: {probe_ms:.3f}ms"
        assert probes["manaReservation"]["maxMana"] > 0
        assert probes["attributes"]["Int"]["current"] > 0
    finally:
        w.stop()


@pytest.mark.skipif(_ENGINE_DIR is None, reason="No runnable PoB engine found")
def test_real_engine_mre_probe_ground_truth_with_sovereignty_and_auras() -> None:
    """Verify MRE probe reads true engine ManaReservationEfficiency + per-aura reservations and fits in PoB."""
    assert _ENGINE_DIR is not None

    def _build_xml(*, extra_mre_pct: int = 0, include_extra_25_aura: bool = False) -> str:
        custom_mods = f"{24 + extra_mre_pct}% increased Mana Reservation Efficiency of Skills"
        extra_gem = (
            '<Gem nameSpec="Herald of Thunder" level="20" quality="20" enabled="true"/>'
            if include_extra_25_aura
            else ""
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447"/></Tree>
  <Skills activeSkillSet="1"><SkillSet id="1">
    <Skill mainActiveSkill="1" enabled="true" label="Main"><Gem nameSpec="Arc" level="20" quality="20" enabled="true"/></Skill>
    <Skill enabled="true" label="Auras">
      <Gem nameSpec="Determination" level="20" quality="20" enabled="true"/>
      <Gem nameSpec="Purity of Elements" level="20" quality="20" enabled="true"/>
      {extra_gem}
    </Skill>
  </SkillSet></Skills>
  <Items activeItemSet="1"><ItemSet id="1"/></Items>
  <Config activeConfigSet="1"><ConfigSet id="1"><Input name="customMods" string="{custom_mods}"/></ConfigSet></Config>
</PathOfBuilding>"""

    w = BridgeWorker(_ENGINE_DIR)
    w.start()
    try:
        # Base build has 24% MRE (Sovereignty-equivalent) + two 50% auras (Determination + Purity of Elements).
        w.call("import_code", {"code": xml_to_share_code(_build_xml()), "mode": "replace"})
        st_res = w.call("get_stats", {})
        stats = st_res.get("stats") or {}
        probe_res = w.call("agent_probe_build", {})
        probes = compute_threshold_probes(
            stats,
            agent_probe=probe_res,
            mana_reservations=st_res.get("manaReservations"),
        )
        mr = probes["manaReservation"]
        assert mr["estimated"] is False
        assert mr["currentMrePct"] == pytest.approx(24.0, abs=0.1)
        need_25 = mr["extra25Aura"]["additionalMreNeededPct"]
        assert isinstance(need_25, int) and need_25 > 0

        # Apply the exact predicted +{need_25}% MRE and enable the extra 25% aura (Herald of Thunder) in real PoB!
        w.call(
            "import_code",
            {"code": xml_to_share_code(_build_xml(extra_mre_pct=need_25, include_extra_25_aura=True)), "mode": "replace"},
        )
        st_after = w.call("get_stats", {}).get("stats") or {}
        assert float(st_after["ManaUnreserved"]) >= float(st_after["ManaCost"]), (
            f"Expected ManaUnreserved >= ManaCost with +{need_25}% MRE, got "
            f"{st_after['ManaUnreserved']} < {st_after['ManaCost']}"
        )

        # And with (need_25 - 1)% MRE, PoB must NOT satisfy ManaUnreserved >= ManaCost (tightness proof!).
        w.call(
            "import_code",
            {
                "code": xml_to_share_code(_build_xml(extra_mre_pct=need_25 - 1, include_extra_25_aura=True)),
                "mode": "replace",
            },
        )
        st_under = w.call("get_stats", {}).get("stats") or {}
        assert float(st_under["ManaUnreserved"]) < float(st_under["ManaCost"]), (
            f"Expected +{need_25 - 1}% MRE to be strictly insufficient, got "
            f"{st_under['ManaUnreserved']} >= {st_under['ManaCost']}"
        )
    finally:
        w.stop()
