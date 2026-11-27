"""Unit and real-engine tests for Layer 3: pairwise synergy ablation & shared PoE1/PoE2 C2 regression schema."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine
from regression_bank_schema import SCHEMA_VERSION, validate_regression_manifest

from pob_cli import cli as cli_mod
from pob_cli.bridge_client import BridgeWorker
from pob_cli.formatters.item_ablate_md import format_item_ablation_markdown
from pob_cli.importers import xml_to_share_code
from pob_cli.item_ablate import ablate_item_mods
from pob_cli.snapshot import load_build


class _FakeSynergyWorker:
    """Fake worker with multiplicative DPS synergy (mods 1 & 2) + joint Str & ColdRes collapse (mods 3 & 4)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.caps: dict[str, Any] = {"itemModLineToggle": True}
        self.raw_item = (
            "Rarity: RARE\n"
            "Synergy Band\n"
            "Two-Stone Ring\n"
            "Implicits: 1\n"
            "+20 to Strength\n"
            "25% increased Cast Speed\n"
            "40% increased Spell Damage\n"
            "+20 to Strength\n"
            "+25% to Cold Resistance\n"
            "+25% to Cold Resistance\n"
        )
        self.mod_lines: list[dict[str, Any]] = [
            {"index": 1, "section": "explicit", "text": "25% increased Cast Speed", "disabled": False},
            {"index": 2, "section": "explicit", "text": "40% increased Spell Damage", "disabled": False},
            {"index": 3, "section": "implicit", "text": "+20 to Strength", "disabled": False},
            {"index": 4, "section": "explicit", "text": "+20 to Strength", "disabled": False},
            {"index": 5, "section": "explicit", "text": "+25% to Cold Resistance", "disabled": False},
            {"index": 6, "section": "explicit", "text": "+25% to Cold Resistance", "disabled": False},
        ]
        self._edit_copy: list[dict[str, Any]] | None = None
        self._saved: dict[str, list[dict[str, Any]]] = {"INIT": copy.deepcopy(self.mod_lines)}
        self._seq = 0
        self.last_good_code = "INIT"

    def _stats(self) -> dict[str, Any]:
        active = {ml["index"] for ml in self.mod_lines if not ml.get("disabled")}
        speed_mult = 1.25 if 1 in active else 1.0
        dmg_mult = 1.40 if 2 in active else 1.0
        dps = 100_000.0 * speed_mult * dmg_mult
        # Baseline Str = 175 (Req 150). Removing ONE +20 -> 155 (>=150 OK); removing BOTH -> 135 (<150 COLLAPSE!)
        str_val = 135.0 + (20.0 if 3 in active else 0.0) + (20.0 if 4 in active else 0.0)
        # Baseline raw ColdRes = 105 (cap 75, overcap 30). Removing ONE +25 -> 80 (capped 75, overcap 5); BOTH -> 55 (<75 COLLAPSE!)
        raw_cold = 55.0 + (25.0 if 5 in active else 0.0) + (25.0 if 6 in active else 0.0)
        cold_res = min(75.0, raw_cold)
        cold_ov = max(0.0, raw_cold - 75.0)
        ehp = 50_000.0 * (1.0 if cold_res >= 75.0 else 0.72)
        return {
            "FullDPS": dps,
            "CombinedDPS": dps,
            "TotalDPS": dps,
            "TotalEHP": ehp,
            "Life": 4000.0,
            "Mana": 800.0,
            "EnergyShield": 0.0,
            "EffectiveSpellSuppressionChance": 100.0,
            "FireResist": 75.0,
            "FireResistMax": 75.0,
            "ColdResist": cold_res,
            "ColdResistMax": 75.0,
            "LightningResist": 75.0,
            "LightningResistMax": 75.0,
            "ChaosResist": 20.0,
            "FireResistOverCap": 10.0,
            "ColdResistOverCap": cold_ov,
            "LightningResistOverCap": 10.0,
            "ChaosResistOverCap": 0.0,
            "Str": str_val,
            "Dex": 100.0,
            "Int": 200.0,
            "Omni": 0.0,
            "ReqStr": 150.0,
            "ReqDex": 90.0,
            "ReqInt": 150.0,
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
                "items": [{"id": 9, "title": "Synergy Band", "baseName": "Two-Stone Ring", "rarity": "RARE"}],
                "slots": [{"name": "Ring 1", "label": "Ring 1", "selItemId": 9, "shown": True}],
            }
        if method == "get_stats":
            return {"stats": self._stats()}
        if method == "get_sidebar":
            st = self._stats()
            warns = [{"raw": "You do not have enough Strength"}] if st["Str"] < st["ReqStr"] else []
            return {"warnings": warns}
        if method == "get_build_info":
            return {"level": 90, "className": "Witch", "ascendClassName": "Elementalist", "points": {"used": 100}}
        if method == "get_build_header":
            return {}
        if method == "item_edit_begin":
            self._edit_copy = copy.deepcopy(self.mod_lines)
            return {"id": 9, "raw": self.raw_item, "modLines": copy.deepcopy(self._edit_copy)}
        if method == "item_edit_set":
            assert self._edit_copy is not None
            idx = int(p["modLine"]["index"])
            for ml in self._edit_copy:
                if ml["index"] == idx:
                    ml["disabled"] = not bool(p["modLine"]["enabled"])
            return {"id": 9, "raw": self.raw_item, "modLines": copy.deepcopy(self._edit_copy)}
        if method == "item_edit_commit":
            assert self._edit_copy is not None
            self.mod_lines = copy.deepcopy(self._edit_copy)
            self._edit_copy = None
            return {"ok": True, "id": 9}
        if method == "item_edit_cancel":
            self._edit_copy = None
            return {"ok": True}
        return {}


