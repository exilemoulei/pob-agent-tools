"""Tests for Layer 3: multi-stage PoB XML (`Spec`/`ItemSet`/`SkillSet`) selection,
`#stage=` fragments, `compare --ref-stage`, and structured 5-section `pob tree diff`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

from pob_cli.analyzer.tree_diff import _diff_snapshots, diff_tree_specs
from pob_cli.bridge_client import BridgeWorker
from pob_cli.cli import main
from pob_cli.formatters.tree_diff_md import (
    format_progression_stages_markdown,
    format_tree_diff_markdown,
)
from pob_cli.importers import load_build_into_worker, xml_to_share_code
from pob_cli.importers_stages import (
    apply_stage_selection_to_xml,
    extract_available_stages,
    split_source_stage_fragment,
)
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

_MULTISTAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1">
    <Spec title="Leveling" treeVersion="3_30" classId="3" ascendClassId="2" nodes="58833,55373,38048,55906,15144,918"/>
    <Spec title="Endgame" treeVersion="3_30" classId="3" ascendClassId="2" nodes="58833,55373,38048,55906,6230,15144,918,28221,24643,33545,18033,46092,51923,48778,37671,27415,32710,49605"/>
  </Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1" title="Leveling">
      <Skill mainActiveSkill="1" enabled="true" label="Main">
        <Gem nameSpec="Arc" level="10" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
    <SkillSet id="2" title="Endgame">
      <Skill mainActiveSkill="1" enabled="true" label="Main">
        <Gem nameSpec="Arc" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Added Lightning Damage" level="20" quality="20" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">Rarity: RARE
Starter Twig
Driftwood Wand
Implicits: 1
11% increased Spell Damage</Item>
    <Item id="2">Rarity: RARE
Storm Sceptre
Profane Wand
Implicits: 1
14% increased Cast Speed
100% increased Spell Damage
+1 to Level of all Lightning Spell Skill Gems
Adds 30 to 90 Lightning Damage to Spells</Item>
    <ItemSet id="1" title="Leveling">
      <Slot name="Weapon 1" itemId="1"/>
    </ItemSet>
    <ItemSet id="2" title="Endgame">
      <Slot name="Weapon 1" itemId="2"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>"""


def test_split_source_stage_fragment_and_xml_stage_selection() -> None:
    base, frag = split_source_stage_fragment("https://pobb.in/abc123#stage=Endgame")
    assert base == "https://pobb.in/abc123"
    assert frag == {"stage": "Endgame"}

    base2, frag2 = split_source_stage_fragment("@build.xml#spec=2&item-set=Endgame&skill-set=2")
    assert base2 == "@build.xml"
    assert frag2 == {"spec": "2", "item_set": "Endgame", "skill_set": "2"}

    # PoE account discriminator must NEVER be stripped!
    acc, frag_acc = split_source_stage_fragment("ExilePlayer#1234")
    assert acc == "ExilePlayer#1234" and frag_acc == {}

    stages = extract_available_stages(_MULTISTAGE_XML)
    assert stages["hasMultipleStages"] is True
    assert len(stages["specs"]) == 2
    assert len(stages["itemSets"]) == 2
    assert len(stages["skillSets"]) == 2
    assert stages["specs"][0]["active"] is True

    updated_xml, info = apply_stage_selection_to_xml(_MULTISTAGE_XML, stage="Endgame")
    assert info["selected"]["spec"]["index"] == 2
    assert info["selected"]["itemSet"]["id"] == "2"
    assert info["selected"]["skillSet"]["id"] == "2"
    assert info["stages"]["specs"][1]["active"] is True
    assert 'activeSpec="2"' in updated_xml
    assert 'activeItemSet="2"' in updated_xml
    assert 'activeSkillSet="2"' in updated_xml

    md_stages = format_progression_stages_markdown(info["stages"], info["selected"])
    assert "Progression Stages" in md_stages
    assert "Endgame" in md_stages

    with pytest.raises(ValueError, match="did not match"):
        apply_stage_selection_to_xml(_MULTISTAGE_XML, stage="NonexistentStage")

    # Non-sequential ItemSet IDs (<ItemSet id="2"/> at pos 1, <ItemSet id="7"/> at pos 2):
    # unified `--stage 2` MUST match by 1-based positional index (picking id="7"), while `--item-set 2` picks id="2".
    nonseq_xml = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Tree activeSpec="1">
    <Spec title="Early" treeVersion="3_30" nodes="1"/>
    <Spec title="Late" treeVersion="3_30" nodes="1,2"/>
    <Spec title="Uber" treeVersion="3_30" nodes="1,2,3"/>
  </Tree>
  <Items activeItemSet="2">
    <ItemSet id="2" title="Early Gear"/>
    <ItemSet id="7" title="Late Gear"/>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1" title="Default Skills"/>
  </Skills>
