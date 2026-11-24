"""Unit and live-engine tests for C1 direct_calc + C2c Lua CoW snapshots and fast sweeps."""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any

import pytest
from engine_paths import CHECKOUT_ROOT, find_runnable_engine

from pob_cli import item_inspect
from pob_cli.bridge_client import BridgeWorker
from pob_cli.errors import DaemonRequestError
from pob_cli.importers import xml_to_share_code
from pob_cli.mutators import edit_item_mods, run_batch, run_sweep, try_item
from pob_cli.snapshot import (
    CORE_STAT_KEYS,
    BuildSnapshot,
    capture_digest,
    capture_direct_digest,
    drop_snapshot,
    restore_snapshot,
    run_mutation,
    supports_cow_snapshot,
    supports_direct_calc,
    take_snapshot,
    verify_cow_restored,
    worker_caps,
)

POB_DIR = find_runnable_engine()
LUA_DIR = Path(__file__).resolve().parent.parent / "src" / "pob_cli" / "lua"
BENCH_FIXTURES_DIR = CHECKOUT_ROOT.parent / "bench" / "fixtures"


class _CowFakeWorker:
    """In-memory fake worker supporting configurable `cowSnapshot` and `directCalc` capabilities."""

    def __init__(self, *, caps: dict[str, Any] | None = None, omit_caps_attr: bool = False) -> None:
        self.lock = threading.RLock()
        if not omit_caps_attr:
            self.caps: dict[str, Any] = dict(caps or {})
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self.last_good_code: str | None = "INIT_GOOD_CODE"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.save_slot_state_calls = 0

        self.dps = 120000.0
        self.full_dps = 0.0
        self.life = 4500.0
        self.ehp: float | str = 45000.0
        self.config_vars: dict[str, Any] = {"enemyIsBoss": "Pinnacle", "usePowerCharges": False}
        self.points_used = 100
        self.warnings: list[str] = []
        self.slot_items: dict[str, str] = {}
        self._cow_snaps: dict[str, str] = {}
        self._cow_seq = 0

    def save_slot_state(self) -> None:
        self.save_slot_state_calls += 1

    def _serialize(self) -> str:
        return json.dumps(
            {
                "dps": self.dps,
                "full_dps": self.full_dps,
                "life": self.life,
                "ehp": self.ehp,
                "config_vars": dict(self.config_vars),
                "points_used": self.points_used,
                "warnings": list(self.warnings),
                "slot_items": dict(self.slot_items),
            },
            sort_keys=True,
        )

    def _deserialize(self, raw: str) -> None:
        data = json.loads(raw)
        self.dps = float(data["dps"])
        self.full_dps = float(data["full_dps"])
        self.life = float(data["life"])
        self.ehp = data["ehp"]
        self.config_vars = dict(data["config_vars"])
        self.points_used = int(data["points_used"])
        self.warnings = list(data.get("warnings", []))
        self.slot_items = dict(data.get("slot_items", {}))

    def _stats_map(self, *, skip_ehp: bool = False) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "CombinedDPS": self.dps,
            "FullDPS": self.full_dps,
            "TotalDPS": self.dps,
            "TotalDotDPS": 0.0,
            "Life": self.life,
            "Mana": 600.0,
            "FireResist": 75.0,
            "ColdResist": 75.0,
            "LightningResist": 75.0,
            "ChaosResist": 0.0,
            "FireMaximumHitTaken": 30000.0,
            "ColdMaximumHitTaken": 28000.0,
            "LightningMaximumHitTaken": 32000.0,
            "PhysicalMaximumHitTaken": 15000.0,
        }
        if not skip_ehp:
            stats["TotalEHP"] = self.ehp
        return stats

    def call_batch(
        self,
        calls: list[tuple[str, dict[str, Any] | None]],
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        return [self.call(m, p or {}) for m, p in calls]

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        p = dict(params or {})
        self.calls.append((method, p))

        if method == "export_code":
            return {"code": self._serialize()}
        if method == "import_code":
            self._deserialize(str(p["code"]))
            return {"ok": True}
        if method == "cow_snapshot":
            self._cow_seq += 1
            sid = f"cow_{self._cow_seq}"
            self._cow_snaps[sid] = self._serialize()
            return {"snapId": sid}
        if method == "cow_restore":
            sid = str(p["snapId"])
            self._deserialize(self._cow_snaps[sid])
            if p.get("drop"):
                self._cow_snaps.pop(sid, None)
            return {"ok": True}
        if method == "cow_drop":
            self._cow_snaps.pop(str(p["snapId"]), None)
            return {"ok": True}
        if method == "direct_calc":
            skip_ehp = bool(p.get("skipEHP"))
            return {
                "stats": self._stats_map(skip_ehp=skip_ehp),
                "skillFlags": {"spell": True, "hit": True},
                "points": {"used": self.points_used, "usedMax": 122, "ascUsed": 8, "ascMax": 8},
                "warnings": [] if skip_ehp else list(self.warnings),
            }
        if method == "get_stats":
            return {"stats": self._stats_map()}
        if method == "get_sidebar":
            return {"warnings": [], "rows": []}
        if method == "get_build_info":
            return {
                "className": "Witch",
                "ascendClassName": "Elementalist",
                "level": 95,
                "points": {"used": self.points_used, "usedMax": 122, "ascUsed": 8, "ascMax": 8},
            }
        if method == "get_build_header":
            return {"mainSocketGroup": {"index": 1}}
        if method == "list_config":
            return {
                "activeConfigSetId": 1,
                "configSets": [{"id": 1, "title": "Default"}],
                "sections": [
                    {
                        "name": "General",
                        "items": [
                            {"var": k, "value": v, "type": "check" if isinstance(v, bool) else "list"}
                            for k, v in self.config_vars.items()
                        ],
                    }
                ],
                "customMods": [],
            }
        if method == "set_config":
            self.config_vars[p["var"]] = p["value"]
            if p["var"] == "usePowerCharges" and p["value"]:
                self.dps += 30000.0
            return {"ok": True}
        if method == "reset_config":
            self.config_vars[p["var"]] = False
            return {"ok": True}
        if method == "list_items":
            slots = [
                {"name": sname, "selItemId": idx, "active": True}
                for idx, (sname, _sraw) in enumerate(sorted(self.slot_items.items()), start=1)
            ]
            items = [
                {"id": idx, "name": sname, "slotName": sname}
                for idx, (sname, _sraw) in enumerate(sorted(self.slot_items.items()), start=1)
            ]
            return {"slots": slots, "items": items}
        if method == "item_raw":
            iid = int(p["id"])
            entries = sorted(self.slot_items.items())
            return {"raw": entries[iid - 1][1] if 1 <= iid <= len(entries) else ""}
        if method == "add_item":
            sname = str(p.get("slotName") or "Amulet")
            self.slot_items[sname] = str(p["raw"])
            self.dps += 5000.0
            return {"item": {"id": 1, "primarySlot": sname}}
        raise AssertionError(f"Unexpected mock RPC '{method}' with params {p}")


def test_capability_helpers_safe_when_fake_worker_omits_caps() -> None:
    no_caps = _CowFakeWorker(omit_caps_attr=True)
    assert not hasattr(no_caps, "caps")
    assert worker_caps(no_caps) == {}
    assert supports_cow_snapshot(no_caps) is False
    assert supports_direct_calc(no_caps) is False

    # Legacy run_mutation what-if keeps exact export_code/import_code call counts
    def _bump(w: Any) -> dict[str, Any]:
        w.dps += 15000.0
        return {"bumped": True}

    res = run_mutation(no_caps, _bump, apply=False)
    assert res["applied"] is False
    assert res["after"]["primaryDps"] == pytest.approx(135000.0)
    assert no_caps.dps == pytest.approx(120000.0)
    methods = [m for m, _ in no_caps.calls]
    assert methods.count("export_code") == 1
    assert methods.count("import_code") == 1
    assert "cow_snapshot" not in methods
    assert "cow_restore" not in methods


def test_run_mutation_cow_what_if_vs_apply_exports_xml() -> None:
    w = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    orig_state = w._serialize()

    # 1. apply=False uses cow_snapshot + cow_restore(drop=True) with 0 export_code/import_code
    def _mutate(worker: Any) -> dict[str, Any]:
        worker.dps = 180000.0
        worker.life = 5200.0
        return {"ok": True}

    res_whatif = run_mutation(w, _mutate, apply=False)
    assert res_whatif["applied"] is False
    assert res_whatif["after"]["primaryDps"] == pytest.approx(180000.0)
    assert w._serialize() == orig_state
    assert w._cow_snaps == {}
    assert w.last_good_code == "INIT_GOOD_CODE"
    methods = [m for m, _ in w.calls]
    assert methods.count("cow_snapshot") == 1
    assert methods.count("cow_restore") == 1
    assert methods.count("export_code") == 0
    assert methods.count("import_code") == 0

    # 2. Exception during apply=False still restores and drops CoW snapshot cleanly
    w.calls.clear()

    def _boom(worker: Any) -> None:
        worker.dps = 999999.0
        raise ValueError("simulated mutation error")

    with pytest.raises(ValueError, match="simulated mutation error"):
        run_mutation(w, _boom, apply=False)
    assert w._serialize() == orig_state
    assert w._cow_snaps == {}
    assert [m for m, _ in w.calls].count("cow_restore") == 1
    assert [m for m, _ in w.calls].count("import_code") == 0

    # 3. apply=True MUST still export XML for undo_stack and slot_<slug>.json (0 cow_snapshot!)
    w.calls.clear()
    res_apply = run_mutation(w, _mutate, apply=True)
    assert res_apply["applied"] is True
    assert w.dps == pytest.approx(180000.0)
    assert len(w.undo_stack) == 1
    assert w.undo_stack[0] == orig_state
    assert w.last_good_code == w._serialize()
    assert w.save_slot_state_calls == 1
    apply_methods = [m for m, _ in w.calls]
    assert apply_methods.count("export_code") == 2
    assert apply_methods.count("cow_snapshot") == 0


def test_capture_direct_digest_and_verify_cow_restored() -> None:
    w = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    w.full_dps = 250000.0
    w.points_used = 114
    w.warnings = ["^xFF8800You do not have enough Strength for your equipment"]
    base_digest = {
        "buildInfo": {"className": "Witch", "level": 95, "points": {"used": 100}},
        "buildHeader": {"mainSocketGroup": {"index": 1}},
    }

    d_full = capture_direct_digest(w, skip_ehp=False, base_digest=base_digest)
    assert d_full["primaryDpsKey"] == "FullDPS"
    assert d_full["primaryDps"] == pytest.approx(250000.0)
    assert d_full["totalEhp"] == pytest.approx(45000.0)
    assert d_full["buildInfo"]["points"]["used"] == 114
    assert d_full["skillFlags"] == {"spell": True, "hit": True}
    assert d_full["warnings"] == ["You do not have enough Strength for your equipment"]

    d_skip = capture_direct_digest(w, skip_ehp=True, base_digest=base_digest)
    assert d_skip["primaryDps"] == pytest.approx(250000.0)
    assert "TotalEHP" not in d_skip["stats"]
    assert w.calls[-1] == ("direct_calc", {"skipEHP": True})

    # verify_cow_restored detects exact parity and drift
    ok_check = verify_cow_restored(w, d_full)
    assert ok_check["verified"] is True

    w.dps = 999999.0
    bad_check = verify_cow_restored(w, d_full)
    assert bad_check["verified"] is False
    assert "CombinedDPS" in bad_check["differing"]

    # Non-directCalc worker falls back to capture_digest cleanly
    w_legacy = _CowFakeWorker(caps={})
    d_leg = capture_direct_digest(w_legacy)
    assert d_leg["primaryDps"] == pytest.approx(120000.0)
    assert all(m != "direct_calc" for m, _ in w_legacy.calls)


def test_run_sweep_with_cow_and_direct_calc_zero_xml_roundtrips() -> None:
    w = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    orig_state = w._serialize()

    res = run_sweep(
        w,
        [
            {"name": "Power Charges", "ops": [{"op": "config", "set": {"usePowerCharges": True}}]},
            {
                "name": "Faulty Variant",
                "ops": [
                    {"op": "config", "set": {"usePowerCharges": True}},
                    {"op": "nonexistent_bad_op"},
                ],
            },
            {"name": "Clean After Fault", "ops": []},
        ],
        base_ops=[{"op": "config", "set": {"enemyIsBoss": "Uber"}}],
    )

    assert res["succeeded"] == 2
    assert res["failed"] == 1
    assert res["variants"][0]["summary"]["primaryDps"] == pytest.approx(150000.0)
    assert res["variants"][1]["ok"] is False
    # Variant 3 sees clean base (no pollution from Faulty Variant!)
    assert res["variants"][2]["summary"]["primaryDps"] == pytest.approx(120000.0)
    assert res["restoreCheck"]["verified"] is True
    assert w._serialize() == orig_state
    assert w._cow_snaps == {}
    assert w.last_good_code == "INIT_GOOD_CODE"

    methods = [m for m, _ in w.calls]
    assert methods.count("export_code") == 0
    assert methods.count("import_code") == 0
    assert methods.count("cow_snapshot") == 2  # orig + base_ops
    assert methods.count("cow_drop") == 1  # base_snap dropped in finally
    restores = [p for m, p in w.calls if m == "cow_restore"]
    assert len(restores) >= 3  # between dirty variants + final orig_snap(drop=True)
    assert all(r.get("rebuild") is False for r in restores[:-1])
    assert restores[-1].get("drop") is True and "rebuild" not in restores[-1]
    assert methods.count("direct_calc") >= 4  # orig_digest + 2 ok variants + verify_cow_restored

    # Without base_ops: only 1 cow_snapshot and 0 cow_drop (orig_snap dropped via cow_restore(drop=True))
    w.calls.clear()
    res_no_base = run_sweep(
        w,
        [
            {"name": "V1", "ops": [{"op": "config", "set": {"usePowerCharges": True}}]},
            {"name": "V2", "ops": []},
        ],
    )
    assert res_no_base["succeeded"] == 2
    assert res_no_base["restoreCheck"]["verified"] is True
    m2 = [m for m, _ in w.calls]
    assert m2.count("export_code") == 0
    assert m2.count("import_code") == 0
    assert m2.count("cow_snapshot") == 1
    assert m2.count("cow_drop") == 0
    assert w._cow_snaps == {}


def test_snapshot_helpers_drop_noop_on_code_snapshot() -> None:
    w = _CowFakeWorker(caps={})
    snap = take_snapshot(w)
    assert isinstance(snap, BuildSnapshot) and snap.kind == "code"
    drop_snapshot(w, snap)
    restore_snapshot(w, snap, drop=True)
    assert [m for m, _ in w.calls] == ["export_code", "import_code"]


def test_fast_calc_cow_lua_module_contracts_and_budget() -> None:
    lua_path = LUA_DIR / "fast_calc_cow.lua"
    assert lua_path.is_file()
    raw = lua_path.read_bytes()
    text = raw.decode("utf-8").replace("\r\n", "\n")
    lines = text.splitlines()
    crlf_bytes = max(len(raw), len(text.encode("utf-8")) + len(lines))
    assert len(lines) <= 650
    assert crlf_bytes <= 24000
    for rpc in ("cow_snapshot", "cow_restore", "cow_drop", "direct_calc"):
        assert rpc in text
    assert "rebuild_cluster_graphs" in text
    assert "sync_slots" in text
    assert "compare_" not in text


def test_offline_points_used_digest_and_verify_cow_restored_cluster_leak() -> None:
    w = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    w.points_used = 81
    w.full_dps = 1870226.10

    d_std = capture_digest(w)
    d_dir = capture_direct_digest(w)
    assert d_std["pointsUsed"] == 81
    assert d_dir["pointsUsed"] == 81

    # Simulate a nested Medium Cluster 3-point leak (81 -> 78) after incomplete restore
    w.points_used = 78
    bad_dir = verify_cow_restored(w, d_dir)
    assert bad_dir["verified"] is False
    assert "pointsUsed" in bad_dir["differing"]

    bad_std = verify_cow_restored(w, d_std)
    assert bad_std["verified"] is False
    assert "pointsUsed" in bad_std["differing"]

    # Restoring exact 81 points passes both direct_calc and standard verification
    w.points_used = 81
    assert verify_cow_restored(w, d_dir)["verified"] is True
    assert verify_cow_restored(w, d_std)["verified"] is True


_SOL_TALISMAN_3P3S_RAW = "\n".join([
    "Rarity: RARE", "Sol Talisman", "Turquoise Amulet", "Item Level: 85", "Implicits: 2",
    "+24 to Dexterity and Intelligence", "Allocates Divine Judgement",
    "+78 to maximum Life", "+34% to Global Critical Strike Multiplier",
    "38% increased Elemental Damage with Attack Skills", "+44 to Intelligence",
    "+35% to Fire Resistance", "{crafted}Non-Channelling Skills have -7 to Total Mana Cost",
])


def test_offline_rare_3p3s_affix_cap_in_edit_item_mods_and_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        {"id": "life", "source": "Explicit", "generation_type": "Prefix", "group": "Life", "affix": "Robust", "level": 30, "stats": ["+(70-79) to maximum Life"]},
        {"id": "wed", "source": "Explicit", "generation_type": "Prefix", "group": "WED", "affix": "Overpowering", "level": 81, "stats": ["(37-42)% increased Elemental Damage with Attack Skills"]},
        {"id": "mana", "source": "Bench", "generation_type": "Bench Prefix", "group": "ManaCost", "affix": "Upgraded", "level": 1, "stats": ["Non-Channelling Skills have -7 to Total Mana Cost"]},
        {"id": "multi", "source": "Explicit", "generation_type": "Suffix", "group": "CritMulti", "affix": "of Ferocity", "level": 59, "stats": ["+(30-38)% to Global Critical Strike Multiplier"]},
        {"id": "int", "source": "Explicit", "generation_type": "Suffix", "group": "Intelligence", "affix": "of the Virtuoso", "level": 74, "stats": ["+(43-50) to Intelligence"]},
        {"id": "fres", "source": "Explicit", "generation_type": "Suffix", "group": "FireRes", "affix": "of the Furnace", "level": 48, "stats": ["+(30-35)% to Fire Resistance"]},
        {"id": "cres", "source": "Explicit", "generation_type": "Suffix", "group": "ColdRes", "affix": "of the Ice", "level": 48, "stats": ["+(30-35)% to Cold Resistance"]},
        {"id": "ccres", "source": "Bench", "generation_type": "Bench Suffix", "group": "ColdRes", "affix": "Crafted", "level": 1, "stats": ["+(16-20)% to Cold Resistance"]},
    ]
    monkeypatch.setattr(item_inspect, "_load_entries", lambda *a, **k: (entries, True, frozenset({"amulet", "default"})))
    monkeypatch.setattr(item_inspect, "classify_mod_affix_types", lambda lines, **k: {ln: {"suffix"} for ln in lines})

    w = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    w.slot_items["Amulet"] = _SOL_TALISMAN_3P3S_RAW

    # 1. Direct edit_item_mods rejects adding a 4th suffix (and removing a prefix does NOT free a suffix!)
    with pytest.raises(DaemonRequestError, match=r"(?s)3/3 suffixes.*Remove or replace an existing suffix first"):
        edit_item_mods(w, slot_name="Amulet", add_mods=["+34% to Cold Resistance"], apply=False)
    with pytest.raises(DaemonRequestError, match=r"3/3 suffixes"):
        edit_item_mods(
            w, slot_name="Amulet", remove_mods=["maximum Life"], add_mods=["+34% to Cold Resistance"], apply=False
        )

    # 2. run_sweep marks 4th-suffix variant as failed while allowing valid suffix replacement
    sw = run_sweep(
        w,
        [
            {"name": "Illegal 4th Suffix", "ops": [{"op": "item_edit", "slot": "Amulet", "addMods": ["+34% to Cold Resistance"]}]},
            {"name": "Illegal Crafted 4th Suffix", "ops": [{"op": "item_edit", "slot": "Amulet", "addMods": ["{crafted}+20% to Cold Resistance"]}]},
            {"name": "Legal Suffix Swap", "ops": [{"op": "item_edit", "slot": "Amulet", "replaceMods": [["Fire Resistance", "+34% to Cold Resistance"]]}]},
        ],
    )
    assert sw["succeeded"] == 1
    assert sw["failed"] == 2
    assert sw["variants"][0]["ok"] is False and "3/3 suffixes" in sw["variants"][0]["error"]
    assert sw["variants"][1]["ok"] is False and "3/3 suffixes" in sw["variants"][1]["error"]
    assert sw["variants"][2]["ok"] is True
    assert sw["restoreCheck"]["verified"] is True


