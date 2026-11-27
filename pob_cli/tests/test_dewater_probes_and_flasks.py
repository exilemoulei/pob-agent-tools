"""Layer 1 tests: Lua dewater_probes diet/CAPS, Pass 0 flask sustain, Mageblood/Toad's Price, Offering corpses."""

from __future__ import annotations

import copy
import pathlib
import threading
from typing import Any

import pytest
from engine_paths import find_runnable_engine

from pob_cli.analyzer import dewater_build_with_records
from pob_cli.analyzer.dewater_provenance import (
    _has_utility_flasks_disabled_fallback,
    evaluate_flask_provenance,
    fetch_dewater_provenance,
    has_corpse_source,
    is_offering_gem,
)
from pob_cli.bridge_client import BridgeWorker
from pob_cli.importers_codec import xml_to_share_code

LUA_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "pob_cli" / "lua"


class _ProbedWorker:
    """Fake worker supporting optional `dewaterProbes` capability and batch RPC counting."""

    def __init__(
        self,
        *,
        caps: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        flasks: list[dict[str, Any]] | None = None,
        extra_items: list[dict[str, Any]] | None = None,
        extra_slots: list[dict[str, Any]] | None = None,
        groups: list[dict[str, Any]] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        if caps is not None:
            self.caps = dict(caps)
        self.provenance = provenance
        self.config: dict[str, Any] = dict(config or {"enemyIsBoss": "Pinnacle"})
        self.flasks: list[dict[str, Any]] = [dict(f) for f in (flasks or [])]
        self.extra_items = copy.deepcopy(list(extra_items or []))
        self.extra_slots = copy.deepcopy(list(extra_slots or []))
        self.groups = copy.deepcopy(list(groups or []))
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.batch_calls: list[list[tuple[str, dict[str, Any]]]] = []

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        self.batch_calls.append([(m, dict(p or {})) for m, p in calls])
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if method == "get_dewater_provenance":
            if self.provenance is None:
                raise RuntimeError("get_dewater_provenance unavailable")
            return self.provenance
        if method == "list_config":
            return {
                "sections": [{"items": [{"var": k, "value": v} for k, v in self.config.items()]}],
                "customMods": [],
            }
        if method == "set_config":
            self.config[params["var"]] = params["value"]
            return {}
        if method == "reset_config":
            self.config[params["var"]] = None
            return {}
        if method == "list_items":
            slots = []
            items = []
            for idx, f in enumerate(self.flasks, 1):
                slots.append({"name": f["slot"], "isFlask": True, "active": f["active"], "selItemId": idx})
                items.append(
                    {
                        "id": idx,
                        "name": f.get("title", f["slot"]),
                        "title": f.get("title", f["slot"]),
                        "baseName": f.get("baseName", "Granite Flask"),
                        "rarity": f.get("rarity", "MAGIC"),
                        "type": f.get("type", "Flask"),
                    }
                )
            base_id = len(items)
            for off, it in enumerate(self.extra_items, 1):
                entry = dict(it)
                entry.setdefault("id", base_id + off)
                items.append(entry)
            for sl in self.extra_slots:
                slots.append(dict(sl))
            return {"slots": slots, "items": items}
        if method == "set_slot_active":
            for f in self.flasks:
                if f["slot"] == params["slotName"]:
                    f["active"] = bool(params["active"])
            return {}
        if method == "list_skills":
            return {"mainSocketGroup": 1, "groups": self.groups}
        if method == "set_gem":
            for g in self.groups:
                if g["index"] == params["group"]:
                    for gm in g.get("gems") or []:
                        if gm["index"] == params["index"]:
                            gm["enabled"] = bool(params.get("enabled", gm.get("enabled", True)))
            return {}
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        raise AssertionError(f"unexpected rpc {method}")


def test_agent_probes_diet_and_dewater_probes_budget() -> None:
    ap_path = LUA_DIR / "agent_probes.lua"
    dp_path = LUA_DIR / "dewater_probes.lua"
    hr_path = LUA_DIR / "headless_runner.lua"
    assert dp_path.is_file()

    ap_raw = ap_path.read_bytes()
    ap_txt = ap_raw.decode("utf-8").replace("\r\n", "\n")
    ap_crlf = max(len(ap_raw), len(ap_txt.encode("utf-8")) + len(ap_txt.splitlines()))
    # Shrunk by ~3.5KB from 23,940B -> well under 21,000B!
    assert ap_crlf < 21_500, f"agent_probes.lua did not shrink enough: {ap_crlf}B"
    assert "eval_scenario_envelope = handle_eval_scenario_envelope" in ap_txt
    assert "__pob4agents_eval_scenario_envelope" in ap_txt

    dp_raw = dp_path.read_bytes()
    dp_txt = dp_raw.decode("utf-8").replace("\r\n", "\n")
    dp_lines = dp_txt.splitlines()
    dp_crlf = max(len(dp_raw), len(dp_txt.encode("utf-8")) + len(dp_lines))
    assert len(dp_lines) <= 350
    assert dp_crlf <= 24_000
    assert "get_dewater_provenance = handle_get_dewater_provenance" in dp_txt
    assert "eval_scenario_envelope = handle_eval_scenario_envelope" in dp_txt

    hr_txt = hr_path.read_text(encoding="utf-8")
    assert 'load_ext("dewater_probes.lua")' in hr_txt
    assert "CAPS.dewaterProbes = true" in hr_txt


def test_fake_worker_without_dewater_probes_cap_incurs_zero_extra_rpcs() -> None:
    w_nocap = _ProbedWorker(
        caps=None,
        config={"enemyIsBoss": "None", "conditionOnslaught": True},
        flasks=[{"slot": "Flask 1", "active": True, "title": "Diamond Flask", "rarity": "MAGIC"}],
    )
    assert fetch_dewater_provenance(w_nocap) is None
    assert w_nocap.calls == []

    dewater_build_with_records(w_nocap)  # type: ignore[arg-type]
    assert len(w_nocap.batch_calls) == 2
    assert all(m != "get_dewater_provenance" for m, _ in w_nocap.calls)

    w_empty = _ProbedWorker(
        caps={},
        config={"enemyIsBoss": "None", "conditionOnslaught": True},
        flasks=[{"slot": "Flask 1", "active": True, "title": "Diamond Flask", "rarity": "MAGIC"}],
    )
    dewater_build_with_records(w_empty)  # type: ignore[arg-type]
    assert len(w_empty.batch_calls) == 2
    assert all(m != "get_dewater_provenance" for m, _ in w_empty.calls)


def test_foulborn_corrupted_mageblood_and_toads_price_veto() -> None:
    # Foulborn rightmost 4 Mageblood: Flask 1 is Unique Cinderswallow Urn (stripped!), Flask 2..5 Magic Utility (kept!)
    flasks = [
        {"slot": "Flask 1", "active": True, "rarity": "UNIQUE", "baseName": "Silver Flask", "title": "Cinderswallow Urn"},
        {"slot": "Flask 2", "active": True, "rarity": "MAGIC", "baseName": "Topaz Flask", "title": "Topaz Flask"},
        {"slot": "Flask 3", "active": True, "rarity": "MAGIC", "baseName": "Ruby Flask", "title": "Ruby Flask"},
        {"slot": "Flask 4", "active": True, "rarity": "MAGIC", "baseName": "Sapphire Flask", "title": "Sapphire Flask"},
        {"slot": "Flask 5", "active": True, "rarity": "MAGIC", "baseName": "Quicksilver Flask", "title": "Quicksilver Flask"},
    ]
    prov_foulborn = {
        "ok": True,
        "mageblood": {
            "leftMagicUtility": 0,
            "rightMagicUtility": 4,
            "utilityFlasksDisabled": False,
            "controlledSlots": ["Flask 2", "Flask 3", "Flask 4", "Flask 5"],
            "hasSpikedConcoction": False,
            "hasBloodsoakedBlade": False,
        },
        "flasks": [
            {"slot": f["slot"], "active": True, "isTincture": False, "isLife": False, "sustained": False, "chargeGainBlocked": False}
            for f in flasks
        ],
        "corpseSources": [],
    }
    w = _ProbedWorker(caps={"dewaterProbes": True}, provenance=prov_foulborn, flasks=flasks)
    stripped, _ = dewater_build_with_records(w)  # type: ignore[arg-type]
    assert {f["slot"]: f["active"] for f in w.flasks} == {
        "Flask 1": False,
        "Flask 2": True,
        "Flask 3": True,
        "Flask 4": True,
        "Flask 5": True,
    }
    assert any("Flask 1" in s for s in stripped["flasks"])
    assert any("Mageblood Rightmost 4" in s for s in stripped["autoKept"])

    # Toad's Price (`utilityFlasksDisabled=True`) vetoes ALL Mageblood exemptions!
    prov_toad = {
        **prov_foulborn,
        "mageblood": {**prov_foulborn["mageblood"], "utilityFlasksDisabled": True, "controlledSlots": []},
    }
    w_toad = _ProbedWorker(caps={"dewaterProbes": True}, provenance=prov_toad, flasks=flasks)
    stripped_toad, _ = dewater_build_with_records(w_toad)  # type: ignore[arg-type]
    assert all(not f["active"] for f in w_toad.flasks)
    assert len(stripped_toad["flasks"]) == 5


def test_pathfinder_per3_spiked_concoction_traitor_per5_and_enkindling_block() -> None:
    # Pathfinder 5-flask per3 >= 100% + Spiked Concoction closes loop on buffAlchemistsGenius;
    # Flask 5 has Enkindling Orb (chargeGainBlocked=True) -> MUST be stripped!
    flasks = [
        {"slot": "Flask 1", "active": True, "rarity": "MAGIC", "baseName": "Divine Life Flask", "title": "Divine Life Flask"},
        {"slot": "Flask 2", "active": True, "rarity": "UNIQUE", "baseName": "Ruby Flask", "title": "Coruscating Elixir"},
        {"slot": "Flask 3", "active": True, "rarity": "MAGIC", "baseName": "Topaz Flask", "title": "Topaz Flask"},
        {"slot": "Flask 4", "active": True, "rarity": "UNIQUE", "baseName": "Iron Flask", "title": "Olroth's Resolve"},
        {"slot": "Flask 5", "active": True, "rarity": "MAGIC", "baseName": "Quicksilver Flask", "title": "Enkindled Quicksilver"},
    ]
    prov = {
        "ok": True,
        "mageblood": {
            "leftMagicUtility": 0,
            "rightMagicUtility": 0,
            "utilityFlasksDisabled": False,
            "controlledSlots": [],
            "hasSpikedConcoction": True,
            "hasBloodsoakedBlade": False,
        },
        "flasks": [
            {"slot": "Flask 1", "active": True, "isTincture": False, "isLife": True, "sustained": True, "chargeGainBlocked": False, "minUptimePct": 100.0, "chargesPerSec": 2.4},
            {"slot": "Flask 2", "active": True, "isTincture": False, "isLife": False, "sustained": True, "chargeGainBlocked": False, "minUptimePct": 100.0, "chargesPerSec": 2.1},
            {"slot": "Flask 3", "active": True, "isTincture": False, "isLife": False, "sustained": True, "chargeGainBlocked": False, "minUptimePct": 100.0, "chargesPerSec": 2.1},
            {"slot": "Flask 4", "active": True, "isTincture": False, "isLife": False, "sustained": True, "chargeGainBlocked": False, "minUptimePct": 100.0, "chargesPerSec": 1.8},
            {"slot": "Flask 5", "active": True, "isTincture": False, "isLife": False, "sustained": False, "chargeGainBlocked": True, "minUptimePct": 0.0, "chargesPerSec": 2.1},
        ],
        "corpseSources": [],
    }
    w = _ProbedWorker(
        caps={"dewaterProbes": True},
        provenance=prov,
        config={"enemyIsBoss": "Pinnacle", "buffAlchemistsGenius": True, "conditionKilledRecently": True},
        flasks=flasks,
    )
    stripped, _ = dewater_build_with_records(w)  # type: ignore[arg-type]
    assert {f["slot"]: f["active"] for f in w.flasks} == {
        "Flask 1": True,
        "Flask 2": True,
        "Flask 3": True,
        "Flask 4": True,
        "Flask 5": False,
    }
    assert w.config["buffAlchemistsGenius"] is True
    assert w.config["conditionKilledRecently"] is None
    assert any("buffAlchemistsGenius=True" in s and "Spiked Concoction" in s for s in stripped["autoKept"])
    assert any("Flask 5" in s for s in stripped["flasks"])

    # --no-auto-keep (auto_keep_live=False) disables sustained flask & Alchemist's Genius auto-keeps
    w_noak = _ProbedWorker(
        caps={"dewaterProbes": True},
        provenance=prov,
        config={"enemyIsBoss": "Pinnacle", "buffAlchemistsGenius": True},
        flasks=flasks,
    )
    st_noak, _ = dewater_build_with_records(w_noak, auto_keep_live=False)  # type: ignore[arg-type]
    assert all(not f["active"] for f in w_noak.flasks)
    assert w_noak.config["buffAlchemistsGenius"] is None
    assert st_noak["autoKept"] == []


def test_offering_stripped_without_corpse_source_and_kept_with_desecrate_or_corpsewalker() -> None:
    offering_grp = {
        "index": 1,
        "enabled": True,
        "gems": [{"index": 1, "name": "Bone Offering", "nameSpec": "Bone Offering", "enabled": True, "support": False}],
    }
    w_dry = _ProbedWorker(groups=[offering_grp])
    stripped_dry, recs_dry = dewater_build_with_records(w_dry)  # type: ignore[arg-type]
    assert w_dry.groups[0]["gems"][0]["enabled"] is False
    assert any("Bone Offering" in s and "no single-boss corpse source" in s for s in stripped_dry["skills"])
    assert any(r["key"] == "Bone Offering" for r in recs_dry)

    desecrate_grp = {
        "index": 2,
        "enabled": True,
        "gems": [{"index": 1, "name": "Desecrate", "nameSpec": "Desecrate", "enabled": True, "support": False}],
    }
    w_fed = _ProbedWorker(groups=[offering_grp, desecrate_grp])
    stripped_fed, _ = dewater_build_with_records(w_fed)  # type: ignore[arg-type]
    assert w_fed.groups[0]["gems"][0]["enabled"] is True
    assert stripped_fed["skills"] == []

    # Corpsewalker boots also satisfy corpse source requirement
    items_cw = {
        "slots": [{"name": "Boots", "selItemId": 10}],
        "items": [{"id": 10, "title": "Corpsewalker", "name": "Corpsewalker"}],
    }
    assert has_corpse_source({"groups": [offering_grp]}, items_cw) is True
    assert evaluate_flask_provenance(w_fed, {"slots": [], "items": []}, has_bloodsoaked_blade=True)["bloodsoakedBlade"] is True


@pytest.mark.parametrize("gem_tok", ["亵渎", "褻瀆", "亵渎之矛", "屍術矢"])
def test_corpse_gem_zh_tokens_keep_offering_enabled(gem_tok: str) -> None:
    offering_grp = {
        "index": 1,
        "enabled": True,
        "gems": [{"index": 1, "name": "装甲奉献", "nameSpec": "装甲奉献", "enabled": True, "support": False}],
    }
    corpse_grp = {
        "index": 2,
        "enabled": True,
        "gems": [{"index": 1, "name": gem_tok, "nameSpec": gem_tok, "enabled": True, "support": False}],
    }
    assert is_offering_gem(offering_grp["gems"][0]) is True
    assert has_corpse_source({"groups": [offering_grp, corpse_grp]}) is True
    w = _ProbedWorker(groups=[offering_grp, corpse_grp])
    stripped, _ = dewater_build_with_records(w)  # type: ignore[arg-type]
    assert w.groups[0]["gems"][0]["enabled"] is True
    assert stripped["skills"] == []


@pytest.mark.parametrize("item_tok", ["灵盒行者", "行屍走肉", "奥库娜的意志", "奧庫納之意"])
def test_corpse_item_zh_tokens_keep_offering_enabled(item_tok: str) -> None:
    offering_grp = {
        "index": 1,
        "enabled": True,
        "gems": [{"index": 1, "name": "骸骨奉献", "nameSpec": "骸骨奉献", "enabled": True, "support": False}],
    }
    items = {
        "slots": [{"name": "Boots", "selItemId": 10}],
        "items": [{"id": 10, "title": item_tok, "name": item_tok}],
    }
    assert has_corpse_source({"groups": [offering_grp]}, items) is True
    w = _ProbedWorker(
        groups=[offering_grp],
        extra_items=[{"id": 10, "title": item_tok, "name": item_tok}],
        extra_slots=[{"name": "Boots", "selItemId": 10}],
    )
    stripped, _ = dewater_build_with_records(w)  # type: ignore[arg-type]
    assert w.groups[0]["gems"][0]["enabled"] is True
    assert stripped["skills"] == []


def test_offering_zh_disabled_without_corpse_source() -> None:
    offering_grp = {
        "index": 1,
        "enabled": True,
        "gems": [{"index": 1, "name": "装甲奉献", "nameSpec": "装甲奉献", "enabled": True, "support": False}],
    }
    assert has_corpse_source({"groups": [offering_grp]}, {"slots": [], "items": []}) is False
    w = _ProbedWorker(groups=[offering_grp])
    stripped, _ = dewater_build_with_records(w)  # type: ignore[arg-type]
    assert w.groups[0]["gems"][0]["enabled"] is False
    assert any("装甲奉献" in s and "no single-boss corpse source" in s for s in stripped["skills"])


@pytest.mark.parametrize(
    "raw_line",
    [
        "Utility Flasks are Disabled",
        "Flasks do not apply to you",
        "功能药剂被禁用",
        "功能藥劑被禁用",
    ],
)
def test_utility_flasks_disabled_fallback_matches_real_en_zh_strings(raw_line: str) -> None:
    assert _has_utility_flasks_disabled_fallback(None, [{"slotName": "Belt", "raw": raw_line}]) is True
    items_res = {
        "slots": [{"name": "Belt", "selItemId": 99}],
        "items": [{"id": 99, "title": "Belt", "raw": raw_line}],
    }
    assert _has_utility_flasks_disabled_fallback(items_res, None) is True
    assert _has_utility_flasks_disabled_fallback({"slots": [], "items": []}, [{"slotName": "Belt", "raw": "Normal belt"}]) is False


_PROVENANCE_ENGINE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="95" targetVersion="3_0" className="Ranger" ascendClassName="Pathfinder" mainSocketGroup="1" viewMode="ITEMS">
    <PlayerStat stat="Life" value="4000"/>
  </Build>
  <Tree activeSpec="1">
    <Spec title="Default" treeVersion="3_30" classId="2" ascendClassId="3" nodes="58833"/>
  </Tree>
  <Config activeConfigSet="1">
    <ConfigSet id="1" title="Default">
      <Input name="enemyIsBoss" string="Pinnacle"/>
    </ConfigSet>
  </Config>
  <Items activeItemSet="1" useSecondWeaponSet="false">
    <Item id="1">Rarity: UNIQUE
Mageblood
Heavy Belt
Leftmost 3 Magic Utility Flasks constantly apply their Flask Effects to you</Item>
    <Item id="2">Rarity: MAGIC
Topaz Flask
Quality: 20</Item>
    <Item id="3">Rarity: UNIQUE
Rumi's Concoction
Granite Flask
+11% Chance to Block Attack Damage during Effect</Item>
    <Item id="4">Rarity: MAGIC
Ruby Flask
Quality: 20
Enkindling Orb
Gains no Charges during Effect</Item>
    <ItemSet id="1" title="Default">
      <Slot name="Belt" itemId="1"/>
      <Slot name="Flask 1" itemId="2" active="true"/>
      <Slot name="Flask 2" itemId="3" active="true"/>
      <Slot name="Flask 3" itemId="4" active="true"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" slot="Body Armour" mainActiveSkill="1">
        <Gem nameSpec="Lightning Strike" level="20" quality="20" enabled="true"/>
      </Skill>
      <Skill enabled="true" slot="Gloves" mainActiveSkill="1">
        <Gem nameSpec="Molten Shell" level="20" quality="0" enabled="true"/>
        <Gem nameSpec="Bone Offering" level="20" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
</PathOfBuilding>
"""


def test_real_engine_dewater_provenance_and_envelope_delegation() -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("no runnable PoB engine available")
    worker = BridgeWorker(engine)
    try:
        worker.call("import_code", {"code": xml_to_share_code(_PROVENANCE_ENGINE_XML)})
        assert worker.caps.get("dewaterProbes") is True
        prov = worker.call("get_dewater_provenance", {})
        assert prov["ok"] is True
        mb = prov["mageblood"]
        assert mb["leftMagicUtility"] == 3
        assert mb["utilityFlasksDisabled"] is False
        assert mb["controlledSlots"] == ["Flask 1", "Flask 3"]
        by_slot = {f["slot"]: f for f in prov["flasks"]}
        assert by_slot["Flask 1"]["magebloodControlled"] is True
        assert by_slot["Flask 2"]["magebloodControlled"] is False
        assert by_slot["Flask 3"]["chargeGainBlocked"] is True
        assert prov["corpseSources"] == []
        assert any(s["name"] == "Molten Shell" and s["duration"] > 0 and s["cooldown"] > 0 for s in prov["cooldownSkills"])

        # Delegated eval_scenario_envelope via agent_probes.lua -> dewater_probes.lua still works
        env_res = worker.call("eval_scenario_envelope", {})
        assert "scenarios" in env_res
        assert "PinnacleSustained" in env_res["scenarios"]
    finally:
        worker.stop()
