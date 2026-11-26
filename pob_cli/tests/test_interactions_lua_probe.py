"""Unit and real-engine tests for Layer 1 Lua interactions probe (`scan_unsupported_mods`, `inspect_mod_interactions`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from engine_paths import find_runnable_engine

from pob_cli.bridge_client import BridgeWorker
from pob_cli.importers import xml_to_share_code
from pob_cli.snapshot import load_build

_TEST_BUILD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="Main">
        <Gem nameSpec="Fireball" level="20" quality="20" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Red Mod Band
Coral Ring
Item Level: 84
Implicits: 1
+30 to maximum Life
25% increased Spell Damage
30% increased Attack Damage
+42% to Fire Resistance
When Mercury is in retrograde gain 999% Quantum Damage
    </Item>
    <ItemSet id="1">
      <Slot name="Ring 1" itemId="1"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1">
    <ConfigSet id="1">
      <CustomModifierBlock title="Default" enabled="true">
+15% to Critical Strike Multiplier
Totally bogus unparseable custom mod line xyz
      </CustomModifierBlock>
    </ConfigSet>
  </Config>
</PathOfBuilding>
"""


class _FakeLegacyWorker:
    """Simulates a 2.67.2 worker that does NOT advertise `interactionsProbe`."""

    def __init__(self) -> None:
        self.caps: dict[str, Any] = {}
        self.calls: list[str] = []

    def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        self.calls.append(method)
        return {}


def test_interactions_probe_lua_registered_and_within_budget() -> None:
    lua_dir = Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "lua"
    probe_path = lua_dir / "interactions_probe.lua"
    runner_path = lua_dir / "headless_runner.lua"
    assert probe_path.is_file()
    probe_text = probe_path.read_text(encoding="utf-8")
    runner_text = runner_path.read_text(encoding="utf-8")
    assert "scan_unsupported_mods" in probe_text
    assert "inspect_mod_interactions" in probe_text
    assert "pcall(" in probe_text
    assert 'load_ext("interactions_probe.lua")' in runner_text
    assert "CAPS.interactionsProbe = true" in runner_text


def test_legacy_worker_without_capability_makes_zero_probe_calls() -> None:
    worker = _FakeLegacyWorker()
    assert not (worker.caps or {}).get("interactionsProbe")
    assert worker.calls == []