# ---------------------------------------------------------------------------
# Live-engine tests (run when real engine advertises cowSnapshot & directCalc)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_cow_worker():
    if POB_DIR is None:
        pytest.skip("No runnable PoB engine found")
    worker = BridgeWorker(slot_name="test", pob_dir=POB_DIR)
    worker.start()
    if not (supports_cow_snapshot(worker) and supports_direct_calc(worker)):
        worker.stop()
        pytest.skip("Engine does not advertise cowSnapshot/directCalc capabilities")

    lc = worker.call("list_classes", {})
    witch = next(c for c in lc["classes"] if c["name"] == "Witch")
    worker.call("set_class", {"classId": witch["id"]})
    elem = next(a for a in witch["ascendancies"] if a["name"] == "Elementalist")
    worker.call("set_ascendancy", {"ascendClassId": elem["id"]})
    worker.call("add_group", {"label": "Main Fireball"})
    worker.call("add_gem", {"group": 1, "nameSpec": "Fireball", "level": 20, "quality": 20, "enabled": True})
    worker.call("set_build_field", {"field": "mainSocketGroup", "value": 1})
    ring_raw = "\n".join([
        "Rarity: RARE", "Cow Test Ring", "Ruby Ring", "Item Level: 84", "Implicits: 1",
        "+30% to Fire Resistance", "+75 to maximum Life", "+40% to Cold Resistance",
    ])
    worker.call("add_item", {"raw": ring_raw, "slotName": "Ring 1", "equip": True})
    worker.call("set_config", {"var": "usePowerCharges", "value": True})
    worker.update_checkpoint()
    try:
        yield worker
    finally:
        worker.stop()


