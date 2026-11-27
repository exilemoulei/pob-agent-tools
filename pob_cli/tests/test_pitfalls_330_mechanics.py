"""Unit tests for Layer 3: 3.30 Pact skills, Overloaded Intensity ramp, and negative-water checks."""

from __future__ import annotations

from pob_cli.analyzer.pitfalls_core import _detect_build_mechanics_and_pitfalls


def test_pact_automation_high_and_unholy_accomplice_affliction_note() -> None:
    skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [{"name": "Spark of the Nova", "enabled": True, "support": False, "level": 20}],
            },
            {
                "index": 2,
                "slot": "Helmet",
                "enabled": True,
                "gems": [
                    {"name": "Automation", "enabled": True, "support": False, "level": 20},
                    {"name": "Pact of Beidat", "enabled": True, "support": False, "level": 21},
                ],
            },
        ],
    }
    gaps, mech = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30},
        skills_res=skills,
        items_res={"items": [], "slots": []},
        cfg_res={"sections": [{"items": [{"var": "pactMode", "value": "MAX"}]}], "customMods": []},
    )
    auto_gaps = [g for g in gaps if "Automation" in g["message"] and "Pact of Beidat" in g["message"]]
    assert len(auto_gaps) == 1
    assert auto_gaps[0]["severity"] == "HIGH"
    cav_gaps = [g for g in gaps if "Affliction" in g["message"] and "Unholy Accomplice" in g["message"]]
    assert len(cav_gaps) == 1
    assert cav_gaps[0]["severity"] == "MEDIUM"
    assert mech["pactSkills"][0]["linkedWithAutomation"] is True

    # With Unholy Accomplice equipped and no Automation -> no HIGH, no Affliction warning
    skills_no_auto = {
        **skills,
        "groups": [
            skills["groups"][0],
            {
                "index": 2,
                "slot": "Helmet",
                "enabled": True,
                "gems": [{"name": "Pact of Beidat", "enabled": True, "support": False, "level": 21}],
            },
        ],
    }
    items_ua = {
        "items": [{"id": 1, "title": "Unholy Accomplice", "name": "Unholy Accomplice", "baseName": "Heavy Belt"}],
        "slots": [{"name": "Belt", "selItemId": 1, "inactive": False}],
    }
    gaps_ua, _ = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30},
        skills_res=skills_no_auto,
        items_res=items_ua,
        cfg_res={"sections": [], "customMods": []},
    )
    assert not any(g["severity"] == "HIGH" for g in gaps_ua)
    assert not any("Affliction" in g["message"] for g in gaps_ua)


def test_dark_pact_does_not_false_trigger_pact_skill_check() -> None:
    skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [{"name": "Dark Pact", "enabled": True, "support": False, "level": 20}],
            }
        ],
    }
    gaps, mech = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30},
        skills_res=skills,
        items_res={"items": [], "slots": []},
        cfg_res={"sections": [], "customMods": []},
    )
    assert "pactSkills" not in mech
    assert not any("Affliction" in g["message"] for g in gaps)


def test_overloaded_intensity_apex_nadir_ramp_note() -> None:
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
                    {"name": "Pinpoint", "enabled": True, "support": True, "level": 21},
                    {"name": "Overloaded Intensity", "enabled": True, "support": True, "level": 4},
                ],
            },
            {
                "index": 4,
                "slot": "Gloves",
                "enabled": True,
                "gems": [{"name": "Frostblink of Wintry Blast", "enabled": True, "support": False, "level": 20}],
            },
        ],
    }
    items = {
        "items": [
            {"id": 11, "title": "Nadir Mode", "baseName": "Cobalt Jewel"},
            {"id": 12, "title": "Apex Mode", "baseName": "Cobalt Jewel"},
        ],
        "slots": [
            {"name": "Jewel 59585", "selItemId": 11, "inactive": False},
            {"name": "Jewel 41263", "selItemId": 12, "inactive": False},
        ],
    }
    cfg = {
        "sections": [
            {"items": [{"var": "OverloadedIntensity", "value": 3}, {"var": "intensifyIntensity", "value": 4}]}
        ],
        "customMods": [],
    }
    gaps, mech = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 30},
        skills_res=skills,
        items_res=items,
        cfg_res=cfg,
    )
    ramp_gaps = [g for g in gaps if "Intensity / Overloaded ramp" in g["message"]]
    assert len(ramp_gaps) == 1
    assert "~9s" in ramp_gaps[0]["message"]
    assert "frostblink of wintry blast" in ramp_gaps[0]["message"].lower()
    assert mech["intensityRamp"]["hasApexMode"] is True
    assert mech["intensityRamp"]["hasNadirMode"] is True


