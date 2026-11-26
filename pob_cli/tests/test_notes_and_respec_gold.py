"""Unit and real-engine tests for B1a Author Notes passthrough and B1b Gold Respec Cost."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_engine_data, find_runnable_engine

from pob_cli import cli as cli_mod
from pob_cli.analyzer.diagnose import diagnose_build
from pob_cli.analyzer.notes import (
    build_notes_payload,
    clean_notes_text,
    fetch_build_notes,
    strip_pob_note_escapes,
)
from pob_cli.analyzer.tree_diff import _diff_snapshots, diff_tree_specs
from pob_cli.bridge_client import BridgeWorker
from pob_cli.formatters.diagnose_md import format_diagnose_markdown
from pob_cli.formatters.notes_md import format_notes_markdown
from pob_cli.formatters.tree_diff_md import format_tree_diff_markdown
from pob_cli.game_data.respec_gold import (
    ASCENDANCY_RESPEC_MULTIPLIER,
    GOLD_RESPEC_PRICES,
    estimate_respec_gold,
    gold_respec_per_point,
)
from pob_cli.importers import xml_to_share_code
from pob_cli.snapshot import load_build


class _MinimalNotesWorker:
    """Minimal worker stub for testing notes passthrough and FakeWorker fallback."""

    def __init__(self, *, notes_text: str | None = None, fail_get_notes: bool = False) -> None:
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = {}
        self.last_good_code: str | None = None
        self._notes_text = notes_text
        self._fail_get_notes = fail_get_notes

    def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        if method == "get_notes":
            if self._fail_get_notes:
                raise RuntimeError("unknown rpc: get_notes")
            return {"text": self._notes_text or "", "unsaved": False, "rev": 1}
        if method == "import_code":
            self.last_good_code = (params or {}).get("code")
            return {"ok": True}
        if method == "export_code":
            return {"code": self.last_good_code or "FAKE_CODE"}
        if method == "get_build_info":
            return {
                "level": 92,
                "className": "Witch",
                "ascendClassName": "Elementalist",
                "treeVersion": "3_30",
                "points": {"used": 100, "usedMax": 123, "ascUsed": 8, "ascMax": 8},
            }
        if method == "get_build_header":
            return {"mainSocketGroup": {"index": 1, "list": ["Fireball"]}, "mainSkill": {"index": 1, "list": ["Fireball"]}}
        if method == "get_stats":
            return {"stats": {"CombinedDPS": 100000, "TotalEHP": 50000, "Life": 4000}}
        if method == "get_sidebar":
            return {"warnings": []}
        if method == "list_items":
            return {"items": [], "slots": [], "activeItemSetId": 1, "itemSets": []}
        if method == "list_skills":
            return {"groups": [], "mainSocketGroup": 1}
        if method == "list_config":
            return {"sections": [], "customMods": [], "activeConfigSetId": 1, "configSets": []}
        if method == "get_tree_state":
            return {"allocatedNodes": [], "treeVersion": "3_30", "classId": 3, "ascendClassId": 2}
        if method == "tree_data":
            return {"nodes": {}}
        return {}

    def get_cached_tree_data(self, _tv: str | None = None) -> dict[str, Any]:
        return {"nodes": {}}


# -- B1a: StripEscapes parity & Author Notes tests ----------------------------


def test_strip_pob_note_escapes_matches_lua_strip_escapes() -> None:
    assert strip_pob_note_escapes("^xFFAA00Act 1^7 -> ^x00ff88Maps^0") == "Act 1 -> Maps"
    # Matches PoB Lua `text:gsub("%^%d",""):gsub("%^x%x%x%x%x%x%x","")` exactly:
    assert strip_pob_note_escapes("2^3") == "2"
    # Non-escape carets (variables, spaced operator, short hex) remain intact:
    assert strip_pob_note_escapes("x^n + a ^ b + ^x123 + ^") == "x^n + a ^ b + ^x123 + ^"
    assert strip_pob_note_escapes("") == ""
    assert strip_pob_note_escapes(None) == ""


def test_clean_notes_preserves_paragraphs_urls_and_bullets() -> None:
    raw = (
        "\r\n\r\n"
        "^xFFAA00=== Leveling Guide ===^7   \r\n"
        "- Act 1-3: Rolling Magma   \r\n"
        "- Maps: https://example.com/guide?stage=maps#end   \r\n"
        "\r\n"
        "  ^2Pantheon:^7 Brine King + Abberath\r\n\r\n"
    )
    cleaned = clean_notes_text(raw)
    assert cleaned == (
        "=== Leveling Guide ===\n"
        "- Act 1-3: Rolling Magma\n"
        "- Maps: https://example.com/guide?stage=maps#end\n"
        "\n"
        "  Pantheon: Brine King + Abberath"
    )
    payload = build_notes_payload(raw)
    assert payload["notes"] == cleaned
    assert payload["notesMeta"] == {"hasNotes": True, "lineCount": 5, "charCount": len(cleaned)}


def test_empty_notes_and_fake_worker_fallback() -> None:
    empty_payload = build_notes_payload("   \r\n^7^xFFFFFF  ")
    assert empty_payload == {"notes": "", "notesMeta": {"hasNotes": False, "lineCount": 0, "charCount": 0}}
    assert format_notes_markdown(empty_payload) == ""

    legacy_worker = _MinimalNotesWorker(fail_get_notes=True)
    fb = fetch_build_notes(legacy_worker)
    assert fb == {"notes": "", "notesMeta": {"hasNotes": False, "lineCount": 0, "charCount": 0}}


def test_clean_notes_strips_ansi_and_c0_controls_preserving_tab_and_newline() -> None:
    dirty = "\x1b[31mRed Alert\x1b[0m\x07\x00\n\tIndented line\x1b[1;32m OK\x1b[0m\x1f"
    assert clean_notes_text(dirty) == "Red Alert\n\tIndented line OK"


def test_fetch_build_notes_propagates_worker_crash_errors() -> None:
    class _BrokenWorker:
        def call(self, _method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
            raise BrokenPipeError("engine pipe closed")

    with pytest.raises(BrokenPipeError):
        fetch_build_notes(_BrokenWorker())


def test_format_notes_markdown_truncates_only_long_md_not_json() -> None:
    short_lines = [f"Line {i}" for i in range(1, 26)]
    short_payload = build_notes_payload("\n".join(short_lines))
    short_md = format_notes_markdown(short_payload)
    assert "Author Notes" in short_md
    assert "Line 25" in short_md
    assert "--json" not in short_md

    long_lines = [f"Stage {i}" for i in range(1, 36)]
    long_payload = build_notes_payload("\n".join(long_lines))
    assert long_payload["notesMeta"] == {"hasNotes": True, "lineCount": 35, "charCount": len(long_payload["notes"])}
    assert "Stage 35" in long_payload["notes"]  # JSON payload is 100% untruncated

    long_md = format_notes_markdown(long_payload)
    assert "Stage 25" in long_md
    assert "Stage 26" not in long_md
    assert "35 total lines" in long_md
    assert "--json" in long_md


def test_load_and_diagnose_attach_notes_json_and_md(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    xml = (
        '<PathOfBuilding><Build level="92" className="Witch" ascendClassName="Elementalist"/>'
        "<Tree activeSpec=\"1\"><Spec treeVersion=\"3_30\" nodes=\"\"/></Tree>"
        "<Notes>^xFFD700League Starter^7\n- Cap res before Kitava</Notes></PathOfBuilding>"
    )
    w = _MinimalNotesWorker(notes_text="^xFFD700League Starter^7\n- Cap res before Kitava")
    load_res = load_build(w, xml_code=xml)
    assert load_res["notes"] == "League Starter\n- Cap res before Kitava"
    assert load_res["notesMeta"] == {"hasNotes": True, "lineCount": 2, "charCount": len(load_res["notes"])}

    # CLI `pob load` Markdown renders Author Notes when present, omits when absent.
    monkeypatch.setattr("pob_cli.cli_build_io.invoke_daemon", lambda _act, _p, **_kw: load_res)
    runner = CliRunner()
    res_md = runner.invoke(cli_mod.main, ["load", "dummy_code"])
    assert res_md.exit_code == 0, res_md.output
    assert "Author Notes" in res_md.output
    assert "League Starter" in res_md.output

    res_json = runner.invoke(cli_mod.main, ["load", "dummy_code", "--json"])
    assert res_json.exit_code == 0, res_json.output
    parsed = json.loads(res_json.output)
    assert parsed["notes"] == "League Starter\n- Cap res before Kitava"
    assert parsed["notesMeta"]["hasNotes"] is True

    empty_load = {**load_res, "notes": "", "notesMeta": {"hasNotes": False, "lineCount": 0, "charCount": 0}}
    monkeypatch.setattr("pob_cli.cli_build_io.invoke_daemon", lambda _act, _p, **_kw: empty_load)
    res_empty = runner.invoke(cli_mod.main, ["load", "dummy_code"])
    assert res_empty.exit_code == 0
    assert "Author Notes" not in res_empty.output

    # `format_diagnose_markdown` includes Author Notes when present and emits 0 extra lines when absent.
    diag_stub = {
        "env": {"slot": "main", "patch": "3.30"},
        "buildInfo": {"level": 92, "className": "Witch", "ascendClassName": "Elementalist", "points": {}},
        "mainGroup": {"index": 1, "displayLabel": "Fireball", "gems": []},
        "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 100000, "totalEhp": 50000},
        "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 90000, "totalEhp": 45000},
        "notes": "League Starter\n- Cap res before Kitava",
        "notesMeta": {"hasNotes": True, "lineCount": 2, "charCount": 38},
    }
    diag_md = format_diagnose_markdown(diag_stub)
    assert "Author Notes" in diag_md
    assert "Cap res before Kitava" in diag_md

    diag_stub_empty = {**diag_stub, "notes": "", "notesMeta": {"hasNotes": False, "lineCount": 0, "charCount": 0}}
    assert "Author Notes" not in format_diagnose_markdown(diag_stub_empty)


def test_diagnose_section_summary_preserves_inner_h4_headings_in_notes() -> None:
    from pob_cli.cli_inspect_parts import _slice_diagnose_markdown

    notes_with_headings = (
        "Intro overview\n"
        "#### Act 1 Leveling\n"
        "- Pick Rolling Magma\n"
        "#### 2. Endgame Maps\n"
        "- Switch to Fireball at Lv.90"
    )
    diag_stub = {
        "env": {"slot": "main", "patch": "3.30"},
        "buildInfo": {"level": 90, "className": "Witch", "ascendClassName": "Elementalist", "points": {}},
        "mainGroup": {"index": 1, "displayLabel": "Fireball", "gems": []},
        "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 100000, "totalEhp": 50000},
        "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 90000, "totalEhp": 45000},
        "notes": notes_with_headings,
        "notesMeta": {"hasNotes": True, "lineCount": 5, "charCount": len(notes_with_headings)},
    }
    full_md = format_diagnose_markdown(diag_stub)
    summary_md = _slice_diagnose_markdown(full_md, "summary")
    assert "#### Author Notes (作者备注)" in summary_md
    assert "#### Act 1 Leveling" in summary_md
    assert "- Pick Rolling Magma" in summary_md
    assert "#### 2. Endgame Maps" in summary_md
    assert "- Switch to Fireball at Lv.90" in summary_md

    # Inner `#### 2. Endgame Maps` inside notes must not leak into `--section gaps`
    gaps_md = _slice_diagnose_markdown(full_md, "gaps")
    assert "#### 2. Endgame Maps" not in gaps_md
    assert "Act 1 Leveling" not in gaps_md


# -- B1b: Gold Respec Cost tests ----------------------------------------------


def test_gold_respec_prices_formula_and_edge_cases() -> None:
    assert len(GOLD_RESPEC_PRICES) == 100
    assert ASCENDANCY_RESPEC_MULTIPLIER == 5
    assert gold_respec_per_point(1) == 4
    assert gold_respec_per_point(90) == 8450
    assert gold_respec_per_point(100) == 29119
    assert gold_respec_per_point(None) is None
    assert gold_respec_per_point(0) is None
    assert gold_respec_per_point(101) is None

    est = estimate_respec_gold(90, regular_nodes=10, ascendancy_nodes=2)
    assert est == {
        "characterLevel": 90,
        "goldPerPoint": 8450,
        "ascendancyMultiplier": 5,
        "deallocatedRegularCount": 10,
        "deallocatedAscendancyCount": 2,
        "estimatedGoldRespecTotal": 10 * 8450 + 2 * 8450 * 5,
    }

    est_unknown = estimate_respec_gold(None, regular_nodes=5, ascendancy_nodes=1)
    assert est_unknown["goldPerPoint"] is None
    assert est_unknown["estimatedGoldRespecTotal"] is None


def test_tree_diff_respec_gold_excludes_cluster_dynamic_nodes_and_renders_md() -> None:
    nodes_db = {
        "1": {"id": 1, "name": "Witch", "type": "ClassStart", "out": [10]},
        "10": {"id": 10, "name": "Node A", "type": "Normal", "out": [11, 12]},
        "11": {"id": 11, "name": "Node B", "type": "Notable", "out": [10]},
        "12": {"id": 12, "name": "Node C", "type": "Normal", "out": [10]},
        "20": {"id": 20, "name": "AscStart", "type": "AscendClassStart", "asc": "Elementalist", "out": [21]},
        "21": {"id": 21, "name": "Shaper of Flames", "type": "Notable", "asc": "Elementalist", "out": [20]},
    }
    snap_a = {"label": "Early", "characterLevel": 90, "allocatedNodes": [1, 10, 11, 20, 21, 65536, 65537]}
    snap_b = {"label": "Late", "characterLevel": 90, "allocatedNodes": [1, 10, 12, 20]}
    diff = _diff_snapshots(snap_a, snap_b, nodes_db)
    rg = diff["respecGold"]
    # Node 11 is 1 regular node refunded; nodes 65536/65537 are dynamic cluster passives (>=65536) and excluded.
    # Node 21 is 1 ascendancy node refunded (5x multiplier).
    assert rg["characterLevel"] == 90
    assert rg["goldPerPoint"] == 8450
    assert rg["deallocatedRegularCount"] == 1
    assert rg["deallocatedAscendancyCount"] == 1
    assert rg["estimatedGoldRespecTotal"] == 8450 + 8450 * 5

    md = format_tree_diff_markdown(diff)
    assert "Estimated Respec Cost" in md
    assert "50,700 Gold" in md
    assert "1 regular x 8,450" in md
    assert "1 ascendancy x 42,250" in md

    # Cross-slot diff (`include_respec_gold=False`), cross-class diff, and unknown level all yield `respecGold=None`.
    slot_diff = _diff_snapshots(snap_a, snap_b, nodes_db, include_respec_gold=False)
    assert slot_diff["respecGold"] is None
    assert "Estimated Respec Cost" not in format_tree_diff_markdown(slot_diff)

    cross_class = _diff_snapshots({**snap_a, "classId": 3}, {**snap_b, "classId": 5}, nodes_db)
    assert cross_class["respecGold"] is None
    assert "Estimated Respec Cost" not in format_tree_diff_markdown(cross_class)

    unknown_lvl = _diff_snapshots({**snap_a, "characterLevel": None}, snap_b, nodes_db)
    assert unknown_lvl["respecGold"] is None
    assert "Estimated Respec Cost" not in format_tree_diff_markdown(unknown_lvl)


def test_gold_respec_table_matches_real_pob_misc_lua() -> None:
    engine = find_engine_data()
    if engine is None:
        pytest.skip("Real PoB engine data not available")
    misc_lua = (engine / "PoE1" / "Data" / "Misc.lua").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"data\.goldRespecPrices\s*=\s*\{([^}]+)\}", misc_lua)
    assert m is not None, "data.goldRespecPrices not found in PoE1/Data/Misc.lua"
    lua_prices = tuple(int(tok.strip()) for tok in m.group(1).split(",") if tok.strip())
    assert GOLD_RESPEC_PRICES == lua_prices


def test_real_engine_notes_and_tree_diff_respec_gold(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("Real PoB engine not available")

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1">
    <Spec title="Stage 1" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447,57226,57264,25178"/>
    <Spec title="Stage 2" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447,57226"/>
  </Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="Main">
        <Gem nameSpec="Fireball" level="20" quality="20" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1"><ItemSet id="1"/></Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
  <Notes>^xFFAA00Wave 0 Author Guide^7
- Level with Rolling Magma
- Transition at Lv.90: https://example.com/pob</Notes>
</PathOfBuilding>
"""
    worker = BridgeWorker(pob_dir=engine, runtime_dir=tmp_path / "rt", slot_name="test_notes")
    try:
        worker.start()
        load_res = load_build(worker, xml_code=xml_to_share_code(xml))
        assert load_res["notes"] == "Wave 0 Author Guide\n- Level with Rolling Magma\n- Transition at Lv.90: https://example.com/pob"
        assert load_res["notesMeta"] == {"hasNotes": True, "lineCount": 3, "charCount": len(load_res["notes"])}

        diag_res = diagnose_build(worker, no_rank=True, pob_dir=engine, runtime_dir=tmp_path / "rt")
        assert diag_res["notes"] == load_res["notes"]
        assert diag_res["notesMeta"] == load_res["notesMeta"]

        diff_res = diff_tree_specs(worker, spec_a="Stage 1", spec_b="Stage 2")
        rg = diff_res["respecGold"]
        assert rg["characterLevel"] == 90
        assert rg["goldPerPoint"] == 8450
        assert rg["deallocatedRegularCount"] + rg["deallocatedAscendancyCount"] >= 1
        assert rg["estimatedGoldRespecTotal"] == (
            rg["deallocatedRegularCount"] * 8450 + rg["deallocatedAscendancyCount"] * 8450 * 5
        )
    finally:
        worker.stop()
