"""Unit tests for Boss shock clamp, read-back verification, on-hit debuffs/charges, and proxy guard."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from pob_cli.analyzer import (
    attribute_water,
    dewater_build_with_records,
    infer_auto_keep_and_flask_notes,
)
from pob_cli.analyzer.dewater_mechanics import (
    find_on_hit_charge_sources,
    infer_on_hit_auto_keeps,
    is_proxy_main_skill,
)
from pob_cli.snapshot import capture_digest


class _ShockAndCritWorker:
    """Fake worker modeling coupled ShockVal, CritRecently, proxy flags, and read-back verification."""

    def __init__(
        self,
        *,
        shock_effect: int | None = 39,
        shock_flag: bool = True,
        computed_shock: float = 43.0,
        can_shock: bool = True,
        max_shock_cap: int = 50,
        boss: str = "Pinnacle",
        crit_chance: float = 99.5,
        speed: float = 6.97,
        crit_recently: bool = True,
        probe_supported: bool = True,
        is_proxy: bool = False,
        skills_res: dict[str, Any] | None = None,
        reject_set_config: bool = False,
    ) -> None:
        self.lock = threading.RLock()
        self.config: dict[str, Any] = {
            "enemyIsBoss": boss,
            "conditionEnemyShocked": shock_flag,
            "conditionCritRecently": crit_recently,
        }
        if shock_effect is not None:
            self.config["conditionShockEffect"] = shock_effect
        self.computed_shock = computed_shock
        self.can_shock = can_shock
        self.max_shock_cap = max_shock_cap
        self.crit_chance = crit_chance
        self.speed = speed
        self.probe_supported = probe_supported
        self.is_proxy = is_proxy
        self.skills_res = skills_res or {"groups": []}
        self.reject_set_config = reject_set_config

    def _stats(self) -> dict[str, Any]:
        dps = 1_000_000.0
        if self.config.get("conditionEnemyShocked"):
            dps *= 1.0 + float(self.config.get("conditionShockEffect") or 0) / 100.0
        if self.config.get("conditionCritRecently"):
            dps *= 1.05
        return {
            "CombinedDPS": dps,
            "TotalEHP": 100_000.0,
            "Life": 5000.0,
            "PhysicalMaximumHitTaken": 12_000.0,
            "FireMaximumHitTaken": 30_000.0,
            "ColdMaximumHitTaken": 30_000.0,
            "LightningMaximumHitTaken": 30_000.0,
            "ChaosMaximumHitTaken": 25_000.0,
            "CritChance": self.crit_chance,
            "Speed": self.speed,
            "HitChance": 100.0,
        }

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m, p in calls:
            try:
                out.append(self.call(m, p or {}))
            except Exception as exc:
                if raise_on_error:
                    raise
                out.append({"ok": False, "_error": str(exc), "error": str(exc)})
        return out

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "list_config":
            return {
                "sections": [{"items": [{"var": k, "value": v} for k, v in self.config.items()]}],
                "customMods": [],
            }
        if method == "reset_config":
            self.config[params["var"]] = None
            return {}
        if method == "set_config":
            if self.reject_set_config and params["var"] in ("conditionCritRecently", "conditionShockEffect"):
                return {}
            self.config[params["var"]] = params["value"]
            return {}
        if method == "list_items":
            return {"slots": [], "items": []}
        if method == "list_skills":
            return self.skills_res
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method == "get_stats":
            return {"stats": self._stats()}
        if method == "get_sidebar":
            return {"warnings": []}
        if method in ("get_build_info", "get_build_header"):
            return {}
        if method == "get_shock_probe":
            if not self.probe_supported:
                raise RuntimeError("unknown rpc get_shock_probe")
            return {
                "ok": True,
                "canShock": self.can_shock,
                "isProxy": self.is_proxy,
                "configDerived": bool(self.config.get("conditionEnemyShocked")),
                "computedShockPct": self.computed_shock,
                "maxShockCap": self.max_shock_cap,
                "enemyIsBoss": self.config.get("enemyIsBoss") or "Pinnacle",
            }
        raise AssertionError(f"unexpected rpc {method}")


def test_boss_shock_auto_keeps_when_below_computed_cap() -> None:
    w = _ShockAndCritWorker(shock_effect=39, computed_shock=43.0, boss="Pinnacle")
    stripped, records = dewater_build_with_records(w)  # type: ignore[arg-type]
    assert w.config["conditionEnemyShocked"] is True
    assert w.config["conditionShockEffect"] == 39
    assert not any("conditionShockEffect" in s for s in stripped["config"])
    assert any("conditionShockEffect=39" in s for s in stripped["autoKept"])
    assert not any(r["key"] in ("conditionEnemyShocked", "conditionShockEffect") for r in records)


def test_boss_shock_clamps_excess_and_attributes_excess_only() -> None:
    w = _ShockAndCritWorker(shock_effect=39, computed_shock=25.4, boss="Uber")
    stripped, records = dewater_build_with_records(w, keep="enemyIsBoss")  # type: ignore[arg-type]
    assert w.config["conditionEnemyShocked"] is True
    assert w.config["conditionShockEffect"] == 25
    assert any("39 -> 25" in s for s in stripped["config"])

    base = capture_digest(w)  # type: ignore[arg-type]
    rows = {r["key"]: r for r in attribute_water(w, records, base, "CombinedDPS")}  # type: ignore[arg-type]
    clamp_row = rows["conditionShockEffect"]
    assert clamp_row.get("error") is None
    assert clamp_row["dpsPct"] == pytest.approx((1.39 / 1.25 - 1.0) * 100.0)
    assert w.config["conditionShockEffect"] == 25


def test_shock_unset_effect_probe_fallback_and_readback_guard() -> None:
    w_unset = _ShockAndCritWorker(shock_effect=None, computed_shock=20.0, crit_recently=False)
    stripped, _ = dewater_build_with_records(w_unset)  # type: ignore[arg-type]
    assert w_unset.config["conditionEnemyShocked"] is True
    assert any("conditionEnemyShocked=True" in s for s in stripped["autoKept"])

    w_legacy = _ShockAndCritWorker(shock_effect=39, probe_supported=False, crit_recently=False)
    _st2, recs2 = dewater_build_with_records(w_legacy)  # type: ignore[arg-type]
    assert w_legacy.config["conditionEnemyShocked"] is None
    assert any(r["key"] == "conditionEnemyShocked" for r in recs2)

    # Read-back verification guard + rollback: if PoB ignores conditionShockEffect, conditionEnemyShocked is rolled back too
    w_rej = _ShockAndCritWorker(shock_effect=39, computed_shock=43.0, crit_recently=True, reject_set_config=True)
    st_rej, recs_rej = dewater_build_with_records(w_rej)  # type: ignore[arg-type]
    assert any(r["key"] == "conditionCritRecently" for r in recs_rej)
    assert any(r["key"] == "conditionShockEffect" for r in recs_rej)
    assert not any("conditionCritRecently=True" in s for s in st_rej["autoKept"])
    assert w_rej.config["conditionEnemyShocked"] is None
    assert w_rej.config["conditionShockEffect"] is None


def test_crit_recently_and_proxy_skills_strip_player_on_hit() -> None:
    w_hi = _ShockAndCritWorker(crit_chance=99.5, speed=6.97, crit_recently=True, can_shock=False)
    stripped_hi, _ = dewater_build_with_records(w_hi)  # type: ignore[arg-type]
    assert w_hi.config["conditionCritRecently"] is True
    assert any("conditionCritRecently=True" in s for s in stripped_hi["autoKept"])

    w_lo = _ShockAndCritWorker(crit_chance=20.0, speed=1.0, crit_recently=True, can_shock=False)
    stripped_lo, _ = dewater_build_with_records(w_lo)  # type: ignore[arg-type]
    assert w_lo.config["conditionCritRecently"] is None
    assert any("conditionCritRecently" in s for s in stripped_lo["config"])

    totem_skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"name": "Freezing Pulse", "enabled": True, "support": False},
                    {"name": "Spell Totem Support", "skillId": "SupportSpellTotem", "enabled": True, "support": True},
                ],
            }
        ],
    }
    mine_skills = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [
                    {"name": "Hexblast", "enabled": True, "support": False},
                    {"name": "High-Impact Mine Support", "enabled": True, "support": True},
                ],
            }
        ],
    }
    assert is_proxy_main_skill(totem_skills) is True
    assert is_proxy_main_skill(mine_skills) is True

    stats = {"Speed": 8.0, "HitChance": 100.0, "CritChance": 95.0}
    ak_totem = infer_on_hit_auto_keeps(
        stats=stats, skills_res=totem_skills, mod_lines=["3% chance to gain an Endurance Charge on Hit"]
    )
    assert "useendurancecharges" not in ak_totem

    w_totem = _ShockAndCritWorker(crit_chance=95.0, speed=8.0, crit_recently=True, can_shock=False, skills_res=totem_skills)
    dewater_build_with_records(w_totem)  # type: ignore[arg-type]
    assert w_totem.config["conditionCritRecently"] is None

    w_mine_probe = _ShockAndCritWorker(crit_chance=95.0, speed=8.0, crit_recently=True, can_shock=False, is_proxy=True)
    dewater_build_with_records(w_mine_probe)  # type: ignore[arg-type]
    assert w_mine_probe.config["conditionCritRecently"] is None


def test_on_hit_debuff_and_charge_auto_keeps_exclude_on_kill() -> None:
    stats = {"Speed": 6.97, "HitChance": 100.0, "CritChance": 95.0}
    kept = infer_on_hit_auto_keeps(
        stats=stats,
        skills_res={"groups": []},
        mod_lines=[
            "25% chance to Unnerve Enemies for 4 seconds on Hit",
            "3% chance to gain an Endurance Charge on Hit",
            "10% chance to Intimidate Enemies for 4 seconds on Kill",
        ],
    )
    assert "conditionenemyunnerved" in kept
    assert "useendurancecharges" in kept
    assert "conditionenemyintimidated" not in kept

    ak, _ = infer_auto_keep_and_flask_notes(
        stats=stats,
        skills_res={"groups": []},
        items_res={"items": [], "slots": []},
        equipped_slots_raw=[
            {"slotName": "Gloves", "raw": "25% chance to Unnerve Enemies for 4 seconds on Hit"},
            {"slotName": "Arm", "raw": "3% chance to gain an Endurance Charge on Hit"},
        ],
    )
    assert "conditionenemyunnerved" in ak
    assert "useendurancecharges" in ak
    assert "useendurancecharges" in find_on_hit_charge_sources(["3% chance to gain an Endurance Charge on Hit"], stats)


def test_cr3_cr4_cr5_hit_chance_zero_selected_active_proxy_and_spell_attack_guards() -> None:
    # CR-3: HitChance=0 must yield 0 effective hit rate (never default to 1.0)
    zero_hit_stats = {"Speed": 8.0, "HitChance": 0.0, "CritChance": 95.0}
    assert find_on_hit_charge_sources(["10% chance to gain an Endurance Charge on Hit"], zero_hit_stats) == {}
    ak_zero = infer_on_hit_auto_keeps(
        stats=zero_hit_stats,
        skills_res={"groups": []},
        mod_lines=[
            "Unnerve Enemies for 4 seconds on Hit",
            "25% chance to Unnerve Enemies for 4 seconds on Hit",
            "Nearby Enemies are Intimidated",
        ],
    )
    assert "conditionenemyunnerved" not in ak_zero
    assert "conditionenemyintimidated" in ak_zero  # passive Nearby still works at hit_rate=0

    # CR-4: selected active gem Fireball sharing group with unselected Decoy Totem is NOT proxy
    shared_group = {
        "mainSocketGroup": 1,
        "groups": [
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "mainActiveSkill": 1,
                "gems": [
                    {"name": "Fireball", "enabled": True, "support": False},
                    {"name": "Decoy Totem", "enabled": True, "support": False},
                ],
            }
        ],
    }
    assert is_proxy_main_skill(shared_group) is False
    shared_group["groups"][0]["mainActiveSkill"] = 2
    assert is_proxy_main_skill(shared_group) is True

    # CR-5: Spell vs Attack qualified `on ... hit` mismatch is filtered out
    spell_only_stats = {"Speed": 6.0, "CritChance": 80.0}  # HitChance is None -> Spell
    ak_spell = infer_on_hit_auto_keeps(
        stats=spell_only_stats,
        skills_res={"groups": []},
        mod_lines=[
            "Intimidate Enemies for 4 seconds on Attack Hit",
            "Unnerve Enemies for 4 seconds on Spell Hit",
        ],
    )
    assert "conditionenemyintimidated" not in ak_spell
    assert "conditionenemyunnerved" in ak_spell

    atk_stats = {"Speed": 6.0, "HitChance": 100.0, "MainHandAccuracy": 3200.0}
    ak_atk = infer_on_hit_auto_keeps(
        stats=atk_stats,
        skills_res={"groups": []},
        mod_lines=[
            "Unnerve Enemies for 4 seconds on Spell Hit",
            "Intimidate Enemies for 4 seconds on Attack Hit",
        ],
    )
    assert "conditionenemyunnerved" not in ak_atk
    assert "conditionenemyintimidated" in ak_atk

    # Minion/ally-owned on-hit charge lines must NOT infer player charges
    assert (
        find_on_hit_charge_sources(
            [
                "Minions have 10% chance to gain a Power Charge on Hit",
                "Nearby Allies have 15% chance to gain a Frenzy Charge on Hit",
                "召唤物击中时有 20% 几率获得暴击球",
            ],
            {"Speed": 8.0, "HitChance": 100.0, "CritChance": 95.0},
        )
        == {}
    )