</PathOfBuilding>"""
    xml_st2, info_st2 = apply_stage_selection_to_xml(nonseq_xml, stage="2")
    assert info_st2["selected"]["spec"]["index"] == 2
    assert info_st2["selected"]["itemSet"]["id"] == "7"
    assert 'activeItemSet="7"' in xml_st2

    # Explicit `--item-set 2` still resolves by XML id="2".
    xml_is2, info_is2 = apply_stage_selection_to_xml(nonseq_xml, item_set="2")
    assert info_is2["selected"]["itemSet"]["id"] == "2"
    assert 'activeItemSet="2"' in xml_is2

    # Stage 3 matches Spec #3 but misses ItemSet (only 2 item sets) -> records concise stageNotes warning.
    _, info_st3 = apply_stage_selection_to_xml(nonseq_xml, stage="3")
    assert info_st3["selected"]["spec"]["index"] == 3
    assert "itemSet" not in info_st3["selected"]
    assert info_st3["selected"].get("stageNotes")
    assert "ItemSet" in info_st3["selected"]["stageNotes"][0]


def test_cli_stage_options_and_tree_diff_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xml_file = tmp_path / "multi.xml"
    xml_file.write_text(_MULTISTAGE_XML, encoding="utf-8")

    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon(action: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        calls.append((action, dict(params or {})))
        if action == "load":
            return {
                "importMethod": "xml_file",
                "buildInfo": {"className": "Witch", "ascendClassName": "Elementalist", "level": 90, "treeVersion": "3_30", "points": {"used": 18, "usedMax": 120, "ascUsed": 0, "ascMax": 8}},
                "buildHeader": {"mainSocketGroup": {"index": 1, "list": ["Arc"]}, "mainSkill": {"index": 1, "list": ["Arc"]}},
                "availableStages": extract_available_stages(_MULTISTAGE_XML),
                "selectedStage": {"spec": {"index": 2, "title": "Endgame"}},
            }
        if action == "compare":
            return {
                "mainBuildInfo": {"className": "Witch", "ascendClassName": "Elementalist", "level": 90},
                "refBuildInfo": {"className": "Witch", "ascendClassName": "Elementalist", "level": 90},
                "baselines": {"original": [], "envAlignedToMain": {"rows": []}, "pinnacleDewatered": {"rows": []}},
                "treeDiff": {},
                "skillDiff": {},
                "itemSlotDiff": [],
            }
        if action == "tree_diff":
            return {
                "mode": "specs" if (params or {}).get("spec_a") else "slots",
                "summary": {"labelA": "current", "labelB": "ref", "pointsA": 10, "pointsB": 15, "pointsDelta": 5, "sharedPoints": 9, "ascendancyAdded": [], "ascendancyRemoved": []},
                "keystones": {"onlyInRef": [], "onlyInCurrent": [], "shared": []},
                "wheels": {"onlyInRef": [], "onlyInCurrent": [], "partialDeltas": [], "highwayTravel": {"currentPoints": 2, "refPoints": 3, "delta": 1}},
                "masteries": {"onlyInRef": [], "onlyInCurrent": [], "changedEffect": []},
                "sockets": {"allocatedCountA": 0, "allocatedCountB": 1, "delta": 1, "onlyInRef": [6230], "onlyInCurrent": [], "outerClustersA": [], "outerClustersB": []},
            }
        return {}

    monkeypatch.setattr("pob_cli.cli._daemon_call", _fake_daemon)
    runner = CliRunner()

    r_load = runner.invoke(main, ["load", f"@{xml_file}#stage=Endgame"])
    assert r_load.exit_code == 0, r_load.output
    assert calls[-1][0] == "load"
    assert calls[-1][1]["stage"] == "Endgame"
    assert "Progression Stages" in r_load.output

    r_cmp = runner.invoke(main, ["compare", f"@{xml_file}", "--ref-stage", "Endgame", "--json"])
    assert r_cmp.exit_code == 0, r_cmp.output
    assert calls[-1][0] == "compare"
    assert calls[-1][1]["ref_stage"] == "Endgame"

    r_diff_slots = runner.invoke(main, ["tree", "diff", "--slot", "current", "--against", "ref"])
    assert r_diff_slots.exit_code == 0, r_diff_slots.output
    assert calls[-1][0] == "tree_diff"
    assert calls[-1][1]["target"] == "main"
    assert calls[-1][1]["ref_target"] == "ref"
    assert "Passive Tree Diff" in r_diff_slots.output

    r_diff_specs = runner.invoke(main, ["tree", "diff", "--spec-a", "Leveling", "--spec-b", "Endgame", "--json"])
    assert r_diff_specs.exit_code == 0, r_diff_specs.output
    assert calls[-1][0] == "tree_diff"
    assert calls[-1][1]["spec_a"] == "Leveling"
    assert calls[-1][1]["spec_b"] == "Endgame"
    assert json.loads(r_diff_specs.output)["mode"] == "specs"


def test_tree_diff_aggregates_all_five_sections_including_mastery_effect_change() -> None:
    nodes_db = {
        "1": {"id": 1, "type": "ClassStart", "name": "Witch", "links": [2]},
        "2": {"id": 2, "type": "Normal", "name": "Intelligence", "stats": ["+10 to Intelligence"], "group": 1, "links": [1, 3, 10, 20, 30]},
        # Wheel A (shared, mastery effect changed!)
        "3": {"id": 3, "type": "Notable", "name": "Arcane Dominion", "group": 200, "links": [2]},
        "4": {
            "id": 4,
            "type": "Mastery",
            "name": "Caster Mastery",
            "group": 200,
            "links": [],
            "masteryEffects": [
                {"effect": 1001, "stats": ["+1 to Level of all Spell Skill Gems"]},
                {"effect": 1002, "stats": ["25% chance to open nearby Chests when you Cast a Spell"]},
            ],
        },
        # Wheel B (only in Ref, 1 hop from node 2)
        "10": {"id": 10, "type": "Normal", "name": "Lightning", "stats": ["10% increased Lightning Damage"], "group": 300, "links": [2, 11]},
        "11": {"id": 11, "type": "Notable", "name": "Heart of Thunder", "group": 300, "links": [10]},
        # Keystone only in Ref
        "20": {"id": 20, "type": "Keystone", "name": "Mind Over Matter", "stats": ["40% of Damage is taken from Mana before Life"], "links": [2]},
        # Jewel socket only in Current
        "30": {"id": 30, "type": "Socket", "name": "Jewel Socket", "links": [2]},
        # Ascendancy node only in Ref
        "60": {"id": 60, "type": "Notable", "name": "Shaper of Storms", "asc": "Elementalist", "stats": ["Your Hits always Shock"], "links": []},
    }
    snap_a = {
        "label": "current",
        "allocatedNodes": [1, 2, 3, 4, 30],
        "masterySelections": {4: 1002},
        "sockets": [{"nodeId": 30, "expansion": False}],
        "itemsRes": {"items": [], "slots": []},
    }
    snap_b = {
        "label": "ref",
        "allocatedNodes": [1, 2, 3, 4, 10, 11, 20, 60],
        "masterySelections": {4: 1001},
        "sockets": [{"nodeId": 30, "expansion": False}],
        "itemsRes": {"items": [], "slots": []},
    }

    diff = _diff_snapshots(snap_a, snap_b, nodes_db)
    assert diff["summary"]["ascendancyAdded"][0]["name"] == "Shaper of Storms"
    assert diff["keystones"]["onlyInRef"][0]["name"] == "Mind Over Matter"
    assert diff["wheels"]["onlyInRef"][0]["name"] == "Heart of Thunder"
    assert diff["wheels"]["onlyInRef"][0]["shortestHopFromCurrent"] == 1
    assert len(diff["masteries"]["changedEffect"]) == 1
    chg = diff["masteries"]["changedEffect"][0]
    assert chg["current"]["allocSpec"] == "4:1002"
    assert chg["ref"]["allocSpec"] == "4:1001"
    assert diff["sockets"]["onlyInCurrent"] == [30]

    md = format_tree_diff_markdown(diff)
    assert "Shaper of Storms" in md
    assert "Mind Over Matter" in md
    assert "Heart of Thunder" in md and "1 hops" in md
    assert "`4:1002` -> `4:1001`" in md
    assert "Sockets Only in Current" in md

    # Per-socket cluster notable grouping: two occupied outer cluster sockets must NOT cross-contaminate notables!
    cluster_nodes_db = {
        "1": {"id": 1, "type": "ClassStart", "name": "Witch", "links": [100, 200]},
        "100": {"id": 100, "type": "Socket", "name": "Outer NE", "links": [1], "expansion": True, "x": 4000, "y": -4000},
        "200": {"id": 200, "type": "Socket", "name": "Outer NW", "links": [1], "expansion": True, "x": -4000, "y": -4000},
    }
    dyn_nodes = [
        {"id": 65536, "type": "Normal", "name": "Small NE", "links": [100, 65537]},
        {"id": 65537, "type": "Notable", "name": "Storm Drinker", "links": [65536]},
        {"id": 65600, "type": "Normal", "name": "Small NW", "links": [200, 65601]},
        {"id": 65601, "type": "Notable", "name": "Prismatic Heart", "links": [65600]},
    ]
    snap_cl = {
        "label": "current",
        "allocatedNodes": [1, 100, 200, 65536, 65537, 65600, 65601],
        "dynamicNodes": dyn_nodes,
        "overrides": {},
        "sockets": [
            {"nodeId": 100, "expansion": True, "expansionSize": 2, "itemId": 11},
            {"nodeId": 200, "expansion": True, "expansionSize": 2, "itemId": 12},
        ],
        "itemsRes": {
            "items": [
                {"id": 11, "title": "Cluster NE", "raw": "Added Small Passive Skills grant: 12% increased Lightning Damage"},
                {"id": 12, "title": "Cluster NW", "raw": "Added Small Passive Skills grant: 12% increased Elemental Damage"},
            ],
            "slots": [],
        },
    }
    diff_cl = _diff_snapshots(snap_cl, snap_cl, cluster_nodes_db)
    by_sock = {c["nodeId"]: c["allocatedClusterNotables"] for c in diff_cl["sockets"]["outerClustersA"]}
    assert by_sock[100] == ["Storm Drinker"]
    assert by_sock[200] == ["Prismatic Heart"]


def test_tree_diff_markdown_sequential_section_numbering_without_keystones() -> None:
    import re

    diff_no_ks = {
        "mode": "specs",
        "summary": {"labelA": "Early", "labelB": "Endgame", "pointsA": 80, "pointsB": 115, "pointsDelta": 35, "sharedPoints": 70, "ascendancyAdded": [{"id": 60, "name": "Shaper of Storms"}], "ascendancyRemoved": []},
        "respecGold": {"characterLevel": 90, "goldPerPoint": 8450, "ascendancyMultiplier": 5, "deallocatedRegularCount": 10, "deallocatedAscendancyCount": 0, "estimatedGoldRespecTotal": 84500},
        "keystones": {"onlyInRef": [], "onlyInCurrent": [], "shared": []},
        "wheels": {"onlyInRef": [], "onlyInCurrent": [], "partialDeltas": [], "highwayTravel": {"currentPoints": 10, "refPoints": 12, "delta": 2}},
        "masteries": {"onlyInRef": [{"allocSpec": "4:1001", "wheel": "Caster", "stats": ["+1 Spell"]}], "onlyInCurrent": [], "changedEffect": []},
        "sockets": {"allocatedCountA": 2, "allocatedCountB": 3, "delta": 1, "onlyInRef": [6230], "onlyInCurrent": [], "outerClustersA": [], "outerClustersB": []},
    }
    md = format_tree_diff_markdown(diff_no_ks)
    assert "Estimated Respec Cost" in md and "84,500 Gold" in md
    assert re.findall(r"^#### (\d+)\. (.+)$", md, flags=re.MULTILINE) == [
        ("1", "Ascendancy & Bloodline Diff"), ("2", "Core Wheels & Highway Travel"),
        ("3", "Masteries"), ("4", "Cluster Jewels & Jewel Sockets"),
    ]
    diff_min = {**diff_no_ks, "summary": {**diff_no_ks["summary"], "ascendancyAdded": []}, "masteries": {"onlyInRef": [], "onlyInCurrent": [], "changedEffect": []}}
    assert re.findall(r"^#### (\d+)\. (.+)$", format_tree_diff_markdown(diff_min), flags=re.MULTILINE) == [
        ("1", "Core Wheels & Highway Travel"), ("2", "Cluster Jewels & Jewel Sockets"),
    ]


@pytest.mark.skipif(_ENGINE_DIR is None, reason="No runnable PoB engine found")
def test_real_engine_multistage_switching_and_spec_tree_diff() -> None:
    assert _ENGINE_DIR is not None
    code_multi = xml_to_share_code(_MULTISTAGE_XML)

    w = BridgeWorker(_ENGINE_DIR)
    w.start()
    try:
        # 1. Load Stage 1 ("Leveling")
        res1 = load_build_into_worker(w, xml_code=code_multi, stage="Leveling")
        assert res1["availableStages"]["hasMultipleStages"] is True
        assert res1["selectedStage"]["spec"]["index"] == 1
        dps1 = float((w.call("get_stats", {}).get("stats") or {}).get("CombinedDPS") or 0.0)
        nodes1 = set(w.call("get_tree_state", {}).get("allocatedNodes") or [])
        skills1 = w.call("list_skills", {})
        items1 = w.call("list_items", {})
        w1_slot1 = next((s for s in items1.get("slots", []) if s.get("name") == "Weapon 1"), {})
        w1_item1 = next((it for it in items1.get("items", []) if it.get("id") == w1_slot1.get("selItemId")), {})
        assert "Starter Twig" in str(w1_item1.get("title") or w1_item1.get("name") or "")
        assert len(skills1["groups"][0]["gems"]) == 1

        # 2. Load Stage 2 via `#stage=Endgame` URL/code fragment -> DPS, gear, gems, and tree ALL update!
        res2 = load_build_into_worker(w, source=f"{code_multi}#stage=Endgame")
        assert res2["selectedStage"]["spec"]["index"] == 2
        assert res2["selectedStage"]["itemSet"]["id"] == "2"
        assert res2["selectedStage"]["skillSet"]["id"] == "2"
        dps2 = float((w.call("get_stats", {}).get("stats") or {}).get("CombinedDPS") or 0.0)
        nodes2 = set(w.call("get_tree_state", {}).get("allocatedNodes") or [])
        skills2 = w.call("list_skills", {})
        items2 = w.call("list_items", {})
        w1_slot2 = next((s for s in items2.get("slots", []) if s.get("name") == "Weapon 1"), {})
        w1_item2 = next((it for it in items2.get("items", []) if it.get("id") == w1_slot2.get("selItemId")), {})
        assert "Storm Sceptre" in str(w1_item2.get("title") or w1_item2.get("name") or "")
        assert len(skills2["groups"][0]["gems"]) == 2
        assert dps2 > dps1 * 2.0, f"Expected Endgame stage DPS ({dps2}) >> Leveling DPS ({dps1})"
        assert len(nodes2) > len(nodes1)

        # 3. Run intra-build `diff_tree_specs` between Leveling and Endgame without mutating active spec.
        diff = diff_tree_specs(w, spec_a="Leveling", spec_b="Endgame")
        assert diff["mode"] == "specs"
        assert "active ItemSet" in str(diff.get("socketsNote") or "")
        assert diff["summary"]["pointsB"] > diff["summary"]["pointsA"]
        assert 6230 in diff["sockets"]["onlyInRef"]
        assert diff["wheels"]["onlyInRef"] or diff["wheels"]["partialDeltas"]
        md = format_tree_diff_markdown(diff)
        assert "Passive Tree Diff" in md
        assert "6230" in md
        assert "Intra-build Spec diff compares passive allocations only" in md
    finally:
        w.stop()
