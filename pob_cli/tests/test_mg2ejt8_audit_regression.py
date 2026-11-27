"""Layer 4 tests: DX-2 calc minion actor auto-switch, DX-1 batch flask aliases, and 10-item regression."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

from pob_cli.analyzer.audit import audit_build
from pob_cli.analyzer.calcs import search_calcs_breakdown
from pob_cli.analyzer.calcs_select import is_pure_minion_skill, parse_actor_keywords
from pob_cli.analyzer.diagnose import diagnose_build
from pob_cli.bridge_client import BridgeWorker
from pob_cli.cli_inspect_parts import cmd_calc
from pob_cli.importers_codec import xml_to_share_code
from pob_cli.mutators import run_batch
from pob_cli.mutators.batch_ops import _execute_batch_ops
from pob_cli.mutators.batch_validate import validate_batch_op
from pob_cli.mutators.common import _BatchCache


class _FakeMinionCalcsWorker:
    """Fake worker modeling player vs minion CalcsTab panels and AG skillMinionItemSetCalcs."""

    def __init__(
        self,
        *,
        ag_main_itemset: int | None = 2,
        ag_calcs_itemset: int | None = 1,
        default_itemset_id: int = 1,
    ) -> None:
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = {}
        self.skill_number = 1
        self.main_active = {1: 1, 2: 1, 3: 1}
        self.show_minion = False
        self.ag_main_itemset: int | None = ag_main_itemset
        self.ag_calcs_itemset: int | None = ag_calcs_itemset
        self.default_itemset_id = default_itemset_id
        self.ag_calcs_history: list[int | None] = [ag_calcs_itemset]
        self.flasks = {f"Flask {i}": True for i in range(1, 6)}

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "list_skills":
            return {
                "mainSocketGroup": 1,
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "enabled": True,
                        "label": "Spark",
                        "skills": [{"index": 1, "name": "Spark of the Nova"}],
                        "gems": [{"index": 1, "name": "Spark of the Nova", "enabled": True, "support": False}],
                    },
                    {
                        "index": 2,
                        "isMain": False,
                        "enabled": True,
                        "label": "Absolution",
                        "skills": [{"index": 1, "name": "Absolution"}],
                        "gems": [{"index": 1, "name": "Absolution", "enabled": True, "support": False}],
                    },
                    {
                        "index": 3,
                        "isMain": False,
                        "enabled": True,
                        "label": "Animate Guardian",
                        "skills": [{"index": 1, "name": "Animate Guardian"}],
                        "gems": [{"index": 1, "name": "Animate Guardian", "enabled": True, "support": False}],
                    },
                ],
            }
        if method == "get_build_header":
            return {
                "mainSocketGroup": {"index": 1, "list": [{"label": "Spark"}]},
                "mainSkill": {"index": 1, "list": [{"label": "Spark of the Nova"}]},
            }
        if method == "get_group_gem_states":
            if params.get("group") == 3:
                gem_entry: dict[str, Any] = {"index": 1}
                if self.ag_main_itemset is not None:
                    gem_entry["skillMinionItemSet"] = self.ag_main_itemset
                if self.ag_calcs_itemset is not None:
                    gem_entry["skillMinionItemSetCalcs"] = self.ag_calcs_itemset
                return {"group": 3, "defaultItemSetId": self.default_itemset_id, "gems": [gem_entry]}
            return {"group": params.get("group"), "defaultItemSetId": self.default_itemset_id, "gems": []}
        if method == "apply_group_gem_states":
            for gm in params.get("gemStates") or []:
                if "skillMinionItemSetCalcs" in gm:
                    val = gm["skillMinionItemSetCalcs"]
                    self.ag_calcs_itemset = None if val == "__nil__" else int(val)
                    self.ag_calcs_history.append(self.ag_calcs_itemset)
            return {"ok": True}
        if method == "set_calcs_input":
            var = params["var"]
            if var == "skill_number":
                self.skill_number = int(params["value"])
            elif var == "mainActiveSkill":
                self.main_active[self.skill_number] = int(params["value"])
            elif var == "showMinion":
                self.show_minion = bool(params["value"])
            return {"ok": True}
        if method == "get_calcs":
            has_minion = self.skill_number in (2, 3)
            sk_names = {1: "Spark of the Nova", 2: "Absolution", 3: "Animate Guardian"}
            # CR-13: group 2 carries "Animate Guardian" in the socket group label while selected skill is Absolution.
            grp_labels = {1: "Spark of the Nova", 2: "Absolution + Animate Guardian", 3: "Animate Guardian"}
            life_val = "45,141" if (self.show_minion and self.skill_number == 3 and self.ag_calcs_itemset == 2) else (
                "12,000" if (self.show_minion and self.skill_number == 2) else "1"
            )
            return {
                "input": {"skill_number": self.skill_number, "showMinion": self.show_minion},
                "hasMinion": has_minion,
                "selectors": {
                    "mainSocketGroup": {
                        "index": self.skill_number,
                        "list": [{"label": grp_labels[i]} for i in (1, 2, 3)],
                    },
                    "mainSkill": {"index": 1, "list": [{"label": sk_names[self.skill_number]}]},
                },
                "sections": [
                    {
                        "si": 1,
                        "id": "Defence",
                        "group": "Defence",
                        "subsections": [
                            {
                                "ui": 1,
                                "label": "Life",
                                "rows": [
                                    {"ri": 1, "label": "Total Life", "cells": [{"ci": 1, "raw": life_val, "text": life_val}]}
                                ],
                            }
                        ],
                    }
                ],
            }
        if method == "set_slot_active":
            self.flasks[params["slotName"]] = bool(params["active"])
            return {"ok": True}
        if method == "list_items":
            return {"slots": [], "items": []}
        raise AssertionError(f"unexpected rpc {method}")


def test_dx2_calc_auto_switches_minion_actor_aligns_ag_itemset_and_restores() -> None:
    assert parse_actor_keywords(["@minion Life", "Resist"]) == (["Life", "Resist"], True)
    assert parse_actor_keywords(["@minion", "Life"]) == (["Life"], True)
    assert is_pure_minion_skill("Animate Guardian") is True
    assert is_pure_minion_skill("Summon Raging Spirit") is True
    assert is_pure_minion_skill("Absolution") is False

    w = _FakeMinionCalcsWorker()
    res = search_calcs_breakdown(w, "Life", group=3, no_breakdown=True)  # type: ignore[arg-type]
    assert res["actor"] == "minion"
    assert res["matches"][0]["cells"][0]["raw"] == "45,141"
    # Full read-only restore of skill_number, showMinion, AND skillMinionItemSetCalcs!
    assert w.skill_number == 1
    assert w.show_minion is False
    assert w.ag_calcs_itemset == 1

    # Unset (nil) skillMinionItemSetCalcs round-trips cleanly None -> 2 -> None via "__nil__" sentinel!
    w_nil = _FakeMinionCalcsWorker(ag_calcs_itemset=None)
    res_nil = search_calcs_breakdown(w_nil, "Life", group=3, no_breakdown=True)  # type: ignore[arg-type]
    assert res_nil["actor"] == "minion"
    assert res_nil["matches"][0]["cells"][0]["raw"] == "45,141"
    assert w_nil.ag_calcs_history == [None, 2, None]
    assert w_nil.ag_calcs_itemset is None
    assert w_nil.show_minion is False

    # Default itemSet alignment: skillMinionItemSet=None (defaultItemSetId=1) while Calcs=2 -> 2 -> 1 -> 2!
    w_def = _FakeMinionCalcsWorker(ag_main_itemset=None, ag_calcs_itemset=2, default_itemset_id=1)
    search_calcs_breakdown(w_def, "Life", group=3, no_breakdown=True)  # type: ignore[arg-type]
    assert w_def.ag_calcs_history == [2, 1, 2]
    assert w_def.ag_calcs_itemset == 2
    assert w_def.show_minion is False

    # Hybrid skill without @minion stays player + emits minionHint; with @minion switches to minion
    res_hyb = search_calcs_breakdown(w, "Life", group=2, no_breakdown=True)  # type: ignore[arg-type]
    assert res_hyb["actor"] == "player"
    assert "@minion" in str(res_hyb.get("minionHint") or "")
    assert res_hyb["matches"][0]["cells"][0]["raw"] == "1"

    res_hyb_m = search_calcs_breakdown(w, "@minion Life", group=2, no_breakdown=True)  # type: ignore[arg-type]
    assert res_hyb_m["actor"] == "minion"
    assert res_hyb_m["matches"][0]["cells"][0]["raw"] == "12,000"
    assert w.show_minion is False
    assert w.skill_number == 1


def test_dx2_cli_calc_renders_minion_actor_and_hybrid_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pob_cli.cli_inspect_parts.invoke_daemon",
        lambda method, params: {
            "keyword": "Life",
            "totalMatches": 1,
            "actor": "minion",
            "calcsSkill": {"socketGroup": 3, "label": "Animate Guardian", "skillLabel": "Animate Guardian"},
            "matches": [
                {
                    "sectionGroup": "Defence",
                    "subsection": "Life",
                    "rowLabel": "Total Life",
                    "cells": [{"text": "45,141", "raw": "45,141", "value": 45141.0}],
                }
            ],
        },
    )
    runner = CliRunner()
    out = runner.invoke(cmd_calc, ["Life", "--group", "3"])
    assert out.exit_code == 0, out.output
    assert "[Actor: Minion]" in out.output
    assert "45,141" in out.output


def test_dx1_batch_flask_state_slot_flask_aliases_and_conflict() -> None:
    w = _FakeMinionCalcsWorker()
    cache = _BatchCache(w)  # type: ignore[arg-type]
    steps = _execute_batch_ops(
        w,  # type: ignore[arg-type]
        [
            {"op": "flask", "slot": "Flask 3", "state": "off"},
            {"op": "flask", "flask": 2, "state": False},
            {"op": "item_flask", "index": 3, "state": "on"},
            {"op": "flask", "slot": 4, "active": False, "state": "off"},
            {"op": "flask", "slot": "Flask 5", "enabled": False},
        ],
        cache,
    )
    assert steps[0]["slotName"] == "Flask 3"
    assert steps[0]["active"] is False
    assert steps[1]["slotName"] == "Flask 2"
    assert steps[2]["active"] is True
    assert steps[4]["slotName"] == "Flask 5"
    assert steps[4]["active"] is False
    assert w.flasks["Flask 3"] is True
    assert w.flasks["Flask 2"] is False
    assert w.flasks["Flask 4"] is False
    assert w.flasks["Flask 5"] is False

    with pytest.raises(ValueError, match=r"conflicting 'active' .* 'state'"):
        validate_batch_op(1, "flask", {"op": "flask", "index": 3, "active": True, "state": "off"})
    with pytest.raises(ValueError, match=r"conflicting 'active' .* 'enabled'"):
        validate_batch_op(1, "flask", {"op": "flask", "index": 3, "active": True, "enabled": False})
    with pytest.raises(ValueError, match=r"conflicting 'state' .* 'enabled'"):
        validate_batch_op(1, "flask", {"op": "flask", "slot": "Flask 3", "state": "on", "enabled": False})
    with pytest.raises(ValueError, match="'state' must be a boolean"):
        validate_batch_op(1, "flask", {"op": "flask", "index": 3, "state": "maybe"})


_REGRESSION_BUILD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="97" targetVersion="3_0" className="Scion" ascendClassName="Luminary" mainSocketGroup="1" viewMode="ITEMS">
    <PlayerStat stat="Life" value="1"/>
  </Build>
  <Tree activeSpec="1">
    <Spec title="Default" treeVersion="3_30" classId="0" ascendClassId="3" nodes="58833,15144,918,28221,48768">
      <Sockets>
        <Socket nodeId="48768" itemId="8"/>
      </Sockets>
    </Spec>
  </Tree>
  <Config activeConfigSet="1">
    <ConfigSet id="1" title="Default">
      <Input name="enemyIsBoss" string="Pinnacle"/>
      <Input name="conditionEnemyShocked" boolean="true"/>
      <Input name="conditionShockEffect" number="50"/>
      <Input name="conditionEnemyUnnerved" boolean="true"/>
      <Input name="conditionCritRecently" boolean="true"/>
      <Input name="OverloadedIntensity" number="3"/>
      <Input name="intensifyIntensity" number="4"/>
    </ConfigSet>
  </Config>
  <Items activeItemSet="1" useSecondWeaponSet="false">
    <Item id="1">Rarity: UNIQUE
Mageblood
Heavy Belt
Leftmost 4 Magic Utility Flasks constantly apply their Flask Effects to you</Item>
    <Item id="2">Rarity: MAGIC
Topaz Flask
Quality: 20</Item>
    <Item id="3">Rarity: UNIQUE
Rumi's Concoction
Granite Flask
+11% Chance to Block Attack Damage during Effect</Item>
    <Item id="4">Rarity: RARE
Plague Hand
Warlock Gloves
Implicits: 1
25% chance to Unnerve Enemies for 4 seconds on Hit</Item>
    <Item id="5">Rarity: UNIQUE
Kingmaker
Despot Axe
nearby Allies have +50% to Critical Strike Multiplier</Item>
    <Item id="6">Rarity: RARE
Maelstrom Touch
Wyvernscale Gauntlets
Implicits: 1
Inflict Lightning Exposure on Hit, applying -11% to Lightning Resistance</Item>
    <Item id="7">Rarity: RARE
Storm Wand
Profane Wand
Adds 400 to 1600 Lightning Damage to Spells
120% increased Spell Damage
100% increased Critical Strike Chance for Spells
+150% to Global Critical Strike Multiplier
200% increased Effect of Non-Damaging Ailments</Item>
    <Item id="8">Rarity: UNIQUE
Watcher's Eye
Prismatic Jewel
Selected Variant: 1
4% increased maximum Energy Shield
{variant:1}10% of Physical Damage Converted to Lightning Damage while affected by Wrath
{variant:1}100% increased Critical Strike Chance while affected by Zealotry</Item>
    <ItemSet id="1" title="Default">
      <Slot name="Weapon 1" itemId="7"/>
      <Slot name="Belt" itemId="1"/>
      <Slot name="Gloves" itemId="4"/>
      <Slot name="Flask 1" itemId="2" active="true"/>
      <Slot name="Flask 3" itemId="3" active="true"/>
    </ItemSet>
    <ItemSet id="2" title="Animate Guardian">
      <Slot name="Weapon 1" itemId="5"/>
      <Slot name="Gloves" itemId="6"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" slot="Body Armour" mainActiveSkill="1">
        <Gem nameSpec="Spark of the Nova" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Pinpoint" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Invert the Rules" level="4" quality="20" enabled="true"/>
        <Gem nameSpec="Lightning Penetration" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Greater Spell Echo" level="3" quality="20" enabled="true"/>
        <Gem nameSpec="Overloaded Intensity" level="4" quality="20" enabled="true"/>
      </Skill>
      <Skill enabled="true" slot="Helmet" mainActiveSkill="1">
        <Gem nameSpec="Automation" level="20" quality="0" enabled="true"/>
        <Gem nameSpec="Pact of Beidat" level="20" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" slot="Weapon 1" mainActiveSkill="1">
        <Gem nameSpec="Animate Guardian" level="20" quality="0" enabled="true" skillMinionItemSet="2" skillMinionItemSetCalcs="2"/>
        <Gem nameSpec="Minion Life" level="20" quality="0" enabled="true"/>
        <Gem nameSpec="Companionship" level="4" quality="0" enabled="true"/>
      </Skill>
      <Skill enabled="true" slot="Weapon 1 Swap" mainActiveSkill="1">
        <Gem nameSpec="Invert the Rules" level="2" quality="0" enabled="true"/>
        <Gem nameSpec="Invert the Rules" level="2" quality="0" enabled="true"/>
        <Gem nameSpec="Awakened Enhance" level="3" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
</PathOfBuilding>
"""


