"""Unit tests for Layer 2: level-aware passive caps, weapon-swap leveling gems, and AG ItemSet."""

from __future__ import annotations

from typing import Any

from pob_cli.analyzer.dewater_auto import infer_auto_keep_and_flask_notes
from pob_cli.analyzer.pitfalls_ag import extract_ag_on_hit_mod_lines
from pob_cli.analyzer.pitfalls_core import _detect_build_mechanics_and_pitfalls
from pob_cli.analyzer.static_checks import collect_passive_point_issues, compute_level_passive_cap


def test_level_aware_passive_point_cap_and_issues() -> None:
    assert compute_level_passive_cap(97, 123) == 120
    assert compute_level_passive_cap(100, 123) == 123
    assert compute_level_passive_cap(90, 123) == 113
    assert compute_level_passive_cap(50, 123) == 123  # below 70 endgame band

    # Lv.97 with 120/123 -> 0 issues (no false positive!)
    assert collect_passive_point_issues({"level": 97, "points": {"used": 120, "usedMax": 123}}, include_unallocated=True) == []

    # Lv.97 with 118/123 -> 2 remaining for Lv.97
    under = collect_passive_point_issues({"level": 97, "points": {"used": 118, "usedMax": 123}}, include_unallocated=True)
    assert len(under) == 1
    assert under[0]["severity"] == "LOW"
    assert "Lv.97: 118/120 (2 remaining)" in under[0]["message"]

    # Lv.90 with 120/123 -> over-allocated for Lv.90 (requires Lv.97)
    over_lv = collect_passive_point_issues({"level": 90, "points": {"used": 120, "usedMax": 123}}, include_unallocated=True)
    assert len(over_lv) == 1
    assert over_lv[0]["severity"] == "MEDIUM"
    assert "Lv.90: 120/113" in over_lv[0]["message"]
    assert "Lv.97" in over_lv[0]["message"]

    # Exceeding hard Lv.100 cap -> HIGH
    over_abs = collect_passive_point_issues({"level": 100, "points": {"used": 125, "usedMax": 123}}, include_unallocated=False)
    assert len(over_abs) == 1
    assert over_abs[0]["severity"] == "HIGH"


def test_weapon_swap_leveling_gems_never_high_and_non_swap_stays_high() -> None:
    # Main group full 6-link + Weapon 1 Swap with 3x duplicate Invert the Rules + Awakened Enhance -> silent skip
    skills_leveling = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {"name": "Spark of the Nova", "enabled": True, "support": False, "level": 20},
                    {"name": "Pinpoint", "enabled": True, "support": True, "level": 21},
                    {"name": "Invert the Rules", "enabled": True, "support": True, "level": 4},
                    {"name": "Lightning Penetration", "enabled": True, "support": True, "level": 21},
                    {"name": "Greater Spell Echo", "enabled": True, "support": True, "level": 3},
                    {"name": "Overloaded Intensity", "enabled": True, "support": True, "level": 4},
                ],
            },
            {
                "index": 5,
                "slot": "Weapon 1 Swap",
                "enabled": True,
                "gems": [
                    {"name": "Invert the Rules", "enabled": True, "support": True, "level": 2},
                    {"name": "Invert the Rules", "enabled": True, "support": True, "level": 2},
                    {"name": "Invert the Rules", "enabled": True, "support": True, "level": 2},
                    {"name": "Awakened Enhance", "enabled": True, "support": True, "level": 3},
                ],
            },
        ],
    }
    gaps, mech_lv = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30},
        skills_res=skills_leveling,
        items_res={"items": [], "slots": []},
        cfg_res={"customMods": []},
    )
    assert not any("Weapon 1 Swap" in g["message"] for g in gaps)
    assert mech_lv["swapLevelingGems"]["count"] == 4
    assert mech_lv["swapLevelingGems"]["groups"][0]["slot"] == "Weapon 1 Swap"

    # Offhand Weapon 2 holding only leveling supports (Empower/Enlighten) with full 6L Body Armour -> no HIGH
    skills_offhand_lv = {
        "mainSocketGroup": 1,
        "groups": [
            skills_leveling["groups"][0],
            {
                "index": 6,
                "slot": "Weapon 2",
                "enabled": True,
                "gems": [
                    {"name": "Empower", "enabled": True, "support": True, "level": 2},
                    {"name": "Enlighten", "enabled": True, "support": True, "level": 2},
                ],
            },
        ],
    }
    gaps_oh, mech_oh = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30},
        skills_res=skills_offhand_lv,
        items_res={"items": [], "slots": []},
        cfg_res={"customMods": []},
    )
    assert not any("Weapon 2" in g["message"] for g in gaps_oh)
    assert mech_oh["swapLevelingGems"]["count"] == 2

    # Non-Swap slot with only a support gem still emits HIGH
    skills_non_swap = {
        "mainSocketGroup": 1,
        "groups": [
            skills_leveling["groups"][0],
            {
                "index": 2,
                "slot": "Boots",
                "enabled": True,
                "gems": [{"name": "Lightning Penetration", "enabled": True, "support": True, "level": 20}],
            },
        ],
    }
    gaps_ns, _ = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30},
        skills_res=skills_non_swap,
        items_res={"items": [], "slots": []},
        cfg_res={"customMods": []},
    )
    boot_gaps = [g for g in gaps_ns if "Boots" in g["message"]]
    assert len(boot_gaps) == 1
    assert boot_gaps[0]["severity"] == "HIGH"