def test_real_engine_scan_unsupported_and_inspect_mod_interactions(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("Real PoB engine not available")

    worker = BridgeWorker(pob_dir=engine, runtime_dir=tmp_path / "rt", slot_name="test_interactions_l1")
    try:
        worker.start()
        assert (worker.caps or {}).get("interactionsProbe") is True

        load_build(worker, xml_code=xml_to_share_code(_TEST_BUILD_XML))

        # 1. scan_unsupported_mods catches both the bogus item line and the bogus customMods line
        scan = worker.call("scan_unsupported_mods", {})
        assert isinstance(scan, dict)
        assert scan["totalCount"] >= 2
        item_lines = [it["rawLine"] for it in scan["items"]]
        custom_lines = [cm["rawLine"] for cm in scan["customMods"]]
        assert any("Mercury is in retrograde" in ln for ln in item_lines)
        assert any("Totally bogus unparseable" in ln for ln in custom_lines)
        assert all("Critical Strike Multiplier" not in ln for ln in custom_lines)
        json.dumps(scan, allow_nan=False)

        # 2. inspect_mod_interactions decodes ModParser INC/MORE/BASE/FLAG/OVERRIDE + ModFlag/KeywordFlag/tags
        probe = worker.call(
            "inspect_mod_interactions",
            {
                "slot": "Ring 1",
                "lines": [
                    "20% increased Spell Damage",
                    "15% more Attack Damage",
                    "+50 to maximum Life",
                    "Cannot be Frozen",
                    "25% increased Damage while on Full Life",
                    "10% increased Damage per Power Charge",
                    "30% increased Projectile Damage",
                    "20% increased Fire Damage",
                    "Nonexistent gibberish line abc",
                ],
            },
        )
        ms = probe["mainSkill"]
        assert ms["skillName"] == "Fireball"
        assert "Spell" in ms["mainModFlagNames"]
        assert "Projectile" in ms["mainModFlagNames"]
        assert "Fire" in ms["mainKeywordFlagNames"]
        assert "Fire" in ms["damageTypes"]

        by_raw = {entry["rawLine"]: entry for entry in probe["lines"]}

        spell_dmg = by_raw["20% increased Spell Damage"]
        assert spell_dmg["unsupported"] is False
        assert any(m["type"] == "INC" and "Spell" in m["flagNames"] for m in spell_dmg["mods"])

        atk_more = by_raw["15% more Attack Damage"]
        assert atk_more["unsupported"] is False
        assert any(m["type"] == "MORE" and "Attack" in m["flagNames"] for m in atk_more["mods"])

        life_base = by_raw["+50 to maximum Life"]
        assert life_base["unsupported"] is False
        assert any(m["name"] == "Life" and m["type"] == "BASE" and m["value"] == 50 for m in life_base["mods"])

        freeze_flag = by_raw["Cannot be Frozen"]
        assert freeze_flag["unsupported"] is False
        assert any(m["type"] in ("FLAG", "BASE", "OVERRIDE") for m in freeze_flag["mods"])

        cond_mod = by_raw["25% increased Damage while on Full Life"]
        assert cond_mod["unsupported"] is False
        assert any(
            any(t.get("type") == "Condition" and t.get("var") == "FullLife" for t in m.get("tags", []))
            for m in cond_mod["mods"]
        )

        mult_mod = by_raw["10% increased Damage per Power Charge"]
        assert mult_mod["unsupported"] is False
        assert any(
            any(t.get("type") == "Multiplier" and t.get("var") == "PowerCharge" for t in m.get("tags", []))
            for m in mult_mod["mods"]
        )

        fire_kw = by_raw["20% increased Fire Damage"]
        assert fire_kw["unsupported"] is False
        assert any(
            m["name"] in ("FireDamage", "Damage")
            and (m["type"] == "INC")
            for m in fire_kw["mods"]
        )

        bogus = by_raw["Nonexistent gibberish line abc"]
        assert bogus["unsupported"] is True
        assert bogus["mods"] == []

        # Slot inspection returns Ring 1 lines including both parsed and unsupported lines
        assert probe["slot"] == "Ring 1"
        slot_by_raw = {entry["rawLine"]: entry for entry in probe["slotLines"]}
        assert any("Mercury is in retrograde" in k and v["unsupported"] for k, v in slot_by_raw.items())
        assert any("Spell Damage" in k and not v["unsupported"] for k, v in slot_by_raw.items())

        # Build-wide mode returns equippedSlots + customModLines + keystones
        wide = worker.call("inspect_mod_interactions", {})
        assert wide["mainSkill"]["skillName"] == "Fireball"
        assert any(s["slot"] == "Ring 1" for s in wide["equippedSlots"])
        assert any("Totally bogus" in cm["rawLine"] and cm["unsupported"] for cm in wide["customModLines"])
        assert isinstance(wide["mainSkill"].get("conversions"), dict)
        json.dumps(wide, allow_nan=False)
    finally:
        worker.stop()


_DOT_FLASK_CONVERSION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="IgniteMain">
        <Gem nameSpec="Flame Surge of Combusting" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Combustion" level="20" quality="20" enabled="true"/>
      </Skill>
      <Skill mainActiveSkill="1" enabled="true" label="EssenceDrain">
        <Gem nameSpec="Essence Drain" level="20" quality="20" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: MAGIC
Quicksilver Flask
Item Level: 84
Implicits: 0
Unticked flask bogus unparsed line xyz
    </Item>
    <Item id="2">
Rarity: UNIQUE
Crown of Eyes
Hubris Circlet
Item Level: 84
Implicits: 0
Increases and Reductions to Spell Damage also apply to Attacks at 150% of their value
    </Item>
    <ItemSet id="1">
      <Slot name="Flask 1" itemId="1" active="false"/>
      <Slot name="Helmet" itemId="2"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>
"""


def test_real_engine_dot_flags_unticked_flask_and_conversions(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("Real PoB engine not available")

    worker = BridgeWorker(pob_dir=engine, runtime_dir=tmp_path / "rt", slot_name="test_interactions_l1_dot")
    try:
        worker.start()
        load_build(worker, xml_code=xml_to_share_code(_DOT_FLASK_CONVERSION_XML))

        # 1. Flame Surge of Combusting (Ignite DoT) unions Dot/Ailment/Ignite/FireDot flags + Crown of Eyes conversion
        probe = worker.call("inspect_mod_interactions", {"lines": ["30% increased Burning Damage", "25% increased Damage over Time"]})
        ms = probe["mainSkill"]
        assert "Flame Surge" in ms["skillName"]
        assert "Dot" in ms["mainModFlagNames"]
        assert "Ailment" in ms["mainModFlagNames"]
        assert "Ignite" in ms["mainKeywordFlagNames"]
        assert "FireDot" in ms["mainKeywordFlagNames"]
        assert ms["conversions"]["spellDamageAppliesToAttacks"] is True

        # 2. Unticked Flask 1 reports active=False in scan_unsupported_mods and inspect_mod_interactions
        scan = worker.call("scan_unsupported_mods", {})
        flask_hits = [it for it in scan["items"] if it["slot"] == "Flask 1"]
        assert flask_hits and all(it["active"] is False for it in flask_hits)

        # 3. Switching to Essence Drain exports dotIsSpell=True and Dot modFlag
        worker.call("set_build_field", {"field": "mainSocketGroup", "value": 2})
        ed_probe = worker.call("inspect_mod_interactions", {"lines": ["25% increased Spell Damage"]})
        ed_ms = ed_probe["mainSkill"]
        assert ed_ms["skillName"] == "Essence Drain"
        assert "Dot" in ed_ms["mainModFlagNames"]
        assert ed_ms["conversions"]["dotIsSpell"] is True
        json.dumps(ed_probe, allow_nan=False)
    finally:
        worker.stop()
