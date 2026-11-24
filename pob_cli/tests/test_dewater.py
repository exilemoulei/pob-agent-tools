"""Unit tests for de-watering keep-list, call_batch pipelining, and per-item PoB attribution.

These use a scripted fake bridge worker so they run on every platform without the
PoB engine. The fake derives its stats from the current config/flask/gem state, the
same way the real engine would, so the tests check that every reported number is
read back from a recalculation (never estimated) and that the build is restored.
"""

from __future__ import annotations

import copy
import json
import threading
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import cli as cli_mod
from pob_cli.analyzer import (
    DEWATER_SENTINEL_TITLE,
    attribute_water,
    audit_build,
    dewater_build_in_place,
    dewater_build_with_records,
    resolve_skill_group_index,
    verify_restored,
)
from pob_cli.comparator import run_matrix
from pob_cli.mutators import (
    _add_gem_with_default_skill_flags,
    _apply_config_in_place,
    run_batch,
)
from pob_cli.snapshot import capture_digest


class FakeWorker:
    """Minimal stand-in for BridgeWorker with state-dependent stats and call_batch support."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.batch_calls: list[list[tuple[str, dict[str, Any]]]] = []
        self.last_good_code: str | None = "ORIGINAL_LAST_GOOD"
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self.config: dict[str, Any] = {
            "bandit": "Eramir",
            "enemyIsBoss": "None",
            "conditionStationary": True,  # e.g. Chieftain's Ramako needs this
            "conditionBeenHitRecently": True,
            "useEnduranceCharges": True,
            "presetBossSkills": None,
        }
        self.defaults: dict[str, Any] = {
            "conditionStationary": False,
            "conditionBeenHitRecently": False,
            "useEnduranceCharges": False,
        }
        self.flasks: dict[str, bool] = {"Flask 1": True, "Flask 2": True}
        self.gems: dict[tuple[int, int], dict[str, Any]] = {(3, 1): {"enabled": True}}
        self.custom_mods: list[dict[str, Any]] = [
            {"title": "Default", "text": "+25 to maximum Mana", "enabled": True}
        ]
        self.party_cleared = False
        self.has_mageblood = False
        self._saved: dict[str, dict[str, Any]] = {}
        self._next_code_id = 0

    def save_slot_state(self) -> None:
        return None

    # -- stats model: each condition contributes a fixed, known amount -------------
    def _stats(self) -> dict[str, Any]:
        dps = 100_000.0
        ehp = 200_000.0
        ele = 25_000.0
        if self.config.get("conditionStationary"):
            dps += 20_000
        if self.config.get("conditionBeenHitRecently"):
            ehp += 10_000
        if self.config.get("useEnduranceCharges"):
            ehp += 30_000
            ele += 3_000
        if self.flasks.get("Flask 1"):
            ele += 5_000
        if self.flasks.get("Flask 2"):
            dps += 1_000
        if self.gems[(3, 1)]["enabled"]:
            ehp += 7_000
        boss = self.config.get("enemyIsBoss")
        if boss == "Pinnacle":
            dps *= 0.9
        elif boss == "Uber":
            dps *= 0.7
        return {
            "CombinedDPS": dps,
            "TotalEHP": ehp,
            "Life": 5000,
            "PhysicalMaximumHitTaken": 9000,
            "FireMaximumHitTaken": ele,
            "ColdMaximumHitTaken": ele + 1,
            "LightningMaximumHitTaken": ele + 2,
            "ChaosMaximumHitTaken": 18000,
        }

    def _snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "config": self.config,
                "flasks": self.flasks,
                "gems": self.gems,
                "custom_mods": self.custom_mods,
            }
        )

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        self.batch_calls.append([(m, dict(p or {})) for m, p in calls])
        results: list[dict[str, Any]] = []
        for method, params in calls:
            if raise_on_error:
                results.append(self.call(method, params or {}))
            else:
                try:
                    results.append(self.call(method, params or {}))
                except Exception as exc:
                    results.append({"ok": False, "_error": str(exc), "error": str(exc)})
        return results

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "list_config":
            items = [{"var": k, "value": v} for k, v in self.config.items()]
            return {"sections": [{"items": items}], "customMods": [dict(b) for b in self.custom_mods]}
        if method == "reset_config":
            self.config[params["var"]] = self.defaults.get(params["var"])
            return {}
        if method == "set_config":
            self.config[params["var"]] = params["value"]
            return {}
        if method == "set_custom_mods":
            self.custom_mods = [dict(b) for b in params["list"]]
            return {}
        if method == "list_items":
            slots = [
                {"name": n, "isFlask": True, "active": a, "selItemId": i + 1}
                for i, (n, a) in enumerate(self.flasks.items())
            ]
            items = [{"id": i + 1, "name": f"Flask{i + 1}"} for i in range(len(self.flasks))]
            if self.has_mageblood:
                belt_id = len(items) + 1
                slots.append({"name": "Belt", "isFlask": False, "active": False, "selItemId": belt_id})
                items.append(
                    {"id": belt_id, "name": "Mageblood", "title": "Mageblood", "baseName": "Heavy Belt"}
                )
            return {"slots": slots, "items": items}
        if method == "set_slot_active":
            self.flasks[params["slotName"]] = params["active"]
            return {}
        if method == "list_skills":
            return {
                "groups": [
                    {
                        "index": 3,
                        "enabled": True,
                        "gems": [
                            {
                                "index": 1,
                                "name": "Molten Shell",
                                "nameSpec": "Molten Shell",
                                "enabled": self.gems[(3, 1)]["enabled"],
                            }
                        ],
                    }
                ]
            }
        if method == "set_gem":
            if "enabled" in params:
                self.gems[(params["group"], params["index"])]["enabled"] = params["enabled"]
            return {}
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method == "get_stats":
            return {"stats": self._stats()}
        if method == "get_sidebar":
            return {"warnings": []}
        if method in ("get_build_info", "get_build_header"):
            return {}
        if method == "export_code":
            snap = self._snapshot()
            for existing_code, existing_snap in self._saved.items():
                if existing_snap == snap:
                    return {"code": existing_code}
            self._next_code_id += 1
            code = f"CODE_{self._next_code_id}"
            self._saved[code] = snap
            return {"code": code}
        if method == "import_code":
            code = params["code"]
            snap = copy.deepcopy(self._saved[code])
            self.config = dict(snap["config"])
            self.flasks = dict(snap["flasks"])
            self.gems = {k: dict(v) for k, v in snap["gems"].items()}
            self.custom_mods = [dict(b) for b in snap["custom_mods"]]
            return {}
        raise AssertionError(f"unexpected bridge call {method}")


def test_dewater_default_strips_conditions_and_flasks() -> None:
    w = FakeWorker()
    stripped = dewater_build_in_place(w)
    assert w.config["conditionStationary"] is False
    assert w.config["useEnduranceCharges"] is False
    assert w.config["enemyIsBoss"] == "Pinnacle"
    assert w.config["bandit"] == "Eramir"  # permanent choice untouched
    assert w.flasks == {"Flask 1": False, "Flask 2": False}
    assert w.gems[(3, 1)]["enabled"] is False
    assert stripped["kept"] == []
    assert any("conditionStationary" in s for s in stripped["config"])


def test_dewater_keep_leaves_listed_items_untouched() -> None:
    w = FakeWorker()
    stripped = dewater_build_in_place(
        w, keep=["conditionStationary,useEnduranceCharges", "flask 1", "MOLTEN SHELL"]
    )
    assert w.config["conditionStationary"] is True
    assert w.config["useEnduranceCharges"] is True
    assert w.config["conditionBeenHitRecently"] is False
    assert w.flasks == {"Flask 1": True, "Flask 2": False}
    assert w.gems[(3, 1)]["enabled"] is True
    reset_vars = {p["var"] for m, p in w.calls if m == "reset_config"}
    assert "conditionStationary" not in reset_vars
    assert len(stripped["kept"]) == 4
    assert not any("conditionStationary" in s for s in stripped["config"])


def test_dewater_keep_all_flasks_and_custom_mods() -> None:
    w = FakeWorker()
    stripped = dewater_build_in_place(w, keep=["flasks", "customMods"])
    assert w.flasks == {"Flask 1": True, "Flask 2": True}
    assert w.custom_mods[0]["text"] == "+25 to maximum Mana"
    assert stripped["flasks"] == []


def test_records_restore_and_revert_are_exact_inverses() -> None:
    w = FakeWorker()
    original = w._snapshot()
    _stripped, records = dewater_build_with_records(w)
    keys = {r["key"] for r in records}
    assert {"conditionStationary", "conditionBeenHitRecently", "useEnduranceCharges"} <= keys
    assert {"Flask 1", "Flask 2", "customMods:Default", "Molten Shell"} <= keys

    dewatered = w._snapshot()
    for rec in records:
        for m, p in rec["restore"]:
            w.call(m, p)
    restored = w._snapshot()
    restored["config"]["enemyIsBoss"] = original["config"]["enemyIsBoss"]
    # The comment-only sentinel group stays; every user group is back as it was.
    restored["custom_mods"] = [b for b in restored["custom_mods"] if b["title"] != DEWATER_SENTINEL_TITLE]
    assert restored == original

    for rec in records:
        for m, p in rec["revert"]:
            w.call(m, p)
    assert w._snapshot() == dewatered


def test_attribute_water_measures_each_item_alone_and_reverts() -> None:
    w = FakeWorker()
    _stripped, records = dewater_build_with_records(w)
    base = capture_digest(w)
    before = w._snapshot()

    rows = {r["key"]: r for r in attribute_water(w, records, base, "CombinedDPS")}

    # Deltas are exactly what the (fake) engine reports after restoring one item.
    assert rows["conditionStationary"]["dpsDelta"] == pytest.approx(20_000 * 0.9)
    assert rows["conditionStationary"]["totalEhpDelta"] == 0
    assert rows["useEnduranceCharges"]["totalEhpDelta"] == 30_000
    assert rows["useEnduranceCharges"]["eleMaxHitDelta"] == 3_000
    assert rows["Flask 1"]["eleMaxHitDelta"] == 5_000
    assert rows["Molten Shell"]["totalEhpDelta"] == 7_000
    assert rows["conditionStationary"]["dpsPct"] == pytest.approx(20.0)
    # Build is back to the de-watered state afterwards.
    assert w._snapshot() == before


@pytest.mark.parametrize("base_enabled", [False, True])
def test_vaal_attribution_restores_original_base_skill_state(base_enabled: bool) -> None:
    """In real PoB (Data/Gems.lua & CalcSetup.lua:1831), enableGlobal1 is the Vaal burst
    skill and enableGlobal2 is the base skill; both dewatering and attribution must keep
    the original enableGlobal2 state untouched while toggling enableGlobal1.
    """

    class VaalWorker(FakeWorker):
        def __init__(self) -> None:
            super().__init__()
            self.gems[(3, 2)] = {
                "enabled": True,
                "enableGlobal1": True,  # Vaal burst (initially ON -> stripped)
                "enableGlobal2": base_enabled,  # Base skill (preserved as-is)
            }

        def _stats(self) -> dict[str, Any]:
            stats = super()._stats()
            vaal = self.gems[(3, 2)]
            stats["CombinedDPS"] += (
                (50_000 if vaal["enableGlobal1"] else 0)
                + (100_000 if vaal["enableGlobal2"] else 0)
            )
            return stats

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "list_skills":
                result = super().call(method, params)
                result["groups"][0]["gems"].append(
                    {
                        "index": 2,
                        "name": "Vaal Discipline",
                        "nameSpec": "Vaal Discipline",
                        "skillId": "VaalDiscipline",
                        **self.gems[(3, 2)],
                    }
                )
                return result
            if method == "set_gem" and params["index"] == 2:
                self.calls.append((method, params))
                self.gems[(3, 2)].update(
                    {k: params[k] for k in ("enableGlobal1", "enableGlobal2") if k in params}
                )
                return {}
            return super().call(method, params)

    w = VaalWorker()
    _stripped, records = dewater_build_with_records(w)
    vaal_record = next(r for r in records if r["key"] == "Vaal Discipline")
    dewatered = w._snapshot()
    assert w.gems[(3, 2)]["enabled"] is True
    assert w.gems[(3, 2)]["enableGlobal1"] is False
    assert w.gems[(3, 2)]["enableGlobal2"] is base_enabled

    row = attribute_water(w, [vaal_record], capture_digest(w), "CombinedDPS")[0]
    assert row["dpsDelta"] == pytest.approx(50_000)
    assert vaal_record["restore"][0][1]["enableGlobal1"] is True
    assert vaal_record["restore"][0][1]["enableGlobal2"] is base_enabled
    assert vaal_record["revert"][0][1]["enableGlobal1"] is False
    assert vaal_record["revert"][0][1]["enableGlobal2"] is base_enabled
    assert w._snapshot() == dewatered


@pytest.mark.parametrize("enabled", [False, True])
def test_added_gem_uses_pob_mcp_skill_defaults(enabled: bool) -> None:
    """Vaal gems default to Vaal burst OFF (enableGlobal1=False) and base ON (enableGlobal2=True);
    non-Vaal gems must NOT receive any set_gem call (which would disable their aura/active skill).
    """

    class AddedGemWorker:
        def __init__(self) -> None:
            self.gem: dict[str, Any] = {}
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, dict(params)))
            if method == "add_gem":
                self.gem = {
                    "index": 2,
                    "name": params.get("nameSpec", ""),
                    "enabled": params["enabled"],
                    "enableGlobal1": True,
                    "enableGlobal2": True,
                }
                return {"gem": dict(self.gem)}
            if method == "set_gem":
                assert params["group"] == 3 and params["index"] == 2
                self.gem.update({k: params[k] for k in ("enableGlobal1", "enableGlobal2")})
                return {"gem": dict(self.gem)}
            raise AssertionError(f"unexpected bridge call {method}")

    # Vaal gem: disable Vaal burst (enableGlobal1=False), keep base skill (enableGlobal2=True)
    w_vaal = AddedGemWorker()
    result_vaal = _add_gem_with_default_skill_flags(
        w_vaal, {"group": 3, "nameSpec": "Vaal Discipline", "enabled": enabled}
    )
    assert result_vaal["gem"] == {
        "index": 2,
        "name": "Vaal Discipline",
        "enabled": enabled,
        "enableGlobal1": False,
        "enableGlobal2": True,
    }
    assert [m for m, _ in w_vaal.calls] == ["add_gem", "set_gem"]

    # Non-Vaal gem: zero set_gem calls, enableGlobal1 stays True
    w_norm = AddedGemWorker()
    result_norm = _add_gem_with_default_skill_flags(
        w_norm, {"group": 3, "nameSpec": "Determination", "enabled": enabled}
    )
    assert result_norm["gem"]["enableGlobal1"] is True
    assert [m for m, _ in w_norm.calls] == ["add_gem"]


def test_config_list_values_and_boss_aliases_match_pob_mcp() -> None:
    class ConfigWorker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, params))
            if method == "list_config":
                return {
                    "sections": [
                        {
                            "items": [
                                {
                                    "var": "enemyIsBoss",
                                    "type": "list",
                                    "list": [
                                        {"val": "None", "label": "No"},
                                        {"val": "Boss", "label": "Standard Boss"},
                                        {"val": "Pinnacle", "label": "Guardian/Pinnacle Boss"},
                                    ],
                                },
                                {
                                    "var": "bandit",
                                    "type": "list",
                                    "list": [
                                        {"val": "None", "label": "Kill all"},
                                    ],
                                },
                                {"var": "usePowerCharges", "type": "check"},
                            ]
                        }
                    ]
                }
            if method in ("set_config", "reset_config"):
                return {}
            raise AssertionError(f"unexpected bridge call {method}")

    w = ConfigWorker()
    _apply_config_in_place(
        w,
        [
            ("enemyIsBoss", "None"),
            ("enemyIsBoss", "Shaper"),
            ("bandit", "Kill all"),
            ("usePowerCharges", "none"),
        ],
    )
    edits = [(method, params) for method, params in w.calls if method != "list_config"]
    assert edits == [
        ("set_config", {"var": "enemyIsBoss", "value": "None"}),
        ("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}),
        ("set_config", {"var": "bandit", "value": "None"}),
        ("reset_config", {"var": "usePowerCharges"}),
    ]


def test_attribute_water_reports_errors_per_item() -> None:
    w = FakeWorker()
    base = capture_digest(w)
    bad = {
        "dimension": "config",
        "key": "x",
        "label": "x",
        "restore": [("no_such_rpc", {})],
        "revert": [],
    }
    rows = attribute_water(w, [bad], base, "CombinedDPS")
    assert "error" in rows[0]


def test_audit_build_keep_and_attribution_and_zero_residue() -> None:
    w = FakeWorker()
    before = w._snapshot()

    res = audit_build(w, keep=["conditionStationary"])
    assert w._snapshot() == before
    assert w.last_good_code == "ORIGINAL_LAST_GOOD"
    assert any("conditionStationary" in k for k in res["inflationStripped"]["kept"])
    keys = {r["key"] for r in res["waterAttribution"]}
    assert "conditionStationary" not in keys
    assert "useEnduranceCharges" in keys
    # Keeping Stationary means the de-watered DPS still includes it.
    assert res["dewateredPinnacle"]["primaryDps"] == pytest.approx((100_000 + 20_000) * 0.9)

    res2 = audit_build(w, attribution=False)
    assert res2["waterAttribution"] == []
    assert res2["dewateredPinnacle"]["primaryDps"] == pytest.approx(100_000 * 0.9)
    assert w.last_good_code == "ORIGINAL_LAST_GOOD"


def test_run_matrix_forwards_keep_to_pinnacle() -> None:
    w = FakeWorker()
    before = w._snapshot()
    res = run_matrix(w, scenarios=["Original", "Pinnacle"], keep=["conditionStationary"])
    pin = res["scenarios"][1]
    assert pin["digest"]["primaryDps"] == pytest.approx((100_000 + 20_000) * 0.9)
    assert w._snapshot() == before


def test_fmt_signed_pct() -> None:
    from pob_cli.formatters import fmt_signed_pct

    assert fmt_signed_pct(None, None) == "-"
    assert fmt_signed_pct(0, 0) == "0"
    assert fmt_signed_pct(1234.4, 4.21) == "+1,234 (+4.2%)"
    assert fmt_signed_pct(-50, -1.0) == "-50 (-1.0%)"


def test_cli_audit_passes_keep_and_renders_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        seen["action"], seen["params"] = action, dict(params or {})
        return {
            "healthIssues": [],
            "inflationStripped": {
                "config": ["useEnduranceCharges=True -> reset"],
                "flasks": [],
                "skills": [],
                "party": [],
                "kept": ["conditionStationary=True (kept)"],
            },
            "waterAttribution": [
                {
                    "dimension": "config",
                    "key": "useEnduranceCharges",
                    "label": "useEnduranceCharges=True",
                    "dpsDelta": 0.0,
                    "dpsPct": 0.0,
                    "totalEhpDelta": 30000.0,
                    "totalEhpPct": 15.0,
                    "physMaxHitDelta": 0.0,
                    "physMaxHitPct": 0.0,
                    "eleMaxHitDelta": 3000.0,
                    "eleMaxHitPct": 12.0,
                }
            ],
            "original": {"primaryDpsKey": "CombinedDPS"},
            "dewateredPinnacle": {},
        }

    monkeypatch.setattr(cli_mod, "_daemon_call", fake_call)
    out = CliRunner().invoke(
        cli_mod.main, ["audit", "--keep", "conditionStationary", "--keep", "Flask 4"]
    )
    assert out.exit_code == 0, out.output
    assert seen["params"]["keep"] == ["conditionStationary", "Flask 4"]
    assert seen["params"].get("attribution", True) is True
    assert "Kept (--keep)" in out.output
    assert "+30,000 (+15.0%)" in out.output
    assert "ΔCombinedDPS" in out.output

    out2 = CliRunner().invoke(cli_mod.main, ["audit", "--no-attribution"])
    assert out2.exit_code == 0, out2.output
    assert seen["params"]["attribution"] is False


def test_attribute_water_flags_restore_that_pob_did_not_apply() -> None:
    """A silent no-op restore must not be reported as 'this item adds nothing'."""

    class NoOpWorker(FakeWorker):
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "set_config" and params.get("var") == "conditionStationary":
                self.calls.append((method, params))
                return {}  # accepted by the RPC, but the value never changes
            return super().call(method, params)

    w = NoOpWorker()
    _stripped, records = dewater_build_with_records(w)
    before = w._snapshot()
    rows = {r["key"]: r for r in attribute_water(w, records, capture_digest(w), "CombinedDPS")}

    bad = rows["conditionStationary"]
    assert "restore not applied" in bad["error"]
    assert "dpsDelta" not in bad
    # Other items are still measured normally and the baseline is intact.
    assert rows["useEnduranceCharges"]["totalEhpDelta"] == 30_000
    assert w._snapshot() == before


def test_attribute_water_stops_measuring_when_revert_leaves_baseline_dirty() -> None:
    class StickyWorker(FakeWorker):
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "set_slot_active" and params == {"slotName": "Flask 1", "active": False}:
                self.calls.append((method, params))
                return {}  # flask stays on after the first restore
            return super().call(method, params)

    w = StickyWorker()
    w.flasks["Flask 1"] = True
    _stripped, records = dewater_build_with_records(w)
    # Dewatering itself could not turn Flask 1 off either; force the de-watered state.
    w.flasks["Flask 1"] = False
    rows = attribute_water(w, records, capture_digest(w), "CombinedDPS")
    keys = [r["key"] for r in rows]
    i = keys.index("Flask 1")
    assert "revert not applied" in rows[i]["error"]
    for later in rows[i + 1 :]:
        assert later["error"] == "skipped: baseline not restored after Flask 1"
        assert "dpsDelta" not in later


def test_keep_typo_is_reported_and_item_is_still_stripped() -> None:
    w = FakeWorker()
    stripped = dewater_build_in_place(w, keep=["conditionStationery", "Flask 9", "flask 2"])
    assert w.config["conditionStationary"] is False  # typo did not protect it
    assert w.flasks["Flask 2"] is True
    rejected = stripped["keepRejected"]
    assert len(rejected) == 2
    assert any(
        r.startswith("conditionstationery") and "did you mean conditionStationary" in r
        for r in rejected
    )
    assert any(r.startswith("flask 9") for r in rejected)


def test_valid_keep_tokens_are_not_rejected() -> None:
    w = FakeWorker()
    stripped = dewater_build_in_place(
        w,
        keep=["conditionStationary", "flask 1", "MOLTEN SHELL", "flasks", "customMods", "party", "bandit"],
    )
    assert stripped["keepRejected"] == []


def test_cli_warns_on_unmatched_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    stripped = {
        "config": [],
        "flasks": [],
        "skills": [],
        "party": [],
        "kept": [],
        "keepRejected": ["conditionstationery: not a config var (did you mean conditionStationary?)"],
    }

    def fake_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if action == "audit":
            return {
                "healthIssues": [],
                "inflationStripped": stripped,
                "waterAttribution": [],
                "original": {},
                "dewateredPinnacle": {},
            }
        digest = {"primaryDps": 1.0, "totalEhp": 1.0, "stats": {}}
        return {
            "baseline": "Original",
            "scenarios": [
                {"name": "Original", "digest": digest, "applied": {"stripped": None}},
                {"name": "Pinnacle", "digest": digest, "applied": {"stripped": stripped}},
            ],
        }

    monkeypatch.setattr(cli_mod, "_daemon_call", fake_call)
    for args in (["audit", "--keep", "conditionStationery"], ["matrix", "--keep", "conditionStationery"]):
        out = CliRunner().invoke(cli_mod.main, args)
        assert out.exit_code == 0, out.output
        assert "--keep not matched (item was NOT kept)" in out.output
        assert "did you mean conditionStationary" in out.output


def test_custom_mods_disabled_per_group_without_overwriting_user_groups() -> None:
    w = FakeWorker()
    w.custom_mods = [
        {"title": "Default", "text": "+25 to maximum Mana", "enabled": True},
        {"title": "Boss buffs", "text": "10% more Damage\n# note", "enabled": True},
        {"title": "Off", "text": "+1 to Level of all Skill Gems", "enabled": False},
        {"title": "Notes", "text": "# only a comment", "enabled": True},
    ]
    original = [dict(b) for b in w.custom_mods]
    stripped, records = dewater_build_with_records(w, keep=["customMods:Boss buffs"])

    by_title = {b["title"]: b for b in w.custom_mods}
    # User groups keep their title and text; only the active, non-kept one is disabled.
    assert by_title["Default"] == {**original[0], "enabled": False}
    assert by_title["Boss buffs"] == original[1]
    assert by_title["Off"] == original[2]
    assert by_title["Notes"] == original[3]
    # Sentinel keeps an enabled block with text so PoB does not fall back to input.customMods.
    assert by_title[DEWATER_SENTINEL_TITLE] == {
        "title": DEWATER_SENTINEL_TITLE,
        "text": "# disabled",
        "enabled": True,
    }
    assert any("customMods[Boss buffs]" in k for k in stripped["kept"])
    assert stripped["keepRejected"] == []
    cm = [r for r in records if r["key"].startswith("customMods")]
    assert [r["key"] for r in cm] == ["customMods:Default"]

    dewatered = w._snapshot()
    rows = attribute_water(w, cm, capture_digest(w), "CombinedDPS")
    assert "error" not in rows[0]
    assert w._snapshot() == dewatered


def test_audit_reports_build_not_restored() -> None:
    class BrokenRestoreWorker(FakeWorker):
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "import_code":
                self.calls.append((method, params))
                return {}  # rollback silently does nothing
            if method == "export_code":
                self.calls.append((method, params))
                return {"code": repr(sorted(self._snapshot()["config"].items(), key=str))}
            return super().call(method, params)

    w = BrokenRestoreWorker()
    res = audit_build(w)
    chk = res["restoreCheck"]
    assert chk["verified"] is False
    assert "CombinedDPS" in chk["differing"]

    w2 = FakeWorker()
    assert audit_build(w2)["restoreCheck"]["verified"] is True
    assert run_matrix(w2, scenarios=["Original", "Pinnacle"])["restoreCheck"]["verified"] is True


def test_restore_check_accepts_identical_stats_with_different_code() -> None:
    w = FakeWorker()
    before = capture_digest(w)

    class OtherCode(FakeWorker):
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "export_code":
                return {"code": "DIFFERENT"}
            return super().call(method, params)

    w3 = OtherCode()
    chk = verify_restored(w3, "CODE_1", before)
    assert chk["verified"] is True
    assert "recalculates identical" in chk["detail"]


def test_cli_warns_when_build_not_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    chk = {"verified": False, "detail": "2 stat(s) differ from before the run (e.g. CombinedDPS)"}

    def fake_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if action == "audit":
            return {
                "healthIssues": [],
                "inflationStripped": {"config": [], "flasks": [], "skills": [], "party": [], "kept": []},
                "waterAttribution": [],
                "original": {},
                "dewateredPinnacle": {},
                "restoreCheck": chk,
            }
        digest = {"primaryDps": 1.0, "totalEhp": 1.0, "stats": {}}
        return {
            "baseline": "Original",
            "scenarios": [{"name": "Original", "digest": digest, "applied": {"stripped": None}}],
            "restoreCheck": chk,
        }

    monkeypatch.setattr(cli_mod, "_daemon_call", fake_call)
    for args in (["audit"], ["matrix"]):
        out = CliRunner().invoke(cli_mod.main, args)
        assert out.exit_code == 0, out.output
        assert "build not verified as restored" in out.output


# --- Wave 2A / 84169b2 architecture & regression coverage -----------------------------------


def test_dewater_and_attribution_use_call_batch_pipelining() -> None:
    w = FakeWorker()
    _stripped, records = dewater_build_with_records(w)

    # dewater_build_with_records: 1 read batch (4 RPCs) + 1 mutation batch, zero unbatched calls.
    assert len(w.batch_calls) == 2
    assert [m for m, _ in w.batch_calls[0]] == [
        "list_config",
        "list_items",
        "list_skills",
        "get_party",
    ]
    assert len(w.calls) == sum(len(b) for b in w.batch_calls)

    base = capture_digest(w)
    w.calls.clear()
    w.batch_calls.clear()

    rows = attribute_water(w, records, base, "CombinedDPS")
    assert len(rows) == len(records)
    assert all("error" not in r for r in rows)

    # attribute_water: 1 deduplicated pre-probe batch + 1 pipelined batch per record.
    assert len(w.batch_calls) == 1 + len(records)
    assert [m for m, _ in w.batch_calls[0]] == ["list_config", "list_items", "list_skills"]
    for item_batch in w.batch_calls[1:]:
        methods = [m for m, _ in item_batch]
        assert "get_stats" in methods
    assert len(w.calls) == sum(len(b) for b in w.batch_calls)


def test_run_matrix_three_scenarios_caches_pinnacle_and_preserves_last_good_code() -> None:
    w = FakeWorker()
    w.last_good_code = "SENTINEL_LAST_GOOD"
    before = w._snapshot()

    res = run_matrix(w, scenarios=["Original", "Pinnacle", "Uber"], keep=["conditionStationary"])

    # Pinnacle dewatering runs only ONCE and its exported snapshot is reused for Uber.
    dewater_read_batches = [
        b for b in w.batch_calls if [m for m, _ in b] == ["list_config", "list_items", "list_skills", "get_party"]
    ]
    reset_calls = [c for c in w.calls if c[0] == "reset_config"]
    assert len(dewater_read_batches) == 1
    assert {p["var"] for _, p in reset_calls} == {"conditionBeenHitRecently", "useEnduranceCharges"}
    assert len(reset_calls) == 2

    by_name = {sc["name"]: sc for sc in res["scenarios"]}
    assert by_name["Original"]["digest"]["primaryDps"] == pytest.approx(121_000.0)
    assert by_name["Pinnacle"]["digest"]["primaryDps"] == pytest.approx((100_000 + 20_000) * 0.9)
    assert by_name["Uber"]["digest"]["primaryDps"] == pytest.approx((100_000 + 20_000) * 0.7)

    # Cached `kept` propagates to both Pinnacle and Uber scenarios.
    for sc_name in ("Pinnacle", "Uber"):
        kept = by_name[sc_name]["applied"]["stripped"]["kept"]
        assert any("conditionStationary" in k for k in kept)

    assert res["restoreCheck"]["verified"] is True
    assert w.last_good_code == "SENTINEL_LAST_GOOD"
    assert w._snapshot() == before


def test_verify_restored_catches_inf_and_nan_stat_corruption() -> None:
    class CustomStatsWorker(FakeWorker):
        def __init__(self, stats_override: dict[str, Any]) -> None:
            super().__init__()
            self._stats_override = stats_override

        def _stats(self) -> dict[str, Any]:
            return dict(self._stats_override)

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "export_code":
                return {"code": "DIFFERENT_AFTER_CODE"}
            return super().call(method, params)

    # Bug #1: inf -> finite must NOT be treated as identical
    chk_inf_to_fin = verify_restored(
        CustomStatsWorker({"CombinedDPS": 100_000.0, "TotalEHP": 50_000.0}),
        "BEFORE_CODE",
        {"stats": {"CombinedDPS": 100_000.0, "TotalEHP": "inf"}},
    )
    assert chk_inf_to_fin["verified"] is False
    assert "TotalEHP" in chk_inf_to_fin["differing"]

    # finite -> inf
    chk_fin_to_inf = verify_restored(
        CustomStatsWorker({"CombinedDPS": 100_000.0, "TotalEHP": float("inf")}),
        "BEFORE_CODE",
        {"stats": {"CombinedDPS": 100_000.0, "TotalEHP": 50_000.0}},
    )
    assert chk_fin_to_inf["verified"] is False
    assert "TotalEHP" in chk_fin_to_inf["differing"]

    # inf -> -inf
    chk_sign_flip = verify_restored(
        CustomStatsWorker({"TotalEHP": "-inf"}),
        "BEFORE_CODE",
        {"stats": {"TotalEHP": "inf"}},
    )
    assert chk_sign_flip["verified"] is False
    assert "TotalEHP" in chk_sign_flip["differing"]

    # inf -> inf is identical
    chk_inf_same = verify_restored(
        CustomStatsWorker({"CombinedDPS": 100_000.0, "TotalEHP": "inf"}),
        "BEFORE_CODE",
        {"stats": {"CombinedDPS": 100_000.0, "TotalEHP": float("inf")}},
    )
    assert chk_inf_same["verified"] is True

    # nan -> finite must fail; nan -> nan passes
    chk_nan_to_fin = verify_restored(
        CustomStatsWorker({"PhysicalMaximumHitTaken": 12_000.0}),
        "BEFORE_CODE",
        {"stats": {"PhysicalMaximumHitTaken": "nan"}},
    )
    assert chk_nan_to_fin["verified"] is False
    assert "PhysicalMaximumHitTaken" in chk_nan_to_fin["differing"]

    chk_nan_same = verify_restored(
        CustomStatsWorker({"PhysicalMaximumHitTaken": float("nan")}),
        "BEFORE_CODE",
        {"stats": {"PhysicalMaximumHitTaken": "nan"}},
    )
    assert chk_nan_same["verified"] is True


def test_attribute_water_json_safe_on_inf_and_nan_stats() -> None:
    """Bug #2: 'inf'/'nan' PoB stats (e.g. CI/Immortal EHP) must emit JSON-safe None deltas/pcts."""

    class NonFiniteStatsWorker(FakeWorker):
        def _stats(self) -> dict[str, Any]:
            stats = super()._stats()
            stats["TotalEHP"] = "inf"
            stats["PhysicalMaximumHitTaken"] = "nan"
            stats["FireMaximumHitTaken"] = "inf"
            stats["ColdMaximumHitTaken"] = "inf"
            stats["LightningMaximumHitTaken"] = "inf"
            return stats

    w = NonFiniteStatsWorker()
    _stripped, records = dewater_build_with_records(w)
    base = capture_digest(w)
    rows = attribute_water(w, records, base, "CombinedDPS")
    by_key = {r["key"]: r for r in rows}

    st_row = by_key["conditionStationary"]
    assert st_row["dpsDelta"] == pytest.approx(20_000 * 0.9)
    assert st_row["dpsPct"] == pytest.approx(20.0)
    assert st_row["totalEhp"] == "inf"
    assert st_row["totalEhpDelta"] is None
    assert st_row["totalEhpPct"] is None
    assert st_row["physMaxHit"] is None
    assert st_row["physMaxHitDelta"] is None
    assert st_row["physMaxHitPct"] is None
    assert st_row["eleMaxHit"] == "inf"
    assert st_row["eleMaxHitDelta"] is None
    assert st_row["eleMaxHitPct"] is None

    # Strict RFC-8259 JSON serialization must never emit raw NaN/Infinity tokens.
    serialized = json.dumps(rows, allow_nan=False)
    assert "NaN" not in serialized
    assert "Infinity" not in serialized


