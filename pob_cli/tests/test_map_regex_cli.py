"""Offline CLI tests and real-engine integration tests for `pob map-regex`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

from pob_cli.bridge_client import BridgeWorker
from pob_cli.cli import main
from pob_cli.daemon.read_cache import READ_CACHE_ACTIONS, SlotReadCache
from pob_cli.daemon.slots import DaemonManager
from pob_cli.formatters import format_diagnose_markdown
from pob_cli.importers import xml_to_share_code

_FIREBALL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="Main">
        <Gem nameSpec="Fireball" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Spell Echo Support" level="20" quality="20" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1"><ItemSet id="1"/></Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>
"""

_LIGHTNING_ARROW_ELE_BOW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="95" targetVersion="3_0" className="Ranger" ascendClassName="Deadeye" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="2" ascendClassId="2" nodes="50459"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="Main" slot="Weapon 1">
        <Gem nameSpec="Lightning Arrow" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Trinity Support" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Elemental Damage with Attacks Support" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Inspiration Support" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Mirage Archer Support" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Returning Projectiles Support" level="20" quality="20" enabled="true"/>
      </Skill>
      <Skill mainActiveSkill="1" enabled="true" label="Move">
        <Gem nameSpec="Blink Arrow" level="20" quality="20" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">Rarity: UNIQUE
The Tempest
Long Bow
Quality: 20
Sockets: G-G-G-G-G-G
No Physical Damage
Adds 1 to 85 Lightning Damage
100% increased Lightning Damage
15% increased Attack Speed
</Item>
    <ItemSet id="1">
      <Slot name="Weapon 1" itemId="1"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>
"""

_CRIT_100_BOW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="95" targetVersion="3_0" className="Ranger" ascendClassName="Deadeye" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="2" ascendClassId="2" nodes="50459"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="Main" slot="Weapon 1">
        <Gem nameSpec="Lightning Arrow" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Increased Critical Strikes Support" level="20" quality="20" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">Rarity: RARE
Test Ele Bow
Spine Bow
Quality: 20
Adds 200 to 400 Fire Damage
Adds 200 to 400 Cold Damage
Adds 100 to 600 Lightning Damage
Hits can't be Evaded
+100% to Critical Strike Chance
Non-Critical Strikes deal no Damage
</Item>
    <ItemSet id="1">
      <Slot name="Weapon 1" itemId="1"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>
"""