def test_live_direct_calc_parity_and_skip_ehp(live_cow_worker: BridgeWorker) -> None:
    ref = live_cow_worker.call("get_stats", {})["stats"]
    got = live_cow_worker.call("direct_calc", {})
    got_stats = got["stats"]
    assert isinstance(got.get("skillFlags"), dict)

    for key, _, _ in CORE_STAT_KEYS:
        rv = ref.get(key)
        gv = got_stats.get(key)
        if isinstance(rv, (int, float)) and isinstance(gv, (int, float)) and math.isfinite(rv) and math.isfinite(gv):
            rel = abs(gv - rv) / max(1.0, abs(rv))
            assert rel < 1e-9, f"direct_calc drift on {key}: ref={rv}, got={gv}"

    skip = live_cow_worker.call("direct_calc", {"skipEHP": True})["stats"]
    for key in ("CombinedDPS", "TotalDPS", "Life", "Mana", "FireResist", "ColdResist"):
        rv = ref.get(key)
        sv = skip.get(key)
        if isinstance(rv, (int, float)) and isinstance(sv, (int, float)):
            assert abs(sv - rv) / max(1.0, abs(rv)) < 1e-9


def test_live_cow_snapshot_and_sweep_zero_residue(live_cow_worker: BridgeWorker) -> None:
    before_stats = live_cow_worker.call("get_stats", {})["stats"]
    before_items = live_cow_worker.call("list_items", {})["items"]

    res = run_sweep(
        live_cow_worker,
        [
            {"name": "Add Support", "ops": [{"op": "skill", "group": "main", "addGem": "Controlled Destruction Support"}]},
            {"name": "Edit Ring", "ops": [{"op": "item_edit", "slot": "Ring 1", "addMods": ["+100 to maximum Life"]}]},
            {"name": "Config Frenzy", "ops": [{"op": "config", "set": {"useFrenzyCharges": True}}]},
            {
                "name": "Attr Warning Variant",
                "ops": [{"op": "item", "slot": "Helmet", "raw": "Rarity: NORMAL\nRoyal Burgonet\nRequires: 999 Str"}],
            },
        ],
    )
    assert res["succeeded"] == 4
    assert res["restoreCheck"]["verified"] is True
    assert len(res["variants"][3]["warnings"]) >= 1
    assert len(res["variants"][3]["diffVsBase"]["newWarnings"]) >= 1

    after_stats = live_cow_worker.call("get_stats", {})["stats"]
    after_items = live_cow_worker.call("list_items", {})["items"]
    assert len(after_items) == len(before_items)
    for key in ("CombinedDPS", "TotalEHP", "Life", "FireResist", "ColdResist"):
        assert after_stats.get(key) == pytest.approx(before_stats.get(key), rel=1e-9)