def test_pairwise_synergy_detects_multiplicative_and_joint_req_and_res_collapse() -> None:
    worker = _FakeSynergyWorker()
    res = ablate_item_mods(worker, "Ring 1", synergy=True, synergy_top=6)  # type: ignore[arg-type]
    assert res["restoreCheck"]["verified"] is True
    assert res["synergyTop"] == 6
    pairs = res["pairwiseSynergies"]
    assert len(pairs) == 15  # C(6, 2)

    by_pair = {(p["lineIndexA"], p["lineIndexB"]): p for p in pairs}
    # Pair (1, 2): removing 1.25x & 1.40x separately gives -20% and -28.571% (sum -48.571%),
    # removing both gives -42.857%, so synergy = +5.714% (>= 0.5%)
    p12 = by_pair[(1, 2)]
    assert p12["nonTrivialSynergy"] is True
    assert abs(p12["synergyDpsPct"]) >= 5.0

    # Pair (3, 4): neither +20 Str alone bricks ReqStr 150, removing both drops Str to 135 < 150!
    p34 = by_pair[(3, 4)]
    assert p34["jointReqCollapse"] is True
    assert any("Str" in r for r in p34["jointCollapseReasons"])

    # Pair (5, 6): neither +25% ColdRes alone uncapped ColdRes 75%, removing both drops to 55% < 75%!
    p56 = by_pair[(5, 6)]
    assert p56["jointReqCollapse"] is True
    assert any("ColdRes" in r for r in p56["jointCollapseReasons"])

    md = format_item_ablation_markdown(res)
    assert "Pairwise Synergy" in md
    assert "[JOINT-REQ-COLLAPSE" in md
    assert "[SYNERGY]" in md
    json.dumps(res, allow_nan=False)


