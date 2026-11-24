"""Real-engine check: agent_probe_build's trigger rate cap equals PoB's own TriggerRateCap."""

from __future__ import annotations

import base64
import tempfile
import zlib
from pathlib import Path
from typing import Any

import pytest

_BUILD = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="95" targetVersion="3_0" className="Shadow" ascendClassName="Assassin" mainSocketGroup="1" viewMode="TREE"/>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
  <Tree activeSpec="1"><Spec classId="6" ascendClassId="1" treeVersion="3_30" nodes="58229"/></Tree>
  <Items activeItemSet="1">
    <Item id="1">Rarity: RARE
Doom Edge
Jewelled Foil
Implicits: 0
14% increased Attack Speed
{SPEED_MOD}
</Item>
    <ItemSet id="1"><Slot name="Weapon 1" itemId="1"/></ItemSet>
  </Items>
  <Skills activeSkillSet="1"><SkillSet id="1">
    <Skill enabled="true" slot="Body Armour" mainActiveSkill="2" includeInFullDPS="true">
      <Gem nameSpec="Cyclone" skillId="Cyclone" level="20" quality="20" enabled="true"/>
      <Gem nameSpec="Cast On Critical Strike" skillId="SupportCastOnCriticalStrike" level="20" quality="20" enabled="true"/>
      <Gem nameSpec="{SPELL}" skillId="{SPELL_ID}" level="20" quality="20" enabled="true"/>
    </Skill>
  </SkillSet></Skills>
</PathOfBuilding>"""


def _encode(xml: str) -> str:
    return base64.urlsafe_b64encode(zlib.compress(xml.encode("utf-8"), 9)).decode("ascii")


@pytest.mark.parametrize(
    ("spell", "spell_id", "speed_mod"),
    [
        # Lightning Conduit adds its cast time, over cast speed, to the trigger cooldown.
        ("Lightning Conduit", "LightningConduit", ""),
        ("Lightning Conduit", "LightningConduit", "30% increased Cast Speed"),
        ("Ice Nova", "IceNova", ""),
    ],
)
def test_probe_trigger_rate_cap_matches_pob(spell: str, spell_id: str, speed_mod: str) -> None:
    from pob_cli.bridge_client import BridgeWorker
    from pob_cli.updater import build_engine_command, resolve_engine_root

    eng = resolve_engine_root()
    try:
        build_engine_command(eng)
    except Exception:
        pytest.skip("Real PoE1 engine runtime not available")
    if not (eng / "PoE1" / "Data").is_dir():
        pytest.skip("Real PoE1 engine data not available")

    xml = _BUILD.replace("{SPELL_ID}", spell_id).replace("{SPELL}", spell).replace("{SPEED_MOD}", speed_mod)
    with tempfile.TemporaryDirectory() as td:
        w = BridgeWorker(slot_name="test", pob_dir=eng, runtime_dir=Path(td))
        try:
            w.start(restore_code="")
            if not w.caps.get("agentProbes"):
                pytest.skip("Engine does not advertise agentProbes")
            w.call("import_code", {"code": _encode(xml), "mode": "replace"})
            stats: dict[str, Any] = w.call("get_stats", {})["stats"]
            pob_cap = stats.get("TriggerRateCap")
            if not isinstance(pob_cap, (int, float)) or pob_cap <= 0:
                pytest.skip(f"PoB reports no TriggerRateCap for {spell}")
            cliffs = w.call("agent_probe_build", {})["triggerCliffs"]
            mine = [c for c in cliffs if c["skillName"] == spell]
            assert len(mine) == 1, cliffs
            assert mine[0]["mainGroup"] is True
            assert mine[0]["triggerRateCap"] == pytest.approx(pob_cap, rel=1e-6)
        finally:
            w._kill_unlocked()