def test_live_nested_cluster_jewel_cow_rollback_zero_drift(live_cow_worker: BridgeWorker) -> None:
    saved_code = live_cow_worker.call("export_code", {})["code"]
    b4_path = BENCH_FIXTURES_DIR / "B4_TimelessCluster.xml"
    try:
        if b4_path.is_file():
            b4_code = xml_to_share_code(b4_path.read_text(encoding="utf-8"))
            live_cow_worker.call("import_code", {"code": b4_code, "mode": "replace"})
        else:
            large_raw = "\n".join([
                "Rarity: RARE", "Kraken Prism", "Large Cluster Jewel", "Item Level: 84", "Implicits: 3",
                "{enchant}Adds 8 Passive Skills", "{enchant}2 Added Passive Skills are Jewel Sockets",
                "{enchant}Added Small Passive Skills grant: 12% increased Damage with Two Handed Weapons",
                "1 Added Passive Skill is Feed the Fury", "1 Added Passive Skill is Fuel the Fight",
                "1 Added Passive Skill is Martial Prowess",
            ])
            med_raw = "\n".join([
                "Rarity: RARE", "Vortex Glimmer", "Medium Cluster Jewel", "Item Level: 84", "Implicits: 3",
                "{enchant}Adds 5 Passive Skills", "{enchant}1 Added Passive Skill is a Jewel Socket",
                "{enchant}Added Small Passive Skills grant: 15% increased Critical Strike Chance",
                "1 Added Passive Skill is Pressure Points", "1 Added Passive Skill is Quick Getaway",
            ])
            run_batch(
                live_cow_worker,
                [
                    {"op": "new_build", "class": "Duelist", "ascendancy": "Slayer", "level": 96},
                    {"op": "tree", "allocate": ["2491"]},
                    {"op": "item", "slot": "Jewel 2491", "raw": large_raw},
                    {"op": "tree", "allocate": ["Feed the Fury", "Fuel the Fight", "Martial Prowess", "46519"]},
                    {"op": "item", "slot": "Jewel 46519", "raw": med_raw},
                    {"op": "tree", "allocate": ["Pressure Points", "Quick Getaway"]},
                    {"op": "add_group", "label": "Main Cyclone", "includeInFullDPS": True, "makeMain": True, "gems": ["Cyclone:20:20"]},
                ],
                apply=True,
                no_undo=True,
            )

        before_digest = capture_digest(live_cow_worker)
        before_pts = before_digest["pointsUsed"]
        before_fdps = float(before_digest["stats"]["FullDPS"])
        if b4_path.is_file():
            assert before_pts == 81
            assert before_fdps == pytest.approx(1870226.10, abs=0.05)

        res = try_item(live_cow_worker, slot_name="Jewel 2491", raw_text="Rarity: NORMAL\nViridian Jewel", apply=False)
        assert res["applied"] is False
        assert int(res["mutation"]["after"]["points"]["used"]) < before_pts

        after_digest = capture_digest(live_cow_worker)
        assert after_digest["pointsUsed"] == before_pts
        assert float(after_digest["stats"]["FullDPS"]) - before_fdps == 0.0
        assert verify_cow_restored(live_cow_worker, before_digest)["verified"] is True
    finally:
        live_cow_worker.call("import_code", {"code": saved_code, "mode": "replace"})
        live_cow_worker.update_checkpoint()


