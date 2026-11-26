"""Layer 2 tests: constants cleanup, Pass 1 clamps/AVERAGE, Pass 2 ailments/charges/rage/fortify/wither/Indigon."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from pob_cli.analyzer import dewater_build_with_records
from pob_cli.analyzer.constants import _POB_TRUTHY_CONFIG_DEFAULTS, INFLATION_CONFIG_EXACT
from pob_cli.analyzer.dewater_provenance import infer_v2_static_auto_keeps, is_close_range_main_skill


class _TwoPassWorker:
    """Fake worker with `dewaterProbes=True` simulating Pass 1 -> Pass 2 clean recalculation."""

    def __init__(
        self,
        *,
        config: dict[str, Any],
        provenance: dict[str, Any],
        skills: dict[str, Any] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.caps = {"dewaterProbes": True}
        self.config = dict(config)
        self.provenance = provenance
        self.skills = skills or {"mainSocketGroup": 1, "groups": []}
        self.prov_calls: list[dict[str, Any]] = []
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
        if method == "get_dewater_provenance":
            self.prov_calls.append(dict(params))
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
            return {"slots": [], "items": []}
        if method == "list_skills":
            return self.skills
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        raise AssertionError(f"unexpected rpc {method}")


def test_constants_dead_keys_removed_and_330_keys_and_defaults_registered() -> None:
    dead_9 = {
        "onslaught", "adrenaline", "fortify", "tailwind", "elusive",
        "unholyMight", "divineBlessing", "innervation", "infusedChannelling",
    }
    assert INFLATION_CONFIG_EXACT.isdisjoint(dead_9)
    required_new = {
        "Unbound", "ShockStacks", "HoarfrostStacks", "ScorchStacks",
        "barkskinStacks", "bannerPlanted", "bannerValour",
        "warcryMode", "pactMode", "sigilOfPowerStages", "frostShieldStages",
        "prideEffect", "innervateInnervation", "infusedChannellingInfusion",
        "closeCombatCombatRush", "configResonanceCount", "traumaStacks",
    }
    assert required_new.issubset(INFLATION_CONFIG_EXACT)
    for k, v in (
        ("bloodsoakedBannerStages", 1),
        ("plagueBearerState", "INC"),
        ("resourceGainMode", "AVERAGE"),
        ("touchedDebuffsCount", 10),
        ("maniaDebuffsCount", 15),
    ):
        assert _POB_TRUTHY_CONFIG_DEFAULTS[k] == v


def test_pass1_nearby_clamp_average_modes_close_range_trauma_and_new_inflation_keys() -> None:
    skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [{"index": 1, "name": "Flicker Strike", "enabled": True, "support": False}],
            }
        ],
    }
    prov = {
        "ok": True,
        "mageblood": {"leftMagicUtility": 0, "rightMagicUtility": 0, "utilityFlasksDisabled": False, "controlledSlots": []},
        "flasks": [],
        "corpseSources": [],
        "flags": {"enemyIsBoss": "Pinnacle", "mainSkill": {"isMelee": True, "isProxy": False}},
    }
    w = _TwoPassWorker(
        config={
            "enemyIsBoss": "None",
            "multiplierNearbyEnemies": 10,
            "multiplierNearbyRareOrUniqueEnemies": 1,
            "conditionAtCloseRange": True,
            "buffHeartstopper": "HIT",
            "warcryMode": "MAX",
            "pactMode": "MAX",
            "traumaStacks": 40,
            "Unbound": True,
            "bannerPlanted": True,
            "sigilOfPowerStages": 4,
            "bloodsoakedBannerStages": 1,  # PoB default -> untouched!
        },
        provenance=prov,
        skills=skills,
    )
    stripped, _ = dewater_build_with_records(w)  # type: ignore[arg-type]
    assert w.config["enemyIsBoss"] == "Pinnacle"
    assert w.config["multiplierNearbyEnemies"] == 1
    assert w.config["multiplierNearbyRareOrUniqueEnemies"] == 1
    assert w.config["conditionAtCloseRange"] is True
    assert w.config["buffHeartstopper"] == "AVERAGE"
    assert w.config["warcryMode"] == "AVERAGE"
    assert w.config["pactMode"] == "AVERAGE"
    assert w.config["traumaStacks"] is None
    assert w.config["Unbound"] is None
    assert w.config["bannerPlanted"] is None
    assert w.config["sigilOfPowerStages"] is None
    assert w.config["bloodsoakedBannerStages"] == 1
    assert any("multiplierNearbyEnemies=10 -> 1" in s for s in stripped["autoClamped"])
    assert any("conditionAtCloseRange=True" in s for s in stripped["autoKept"])


def test_pass2_ailments_rage_fortify_ignite_wither_elusive_lowlife_and_indigon() -> None:
    skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"index": 1, "name": "Smite", "enabled": True, "support": False},
                    {"index": 2, "name": "Fortify Support", "enabled": True, "support": True},
                    {"index": 3, "name": "Nightblade Support", "enabled": True, "support": True},
                ],
            },
            {
                "index": 2,
                "isMain": False,
                "enabled": True,
                "gems": [{"index": 1, "name": "Petrified Blood", "enabled": True, "support": False}],
            },
        ],
    }
    prov = {
        "ok": True,
        "mageblood": {"leftMagicUtility": 0, "rightMagicUtility": 0, "utilityFlasksDisabled": False, "controlledSlots": []},
        "flasks": [],
        "corpseSources": [],
        "ailments": {
            "Shock": {"canInflict": True, "computedPct": 25.0, "minimumPct": 25.0, "maxCap": 50, "chance": 100.0, "duration": 2.0},
            "Chill": {"canInflict": True, "computedPct": 18.0, "minimumPct": 0.0, "maxCap": 30, "chance": 100.0, "duration": 2.0},
            "Scorch": {"canInflict": False, "computedPct": 0.0, "minimumPct": 0.0, "maxCap": 30, "chance": 0.0, "duration": 4.0},
            "Brittle": {"canInflict": True, "computedPct": 4.2, "minimumPct": 0.0, "maxCap": 6, "chance": 100.0, "duration": 4.0},
            "Sap": {"canInflict": False, "computedPct": 0.0, "minimumPct": 0.0, "maxCap": 20, "chance": 0.0, "duration": 4.0},
        },
        "flags": {
            "enemyIsBoss": "Pinnacle",
            "enemyThreshold": 25_000_000.0,
            "canGainRage": True,
            "maximumRage": 30,
            "rageRegen": 0.0,
            "canBeElusive": True,
            "canWither": True,
            "shockCanStack": True,
            "shockStacksMax": 50,
            "mainSkill": {
                "name": "Smite",
                "isProxy": False,
                "isMelee": True,
                "canFortify": True,
                "speed": 10.0,
                "hitChance": 100.0,
                "critChance": 85.0,
                "averageHit": 300_000.0,  # >= 0.5% of 25M (125k) -> Fortify allowed!
                "igniteChance": 99.6,     # >= 99.5% -> Ignite sustained (no 100% < 100% bug!)
                "igniteDps": 50_000.0,
                "igniteDuration": 4.0,
                "bleedChance": 20.0,
                "bleedDps": 10_000.0,
                "bleedDuration": 5.0,     # 10 * 0.2 * 5 = 10 >= 3.0 -> Bleed sustained!
                "manaCost": 50.0,
                "manaSpendPerSec": 500.0,
                "sustainableManaRecovPerSec": 300.0,  # 4s cap = min(2000, 1200) = 1200!
                "lifeUnreservedPct": 52.0,
            },
        },
    }
    tree_summary = {"ascendancyNodes": [{"name": "Withering Presence"}], "keystones": [], "notables": []}
    w = _TwoPassWorker(
        config={
            "enemyIsBoss": "Pinnacle",
            "conditionEnemyShocked": True,
            "conditionShockEffect": 50,
            "conditionEnemyChilled": True,
            "conditionEnemyChilledEffect": 30,
            "conditionEnemyBrittle": True,
            "conditionBrittleEffect": 6,
            "multiplierRage": 50,
            "buffFortification": True,
            "overrideFortification": 25,
            "buffElusive": True,
            "overrideBuffElusive": 180,
            "conditionEnemyIgnited": True,
            "conditionEnemyBleeding": True,
            "multiplierWitheredStackCount": 15,
            "ShockStacks": 50,
            "conditionLowLife": True,
            "multiplierManaSpentRecently": 8000,
        },
        provenance=prov,
        skills=skills,
    )
    stripped, _ = dewater_build_with_records(w, tree_summary=tree_summary)  # type: ignore[arg-type]
    # Ailments clamped cleanly
    assert w.config["conditionEnemyShocked"] is True
    assert w.config["conditionShockEffect"] == 25
    assert w.config["conditionEnemyChilled"] is True
    assert w.config["conditionEnemyChilledEffect"] == 18
    assert w.config["conditionEnemyBrittle"] is True
    assert w.config["conditionBrittleEffect"] == 4
    # Rage clamped to 30, Fortify kept while overrideFortification stays cleared, Elusive kept while override cleared!
    assert w.config["multiplierRage"] == 30
    assert w.config["buffFortification"] is True
    assert w.config["overrideFortification"] is None
    assert w.config["buffElusive"] is True
    assert w.config["overrideBuffElusive"] is None
    # Ignite (99.6%), Bleed (expected 10 >= 3), Wither (15), Warden ShockStacks (floor(10*1*2)=20), LowLife (PB no sust life flask)
    assert w.config["conditionEnemyIgnited"] is True
    assert w.config["conditionEnemyBleeding"] is True
    assert w.config["multiplierWitheredStackCount"] == 15
    assert w.config["ShockStacks"] == 20
    assert w.config["conditionLowLife"] is True
    # Indigon clamped to min(4*500, 4*300) = 1200
    assert w.config["multiplierManaSpentRecently"] == 1200
    assert any("multiplierManaSpentRecently=8000 -> 1200" in s for s in stripped["autoClamped"])

    # Sustained Life Flask blocks Petrified Blood Low Life!
    prov_sust_lf = {
        **prov,
        "flasks": [{"slot": "Flask 1", "active": True, "isTincture": False, "isLife": True, "sustained": True, "chargeGainBlocked": False, "minUptimePct": 100.0}],
    }
    w_pb = _TwoPassWorker(config={"enemyIsBoss": "Pinnacle", "conditionLowLife": True}, provenance=prov_sust_lf, skills=skills)
    dewater_build_with_records(w_pb)  # type: ignore[arg-type]
    assert w_pb.config["conditionLowLife"] is None

    # Low melee hit (< 0.5% threshold) blocks Fortify!
    prov_weak_hit = {
        **prov,
        "flags": {**prov["flags"], "mainSkill": {**prov["flags"]["mainSkill"], "averageHit": 20_000.0}},
    }
    w_weak = _TwoPassWorker(config={"enemyIsBoss": "Pinnacle", "buffFortification": True}, provenance=prov_weak_hit, skills=skills)
    dewater_build_with_records(w_weak)  # type: ignore[arg-type]
    assert w_weak.config["buffFortification"] is None


def test_v2_static_charges_proxy_exclusion_and_core_buffs() -> None:
    # Proxy totem PCoC must NOT grant player Power Charges; Q0 Assassin's Mark must NOT; Autoexerted Enduring Cry must NOT!
    proxy_skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"name": "Spark", "enabled": True, "support": False},
                    {"name": "Spell Totem Support", "enabled": True, "support": True},
                    {"name": "Power Charge On Critical Support", "enabled": True, "support": True},
                    {"name": "Lifetap Support", "enabled": True, "support": True},
                ],
            },
            {
                "index": 2,
                "enabled": True,
                "gems": [{"name": "Assassin's Mark", "quality": 0, "enabled": True, "support": False}],
            },
            {
                "index": 3,
                "enabled": True,
                "gems": [
                    {"name": "Enduring Cry", "enabled": True, "support": False},
                    {"name": "Autoexertion Support", "enabled": True, "support": True},
                ],
            },
        ],
    }
    ak_proxy = infer_v2_static_auto_keeps(
        stats={"Speed": 8.0, "CritChance": 90.0, "HitChance": 100.0},
        skills_res=proxy_skills,
        node_names=set(),
        item_blob="",
        mod_lines=[],
        mods_low="",
    )
    assert "usepowercharges" not in ak_proxy
    assert "useendurancecharges" not in ak_proxy
    assert "bufflifetap" not in ak_proxy

    # Charged Mines exception + Q20 Assassin's Mark + Manaforged Frenzy + Eldritch chest (unbound from hit-tank!) + Soul Drinker + Arcane Blessing
    good_skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"name": "Hexblast", "enabled": True, "support": False},
                    {"name": "High-Impact Mine Support", "enabled": True, "support": True},
                    {"name": "Charged Mines Support", "enabled": True, "support": True},
                ],
            },
            {
                "index": 2,
                "enabled": True,
                "gems": [
                    {"name": "Frenzy", "enabled": True, "support": False},
                    {"name": "Manaforged Arrows Support", "enabled": True, "support": True},
                ],
            },
        ],
    }
    ak_good = infer_v2_static_auto_keeps(
        stats={"Speed": 6.0, "CritChance": 80.0, "HitChance": 100.0},
        skills_res=good_skills,
        node_names={"arcane blessing", "soul drinker", "sanctuary", "pious path"},
        item_blob="",
        mod_lines=["Gain an Endurance Charge every 10 seconds"],
        mods_low="gain an endurance charge every 10 seconds",
        endurance_duration=10.0,
    )
    assert "Charged Mines" in ak_good["usepowercharges"]
    assert "Manaforged + Frenzy" in ak_good["usefrenzycharges"]
    assert "Eldritch Endurance Charge" in ak_good["useendurancecharges"]
    assert "Arcane Blessing" in ak_good["buffarcanesurge"]
    assert "Sanctuary + Pious Path" in ak_good["conditiononconsecratedground"]
    assert "Soul Drinker" in ak_good["conditionfullenergyshield"]


@pytest.mark.parametrize("skill_zh", ["重擊", "旋風斬", "飛刃風暴", "閃現打擊"])
def test_close_range_main_skill_matches_traditional_chinese_tokens(skill_zh: str) -> None:
    skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [{"index": 1, "name": skill_zh, "enabled": True, "support": False}],
            }
        ],
    }
    assert is_close_range_main_skill(skills) is True


def test_pass2_degradation_exposes_flag_and_falls_back_to_basic_mechanics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pob_cli.analyzer import dewater_clamp

    prov_ok = {
        "ok": True,
        "mageblood": {"leftMagicUtility": 0, "rightMagicUtility": 0, "utilityFlasksDisabled": False, "controlledSlots": []},
        "flasks": [],
        "corpseSources": [],
        "ailments": {"Shock": {"canInflict": True, "computedPct": 25.0, "minimumPct": 0.0, "maxCap": 50}},
        "flags": {"enemyIsBoss": "Pinnacle", "mainSkill": {"isProxy": False}},
    }

    # Normal path -> pass2Applied is True
    w_ok = _TwoPassWorker(
        config={"enemyIsBoss": "Pinnacle", "conditionEnemyShocked": True, "conditionShockEffect": 50},
        provenance=prov_ok,
    )
    stripped_ok, _ = dewater_build_with_records(w_ok)  # type: ignore[arg-type]
    assert stripped_ok["pass2Applied"] is True
    assert not stripped_ok.get("pass2Warnings")

    # Pass 2 get_dewater_provenance raises -> pass2Applied is False and refine_post_dewater_mechanics called once
    refine_calls: list[Any] = []

    def fake_refine(*args: Any, **kwargs: Any) -> None:
        refine_calls.append((args, kwargs))

    monkeypatch.setattr(dewater_clamp, "refine_post_dewater_mechanics", fake_refine)

    class _FailingProvWorker(_TwoPassWorker):
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "get_dewater_provenance" and params.get("includeAilments") is not None:
                raise RuntimeError("lua probe boom")
            return super().call(method, params)

    w_fail = _FailingProvWorker(
        config={"enemyIsBoss": "Pinnacle", "conditionEnemyShocked": True, "conditionShockEffect": 50},
        provenance=prov_ok,
    )
    stripped_fail, _ = dewater_build_with_records(w_fail)  # type: ignore[arg-type]
    assert stripped_fail["pass2Applied"] is False
    assert len(refine_calls) == 1
    assert any("lua probe boom" in w for w in stripped_fail.get("pass2Warnings", []))

    # _set_configs_verified returns False -> pass2Applied is False and fallback runs
    refine_calls.clear()
    monkeypatch.setattr(dewater_clamp, "_set_configs_verified", lambda _w, _a: False)
    w_ver = _TwoPassWorker(
        config={"enemyIsBoss": "Pinnacle", "conditionEnemyShocked": True, "conditionShockEffect": 50},
        provenance=prov_ok,
    )
    stripped_ver, _ = dewater_build_with_records(w_ver)  # type: ignore[arg-type]
    assert stripped_ver["pass2Applied"] is False
    assert len(refine_calls) == 1
    assert any("verification failed" in w for w in stripped_ver.get("pass2Warnings", []))
