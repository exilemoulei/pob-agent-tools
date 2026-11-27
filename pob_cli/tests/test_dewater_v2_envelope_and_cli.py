"""Layer 3 tests: 5-cmd sync, audit --boss, matrix Uber clamp, sweep --no-auto-keep, Cycle/Burst ref & real PoB 2.69."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

from pob_cli import cli as cli_mod
from pob_cli.analyzer import attribute_water, audit_build, diagnose_build
from pob_cli.analyzer.dewater_attribution import COUPLED_CONFIG_PAIRS, group_coupled_records
from pob_cli.analyzer.dewater_provenance import build_cycle_envelope
from pob_cli.bridge_client import BridgeWorker
from pob_cli.comparator import run_matrix
from pob_cli.daemon import rpc_handlers, rpc_mutate
from pob_cli.formatters import (
    format_compare_markdown,
    format_cycle_burst_line,
    format_diagnose_markdown,
    format_matrix_markdown,
    format_sweep_markdown,
)
from pob_cli.importers_codec import xml_to_share_code
from pob_cli.mutators import run_sweep


def test_coupled_elusive_and_fortification_attribution_and_partial_clamp() -> None:
    pair_flags = {f for f, _ in COUPLED_CONFIG_PAIRS}
    assert {"buffElusive", "buffFortification"}.issubset(pair_flags)

    recs = [
        {
            "dimension": "config",
            "key": "buffElusive",
            "label": "buffElusive=True",
            "restore": [("set_config", {"var": "buffElusive", "value": True})],
            "revert": [("reset_config", {"var": "buffElusive"})],
            "probe": {"kind": "config", "var": "buffElusive"},
            "original": True,
        },
        {
            "dimension": "config",
            "key": "overrideBuffElusive",
            "label": "overrideBuffElusive=180",
            "restore": [("set_config", {"var": "overrideBuffElusive", "value": 180})],
            "revert": [("reset_config", {"var": "overrideBuffElusive"})],
            "probe": {"kind": "config", "var": "overrideBuffElusive"},
            "original": 180,
        },
        {
            "dimension": "config",
            "key": "buffFortification",
            "label": "buffFortification=True",
            "restore": [("set_config", {"var": "buffFortification", "value": True})],
            "revert": [("reset_config", {"var": "buffFortification"})],
            "probe": {"kind": "config", "var": "buffFortification"},
            "original": True,
        },
        {
            "dimension": "config",
            "key": "overrideFortification",
            "label": "overrideFortification=25",
            "restore": [("set_config", {"var": "overrideFortification", "value": 25})],
            "revert": [("reset_config", {"var": "overrideFortification"})],
            "probe": {"kind": "config", "var": "overrideFortification"},
            "original": 25,
        },
    ]
    grouped = group_coupled_records(recs)
    assert len(grouped) == 2
    assert grouped[0]["key"] == "buffElusive+overrideBuffElusive"
    assert grouped[1]["key"] == "buffFortification+overrideFortification"

    # Partial clamp (e.g. buffFortification kept, only overrideFortification stripped) stays ungrouped!
    partial = [recs[3]]
    assert group_coupled_records(partial) == partial


def test_cycle_burst_line_and_dewater_md_across_audit_diagnose_sweep_batch_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prov = {
        "cooldownSkills": [
            {
                "name": "Arcane Cloak",
                "duration": 3.0,
                "cooldown": 5.5,
                "dutyCyclePct": 35.29,
                "isGuard": True,
                "isVaal": False,
                "isAutomated": False,
                "manaSpend": 4200,
            },
            {
                "name": "Molten Shell",
                "duration": 3.0,
                "cooldown": 7.5,
                "dutyCyclePct": 28.57,
                "isGuard": True,
                "isVaal": False,
                "isAutomated": True,
            },
        ],
        "flasks": [
            {"slot": "Flask 5", "title": "Prismatic Tincture", "isTincture": True},
        ],
    }
    stripped = {
        "config": ["conditionShockEffect=50 -> 25 (clamped to PoB Pinnacle Boss shock 25%)"],
        "flasks": ["Flask 5 (Prismatic Tincture) -> active=False"],
        "skills": [
            "Group #2 Gem #1 (Arcane Cloak) -> enabled=False",
            "Group #3 Gem #1 (Molten Shell) -> enabled=False",
        ],
        "party": [],
        "kept": [],
        "autoKept": ["conditionEnemyShocked=True (auto-kept: PoB computed Pinnacle Boss shock 25%)"],
        "autoClamped": ["conditionShockEffect=50 -> 25 (clamped to PoB Pinnacle Boss shock 25%)"],
        "keepRejected": [],
    }
    sc_env = {
        "scenarios": {
            "Burst": {"dps": 14_200_000.0, "totalEhp": 95_000.0, "physMaxHit": 45_000.0, "eleMinMaxHit": 92_000.0},
            "PinnacleSustained": {"dps": 8_000_000.0, "totalEhp": 52_000.0, "physMaxHit": 18_000.0, "eleMinMaxHit": 41_000.0},
        }
    }
    cycle_env = build_cycle_envelope(prov, stripped, sc_env)
    assert cycle_env is not None
    assert cycle_env["sustainedGuardOff"] is True
    assert len(cycle_env["skills"]) == 3

    line = format_cycle_burst_line(cycle_env)
    assert line is not None
    assert "Cycle / Burst Ref" in line
    assert "Sustained main panel keeps guard/CD skills OFF" in line
    assert "Arcane Cloak (35.3% uptime, 3.0s/5.5s CD, ~4,200 Mana spend)" in line
    assert "[Auto] Molten Shell (28.6% uptime, 3.0s/7.5s CD)" in line
    assert "Prismatic Tincture (non-sustained Tincture)" in line
    assert "Burst DPS `14,200,000`" in line

    # Diagnose renders both Auto-Clamped and Cycle / Burst Ref
    diag_md = format_diagnose_markdown(
        {
            "env": {"slot": "main"},
            "buildInfo": {"level": 95, "className": "Templar", "ascendClassName": "Hierophant"},
            "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 14_000_000.0, "totalEhp": 90_000.0},
            "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 8_000_000.0, "totalEhp": 50_000.0},
            "inflationStripped": stripped,
            "cycleEnvelope": cycle_env,
        }
    )
    assert "**Auto-Clamped (Boss Physical Cap)**" in diag_md
    assert "**Cycle / Burst Ref**" in diag_md

    # Sweep & Matrix & Compare all surface autoKept & autoClamped
    sweep_md = format_sweep_markdown(
        {
            "baseline": {"summary": {"primaryDps": 100_000.0, "totalEhp": 50_000.0}},
            "variants": [{"name": "v1", "ok": True, "summary": {"primaryDps": 110_000.0, "totalEhp": 50_000.0}}],
            "scenario": "Pinnacle",
            "scenarioApplied": {"stripped": stripped},
        }
    )
    assert "**Auto-Kept (Build-Guaranteed)**" in sweep_md
    assert "**Auto-Clamped (Boss Physical Cap)**" in sweep_md

    mat_md = format_matrix_markdown(
        {
            "baseline": "Original",
            "scenarios": [{"name": "Pinnacle", "summary": {"primaryDps": 100_000.0, "totalEhp": 50_000.0}, "applied": {"stripped": stripped}}],
        }
    )
    assert "**Auto-Clamped (Boss Physical Cap)**" in mat_md

    cmp_md = format_compare_markdown(
        {
            "mainBuildInfo": {"level": 95, "ascendClassName": "Hierophant"},
            "refBuildInfo": {"level": 95, "ascendClassName": "Elementalist"},
            "baselines": {
                "original": [],
                "envAlignedToMain": {"rows": []},
                "pinnacleDewatered": {"mainStripped": stripped, "refStripped": {}, "rows": []},
            },
        }
    )
    assert "**Auto-Clamped (Boss Physical Cap)**" in cmp_md

    # CLI audit --boss Uber + sweep --no-auto-keep + batch attached audit autoKept/autoClamped
    captured: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(action: str, params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        captured.append((action, dict(params)))
        if action == "audit":
            return {
                "healthIssues": [],
                "inflationStripped": stripped,
                "cycleEnvelope": cycle_env,
                "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 100_000.0, "totalEhp": 50_000.0},
                "dewateredPinnacle": {"primaryDpsKey": "CombinedDPS", "primaryDps": 80_000.0, "totalEhp": 40_000.0},
            }
        if action == "sweep":
            return {
                "baseline": {"summary": {"primaryDps": 100_000.0, "totalEhp": 50_000.0}},
                "variants": [{"name": "v1", "ok": True, "summary": {"primaryDps": 100_000.0, "totalEhp": 50_000.0}}],
                "scenarioApplied": {"stripped": stripped},
            }
        if action == "batch":
            return {
                "applied": False,
                "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 100_000.0, "totalEhp": 50_000.0, "life": 5000.0},
                "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 100_000.0, "totalEhp": 50_000.0, "life": 5000.0},
                "diff": {},
                "returns": {"audit": {"inflationStripped": stripped, "cycleEnvelope": cycle_env}},
            }
        raise AssertionError(action)

    monkeypatch.setattr("pob_cli.cli_inspect.invoke_daemon", fake_invoke)
    monkeypatch.setattr("pob_cli.cli_optimize.invoke_daemon", fake_invoke)
    runner = CliRunner()

    r_aud = runner.invoke(cli_mod.main, ["audit", "--boss", "Uber"])
    assert r_aud.exit_code == 0, r_aud.output
    assert captured[0][1]["boss"] == "Uber"
    assert "**Auto-Clamped (Boss Physical Cap)**" in r_aud.output
    assert "**Cycle / Burst Ref**" in r_aud.output
    assert "Inflation ('Water') Stripped for Uber Boss Baseline" in r_aud.output
    assert "Original Panel vs True Uber Boss Baseline (De-watered)" in r_aud.output
    assert "| Dewatered Uber" in r_aud.output
    assert "Original/Pinnacle Burst DPS `14,200,000`" in r_aud.output
    assert "Dewatered Pinnacle" not in r_aud.output

    r_swp = runner.invoke(
        cli_mod.main,
        ["sweep", "--scenario", "Pinnacle", "--no-auto-keep", "--json-variants", '[{"name":"v1","ops":[]}]'],
    )
    assert r_swp.exit_code == 0, r_swp.output
    assert captured[1][1]["no_auto_keep"] is True
    assert "**Auto-Clamped (Boss Physical Cap)**" in r_swp.output

    r_bat = runner.invoke(
        cli_mod.main,
        ["batch", "--json-ops", '[{"op":"config","var":"enemyIsBoss","value":"Pinnacle"}]', "--return", "audit"],
    )
    assert r_bat.exit_code == 0, r_bat.output
    assert "**Auto-Kept (Build-Guaranteed)**" in r_bat.output
    assert "**Auto-Clamped (Boss Physical Cap)**" in r_bat.output


def test_vaal_skill_cycle_envelope_never_emits_fake_uptime_and_pass2_warn_renders() -> None:
    prov = {
        "cooldownSkills": [
            {
                "name": "Vaal Molten Shell",
                "duration": 10.9,
                "cooldown": 0.5,
                "dutyCyclePct": 95.6,
                "isGuard": True,
                "isVaal": True,
                "isAutomated": False,
            },
        ],
        "flasks": [],
    }
    stripped = {
        "config": [],
        "flasks": [],
        "skills": ["Group #2 Gem #1 (Vaal Molten Shell) -> enableGlobal1=False (base skill kept)"],
        "party": [],
        "kept": [],
        "autoKept": [],
        "autoClamped": [],
        "keepRejected": [],
        "pass2Applied": False,
        "pass2Warnings": ["get_dewater_provenance failed (timeout); fell back to basic mechanics"],
    }
    cyc = build_cycle_envelope(prov, stripped)
    assert cyc is not None
    assert cyc["skills"][0]["isVaal"] is True
    assert cyc["skills"][0]["dutyCyclePct"] == 0.0
    line = format_cycle_burst_line(cyc)
    assert line is not None
    assert "Vaal Molten Shell (Vaal burst, soul-limited, 10.9s dur)" in line
    assert "% uptime" not in line

    from pob_cli.formatters import format_dewater_summary_lines

    summary = "\n".join(format_dewater_summary_lines(stripped, cycle_envelope=cyc))
    assert "[WARN] Pass 2 Boss clamping degraded" in summary
    assert "timeout" in summary


def test_rpc_handlers_forward_audit_boss_and_sweep_no_auto_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_audit: dict[str, Any] = {}
    seen_sweep: dict[str, Any] = {}

    def fake_audit(_w: Any, **kw: Any) -> dict[str, Any]:
        seen_audit.update(kw)
        return {"ok": True}

    def fake_sweep(_w: Any, **kw: Any) -> dict[str, Any]:
        seen_sweep.update(kw)
        return {"ok": True}

    monkeypatch.setattr("pob_cli.analyzer.audit_build", fake_audit)
    monkeypatch.setattr("pob_cli.mutators.run_sweep", fake_sweep)
    rpc_handlers._h_audit(None, {"boss": "uber", "no_auto_keep": False}, None)  # type: ignore[arg-type]
    assert seen_audit["boss"] == "Uber"
    rpc_mutate._h_sweep(None, {"variants": [{"name": "v1", "ops": []}], "no_auto_keep": True}, None)  # type: ignore[arg-type]
    assert seen_sweep["auto_keep"] is False


class _MatrixUberClampWorker:
    """Fake worker verifying `matrix Uber` clamps Pinnacle shock (39% -> 25%) on Uber boss."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.caps = {"dewaterProbes": True}
        self.config: dict[str, Any] = {
            "enemyIsBoss": "Pinnacle",
            "conditionEnemyShocked": True,
            "conditionShockEffect": 39,
        }
        self._saved: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def _stats(self) -> dict[str, Any]:
        boss_mult = 0.7 if self.config.get("enemyIsBoss") == "Uber" else 0.9
        shock = float(self.config.get("conditionShockEffect") or 0) if self.config.get("conditionEnemyShocked") else 0.0
        dps = 100_000.0 * boss_mult * (1.0 + shock / 100.0)
        return {
            "CombinedDPS": dps, "TotalEHP": 50_000.0, "Life": 4000.0,
            "PhysicalMaximumHitTaken": 15_000.0, "FireMaximumHitTaken": 30_000.0,
            "ColdMaximumHitTaken": 30_000.0, "LightningMaximumHitTaken": 30_000.0,
            "ChaosMaximumHitTaken": 20_000.0,
        }

    def call_batch(self, calls: list[tuple[str, dict[str, Any] | None]], **_kw: Any) -> list[dict[str, Any]]:
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "export_code":
            self._seq += 1
            tok = f"CODE_{self._seq}"
            self._saved[tok] = dict(self.config)
            return {"code": tok}
        if method == "import_code":
            self.config = dict(self._saved[params["code"]])
            return {}
        if method == "list_config":
            return {"sections": [{"items": [{"var": k, "value": v} for k, v in self.config.items()]}], "customMods": []}
        if method == "set_config":
            self.config[params["var"]] = params["value"]
            return {}
        if method == "reset_config":
            self.config[params["var"]] = None
            return {}
        if method == "list_items":
            return {"slots": [], "items": []}
        if method == "list_skills":
            return {"mainSocketGroup": 1, "groups": []}
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method == "get_stats":
            return {"stats": self._stats()}
        if method == "get_sidebar":
            return {"warnings": []}
        if method in ("get_build_info", "get_build_header"):
            return {"level": 95, "className": "Witch", "ascendClassName": "Elementalist"}
        if method == "get_dewater_provenance":
            boss = self.config.get("enemyIsBoss") or "Pinnacle"
            shock_cap = 25.0 if boss == "Uber" else 42.0
            return {
                "ok": True,
                "mageblood": {"leftMagicUtility": 0, "rightMagicUtility": 0, "utilityFlasksDisabled": False, "controlledSlots": []},
                "flasks": [],
                "corpseSources": [],
                "cooldownSkills": [],
                "ailments": {"Shock": {"canInflict": True, "computedPct": shock_cap, "minimumPct": 0.0, "maxCap": 50}},
                "flags": {"enemyIsBoss": boss, "mainSkill": {"isProxy": False}},
            }
        raise AssertionError(f"unexpected rpc {method}")