def test_watchers_eye_zero_active_luminary_and_unchecked_endurance_on_hit() -> None:
    we_raw = (
        "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\nSelected Variant: 1\n"
        "{variant:1}Gain Arcane Surge for 4 seconds when you create Consecrated Ground while affected by Zealotry\n"
        "{variant:1}60% increased Lightning Damage while affected by Wrath\n"
        "{variant:1}Damage Penetrates 13% Lightning Resistance while affected by Wrath\n"
        "{variant:2}+20% to Chaos Resistance while affected by Purity of Elements\n"
    )
    items = {
        "items": [
            {"id": 4, "title": "Watcher's Eye", "baseName": "Prismatic Jewel"},
            {"id": 41, "title": "Nightmarish Book of Depravity", "type": "Viscera"},
        ],
        "slots": [
            {"name": "Jewel 61419", "selItemId": 4, "inactive": False},
            {"name": "Arm", "selItemId": 41, "inactive": False},
        ],
    }
    raw_slots = [
        {"slotName": "Jewel 61419", "raw": we_raw},
        {"slotName": "Arm", "raw": "Rarity: NORMAL\nNightmarish Book of Depravity\n3% chance to gain an Endurance Charge on Hit"},
    ]
    skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Body Armour",
                "enabled": True,
                "gems": [{"name": "Spark of the Nova", "enabled": True, "support": False, "level": 20}],
            },
            {
                "index": 6,
                "slot": "Boots",
                "enabled": True,
                "gems": [{"name": "Purity of Lightning", "enabled": True, "support": False, "level": 21}],
            },
        ],
    }
    gaps, mech = _detect_build_mechanics_and_pitfalls(
        stats={"Speed": 6.97, "HitChance": 100.0, "ManaCost": 30},
        skills_res=skills,
        items_res=items,
        cfg_res={"sections": [{"items": [{"var": "useEnduranceCharges", "value": False}]}], "customMods": []},
        equipped_slots_raw=raw_slots,
        build_info={"ascendClassName": "Luminary"},
        party_res={"fields": {"aura": ""}, "enableExportBuffs": False},
    )
    we_gaps = [g for g in gaps if "Watcher's Eye" in g["message"] and "0/3" in g["message"]]
    assert len(we_gaps) == 1
    assert we_gaps[0]["severity"] == "MEDIUM"
    assert "Wrath" in we_gaps[0]["message"] and "Zealotry" in we_gaps[0]["message"]
    assert mech["watchersEye"]["activeMods"] == 0

    assert any("Luminary" in g["message"] and "Party" in g["message"] for g in gaps)
    assert any("useEnduranceCharges" in g["message"] and "negative water" in g["message"] for g in gaps)

    # When Party tab supplies Wrath, Watcher's Eye is 2/3 active and Luminary warning clears!
    gaps_party, mech_party = _detect_build_mechanics_and_pitfalls(
        stats={"Speed": 6.97, "HitChance": 100.0, "ManaCost": 30},
        skills_res=skills,
        items_res=items,
        cfg_res={"sections": [{"items": [{"var": "useEnduranceCharges", "value": True}]}], "customMods": []},
        equipped_slots_raw=raw_slots,
        build_info={"ascendClassName": "Luminary"},
        party_res={"fields": {"aura": "Wrath"}, "enableExportBuffs": True},
    )
    assert mech_party["watchersEye"]["activeMods"] == 2
    assert not any("Watcher's Eye" in g["message"] for g in gaps_party)
    assert not any("Luminary" in g["message"] for g in gaps_party)
    assert not any("useEnduranceCharges" in g["message"] for g in gaps_party)


def test_watchers_eye_word_boundary_and_vaal_only_aura_guard() -> None:
    we_raw = (
        "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\n"
        "+40% to Critical Strike Multiplier while affected by Anger\n"
        "+8% chance to Evade Attack Hits while affected by Grace\n"
    )
    items = {
        "items": [{"id": 1, "title": "Watcher's Eye", "baseName": "Prismatic Jewel"}],
        "slots": [{"name": "Jewel 100", "selItemId": 1, "inactive": False}],
    }
    raw_slots = [{"slotName": "Jewel 100", "raw": we_raw}]
    # "Ranger" / "Graceful" in customMods and Vaal-only Grace (enableGlobal2=False) must NOT match Anger / Grace
    skills_vaal_only = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Boots",
                "enabled": True,
                "gems": [
                    {
                        "name": "Vaal Grace",
                        "skillId": "VaalGrace",
                        "enabled": True,
                        "support": False,
                        "enableGlobal1": True,
                        "enableGlobal2": False,
                    }
                ],
            }
        ],
    }
    gaps, mech = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 20},
        skills_res=skills_vaal_only,
        items_res=items,
        cfg_res={
            "sections": [],
            "customMods": [{"title": "Default", "enabled": True, "text": "Ranger Graceful Danger"}],
        },
        equipped_slots_raw=raw_slots,
    )
    assert mech["watchersEye"]["activeMods"] == 0
    assert any("Watcher's Eye" in g["message"] and "0/2" in g["message"] for g in gaps)

    # When Vaal Grace base aura IS enabled (enableGlobal2=True), Grace counts as active
    skills_base_on = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Boots",
                "enabled": True,
                "gems": [
                    {
                        "name": "Vaal Grace",
                        "skillId": "VaalGrace",
                        "enabled": True,
                        "support": False,
                        "enableGlobal1": False,
                        "enableGlobal2": True,
                    }
                ],
            }
        ],
    }
    gaps_on, mech_on = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 20},
        skills_res=skills_base_on,
        items_res=items,
        cfg_res={"sections": [], "customMods": []},
        equipped_slots_raw=raw_slots,
    )
    assert mech_on["watchersEye"]["activeMods"] == 1
    assert not any("Watcher's Eye" in g["message"] for g in gaps_on)

    # CR-12: conditional consumer line in customMods ("while affected by Anger") must NOT count as active aura
    gaps_cond, mech_cond = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 20},
        skills_res={"mainSocketGroup": 1, "groups": []},
        items_res=items,
        cfg_res={
            "sections": [],
            "customMods": [
                {
                    "title": "Default",
                    "enabled": True,
                    "text": "60% increased Damage while affected by Anger\n受优雅影响时闪避提高 20%",
                }
            ],
        },
        equipped_slots_raw=raw_slots,
    )
    assert mech_cond["watchersEye"]["activeMods"] == 0
    assert any("Watcher's Eye" in g["message"] and "0/2" in g["message"] for g in gaps_cond)


