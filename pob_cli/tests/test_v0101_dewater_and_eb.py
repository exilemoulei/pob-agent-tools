"""Offline unit tests for v0.10.1 smart dewater parity, --no-auto-keep, flask ignite revocation, tattoo guard, and EB/ESCost."""

from __future__ import annotations

import copy
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from pob_cli import analyzer, comparator
from pob_cli import cli as cli_mod
from pob_cli.analyzer.dewater_auto import dewater_smart_in_place, infer_auto_keep_and_flask_notes
from pob_cli.cli import main
from pob_cli.daemon import rpc_handlers
from pob_cli.formatters import format_compare_markdown, format_matrix_markdown


class _SmartDewaterWorker:
    """Stateful fake BridgeWorker modeling Chieftain/CwS, flask-crit IgniteChance, and tree overrides."""

    def __init__(
        self,
        *,
        slot_name: str = "main",
        config: dict[str, Any] | None = None,
        flasks: dict[str, bool] | None = None,
        skills_groups: list[dict[str, Any]] | None = None,
        items: list[dict[str, Any]] | None = None,
        slots: list[dict[str, Any]] | None = None,
        allocated_nodes: list[int] | None = None,
        nodes_db: dict[str, dict[str, Any]] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
        base_stats: dict[str, Any] | None = None,
        flask_ignite_bonus: float = 0.0,
        build_info: dict[str, Any] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.slot_name = slot_name
        self.last_good_code: str | None = "INIT_CODE"
        self.config: dict[str, Any] = {
            "enemyIsBoss": "None",
            "conditionStationary": False,
            "conditionEnemyIgnited": False,
            "conditionBeenHitRecently": False,
            "useEnduranceCharges": False,
            "useFrenzyCharges": False,
            **(config or {}),
        }
        self.defaults: dict[str, Any] = {
            "conditionStationary": False,
            "conditionEnemyIgnited": False,
            "conditionBeenHitRecently": False,
            "useEnduranceCharges": False,
            "useFrenzyCharges": False,
        }
        self.flasks: dict[str, bool] = dict(flasks if flasks is not None else {"Flask 1": True})
        self._skills_groups = copy.deepcopy(skills_groups or [{"index": 1, "isMain": True, "enabled": True, "gems": []}])
        self._extra_items = copy.deepcopy(items or [])
        self._extra_slots = copy.deepcopy(slots or [])
        self._allocated = list(allocated_nodes or [])
        self._nodes_db = copy.deepcopy(nodes_db or {})
        self._overrides = copy.deepcopy(overrides or {})
        self._base_stats = {
            "CombinedDPS": 100_000.0,
            "TotalEHP": 80_000.0,
            "Life": 5_000.0,
            "LifeUnreserved": 5_000.0,
            "Mana": 800.0,
            "ManaUnreserved": 250.0,
            "EnergyShield": 0.0,
            "PhysicalMaximumHitTaken": 15_000.0,
            "FireMaximumHitTaken": 30_000.0,
            "ColdMaximumHitTaken": 30_000.0,
            "LightningMaximumHitTaken": 30_000.0,
            "ChaosMaximumHitTaken": 20_000.0,
            "FireResist": 75.0,
            "FireResistOverCap": 30.0,
            "ColdResist": 75.0,
            "ColdResistOverCap": 30.0,
            "LightningResist": 75.0,
            "LightningResistOverCap": 30.0,
            "ChaosResist": 30.0,
            **(base_stats or {}),
        }
        self._flask_ignite_bonus = flask_ignite_bonus
        self._build_info = {"level": 95, "className": "Marauder", "ascendClassName": "Chieftain", **(build_info or {})}
        self._saved: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._saved["INIT_CODE"] = self._snap()

    def _snap(self) -> dict[str, Any]:
        return copy.deepcopy({"config": self.config, "flasks": self.flasks})

    def get_cached_tree_data(self, _tree_version: str | None = None) -> dict[str, Any]:
        return {"nodes": copy.deepcopy(self._nodes_db)}

    def _compute_stats(self) -> dict[str, Any]:
        st = dict(self._base_stats)
        dps = float(st["CombinedDPS"])
        ehp = float(st["TotalEHP"])
        if self.config.get("conditionStationary"):
            dps += 50_000.0
        if self.config.get("conditionEnemyIgnited"):
            dps += 40_000.0
        if self.config.get("conditionBeenHitRecently"):
            ehp += 10_000.0
        if self.config.get("useEnduranceCharges"):
            ehp += 25_000.0
        if self.config.get("useFrenzyCharges"):
            dps += 15_000.0
        if any(self.flasks.values()):
            dps += 5_000.0
            if "IgniteChance" in st:
                st["IgniteChance"] = min(100.0, float(st["IgniteChance"]) + self._flask_ignite_bonus)
        if self.config.get("enemyIsBoss") == "Pinnacle":
            dps *= 0.9
        elif self.config.get("enemyIsBoss") == "Uber":
            dps *= 0.7
        st["CombinedDPS"] = dps
        st["TotalEHP"] = ehp
        return st

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

    def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        p = params or {}
        if method == "get_stats":
            return {"stats": self._compute_stats()}
        if method == "get_sidebar":
            return {"warnings": [], "rows": []}
        if method == "get_build_info":
            return copy.deepcopy(self._build_info)
        if method == "get_build_header":
            return {}
        if method == "list_specs":
            return {"activeSpec": 1, "specs": [{"index": 1, "title": "Main"}]}
        if method == "list_config":
            return {"sections": [{"items": [{"var": k, "value": v} for k, v in self.config.items()]}], "customMods": []}
        if method == "set_config":
            self.config[p["var"]] = p["value"]
            return {"ok": True}
        if method == "reset_config":
            self.config[p["var"]] = self.defaults.get(p["var"], False)
            return {"ok": True}
        if method == "set_custom_mods":
            return {"ok": True}
        if method == "list_items":
            slots = copy.deepcopy(self._extra_slots)
            items = copy.deepcopy(self._extra_items)
            base_id = max((int(it.get("id") or 0) for it in items), default=0) + 1
            for idx, (fname, active) in enumerate(self.flasks.items()):
                fid = base_id + idx
                slots.append({"name": fname, "isFlask": True, "active": active, "selItemId": fid})
                items.append({"id": fid, "name": "Diamond Flask", "title": "Diamond Flask", "raw": "Diamond Flask"})
            return {"slots": slots, "items": items}
        if method == "item_raw":
            iid = int(p.get("id") or 0)
            for it in self._extra_items:
                if int(it.get("id") or 0) == iid:
                    return {"raw": str(it.get("raw") or it.get("title") or "")}
            return {"raw": "Diamond Flask"}
        if method == "set_slot_active":
            self.flasks[p["slotName"]] = bool(p["active"])
            return {"ok": True}
        if method == "list_skills":
            return {"mainSocketGroup": 1, "groups": copy.deepcopy(self._skills_groups)}
        if method == "get_party":
            return {"fields": {}, "enableExportBuffs": False}
        if method == "get_tree_state":
            return {
                "treeVersion": "3_30",
                "allocatedNodes": list(self._allocated),
                "overrides": copy.deepcopy(self._overrides),
                "dynamicNodes": [],
                "jewelSockets": [],
            }
        if method == "export_code":
            snap = self._snap()
            for code, saved_snap in self._saved.items():
                if saved_snap == snap:
                    return {"code": code}
            self._seq += 1
            code = f"SNAP_{self._seq}"
            self._saved[code] = snap
            return {"code": code}
        if method == "import_code":
            snap = copy.deepcopy(self._saved[p["code"]])
            self.config = dict(snap["config"])
            self.flasks = dict(snap["flasks"])
            return {"ok": True}
        if method == "list_warnings":
            return {"warnings": []}
        if method == "set_gem":
            return {"ok": True}
        raise AssertionError(f"Unexpected RPC {method}({p})")


def test_tattoo_excluded_from_ramako_auto_keep() -> None:
    # Ramako / Tukohama tattoos on small passives must NOT trigger conditionStationary auto-keep
    ak_tattoo, _ = infer_auto_keep_and_flask_notes(
        stats={},
        skills_res={"groups": []},
        items_res={"slots": [], "items": []},
        tree_summary={
            "notables": [
                {"id": 101, "name": "Tattoo of the Ramako Sniper", "overrideWhy": "tattoo"},
                {"id": 102, "name": "雷默科萨满的纹身"},
                {"id": 103, "name": "Tattoo of the Tukohama Warcaller"},
            ]
        },
        alloc_node_stats=[
            ("Tattoo of the Ramako Makanga", ["+2% to Projectile Speed"]),
            ("雷默科狙击手的纹身", ["5% increased Projectile Speed"]),
        ],
    )
    assert "conditionstationary" not in ak_tattoo

    # Genuine Chieftain Ramako, Sun's Light (61355 / EN / ZH) DOES trigger conditionStationary
    for tree_case in (
        {"ascendancyNodes": [{"id": 61355, "name": "Some Custom Label"}]},
        {"ascendancyNodes": [{"name": "Ramako, Sun's Light"}]},
        {"ascendancyNodes": [{"nameZh": "雷默科，太阳之光"}]},
        {"ascendancyNodes": [{"nameZh": "烈陽之光．雷默科"}]},
    ):
        ak_real, _ = infer_auto_keep_and_flask_notes(
            stats={},
            skills_res={"groups": []},
            items_res={"slots": [], "items": []},
            tree_summary=tree_case,
        )
        assert "conditionstationary" in ak_real


def test_flask_inflated_ignite_chance_revoked_post_dewater_unless_guaranteed() -> None:
    # Pre-dewater IgniteChance = 62 + 38 (Diamond Flask) = 100%, drops to 62% once Flask 1 is stripped.
    w_inflated = _SmartDewaterWorker(
        config={"conditionEnemyIgnited": True, "enemyIsBoss": "None"},
        flasks={"Flask 1": True},
        base_stats={"IgniteChance": 62.0},
        flask_ignite_bonus=38.0,
    )
    before_snap = w_inflated._snap()
    aud = analyzer.audit_build(w_inflated, attribution=True)  # type: ignore[arg-type]

    stripped = aud["inflationStripped"]
    assert not any("conditionEnemyIgnited" in s for s in stripped["autoKept"])
    assert any("conditionEnemyIgnited" in s and "62%" in s for s in stripped["config"])
    # Dewatered Pinnacle DPS must NOT include the +40,000 conditionEnemyIgnited bonus: 100,000 * 0.9 = 90,000
    assert aud["dewateredPinnacle"]["primaryDps"] == pytest.approx(90_000.0)
    # Water attribution must measure conditionEnemyIgnited (+40,000 * 0.9 = +36,000)
    attr_by_key = {r["key"]: r for r in aud["waterAttribution"]}
    assert "conditionEnemyIgnited" in attr_by_key
    assert attr_by_key["conditionEnemyIgnited"]["dpsDelta"] == pytest.approx(36_000.0)
    assert aud["restoreCheck"]["verified"] is True
    assert w_inflated._snap() == before_snap

    # Build with real 100% IgniteChance without flasks keeps conditionEnemyIgnited in autoKept
    w_real_100 = _SmartDewaterWorker(
        config={"conditionEnemyIgnited": True},
        flasks={"Flask 1": True},
        base_stats={"IgniteChance": 100.0},
        flask_ignite_bonus=0.0,
    )
    st_real = dewater_smart_in_place(w_real_100)  # type: ignore[arg-type]
    assert any("conditionEnemyIgnited" in s for s in st_real["autoKept"])
    assert not any("conditionEnemyIgnited" in s for s in st_real["config"])
    assert w_real_100.config["conditionEnemyIgnited"] is True

    # Shaper of Flames guarantees Ignite regardless of post-dewater IgniteChance stat
    w_shaper = _SmartDewaterWorker(
        config={"conditionEnemyIgnited": True},
        flasks={"Flask 1": True},
        allocated_nodes=[9001],
        nodes_db={"9001": {"name": "Shaper of Flames", "asc": "Elementalist", "type": "Notable"}},
        base_stats={"IgniteChance": 60.0},
        flask_ignite_bonus=40.0,
    )
    st_shaper = dewater_smart_in_place(w_shaper)  # type: ignore[arg-type]
    assert any("conditionEnemyIgnited" in s and "Shaper of Flames" in s for s in st_shaper["autoKept"])
    assert w_shaper.config["conditionEnemyIgnited"] is True


def test_compare_matrix_and_diagnose_compare_smart_dewater_and_no_auto_keep() -> None:
    mw = _SmartDewaterWorker(
        slot_name="main",
        config={
            "conditionStationary": True,
            "useEnduranceCharges": True,
            "conditionBeenHitRecently": True,
        },
        skills_groups=[
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [{"name": "Cast when Stunned Support", "skillId": "SupportCastWhenStunned", "enabled": True, "support": True}],
            }
        ],
        allocated_nodes=[61355, 4001],
        nodes_db={
            "61355": {"name": "Ramako, Sun's Light", "asc": "Chieftain", "type": "Notable"},
            "4001": {"name": "Inexorable", "type": "Notable"},
        },
    )
    rw = _SmartDewaterWorker(
        slot_name="ref",
        config={"useFrenzyCharges": True, "conditionBeenHitRecently": True},
        base_stats={"FrenzyChargesMin": 3.0, "FrenzyChargesMax": 3.0},
    )

    # Default smart auto-keep in compare_builds
    cmp_smart = comparator.compare_builds(mw, rw)  # type: ignore[arg-type]
    pin_smart = cmp_smart["baselines"]["pinnacleDewatered"]
    assert any("conditionStationary" in s for s in pin_smart["mainStripped"]["autoKept"])
    assert any("useEnduranceCharges" in s for s in pin_smart["mainStripped"]["autoKept"])
    assert any("useFrenzyCharges" in s for s in pin_smart["refStripped"]["autoKept"])
    b3_smart = {r["stat"]: r for r in pin_smart["rows"]}
    # main: (100k + 50k stationary) * 0.9 = 135k; ref: (100k + 15k frenzy) * 0.9 = 103.5k
    assert b3_smart["CombinedDPS"]["main"] == pytest.approx(135_000.0)
    assert b3_smart["CombinedDPS"]["ref"] == pytest.approx(103_500.0)

    # Markdown rendering of Auto-Kept in format_compare_markdown
    cmp_md = format_compare_markdown(cmp_smart, target="main", ref_target="ref")
    assert "**Auto-Kept (Build-Guaranteed)**:" in cmp_md
    assert "conditionStationary" in cmp_md
    assert "useFrenzyCharges" in cmp_md

    # auto_keep=False (--no-auto-keep) strips everything not explicitly in keep
    cmp_bare = comparator.compare_builds(mw, rw, keep=["useFrenzyCharges"], auto_keep=False)  # type: ignore[arg-type]
    pin_bare = cmp_bare["baselines"]["pinnacleDewatered"]
    assert pin_bare["mainStripped"]["autoKept"] == []
    assert pin_bare["refStripped"]["autoKept"] == []
    assert any("useFrenzyCharges" in s for s in pin_bare["refStripped"]["kept"])
    b3_bare = {r["stat"]: r for r in pin_bare["rows"]}
    assert b3_bare["CombinedDPS"]["main"] == pytest.approx(90_000.0)
    assert b3_bare["CombinedDPS"]["ref"] == pytest.approx(103_500.0)

    # run_matrix parity (smart auto-keep + auto_keep=False + format_matrix_markdown)
    mat_smart = comparator.run_matrix(mw, ["Original", "Pinnacle", "Uber"])  # type: ignore[arg-type]
    by_sc = {sc["name"]: sc for sc in mat_smart["scenarios"]}
    assert by_sc["Pinnacle"]["digest"]["primaryDps"] == pytest.approx(135_000.0)
    assert by_sc["Uber"]["digest"]["primaryDps"] == pytest.approx(105_000.0)
    mat_md = format_matrix_markdown(mat_smart, target="main")
    assert "**Auto-Kept (Build-Guaranteed)**:" in mat_md
    assert "conditionStationary" in mat_md

    mat_bare = comparator.run_matrix(mw, ["Original", "Pinnacle"], auto_keep=False)  # type: ignore[arg-type]
    by_sc_bare = {sc["name"]: sc for sc in mat_bare["scenarios"]}
    assert by_sc_bare["Pinnacle"]["digest"]["primaryDps"] == pytest.approx(90_000.0)

    # diagnose_compare_builds forwards keep + auto_keep=False
    diag_cmp = analyzer.diagnose_compare_builds(mw, rw, auto_keep=False, no_rank=True)  # type: ignore[arg-type]
    assert diag_cmp["comparison"]["baselines"]["pinnacleDewatered"]["mainStripped"]["autoKept"] == []
    assert diag_cmp["buildA"]["inflationStripped"]["autoKept"] == []
    assert diag_cmp["buildB"]["inflationStripped"]["autoKept"] == []


