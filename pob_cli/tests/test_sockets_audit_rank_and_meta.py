"""Tests for item 73 Layer 3: `[EMPTY-ALLOCATED-SOCKET]` audit gap, `rank-nodes`/`diagnose`
nearby jewel/cluster socket rendering, `pob meta` bilingual EN/ZH/slang resolution, and `SKILL.md`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner

from pob_cli import external_data as ext
from pob_cli.analyzer.audit import _collect_static_health_issues
from pob_cli.cli_data import cmd_meta
from pob_cli.cli_optimize import cmd_rank_nodes
from pob_cli.comparator.ranking import rank_nodes
from pob_cli.formatters.diagnose_md import format_diagnose_markdown


def test_audit_flags_empty_allocated_jewel_sockets_and_ignores_occupied_or_charm() -> None:
    tree_summary = {
        "jewelSockets": [
            {"nodeId": 26725, "socketIndex": 1, "kind": "basic", "allocated": True, "occupied": False},
            {"nodeId": 7960, "socketIndex": 2, "kind": "large_cluster_outer", "sector": "NE (Witch/Shadow)", "allocated": True, "occupied": False},
            {"nodeId": 32763, "socketIndex": 3, "kind": "basic", "allocated": True, "occupied": True, "itemId": 7},
            {"nodeId": 18060, "kind": "charm", "allocated": True, "occupied": False},
        ],
        "socketSummary": {"allocatedTotal": 3, "allocatedOccupied": 1, "allocatedEmptyIds": [7960, 26725], "activeOuterClusters": 0},
    }
    issues = _collect_static_health_issues({}, {"points": {}}, {"slots": []}, {}, [], tree_summary=tree_summary)
    sock_msgs = [i["message"] for i in issues if "[EMPTY-ALLOCATED-SOCKET]" in i["message"]]
    assert len(sock_msgs) == 2
    assert any("nodeId=26725 (#1)" in m for m in sock_msgs)
    assert any("nodeId=7960 (#2) (large_cluster_outer, NE (Witch/Shadow))" in m for m in sock_msgs)


def test_rank_nodes_and_cli_render_nearby_sockets_without_synthetic_dps(monkeypatch: Any) -> None:
    class _FakeWorker:
        class _Lock:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_a: Any) -> None:
                return None

        lock = _Lock()

        def call(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
            if method == "node_power":
                return {
                    "stat": "CombinedDPS",
                    "stats": ["CombinedDPS"],
                    "report": [
                        {"id": 500, "name": "Arcane Focus", "type": "Notable", "allocated": False, "pathDist": 2, "power": 100.0, "pathPower": 50.0, "powerStr": "+100", "pathPowerStr": "+50"}
                    ],
                    "socketSummary": {"allocatedTotal": 1, "allocatedOccupied": 0, "allocatedEmptyIds": [26725], "activeOuterClusters": 0},
                    "nearbySockets": [
                        {"nodeId": 26725, "kind": "basic", "allocated": True, "occupied": False, "cost": 0, "pathIds": [], "travelStats": []},
                        {"nodeId": 7960, "kind": "large_cluster_outer", "sector": "NE (Witch/Shadow)", "allocated": False, "occupied": False, "cost": 2, "pathIds": [41263, 7960], "travelStats": ["+10 to Intelligence"]},
                    ],
                }
            if method == "list_items":
                return {"slots": [], "items": {}}
            raise AssertionError(method)

    res = rank_nodes(_FakeWorker(), stat="CombinedDPS", max_dist=5, top=5)  # type: ignore[arg-type]
    assert res["socketSummary"]["allocatedEmptyIds"] == [26725]
    assert len(res["nearbySockets"]) == 2
    # R5 guard: socket candidates must never carry synthetic DPS/power fields
    for sock in res["nearbySockets"]:
        assert not ({"power", "pathPower", "dps", "gain"} & set(sock))

    monkeypatch.setattr("pob_cli.cli_optimize.invoke_daemon", lambda *_a, **_k: res)
    out = CliRunner().invoke(cmd_rank_nodes, [])
    assert out.exit_code == 0, out.output
    assert "[EMPTY-ALLOCATED-SOCKET]" in out.output
    assert "Nearby Unallocated Jewel & Cluster Sockets" in out.output
    assert "Outer Large Cluster (NE (Witch/Shadow))" in out.output

    # max_dist=1 filters unallocated sockets costing >1 while keeping cost=0 allocated empty socket and full socketSummary
    res_d1 = rank_nodes(_FakeWorker(), stat="CombinedDPS", max_dist=1, top=5)  # type: ignore[arg-type]
    assert res_d1["socketSummary"] == res["socketSummary"]
    assert [s["nodeId"] for s in res_d1["nearbySockets"]] == [26725]
    monkeypatch.setattr("pob_cli.cli_optimize.invoke_daemon", lambda *_a, **_k: res_d1)
    out_d1 = CliRunner().invoke(cmd_rank_nodes, ["--max-dist", "1"])
    assert out_d1.exit_code == 0, out_d1.output
    assert "[EMPTY-ALLOCATED-SOCKET]" in out_d1.output
    assert "7960" not in out_d1.output


def test_diagnose_markdown_renders_nearby_sockets_and_applies_n1_fold() -> None:
    base_diag: dict[str, Any] = {
        "env": {"game": "poe1", "pobVersion": "2.69", "rev": 1, "target": "main"},
        "buildInfo": {"level": 88, "className": "Witch", "ascendClassName": "Necromancer", "points": {"used": 100, "usedMax": 110}},
        "offence": {},
        "defence": {},
        "healthGaps": [],
        "nearbySockets": [
            {"nodeId": 7960, "kind": "large_cluster_outer", "sector": "NE (Witch/Shadow)", "allocated": False, "cost": 2, "travelStats": ["+10 to Intelligence"]},
        ],
    }
    md_open = format_diagnose_markdown({**base_diag, "socketSummary": {"allocatedEmptyIds": [], "activeOuterClusters": 0}})
    assert "Nearby Unallocated Jewel & Cluster Sockets" in md_open
    assert "7960" in md_open

    md_fold = format_diagnose_markdown({**base_diag, "socketSummary": {"allocatedEmptyIds": [10, 11], "activeOuterClusters": 0}})
    assert "Fill existing 2 empty allocated jewel sockets first" in md_fold
    assert "7960" not in md_fold


def test_query_meta_bilingual_zh_tw_slang_and_en_substring(monkeypatch: Any) -> None:
    fake_state = {
        "leagueBuilds": [
            {
                "leagueName": "Covens",
                "leagueUrl": "covens",
                "total": 100000,
                "hardcore": False,
                "category": 0,
                "statistics": [
                    {"class": "Chieftain", "skill": "Righteous Fire", "percentage": 8.5, "trend": 1},
                    {"class": "Hierophant", "skill": "Ice Nova of Frostbolts", "percentage": 6.2, "trend": 0},
                    {"class": "Necromancer", "skill": "Raise Spectre", "percentage": 5.0, "trend": 0},
                    {"class": "Reliquarian", "skill": "Spark", "percentage": 2.1, "trend": 2},
                ],
            }
        ]
    }
    monkeypatch.setattr(ext, "_fetch_poe_ninja_build_index_state", lambda **_k: fake_state)

    res_zh = ext.query_meta(skill="正火", class_name="酋长")
    assert res_zh["ascendancies"][0]["class"] == "Chieftain"
    assert res_zh["topSkills"][0]["skill"] == "Righteous Fire"
    assert res_zh["filters"]["resolvedSkill"] == "Righteous Fire"
    assert res_zh["filters"]["resolvedClassName"] == "Chieftain"

    res_tw = ext.query_meta(class_name="聖宗")
    assert res_tw["ascendancies"][0]["class"] == "Hierophant"

    res_rel = ext.query_meta(class_name="守藏者")
    assert res_rel["ascendancies"][0]["class"] == "Reliquarian"

    # Partial EN substring still works unchanged
    res_en = ext.query_meta(class_name="Necro")
    assert res_en["ascendancies"][0]["class"] == "Necromancer"
    assert "resolvedClassName" not in res_en["filters"]

    # CLI Markdown shows resolved filter + bilingual display
    monkeypatch.setattr("pob_cli.locale_cfg.current_lang", lambda: "zh-CN")
    cli_out = CliRunner().invoke(cmd_meta, ["--skill", "正火", "--class", "酋长"])
    assert cli_out.exit_code == 0, cli_out.output
    assert "Chieftain (酋长)" in cli_out.output
    assert "Righteous Fire (正火)" in cli_out.output

    # Offline TW alias returning English via normalize_tw_query without PoEDB or local engine dict
    monkeypatch.setattr("pob_cli.game_data.zh_match.english_for_zh", lambda _q: None)
    monkeypatch.setattr("pob_cli.external_data.meta_wiki._lookup_poedb_local_en", lambda _cands: None)
    res_tw_skill = ext.query_meta(skill="喚醒幽魂")
    assert res_tw_skill["filters"]["resolvedSkill"] == "Raise Spectre"
    assert [r["skill"] for r in res_tw_skill["topSkills"]] == ["Raise Spectre"]

    # Negative case: unknown CJK skill safely falls back without crashing or fabricating a resolvedSkill
    res_unknown = ext.query_meta(skill="不存在的神秘技能")
    assert "resolvedSkill" not in res_unknown["filters"]
    assert res_unknown["topSkills"] == []
    assert res_unknown["ascendancies"] == []


def test_skill_md_includes_meta_and_new_within_line_budget() -> None:
    skill_md = (Path(__file__).resolve().parents[2] / "skills" / "pob" / "SKILL.md").read_text(encoding="utf-8")
    assert len(skill_md.splitlines()) <= 85
    assert "pob meta" in skill_md
    assert "pob new" in skill_md
    assert "| pob batch" not in skill_md
    assert "; pob batch" in skill_md
