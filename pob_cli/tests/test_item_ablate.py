"""Unit and real-engine tests for B1c single-mod CoW ablation (`pob item inspect <slot> --ablate`)."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

from pob_cli import cli as cli_mod
from pob_cli.bridge_client import BridgeWorker
from pob_cli.errors import DaemonRequestError
from pob_cli.formatters.item_ablate_md import format_item_ablation_markdown
from pob_cli.importers import share_code_to_xml, xml_to_share_code
from pob_cli.item_ablate import ablate_item_mods
from pob_cli.snapshot import capture_digest, history_of, load_build


class _FakeAblateWorker:
    """Stateful fake worker simulating `item_edit_begin/set/commit/cancel` + CoW rollback."""

    def __init__(self, *, caps: dict[str, Any] | None = None, fail_on_index: int | None = None) -> None:
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = dict(caps) if caps is not None else {"itemModLineToggle": True}
        self.fail_on_index = fail_on_index
        self.cancel_calls = 0
        self.display_open = False
        self.raw_item = (
            "Rarity: RARE\n"
            "Brimstone Band\n"
            "Two-Stone Ring\n"
            "Implicits: 1\n"
            "+14% to Cold and Lightning Resistances\n"
            "{fractured}+75 to maximum Life\n"
            "+45 to Strength\n"
            "{crafted}+12% increased Cast Speed\n"
            "10% increased Light Radius\n"
        )
        self.mod_lines: list[dict[str, Any]] = [
            {"index": 1, "section": "implicit", "text": "+14% to Cold and Lightning Resistances", "disabled": False},
            {"index": 2, "section": "explicit", "text": "+75 to maximum Life", "disabled": False},
            {"index": 3, "section": "explicit", "text": "+45 to Strength", "disabled": False},
            {"index": 4, "section": "explicit", "kind": "crafted", "text": "+12% increased Cast Speed", "disabled": False},
            {"index": 5, "section": "explicit", "text": "10% increased Light Radius", "disabled": False},
        ]
        self._edit_copy: list[dict[str, Any]] | None = None
        self._saved: dict[str, list[dict[str, Any]]] = {}
        self._seq = 0
        self.last_good_code = "CODE_INIT"
        self._saved["CODE_INIT"] = copy.deepcopy(self.mod_lines)

    def _stats(self) -> dict[str, Any]:
        active = {ml["index"] for ml in self.mod_lines if not ml.get("disabled")}
        dps = 100_000.0 + (12_000.0 if 4 in active else 0.0)
        life = 4000.0 + (75.0 if 2 in active else 0.0)
        ehp = 50_000.0 + (2_500.0 if 2 in active else 0.0) + (1_500.0 if 1 in active else 0.0)
        cold_res = 75.0 if 1 in active else 61.0
        light_res = 75.0 if 1 in active else 61.0
        str_val = 160.0 if 3 in active else 115.0
        return {
            "FullDPS": dps,
            "CombinedDPS": dps,
            "TotalDPS": dps,
            "TotalEHP": ehp,
            "Life": life,
            "Mana": 800.0,
            "EnergyShield": 0.0,
            "EffectiveSpellSuppressionChance": 100.0,
            "FireResist": 75.0,
            "ColdResist": cold_res,
            "LightningResist": light_res,
            "ChaosResist": 20.0,
            "FireResistOverCap": 10.0,
            "ColdResistOverCap": 0.0,
            "LightningResistOverCap": 0.0,
            "ChaosResistOverCap": 0.0,
            "Str": str_val,
            "Dex": 100.0,
            "Int": 200.0,
            "Omni": 0.0,
            "ReqStr": 155.0,
            "ReqDex": 90.0,
            "ReqInt": 155.0,
            "ReqOmni": 0.0,
        }

    def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        p = params or {}
        if method == "export_code":
            self._seq += 1
            code = f"SNAP_{self._seq}"
            self._saved[code] = copy.deepcopy(self.mod_lines)
            return {"code": code}
        if method == "import_code":
            self.mod_lines = copy.deepcopy(self._saved[str(p["code"])])
            return {"ok": True}
        if method == "list_items":
            return {
                "items": [{"id": 7, "title": "Brimstone Band", "baseName": "Two-Stone Ring", "rarity": "RARE"}],
                "slots": [
                    {"name": "Ring 1", "label": "Ring 1", "selItemId": 7, "shown": True},
                    {"name": "Ring 2", "label": "Ring 2", "selItemId": 0, "shown": True},
                ],
            }
        if method == "get_stats":
            return {"stats": self._stats()}
        if method == "get_sidebar":
            st = self._stats()
            warns = (
                [{"raw": "You do not have enough Strength to use your gems"}]
                if st["Str"] < st["ReqStr"]
                else []
            )
            return {"warnings": warns}
        if method == "get_build_info":
            return {"level": 90, "className": "Witch", "ascendClassName": "Elementalist", "points": {"used": 100}}
        if method == "get_build_header":
            return {}
        if method == "item_edit_begin":
            self.display_open = True
            self._edit_copy = copy.deepcopy(self.mod_lines)
            return {"id": 7, "raw": self.raw_item, "modLines": copy.deepcopy(self._edit_copy)}
        if method == "item_edit_set":
            assert self._edit_copy is not None
            idx = int(p["modLine"]["index"])
            if self.fail_on_index == idx:
                raise RuntimeError("injected engine edit failure")
            for ml in self._edit_copy:
                if ml["index"] == idx:
                    ml["disabled"] = not bool(p["modLine"]["enabled"])
            return {"id": 7, "raw": self.raw_item, "modLines": copy.deepcopy(self._edit_copy)}
        if method == "item_edit_commit":
            assert self._edit_copy is not None
            self.mod_lines = copy.deepcopy(self._edit_copy)
            self._edit_copy = None
            self.display_open = False
            return {"ok": True, "id": 7}
        if method == "item_edit_cancel":
            self.cancel_calls += 1
            self._edit_copy = None
            self.display_open = False
            return {"ok": True}
        return {}


def test_ablate_requires_item_mod_line_toggle_capability() -> None:
    worker = _FakeAblateWorker(caps={})
    with pytest.raises(DaemonRequestError, match="itemModLineToggle"):
        ablate_item_mods(worker, "Ring 1")  # type: ignore[arg-type]


def test_ablate_empty_slot_raises_actionable_error() -> None:
    worker = _FakeAblateWorker()
    with pytest.raises(DaemonRequestError, match="empty"):
        ablate_item_mods(worker, "Ring 2")  # type: ignore[arg-type]


def test_ablate_unit_deltas_categories_dead_mod_load_bearing_and_zero_residue() -> None:
    worker = _FakeAblateWorker()
    history_of(worker).load_build("CODE_INIT", "unit")
    rev_before = history_of(worker).rev

    res = ablate_item_mods(worker, "Ring 1")  # type: ignore[arg-type]
    assert res["slotName"] == "Ring 1"
    assert res["restoreCheck"]["verified"] is True
    assert history_of(worker).rev == rev_before
    assert all(not ml["disabled"] for ml in worker.mod_lines)
    assert not worker.display_open

    by_idx = {d["lineIndex"]: d for d in res["ablation_deltas"]}
    assert [d["slotCategory"] for d in res["ablation_deltas"]] == [
        "implicit",
        "fractured",
        "explicit",
        "crafted",
        "explicit",
    ]
    # Implicit resists
    assert by_idx[1]["deltaResists"]["cold"] == -14.0
    assert by_idx[1]["deltaResists"]["lightning"] == -14.0
    assert by_idx[1]["isDeadMod"] is False
    # Fractured life
    assert by_idx[2]["deltaLife"] == -75.0
    assert by_idx[2]["deltaEhp"] == -2500.0
    assert by_idx[2]["isDeadMod"] is False
    # Load-bearing strength
    assert by_idx[3]["deltaAttributes"]["str"] == -45.0
    assert by_idx[3]["bricksRequirements"] is True
    assert by_idx[3]["brickedReasons"]
    assert by_idx[3]["isDeadMod"] is False
    # Crafted cast speed
    assert by_idx[4]["deltaDps"] == -12000.0
    assert by_idx[4]["deltaDpsPct"] == pytest.approx(-10.714, abs=0.01)
    assert by_idx[4]["isDeadMod"] is False
    # Dead mod
    assert by_idx[5]["deltaDps"] == 0.0
    assert by_idx[5]["deltaEhp"] == 0.0
    assert by_idx[5]["bricksRequirements"] is False
    assert by_idx[5]["isDeadMod"] is True

    assert res["deadModCount"] == 1
    assert res["annullableDeadModCount"] == 1
    assert res["craftable"] is True
    assert res["loadBearingCount"] == 1

    md = format_item_ablation_markdown(res)
    assert "[REQ-LOAD-BEARING]" in md
    assert "[DEAD-MOD]" in md
    assert "0-impact explicit/crafted mod(s) under current Config (annulment is random; verify recovery/block/utility first)" in md
    assert "safe candidate" not in md
    assert "Single-Mod Ablation" in md
    assert (
        "Note: [DEAD-MOD] means 0 direct PoB DPS/EHP/res/attr/speed change under current Config; "
        "verify mana-cost, flask, or on-kill utility before annulling."
    ) in md


@pytest.mark.parametrize(
    ("rarity", "extra_tail", "category"),
    [
        ("UNIQUE", "", "explicit"),
        ("RELIC", "", "explicit"),
        ("RARE", "Corrupted\n", "explicit"),
        ("RARE", "Mirrored\n", "explicit"),
        ("RARE", "", "implicit"),
        ("RARE", "", "fractured"),
        ("RARE", "", "enchant"),
        ("RARE", "", "crucible"),
    ],
)
def test_ablate_never_suggests_annul_reforge_on_non_craftable_or_non_explicit(
    rarity: str, extra_tail: str, category: str
) -> None:
    worker = _FakeAblateWorker()
    is_fractured = category == "fractured"
    mod_prefix = "{fractured}" if is_fractured else ""
    worker.raw_item = (
        f"Rarity: {rarity}\n"
        "Guardrail Band\n"
        "Coral Ring\n"
        "Implicits: 1\n"
        f"{mod_prefix}10% increased Light Radius\n"
        f"{extra_tail}"
    )
    ml_section = "explicit" if is_fractured else category
    ml_kind = "crucible" if category == "crucible" else None
    worker.mod_lines = [
        {
            "index": 5,
            "section": ml_section,
            **({"kind": ml_kind} if ml_kind else {}),
            "text": "10% increased Light Radius",
            "disabled": False,
        }
    ]
    worker._saved["CODE_INIT"] = copy.deepcopy(worker.mod_lines)
    orig_call = worker.call

    def _patched_call(method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        if method == "list_items":
            return {
                "items": [{"id": 7, "title": "Guardrail Band", "baseName": "Coral Ring", "rarity": rarity}],
                "slots": [{"name": "Ring 1", "label": "Ring 1", "selItemId": 7, "shown": True}],
            }
        return orig_call(method, params, **kw)

    worker.call = _patched_call  # type: ignore[method-assign]
    history_of(worker).load_build("CODE_INIT", "unit")

    res = ablate_item_mods(worker, "Ring 1")  # type: ignore[arg-type]
    assert res["deadModCount"] == 1
    assert res["annullableDeadModCount"] == 0
    md = format_item_ablation_markdown(res)
    assert "0-impact explicit/crafted mod(s)" not in md
    assert "annul/reforge" not in md
    assert "safe candidate" not in md
    assert (
        "Note: [DEAD-MOD] means 0 direct PoB DPS/EHP/res/attr/speed change under current Config; "
        "verify mana-cost, flask, or on-kill utility before annulling."
    ) in md


def test_ablate_movement_speed_prevents_dead_mod_and_omits_caveat_when_no_dead_mods() -> None:
    worker = _FakeAblateWorker()
    worker.raw_item = (
        "Rarity: RARE\n"
        "Fleet Greaves\n"
        "Sorcerer Boots\n"
        "Implicits: 0\n"
        "30% increased Movement Speed\n"
    )
    worker.mod_lines = [
        {"index": 5, "section": "explicit", "text": "30% increased Movement Speed", "disabled": False}
    ]
    worker._saved["CODE_INIT"] = copy.deepcopy(worker.mod_lines)
    orig_stats = worker._stats

    def _ms_stats() -> dict[str, Any]:
        st = orig_stats()
        active = {ml["index"] for ml in worker.mod_lines if not ml.get("disabled")}
        st["EffectiveMovementSpeedMod"] = 1.30 if 5 in active else 1.00
        return st

    worker._stats = _ms_stats  # type: ignore[method-assign]
    history_of(worker).load_build("CODE_INIT", "unit")

    res = ablate_item_mods(worker, "Ring 1")  # type: ignore[arg-type]
    assert len(res["ablation_deltas"]) == 1
    d = res["ablation_deltas"][0]
    assert d["deltaMovementSpeed"] == pytest.approx(-30.0)
    assert d["isDeadMod"] is False
    assert res["deadModCount"] == 0
    assert res["annullableDeadModCount"] == 0

    md = format_item_ablation_markdown(res)
    assert "-30%" in md
    assert "[DEAD-MOD]" not in md
    assert "ΔPool/Supp" not in md
    assert "verify mana-cost, flask, or on-kill utility" not in md


def test_ablate_infinite_total_ehp_transition_max_hit_and_pool_supp_column() -> None:
    worker = _FakeAblateWorker()
    worker.raw_item = (
        "Rarity: RARE\n"
        "Aegis Guard Ring\n"
        "Coral Ring\n"
        "Implicits: 0\n"
        "+4% Chance to Block Attack Damage\n"
        "+120 to Armour\n"
        "+50 to maximum Mana\n"
        "12% chance to Suppress Spell Damage\n"
        "15% increased Stun and Block Recovery\n"
    )
    worker.mod_lines = [
        {"index": 11, "section": "explicit", "text": "+4% Chance to Block Attack Damage", "disabled": False},
        {"index": 12, "section": "explicit", "text": "+120 to Armour", "disabled": False},
        {"index": 13, "section": "explicit", "text": "+50 to maximum Mana", "disabled": False},
        {"index": 14, "section": "explicit", "text": "12% chance to Suppress Spell Damage", "disabled": False},
        {"index": 15, "section": "explicit", "text": "15% increased Stun and Block Recovery", "disabled": False},
    ]
    worker._saved["CODE_INIT"] = copy.deepcopy(worker.mod_lines)
    orig_stats = worker._stats

    def _inf_stats() -> dict[str, Any]:
        st = orig_stats()
        active = {ml["index"] for ml in worker.mod_lines if not ml.get("disabled")}
        st["TotalEHP"] = "inf" if 11 in active else 50000.0
        st["PhysicalMaximumHitTaken"] = 18000.0 if 12 in active else 15000.0
        st["Mana"] = 850.0 if 13 in active else 800.0
        st["EffectiveSpellSuppressionChance"] = 100.0 if 14 in active else 88.0
        return st

    worker._stats = _inf_stats  # type: ignore[method-assign]
    history_of(worker).load_build("CODE_INIT", "unit")

    res = ablate_item_mods(worker, "Ring 1")  # type: ignore[arg-type]
    assert res["baseline"]["totalEhp"] == "inf"
    assert res["baseline"]["totalEhpInfinite"] is True

    by_idx = {d["lineIndex"]: d for d in res["ablation_deltas"]}
    # Mod 11 breaks inf -> 50000: MUST NOT report deltaEhp=+50000 or isDeadMod=True
    assert by_idx[11]["deltaEhp"] is None
    assert by_idx[11]["deltaEhpPct"] is None
    assert by_idx[11]["ehpTransition"] == "inf->50000"
    assert by_idx[11]["isDeadMod"] is False
    # Mod 12 keeps inf->inf but drops PhysMaxHit 18000->15000: MUST NOT be isDeadMod=True
    assert by_idx[12]["ehpTransition"] == "inf->inf"
    assert by_idx[12]["deltaMaxHits"]["phys"] == -3000.0
    assert by_idx[12]["isDeadMod"] is False
    # Mod 13 (-50 Mana) and Mod 14 (-12% Supp): not dead, rendered in ΔPool/Supp
    assert by_idx[13]["deltaMana"] == -50.0
    assert by_idx[13]["isDeadMod"] is False
    assert by_idx[14]["deltaSuppression"] == -12.0
    assert by_idx[14]["isDeadMod"] is False
    # Mod 15: truly dead even under inf->inf
    assert by_idx[15]["ehpTransition"] == "inf->inf"
    assert by_idx[15]["isDeadMod"] is True

    md = format_item_ablation_markdown(res)
    assert "inf -> 50,000" in md
    assert "+50,000" not in md
    assert "ΔPool/Supp" in md
    assert "Mana-50" in md
    assert "Supp-12%" in md


def test_ablate_restores_build_and_cancels_edit_on_exception() -> None:
    worker = _FakeAblateWorker(fail_on_index=3)
    history_of(worker).load_build("CODE_INIT", "unit")
    with pytest.raises(RuntimeError, match="injected engine edit failure"):
        ablate_item_mods(worker, "Ring 1")  # type: ignore[arg-type]
    assert not worker.display_open
    assert worker.cancel_calls >= 2
    assert all(not ml["disabled"] for ml in worker.mod_lines)


def test_cli_item_inspect_ablate_flags_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _FakeAblateWorker()
    history_of(worker).load_build("CODE_INIT", "unit")
    ablation_res = ablate_item_mods(worker, "Ring 1")  # type: ignore[arg-type]

    monkeypatch.setattr("pob_cli.cli_item_inspect.invoke_daemon", lambda act, p, **_kw: ablation_res)
    monkeypatch.setattr(
        "pob_cli.item_inspect.inspect_item",
        lambda *_a, **_kw: {
            "item": {"name": "Brimstone Band", "rarity": "RARE", "itemLevel": 84, "slot": "Ring 1"},
            "affixes": [],
            "unmatched": [],
            "counts": {"prefix": 1, "suffix": 2},
            "open": {"prefix": 2, "suffix": 1},
            "capacity": {"prefix": 3, "suffix": 3},
            "certain": True,
            "dataWarnings": [],
            "craftEligible": True,
            "suggestions": [],
        },
    )
    runner = CliRunner()
    res_json = runner.invoke(cli_mod.main, ["item", "inspect", "Ring 1", "--ablate", "--json"])
    assert res_json.exit_code == 0, res_json.output
    parsed = json.loads(res_json.output)
    assert "ablation" in parsed
    assert len(parsed["ablation"]["ablation_deltas"]) == 5

    res_md = runner.invoke(cli_mod.main, ["item", "inspect", "--slot", "Ring 1", "--ablate"])
    assert res_md.exit_code == 0, res_md.output
    assert "[DEAD-MOD]" in res_md.output
    assert "[REQ-LOAD-BEARING]" in res_md.output

    bad_combo = runner.invoke(cli_mod.main, ["item", "inspect", "--ablate", "--raw", "Rarity: RARE"])
    assert bad_combo.exit_code != 0

    missing_slot = runner.invoke(cli_mod.main, ["item", "inspect", "--ablate"])
    assert missing_slot.exit_code != 0


def test_ablate_recovery_block_cost_stats_and_lexical_utility_guard() -> None:
    worker = _FakeAblateWorker()
    worker.raw_item = (
        "Rarity: RARE\n"
        "Sustain Guard Ring\n"
        "Coral Ring\n"
        "Implicits: 0\n"
        "Regenerate 1.5% of Life per second\n"
        "+4% Chance to Block Attack Damage\n"
        "-7 to Total Mana Cost of Non-Channelling Skills\n"
        "15% increased Stun and Block Recovery\n"
        "+20% to Fire Resistance\n"
    )
    worker.mod_lines = [
        {"index": 21, "section": "explicit", "text": "Regenerate 1.5% of Life per second", "disabled": False},
        {"index": 22, "section": "explicit", "text": "+4% Chance to Block Attack Damage", "disabled": False},
        {"index": 23, "section": "explicit", "text": "-7 to Total Mana Cost of Non-Channelling Skills", "disabled": False},
        {"index": 24, "section": "explicit", "text": "15% increased Stun and Block Recovery", "disabled": False},
        {"index": 25, "section": "explicit", "text": "+20% to Fire Resistance", "disabled": False},
    ]
    worker._saved["CODE_INIT"] = copy.deepcopy(worker.mod_lines)
    orig_stats = worker._stats

    def _util_stats() -> dict[str, Any]:
        st = orig_stats()
        active = {ml["index"] for ml in worker.mod_lines if not ml.get("disabled")}
        st["NetLifeRegen"] = 180.0 if 21 in active else 120.0
        st["EffectiveBlockChance"] = 75.0 if 22 in active else 71.0
        st["ManaCost"] = 10.0 if 23 in active else 17.0
        # Overcap-only Fire Resist change (capped stays 75%, overcap drops 30->10)
        st["FireResistOverCap"] = 30.0 if 25 in active else 10.0
        return st

    worker._stats = _util_stats  # type: ignore[method-assign]
    history_of(worker).load_build("CODE_INIT", "unit")

    res = ablate_item_mods(worker, "Ring 1")  # type: ignore[arg-type]
    by_idx = {d["lineIndex"]: d for d in res["ablation_deltas"]}
    assert by_idx[21]["isDeadMod"] is False
    assert by_idx[21]["deltaUtility"]["lifeRegen"] == -60.0
    assert by_idx[22]["isDeadMod"] is False
    assert by_idx[22]["deltaUtility"]["block"] == -4.0
    assert by_idx[23]["isDeadMod"] is False
    assert by_idx[23]["deltaUtility"]["manaCost"] == 7.0
    # Mod 24 has 0 direct PoB change, yet lexical guard keeps it out of annullableDeadModCount
    assert by_idx[24]["isDeadMod"] is True
    assert by_idx[25]["isDeadMod"] is False
    assert res["annullableDeadModCount"] == 0

    md = format_item_ablation_markdown(res)
    assert "LReg-60/s" in md
    assert "Block-4%" in md
    assert "MCost+7" in md
    assert "F-20%(ov)" in md
    assert "0-impact explicit/crafted mod(s)" not in md


def test_ablate_zh_locale_warning_alignment_and_daemon_handler_dispatch() -> None:
    from pob_cli.daemon.rpc_handlers import _SLOT_ACTION_HANDLERS, dispatch_slot_action

    assert "item_ablate" in _SLOT_ACTION_HANDLERS
    worker = _FakeAblateWorker()
    worker.caps["directCalc"] = True
    history_of(worker).load_build("CODE_INIT", "unit")
    orig_call = worker.call

    def _zh_call(method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        if method == "get_sidebar":
            return {"warnings": [{"raw": "You do not have enough Dexterity"}]}
        if method == "direct_calc":
            return {"stats": worker._stats(), "warnings": ["敏捷不足，无法使用技能"]}
        return orig_call(method, params, **kw)

    worker.call = _zh_call  # type: ignore[method-assign]

    class _FakeMgr:
        def _attach_warning_if_degraded(self, payload: dict[str, Any]) -> dict[str, Any]:
            return payload

    res = dispatch_slot_action(_FakeMgr(), "item_ablate", {"slot": "Ring 1"}, worker)  # type: ignore[arg-type]
    by_idx = {d["lineIndex"]: d for d in res["ablation_deltas"]}
    # Pre-existing zh warning is identical in baseline and candidate -> mod 5 must NOT brick requirements
    assert by_idx[5]["bricksRequirements"] is False
    # Mod 3 still bricks via numeric Str < ReqStr
    assert by_idx[3]["bricksRequirements"] is True


def test_real_engine_item_ablate_zero_residue_and_dead_mod(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("Real PoB engine not available")

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="Main">
        <Gem nameSpec="Fireball" level="20" quality="20" enabled="true"/>
      </Skill>
      <Skill mainActiveSkill="1" enabled="true" label="Aura">
        <Gem nameSpec="Determination" level="1" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Test Ablate Ring
Coral Ring
Item Level: 84
Implicits: 1
+30 to maximum Life
+50 to Strength
16% increased Cast Speed
+42% to Fire Resistance
10% increased Light Radius
{crafted}+55 to maximum Life
    </Item>
    <ItemSet id="1">
      <Slot name="Ring 1" itemId="1"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>
"""
    worker = BridgeWorker(pob_dir=engine, runtime_dir=tmp_path / "rt", slot_name="test_ablate")
    try:
        worker.start()
        if not (worker.caps or {}).get("itemModLineToggle"):
            pytest.skip("Engine bridge does not advertise itemModLineToggle")

        load_build(worker, xml_code=xml_to_share_code(xml))
        rev_before = history_of(worker).rev
        xml_before = share_code_to_xml(worker.call("export_code", {})["code"])
        dig_before = capture_digest(worker)
        dps_before = float(dig_before["stats"].get("CombinedDPS") or 0.0)
        ehp_before = float(dig_before["stats"].get("TotalEHP") or 0.0)

        res = ablate_item_mods(worker, "Ring 1")
        assert res["restoreCheck"]["verified"] is True

        deltas = res["ablation_deltas"]
        assert len(deltas) == 6

        by_text = {d["modText"].lower(): d for d in deltas}
        cast_mod = next(v for k, v in by_text.items() if "cast speed" in k)
        assert cast_mod["deltaDps"] < 0
        assert cast_mod["deltaDpsPct"] < 0
        assert cast_mod["isDeadMod"] is False

        fire_mod = next(v for k, v in by_text.items() if "fire resistance" in k)
        assert fire_mod["deltaResists"]["fire"] < 0
        assert fire_mod["deltaEhp"] < 0
        assert fire_mod["isDeadMod"] is False

        crafted_life = next(d for d in deltas if d["slotCategory"] == "crafted")
        assert crafted_life["deltaLife"] < 0
        assert crafted_life["deltaEhp"] < 0
        assert crafted_life["isDeadMod"] is False

        str_mod = next(v for k, v in by_text.items() if "strength" in k)
        assert str_mod["deltaAttributes"]["str"] < 0
        assert str_mod["bricksRequirements"] is True
        assert str_mod["isDeadMod"] is False

        dead_mod = next(v for k, v in by_text.items() if "light radius" in k)
        assert dead_mod["deltaDps"] == 0.0
        assert dead_mod["deltaEhp"] == 0.0
        assert dead_mod["bricksRequirements"] is False
        assert dead_mod["isDeadMod"] is True
        assert res["deadModCount"] == 1
        assert res["annullableDeadModCount"] == 1

        # Strict zero-residue invariance check (rev, XML, DPS, EHP)
        assert history_of(worker).rev == rev_before
        xml_after = share_code_to_xml(worker.call("export_code", {})["code"])
        assert xml_after == xml_before
        dig_after = capture_digest(worker)
        assert float(dig_after["stats"].get("CombinedDPS") or 0.0) == pytest.approx(dps_before)
        assert float(dig_after["stats"].get("TotalEHP") or 0.0) == pytest.approx(ehp_before)
    finally:
        worker.stop()


def test_real_engine_boots_movement_speed_not_dead_mod(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("Real PoB engine not available")

    xml = """<?xml version="1.0" encoding="UTF-8"?>
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
    <Item id="2">
Rarity: RARE
Speed Test Boots
Sorcerer Boots
Item Level: 84
Implicits: 0
30% increased Movement Speed
    </Item>
    <ItemSet id="1">
      <Slot name="Boots" itemId="2"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>
"""
    worker = BridgeWorker(pob_dir=engine, runtime_dir=tmp_path / "rt", slot_name="test_ablate_boots")
    try:
        worker.start()
        if not (worker.caps or {}).get("itemModLineToggle"):
            pytest.skip("Engine bridge does not advertise itemModLineToggle")

        load_build(worker, xml_code=xml_to_share_code(xml))
        boots_res = ablate_item_mods(worker, "Boots")
        assert boots_res["restoreCheck"]["verified"] is True
        ms_mod = boots_res["ablation_deltas"][0]
        assert ms_mod["deltaMovementSpeed"] == pytest.approx(-30.0, abs=0.5)
        assert ms_mod["isDeadMod"] is False
        assert boots_res["deadModCount"] == 0
    finally:
        worker.stop()