def test_cli_preset_only_offline_json_and_markdown() -> None:
    runner = CliRunner()
    res = runner.invoke(
        main,
        [
            "map-regex",
            "--preset-only",
            "--avoid",
            "元反,无回复,drowning",
            "--min-quant",
            "80",
            "--client-lang",
            "en",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["falsePositiveCollisions"] == 0
    assert data["length"] <= 250
    assert data["clientLang"] == "en"
    assert data["corpusVersion"] == "3.30"
    assert data["langOverlay"] == "none"
    assert data["presetOnly"] is True
    assert [m["id"] for m in data["excludedMods"]] == ["drowning_orbs", "no_regen", "reflect_ele"]
    assert "availablePresets" in data and len(data["availablePresets"]) >= 40

    quant_only = runner.invoke(main, ["map-regex", "--preset-only", "--min-quant", "80", "--client-lang", "en"])
    assert quant_only.exit_code == 0, quant_only.output
    assert "no modifiers excluded" in quant_only.output
    assert "build can run all evaluated" not in quant_only.output
    assert "`corpus=3.30`, `overlay=none`" in quant_only.output

    md_res = runner.invoke(
        main,
        [
            "map-regex",
            "--preset-only",
            "--avoid",
            "reflect_ele,no_regen,no_leech,less_cdr,max_res,less_recovery,drowning_orbs,volatile_cores,sawblades",
            "--max-len",
            "24",
            "--client-lang",
            "zh-TW",
        ],
    )
    assert md_res.exit_code == 0, md_res.output
    assert "0 false-positive collisions verified" in md_res.output
    assert "Overflow Regex" in md_res.output


def test_cli_unknown_preset_exits_1_with_available_list() -> None:
    runner = CliRunner()
    res = runner.invoke(main, ["map-regex", "--preset-only", "--avoid", "not_a_real_mod"])
    assert res.exit_code == 1
    assert "Available presets:" in res.output


def test_cli_preset_only_requires_filter() -> None:
    runner = CliRunner()
    res = runner.invoke(main, ["map-regex", "--preset-only"])
    assert res.exit_code == 1
    assert "requires --avoid" in res.output


def test_cli_daemon_dispatch_and_markdown_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_invoke(action: str, params: dict[str, Any], timeout: float = 180.0) -> dict[str, Any]:
        captured["action"] = action
        captured["params"] = params
        return {
            "regex": '"!tal d|a or"',
            "length": 13,
            "maxLength": 250,
            "overflowRegex": [],
            "clientLang": "en",
            "tier": "both",
            "mode": "safe",
            "falsePositiveCollisions": 0,
            "excludedMods": [
                {"id": "reflect_ele", "severity": "brick", "token": "tal d", "reason": "Fireball hit avg 5,000"},
                {"id": "no_regen", "severity": "brick", "token": "a or", "reason": "Spends mana without leech"},
            ],
            "exemptedMods": [
                {"id": "action_speed", "severity": "safe", "reason": "SAFE: Trickster One Step Ahead"},
            ],
            "availablePresets": ["reflect_ele", "no_regen"],
        }

    monkeypatch.setattr("pob_cli.cli_map_regex.invoke_daemon", _fake_invoke)
    runner = CliRunner()
    res = runner.invoke(main, ["map-regex", "--tier", "t16", "--mode", "brick", "--client-lang", "en"])
    assert res.exit_code == 0, res.output
    assert captured["action"] == "map_regex"
    assert captured["params"]["tier"] == "t16"
    assert captured["params"]["mode"] == "brick"
    assert '"!tal d|a or"' in res.output
    assert "[BRICK]" in res.output
    assert "Auto-Exempted (`SAFE`)" in res.output


def test_diagnose_markdown_includes_map_regex_tip() -> None:
    md_main = format_diagnose_markdown({"buildInfo": {"level": 90, "className": "Witch"}})
    assert "`pob map-regex`" in md_main
    md_alt = format_diagnose_markdown(
        {"buildInfo": {"level": 90, "className": "Witch"}, "targetSlot": "alternate"}
    )
    assert "`pob map-regex --target alternate`" in md_alt


def test_read_cache_supports_map_regex_action() -> None:
    assert "map_regex" in READ_CACHE_ACTIONS
    cache = SlotReadCache()
    payload = {"regex": '"!tal d"', "length": 8, "falsePositiveCollisions": 0}
    cache.store("main", 1, "en", "map_regex", {"tier": "both", "mode": "safe"}, payload)
    hit = cache.lookup("main", 1, "en", "map_regex", {"tier": "both", "mode": "safe"})
    assert hit is not None
    assert hit["cached"] is True
    assert hit["regex"] == '"!tal d"'


def test_real_engine_map_regex_probe_and_daemon_cache(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("No runnable PoB engine available")

    mgr = DaemonManager(pob_dir=engine, runtime_dir=tmp_path, offline=True, skip_update_check=True)
    try:
        share_code = xml_to_share_code(_FIREBALL_XML)
        res = mgr.dispatch(
            "map_regex",
            {
                "target": "main",
                "xml_code": share_code,
                "tier": "both",
                "mode": "safe",
                "client_lang": "en",
            },
        )
        assert res["falsePositiveCollisions"] == 0
        assert 0 < res["length"] <= 250
        excl_ids = {m["id"]: m["severity"] for m in res["excludedMods"]}
        assert excl_ids.get("reflect_ele") == "brick"
        assert "reflect_phys" not in excl_ids

        # Second identical call hits SlotReadCache
        res2 = mgr.dispatch(
            "map_regex",
            {
                "target": "main",
                "xml_code": share_code,
                "tier": "both",
                "mode": "safe",
                "client_lang": "en",
            },
        )
        assert res2.get("cached") is True
        assert res2["regex"] == res["regex"]

        # Verify zh-CN live overlay compiles with 0 FP as well
        res_cn = mgr.dispatch(
            "map_regex",
            {
                "target": "main",
                "tier": "both",
                "mode": "safe",
                "avoid": ["drowning_orbs"],
                "client_lang": "zh-CN",
            },
        )
        assert res_cn["falsePositiveCollisions"] == 0
        assert res_cn["length"] <= 250
        assert res_cn["clientLang"] == "zh-CN"
    finally:
        mgr.shutdown_all()


def test_real_engine_map_probe_capability() -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("No runnable PoB engine available")
    worker = BridgeWorker(pob_dir=engine, slot_name="map_probe_cap")
    try:
        ver = worker.call("version", {})
        assert (ver.get("caps") or {}).get("mapRegexProbe") is True
    finally:
        worker.stop()


def test_real_engine_bow_attack_mainhand_hit_average_bricks_reflect_ele(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("No runnable PoB engine available")

    mgr = DaemonManager(pob_dir=engine, runtime_dir=tmp_path, offline=True, skip_update_check=True)
    try:
        share_code = xml_to_share_code(_LIGHTNING_ARROW_ELE_BOW_XML)
        mgr.dispatch("load", {"target": "main", "xml_code": share_code})
        before_sum = mgr.dispatch("summary", {"target": "main"})
        before_status = mgr.dispatch("status", {})
        before_snap = (
            before_sum.get("rev"),
            (before_sum.get("offence") or {}).get("CombinedDPS"),
            (before_sum.get("offence") or {}).get("FullDPS"),
            (before_sum.get("defence") or {}).get("TotalEHP"),
            (before_sum.get("defence") or {}).get("Life"),
            (before_sum.get("defence") or {}).get("EnergyShield"),
            (before_status.get("slots") or {}).get("main", {}).get("rev"),
        )

        res = mgr.dispatch(
            "map_regex",
            {"target": "main", "tier": "both", "mode": "safe", "client_lang": "en"},
        )
        assert res["falsePositiveCollisions"] == 0
        assert res["corpusVersion"] == "3.30"
        assert res["langOverlay"] == "none"
        excl = {m["id"]: m["severity"] for m in res["excludedMods"]}
        assert excl.get("reflect_ele") == "brick", f"Expected reflect_ele=brick, got {excl}"
        assert "reflect_phys" not in excl, f"Pure ele bow should not exclude reflect_phys: {excl}"

        # Direct probe check: MainHand weapon attack elemental hit avg > 0, phys == 0, movement skill probed
        worker = mgr._workers["main"]
        probe = worker.call("map_regex_probe", {})
        ms = probe["mainSkill"]
        assert ms["name"] == "Lightning Arrow"
        assert (ms["avgFire"] + ms["avgCold"] + ms["avgLight"]) > 0
        assert ms["avgPhys"] == 0
        mv = probe.get("movementSkills") or []
        assert any(m["name"] == "Blink Arrow" and (m["avgFire"] + m["avgCold"] + m["avgLight"]) > 0 for m in mv)

        # Zero-residue proof: clear read cache so `summary` hits the live Lua engine, proving no state drift
        mgr._read_cache.clear_all()
        after_sum = mgr.dispatch("summary", {"target": "main"})
        assert not after_sum.get("cached")
        after_status = mgr.dispatch("status", {})
        after_snap = (
            after_sum.get("rev"),
            (after_sum.get("offence") or {}).get("CombinedDPS"),
            (after_sum.get("offence") or {}).get("FullDPS"),
            (after_sum.get("defence") or {}).get("TotalEHP"),
            (after_sum.get("defence") or {}).get("Life"),
            (after_sum.get("defence") or {}).get("EnergyShield"),
            (after_status.get("slots") or {}).get("main", {}).get("rev"),
        )
        assert after_snap == before_snap, f"map_regex_probe mutated build state: {before_snap} -> {after_snap}"
    finally:
        mgr.shutdown_all()


def test_real_engine_100pct_crit_attack_uses_crit_average_not_zero(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("No runnable PoB engine available")

    mgr = DaemonManager(pob_dir=engine, runtime_dir=tmp_path, offline=True, skip_update_check=True)
    try:
        share_code = xml_to_share_code(_CRIT_100_BOW_XML)
        res = mgr.dispatch(
            "map_regex",
            {"target": "main", "xml_code": share_code, "tier": "both", "mode": "brick", "client_lang": "en"},
        )
        worker = mgr._workers["main"]
        probe = worker.call("map_regex_probe", {})
        ms = probe["mainSkill"]
        assert ms["critChance"] >= 99.9
        assert (ms["avgFire"] + ms["avgCold"] + ms["avgLight"]) > 0
        excl = {m["id"]: m["severity"] for m in res["excludedMods"]}
        assert excl.get("reflect_ele") == "brick"
    finally:
        mgr.shutdown_all()