def test_cli_and_rpc_no_auto_keep_and_audit_compare_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _fake_daemon_call(action: str, params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        captured.append((action, dict(params)))
        if action == "audit":
            return {
                "healthIssues": [],
                "inflationStripped": {
                    "config": [],
                    "flasks": [],
                    "skills": [],
                    "party": [],
                    "autoKept": ["conditionStationary=True (auto-kept: Chieftain Ramako stationary build)"],
                    "kept": ["Flask 1 (Ruby Flask) (kept)"],
                    "keepRejected": [],
                },
                "waterAttribution": [],
                "restoreCheck": {"verified": True},
                "original": {"primaryDpsKey": "CombinedDPS", "primaryDps": 500_000, "totalEhp": 90_000},
                "dewateredPinnacle": {"primaryDps": 450_000, "totalEhp": 85_000},
            }
        if action == "compare":
            return {
                "mainBuildInfo": {"level": 96, "ascendClassName": "Chieftain"},
                "refBuildInfo": {"level": 97, "ascendClassName": "Juggernaut"},
                "baselines": {
                    "original": [],
                    "envAlignedToMain": {"rows": []},
                    "pinnacleDewatered": {
                        "mainStripped": {"autoKept": ["conditionStationary=True (auto-kept: CwS/Ramako)"]},
                        "refStripped": {"kept": ["useEnduranceCharges=True (kept)"]},
                        "rows": [],
                    },
                },
                "treeDiff": {"keystones": {"onlyInMain": [], "onlyInRef": []}, "notables": {"sharedCount": 0, "onlyInMain": [], "onlyInRef": []}},
                "skillDiff": {},
                "itemSlotDiff": [],
            }
        return {"env": {"slot": "main"}, "buildInfo": {}, "original": {}, "dewateredPinnacle": {}, "inflationStripped": {}}

    monkeypatch.setattr(cli_mod, "_daemon_call", _fake_daemon_call)
    runner = CliRunner()

    r_aud = runner.invoke(main, ["audit", "--keep", "Flask 1", "--no-auto-keep"])
    assert r_aud.exit_code == 0, r_aud.output
    assert captured[-1] == (
        "audit",
        {"target": "main", "attribution": True, "keep": ["Flask 1"], "no_auto_keep": True},
    )
    assert "**Auto-Kept (Build-Guaranteed)**: conditionStationary=True" in r_aud.output
    assert "**Kept (--keep)**: Flask 1" in r_aud.output

    r_cmp = runner.invoke(main, ["compare", "--keep", "useEnduranceCharges", "--no-auto-keep"])
    assert r_cmp.exit_code == 0, r_cmp.output
    assert captured[-1] == (
        "compare",
        {"target": "main", "ref_target": "ref", "keep": ["useEnduranceCharges"], "no_auto_keep": True},
    )
    assert "**Auto-Kept (Build-Guaranteed)**: `main`: conditionStationary=True" in r_cmp.output
    assert "**Kept (--keep)**: `ref`: useEnduranceCharges=True" in r_cmp.output

    r_diag = runner.invoke(main, ["diagnose", "--no-auto-keep"])
    assert r_diag.exit_code == 0, r_diag.output
    assert captured[-1][0] == "diagnose" and captured[-1][1]["no_auto_keep"] is True

    # Verify rpc_handlers bind auto_keep=False cleanly for audit, diagnose, compare, and dual diagnose
    seen_ak: dict[str, bool] = {}
    mgr = SimpleNamespace(
        pob_dir=None,
        runtime_dir=None,
        _read_cache=None,
        _attach_warning_if_degraded=lambda r: r,
        _attach_state_meta=lambda r, _w: r,
    )
    monkeypatch.setattr(
        analyzer,
        "audit_build",
        lambda _w, *, keep=None, attribution=True, auto_keep=True, **_kw: seen_ak.update(audit=auto_keep) or {},
    )
    monkeypatch.setattr(
        analyzer,
        "diagnose_build",
        lambda _w, *, auto_keep=True, **_kw: seen_ak.update(diagnose=auto_keep) or {},
    )
    monkeypatch.setattr(
        comparator,
        "compare_builds",
        lambda _mw, _rw, *, auto_keep=True, **_kw: seen_ak.update(compare=auto_keep) or {},
    )
    monkeypatch.setattr(
        analyzer,
        "diagnose_compare_builds",
        lambda _mw, _rw, *, auto_keep=True, **_kw: seen_ak.update(diag_cmp=auto_keep) or {},
    )

    dummy_w = SimpleNamespace(slot_name="main", last_good_code="X")
    dummy_rw = SimpleNamespace(slot_name="ref", last_good_code="Y")
    rpc_handlers._h_audit(mgr, {"no_auto_keep": True}, dummy_w)  # type: ignore[arg-type]
    rpc_handlers._h_diagnose(mgr, {"no_auto_keep": True}, dummy_w)  # type: ignore[arg-type]
    rpc_handlers.dispatch_dual_slot_action(mgr, "compare", {"no_auto_keep": True}, dummy_w, dummy_rw)  # type: ignore[arg-type]
    rpc_handlers.dispatch_dual_slot_action(mgr, "diagnose", {"no_auto_keep": True}, dummy_w, dummy_rw)  # type: ignore[arg-type]
    assert seen_ak == {"audit": False, "diagnose": False, "compare": False, "diag_cmp": False}


def test_eldritch_battery_and_es_cost_static_health_checks() -> None:
    # 1. EB via allocated tree keystone (ES=1188, ManaUnreserved=42, ManaCost=115, ManaRegen=-2.0): NO false-positive!
    w_eb_tree = _SmartDewaterWorker(
        allocated_nodes=[39085],
        nodes_db={"39085": {"name": "Eldritch Battery", "nameZh": "异能魔力", "type": "Keystone"}},
        base_stats={
            "EnergyShield": 1188.0,
            "ManaUnreserved": 42.0,
            "ManaCost": 115.0,
            "ManaRegenRecovery": -2.0,
            "Speed": 4.5,
        },
    )
    aud_eb_tree = analyzer.audit_build(w_eb_tree, attribution=False)  # type: ignore[arg-type]
    sustain_msgs = [g["message"] for g in aud_eb_tree["healthIssues"] if g["category"] == "Sustain"]
    assert sustain_msgs == [], f"Unexpected sustain false-positives with EB tree keystone: {sustain_msgs}"

    # 2. EB via EnergyShieldProtectsMana stat (e.g. Devouring Diadem) with positive Speed > ManaRegen: NO false-positive!
    w_eb_stat = _SmartDewaterWorker(
        base_stats={
            "EnergyShieldProtectsMana": True,
            "EnergyShield": 1188.0,
            "ManaUnreserved": 42.0,
            "ManaCost": 115.0,
            "ManaRegenRecovery": 5.0,
            "Speed": 4.5,
        },
    )
    aud_eb_stat = analyzer.audit_build(w_eb_stat, attribution=False)  # type: ignore[arg-type]
    assert [g for g in aud_eb_stat["healthIssues"] if g["category"] == "Sustain"] == []

    # 3. EB active, but ES + ManaUnreserved (50 + 42 = 92) < ManaCost (115): MUST flag HIGH Sustain!
    w_eb_short = _SmartDewaterWorker(
        base_stats={
            "EnergyShieldProtectsMana": True,
            "EnergyShield": 50.0,
            "ManaUnreserved": 42.0,
            "ManaCost": 115.0,
        },
    )
    aud_eb_short = analyzer.audit_build(w_eb_short, attribution=False)  # type: ignore[arg-type]
    short_gaps = [g for g in aud_eb_short["healthIssues"] if g["category"] == "Sustain" and g["severity"] == "HIGH"]
    assert len(short_gaps) == 1
    assert "Eldritch Battery" in short_gaps[0]["message"] and "92" in short_gaps[0]["message"]

    # 4. Non-EB with ES=1188, ManaUnreserved=42, ManaCost=115: MUST still flag Unreserved Mana deficit!
    w_no_eb = _SmartDewaterWorker(
        base_stats={
            "EnergyShield": 1188.0,
            "ManaUnreserved": 42.0,
            "ManaCost": 115.0,
        },
    )
    aud_no_eb = analyzer.audit_build(w_no_eb, attribution=False)  # type: ignore[arg-type]
    no_eb_gaps = [g for g in aud_no_eb["healthIssues"] if g["category"] == "Sustain" and g["severity"] == "HIGH"]
    assert len(no_eb_gaps) == 1
    assert "Unreserved Mana (42)" in no_eb_gaps[0]["message"]

    # 5. ESCost > EnergyShield (e.g. Whispers of Infinity with 0 ES) MUST flag HIGH Sustain; sufficient ES passes.
    w_es_zero = _SmartDewaterWorker(
        base_stats={"ManaCost": 0.0, "ESCost": 85.0, "EnergyShield": 0.0, "ManaUnreserved": 500.0},
    )
    aud_es_zero = analyzer.audit_build(w_es_zero, attribution=False)  # type: ignore[arg-type]
    es_gaps = [g for g in aud_es_zero["healthIssues"] if g["category"] == "Sustain" and g["severity"] == "HIGH"]
    assert len(es_gaps) == 1
    assert "Skill ES Cost (85) exceeds Energy Shield (0)" in es_gaps[0]["message"]

    w_es_ok = _SmartDewaterWorker(
        base_stats={"ManaCost": 0.0, "ESCost": 85.0, "EnergyShield": 600.0, "ManaUnreserved": 500.0},
    )
    aud_es_ok = analyzer.audit_build(w_es_ok, attribution=False)  # type: ignore[arg-type]
    assert [g for g in aud_es_ok["healthIssues"] if g["category"] == "Sustain"] == []

    # 6. EB + ESCost + ManaCost combined boundary: remaining ES after ESCost covers (or fails to cover) ManaCost!
    w_eb_es_ok = _SmartDewaterWorker(
        base_stats={
            "EnergyShieldProtectsMana": True,
            "EnergyShield": 250.0,
            "ManaUnreserved": 40.0,
            "ESCost": 80.0,
            "ManaCost": 120.0,
            "ManaRegenRecovery": -1.5,
            "Speed": 4.0,
        },
    )
    aud_eb_es_ok = analyzer.audit_build(w_eb_es_ok, attribution=False)  # type: ignore[arg-type]
    assert [g for g in aud_eb_es_ok["healthIssues"] if g["category"] == "Sustain"] == []

    # ES=100 covers ESCost=60 alone, and ES+Mana=120 >= ManaCost=80 alone, but remaining ES(40)+Mana(20)=60 < 80!
    w_eb_es_short = _SmartDewaterWorker(
        base_stats={
            "EnergyShieldProtectsMana": True,
            "EnergyShield": 100.0,
            "ManaUnreserved": 20.0,
            "ESCost": 60.0,
            "ManaCost": 80.0,
        },
    )
    aud_eb_es_short = analyzer.audit_build(w_eb_es_short, attribution=False)  # type: ignore[arg-type]
    comb_gaps = [g for g in aud_eb_es_short["healthIssues"] if g["category"] == "Sustain" and g["severity"] == "HIGH"]
    assert len(comb_gaps) == 1
    assert "after 60 ES Cost" in comb_gaps[0]["message"] and "= 60" in comb_gaps[0]["message"]


def test_five_command_smart_dewater_parity() -> None:
    """Verify audit, diagnose, compare, diagnose --compare, and matrix share identical auto-kept & Pinnacle stats."""
    w_main = _SmartDewaterWorker(
        slot_name="main",
        config={
            "conditionStationary": True,
            "useFrenzyCharges": True,
            "conditionBeenHitRecently": True,
            "conditionEnemyIgnited": True,
        },
        flasks={"Flask 1": True},
        skills_groups=[
            {
                "index": 1,
                "isMain": True,
                "enabled": True,
                "gems": [{"name": "Righteous Fire", "enabled": True, "support": False}],
            }
        ],
        allocated_nodes=[61355],
        nodes_db={"61355": {"name": "Ramako, Sun's Light", "asc": "Chieftain", "type": "Notable"}},
        base_stats={"FrenzyChargesMin": 3.0, "FrenzyChargesMax": 3.0, "IgniteChance": 62.0},
        flask_ignite_bonus=38.0,  # pre-dewater 100%, post-dewater 62% -> revoked across all 5 commands!
    )
    w_ref = _SmartDewaterWorker(slot_name="ref")
    snap_before = w_main._snap()

    aud = analyzer.audit_build(w_main, attribution=False)  # type: ignore[arg-type]
    diag = analyzer.diagnose_build(w_main, no_rank=True)  # type: ignore[arg-type]
    cmp_res = comparator.compare_builds(w_main, w_ref)  # type: ignore[arg-type]
    diag_cmp = analyzer.diagnose_compare_builds(w_main, w_ref, no_rank=True)  # type: ignore[arg-type]
    mat = comparator.run_matrix(w_main, ["Original", "Pinnacle"])  # type: ignore[arg-type]

    # Expected: (100,000 base + 50,000 stationary + 15,000 frenzy) * 0.9 = 148,500.0; conditionEnemyIgnited revoked!
    expected_dps = 148_500.0
    expected_ehp = 80_000.0
    aud_ak = list(aud["inflationStripped"]["autoKept"])
    assert any("conditionStationary" in s for s in aud_ak)
    assert any("useFrenzyCharges" in s for s in aud_ak)
    assert not any("conditionEnemyIgnited" in s for s in aud_ak)

    cmp_pin = cmp_res["baselines"]["pinnacleDewatered"]
    dc_pin = diag_cmp["comparison"]["baselines"]["pinnacleDewatered"]
    mat_pin = next(sc for sc in mat["scenarios"] if sc["name"] == "Pinnacle")

    for name, ak in (
        ("diagnose", diag["inflationStripped"]["autoKept"]),
        ("compare", cmp_pin["mainStripped"]["autoKept"]),
        ("diagnose_compare.comparison", dc_pin["mainStripped"]["autoKept"]),
        ("diagnose_compare.buildA", diag_cmp["buildA"]["inflationStripped"]["autoKept"]),
        ("matrix", mat_pin["applied"]["stripped"]["autoKept"]),
    ):
        assert list(ak) == aud_ak, f"autoKept mismatch in {name}: {ak} != {aud_ak}"

    cmp_rows = {r["stat"]: r["main"] for r in cmp_pin["rows"]}
    dc_rows = {r["stat"]: r["main"] for r in dc_pin["rows"]}
    for name, dps, ehp in (
        ("audit", aud["dewateredPinnacle"]["primaryDps"], aud["dewateredPinnacle"]["totalEhp"]),
        ("diagnose", diag["dewateredPinnacle"]["primaryDps"], diag["dewateredPinnacle"]["totalEhp"]),
        ("compare", cmp_rows["CombinedDPS"], cmp_rows["TotalEHP"]),
        ("diagnose_compare.comparison", dc_rows["CombinedDPS"], dc_rows["TotalEHP"]),
        ("diagnose_compare.buildA", diag_cmp["buildA"]["dewateredPinnacle"]["primaryDps"], diag_cmp["buildA"]["dewateredPinnacle"]["totalEhp"]),
        ("matrix", mat_pin["digest"]["primaryDps"], mat_pin["digest"]["totalEhp"]),
    ):
        assert dps == pytest.approx(expected_dps), f"DPS mismatch in {name}: {dps}"
        assert ehp == pytest.approx(expected_ehp), f"EHP mismatch in {name}: {ehp}"

    assert w_main._snap() == snap_before