def test_party_export_only_vaal_impurity_and_generosity_exclusion() -> None:
    # 1. enableExportBuffs alone is outgoing export, NOT incoming ally buffs (Luminary must still fire)
    gaps_lum, mech_lum = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 20},
        skills_res={"mainSocketGroup": 1, "groups": []},
        items_res={"items": [], "slots": []},
        cfg_res={"sections": [], "customMods": []},
        build_info={"ascendClassName": "Luminary"},
        party_res={"fields": {}, "enableExportBuffs": True},
    )
    assert mech_lum.get("luminaryUnconfigured") is True
    assert any("Luminary" in g["message"] for g in gaps_lum)

    # 2. Vaal Impurity of Fire/Ice/Lightning grants permanent Purity of Fire/Ice/Lightning when enableGlobal2=True
    for elem, skill_id in (
        ("Fire", "VaalImpurityOfFire"),
        ("Ice", "VaalImpurityOfIce"),
        ("Lightning", "VaalImpurityOfLightning"),
    ):
        we_raw = (
            "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\n"
            f"+12% to {elem} Resistance while affected by Purity of {elem}\n"
        )
        items_e = {
            "items": [{"id": 1, "title": "Watcher's Eye", "baseName": "Prismatic Jewel"}],
            "slots": [{"name": "Jewel 100", "selItemId": 1, "inactive": False}],
        }
        raw_e = [{"slotName": "Jewel 100", "raw": we_raw}]
        for g2_enabled, expected_active in ((True, 1), (False, 0)):
            sk_imp = {
                "mainSocketGroup": 1,
                "groups": [
                    {
                        "index": 1,
                        "isMain": True,
                        "slot": "Boots",
                        "enabled": True,
                        "gems": [
                            {
                                "name": f"Vaal Impurity of {elem}",
                                "skillId": skill_id,
                                "enabled": True,
                                "support": False,
                                "enableGlobal1": True,
                                "enableGlobal2": g2_enabled,
                            }
                        ],
                    }
                ],
            }
            _, mech_e = _detect_build_mechanics_and_pitfalls(
                stats={"ManaCost": 20},
                skills_res=sk_imp,
                items_res=items_e,
                cfg_res={"sections": [], "customMods": []},
                equipped_slots_raw=raw_e,
            )
            assert mech_e["watchersEye"]["activeMods"] == expected_active

    # 3. Generosity-supported Wrath does NOT affect the player (0/1), while unsupported or Party Wrath DOES (1/1)
    we_wrath = "Rarity: UNIQUE\nWatcher's Eye\nPrismatic Jewel\n10% increased Lightning Damage while affected by Wrath\n"
    items_w = {
        "items": [{"id": 1, "title": "Watcher's Eye", "baseName": "Prismatic Jewel"}],
        "slots": [{"name": "Jewel 100", "selItemId": 1, "inactive": False}],
    }
    raw_w = [{"slotName": "Jewel 100", "raw": we_wrath}]
    sk_gen = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "slot": "Helmet",
                "enabled": True,
                "gems": [
                    {"name": "Wrath", "skillId": "Wrath", "enabled": True, "support": False},
                    {"name": "Awakened Generosity", "skillId": "SupportGenerosityPlus", "enabled": True, "support": True},
                ],
            }
        ],
    }
    gaps_gen, mech_gen = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 20},
        skills_res=sk_gen,
        items_res=items_w,
        cfg_res={"sections": [], "customMods": []},
        equipped_slots_raw=raw_w,
    )
    assert mech_gen["watchersEye"]["activeMods"] == 0
    assert any("Watcher's Eye" in g["message"] and "0/1" in g["message"] for g in gaps_gen)

    # Party Wrath still satisfies Watcher's Eye even when player's own Wrath has Generosity
    _, mech_party_w = _detect_build_mechanics_and_pitfalls(
        stats={"ManaCost": 20},
        skills_res=sk_gen,
        items_res=items_w,
        cfg_res={"sections": [], "customMods": []},
        equipped_slots_raw=raw_w,
        party_res={"fields": {"aura": "Wrath"}, "enableExportBuffs": False},
    )
    assert mech_party_w["watchersEye"]["activeMods"] == 1