def test_matrix_uber_clamps_shock_and_sweep_respects_no_auto_keep() -> None:
    w = _MatrixUberClampWorker()
    mat = run_matrix(w, ["Pinnacle", "Uber"])  # type: ignore[arg-type]
    by_name = {s["name"]: s for s in mat["scenarios"]}
    # Pinnacle kept 39% shock -> 100k * 0.9 * 1.39 = 125,100
    assert by_name["Pinnacle"]["digest"]["primaryDps"] == pytest.approx(125_100.0)
    # Uber re-clamped 39% -> 25% -> 100k * 0.7 * 1.25 = 87,500!
    assert by_name["Uber"]["digest"]["primaryDps"] == pytest.approx(87_500.0)
    assert any("39 -> 25" in s for s in (by_name["Uber"]["applied"]["stripped"].get("autoClamped") or []))

    swp = run_sweep(w, [{"name": "base", "ops": []}], scenario="Pinnacle", auto_keep=False)  # type: ignore[arg-type]
    # With auto_keep=False, shock is stripped (0%) -> 100k * 0.9 = 90,000
    assert swp["variants"][0]["summary"]["primaryDps"] == pytest.approx(90_000.0)


_ARCHMAGE_AND_MELEE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="95" targetVersion="3_0" className="Templar" ascendClassName="Hierophant" mainSocketGroup="1" viewMode="ITEMS">
    <PlayerStat stat="Life" value="3500"/>
  </Build>
  <Tree activeSpec="1">
    <Spec title="Default" treeVersion="3_30" classId="5" ascendClassId="2" nodes="58833"/>
  </Tree>
  <Config activeConfigSet="1">
    <ConfigSet id="1" title="Default">
      <Input name="enemyIsBoss" string="None"/>
      <Input name="multiplierNearbyEnemies" number="10"/>
      <Input name="conditionEnemyShocked" boolean="true"/>
      <Input name="conditionShockEffect" number="50"/>
      <Input name="multiplierManaSpentRecently" number="12000"/>
    </ConfigSet>
  </Config>
  <Items activeItemSet="1" useSecondWeaponSet="false">
    <Item id="1">Rarity: RARE
