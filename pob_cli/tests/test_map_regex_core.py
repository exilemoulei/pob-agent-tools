"""Offline unit tests for PoB map-mod rule classifier and 0-FP regex compiler."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from pob_cli.map_regex import (
    build_map_regex_result,
    classify_map_mods,
    compile_map_regex,
    load_corpus,
    min_value_regex,
)


def _base_probe(**overrides: Any) -> dict[str, Any]:
    probe: dict[str, Any] = {
        "mainSkill": {
            "name": "Ice Nova",
            "avgPhys": 0.0,
            "avgFire": 12000.0,
            "avgCold": 36200.0,
            "avgLight": 0.0,
            "avgChaos": 0.0,
            "speed": 4.2,
            "manaCost": 28.0,
            "cooldown": 0.0,
            "critChance": 65.0,
            "combinedDps": 1_500_000.0,
            "totalDps": 1_500_000.0,
            "igniteDps": 0.0,
            "poisonDps": 0.0,
            "bleedDps": 0.0,
            "cannotBeReflected": False,
            "isMinion": False,
            "isTotem": False,
            "isTrap": False,
            "isMine": False,
            "isAttack": False,
            "isSpell": True,
            "hitChance": 100.0,
            "hitsCannotBeEvaded": False,
        },
        "triggeredSkills": [],
        "movementSkills": [],
        "defence": {
            "life": 4500.0,
            "lifeUnreserved": 4500.0,
            "lifeUnreservedPct": 100.0,
            "es": 1200.0,
            "mana": 2000.0,
            "manaUnreserved": 600.0,
            "netLifeRegen": 220.0,
            "lifeRegen": 220.0,
            "lifeDegen": 0.0,
            "netEsRegen": 0.0,
            "esRegen": 0.0,
            "netManaRegen": 180.0,
            "lifeLeechRate": 0.0,
            "manaLeechRate": 0.0,
            "esLeechRate": 300.0,
            "manaGainOnHit": 0.0,
            "armour": 12000.0,
            "evasion": 10000.0,
            "blockChance": 25.0,
            "spellBlockChance": 20.0,
            "spellSuppressionChance": 100.0,
            "spellSuppressionOvercap": 8.0,
            "fireMaxRes": 75.0,
            "coldMaxRes": 75.0,
            "lightMaxRes": 75.0,
            "fireOvercap": 42.0,
            "coldOvercap": 40.0,
            "lightOvercap": 38.0,
            "physTakenReflect": 100.0,
            "fireTakenReflect": 100.0,
            "coldTakenReflect": 100.0,
            "lightTakenReflect": 100.0,
            "curseEffectOnSelf": 100.0,
            "critExtraDamageReduction": 0.0,
            "shockAvoid": 100.0,
            "unaffectedByShock": False,
            "unaffectedByBurningGround": False,
            "actionSpeedMin": 0.0,
            "physMaxHit": 22000.0,
        },
        "keystones": [],
        "ascendancyNodes": [],
        "uniques": [],
        "pantheon": {"major": "", "minor": ""},
        "charges": {
            "usePower": False,
            "useFrenzy": False,
            "useEndurance": False,
            "maxPower": 3,
            "maxFrenzy": 3,
            "maxEndurance": 3,
            "minPower": 0,
            "minFrenzy": 0,
            "minEndurance": 0,
        },
        "auras": {"count": 2, "names": ["Determination", "Zealotry"]},
        "flasks": {"hasEnduringMana": False},
        "curses": {"count": 1, "names": ["Frostbite"]},
        "triggerCliffs": [],
        "activeSkillNames": ["Ice Nova", "Frostblink"],
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(probe.get(key), dict):
            probe[key] = {**probe[key], **val}
        else:
            probe[key] = val
    return probe


@pytest.mark.parametrize("lang", ["en", "zh-CN", "zh-TW"])
def test_zero_false_positives_every_preset_all_langs(lang: str) -> None:
    corpus = load_corpus(lang, use_cn_overlay=False)
    for mod in corpus["mods"]:
        res = compile_map_regex(
            [{"id": mod["id"], "severity": "brick", "reason": "test"}],
            corpus=corpus,
            client_lang=lang,
            max_len=250,
        )
        assert res["falsePositiveCollisions"] == 0, f"FP on {lang}/{mod['id']}: {res['regex']}"
        assert res["length"] <= 250
        assert res["regex"].startswith('"!'), res["regex"]
        # Positive recall: inner negation branch must match every instantiated target line
        inner = res["regex"][2:-1]
        rx = re.compile(inner, re.IGNORECASE)
        for line in mod["lines"]:
            sample = line.replace("#", "18")
            assert rx.search(sample) is not None, f"{lang}/{mod['id']} token {inner!r} missed {sample!r}"


def test_notorious_collision_traps_zero_false_positives() -> None:
    corpus = load_corpus("en", use_cn_overlay=False)
    # Excluding reflect_ele must NEVER match reflect_phys or Extra Maximum Energy Shield
    res_ele = compile_map_regex([{"id": "reflect_ele", "severity": "brick", "reason": "ele"}], corpus=corpus)
    rx_ele = re.compile(res_ele["regex"][2:-1], re.IGNORECASE)
    assert rx_ele.search("Monsters reflect 18% of Elemental Damage") is not None
    assert rx_ele.search("Rare Monsters have Elemental Thorns reflecting 2500 Elemental Damage") is not None
    assert rx_ele.search("Monsters reflect 18% of Physical Damage") is None
    assert rx_ele.search("Monsters gain 45% of Maximum Life as Extra Maximum Energy Shield") is None

    # Excluding crit_res must NEVER match monster_crit
    res_crit = compile_map_regex([{"id": "crit_res", "severity": "danger", "reason": "crit"}], corpus=corpus)
    rx_crit = re.compile(res_crit["regex"][2:-1], re.IGNORECASE)
    assert rx_crit.search("Monsters take 40% reduced Extra Damage from Critical Strikes") is not None
    assert rx_crit.search("Monsters have 380% increased Critical Strike Chance") is None


def test_max_len_50_overflow_packs_bricks_first_with_zero_fp() -> None:
    corpus = load_corpus("en", use_cn_overlay=False)
    excluded = [
        {"id": "drowning_orbs", "severity": "danger", "reason": "d1"},
        {"id": "volatile_cores", "severity": "danger", "reason": "d2"},
        {"id": "reflect_ele", "severity": "brick", "reason": "b1"},
        {"id": "no_regen", "severity": "brick", "reason": "b2"},
        {"id": "no_leech", "severity": "brick", "reason": "b3"},
        {"id": "less_cdr", "severity": "brick", "reason": "b4"},
        {"id": "max_res", "severity": "brick", "reason": "b5"},
        {"id": "less_recovery", "severity": "brick", "reason": "b6"},
        {"id": "sawblades", "severity": "avoid", "reason": "a1"},
        {"id": "meteor_on_flask", "severity": "avoid", "reason": "a2"},
    ]
    res = compile_map_regex(excluded, corpus=corpus, max_len=50)
    assert res["length"] <= 50
    assert res["falsePositiveCollisions"] == 0
    assert len(res["overflowRegex"]) >= 1
    assert all(len(chunk) <= 50 for chunk in res["overflowRegex"])
    # All bricks fit before dangers/avoids overflow
    assert all(m["inPrimary"] for m in res["excludedMods"] if m["severity"] == "brick")
    assert any(not m["inPrimary"] for m in res["excludedMods"] if m["severity"] != "brick")

    tight = compile_map_regex(excluded, corpus=corpus, max_len=25)
    assert tight["length"] <= 25
    assert tight["falsePositiveCollisions"] == 0
    assert len(tight["overflowRegex"]) >= 2
    assert all(m["severity"] == "brick" for m in tight["excludedMods"] if m["inPrimary"])


def test_min_value_regex_exact_bounds() -> None:
    for thresh in (8, 35, 80, 95, 100, 125):
        rx = re.compile(f"^{min_value_regex(thresh)}$")
        for val in range(1, 205):
            assert (rx.match(str(val)) is not None) == (val >= thresh), f"thresh={thresh} val={val} pat={rx.pattern}"


def test_rules_reflect_and_immunities() -> None:
    # Default Ice Nova hit build: reflect_ele=brick, reflect_phys=safe
    cls = classify_map_mods(_base_probe())
    by_id = {r["id"]: r for r in cls["classifications"]}
    assert by_id["reflect_ele"]["severity"] == "brick"
    assert by_id["reflect_phys"]["severity"] == "safe"

    # Reflect-immune elementalist/slayer
    imm = classify_map_mods(
        _base_probe(defence={"fireTakenReflect": 0.0, "coldTakenReflect": 0.0, "lightTakenReflect": 0.0})
    )
    assert {r["id"]: r for r in imm["classifications"]}["reflect_ele"]["severity"] == "safe"

    # Minion build immune to reflect, unless heavy movement skill triggers danger
    minion = classify_map_mods(
        _base_probe(
            mainSkill={"isMinion": True},
            movementSkills=[{"name": "Shield Charge", "avgPhys": 0.0, "avgFire": 2000.0, "avgCold": 0.0, "avgLight": 0.0}],
        )
    )
    assert {r["id"]: r for r in minion["classifications"]}["reflect_ele"]["severity"] == "danger"

    # Pure Elemental Bow (90,000 Ele + 400 residual unconverted Phys): reflect_ele=brick, reflect_phys=safe
    ele_bow = classify_map_mods(
        _base_probe(
            mainSkill={
                "name": "Lightning Arrow",
                "avgPhys": 400.0,
                "avgFire": 20000.0,
                "avgCold": 25000.0,
                "avgLight": 45000.0,
                "speed": 4.5,
                "isAttack": True,
                "isSpell": False,
            },
            defence={"life": 3800.0, "lifeUnreserved": 3800.0, "es": 0.0, "lifeLeechRate": 745.0},
        )
    )
    eb_map = {r["id"]: r for r in ele_bow["classifications"]}
    assert eb_map["reflect_ele"]["severity"] == "brick"
    assert eb_map["reflect_phys"]["severity"] == "safe"
    assert "negligible residual" in eb_map["reflect_phys"]["reason"]

    # Genuine Physical hit build (15,000 Phys) still classifies reflect_phys=brick
    phys_build = classify_map_mods(
        _base_probe(
            mainSkill={
                "name": "Cyclone",
                "avgPhys": 15000.0,
                "avgFire": 0.0,
                "avgCold": 0.0,
                "avgLight": 0.0,
                "speed": 8.0,
                "isAttack": True,
                "isSpell": False,
            },
            defence={"life": 4000.0, "lifeUnreserved": 4000.0, "es": 0.0},
        )
    )
    pb_map = {r["id"]: r for r in phys_build["classifications"]}
    assert pb_map["reflect_phys"]["severity"] == "brick"
    assert pb_map["reflect_ele"]["severity"] == "safe"


def test_rules_archetype_matrix() -> None:
    # 1. RF + Melding (88% max res)
    rf = classify_map_mods(
        _base_probe(
            activeSkillNames=["Righteous Fire"],
            uniques=["Melding of the Flesh"],
            defence={"fireMaxRes": 88.0, "lifeRegen": 1800.0, "netLifeRegen": 400.0, "lifeDegen": 1400.0},
        )
    )
    rf_map = {r["id"]: r for r in rf["classifications"]}
    assert rf_map["no_regen"]["severity"] == "brick"
    assert rf_map["less_recovery"]["severity"] == "brick"
    assert rf_map["max_res"]["severity"] == "brick"

    # 2. Trickster One Step Ahead + CI + Vaal Pact + CoC triggerCliffs
    trick = classify_map_mods(
        _base_probe(
            keystones=["Chaos Inoculation", "Vaal Pact"],
            ascendancyNodes=["One Step Ahead"],
            defence={"life": 1.0, "es": 9500.0, "curseEffectOnSelf": 0.0, "critExtraDamageReduction": 100.0},
            auras={"count": 3, "names": ["Discipline", "Grace", "Wrath"]},
            triggerCliffs=[{"skillName": "Ice Spear", "triggerName": "Cast on Critical Strike"}],
        )
    )
    t_map = {r["id"]: r for r in trick["classifications"]}
    assert t_map["action_speed"]["severity"] == "safe"
    assert "One Step Ahead" in t_map["action_speed"]["reason"]
    assert t_map["no_leech"]["severity"] == "brick"
    assert t_map["less_cdr"]["severity"] == "brick"
    assert t_map["remove_life_es_on_hit"]["severity"] == "brick"
    assert t_map["less_defences"]["severity"] == "danger"
    assert t_map["less_aura"]["severity"] == "brick"
    assert t_map["monster_crit"]["severity"] == "safe"
    assert t_map["ele_weakness"]["severity"] == "safe"
    assert t_map["temp_chains"]["severity"] == "safe"

    # 3. Flicker Strike without vs with Ralakesh + Dissolution of the Flesh + Aegis Aurora
    flk = classify_map_mods(
        _base_probe(
            activeSkillNames=["Flicker Strike"],
            uniques=["Dissolution of the Flesh", "Aegis Aurora"],
            auras={"count": 0, "names": []},
        )
    )
    f_map = {r["id"]: r for r in flk["classifications"]}
    assert f_map["steal_charges"]["severity"] == "brick"
    assert f_map["no_charges"]["severity"] == "brick"
    assert f_map["desolation"]["severity"] == "brick"
    assert f_map["burning_ground"]["severity"] == "brick"
    assert f_map["less_armour_block"]["severity"] == "brick"
    assert f_map["less_aura"]["severity"] == "safe"

    flk_ral = classify_map_mods(
        _base_probe(activeSkillNames=["Flicker Strike"], uniques=["Ralakesh's Impatience"])
    )
    assert {r["id"]: r for r in flk_ral["classifications"]}["steal_charges"]["severity"] == "safe"

    # 4. Ignite + Poison/Bleed + Hexblast
    ign = classify_map_mods(
        _base_probe(
            mainSkill={"igniteDps": 900_000.0, "combinedDps": 1_000_000.0},
            activeSkillNames=["Hexblast"],
        )
    )
    i_map = {r["id"]: r for r in ign["classifications"]}
    assert i_map["avoid_ailments"]["severity"] == "brick"
    assert i_map["monsters_hexproof"]["severity"] == "brick"

    psn = classify_map_mods(
        _base_probe(
            mainSkill={"poisonDps": 600_000.0, "bleedDps": 100_000.0, "totalDps": 300_000.0, "combinedDps": 1_000_000.0}
        )
    )
    assert {r["id"]: r for r in psn["classifications"]}["avoid_poison_bleed"]["severity"] == "brick"


def test_build_map_regex_result_preset_only_and_overrides() -> None:
    res = build_map_regex_result(
        None,
        preset_only=True,
        avoid_ids=["reflect_ele", "no_regen", "drowning_orbs"],
        client_lang="en",
        min_quant=80,
        min_pack=35,
    )
    assert res["falsePositiveCollisions"] == 0
    assert res["length"] <= 250
    assert '"m q.*\\+([89]\\d|\\d{3})%"' in res["regex"]
    assert '"pack.*\\+(3[5-9]|[4-9]\\d|\\d{3})%"' in res["regex"]

    # --allow overrides --mode brick auto-excluded mod
    res2 = build_map_regex_result(
        _base_probe(),
        mode="brick",
        allow_ids=["reflect_ele"],
        avoid_ids=["drowning_orbs"],
        client_lang="en",
    )
    excl_ids = {m["id"] for m in res2["excludedMods"]}
    assert "reflect_ele" not in excl_ids
    assert "drowning_orbs" in excl_ids


def test_duplicate_tokens_deduplicated_in_zh_cn_and_zero_cost_packing() -> None:
    corpus = load_corpus("zh-CN", use_cn_overlay=False)
    excluded = [
        {"id": "no_leech", "severity": "brick", "reason": "no leech"},
        {"id": "steal_charges", "severity": "danger", "reason": "steal charges"},
    ]
    res = compile_map_regex(excluded, corpus=corpus, client_lang="zh-CN", max_len=250)
    assert res["falsePositiveCollisions"] == 0
    assert res["regex"].count("偷取") <= 1
    inner_branches = res["regex"][2:-1].split("|")
    assert len(inner_branches) == len(set(inner_branches))

    # Shared token costs 0 extra chars even when primary is already full
    tight = compile_map_regex(excluded, corpus=corpus, client_lang="zh-CN", max_len=len(res["regex"]))
    assert tight["falsePositiveCollisions"] == 0
    assert tight["overflowRegex"] == []
    assert all(m["inPrimary"] for m in tight["excludedMods"])


def test_reflect_negligible_residual_exact_boundaries_and_provenance() -> None:
    # Pool cutoff: 3.0% of 4,000 unreserved pool = 120 reflect/hit (at 20% reflect -> 600 avgPhys).
    # 2.9% of pool (avgPhys=580 -> 116 reflect/hit) with <5% share -> safe + reflectExempt metadata
    below_pool = build_map_regex_result(
        _base_probe(
            mainSkill={
                "name": "Lightning Arrow",
                "avgPhys": 580.0,
                "avgFire": 0.0,
                "avgCold": 0.0,
                "avgLight": 99420.0,
                "speed": 4.0,
                "isAttack": True,
                "isSpell": False,
            },
            defence={"life": 4000.0, "lifeUnreserved": 4000.0, "es": 0.0, "lifeRegen": 0.0, "lifeLeechRate": 0.0},
        ),
        tier="t16",
        mode="safe",
        client_lang="en",
    )
    assert "reflect_phys" not in {m["id"] for m in below_pool["excludedMods"]}
    assert "reflect_phys" in below_pool["reflectExempt"]
    ex_meta = below_pool["reflectExempt"]["reflect_phys"]
    assert ex_meta["poolPct"] == pytest.approx(2.9, abs=0.05)
    assert ex_meta["sharePct"] < 5.0
    phys_safe = next(m for m in below_pool["exemptedMods"] if m["id"] == "reflect_phys")
    assert "% of hit < 5%" in phys_safe["reason"]
    assert "2.9% of 4,000 pool < 3%" in phys_safe["reason"]

    # 3.1% of pool (avgPhys=620 -> 124 reflect/hit >= 3.0%) -> danger (excluded in safe mode)
    above_pool = classify_map_mods(
        _base_probe(
            mainSkill={
                "name": "Lightning Arrow",
                "avgPhys": 620.0,
                "avgFire": 0.0,
                "avgCold": 0.0,
                "avgLight": 99380.0,
                "speed": 4.0,
                "isAttack": True,
                "isSpell": False,
            },
            defence={"life": 4000.0, "lifeUnreserved": 4000.0, "es": 0.0, "lifeRegen": 0.0, "lifeLeechRate": 0.0},
        )
    )
    assert {r["id"]: r for r in above_pool["classifications"]}["reflect_phys"]["severity"] == "danger"

    # Share cutoff: 5.0% of 10,000 total hit when recovery_rate == 0 (pool=4,000 -> ~2.5% pool < 3%).
    # 4.9% share (avgPhys=490, avgLight=9510) -> safe; 5.1% share (avgPhys=510, avgLight=9490) -> danger
    below_share = classify_map_mods(
        _base_probe(
            mainSkill={
                "name": "Lightning Arrow",
                "avgPhys": 490.0,
                "avgFire": 0.0,
                "avgCold": 0.0,
                "avgLight": 9510.0,
                "speed": 3.0,
                "isAttack": True,
                "isSpell": False,
            },
            defence={"life": 4000.0, "lifeUnreserved": 4000.0, "es": 0.0, "lifeRegen": 0.0, "lifeLeechRate": 0.0, "esLeechRate": 0.0},
        )
    )
    assert {r["id"]: r for r in below_share["classifications"]}["reflect_phys"]["severity"] == "safe"

    above_share = classify_map_mods(
        _base_probe(
            mainSkill={
                "name": "Lightning Arrow",
                "avgPhys": 510.0,
                "avgFire": 0.0,
                "avgCold": 0.0,
                "avgLight": 9490.0,
                "speed": 3.0,
                "isAttack": True,
                "isSpell": False,
            },
            defence={"life": 4000.0, "lifeUnreserved": 4000.0, "es": 0.0, "lifeRegen": 0.0, "lifeLeechRate": 0.0, "esLeechRate": 0.0},
        )
    )
    assert {r["id"]: r for r in above_share["classifications"]}["reflect_phys"]["severity"] == "danger"


def test_zero_false_positives_all_presets_with_zh_rcn_overlay_active(tmp_path: Path) -> None:
    stats_dir = tmp_path / "Data" / "poe1" / "zh-rCN"
    stats_dir.mkdir(parents=True)
    (stats_dir / "stats.json").write_text(
        json.dumps(
            {
                "entries": {
                    "Area contains Drowning Orbs": "区域内有淹没法球",
                    "Monsters cannot be Leeched from": "怪物无法被偷取",
                    "Players cannot Regenerate Life, Mana or Energy Shield": "玩家不能再生生命、魔力和能量护盾",
                    "Rare Monsters have Volatile Cores": "稀有怪物有无常之核",
                    "Players are assaulted by Bloodstained Sawblades": "玩家会受到血痕锯刃魔的袭击",
                    "Monsters gain {0}% of Maximum Life as Extra Maximum Energy Shield": "怪物获得等同于最大生命 {0}% 的额外最大能量护盾",
                    "Monsters reflect {0}% of Elemental Damage": "怪物会反射 {0}% 元素伤害",
                    "Monsters reflect {0}% of Physical Damage": "怪物会反射 {0}% 物理伤害",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    corpus = load_corpus("zh-CN", engine_root=tmp_path, use_cn_overlay=True)
    assert corpus["langOverlay"] == "zh-rCN"
    assert corpus["corpusVersion"] == "3.30"
    for mod in corpus["mods"]:
        res = compile_map_regex(
            [{"id": mod["id"], "severity": "brick", "reason": "overlay-test"}],
            corpus=corpus,
            client_lang="zh-CN",
            max_len=250,
        )
        assert res["falsePositiveCollisions"] == 0, f"FP under zh-rCN overlay on {mod['id']}: {res['regex']}"
        assert res["length"] <= 250


