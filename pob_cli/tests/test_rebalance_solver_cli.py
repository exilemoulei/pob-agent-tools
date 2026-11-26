"""Offline & real-engine tests for Layer 4: `pob rebalance` CoW-verified solver, ranking, Tier 5 trade fallback, and CLI."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from engine_paths import find_runnable_engine

import pob_cli.cli_rebalance as cli_reb_mod
from pob_cli.cli import main as cli_main
from pob_cli.daemon.rpc_mutate import MUTATE_ACTION_HANDLERS
from pob_cli.errors import DaemonRequestError
from pob_cli.formatters.rebalance_md import format_rebalance_markdown
from pob_cli.rebalance import solve_rebalance
from pob_cli.rebalance.steps_catalyst_tattoo import parse_catalyst_header
from pob_cli.rebalance.steps_craft_harvest import parse_stat_contributions

REAL_ENGINE = find_runnable_engine()
HAS_REAL_POB = REAL_ENGINE is not None
EXAMPLE_CODE_PATH = Path(__file__).resolve().parents[2] / "examples" / "builds" / "dark_bargain_ci_skeleton_330_clean.code.txt"


class _FakeRebalanceWorker:
    def __init__(self, items_by_slot: dict[str, str], *, base_fire: float = 50.0, base_cold: float = 120.0, base_dex: float = 125.0, req_dex: float = 140.0) -> None:
        self.lock = threading.RLock()
        self.caps = {"tattoos": True}
        self._base_fire = base_fire
        self._base_cold = base_cold
        self._base_dex = base_dex
        self._req_dex = req_dex
        self._slot_raw: dict[str, str] = dict(items_by_slot)
        self._overrides: dict[str, dict[str, Any]] = {}
        self._nodes_db: dict[str, Any] = {
            "1": {"id": 1, "name": "Marauder", "type": "ClassStart", "linked": [10]},
            "10": {"id": 10, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [1]},
            "11": {"id": 11, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [10]},
        }
        self._snaps: dict[str, tuple[dict[str, str], dict[str, dict[str, Any]]]] = {}
        self._seq = 0

    def _compute_stats(self) -> dict[str, Any]:
        tot: dict[str, float] = {"Str": 160.0, "Dex": self._base_dex, "Int": 140.0, "Fire": self._base_fire, "Cold": self._base_cold, "Lightning": 95.0, "Chaos": 0.0}
        for raw in self._slot_raw.values():
            contrib = parse_stat_contributions(raw, use_min_roll=False)
            cat, qual = parse_catalyst_header(raw)
            mult_attr = 1.0 + (qual / 100.0 if (cat or "").lower() == "intrinsic" else 0.0)
            mult_res = 1.0 + (qual / 100.0 if (cat or "").lower() == "prismatic" else 0.0)
            for k, v in contrib.items():
                if k in ("Str", "Dex", "Int"):
                    tot[k] = tot.get(k, 0.0) + int(v * mult_attr)
                elif k in ("Fire", "Cold", "Lightning", "Chaos"):
                    tot[k] = tot.get(k, 0.0) + int(v * mult_res)
        for ov in self._overrides.values():
            if ov.get("name") == "Tattoo of the Ngamahu Firewalker":
                tot["Str"] -= 10.0
                tot["Fire"] += 6.0
        fire_eff = min(75.0, tot["Fire"])
        cold_eff = min(75.0, tot["Cold"])
        light_eff = min(75.0, tot["Lightning"])
        return {
            "CombinedDPS": 1_000_000.0,
            "FullDPS": 1_000_000.0,
            "TotalDPS": 1_000_000.0,
            "TotalEHP": 50_000.0 + fire_eff * 100.0,
            "Life": 4000.0,
            "Str": tot["Str"],
            "ReqStr": 100.0,
            "Dex": tot["Dex"],
            "ReqDex": self._req_dex,
            "Int": tot["Int"],
            "ReqInt": 100.0,
            "FireResist": fire_eff,
            "FireResistMax": 75.0,
            "FireResistOverCap": max(0.0, tot["Fire"] - 75.0),
            "ColdResist": cold_eff,
            "ColdResistMax": 75.0,
            "ColdResistOverCap": max(0.0, tot["Cold"] - 75.0),
            "LightningResist": light_eff,
            "LightningResistMax": 75.0,
            "LightningResistOverCap": max(0.0, tot["Lightning"] - 75.0),
            "ChaosResist": tot["Chaos"],
            "ChaosResistMax": 75.0,
        }

    def get_cached_tree_data(self, tree_version: str | None = None) -> dict[str, Any]:
        return {"treeVersion": tree_version or "3_30", "nodes": self._nodes_db}

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        p = params or {}
        if method == "get_stats":
            return {"stats": self._compute_stats()}
        if method == "get_build_info":
            return {"className": "Marauder", "ascendClassName": "Juggernaut", "level": 95, "points": {"used": 3, "usedMax": 123}}
        if method == "get_warnings":
            return {"warnings": []}
        if method == "list_skills":
            return {"groups": [], "mainSocketGroup": 1}
        if method == "list_items":
            items = []
            slots = []
            for idx, (sn, raw) in enumerate(sorted(self._slot_raw.items()), start=1):
                lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                title = lines[1] if len(lines) > 1 else sn
                items.append({"id": idx, "name": title, "title": title})
                slots.append({"name": sn, "selItemId": idx, "inactive": False})
            return {"items": items, "slots": slots}
        if method == "item_raw":
            iid = int(p["id"])
            for idx, (_, raw) in enumerate(sorted(self._slot_raw.items()), start=1):
                if idx == iid:
                    return {"raw": raw}
            return {"raw": ""}
        if method == "add_item":
            sn = str(p.get("slotName") or "Belt")
            self._slot_raw[sn] = str(p["raw"])
            lines = [ln.strip() for ln in str(p["raw"]).splitlines() if ln.strip()]
            return {"item": {"title": lines[1] if len(lines) > 1 else sn, "primarySlot": sn}}
        if method == "get_tree_state":
            return {
                "treeVersion": "3_30",
                "className": "Marauder",
                "ascendClassName": "Juggernaut",
                "allocatedNodes": [1, 10, 11],
                "overrides": copy.deepcopy(self._overrides),
                "dynamicNodes": [],
            }
        if method == "tattoo_apply":
            sid = str(int(p["id"]))
            if p.get("reset"):
                self._overrides.pop(sid, None)
            else:
                self._overrides[sid] = {"why": "tattoo", "name": str(p["tattoo"])}
            return {"ok": True}
        if method == "export_code":
            self._seq += 1
            tok = f"SNAP_{self._seq}"
            self._snaps[tok] = (copy.deepcopy(self._slot_raw), copy.deepcopy(self._overrides))
            return {"code": tok}
        if method == "import_code":
            tok = str(p.get("code") or "")
            if tok in self._snaps:
                self._slot_raw, self._overrides = copy.deepcopy(self._snaps[tok])
            return {"ok": True}
        return {"ok": True}

    def call_batch(self, calls: list[tuple[str, dict[str, Any]]], *, timeout: float = 30.0, raise_on_error: bool = True) -> list[dict[str, Any]]:
        _ = (timeout, raise_on_error)
        return [self.call(m, p) for m, p in calls]


def test_rebalance_solver_ranks_bench_before_harvest_try_item_and_apply_plan() -> None:
    ring1 = "\n".join(
        [
            "Rarity: RARE",
            "Gale Loop",
            "Coral Ring",
            "Implicits: 1",
            "+25 to maximum Life",
            "+43% to Cold Resistance",
            "{crafted}+14 to Intelligence",
        ]
    )
    boots = "\n".join(
        [
            "Rarity: RARE",
            "Dusk Greaves",
            "Titan Greaves",
            "Implicits: 0",
            "+70 to maximum Life",
            "30% increased Movement Speed",
        ]
    )
    belt_rare = "\n".join(
        [
            "Rarity: RARE",
            "Viper Cord",
            "Leather Belt",
            "Implicits: 1",
            "+35 to maximum Life",
            "+30% to Fire Resistance",
            "+25 to Dexterity",
        ]
    )
    unique_belt = "\n".join(
        [
            "Rarity: UNIQUE",
            "Mageblood",
            "Heavy Belt",
            "Implicits: 1",
            "+30 to Strength",
            "+40 to Dexterity",
        ]
    )

    # 1) Already balanced build -> already_balanced
    w_ok = _FakeRebalanceWorker({"Ring 1": ring1, "Boots": boots, "Belt": belt_rare}, base_fire=55.0, base_dex=130.0)
    r_ok = solve_rebalance(w_ok)  # type: ignore[arg-type]
    assert r_ok["status"] == "already_balanced" and r_ok["verifiedPlans"] == []

    # 2) --try-item equipping Unique Belt drops -30% Fire Res (55 < 75 -> 20% Fire gap), solver fixes via Bench/Harvest with 0 residue!
    before_raw = copy.deepcopy(w_ok._slot_raw)
    r_try = solve_rebalance(w_ok, try_item_raw=unique_belt, slot_name="Belt", max_steps=2)  # type: ignore[arg-type]
    assert r_try["status"] == "solved"
    assert r_try["applied"] is False
    assert r_try["tryItem"]["slot"] == "Belt"
    assert len(r_try["verifiedPlans"]) >= 1
    # Ranked cheapest first: Tier 1 Bench recraft (~0-4c) comes before Tier 2 Harvest (~8c)
    assert r_try["verifiedPlans"][0]["steps"][0]["tier"] == 1
    assert r_try["verifiedPlans"][0]["estCostChaos"] <= 5.0
    # Zero residue rollback: original Belt and Ring 1 intact
    assert w_ok._slot_raw == before_raw

    # 3) --apply-plan 1 persists both the tried Belt AND the winning rebalance step!
    r_apply = solve_rebalance(w_ok, try_item_raw=unique_belt, slot_name="Belt", max_steps=2, apply_plan=1)  # type: ignore[arg-type]
    assert r_apply["applied"] is True and r_apply["appliedPlan"]["planIndex"] == 1
    assert "Mageblood" in w_ok._slot_raw["Belt"]
    assert w_ok._compute_stats()["FireResist"] >= 75.0


def test_rebalance_tier5_trade_fallback_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unsolvable huge Fire + Dex deficit on corrupted/full gear -> Tier 5 trade fallback emitted!
    corrupted_ring = "\n".join(
        [
            "Rarity: RARE",
            "Brick Band",
            "Coral Ring",
            "Implicits: 1",
            "+20 to maximum Life",
            "+30% to Lightning Resistance",
            "Corrupted",
        ]
    )
    w_hard = _FakeRebalanceWorker({"Ring 1": corrupted_ring}, base_fire=-20.0, base_cold=75.0, base_dex=80.0, req_dex=140.0)
    w_hard.caps = {"tattoos": False}
    res = solve_rebalance(w_hard, max_steps=2)  # type: ignore[arg-type]
    assert res["status"] == "trade_needed"
    assert res["verifiedPlans"] == []
    fb = res["tradeFallback"]
    assert fb is not None and fb["slot"] == "Ring 1"
    assert 'pob trade search --slot "Ring 1"' in fb["command"]
    assert '--stat "Fire Resistance=' in fb["command"] and '--stat "Dexterity=' in fb["command"]

    # Out-of-range --apply-plan fails cleanly with DaemonRequestError
    with pytest.raises(DaemonRequestError, match="out of range"):
        solve_rebalance(w_hard, apply_plan=1)  # type: ignore[arg-type]

    # CLI `pob rebalance` Markdown, --json, and --slot without --try-item usage guard
    monkeypatch.setattr(
        cli_reb_mod,
        "invoke_daemon",
        lambda action, params=None, **_kw: MUTATE_ACTION_HANDLERS[action](None, params or {}, w_hard),  # type: ignore[arg-type]
    )
    runner = CliRunner()
    r_bad = runner.invoke(cli_main, ["rebalance", "--slot", "Belt"])
    assert r_bad.exit_code == 2 and "requires `--try-item`" in r_bad.output
    r_md = runner.invoke(cli_main, ["rebalance"])
    assert r_md.exit_code == 0 and "Tier 5 Trade Fallback" in r_md.output and "pob trade search" in r_md.output
    r_js = runner.invoke(cli_main, ["rebalance", "--json"])
    assert r_js.exit_code == 0 and json.loads(r_js.output)["status"] == "trade_needed"


def test_rebalance_bom_stripping_continuous_plan_index_and_error_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1) Windows UTF-8 BOM stripping (Eval P1-3) on both file (`@item.txt`) and inline raw text
    ring1 = "\n".join(
        [
            "Rarity: RARE",
            "Gale Loop",
            "Coral Ring",
            "Implicits: 1",
            "+25 to maximum Life",
            "+43% to Cold Resistance",
            "{crafted}+14 to Intelligence",
        ]
    )
    bom_belt = "\ufeff" + "\n".join(
        [
            "Rarity: UNIQUE",
            "Headhunter",
            "Leather Belt",
            "Implicits: 1",
            "+35 to maximum Life",
            "+50 to Strength",
        ]
    )
    old_belt = "Rarity: RARE\nPlain Belt\nLeather Belt\nImplicits: 1\n+25 to maximum Life"
    w_bom = _FakeRebalanceWorker({"Ring 1": ring1, "Belt": old_belt}, base_fire=55.0, base_dex=150.0)
    r_direct = solve_rebalance(w_bom, try_item_raw=bom_belt, slot_name="Belt", max_steps=1)  # type: ignore[arg-type]
    assert r_direct["tryItem"]["name"] == "Headhunter"
    assert "\ufeff" not in format_rebalance_markdown(r_direct)

    bom_file = tmp_path / "hh_bom.txt"
    bom_file.write_bytes(bom_belt.encode("utf-8"))
    monkeypatch.setattr(
        cli_reb_mod,
        "invoke_daemon",
        lambda action, params=None, **_kw: MUTATE_ACTION_HANDLERS[action](None, params or {}, w_bom),  # type: ignore[arg-type]
    )
    runner = CliRunner()
    r_cli_bom = runner.invoke(cli_main, ["rebalance", "--try-item", f"@{bom_file}", "--slot", "Belt"])
    assert r_cli_bom.exit_code == 0
    assert "`Headhunter`" in r_cli_bom.output and "\ufeff" not in r_cli_bom.output

    # 2) already_balanced with --apply-plan returns clear message and applied=False
    w_bal = _FakeRebalanceWorker({"Ring 1": ring1}, base_fire=80.0, base_dex=150.0)
    r_bal = solve_rebalance(w_bal, apply_plan=1)  # type: ignore[arg-type]
    assert r_bal["status"] == "already_balanced" and r_bal["applied"] is False
    assert "not applied" in r_bal.get("message", "")
    assert "not applied" in format_rebalance_markdown(r_bal)

    # 3) Continuous planIndex across verifiedPlans (#1..#V) and partialPlans (#V+1..#V+P) + --apply-plan on partial
    #    Ring 1 bench recraft fixes 20% Fire (verified), while a single Tattoo only fixes 6% (partial).
    w_mix = _FakeRebalanceWorker({"Ring 1": ring1}, base_fire=55.0, base_dex=150.0)
    r_mix = solve_rebalance(w_mix, max_steps=1)  # type: ignore[arg-type]
    assert r_mix["verifiedPlans"] and r_mix["partialPlans"]
    v_len = len(r_mix["verifiedPlans"])
    assert [p["planIndex"] for p in r_mix["verifiedPlans"]] == list(range(1, v_len + 1))
    assert [p["planIndex"] for p in r_mix["partialPlans"]] == list(range(v_len + 1, v_len + 1 + len(r_mix["partialPlans"])))
    md_mix = format_rebalance_markdown(r_mix)
    assert f"#{v_len + 1}" in md_mix and "combos verified" in md_mix

    # Applying the partial plan index (#v_len + 1) succeeds and applies that exact partial plan
    partial_idx = v_len + 1
    r_ap_part = solve_rebalance(w_mix, max_steps=1, apply_plan=partial_idx)  # type: ignore[arg-type]
    assert r_ap_part["applied"] is True and r_ap_part["appliedPlan"]["planIndex"] == partial_idx

    # 4) Non-silent evalErrors accounting when a combo evaluation raises
    class _FailingTattooWorker(_FakeRebalanceWorker):
        def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == "tattoo_apply":
                raise RuntimeError("simulated tattoo rpc failure")
            return super().call(method, params)

    w_err = _FailingTattooWorker({"Ring 1": ring1}, base_fire=55.0, base_dex=150.0)
    r_err = solve_rebalance(w_err, max_steps=1)  # type: ignore[arg-type]
    assert r_err["evalErrors"] >= 1
    assert r_err["evaluatedCombos"] >= 1
    assert isinstance(r_err["candidateCapApplied"], bool)
    assert "eval error(s)" in format_rebalance_markdown(r_err)


def test_rebalance_delta_dps_ehp_markdown_and_none_safety() -> None:
    from pob_cli.rebalance.trade_fallback import compute_stat_delta

    # Non-zero deltaDps / deltaEhp render with BOTH absolute delta and (+X.X%) in Markdown + tier clarity header
    md = format_rebalance_markdown(
        {
            "status": "solved",
            "applied": False,
            "deficits": {"needs": {"Fire": 20.0}},
            "candidateCounts": {"bench": 2, "harvest": 0, "catalyst": 0, "tattoo": 0},
            "evaluatedCombos": 2,
            "elapsedMs": 14,
            "evalBudgetReached": False,
            "verifiedPlans": [
                {
                    "planIndex": 1,
                    "estCostChaos": 1.0,
                    "deltaDps": 12500.0,
                    "deltaDpsPct": 5.0,
                    "deltaEhp": 3200.0,
                    "deltaEhpPct": 4.2,
                    "steps": [{"title": "Bench Craft Ring 1: +20% to Fire Resistance"}],
                }
            ],
            "partialPlans": [
                {
                    "planIndex": 2,
                    "estCostChaos": 8.0,
                    "deltaDps": -2500.0,
                    "deltaDpsPct": -1.0,
                    "remainingDeficits": {"Fire": 14.0},
                    "steps": [{"title": "Tattoo Node 10"}],
                }
            ],
        }
    )
    assert "Tier 1-4 = 0-buy Bench/Harvest/Catalyst/Tattoo; Tier 5 = single-slot trade replacement" in md
    assert "`2` combos verified in `14ms`" in md
    assert "+12,500" in md and "(+5.0%)" in md
    assert "+3,200" in md and "(+4.2%)" in md
    assert "-2,500" in md and "(-1.0%)" in md

    # Unavailable / non-finite stats yield (None, None), NOT 0.0
    assert compute_stat_delta({"CombinedDPS": 100.0}, {}, "CombinedDPS") == (None, None)
    assert compute_stat_delta({"CombinedDPS": "nan"}, {"CombinedDPS": 100.0}, "CombinedDPS") == (None, None)
    assert compute_stat_delta({"CombinedDPS": 250000.0}, {"CombinedDPS": 262500.0}, "CombinedDPS") == (12500.0, 5.0)


def test_rebalance_suppression_partial_single_state_fallback_and_eval_budget() -> None:
    from pob_cli.rebalance.deficits import RebalanceStep, extract_build_deficits
    from pob_cli.rebalance.solver import _select_candidate_combos
    from pob_cli.rebalance.trade_fallback import build_trade_fallback

    # 1) --include-suppression partial plan qualifies even when hard_gap_total() == 0
    d_supp = extract_build_deficits(
        {
            "Str": 150.0,
            "ReqStr": 100.0,
            "Dex": 150.0,
            "ReqDex": 100.0,
            "Int": 150.0,
            "ReqInt": 100.0,
            "FireResist": 75.0,
            "ColdResist": 75.0,
            "LightningResist": 75.0,
            "ChaosResist": 15.0,
            "SpellSuppressionChance": 86.0,
        },
        include_chaos=True,
        chaos_target=0.0,
        include_suppression=True,
    )
    assert d_supp.hard_gap_total() == 0.0 and d_supp.need_map() == {"SpellSuppression": 14.0}
    step_part = RebalanceStep(
        tier=4,
        kind="tattoo",
        slot="Node 20",
        target_key="node:20",
        title="Tattoo Node 20: +2% Suppress",
        est_cost_chaos=22.0,
        cost_label="~22c",
        covers={"SpellSuppression": 2.0, "Dex": -10.0},
        apply_spec={"kind": "tattoo", "node": 20, "tattoo": "Tattoo of the Ramako Shaman"},
    )
    combos, _ = _select_candidate_combos([step_part], d_supp, max_steps=1)
    assert len(combos) == 1

    # 2) Single-state Tier 5 trade fallback preserves load-bearing Chaos & SpellSuppression on the replaced ring
    ring_supp_chaos = "\n".join(
        [
            "Rarity: RARE",
            "Warden Band",
            "Amethyst Ring",
            "Implicits: 1",
            "+20% to Chaos Resistance",
            "+12% chance to Suppress Spell Damage",
            "+30% to Fire Resistance",
        ]
    )
    fb = build_trade_fallback([{"slotName": "Ring 1", "raw": ring_supp_chaos}], d_supp)
    assert fb is not None
    # Chaos: current=15, target=0 -> headroom=15, item has 20 -> load_bearing=5
    assert fb["loadBearingStats"]["Chaos"] == 5
    # Suppression: current=86 (<100) -> surplus=0, item has 12 -> load_bearing=12; target=12+14=26
    assert fb["loadBearingStats"]["SpellSuppression"] == 12
    assert fb["targetStats"]["SpellSuppression"] == 26
    assert '--stat "Chance to Suppress Spell Damage=26"' in fb["command"]
    assert '--stat "Chaos Resistance=5"' in fb["command"]

    # 3) max_evals budget & non-zero deltaEhp measured end-to-end by solve_rebalance
    ring1 = "\n".join(
        [
            "Rarity: RARE",
            "Gale Loop",
            "Coral Ring",
            "Implicits: 1",
            "+25 to maximum Life",
            "+43% to Cold Resistance",
            "{crafted}+14 to Intelligence",
        ]
    )
    w = _FakeRebalanceWorker({"Ring 1": ring1}, base_fire=55.0, base_dex=150.0)
    res = solve_rebalance(w, max_steps=1, max_evals=1)  # type: ignore[arg-type]
    assert res["evaluatedCombos"] == 1
    assert res["evalBudgetReached"] is True
    assert isinstance(res["elapsedMs"], int) and res["elapsedMs"] >= 0
    assert res["verifiedPlans"] and res["verifiedPlans"][0]["deltaEhp"] > 0.0
    assert res["verifiedPlans"][0]["deltaEhpPct"] > 0.0


@pytest.mark.skipif(not HAS_REAL_POB, reason="No runnable real PoB engine found")
def test_real_pob_rebalance_tier3_catalyst_and_tier4_tattoo_e2e(tmp_path: Path) -> None:
    from collections import deque

    from pob_cli.bridge_client import BridgeWorker
    from pob_cli.mutators import change_tree, create_new_build

    assert REAL_ENGINE is not None
    w = BridgeWorker("rebalance-cat-tat-e2e", pob_dir=REAL_ENGINE, runtime_dir=tmp_path)
    w.start()
    try:
        # Part A: Tier 3 Prismatic Catalyst verified plan on real PoB 2.69.0 (plain untagged Unique ring, 0 small nodes)
        create_new_build(w, class_name="Marauder", level=90, apply=True)
        corrupted_chest = "\n".join(
            [
                "Rarity: RARE",
                "Iron Shell",
                "Plate Vest",
                "Implicits: 0",
                "+90% to Fire Resistance",
                "+135% to Cold Resistance",
                "+135% to Lightning Resistance",
                "+40 to Strength",
                "+20 to Dexterity",
                "+20 to Intelligence",
                "Corrupted",
            ]
        )
        unique_fire_ring = "\n".join(
            [
                "Rarity: UNIQUE",
                "Pyre",
                "Sapphire Ring",
                "Implicits: 1",
                "+30% to Cold Resistance",
                "+40% to Fire Resistance",
            ]
        )
        w.call("add_item", {"raw": corrupted_chest, "slotName": "Body Armour", "equip": True})
        w.call("add_item", {"raw": unique_fire_ring, "slotName": "Ring 1", "equip": True})
        # Fire is 90 + 40 - 60 = 70% (short by 5%); Prismatic Catalyst 20% on Ring 1 adds +8% -> 75%!
        code_pre_cat = w.call("export_code", {})["code"]
        res_cat = solve_rebalance(w, max_steps=1)
        assert res_cat["candidateCounts"]["catalyst"] >= 1
        assert res_cat["status"] == "solved", res_cat
        assert res_cat["verifiedPlans"][0]["steps"][0]["tier"] == 3
        assert res_cat["verifiedPlans"][0]["steps"][0]["kind"] == "catalyst"
        assert w.call("export_code", {})["code"] == code_pre_cat

        # Part B: Tier 4 Tattoo E2E (nested CoW 0-residue dry-run + --apply-plan persistence)
        # Replace Ring 1 with a corrupted ring so Bench/Harvest/Catalyst are ineligible; allocate a Small Strength node.
        corrupted_ring = "\n".join(
            [
                "Rarity: RARE",
                "Fixed Band",
                "Iron Ring",
                "Implicits: 0",
                "+40% to Fire Resistance",
                "Corrupted",
            ]
        )
        w.call("add_item", {"raw": corrupted_ring, "slotName": "Ring 1", "equip": True})
        ts0 = w.call("get_tree_state", {})
        ndb = w.get_cached_tree_data(ts0.get("treeVersion")).get("nodes", {})
        # BFS from allocated start nodes to find the closest Small Strength (+10 Str) node
        q: deque[int] = deque(int(x) for x in (ts0.get("allocatedNodes") or ()))
        seen: set[int] = set(q)
        small_str_nid: int | None = None
        while q:
            cur = q.popleft()
            nd = ndb.get(str(cur)) or {}
            if nd.get("type") == "Normal" and nd.get("name") == "Strength":
                small_str_nid = cur
                break
            for nxt in nd.get("linked") or ():
                ni = int(nxt)
                if ni not in seen:
                    seen.add(ni)
                    q.append(ni)
        assert small_str_nid is not None
        change_tree(w, allocate=[str(small_str_nid)], apply=True)

        ov_before = copy.deepcopy(w.call("get_tree_state", {}).get("overrides") or {})
        code_before = w.call("export_code", {})["code"]

        # Dry-run: produces verified Tier 4 Tattoo plan and leaves 0 residue in overrides & export_code
        res_tat = solve_rebalance(w, max_steps=1)
        assert res_tat["candidateCounts"]["tattoo"] >= 1, res_tat
        assert res_tat["status"] == "solved", res_tat
        assert res_tat["verifiedPlans"][0]["steps"][0]["tier"] == 4
        assert res_tat["verifiedPlans"][0]["steps"][0]["kind"] == "tattoo"
        assert (w.call("get_tree_state", {}).get("overrides") or {}) == ov_before
        assert w.call("export_code", {})["code"] == code_before

        # --apply-plan 1 persists the tattoo override in real PoB and caps Fire Resistance
        res_tat_ap = solve_rebalance(w, max_steps=1, apply_plan=1)
        assert res_tat_ap["applied"] is True
        ov_after = w.call("get_tree_state", {}).get("overrides") or {}
        assert str(small_str_nid) in ov_after
        assert float(w.call("get_stats", {})["stats"]["FireResist"]) >= 75.0
    finally:
        w.stop()


@pytest.mark.skipif(not HAS_REAL_POB, reason="No runnable real PoB engine found")
def test_real_pob_rebalance_end_to_end_zero_residue(tmp_path: Path) -> None:
    from pob_cli.bridge_client import BridgeWorker
    from pob_cli.mutators import create_new_build

    assert REAL_ENGINE is not None
    w = BridgeWorker("rebalance-e2e", pob_dir=REAL_ENGINE, runtime_dir=tmp_path)
    w.start()
    try:
        # Fresh Lv90 Ranger (no Supreme Ostentation) with real item Dex requirement and uncapped Fire Resist
        create_new_build(w, class_name="Ranger", level=90, apply=True)
        ring1 = "\n".join(
            [
                "Rarity: RARE",
                "Test Loop",
                "Sapphire Ring",
                "Implicits: 1",
                "+30% to Cold Resistance",
                "+60 to maximum Life",
                "+44% to Cold Resistance",
                "+42% to Lightning Resistance",
                "{crafted}+12 to Intelligence",
            ]
        )
        ring2 = "\n".join(
            [
                "Rarity: RARE",
                "Storm Coil",
                "Two-Stone Ring",
                "Implicits: 1",
                "+16% to Cold and Lightning Resistances",
                "+45% to Cold Resistance",
                "+45% to Lightning Resistance",
                "+40% to Fire Resistance",
                "+45 to Dexterity",
            ]
        )
        belt = "\n".join(
            [
                "Rarity: RARE",
                "Viper Cord",
                "Leather Belt",
                "Implicits: 1",
                "+35 to maximum Life",
                "+45% to Fire Resistance",
                "+45% to Cold Resistance",
                "+45% to Lightning Resistance",
                "+30 to Dexterity",
            ]
        )
        boots = "\n".join(
            [
                "Rarity: RARE",
                "Wind Stride",
                "Slink Boots",
                "Implicits: 0",
                "+70 to maximum Life",
                "30% increased Movement Speed",
                "+25% to Fire Resistance",
            ]
        )
        for sn, raw in (("Ring 1", ring1), ("Ring 2", ring2), ("Belt", belt), ("Boots", boots)):
            w.call("add_item", {"raw": raw, "slotName": sn, "equip": True})

        # Verify real PoB reports Fire Resist < 75% (cap) and Dex < ReqDex (120 for Slink Boots)
        broken_st = w.call("get_stats", {})["stats"]
        assert float(broken_st["FireResist"]) < float(broken_st.get("FireResistMax") or 75.0)
        assert float(broken_st["Dex"]) < float(broken_st["ReqDex"])
        code_before = w.call("export_code", {})["code"]

        res = solve_rebalance(w, max_steps=2)
        assert res["status"] == "solved", res
        assert len(res["verifiedPlans"]) >= 1
        top = res["verifiedPlans"][0]
        assert top["fullyResolved"] is True and top["remainingHardGap"] == 0.0
        assert res["restoreCheck"] is not None and res["restoreCheck"].get("verified") is True
        # Zero state residue verified against real PoB export_code
        assert w.call("export_code", {})["code"] == code_before
    finally:
        w.stop()