Storm Wand
Profane Wand
Adds 350 to 1400 Lightning Damage to Spells
120% increased Spell Damage
100% increased Critical Strike Chance for Spells
200% increased Effect of Non-Damaging Ailments</Item>
    <Item id="2">Rarity: UNIQUE
Indigon
Hubris Circlet
50% increased Spell Damage for each 200 Total Mana you have Spent Recently</Item>
    <ItemSet id="1" title="Default">
      <Slot name="Weapon 1" itemId="1"/>
      <Slot name="Helmet" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill enabled="true" slot="Body Armour" mainActiveSkill="1">
        <Gem nameSpec="Ball Lightning" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Spell Echo" level="20" quality="20" enabled="true"/>
      </Skill>
      <Skill enabled="true" slot="Gloves" mainActiveSkill="1">
        <Gem nameSpec="Arcane Cloak" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Increased Duration" level="20" quality="20" enabled="true"/>
      </Skill>
      <Skill enabled="true" slot="Boots" mainActiveSkill="1">
        <Gem nameSpec="Automation" level="20" quality="0" enabled="true"/>
        <Gem nameSpec="Molten Shell" level="20" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
</PathOfBuilding>
"""


def test_real_engine_v2_audit_boss_uber_cycle_envelope_and_cow_stability() -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("no runnable PoB engine available")
    worker = BridgeWorker(engine)
    try:
        worker.call("import_code", {"code": xml_to_share_code(_ARCHMAGE_AND_MELEE_XML)})
        # Default audit locks Pinnacle, clamps multiplierNearbyEnemies 10 -> 1, keeps Arcane Cloak & Molten Shell OFF,
        # emits cycleEnvelope with Arcane Cloak duty cycle + manaSpend, and restores cleanly with zero CoW fallback!
        aud_pin = audit_build(worker)
        assert aud_pin["restoreCheck"]["verified"] is True
        assert not aud_pin["restoreCheck"].get("fallback"), aud_pin["restoreCheck"]
        stripped = aud_pin["inflationStripped"]
        assert any("multiplierNearbyEnemies=10 -> 1" in s for s in stripped.get("autoClamped") or [])
        assert any("Arcane Cloak" in s or "奥术斗篷" in s or "奧術斗篷" in s for s in stripped["skills"])
        assert any("Molten Shell" in s or "熔岩护盾" in s or "熔岩護盾" in s for s in stripped["skills"])
        cyc = aud_pin.get("cycleEnvelope")
        assert isinstance(cyc, dict) and cyc.get("sustainedGuardOff") is True
        cyc_by_name = {s["name"]: s for s in cyc["skills"]}
        assert "Arcane Cloak" in cyc_by_name
        assert cyc_by_name["Arcane Cloak"]["dutyCyclePct"] > 0
        assert cyc_by_name["Arcane Cloak"].get("manaSpend", 0) > 0
        assert "Molten Shell" in cyc_by_name

        # audit --boss Uber evaluates directly on Uber boss without modifying persisted build
        aud_uber = audit_build(worker, boss="Uber", include_envelope=False)
        assert any("-> 'Uber'" in s for s in aud_uber["inflationStripped"]["config"])
        assert aud_uber["dewateredPinnacle"]["primaryDps"] < aud_pin["dewateredPinnacle"]["primaryDps"]
        assert aud_uber["restoreCheck"]["verified"] is True

        # diagnose exposes cycleEnvelope too
        diag = diagnose_build(worker, no_rank=True)
        assert isinstance(diag.get("cycleEnvelope"), dict)
        assert any(s["name"] == "Arcane Cloak" for s in diag["cycleEnvelope"]["skills"])
        assert attribute_water is not None
    finally:
        worker.stop()