def test_apply_config_edge_cases_and_keep_rejected_exclusions() -> None:
    class EdgeConfigWorker:
        def __init__(self, preset_boss_skills: Any) -> None:
            self.preset_boss_skills = preset_boss_skills
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, dict(params)))
            if method == "list_config":
                return {
                    "sections": [
                        {
                            "items": [
                                {
                                    "var": "enemyIsBoss",
                                    "type": "list",
                                    "list": [
                                        {"val": "None", "label": "No"},
                                        {"val": "Pinnacle", "label": "Guardian/Pinnacle Boss"},
                                    ],
                                },
                                {
                                    "var": "resistancePenalty",
                                    "type": "list",
                                    "list": [
                                        {"val": 0, "label": "None"},
                                        {"val": -30, "label": "Act 5 (-30%)"},
                                        {"val": -60, "label": "Act 10 (-60%)"},
                                    ],
                                },
                                {
                                    "var": "presetBossSkills",
                                    "type": "list",
                                    "value": self.preset_boss_skills,
                                    "list": [
                                        {"val": "None", "label": "None"},
                                        {"val": "ShaperBeam", "label": "Shaper Beam"},
                                    ],
                                },
                            ]
                        }
                    ]
                }
            if method in ("set_config", "reset_config"):
                return {}
            raise AssertionError(f"unexpected bridge call {method}")

    # 1. enemyIsBoss="No" -> "None" (NOT bool False), resistancePenalty="None" -> 0,
    #    and presetBossSkills="None" produces NO spurious re-apply set_config.
    w_none = EdgeConfigWorker(preset_boss_skills="None")
    _apply_config_in_place(
        w_none,
        [("enemyIsBoss", "No"), ("resistancePenalty", "None")],
    )
    edits_none = [(m, p) for m, p in w_none.calls if m != "list_config"]
    assert edits_none == [
        ("set_config", {"var": "enemyIsBoss", "value": "None"}),
        ("set_config", {"var": "resistancePenalty", "value": 0}),
    ]

    # Contrast: active presetBossSkills IS re-applied after enemyIsBoss changes (R12).
    w_active = EdgeConfigWorker(preset_boss_skills="ShaperBeam")
    _apply_config_in_place(w_active, [("enemyIsBoss", "Pinnacle")])
    edits_active = [(m, p) for m, p in w_active.calls if m != "list_config"]
    assert edits_active == [
        ("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"}),
        ("set_config", {"var": "presetBossSkills", "value": "ShaperBeam"}),
    ]

    # 2. --keep enemyIsBoss keeps original boss setting without rejecting token.
    w_keep_boss = FakeWorker()
    stripped_boss = dewater_build_in_place(w_keep_boss, keep=["enemyIsBoss"])
    assert w_keep_boss.config["enemyIsBoss"] == "None"
    assert not any(
        m == "set_config" and p.get("var") == "enemyIsBoss" for m, p in w_keep_boss.calls
    )
    assert any("enemyIsBoss='None' (kept)" in k for k in stripped_boss["kept"])
    assert stripped_boss["keepRejected"] == []

    # 3. Mageblood Flask 1 and disabled customMods:Off are recognized known names (not keepRejected).
    w_mb = FakeWorker()
    w_mb.has_mageblood = True
    w_mb.custom_mods = [
        {"title": "Default", "text": "+25 to maximum Mana", "enabled": True},
        {"title": "Off", "text": "+1 to Level of all Skill Gems", "enabled": False},
    ]
    stripped_mb = dewater_build_in_place(w_mb, keep=["Flask 1", "customMods:Off"])
    assert w_mb.flasks == {"Flask 1": True, "Flask 2": True}
    assert stripped_mb["flasks"] == []
    assert stripped_mb["keepRejected"] == []