def test_live_sweep_rejects_fourth_suffix_on_3p3s_rare_amulet(live_cow_worker: BridgeWorker) -> None:
    saved_code = live_cow_worker.call("export_code", {})["code"]
    b1_path = BENCH_FIXTURES_DIR / "B1_DirectHit.xml"
    try:
        if b1_path.is_file():
            b1_code = xml_to_share_code(b1_path.read_text(encoding="utf-8"))
            live_cow_worker.call("import_code", {"code": b1_code, "mode": "replace"})
        else:
            live_cow_worker.call("add_item", {"raw": _SOL_TALISMAN_3P3S_RAW, "slotName": "Amulet", "equip": True})

        with pytest.raises(DaemonRequestError, match=r"3/3 suffixes"):
            edit_item_mods(
                live_cow_worker, slot_name="Amulet", add_mods=["+35% to Global Critical Strike Multiplier"], apply=False
            )

        sw = run_sweep(
            live_cow_worker,
            [
                {"name": "Illegal 4th Suffix", "ops": [{"op": "item_edit", "slot": "Amulet", "addMods": ["+35% to Global Critical Strike Multiplier"]}]},
                {"name": "Legal Suffix Swap", "ops": [{"op": "item_edit", "slot": "Amulet", "replaceMods": [["Fire Resistance", "+35% to Cold Resistance"]]}]},
            ],
        )
        assert sw["succeeded"] == 1
        assert sw["failed"] == 1
        assert sw["variants"][0]["ok"] is False
        assert "3/3 suffixes" in sw["variants"][0]["error"]
        assert sw["variants"][1]["ok"] is True
        assert sw["restoreCheck"]["verified"] is True
    finally:
        live_cow_worker.call("import_code", {"code": saved_code, "mode": "replace"})
        live_cow_worker.update_checkpoint()