def _sample_ag_minion_itemsets(*, calcs_set: int = 2) -> dict[str, Any]:
    return {
        "ok": True,
        "activeItemSetId": 1,
        "bindings": [
            {
                "group": 3,
                "gemIndex": 4,
                "name": "Animate Guardian",
                "skillId": "AnimateArmour",
                "enabled": True,
                "slot": "Weapon 1",
                "skillMinionItemSet": 2,
                "skillMinionItemSetCalcs": calcs_set,
                "supportNames": ["Elemental Army", "Minion Life", "Empower", "Meat Shield", "Companionship"],
            }
        ],
        "minionItemSets": {
            "2": {
                "id": 2,
                "title": "Animate Guardian",
                "isActiveSet": False,
                "slots": [
                    {
                        "slotName": "Weapon 1",
                        "itemId": 38,
                        "title": "Kingmaker",
                        "raw": "Rarity: UNIQUE\nKingmaker\nnearby Allies have +50% to Critical Strike Multiplier",
                    },
                    {
                        "slotName": "Body Armour",
                        "itemId": 34,
                        "title": "Garb of the Ephemeral",
                        "raw": "Rarity: UNIQUE\nGarb of the Ephemeral\nnearby Enemies cannot deal Critical Strikes",
                    },
                    {
                        "slotName": "Gloves",
                        "itemId": 35,
                        "title": "Maelstrom Touch",
                        "raw": (
                            "Rarity: RARE\nMaelstrom Touch\nWyvernscale Gauntlets\n"
                            "Inflict Lightning Exposure on Hit, applying -11% to Lightning Resistance\n"
                            "Nearby Enemies are Intimidated"
                        ),
                    },
                ],
            }
        },
    }


