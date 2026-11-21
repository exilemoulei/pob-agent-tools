"""Unit tests for de-watering keep-list and per-item PoB water attribution.

These use a scripted fake bridge worker so they run on every platform without the
PoB engine. The fake derives its stats from the current config/flask/gem state, the
same way the real engine would, so the tests check that every reported number is
read back from a recalculation (never estimated) and that the build is restored.
"""

from __future__ import annotations

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
)
from pob_cli.comparator import run_matrix
from pob_cli.mutators import _add_gem_with_default_skill_flags, _apply_config_in_place
from pob_cli.snapshot import capture_digest


class FakeWorker:
    """Minimal stand-in for BridgeWorker with state-dependent stats."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.config: dict[str, Any] = {
            "bandit": "Eramir",
            "enemyIsBoss": "None",
            "conditionStationary": True,  # e.g. Chieftain's Ramako needs this
            "conditionBeenHitRecently": True,
            "useEnduranceCharges": True,
            "presetBossSkills": None,
        }
        self.defaults = {
            "conditionStationary": False,
            "conditionBeenHitRecently": False,
            "useEnduranceCharges": False,
        }
        self.flasks = {"Flask 1": True, "Flask 2": True}
        self.gems = {(3, 1): {"enabled": True}}
        self.custom_mods = [{"title": "Default", "text": "+25 to maximum Mana", "enabled": True}]
        self.party_cleared = False

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
        if self.config.get("enemyIsBoss") == "Pinnacle":
            dps *= 0.9
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
        import copy

        return copy.deepcopy(
            {
                "config": self.config,
                "flasks": self.flasks,
                "gems": self.gems,
                "custom_mods": self.custom_mods,
            }
        )

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "list_config":
            items = [{"var": k, "value": v} for k, v in self.config.items()]
            return {"sections": [{"items": items}], "customMods": list(self.custom_mods)}
        if method == "reset_config":
            self.config[params["var"]] = self.defaults.get(params["var"])
            return {}
        if method == "set_config":
            self.config[params["var"]] = params["value"]
            return {}
        if method == "set_custom_mods":
            self.custom_mods = list(params["list"])
            return {}
        if method == "list_items":
            slots = [
                {"name": n, "isFlask": True, "active": a, "selItemId": i + 1}
                for i, (n, a) in enumerate(self.flasks.items())
            ]
            items = [{"id": i + 1, "name": f"Flask{i + 1}"} for i in range(len(self.flasks))]
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
            self._saved = self._snapshot()
            return {"code": "CODE"}
        if method == "import_code":
            snap = self._saved
            self.config = dict(snap["config"])
            self.flasks = dict(snap["flasks"])
            self.gems = {k: dict(v) for k, v in snap["gems"].items()}
            self.custom_mods = list(snap["custom_mods"])
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
    class VaalWorker(FakeWorker):
        def __init__(self) -> None:
            super().__init__()
            self.gems[(3, 2)] = {
                "enabled": True,
                "enableGlobal1": base_enabled,
                "enableGlobal2": True,
            }

        def _stats(self) -> dict[str, Any]:
            stats = super()._stats()
            vaal = self.gems[(3, 2)]
            stats["CombinedDPS"] += (
                (100_000 if vaal["enableGlobal1"] else 0)
                + (50_000 if vaal["enableGlobal2"] else 0)
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
    assert w.gems[(3, 2)]["enableGlobal1"] is True
    assert w.gems[(3, 2)]["enableGlobal2"] is False

    row = attribute_water(w, [vaal_record], capture_digest(w), "CombinedDPS")[0]
    assert row["dpsDelta"] == (-50_000 if not base_enabled else 50_000)
    assert vaal_record["restore"][0][1]["enableGlobal1"] is base_enabled
    assert w._snapshot() == dewatered


@pytest.mark.parametrize("enabled", [False, True])
def test_added_gem_uses_pob_mcp_skill_defaults(enabled: bool) -> None:
    class AddedGemWorker:
        def __init__(self) -> None:
            self.gem: dict[str, Any] = {}

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "add_gem":
                self.gem = {
                    "index": 2, "enabled": params["enabled"],
                    "enableGlobal1": True, "enableGlobal2": True,
                }
                return {"gem": dict(self.gem)}
            if method == "set_gem":
                assert params["group"] == 3 and params["index"] == 2
                self.gem.update({k: params[k] for k in ("enableGlobal1", "enableGlobal2")})
                return {"gem": dict(self.gem)}
            raise AssertionError(f"unexpected bridge call {method}")

    w = AddedGemWorker()
    result = _add_gem_with_default_skill_flags(
        w, {"group": 3, "nameSpec": "Vaal Discipline", "enabled": enabled}
    )
    assert result["gem"] == {
        "index": 2, "enabled": enabled,
        "enableGlobal1": True, "enableGlobal2": False,
    }


def test_config_list_values_and_boss_aliases_match_pob_mcp() -> None:
    class ConfigWorker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, params))
            if method == "list_config":
                return {"sections": [{"items": [
                    {"var": "enemyIsBoss", "type": "list", "list": [
                        {"val": "None", "label": "No"},
                        {"val": "Boss", "label": "Standard Boss"},
                        {"val": "Pinnacle", "label": "Guardian/Pinnacle Boss"},
                    ]},
                    {"var": "bandit", "type": "list", "list": [
                        {"val": "None", "label": "Kill all"},
                    ]},
                    {"var": "usePowerCharges", "type": "check"},
                ]}]}
            if method in ("set_config", "reset_config"):
                return {}
            raise AssertionError(f"unexpected bridge call {method}")

    w = ConfigWorker()
    _apply_config_in_place(w, [
        ("enemyIsBoss", "None"),
        ("enemyIsBoss", "Shaper"),
        ("bandit", "Kill all"),
        ("usePowerCharges", "none"),
    ])
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
    assert any("conditionStationary" in k for k in res["inflationStripped"]["kept"])
    keys = {r["key"] for r in res["waterAttribution"]}
    assert "conditionStationary" not in keys
    assert "useEnduranceCharges" in keys
    # Keeping Stationary means the de-watered DPS still includes it.
    assert res["dewateredPinnacle"]["primaryDps"] == pytest.approx((100_000 + 20_000) * 0.9)

    res2 = audit_build(w, attribution=False)
    assert res2["waterAttribution"] == []
    assert res2["dewateredPinnacle"]["primaryDps"] == pytest.approx(100_000 * 0.9)


def test_run_matrix_forwards_keep_to_pinnacle() -> None:
    w = FakeWorker()
    before = w._snapshot()
    res = run_matrix(w, scenarios=["Original", "Pinnacle"], keep=["conditionStationary"])
    pin = res["scenarios"][1]
    assert pin["digest"]["primaryDps"] == pytest.approx((100_000 + 20_000) * 0.9)
    assert w._snapshot() == before


def test_fmt_signed_pct() -> None:
    assert cli_mod._fmt_signed_pct(None, None) == "-"
    assert cli_mod._fmt_signed_pct(0, 0) == "0"
    assert cli_mod._fmt_signed_pct(1234.4, 4.21) == "+1,234 (+4.2%)"
    assert cli_mod._fmt_signed_pct(-50, -1.0) == "-50 (-1.0%)"


def test_cli_audit_passes_keep_and_renders_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_call(action: str, params: dict[str, Any]) -> dict[str, Any]:
        seen["action"], seen["params"] = action, params
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
    assert seen["params"]["attribution"] is True
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
    assert any(r.startswith("conditionstationery") and "did you mean conditionStationary" in r for r in rejected)
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

    def fake_call(action: str, params: dict[str, Any]) -> dict[str, Any]:
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
        "title": DEWATER_SENTINEL_TITLE, "text": "# disabled", "enabled": True,
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
    from pob_cli.analyzer import verify_restored

    w = FakeWorker()
    before = capture_digest(w)

    class OtherCode(FakeWorker):
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "export_code":
                return {"code": "DIFFERENT"}
            return super().call(method, params)

    w3 = OtherCode()
    chk = verify_restored(w3, "CODE", before)
    assert chk["verified"] is True
    assert "recalculates identical" in chk["detail"]


def test_cli_warns_when_build_not_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    chk = {"verified": False, "detail": "2 stat(s) differ from before the run (e.g. CombinedDPS)"}

    def fake_call(action: str, params: dict[str, Any]) -> dict[str, Any]:
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