def test_batch_return_audit_vs_audit_full_with_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct run_batch on FakeWorker: `audit` defaults to attribution=False, `audit:full` to True.
    w = FakeWorker()
    before = w._snapshot()
    ops = [{"op": "flask", "index": 2, "active": False}]

    res_fast = run_batch(
        w, ops, apply=False, return_sections=["audit"], keep=["conditionStationary"]
    )
    assert w._snapshot() == before
    aud_fast = res_fast["returns"]["audit"]
    assert aud_fast["waterAttribution"] == []
    assert any("conditionStationary" in k for k in aud_fast["inflationStripped"]["kept"])
    assert aud_fast["dewateredPinnacle"]["primaryDps"] == pytest.approx((100_000 + 20_000) * 0.9)

    res_full = run_batch(
        w, ops, apply=False, return_sections=["audit:full"], keep=["conditionStationary"]
    )
    assert w._snapshot() == before
    aud_full = res_full["returns"]["audit"]
    attr_keys = {r["key"] for r in aud_full["waterAttribution"]}
    assert "conditionStationary" not in attr_keys
    assert "useEnduranceCharges" in attr_keys
    assert any("conditionStationary" in k for k in aud_full["inflationStripped"]["kept"])

    with pytest.raises(ValueError, match="Unsupported audit return modifier"):
        run_batch(w, ops, apply=False, return_sections=["audit:bogus"])

    # CLI forwarding for `pob batch --return audit` / `audit:full` with `--keep`
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_call(action: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        seen.append((action, dict(params or {})))
        return {
            "applied": False,
            "mode": "WHAT-IF",
            "detail": {"steps": [{"step": 1, "op": "flask"}]},
            "before": {"primaryDpsKey": "CombinedDPS", "primaryDps": 120_000.0, "totalEhp": 200_000.0, "life": 5000.0},
            "after": {"primaryDpsKey": "CombinedDPS", "primaryDps": 119_000.0, "totalEhp": 200_000.0, "life": 5000.0},
            "diff": {"deltas": []},
            "returns": {
                "audit": {
                    "inflationStripped": {
                        "config": [],
                        "flasks": [],
                        "skills": [],
                        "party": [],
                        "kept": ["conditionStationary=True (kept)"],
                        "keepRejected": ["conditionstationery: not a config var (did you mean conditionStationary?)"],
                    },
                    "restoreCheck": {"verified": False, "detail": "1 stat(s) differ"},
                }
            },
        }

    monkeypatch.setattr(cli_mod, "_daemon_call", fake_call)
    runner = CliRunner()
    out_fast = runner.invoke(
        cli_mod.main,
        [
            "batch",
            "--json-ops",
            '[{"op": "flask", "index": 2, "active": false}]',
            "--return",
            "audit",
            "--keep",
            "conditionStationary",
        ],
    )
    assert out_fast.exit_code == 0, out_fast.output
    assert seen[-1][0] == "batch"
    assert seen[-1][1]["return_sections"] == ["audit"]
    assert seen[-1][1]["keep"] == ["conditionStationary"]
    assert "build not verified as restored" in out_fast.output
    assert "--keep not matched" in out_fast.output

    out_full = runner.invoke(
        cli_mod.main,
        [
            "batch",
            "--json-ops",
            '[{"op": "flask", "index": 2, "active": false}]',
            "--return",
            "audit:full",
            "--keep",
            "conditionStationary",
            "--keep",
            "Flask 1",
        ],
    )
    assert out_full.exit_code == 0, out_full.output
    assert seen[-1][1]["return_sections"] == ["audit:full"]
    assert seen[-1][1]["keep"] == ["conditionStationary", "Flask 1"]


# --- Wave 4 regression coverage: Vaal main skills, hybrid Vaal RF/Impurity, default config, polish ---


@pytest.mark.parametrize("orig_g1", [True, False])
def test_vaal_main_skill_switches_main_active_skill_and_restores_in_attribution(orig_g1: bool) -> None:
    """Non-hasGlobalEffect Vaal main skills (e.g. Vaal Ice Nova) ignore enableGlobal1 in PoB
    CalcSetup.lua:1831; dewatering must also switch mainActiveSkill to the base skill (1 -> 2)
    and attribution must restore/verify mainActiveSkill back to 1 and revert to 2.
    """

    class VaalMainSkillWorker(FakeWorker):
        def __init__(self) -> None:
            super().__init__()
            self.main_active_skill = 1
            self.gems[(1, 1)] = {
                "enabled": True,
                "enableGlobal1": orig_g1,
                "enableGlobal2": True,
            }

        def _snapshot(self) -> dict[str, Any]:
            snap = super()._snapshot()
            snap["main_active_skill"] = self.main_active_skill
            return snap

        def _stats(self) -> dict[str, Any]:
            stats = super()._stats()
            # Simulates CalcSetup.lua:1963: CombinedDPS follows mainActiveSkill (1=Vaal, 2=Base)
            stats["CombinedDPS"] += 300_000 if self.main_active_skill == 1 else 120_000
            return stats

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "list_skills":
                result = super().call(method, params)
                result["mainSocketGroup"] = 1
                result["groups"].insert(
                    0,
                    {
                        "index": 1,
                        "enabled": True,
                        "isMain": True,
                        "mainActiveSkill": self.main_active_skill,
                        "skills": [
                            {"index": 1, "name": "Vaal Ice Nova", "nameZh": "瓦尔：冰霜新星"},
                            {"index": 2, "name": "Ice Nova", "nameZh": "冰霜新星"},
                        ],
                        "gems": [
                            {
                                "index": 1,
                                "name": "Vaal Ice Nova",
                                "nameSpec": "Vaal Ice Nova",
                                "skillId": "VaalIceNova",
                                "hasGlobalEffect": False,
                                **self.gems[(1, 1)],
                            }
                        ],
                    },
                )
                return result
            if method == "set_gem" and params["group"] == 1:
                self.calls.append((method, params))
                self.gems[(1, 1)].update(
                    {k: params[k] for k in ("enabled", "enableGlobal1", "enableGlobal2") if k in params}
                )
                return {}
            if method == "set_group" and params["index"] == 1:
                self.calls.append((method, params))
                if "mainActiveSkill" in params:
                    self.main_active_skill = int(params["mainActiveSkill"])
                return {}
            if method == "import_code":
                snap = copy.deepcopy(self._saved[params["code"]])
                self.main_active_skill = snap["main_active_skill"]
                return super().call(method, params)
            return super().call(method, params)

    w = VaalMainSkillWorker()
    stripped, records = dewater_build_with_records(w)
    assert w.main_active_skill == 2
    assert w.gems[(1, 1)]["enableGlobal1"] is False
    assert w.gems[(1, 1)]["enableGlobal2"] is True
    assert any("mainActiveSkill=2" in s for s in stripped["skills"])

    vaal_rec = next(r for r in records if r["key"] == "Vaal Ice Nova")
    assert "mainActiveSkill" in vaal_rec["probe"]["fields"]
    assert vaal_rec["original"] == {
        "enabled": True,
        "enableGlobal1": orig_g1,
        "enableGlobal2": True,
        "mainActiveSkill": 1,
    }
    assert ("set_group", {"index": 1, "mainActiveSkill": 1}) in vaal_rec["restore"]
    assert ("set_group", {"index": 1, "mainActiveSkill": 2}) in vaal_rec["revert"]

    dewatered = w._snapshot()
    row = attribute_water(w, [vaal_rec], capture_digest(w), "CombinedDPS")[0]
    assert "error" not in row
    assert row["dpsDelta"] == pytest.approx(180_000)
    assert w._snapshot() == dewatered


def test_vaal_righteous_fire_and_impurity_keep_permanent_base_skill() -> None:
    """Vaal Righteous Fire and Vaal Impurity must use the Vaal hybrid branch instead of
    TEMPORARY_SKILL_PATTERNS so permanent base Righteous Fire / Purity auras stay enabled.
    """

    class VaalRfWorker(FakeWorker):
        def __init__(self, rf_g1: bool, imp_g1: bool) -> None:
            super().__init__()
            self.gems[(1, 1)] = {"enabled": True, "enableGlobal1": rf_g1, "enableGlobal2": True}
            self.gems[(2, 1)] = {"enabled": True, "enableGlobal1": imp_g1, "enableGlobal2": True}

        def _stats(self) -> dict[str, Any]:
            stats = super()._stats()
            rf = self.gems[(1, 1)]
            if rf["enabled"]:
                stats["CombinedDPS"] += (80_000 if rf["enableGlobal1"] else 0) + (
                    200_000 if rf["enableGlobal2"] else 0
                )
            imp = self.gems[(2, 1)]
            if imp["enabled"]:
                stats["TotalEHP"] += (40_000 if imp["enableGlobal1"] else 0) + (
                    60_000 if imp["enableGlobal2"] else 0
                )
            return stats

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "list_skills":
                result = super().call(method, params)
                result["mainSocketGroup"] = 1
                result["groups"] = [
                    {
                        "index": 1,
                        "enabled": True,
                        "isMain": True,
                        "mainActiveSkill": 1,
                        "skills": (
                            [{"index": 1, "name": "Vaal Righteous Fire"}, {"index": 2, "name": "Righteous Fire"}]
                            if self.gems[(1, 1)]["enableGlobal1"]
                            else [{"index": 1, "name": "Righteous Fire"}]
                        ),
                        "gems": [
                            {
                                "index": 1,
                                "name": "Vaal Righteous Fire",
                                "nameSpec": "Vaal Righteous Fire",
                                "nameZh": "瓦尔：正义之火",
                                "skillId": "VaalRighteousFire",
                                "hasGlobalEffect": True,
                                **self.gems[(1, 1)],
                            }
                        ],
                    },
                    {
                        "index": 2,
                        "enabled": True,
                        "isMain": False,
                        "mainActiveSkill": 1,
                        "gems": [
                            {
                                "index": 1,
                                "name": "Vaal Impurity of Fire",
                                "nameSpec": "Vaal Impurity of Fire",
                                "nameZh": "瓦尔：火焰不净",
                                "skillId": "VaalFireImpurity",
                                "hasGlobalEffect": True,
                                **self.gems[(2, 1)],
                            }
                        ],
                    },
                ] + result["groups"]
                return result
            if method == "set_gem" and params["group"] in (1, 2):
                self.calls.append((method, params))
                key = (params["group"], params["index"])
                self.gems[key].update(
                    {k: params[k] for k in ("enabled", "enableGlobal1", "enableGlobal2") if k in params}
                )
                return {}
            return super().call(method, params)

    # Case 1: Only base RF / Purity active (enableGlobal1=False) -> untouched, NOT stripped!
    w_base_only = VaalRfWorker(rf_g1=False, imp_g1=False)
    stripped0, records0 = dewater_build_with_records(w_base_only)
    assert w_base_only.gems[(1, 1)] == {"enabled": True, "enableGlobal1": False, "enableGlobal2": True}
    assert w_base_only.gems[(2, 1)] == {"enabled": True, "enableGlobal1": False, "enableGlobal2": True}
    assert not any("正义之火" in s or "Righteous Fire" in s for s in stripped0["skills"])
    assert not any("不净" in s or "Impurity" in s for s in stripped0["skills"])
    assert not any(r["key"] in ("瓦尔：正义之火", "瓦尔：火焰不净") for r in records0)

    # Case 2: Vaal bursts active -> ONLY enableGlobal1 stripped, base enabled=True & enableGlobal2=True kept!
    w_burst = VaalRfWorker(rf_g1=True, imp_g1=True)
    _stripped1, records1 = dewater_build_with_records(w_burst)
    assert w_burst.gems[(1, 1)] == {"enabled": True, "enableGlobal1": False, "enableGlobal2": True}
    assert w_burst.gems[(2, 1)] == {"enabled": True, "enableGlobal1": False, "enableGlobal2": True}
    rf_rec = next(r for r in records1 if r["key"] == "瓦尔：正义之火")
    imp_rec = next(r for r in records1 if r["key"] == "瓦尔：火焰不净")
    rows = {r["key"]: r for r in attribute_water(w_burst, [rf_rec, imp_rec], capture_digest(w_burst), "CombinedDPS")}
    assert "error" not in rows["瓦尔：正义之火"]
    assert rows["瓦尔：正义之火"]["dpsDelta"] == pytest.approx(80_000)
    assert "error" not in rows["瓦尔：火焰不净"]
    assert rows["瓦尔：火焰不净"]["totalEhpDelta"] == pytest.approx(40_000)


def test_default_and_noop_config_vars_are_not_stripped_or_attributed() -> None:
    """presetBossSkills='None', isDefault=True items, PoB truthy defaultState vars, and
    no-op reset_config vars must not be falsely stripped or emit phantom attribution rows.
    """

    class DefaultCfgWorker(FakeWorker):
        def __init__(self) -> None:
            super().__init__()
            self.config["presetBossSkills"] = "None"
            self.config["conditionCorruptingCryStages"] = 1  # PoB defaultState=1
            self.config["conditionChampionIntimidate"] = True  # PoB defaultState=true
            self.config["conditionSacrificeMinion"] = True  # PoB defaultState=true
            self.config["conditionCustomDefaultFlag"] = True  # marked isDefault=True
            self.config["conditionFutureNoopDefault"] = 3  # reset_config leaves unchanged
            self.defaults["conditionFutureNoopDefault"] = 3

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "list_config":
                res = super().call(method, params)
                for it in res["sections"][0]["items"]:
                    if it["var"] == "conditionCustomDefaultFlag":
                        it["isDefault"] = True
                return res
            return super().call(method, params)

    w = DefaultCfgWorker()
    stripped, records = dewater_build_with_records(w)
    reset_vars = {p["var"] for m, p in w.calls if m == "reset_config"}
    stripped_cfg_text = "\n".join(stripped["config"])
    rec_keys = {r["key"] for r in records}

    for skipped_var in (
        "presetBossSkills",
        "conditionCorruptingCryStages",
        "conditionChampionIntimidate",
        "conditionSacrificeMinion",
        "conditionCustomDefaultFlag",
    ):
        assert skipped_var not in reset_vars
        assert skipped_var not in stripped_cfg_text
        assert skipped_var not in rec_keys

    # No redundant second enemyIsBoss set_config when presetBossSkills was 'None'
    boss_sets = [p for m, p in w.calls if m == "set_config" and p.get("var") == "enemyIsBoss"]
    assert len(boss_sets) == 1

    # Unknown future var whose reset_config is a no-op produces zero attribution row
    rows = attribute_water(w, records, capture_digest(w), "CombinedDPS")
    assert all(r["key"] != "conditionFutureNoopDefault" for r in rows)

    # Non-default Corrupting Cry stacks (5 != 1) ARE stripped and attributed
    w2 = DefaultCfgWorker()
    w2.config["conditionCorruptingCryStages"] = 5
    w2.defaults["conditionCorruptingCryStages"] = 1
    stripped2, records2 = dewater_build_with_records(w2)
    assert any("conditionCorruptingCryStages=5 -> reset" in s for s in stripped2["config"])
    assert any(r["key"] == "conditionCorruptingCryStages" for r in records2)
    assert w2.config["conditionCorruptingCryStages"] == 1


def test_enemy_is_boss_uber_logged_when_stripped_to_pinnacle() -> None:
    w = FakeWorker()
    w.config["enemyIsBoss"] = "Uber"
    stripped = dewater_build_in_place(w)
    assert w.config["enemyIsBoss"] == "Pinnacle"
    assert "enemyIsBoss='Uber' -> 'Pinnacle'" in stripped["config"]


def test_custom_mods_color_escaped_title_matched_by_clean_keep_token() -> None:
    w = FakeWorker()
    w.custom_mods = [
        {"title": "^xFF8800Boss buffs", "text": "10% more Damage", "enabled": True},
        {"title": "^x00FF00Extra", "text": "+50 to maximum Life", "enabled": True},
    ]
    stripped, records = dewater_build_with_records(w, keep=["customMods:Boss buffs"])
    by_title = {b["title"]: b for b in w.custom_mods}
    assert by_title["^xFF8800Boss buffs"]["enabled"] is True
    assert by_title["^x00FF00Extra"]["enabled"] is False
    assert any("customMods[Boss buffs]" in k for k in stripped["kept"])
    assert stripped["keepRejected"] == []
    cm_records = [r for r in records if r["key"].startswith("customMods:")]
    assert [r["key"] for r in cm_records] == ["customMods:Extra"]


def test_verify_restored_empty_before_code_falls_through_to_stat_check() -> None:
    class EmptyExportWorker(FakeWorker):
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "export_code":
                return {"code": ""}
            return super().call(method, params)

    w = EmptyExportWorker()
    chk = verify_restored(w, "", {"stats": {"CombinedDPS": 999_999.0}})
    assert chk["verified"] is False
    assert "CombinedDPS" in chk["differing"]


def test_resolve_skill_group_index_validates_main_group_and_empty_spec() -> None:
    with pytest.raises(ValueError, match="no skill groups"):
        resolve_skill_group_index({"groups": []}, "main")
    # Out-of-range mainSocketGroup falls back cleanly to first group instead of StopIteration
    skills_stale = {"mainSocketGroup": 9, "groups": [{"index": 2, "label": "Arc"}]}
    assert resolve_skill_group_index(skills_stale, "main") == 2
    assert resolve_skill_group_index(skills_stale, "") == 2
    assert resolve_skill_group_index(skills_stale, None) == 2


def test_run_matrix_uber_reapplies_kept_preset_boss_skills() -> None:
    w = FakeWorker()
    w.config["presetBossSkills"] = "ShaperBeam"
    run_matrix(w, scenarios=["Original", "Uber"], keep=["presetBossSkills"])
    boss_and_preset = [
        (p["var"], p["value"])
        for m, p in w.calls
        if m == "set_config" and p.get("var") in ("enemyIsBoss", "presetBossSkills")
    ]
    assert ("enemyIsBoss", "Uber") in boss_and_preset
    uber_idx = boss_and_preset.index(("enemyIsBoss", "Uber"))
    assert ("presetBossSkills", "ShaperBeam") in boss_and_preset[uber_idx + 1 :]
