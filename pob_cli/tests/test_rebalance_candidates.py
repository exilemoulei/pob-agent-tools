"""Offline unit tests for Layer 3: rebalance deficits, Bench recraft, Harvest swap guards, Catalyst, and Tattoo candidates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from engine_paths import find_runnable_engine

from pob_cli import item_inspect
from pob_cli.rebalance import (
    apply_catalyst_to_raw,
    enumerate_bench_recraft_steps,
    enumerate_catalyst_steps,
    enumerate_harvest_swap_steps,
    enumerate_tattoo_steps,
    extract_build_deficits,
    harvest_tier_min_roll,
    parse_catalyst_header,
)

REAL_ENGINE = find_runnable_engine()
HAS_REAL_POB = REAL_ENGINE is not None


def _sample_stats(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "Str": 160.0,
        "ReqStr": 100.0,
        "Dex": 120.0,
        "ReqDex": 145.0,
        "Int": 140.0,
        "ReqInt": 110.0,
        "FireResist": 50.0,
        "FireResistMax": 75.0,
        "FireResistOverCap": 0.0,
        "ColdResist": 75.0,
        "ColdResistMax": 75.0,
        "ColdResistOverCap": 42.0,
        "LightningResist": 75.0,
        "LightningResistMax": 75.0,
        "LightningResistOverCap": 5.0,
        "ChaosResist": -20.0,
        "ChaosResistMax": 75.0,
        "Life": 4200.0,
        "Mana": 900.0,
        "ManaUnreserved": 120.0,
        "SpellSuppressionChance": 88.0,
    }
    base.update(overrides)
    return base


def test_extract_build_deficits_respects_doryani_ci_and_optional_probes() -> None:
    d = extract_build_deficits(_sample_stats(), include_suppression=True)
    assert d.has_hard_deficits is True
    assert d.hard_attr_deficits() == {"Dex": 25.0}
    assert d.hard_resist_deficits() == {"Fire": 25.0}
    assert d.resist_slack("Cold") == 42.0
    assert d.attr_surplus("Str") == 60.0
    assert d.need_map()["SpellSuppression"] == 12.0

    # Doryani's Prototype exempts negative Lightning Resist; CI exempts Chaos Resist
    items_doryani = {
        "items": [{"id": 1, "name": "Doryani's Prototype", "title": "Doryani's Prototype"}],
        "slots": [{"name": "Body Armour", "selItemId": 1}],
    }
    d_dor = extract_build_deficits(
        _sample_stats(
            Dex=150.0,
            FireResist=75.0,
            LightningResist=-180.0,
            Life=1.0,
            ChaosResist=100.0,
        ),
        items_res=items_doryani,
        include_chaos=True,
    )
    assert d_dor.has_doryani is True and d_dor.is_chaos_immune is True
    assert d_dor.has_hard_deficits is False


def test_bench_recraft_and_harvest_swap_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    d = extract_build_deficits(_sample_stats())
    monkeypatch.setattr(
        item_inspect,
        "inspect_item",
        lambda *_a, **_kw: {"counts": {"prefix": 1, "suffix": 1}, "capacity": {"prefix": 3, "suffix": 3}, "unmatched": []},
    )
    rare_ring = "\n".join(
        [
            "Rarity: RARE",
            "Gale Loop",
            "Two-Stone Ring",
            "Implicits: 1",
            "+14% to Cold and Lightning Resistances",
            "+62 to maximum Life",
            "+43% to Cold Resistance",
            "{crafted}+18 to Intelligence",
        ]
    )
    corrupted_ring = rare_ring + "\nCorrupted"
    fractured_boots = "\n".join(
        [
            "Rarity: RARE",
            "Dusk Greaves",
            "Titan Greaves",
            "Implicits: 0",
            "+75 to maximum Life",
            "{fractured}+44% to Cold Resistance",
            "30% increased Movement Speed",
        ]
    )
    unique_belt = "\n".join(
        [
            "Rarity: UNIQUE",
            "Headhunter",
            "Leather Belt",
            "Implicits: 1",
            "+35 to maximum Life",
            "+52 to Strength",
            "+50 to Dexterity",
        ]
    )
    equipped = [
        {"slotName": "Ring 1", "raw": rare_ring},
        {"slotName": "Ring 2", "raw": corrupted_ring},
        {"slotName": "Boots", "raw": fractured_boots},
        {"slotName": "Belt", "raw": unique_belt},
    ]

    # Tier 1 Bench recraft: Ring 1 (replaceable {crafted}) and Boots (open affix), NEVER corrupted Ring 2 or unique Belt
    bench_steps = enumerate_bench_recraft_steps(equipped, d, allow_engine=False)
    assert bench_steps
    slots_touched = {s.slot for s in bench_steps}
    assert "Ring 1" in slots_touched and "Boots" in slots_touched
    assert "Ring 2" not in slots_touched and "Belt" not in slots_touched
    assert any("Fire" in s.covers or "Dex" in s.covers for s in bench_steps)

    # Tier 2 Harvest swap: only Ring 1 explicit Cold (+43%, T2 -> floor +42%) -> Fire; NEVER corrupted/fractured/unique
    assert harvest_tier_min_roll(43) == 42 and harvest_tier_min_roll(47) == 46 and harvest_tier_min_roll(35) == 30
    harvest_steps = enumerate_harvest_swap_steps(equipped, d)
    assert len(harvest_steps) == 1
    hs = harvest_steps[0]
    assert hs.slot == "Ring 1"
    assert hs.covers["Fire"] == 42.0 and hs.covers["Cold"] == -43.0
    assert hs.apply_spec["tierFloor"] == 42 and "+42% to Fire Resistance" in hs.apply_spec["replaceMods"][0][1]


def test_catalyst_eligibility_and_tattoo_surplus_guards() -> None:
    d = extract_build_deficits(_sample_stats())
    amulet = "\n".join(
        [
            "Rarity: UNIQUE",
            "Astramentis",
            "Onyx Amulet",
            "Implicits: 1",
            "+16 to all Attributes",
            "+95 to all Attributes",
        ]
    )
    ring_res = "\n".join(
        [
            "Rarity: RARE",
            "Storm Band",
            "Ruby Ring",
            "Catalyst: Prismatic",
            "CatalystQuality: 10",
            "Implicits: 1",
            "+30% to Fire Resistance",
            "+40% to Fire Resistance",
        ]
    )
    ring_corrupted = ring_res + "\nCorrupted"
    ring_already_20 = apply_catalyst_to_raw(ring_res, "Prismatic", 20)
    assert parse_catalyst_header(ring_already_20) == ("Prismatic", 20)

    cat_steps = enumerate_catalyst_steps(
        [
            {"slotName": "Amulet", "raw": amulet},
            {"slotName": "Ring 1", "raw": ring_res},
            {"slotName": "Ring 2", "raw": ring_corrupted},
            {"slotName": "Belt", "raw": ring_already_20},
        ],
        d,
    )
    assert any(s.slot == "Amulet" and s.apply_spec["catalyst"] == "Intrinsic" and s.covers["Dex"] >= 20 for s in cat_steps)
    assert any(s.slot == "Ring 1" and s.apply_spec["catalyst"] == "Prismatic" and s.covers["Fire"] > 0 for s in cat_steps)
    assert not any(s.slot in ("Ring 2", "Belt") for s in cat_steps)

    # Tier 4 Tattoos: Str has +60 surplus -> eligible for Fire Res tattoo; Dex is in deficit -> MUST NOT sacrifice Dex!
    nodes_db = {
        "1": {"id": 1, "name": "Marauder", "type": "ClassStart", "linked": [10]},
        "10": {"id": 10, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [1]},
        "11": {"id": 11, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [10]},
        "20": {"id": 20, "name": "Dexterity", "type": "Normal", "stats": ["+10 to Dexterity"], "linked": [1]},
        "30": {"id": 30, "name": "Intelligence", "type": "Normal", "stats": ["+10 to Intelligence"], "linked": [1]},
    }
    ts = {
        "allocatedNodes": [1, 10, 11, 20, 30],
        "overrides": {"11": {"why": "tattoo", "name": "Tattoo of the Ngamahu Firewalker"}},
        "dynamicNodes": [],
    }
    d_supp = extract_build_deficits(_sample_stats(), include_suppression=True)
    tat_steps = enumerate_tattoo_steps(ts, nodes_db, d_supp, tattoo_catalog={})
    assert len(tat_steps) == 1
    assert tat_steps[0].apply_spec == {"kind": "tattoo", "node": 10, "tattoo": "Tattoo of the Ngamahu Firewalker"}
    assert tat_steps[0].covers["Fire"] == 6.0 and tat_steps[0].covers["Str"] == -10.0
    # Node 11 is already tattooed, Node 20 has Dex deficit so cannot sacrifice Dex for suppression tattoo!
    assert all(s.apply_spec["node"] != 11 and s.apply_spec["node"] != 20 for s in tat_steps)


def test_bench_recraft_blocks_4th_suffix_on_crafted_prefix_and_offline_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    d = extract_build_deficits(_sample_stats())
    # Rare ring with 1 natural prefix, 3 natural suffixes, and a {crafted} Life PREFIX -> removing crafted prefix frees ONLY a prefix!
    full_suffix_ring = "\n".join(
        [
            "Rarity: RARE",
            "Brimstone Knot",
            "Coral Ring",
            "Implicits: 1",
            "+25 to maximum Life",
            "+20 to maximum Energy Shield",
            "+42% to Cold Resistance",
            "+41% to Lightning Resistance",
            "+32 to Strength",
            "{crafted}+50 to maximum Life",
        ]
    )
    uncrafted_full_suffix_boots = "\n".join(
        [
            "Rarity: RARE",
            "Gale Tread",
            "Titan Greaves",
            "Implicits: 0",
            "+75 to maximum Life",
            "+40% to Cold Resistance",
            "+40% to Lightning Resistance",
            "+30 to Strength",
        ]
    )

    # 1) With item_inspect reporting 1 prefix, 3 suffixes after removing {crafted} Life prefix -> NO suffix crafts allowed!
    def _fake_inspect(raw: str, **_kw: Any) -> dict[str, Any]:
        assert "{crafted}" not in raw
        return {"counts": {"prefix": 1, "suffix": 3}, "capacity": {"prefix": 3, "suffix": 3}, "unmatched": []}

    monkeypatch.setattr(item_inspect, "inspect_item", _fake_inspect)
    steps_inspected = enumerate_bench_recraft_steps([{"slotName": "Ring 1", "raw": full_suffix_ring}], d, allow_engine=False)
    assert steps_inspected == []

    # 2) Offline fallback (item_inspect raises): non-crafted item returns NO steps (never assumes (True, True)),
    #    and crafted Life prefix ring STILL refuses suffix resistance/attribute crafts!
    def _boom(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise FileNotFoundError("offline")

    monkeypatch.setattr(item_inspect, "inspect_item", _boom)
    steps_offline = enumerate_bench_recraft_steps(
        [
            {"slotName": "Ring 1", "raw": full_suffix_ring},
            {"slotName": "Boots", "raw": uncrafted_full_suffix_boots},
        ],
        d,
        allow_engine=False,
    )
    assert steps_offline == []


def test_catalyst_tag_annotation_preserves_prefixes_and_reverse_loss_math() -> None:
    raw_ring = "\n".join(
        [
            "Rarity: RARE",
            "Sol Loop",
            "Amber Amulet",
            "Implicits: 1",
            "+25 to Strength",
            "+60 to maximum Life",
            "{crafted}+20 to Dexterity",
            "{tags:elemental,fire,resistance}+40% to Fire Resistance",
            "{fractured}+25% to Cold Resistance",
            "Catalyst: Prismatic",
            "CatalystQuality: 20",
        ]
    )
    # Intrinsic annotates untagged Str/Dex (preserving {crafted}), preserves existing {tags:...}, ignores Life
    out_int = apply_catalyst_to_raw(raw_ring, "Intrinsic", 20)
    assert "{tags:jewellery_attribute}+25 to Strength" in out_int
    assert "{tags:jewellery_attribute}{crafted}+20 to Dexterity" in out_int
    assert "\n+60 to maximum Life\n" in out_int
    assert "{tags:elemental,fire,resistance}+40% to Fire Resistance" in out_int

    # Prismatic annotates untagged fractured Cold Res (preserving {fractured}), preserves existing {tags:...}
    out_pri = apply_catalyst_to_raw(raw_ring, "Prismatic", 20)
    assert "{tags:jewellery_resistance}{fractured}+25% to Cold Resistance" in out_pri
    assert "{tags:elemental,fire,resistance}+40% to Fire Resistance" in out_pri

    # Base-roll Catalyst math: stripping 20% Prismatic on base +40% Fire, +25% Cold -> -8 Fire, -5 Cold
    d = extract_build_deficits(_sample_stats())
    steps = enumerate_catalyst_steps([{"slotName": "Amulet", "raw": raw_ring}], d)
    int_step = next(s for s in steps if s.apply_spec["catalyst"] == "Intrinsic")
    assert int_step.covers["Fire"] == -8.0
    assert int_step.covers["Cold"] == -5.0

    # Upgrading 10% -> 20% Prismatic on base +40% Fire -> floor(40*1.2) - floor(40*1.1) = 48 - 44 = +4
    ring_10 = "\n".join(
        [
            "Rarity: RARE",
            "Ember Loop",
            "Coral Ring",
            "Catalyst: Prismatic",
            "CatalystQuality: 10",
            "Implicits: 0",
            "+40% to Fire Resistance",
        ]
    )
    steps_10 = enumerate_catalyst_steps([{"slotName": "Ring 1", "raw": ring_10}], d)
    pri_step = next(s for s in steps_10 if s.apply_spec["catalyst"] == "Prismatic")
    assert pri_step.covers["Fire"] == 4.0


def test_uncapped_resist_below_neg200_and_range_roll_resolution() -> None:
    from pob_cli.rebalance.steps_craft_harvest import parse_stat_contributions

    # Uncapped FireResistTotal=-240 with clamped display FireResist=-200 -> full 315 deficit (cap 75)
    d = extract_build_deficits(
        _sample_stats(FireResist=-200.0, FireResistTotal=-240.0, FireResistMax=75.0, FireResistOverCap=0.0)
    )
    assert d.resistances["Fire"]["cap"] == 75.0
    assert d.resistances["Fire"]["deficit"] == 315.0

    # {range:0.5}+(42-45)% resolves to 42 + floor(3*0.5 + 0.5) = 44; missing {range} falls back to lo=42
    assert parse_stat_contributions("{range:0.5}+(42-45)% to Cold Resistance", use_min_roll=False)["Cold"] == 44.0
    assert parse_stat_contributions("+(42-45)% to Cold Resistance", use_min_roll=False)["Cold"] == 42.0


def test_craft_ilvl_implicit_chaos_headroom_and_harvest_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    from pob_cli.rebalance.steps_craft_harvest import _craft_reduces_net_hole

    # 1) Chaos headroom in _craft_reduces_net_hole: cur=10, target=0 -> headroom=10; losing 25 Chaos creates 15 hole
    d_ch = extract_build_deficits(_sample_stats(ChaosResist=10.0), include_chaos=True, chaos_target=0.0)
    ok, _ = _craft_reduces_net_hole({"Fire": 12.0, "Chaos": -25.0}, d_ch, d_ch.need_map())
    assert ok is False  # new_hole=15 >= helpful=12

    # 2) Ruby Ring implicit (+30% Fire Res) must NOT block Fire Res suffix craft; low ilvl filters high-level crafts
    monkeypatch.setattr(
        item_inspect,
        "inspect_item",
        lambda *_a, **_kw: {"counts": {"prefix": 1, "suffix": 1}, "capacity": {"prefix": 3, "suffix": 3}, "unmatched": []},
    )
    ruby_ring = "\n".join(
        [
            "Rarity: RARE",
            "Ruby Test",
            "Ruby Ring",
            "Item Level: 84",
            "Implicits: 1",
            "+30% to Fire Resistance",
            "+50 to maximum Life",
        ]
    )
    low_ilvl_ring = ruby_ring.replace("Item Level: 84", "Item Level: 1")
    d_fire = extract_build_deficits(_sample_stats(Dex=160.0))
    steps_hi = enumerate_bench_recraft_steps([{"slotName": "Ring 1", "raw": ruby_ring}], d_fire, allow_engine=False)
    assert any("Fire" in s.covers for s in steps_hi)
    steps_lo = enumerate_bench_recraft_steps([{"slotName": "Ring 1", "raw": low_ilvl_ring}], d_fire, allow_engine=False)
    assert len(steps_lo) < len(steps_hi)

    # 3) Harvest: crafted Fire Res locks Fire; donor hole >= closed gap rejected
    crafted_fire_ring = "\n".join(
        [
            "Rarity: RARE",
            "Lock Band",
            "Coral Ring",
            "Implicits: 0",
            "+43% to Cold Resistance",
            "{crafted}+20% to Fire Resistance",
        ]
    )
    assert enumerate_harvest_swap_steps([{"slotName": "Ring 1", "raw": crafted_fire_ring}], d_fire) == []

    # Donor Cold +43% with only 15% slack -> donor_gap_created = 28 >= dst_need (25) -> rejected
    d_tight_cold = extract_build_deficits(_sample_stats(Dex=160.0, ColdResistOverCap=15.0))
    plain_cold_ring = "\n".join(
        ["Rarity: RARE", "Tight Band", "Coral Ring", "Implicits: 0", "+43% to Cold Resistance"]
    )
    assert enumerate_harvest_swap_steps([{"slotName": "Ring 1", "raw": plain_cold_ring}], d_tight_cold) == []

    # 4) Non-empty tattoo catalog with only legacy entries returns [] (does NOT fall back to _FALLBACK_TATTOOS)
    nodes_db = {
        "1": {"id": 1, "name": "Marauder", "type": "ClassStart", "linked": [10]},
        "10": {"id": 10, "name": "Strength", "type": "Normal", "stats": ["+10 to Strength"], "linked": [1]},
    }
    ts = {"allocatedNodes": [1, 10], "overrides": {}, "dynamicNodes": []}
    legacy_only_cat = {
        "old": {
            "name": "Legacy Str Fire",
            "kind": "tattoo",
            "target_type": "Small Strength",
            "stats": ["+6% to Fire Resistance"],
            "legacy": True,
        }
    }
    assert enumerate_tattoo_steps(ts, nodes_db, d_fire, tattoo_catalog=legacy_only_cat) == []


@pytest.mark.skipif(not HAS_REAL_POB, reason="No runnable real PoB engine found")
def test_real_pob_catalyst_scaling_intrinsic_and_prismatic(tmp_path: Path) -> None:
    from pob_cli.bridge_client import BridgeWorker
    from pob_cli.mutators import create_new_build

    assert REAL_ENGINE is not None
    w = BridgeWorker("catalyst-scale-e2e", pob_dir=REAL_ENGINE, runtime_dir=tmp_path)
    w.start()
    try:
        create_new_build(w, class_name="Marauder", level=90, apply=True)
        plain_ring = "\n".join(
            [
                "Rarity: RARE",
                "Cat Test Ring",
                "Iron Ring",
                "Implicits: 0",
                "+45 to Strength",
                "+40% to Fire Resistance",
            ]
        )
        w.call("add_item", {"raw": plain_ring, "slotName": "Ring 1", "equip": True})
        base_st = w.call("get_stats", {})["stats"]
        base_str = float(base_st["Str"])
        base_fire = float(base_st["FireResist"]) + float(base_st.get("FireResistOverCap") or 0.0)

        # Intrinsic +20% on plain untagged +45 Str -> PoB Str increases by +9
        int_ring = apply_catalyst_to_raw(plain_ring, "Intrinsic", 20)
        w.call("add_item", {"raw": int_ring, "slotName": "Ring 1", "equip": True})
        int_st = w.call("get_stats", {})["stats"]
        assert float(int_st["Str"]) - base_str == pytest.approx(9.0, abs=0.01)

        # Prismatic +20% on plain untagged +40% Fire Res -> PoB Fire Res increases by +8%
        pri_ring = apply_catalyst_to_raw(plain_ring, "Prismatic", 20)
        w.call("add_item", {"raw": pri_ring, "slotName": "Ring 1", "equip": True})
        pri_st = w.call("get_stats", {})["stats"]
        pri_fire = float(pri_st["FireResist"]) + float(pri_st.get("FireResistOverCap") or 0.0)
        assert pri_fire - base_fire == pytest.approx(8.0, abs=0.01)
    finally:
        w.stop()
