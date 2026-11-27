"""Unit tests for extracted `dewater_attribution` and atomic coupled ailment attribution."""

from __future__ import annotations

import copy
import threading
from typing import Any

import pytest

from pob_cli.analyzer import attribute_water, dewater_build_with_records
from pob_cli.analyzer.dewater_attribution import group_coupled_records
from pob_cli.analyzer.dewater_steps import _same_state
from pob_cli.snapshot import capture_digest


class _CoupledAilmentWorker:
    """Fake worker modeling PoB ConfigOptions.lua coupled (flag, effect) ailment pairs."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.lock = threading.RLock()
        self.config = dict(config)

    def _stats(self) -> dict[str, Any]:
        dps = 1_000_000.0
        if self.config.get("conditionEnemyShocked"):
            dps *= 1.0 + float(self.config.get("conditionShockEffect") or 0) / 100.0
        if self.config.get("conditionEnemyScorched"):
            dps *= 1.0 + float(self.config.get("conditionScorchedEffect") or 0) / 100.0
        if self.config.get("conditionEnemyBrittle"):
            dps *= 1.0 + float(self.config.get("conditionBrittleEffect") or 0) / 10.0
        return {
            "CombinedDPS": dps,
            "TotalEHP": 100_000.0,
            "Life": 5000.0,
            "PhysicalMaximumHitTaken": 12_000.0,
            "FireMaximumHitTaken": 30_000.0,
            "ColdMaximumHitTaken": 30_000.0,
            "LightningMaximumHitTaken": 30_000.0,
            "ChaosMaximumHitTaken": 25_000.0,
        }

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "list_config":
            return {"sections": [{"items": [{"var": k, "value": v} for k, v in self.config.items()]}], "customMods": []}
        if method == "reset_config":
            self.config[params["var"]] = None
            return {}
        if method == "set_config":
            self.config[params["var"]] = params["value"]
            return {}
        if method in ("list_items", "list_skills"):
            return {"slots": [], "items": [], "groups": []}
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method == "get_stats":
            return {"stats": self._stats()}
        if method == "get_sidebar":
            return {"warnings": []}
        if method in ("get_build_info", "get_build_header"):
            return {}
        raise AssertionError(f"unexpected rpc {method}")


def test_coupled_shock_and_scorch_attribution_is_atomic_and_reverts() -> None:
    w = _CoupledAilmentWorker(
        {
            "enemyIsBoss": "Pinnacle",
            "conditionEnemyShocked": True,
            "conditionShockEffect": 39,
            "conditionEnemyScorched": True,
            "conditionScorchedEffect": 20,
        }
    )
    _stripped, records = dewater_build_with_records(w)  # type: ignore[arg-type]
    base = capture_digest(w)  # type: ignore[arg-type]
    before = copy.deepcopy(w.config)

    rows = {r["key"]: r for r in attribute_water(w, records, base, "CombinedDPS")}  # type: ignore[arg-type]
    shock = rows["conditionEnemyShocked+conditionShockEffect"]
    assert shock.get("error") is None
    assert shock["dpsDelta"] == pytest.approx(390_000.0)
    assert shock["dpsPct"] == pytest.approx(39.0)
    assert shock["coupledKeys"] == ["conditionEnemyShocked", "conditionShockEffect"]

    scorch = rows["conditionEnemyScorched+conditionScorchedEffect"]
    assert scorch.get("error") is None
    assert scorch["dpsDelta"] == pytest.approx(200_000.0)
    assert scorch["coupledKeys"] == ["conditionEnemyScorched", "conditionScorchedEffect"]
    assert w.config == before


@pytest.mark.parametrize(
    ("flag_var", "eff_var"),
    [
        ("conditionEnemyShocked", "conditionShockEffect"),
        ("conditionEnemyChilled", "conditionEnemyChilledEffect"),
        ("conditionEnemyScorched", "conditionScorchedEffect"),
        ("conditionEnemyBrittle", "conditionBrittleEffect"),
        ("conditionEnemySapped", "conditionSapEffect"),
    ],
)
def test_group_coupled_records_covers_all_five_ailment_pairs(flag_var: str, eff_var: str) -> None:
    recs = [
        {
            "dimension": "config",
            "key": flag_var,
            "label": f"{flag_var}=True",
            "restore": [("set_config", {"var": flag_var, "value": True})],
            "revert": [("reset_config", {"var": flag_var})],
            "probe": {"kind": "config", "var": flag_var},
            "original": True,
        },
        {
            "dimension": "config",
            "key": eff_var,
            "label": f"{eff_var}=20",
            "restore": [("set_config", {"var": eff_var, "value": 20})],
            "revert": [("reset_config", {"var": eff_var})],
            "probe": {"kind": "config", "var": eff_var},
            "original": 20,
        },
    ]
    grouped = group_coupled_records(recs)
    assert len(grouped) == 1
    assert grouped[0]["key"] == f"{flag_var}+{eff_var}"
    assert grouped[0]["probe"] == {"kind": "config_multi", "vars": [flag_var, eff_var]}
    assert grouped[0]["original"] == {flag_var: True, eff_var: 20}


def test_partial_clamp_record_passes_through_ungrouped() -> None:
    clamp_rec = [
        {
            "dimension": "config",
            "key": "conditionShockEffect",
            "label": "conditionShockEffect=39 -> 23",
            "restore": [("set_config", {"var": "conditionShockEffect", "value": 39})],
            "revert": [("set_config", {"var": "conditionShockEffect", "value": 23})],
            "probe": {"kind": "config", "var": "conditionShockEffect"},
            "original": 39,
        }
    ]
    assert group_coupled_records(clamp_rec) == clamp_rec


def test_same_state_handles_bool_none_numeric_list_and_nested_dicts() -> None:
    assert _same_state(False, None) is True
    assert _same_state(True, False) is False
    assert _same_state(39, 39.0) is True
    assert _same_state(39, 40) is False
    assert _same_state([1, 2], [1, 2]) is True
    assert _same_state([1, 2], [1, 3]) is False
    assert _same_state(
        {"conditionEnemyShocked": False, "conditionShockEffect": 39, "nested": {"a": 1.0}},
        {"conditionEnemyShocked": None, "conditionShockEffect": 39.0, "nested": {"a": 1}},
    ) is True
    assert _same_state(
        {"conditionEnemyShocked": True, "conditionShockEffect": 39},
        {"conditionEnemyShocked": True, "conditionShockEffect": 23},
    ) is False
    assert _same_state({"a": 1}, {"a": 1, "b": 2}) is False


def test_keep_plus_joined_coupled_key_keeps_both_without_rejection() -> None:
    w = _CoupledAilmentWorker(
        {
            "enemyIsBoss": "Pinnacle",
            "conditionEnemyShocked": True,
            "conditionShockEffect": 39,
        }
    )
    stripped, records = dewater_build_with_records(w, keep="conditionEnemyShocked+conditionShockEffect")  # type: ignore[arg-type]
    assert stripped["keepRejected"] == []
    assert stripped["config"] == []
    assert records == []
    assert w.config["conditionEnemyShocked"] is True
    assert w.config["conditionShockEffect"] == 39