def test_ag_itemset_exposure_inversion_conflict_dependencies_mismatch_and_autokeep() -> None:
    ag_sets = _sample_ag_minion_itemsets(calcs_set=1)
    skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [
                    {"name": "Spark of the Nova", "enabled": True, "support": False, "level": 20},
                    {"name": "Invert the Rules", "skillId": "SupportInvertTheRules", "enabled": True, "support": True, "level": 4},
                ],
            }
        ],
    }
    gaps, mech = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30},
        skills_res=skills,
        items_res={"items": [], "slots": []},
        cfg_res={"customMods": []},
        minion_itemsets=ag_sets,
    )
    inv_gaps = [g for g in gaps if "Resistance Inversion anti-synergy" in g["message"]]
    assert len(inv_gaps) == 1
    assert inv_gaps[0]["severity"] == "HIGH"
    assert "Maelstrom Touch" in inv_gaps[0]["message"]
    assert "Lightning Exposure" in inv_gaps[0]["message"]
    assert mech["resistanceInversion"]["conflictingAgExposure"]

    # AG dependencies + ItemSet mismatch warning
    assert any("ItemSet mismatch" in g["message"] for g in gaps)
    assert any("Kingmaker" in g["message"] and "Garb of the Ephemeral" in g["message"] and "Companionship" in g["message"] for g in gaps)
    assert mech["animateGuardian"]["itemSetMismatch"] is True

    # AG on-hit debuffs feed into Layer 1 auto-keep (both explicit and worker-derived P2-3)
    ag_lines = extract_ag_on_hit_mod_lines(ag_sets)
    ak, _ = infer_auto_keep_and_flask_notes(
        stats={"Speed": 6.0, "HitChance": 100.0},
        skills_res={"groups": []},
        items_res={"items": [], "slots": []},
        extra_mod_lines=ag_lines,
    )
    assert "conditionenemyintimidated" in ak

    class _FakeAgWorker:
        def __init__(self) -> None:
            self.caps = {"minionItemSets": True}

        def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
            if action == "get_minion_itemsets":
                sets = _sample_ag_minion_itemsets()
                sets["minionItemSets"]["2"]["slots"][2]["raw"] += "\n100% chance to Unnerve Enemies for 4 seconds on Hit"
                return sets
            return {}

    ak_from_worker, _ = infer_auto_keep_and_flask_notes(
        stats={"Speed": 6.0, "HitChance": 100.0},
        skills_res={"groups": []},
        items_res={"items": [], "slots": []},
        worker=_FakeAgWorker(),
    )
    assert "conditionenemyunnerved" in ak_from_worker
    assert "conditionenemyintimidated" in ak_from_worker


def test_cr8_cr9_cr10_cr11_ag_isolation_and_uul_netols_vow_exemption() -> None:
    # CR-8: non-AG minion ItemSet bindings must not leak into extract_ag_on_hit_mod_lines
    non_ag = {
        "bindings": [
            {"name": "Raise Spectre", "skillId": "RaiseSpectre", "enabled": True, "slot": "Helmet", "skillMinionItemSet": 3}
        ],
        "minionItemSets": {
            "3": {"slots": [{"slotName": "Gloves", "raw": "Nearby Enemies are Unnerved"}]}
        },
    }
    assert extract_ag_on_hit_mod_lines(non_ag) == []

    # CR-9 & CR-10: AG low-chance on-hit debuff uses AG hit rate (~1.8/s), NOT player 10/s,
    # and AG Charge-on-Hit NEVER grants player useEnduranceCharges!
    ag_gear_lines = [
        "10% chance to Unnerve Enemies for 4 seconds on Hit",
        "25% chance to gain an Endurance Charge on Hit",
        "Intimidate Enemies for 4 seconds on Hit",
    ]
    ak_ag, _ = infer_auto_keep_and_flask_notes(
        stats={"Speed": 10.0, "HitChance": 100.0, "CritChance": 95.0},
        skills_res={"groups": []},
        items_res={"items": [], "slots": []},
        extra_mod_lines=ag_gear_lines,
    )
    assert "conditionenemyunnerved" not in ak_ag
    assert "useendurancecharges" not in ak_ag
    assert "conditionenemyintimidated" in ak_ag

    # CR-11: Uul-Netol's Vow Amulet 7th-link support gem is exempt from orphan HIGH warning
    skills_amulet = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [{"name": "Spark", "enabled": True, "support": False, "level": 20}],
            },
            {
                "index": 2,
                "slot": "Amulet",
                "enabled": True,
                "gems": [{"name": "Lightning Penetration", "enabled": True, "support": True, "level": 20}],
            },
        ],
    }
    vow_items = {
        "items": [{"id": 9, "title": "Uul-Netol's Vow", "name": "Unset Amulet"}],
        "slots": [{"name": "Amulet", "selItemId": 9, "inactive": False}],
    }
    gaps_vow, _ = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30}, skills_res=skills_amulet, items_res=vow_items, cfg_res={"customMods": []}
    )
    assert not any("Amulet" in g["message"] for g in gaps_vow)

    plain_items = {
        "items": [{"id": 9, "title": "Marble Amulet", "name": "Marble Amulet"}],
        "slots": [{"name": "Amulet", "selItemId": 9, "inactive": False}],
    }
    gaps_plain, _ = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30}, skills_res=skills_amulet, items_res=plain_items, cfg_res={"customMods": []}
    )
    assert any("Amulet" in g["message"] and g["severity"] == "HIGH" for g in gaps_plain)