def test_real_engine_audit_diagnose_calc_minion_and_batch_flask_regression() -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("no runnable PoB engine available")
    worker = BridgeWorker(pob_root=engine)
    try:
        worker.call("import_code", {"code": xml_to_share_code(_REGRESSION_BUILD_XML)})
        # P2-1: get_shock_probe before dewater must not let config Shock=50 pollute computedShockPct
        probe_pre = worker.call("get_shock_probe", {})
        assert probe_pre["ok"] is True
        assert probe_pre["configDerived"] is True
        assert probe_pre["currentShock"] == 50
        assert 15 <= probe_pre["computedShockPct"] < 50

        # Sub-case B: conditionShockEffect=50 > computed Pinnacle shock -> clamped + positive 2b attribution
        aud = audit_build(worker, include_envelope=False)
        assert any("Flask 3" in s for s in aud["inflationStripped"]["flasks"])
        assert not any("Flask 1" in s for s in aud["inflationStripped"]["flasks"])
        assert any(
            "conditionShockEffect=50 -> " in s and "clamped to PoB Pinnacle Boss shock" in s
            for s in aud["inflationStripped"]["config"]
        )
        shock_attr = [r for r in aud["waterAttribution"] if r.get("key") == "conditionShockEffect"]
        assert len(shock_attr) == 1
        assert shock_attr[0].get("error") is None
        assert float(shock_attr[0].get("dpsDelta") or 0.0) > 0.0

        # Sub-case A: conditionShockEffect=15 <= computed Pinnacle shock -> both shock configs auto-kept
        worker.call("set_config", {"var": "conditionShockEffect", "value": 15})
        aud_keep = audit_build(worker, include_envelope=False)
        auto_kept = aud_keep["inflationStripped"]["autoKept"]
        assert any("conditionEnemyShocked=True" in s and "PoB computed Pinnacle Boss shock" in s for s in auto_kept)
        assert any("conditionShockEffect=15" in s and "PoB computed Pinnacle Boss shock" in s for s in auto_kept)
        worker.call("set_config", {"var": "conditionShockEffect", "value": 50})

        diag = diagnose_build(worker, no_rank=True)
        msgs = [g["message"] for g in diag["healthGaps"]]
        # Swap leveling gems produce zero HIGH/WARN
        assert not any("Weapon 1 Swap" in m for m in msgs)
        # AG Exposure vs Resistance Inversion + AG Kingmaker/Companionship
        assert any("Maelstrom Touch" in m and "Lightning Exposure" in m for m in msgs)
        assert any("Kingmaker" in m and "Companionship" in m for m in msgs)
        # 3.30 Pact + Automation HIGH, Overloaded Intensity, Luminary, Watcher's Eye 0/2
        assert any("Pact of Beidat" in m and "Automation" in m for m in msgs)
        assert any("Intensity / Overloaded ramp" in m for m in msgs)
        assert any("Luminary" in m for m in msgs)
        assert any("Watcher's Eye" in m and "0/" in m for m in msgs)

        # DX-2: calc --group 3 switches to Animate Guardian minion panel & restores showMinion
        calcs_ag = search_calcs_breakdown(worker, "Life", keywords=["Resist"], group=3, no_breakdown=True)
        assert calcs_ag["actor"] == "minion"
        assert calcs_ag["totalMatches"] >= 2
        assert bool((worker.call("get_calcs", {}).get("input") or {}).get("showMinion")) is False

        # DX-1: batch flask with slot + state + enabled aliases
        res_batch = run_batch(
            worker,
            [
                {"op": "flask", "slot": "Flask 3", "state": "off"},
                {"op": "flask", "slot": "Flask 3", "enabled": True},
                {"op": "flask", "slot": "Flask 3", "enabled": False},
            ],
        )
        assert res_batch["detail"]["steps"][0]["slotName"] == "Flask 3"
        assert res_batch["detail"]["steps"][0]["active"] is False
        assert res_batch["detail"]["steps"][1]["active"] is True
        assert res_batch["detail"]["steps"][2]["active"] is False
    finally:
        worker.stop()
