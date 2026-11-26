"""Regression tests for v0.5 universal CLI generalization, anti-hallucination & long-context guards."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data

from pob_cli import analyzer, cli, comparator, formatters, game_data, guardrails

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_POB_DIR = find_engine_data()
HAS_REAL_POB = REAL_POB_DIR is not None


@pytest.fixture(autouse=True)
def _reset_guardrail_warnings() -> Any:
    guardrails.reset_cli_warnings()
    yield
    guardrails.reset_cli_warnings()


# ---------------------------------------------------------------------------
# 1. Recency State Stamp (`rev` + `(NOT PoE2)`) & 16KB JSON Bloat Guard
# ---------------------------------------------------------------------------


def test_recency_stamp_includes_not_poe2_and_rev() -> None:
    stamp = formatters.format_recency_stamp(
        {"env": {"patch": "3.30", "pobVersion": "2.68.1", "dataVersion": "3.30", "treeVersion": "3_30", "slot": "main", "rev": 4}}
    )
    assert "(NOT PoE2)" in stamp
    assert "Slot: main (rev=4)" in stamp
    assert "PoB 2.68.1" in stamp

    stamp_min = formatters.format_recency_stamp({"rev": 2, "buildInfo": {"treeVersion": "3_30"}}, slot="boss")
    assert stamp_min == "[Env: PoE1 3.30 (NOT PoE2) | Tree 3_30 | Slot: boss (rev=2)]"

    stamp_no_rev = formatters.format_recency_stamp({}, slot="main")
    assert stamp_no_rev == "[Env: PoE1 3.30 (NOT PoE2) | Slot: main]"


def test_slot_history_rev_persists_to_slot_state(tmp_path: Path) -> None:
    from pob_cli.bridge_client import BridgeWorker
    from pob_cli.snapshot import SlotHistory

    h = SlotHistory("rev_test", tmp_path)
    assert h.rev == 0
    h.load_build("dummy_code", "pasted")
    h.commit("dummy_code", "next_code")
    assert h.rev == 2

    h2 = SlotHistory.load("rev_test", tmp_path)
    assert h2.rev == 2
    assert (h2.committed, h2.undo, h2.source_label) == ("next_code", ["dummy_code"], "pasted")
    w2 = BridgeWorker(slot_name="rev_test", pob_dir=tmp_path, runtime_dir=tmp_path)
    h2.bind(w2)
    assert w2.last_good_code == "next_code"


def test_json_bloat_guard_auto_compacts_large_stdout_and_warns(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    big_payload = {"items": [{"idx": i, "text": "x" * 120} for i in range(200)]}

    # 1. Large stdout without --out: auto-compacts to single-line valid JSON + stderr hint
    emitted = formatters.emit_json(big_payload, compact=False, out=None)
    cap = capsys.readouterr()
    assert "\n  " not in emitted
    assert json.loads(cap.out) == big_payload
    assert "auto-compacted" in cap.err
    assert "--out" in cap.err

    # 2. Small stdout stays pretty-printed with zero stderr hint
    small_payload = {"ok": True, "val": 42}
    emitted_small = formatters.emit_json(small_payload, compact=False, out=None)
    cap_small = capsys.readouterr()
    assert "\n  " in emitted_small
    assert cap_small.err == ""

    # 3. Large payload written to --out stays pretty-printed with zero stderr hint
    out_file = tmp_path / "big.json"
    formatters.emit_json(big_payload, compact=False, out=out_file)
    cap_file = capsys.readouterr()
    assert cap_file.err == ""
    assert "\n  " in out_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. Universal Dead / Anti-Synergy Supports & Rank-Gems Caveats
# ---------------------------------------------------------------------------


def test_detect_anti_synergy_supports_and_zero_mana_inspiration() -> None:
    skills_res = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"index": 1, "name": "Ice Nova of Frostbolts", "support": False, "enabled": True, "level": 20},
                    {"index": 2, "name": "Elemental Focus Support", "support": True, "enabled": True, "level": 20},
                    {"index": 3, "name": "Hypothermia Support", "support": True, "enabled": True, "level": 20},
                    {"index": 4, "name": "Inspiration Support", "support": True, "enabled": True, "level": 20},
                    {"index": 5, "name": "Brutality Support", "support": True, "enabled": True, "level": 20},
                    {"index": 6, "name": "Added Fire Damage Support", "support": True, "enabled": True, "level": 20},
                ],
            }
        ],
    }
    gaps, _mech = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 0, "IgniteChance": 0, "Life": 4000, "LifeUnreserved": 4000, "LifeUnreservedPercent": 100},
        skills_res=skills_res,
        items_res={"slots": [], "items": []},
        cfg_res={"sections": []},
    )
    msgs = "\n".join(g["message"] for g in gaps)
    assert "Inspiration" in msgs and "0-mana" in msgs
    assert "Elemental Focus" in msgs and "Hypothermia" in msgs
    assert "Brutality" in msgs and "Added Fire Damage" in msgs
    assert len(gaps) >= 3


def test_engine_verified_dead_support_detector_and_exemptions() -> None:
    class _FakeDeadSupWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()
            self._disabled: set[int] = set()

        def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            return self.call_batch([(method, params or {})])[0]

        def call_batch(self, calls: list[tuple[str, dict[str, Any] | None]], **_: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for method, params in calls:
                p = params or {}
                if method == "set_gem":
                    idx = int(p["index"])
                    if p.get("enabled") is False:
                        self._disabled.add(idx)
                    else:
                        self._disabled.discard(idx)
                    out.append({"ok": True})
                elif method == "get_stats":
                    # Gem #2 (Hypothermia) has 0% effect; Gem #3 (Cold Pen) has huge effect;
                    # Gem #4 (Culling Strike) has 0% PoB hit DPS but is utility-exempt!
                    dps = 1_000_000.0 if 3 not in self._disabled else 700_000.0
                    out.append({"stats": {"CombinedDPS": dps, "FullDPS": 0.0, "TotalEHP": 50_000.0}})
                else:
                    out.append({})
            return out

    mg = {
        "index": 1,
        "gems": [
            {"index": 1, "name": "Ice Nova", "support": False, "enabled": True},
            {"index": 2, "name": "Hypothermia Support", "support": True, "enabled": True},
            {"index": 3, "name": "Cold Penetration Support", "support": True, "enabled": True},
            {"index": 4, "name": "Culling Strike Support", "support": True, "enabled": True},
        ],
    }
    gaps, dead = analyzer._detect_dead_main_supports(
        _FakeDeadSupWorker(),  # type: ignore[arg-type]
        mg,
        {"CombinedDPS": 1_000_000.0, "FullDPS": 0.0, "TotalEHP": 50_000.0, "ManaCost": 25},
    )
    dead_names = [d["name"] for d in dead]
    assert "Hypothermia Support" in dead_names
    assert "Cold Penetration Support" not in dead_names
    assert "Culling Strike Support" not in dead_names
    assert any("0.0% CombinedDPS" in g["message"] and "0.0% TotalEHP" in g["message"] for g in gaps)


def test_rank_gems_caveats_and_zero_mana_inspiration_filter() -> None:
    class _FakeRankWorker:
        def __init__(self) -> None:
            self.lock = threading.RLock()

        def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
            if method == "list_skills":
                return {
                    "mainSocketGroup": 1,
                    "groups": [
                        {
                            "index": 1,
                            "isMain": True,
                            "displayLabel": "Spark",
                            "gems": [{"index": 1, "name": "Spark", "enabled": True, "support": False}],
                        }
                    ],
                }
            if method == "get_stats":
                return {"stats": {"ManaCost": 0.0}}
            if method == "get_gem_options":
                return {"sortGemsByDPS": False, "sortGemsByDPSField": "CombinedDPS"}
            if method == "set_gem_options":
                return {"ok": True}
            if method == "gem_search":
                return {
                    "dpsField": "CombinedDPS",
                    "baseDps": 500_000,
                    "gems": [
                        {"name": "Inspiration", "nameZh": "启迪", "delta": 200_000, "pct": 40.0},
                        {"name": "Immolate", "nameZh": "献祭", "delta": 180_000, "pct": 36.0},
                        {"name": "Elemental Focus", "nameZh": "元素集中", "delta": 150_000, "pct": 30.0},
                        {"name": "Sacrifice", "nameZh": "献祭辅助", "delta": 140_000, "pct": 28.0},
                    ],
                }
            return {}

    res = comparator.rank_gems(
        _FakeRankWorker(),  # type: ignore[arg-type]
        group_selector="main",
        add_link=True,
        field="CombinedDPS",
        top=5,
        exclude_archetype_swaps=True,
    )
    by_name = {c["name"]: c for c in res["candidates"]}
    assert "Inspiration" not in by_name
    assert any("Inspiration" in s and "0-mana" in s for s in (res.get("filteredArchetypeSwaps") or []))
    assert "Burning" in (by_name["Immolate"].get("caveat") or "")
    assert "Ailments" in (by_name["Elemental Focus"].get("caveat") or "")
    assert "20% Current Life" in (by_name["Sacrifice"].get("caveat") or "")


# ---------------------------------------------------------------------------
# 3. Universal Triggers (`0.033s` CoC Overspeed, Awakened CoC CDR, CwC Non-CDR)
# ---------------------------------------------------------------------------


def test_detect_coc_overspeed_awakened_cdr_and_cwc_non_cdr() -> None:
    # A. CoC with 14% CDR (4t=132ms -> 7.58/s cap) and 8.50 APS -> TriggerOverspeed. Without the
    # engine's agentProbes cross-check it is a Python-table estimate: MEDIUM and labelled as such.
    skills_coc = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"index": 1, "name": "Cyclone", "support": False, "enabled": True, "level": 20},
                    {"index": 2, "name": "Cast On Critical Strike Support", "support": True, "enabled": True, "level": 20},
                    {"index": 3, "name": "Ice Nova of Frostbolts", "support": False, "enabled": True, "level": 20},
                ],
            }
        ],
    }
    boots_14cdr = [{"slotName": "Boots", "raw": "Rarity: RARE\nBoots\n14% increased Cooldown Recovery Rate\n"}]
    gaps_coc, mech_coc = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"Speed": 7.58, "CritChance": 99.0, "HitChance": 100.0},
        skills_res=skills_coc,
        items_res={"slots": [], "items": []},
        cfg_res={"sections": []},
        equipped_slots_raw=boots_14cdr,
        trigger_attack_aps=8.5,
    )
    overspeed = [g for g in gaps_coc if g["category"] == "TriggerOverspeed"]
    assert overspeed and all(g["severity"] == "MEDIUM" for g in overspeed)
    assert all("estimate, not engine-verified" in g["message"] for g in overspeed)
    trigs = mech_coc.get("triggers") or []
    assert trigs and trigs[0]["cooldown"]["ticks"] == 4 and trigs[0]["cooldown"]["overspeed"] is True

    # B. Awakened CoC Lv5 (+22% gem CDR) + 30% belt/boots CDR = 52% total -> 3t = 99ms (10.10/s cap), 9.8 APS is OK!
    skills_acoc = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"index": 1, "name": "Cyclone", "support": False, "enabled": True, "level": 20},
                    {"index": 2, "name": "Awakened Cast On Critical Strike Support", "support": True, "enabled": True, "level": 5},
                    {"index": 3, "name": "Ice Nova of Frostbolts", "support": False, "enabled": True, "level": 20},
                ],
            }
        ],
    }
    belt_30cdr = [{"slotName": "Belt", "raw": "Rarity: RARE\nBelt\n30% increased Cooldown Recovery Rate\n"}]
    gaps_acoc, mech_acoc = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"Speed": 9.8, "CritChance": 99.0, "HitChance": 100.0},
        skills_res=skills_acoc,
        items_res={"slots": [], "items": []},
        cfg_res={"sections": []},
        equipped_slots_raw=belt_30cdr,
        trigger_attack_aps=9.8,
    )
    assert not any(g["category"] == "TriggerOverspeed" for g in gaps_acoc)
    assert mech_acoc["triggers"][0]["gemCdrPct"] == 22
    assert mech_acoc["triggers"][0]["totalCdrPct"] == 52.0
    assert mech_acoc["triggers"][0]["cooldown"]["ticks"] == 3

    # C. Cast while Channelling -> fixed interval (320ms Lv20), NOT CDR!
    skills_cwc = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"index": 1, "name": "Cyclone", "support": False, "enabled": True, "level": 20},
                    {"index": 2, "name": "Cast while Channelling Support", "support": True, "enabled": True, "level": 20},
                    {"index": 3, "name": "Volatile Dead", "support": False, "enabled": True, "level": 20},
                ],
            }
        ],
    }
    _, mech_cwc = analyzer._detect_build_mechanics_and_pitfalls(
        stats={},
        skills_res=skills_cwc,
        items_res={"slots": [], "items": []},
        cfg_res={"sections": []},
    )
    assert any(t.get("kind") == "fixed_interval_channelling" and t.get("intervalMs") == 320 for t in mech_cwc["triggers"])


# ---------------------------------------------------------------------------
# 4. Universal Defensive / Sustain Bricks (Esh's Visage, Low-Life, EB+CI, Dissolution, Crit Acc)
# ---------------------------------------------------------------------------


def test_detect_defensive_bricks_esh_lowlife_eb_ci_dissolution_and_crit_acc() -> None:
    # A. Esh's Visage on Low-Life -> CRITICAL HIGH gap!
    gaps_esh, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"Life": 1400, "LifeUnreserved": 450, "LifeUnreservedPercent": 32, "EnergyShield": 6200, "Mana": 900, "ManaUnreserved": 300, "ManaUnreservedPercent": 33},
        skills_res={"groups": []},
        items_res={
            "slots": [{"name": "Weapon 2", "selItemId": 1}],
            "items": [{"id": 1, "name": "Esh's Visage", "title": "Esh's Visage"}],
        },
        cfg_res={"sections": []},
    )
    assert any(g["category"] == "DefensiveBrick" and "Esh's Visage" in g["message"] and g["severity"] == "HIGH" for g in gaps_esh)

    # B. EB + CI fatal brick!
    gaps_ebci, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={"Life": 1, "EnergyShield": 5000},
        skills_res={"groups": []},
        items_res={"slots": [], "items": []},
        cfg_res={"sections": []},
        tree_summary={"keystones": [{"name": "Chaos Inoculation"}, {"name": "Eldritch Battery"}]},
    )
    assert any(g["category"] == "DefensiveBrick" and "Eldritch Battery + Chaos Inoculation" in g["message"] for g in gaps_ebci)

    # C. Dissolution of the Flesh + Righteous Fire conflict + Crit Attack Accuracy Double-Roll
    gaps_dis, _ = analyzer._detect_build_mechanics_and_pitfalls(
        stats={
            "Life": 8000,
            "LifeUnreserved": 8000,
            "LifeUnreservedPercent": 100,
            "CritChance": 65.0,
            "HitChance": 86.0,
            "MainHandAccuracy": 2200,
        },
        skills_res={
            "mainSocketGroup": 1,
            "groups": [
                {
                    "index": 1,
                    "isMain": True,
                    "enabled": True,
                    "gems": [
                        {"index": 1, "name": "Lightning Strike", "support": False, "enabled": True},
                        {"index": 2, "name": "Righteous Fire", "support": False, "enabled": True},
                    ],
                }
            ],
        },
        items_res={
            "slots": [{"name": "Jewel 1", "selItemId": 10}],
            "items": [{"id": 10, "name": "Dissolution of the Flesh", "title": "Dissolution of the Flesh"}],
        },
        cfg_res={"sections": []},
    )
    assert any(g["category"] == "DefensiveBrick" and "Dissolution of the Flesh" in g["message"] for g in gaps_dis)
    assert any(g["category"] == "Offence" and "TWICE" in g["message"] for g in gaps_dis)


# ---------------------------------------------------------------------------
# 5. CLI `diagnose --compare --ask` + 0ms `data unique --mod` / `data tree` / `data tick`
# ---------------------------------------------------------------------------


def test_cli_diagnose_compare_and_ask_routing_and_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_daemon_call(action: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        captured["action"] = action
        captured["params"] = dict(params or {})
        return {
            "mode": "compare",
            "target": "main",
            "refTarget": "ref",
            "env": {"patch": "3.30", "pobVersion": "2.68.1", "dataVersion": "3.30", "treeVersion": "3_30", "slot": "main", "rev": 2},
            "comparison": {
                "mainBuildInfo": {"level": 95, "ascendClassName": "Hierophant"},
                "refBuildInfo": {"level": 94, "ascendClassName": "Assassin"},
                "baselines": {
                    "original": [{"label": "DPS", "mainStr": "10M", "refStr": "12M", "diffRefVsMainStr": "+20%"}],
                    "envAlignedToMain": {"rows": [{"label": "DPS", "mainStr": "10M", "refStr": "11M", "diffRefVsMainStr": "+10%"}]},
                    "pinnacleDewatered": {"rows": [{"label": "DPS", "mainStr": "6M", "refStr": "8M", "diffRefVsMainStr": "+33%"}]},
                },
                "treeDiff": {"keystones": {"onlyInMain": [], "onlyInRef": []}, "notables": {"sharedCount": 5, "onlyInMain": [], "onlyInRef": []}},
                "skillDiff": {"mainGroupMain": {"index": 1, "displayLabel": "Spark"}, "refGroupMain": {"index": 1, "displayLabel": "Ice Nova"}},
                "itemSlotDiff": [],
            },
            "buildA": {
                "env": {"patch": "3.30", "pobVersion": "2.68.1", "dataVersion": "3.30", "treeVersion": "3_30", "slot": "main", "rev": 2},
                "buildInfo": {"level": 95, "className": "Templar", "ascendClassName": "Hierophant", "points": {"used": 115, "usedMax": 123}},
                "mainGroup": {"index": 1, "displayLabel": "Spark", "gems": []},
                "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 10_000_000, "totalEhp": 80_000},
                "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 6_000_000, "totalEhp": 60_000},
                "healthGaps": [],
                "askGrounding": {"query": "CwC CDR?", "resolvedSlang": [], "addedEntities": ["Cast while Channelling Support"]},
                "advisories": [{"tag": "[POE1-PATCH-WARN]", "trigger": "cwc", "message": "CwC fixed interval"}],
            },
            "buildB": {
                "env": {"patch": "3.30", "pobVersion": "2.68.1", "dataVersion": "3.30", "treeVersion": "3_30", "slot": "ref", "rev": 1},
                "buildInfo": {"level": 94, "className": "Shadow", "ascendClassName": "Assassin", "points": {"used": 110, "usedMax": 123}},
                "mainGroup": {"index": 1, "displayLabel": "Ice Nova", "gems": []},
                "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 12_000_000, "totalEhp": 50_000},
                "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 8_000_000, "totalEhp": 40_000},
                "healthGaps": [],
            },
        }

    monkeypatch.setattr(cli, "_daemon_call", _fake_daemon_call)
    runner = CliRunner()
    res = runner.invoke(cli.main, ["diagnose", "https://pobb.in/aaa", "--compare", "https://pobb.in/bbb", "-q", "CwC CDR?"])
    assert res.exit_code == 0, res.output
    assert captured["action"] == "diagnose"
    assert captured["params"]["no_rank"] is True  # default True in --compare mode when --top omitted
    assert captured["params"]["ask"] == "CwC CDR?"
    assert captured["params"]["compare_source"] == "https://pobb.in/bbb"
    assert "(NOT PoE2)" in res.output
    assert "Slots: main (rev=2) vs ref (rev=1)" in res.output
    assert "## Dual-Build 3-Baseline Comparison" in res.output
    assert "## Build A Diagnosis" in res.output
    assert "## Build B Diagnosis" in res.output
    assert "[Question Grounding]" in res.output
    assert len(res.output.encode("utf-8")) < 8192


def test_cli_data_tick_and_presets() -> None:
    runner = CliRunner()
    res = runner.invoke(cli.main, ["data", "tick", "0.15", "--cdr", "14", "--aps", "8.5"])
    assert res.exit_code == 0, res.output
    assert "4` ticks" in res.output or "4 ticks" in res.output
    assert "132ms" in res.output
    assert "7.58/s" in res.output
    assert "OVERSPEED WARNING" in res.output
    assert "52%" in res.output
    assert "Cast while Channelling" in res.output

    res_preset = runner.invoke(cli.main, ["data", "tick", "coc", "--cdr", "52", "--json"])
    assert res_preset.exit_code == 0, res_preset.output
    payload = json.loads(res_preset.output)
    assert payload["baseCdSec"] == 0.15
    assert payload["current"]["ticks"] == 3
    assert payload["current"]["effectiveCdMs"] == 99


@pytest.mark.skipif(not HAS_REAL_POB, reason="Real PoE1 3.30 engine not present")
def test_real_0ms_data_unique_mod_and_tree_queries() -> None:
    # 1. `pob data unique --mod` reverse search finds Shavronne's Wrappings, Solaris Lorica, Coruscating Elixir, Esh's Visage
    u_res = game_data.query_local_data(
        "unique", "", mod_filter="Chaos Damage taken does not bypass", limit=15, pob_dir=REAL_POB_DIR
    )
    names = {r["name"] for r in u_res["results"]}
    assert "Shavronne's Wrappings" in names
    assert "Solaris Lorica" in names
    assert "Coruscating Elixir" in names
    assert "Esh's Visage" in names
    assert all(r.get("matched_mods") for r in u_res["results"])

    # 2. `pob data tree --asc Warden` returns Ranger's 3.30 Warden (NOT legacy 3.23 Warden of the Maji!)
    w_res = game_data.query_tree_data(asc="Warden", limit=15, pob_dir=REAL_POB_DIR)
    w_names = {r["name"] for r in w_res["results"]}
    assert "Oath of Summer" in w_names
    assert "Oath of Spring" in w_names
    assert "Oath of Winter" in w_names
    assert "Detect Evil" not in w_names

    # 3. `pob data tree "聚魂"` resolves slang -> Polymath with 3.30 live 2% stat
    p_res = game_data.query_tree_data("聚魂", limit=5, pob_dir=REAL_POB_DIR)
    assert p_res["results"] and p_res["results"][0]["name"] == "Polymath"
    assert any("2% more Damage" in s for s in p_res["results"][0]["stats"])

    # 4. `pob data tree --type Keystone "Eldritch Battery"` has complete multiline 3.30 stats
    eb_res = game_data.query_tree_data("Eldritch Battery", node_type="Keystone", limit=3, pob_dir=REAL_POB_DIR)
    assert eb_res["results"] and eb_res["results"][0]["name"] == "Eldritch Battery"
    assert any("protects Mana instead of Life" in s for s in eb_res["results"][0]["stats"])

    # 5. Guardrails trap rules for CwC and Esh's Visage
    g_res = game_data.ground_text_entities("吟唱时施放堆CDR能加快吗？低血带艾许之面安全吗？", pob_dir=REAL_POB_DIR)
    rule_ids = {w.get("ruleId") for w in g_res.get("warnings") or []}
    assert "PATCH_330_CWC_NOT_COOLDOWN" in rule_ids
    assert "PATCH_330_LOW_LIFE_ESH_VISAGE" in rule_ids

    # 6. Dual-part support gem (`secondaryGrantedEffectId`) merges Awakened CoC Lv5 +22% spell CDR
    acoc_res = game_data.query_local_data("gem", "Awakened Cast On Critical Strike", limit=3, pob_dir=REAL_POB_DIR)
    assert acoc_res["results"]
    acoc = acoc_res["results"][0]
    lv5_stats = (acoc.get("levels") or {}).get("5", {}).get("stats") or {}
    assert lv5_stats.get("base_spell_cooldown_speed_+%") == 22

    # 7. Timeless Jewel Keystones (`LegionPassives.lua`) indexed in `pob data tree`
    tbw_res = game_data.query_tree_data("Tempered by War", node_type="Keystone", limit=3, pob_dir=REAL_POB_DIR)
    assert tbw_res["results"] and tbw_res["results"][0]["name"] == "Tempered by War"
    assert any("Cold and Lightning Damage taken as Fire" in s for s in tbw_res["results"][0]["stats"])


def test_universal_skill_and_triggers_reference_contracts() -> None:
    skill_md = REPO_ROOT / "skills" / "pob" / "SKILL.md"
    trig_ref = REPO_ROOT / "skills" / "pob" / "references" / "triggers-and-mechanics.md"
    skill_text = skill_md.read_text(encoding="utf-8")
    skill_lines = skill_text.splitlines()
    assert len(skill_lines) <= 85, f"SKILL.md has {len(skill_lines)} lines (must be <= 85)"
    assert "diagnose" in skill_text and "--compare" in skill_text and "-q" in skill_text
    assert "Every Number Comes From `pob` Output" in skill_text and "[待验证]" in skill_text
    assert "triggers-and-mechanics.md" in skill_text
    assert "data tick" in skill_text and "data tree" in skill_text

    assert trig_ref.is_file()
    trig_lines = trig_ref.read_text(encoding="utf-8").splitlines()
    assert len(trig_lines) <= 45, f"triggers-and-mechanics.md has {len(trig_lines)} lines (must be <= 45)"
    trig_text = "\n".join(trig_lines)
    assert "0.033" in trig_text and "Cast while Channelling" in trig_text and "Esh's Visage" in trig_text