def test_cli_item_inspect_synergy_flags_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    bad = runner.invoke(cli_mod.main, ["item", "inspect", "Ring 1", "--synergy"])
    assert bad.exit_code != 0
    assert "--synergy requires --ablate" in bad.output

    captured: dict[str, Any] = {}

    def _fake_daemon(action: str, params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        captured["action"] = action
        captured["params"] = params
        return {
            "slotName": "Ring 1",
            "itemName": "Synergy Band",
            "raw": "Rarity: RARE\nSynergy Band\nTwo-Stone Ring\nImplicits: 0\n+50 to maximum Life\n",
            "primaryDpsKey": "CombinedDPS",
            "baseline": {"primaryDpsKey": "CombinedDPS", "primaryDps": 100000, "totalEhp": 50000},
            "ablation_deltas": [],
            "synergyTop": 4,
            "pairwiseSynergies": [],
            "restoreCheck": {"verified": True},
        }

    monkeypatch.setattr("pob_cli.cli_item_inspect.invoke_daemon", _fake_daemon)
    ok = runner.invoke(cli_mod.main, ["item", "inspect", "Ring 1", "--ablate", "--synergy", "--synergy-top", "4", "--json"])
    assert ok.exit_code == 0, ok.output
    assert captured["action"] == "item_ablate"
    assert captured["params"]["synergy"] is True
    assert captured["params"]["synergy_top"] == 4
    payload = json.loads(ok.output)
    assert payload["ablation"]["synergyTop"] == 4
    assert payload["ablation"]["pairwiseSynergies"] == []


def test_shared_poe1_and_poe2_regression_manifest_schema_validation() -> None:
    valid_manifest = {
        "schema_version": SCHEMA_VERSION,
        "cases": [
            {
                "id": "poe1-fireball-red-mod-and-synergy",
                "game": "poe1",
                "title": "Detect unparsed customMods red line and Spell vs Attack flag mismatch",
                "build_source": {"kind": "fixture", "value": "fixtures/poe1_fireball_red_mod.xml"},
                "prompt": "Run pob interactions and diagnose to report uncomputed red mods and dead attack mods.",
                "ground_truth_checks": [
                    {"type": "contains", "pattern": "UNSUPPORTED_BY_POB"},
                    {"type": "contains", "pattern": "FLAG_MISMATCH"},
                    {"type": "numeric_range", "path": "unsupportedMods.totalCount", "min": 2, "max": 10},
                    {"type": "command_exit", "code": 0},
                ],
                "forbidden_patterns": ["Spirit", "Uncut Skill Gem", "Waystone"],
                "tags": ["poe1", "interactions", "red-mods"],
            },
            {
                "id": "poe2-invoker-spirit-reservation-audit",
                "game": "poe2",
                "title": "Verify PoE2 Spirit reservation and Monk Ascendancy interactions",
                "build_source": {"kind": "fixture", "value": "fixtures/poe2_invoker_spirit.xml"},
                "prompt": "Inspect Spirit reservation budget and active meta-gem triggers.",
                "ground_truth_checks": [
                    {"type": "regex", "pattern": r"Spirit.*\d+"},
                    {"type": "json_path", "path": "game", "expected": "poe2"},
                ],
                "forbidden_patterns": ["Cluster Jewel", "Crafted Bench Veiled Orb", "Elder Influence"],
                "tags": ["poe2", "spirit", "regression"],
            },
        ],
    }
    assert validate_regression_manifest(valid_manifest) == []

    # Invalid cases must fail deterministically
    bad_manifest = copy.deepcopy(valid_manifest)
    bad_manifest["cases"][0]["game"] = "poe3"
    bad_manifest["cases"][0]["build_source"] = {"kind": "fixture", "value": "/" + "Users" + "/example/secret.xml"}
    bad_manifest["cases"].append(copy.deepcopy(bad_manifest["cases"][1]))  # duplicate ID
    bad_manifest["cases"][1]["ground_truth_checks"] = []
    errs = validate_regression_manifest(bad_manifest)
    assert any("game must be one of" in e for e in errs)
    assert any("machine-local" in e for e in errs)
    assert any("duplicate case id" in e for e in errs)
    assert any("ground_truth_checks" in e for e in errs)


_REAL_SYNERGY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="90" targetVersion="3_0" className="Witch" ascendClassName="Elementalist" mainSocketGroup="1" viewMode="SKILLS"/>
  <Tree activeSpec="1"><Spec title="Default" treeVersion="3_30" classId="3" ascendClassId="2" nodes="54447"/></Tree>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill mainActiveSkill="1" enabled="true" label="Main">
        <Gem nameSpec="Fireball" level="20" quality="20" enabled="true"/>
        <Gem nameSpec="Determination" level="1" quality="0" enabled="true"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Real Synergy Band
Heavy Belt
Item Level: 84
Implicits: 1
+30 to Strength
50% increased Cast Speed
120% increased Spell Damage
+30 to Strength
+35% to Fire Resistance
    </Item>
    <ItemSet id="1">
      <Slot name="Belt" itemId="1"/>
    </ItemSet>
  </Items>
  <Config activeConfigSet="1"><ConfigSet id="1"/></Config>
</PathOfBuilding>
"""


def test_real_engine_pairwise_synergy_zero_residue(tmp_path: Path) -> None:
    engine = find_runnable_engine()
    if engine is None:
        pytest.skip("Real PoB engine not available")

    worker = BridgeWorker(pob_dir=engine, runtime_dir=tmp_path / "rt", slot_name="test_synergy_l3")
    try:
        worker.start()
        if not (worker.caps or {}).get("itemModLineToggle"):
            pytest.skip("Engine bridge does not advertise itemModLineToggle")

        load_build(worker, xml_code=xml_to_share_code(_REAL_SYNERGY_XML))
        res = ablate_item_mods(worker, "Belt", synergy=True, synergy_top=5)
        assert res["restoreCheck"]["verified"] is True
        pairs = res["pairwiseSynergies"]
        assert len(pairs) == 10  # C(5, 2)

        # Cast Speed x Spell Damage is strictly multiplicative -> non-trivial synergy!
        cs_sp = next(
            p
            for p in pairs
            if ("Cast Speed" in p["modTextA"] and "Spell Damage" in p["modTextB"])
            or ("Spell Damage" in p["modTextA"] and "Cast Speed" in p["modTextB"])
        )
        assert cs_sp["nonTrivialSynergy"] is True
        assert abs(cs_sp["synergyDpsPct"]) >= 0.5

        md = format_item_ablation_markdown(res)
        assert "Pairwise Synergy" in md
        json.dumps(res, allow_nan=False)
    finally:
        worker.stop()


def test_l3_defensive_rpc_validation_timeout_help_brief_cand_cap_and_schema_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pob_cli.daemon.rpc_handlers import _h_interactions_inspect, _h_item_ablate
    from pob_cli.errors import DaemonRequestError
    from pob_cli.item_ablate.synergy import _detect_joint_resist_cap_collapse

    class _DummyMgr:
        def _attach_warning_if_degraded(self, payload: dict[str, Any]) -> dict[str, Any]:
            return payload

    # 1. _h_item_ablate rejects bool / non-int synergy_top with DaemonRequestError
    for bad_top in (True, False, "not-an-int", [5]):
        with pytest.raises(DaemonRequestError):
            _h_item_ablate(_DummyMgr(), {"slot": "Ring 1", "synergy": True, "synergy_top": bad_top}, object())  # type: ignore[arg-type]

    # 2. _h_interactions_inspect accepts single string mods and rejects non-str/list/None
    seen_mods: list[Any] = []
    monkeypatch.setattr(
        "pob_cli.analyzer.interactions.inspect_interactions",
        lambda _w, *, slot_name=None, mod_lines=None, unsupported_only=False: seen_mods.append(mod_lines) or {"mode": "targeted"},
    )
    _h_interactions_inspect(_DummyMgr(), {"mods": "20% increased Spell Damage"}, object())  # type: ignore[arg-type]
    assert seen_mods == [["20% increased Spell Damage"]]
    for bad_mods in (123, {"a": 1}, True):
        with pytest.raises(DaemonRequestError):
            _h_interactions_inspect(_DummyMgr(), {"mods": bad_mods}, object())  # type: ignore[arg-type]

    # 3. --ablate --synergy scales daemon timeout and `pob help --brief` exposes --synergy-top
    timeouts: list[float] = []

    def _fake_daemon(action: str, params: dict[str, Any], *, timeout: float = 30.0, **_kw: Any) -> dict[str, Any]:
        timeouts.append(timeout)
        return {
            "slotName": "Ring 1",
            "itemName": "Band",
            "raw": "Rarity: RARE\nBand\nCoral Ring\nImplicits: 0\n+50 to maximum Life\n",
            "primaryDpsKey": "CombinedDPS",
            "baseline": {"primaryDpsKey": "CombinedDPS", "primaryDps": 100000, "totalEhp": 50000},
            "ablation_deltas": [],
            "synergyTop": params.get("synergy_top", 5),
            "pairwiseSynergies": [],
            "restoreCheck": {"verified": True},
        }

    monkeypatch.setattr("pob_cli.cli_item_inspect.invoke_daemon", _fake_daemon)
    runner = CliRunner()
    assert runner.invoke(cli_mod.main, ["item", "inspect", "Ring 1", "--ablate", "--synergy", "--synergy-top", "5", "--json"]).exit_code == 0
    assert runner.invoke(cli_mod.main, ["item", "inspect", "Ring 1", "--ablate", "--synergy", "--synergy-top", "8", "--json"]).exit_code == 0
    assert timeouts[0] == 150.0
    assert timeouts[1] == 202.0

    brief = runner.invoke(cli_mod.main, ["help", "--brief"])
    assert brief.exit_code == 0, brief.output
    assert "--synergy-top" in brief.output

    # 4. cand_cap respected when removing mods also lowers max resist cap (78 -> 75, actual 75 -> no false collapse)
    base_st = {"FireResist": 78.0, "FireResistMax": 78.0, "ColdResist": 75.0, "ColdResistMax": 75.0, "LightningResist": 75.0, "LightningResistMax": 75.0}
    cand_st = {**base_st, "FireResist": 75.0, "FireResistMax": 75.0}
    assert _detect_joint_resist_cap_collapse(base_st, cand_st, {"deltaResists": {"fire": 0.0}}, {"deltaResists": {"fire": 0.0}}) == []
    cand_uncapped = {**base_st, "FireResist": 70.0, "FireResistMax": 75.0}
    reasons = _detect_joint_resist_cap_collapse(base_st, cand_uncapped, {"deltaResists": {"fire": 0.0}}, {"deltaResists": {"fire": 0.0}})
    assert reasons == ["FireRes 70% < 75% cap"]

    # 5. regression_bank_schema rejects bool/non-numeric/non-finite bounds without raising ValueError
    bad_num_manifest = {
        "schema_version": SCHEMA_VERSION,
        "cases": [
            {
                "id": "poe1-bad-bounds",
                "game": "poe1",
                "title": "Bad numeric bounds",
                "build_source": {"kind": "fixture", "value": "fixtures/demo.xml"},
                "prompt": "Check bounds.",
                "ground_truth_checks": [
                    {"type": "command_exit", "code": True},
                    {"type": "numeric_range", "path": "a", "min": True, "max": 10},
                    {"type": "numeric_range", "path": "b", "min": "abc", "max": float("nan")},
                ],
                "forbidden_patterns": [],
                "tags": ["poe1"],
            }
        ],
    }
    errs = validate_regression_manifest(bad_num_manifest)
    assert any("command_exit" in e for e in errs)
    assert any("not boolean" in e for e in errs)
    assert any("must be a finite number" in e for e in errs)
    assert any("must be finite" in e for e in errs)
