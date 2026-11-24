"""CoW snapshot / direct_calc / affix-cap regressions found in PR #37 review (offline fakes + live engine)."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

import pytest
from engine_paths import find_runnable_engine
from test_cow_snapshot_sweep import _SOL_TALISMAN_3P3S_RAW, _CowFakeWorker

from pob_cli import item_inspect
from pob_cli.bridge_client import BridgeError, BridgeWorker
from pob_cli.errors import DaemonRequestError
from pob_cli.mutators import edit_item_mods, run_batch, run_sweep, switch_main
from pob_cli.mutators.batch_ops import ops_need_lossless_snapshot
from pob_cli.snapshot import (
    CORE_STAT_KEYS,
    restore_snapshot,
    run_mutation,
    supports_cow_snapshot,
    supports_direct_calc,
    take_snapshot,
)

POB_DIR = find_runnable_engine()


class _FlakyCowWorker(_CowFakeWorker):
    """Fake whose `cow_restore` can raise or silently leave the build mutated."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.cow_restore_mode = "ok"
        self.last_good_code = self._serialize()

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if method == "import_code" and self.cow_restore_mode == "raise_all":
            raise BridgeError("RPC 'import_code' failed: engine gone")
        if method == "cow_restore" and self.cow_restore_mode != "ok":
            self.calls.append((method, dict(params or {})))
            if self.cow_restore_mode in ("raise", "raise_all"):
                raise BridgeError("RPC 'cow_restore' failed: Stale cow snapshot cow_1: the build was replaced")
            return {"ok": True}
        return super().call(method, params, timeout)


def _bump(w: Any) -> dict[str, Any]:
    w.dps += 15000.0
    return {"bumped": True}


def test_cow_restore_failure_falls_back_to_saved_code_and_reports_it() -> None:
    w = _FlakyCowWorker(caps={"cowSnapshot": True, "directCalc": True})
    orig = w._serialize()
    w.cow_restore_mode = "raise"
    res = run_mutation(w, _bump, apply=False)
    assert w._serialize() == orig
    assert "CoW restore failed" in res["restoreFallback"]
    assert "Stale cow snapshot" in res["restoreFallback"]
    assert [m for m, _ in w.calls].count("import_code") == 1


def test_mutation_error_survives_a_failing_restore() -> None:
    w = _FlakyCowWorker(caps={"cowSnapshot": True})
    w.cow_restore_mode = "raise_all"

    def _boom(worker: Any) -> None:
        raise ValueError("the original mutation error")

    # Both the CoW restore and the fallback import fail: the mutation's own error is still raised.
    with pytest.raises(ValueError, match="the original mutation error") as info:
        run_mutation(w, _boom, apply=False)
    assert any("also failed" in n for n in getattr(info.value, "__notes__", []))


def test_restore_snapshot_without_fallback_after_engine_restart_raises() -> None:
    w = _FlakyCowWorker(caps={"cowSnapshot": True})
    snap = take_snapshot(w)
    snap.fallback_code = None
    w._proc = object()
    with pytest.raises(BridgeError, match="engine restarted"):
        restore_snapshot(w, snap)
    assert all(m != "import_code" for m, _ in w.calls)


def test_sweep_reimports_saved_code_when_cow_restore_drifts() -> None:
    w = _FlakyCowWorker(caps={"cowSnapshot": True, "directCalc": True})
    orig = w._serialize()
    w.cow_restore_mode = "noop"
    res = run_sweep(w, [{"name": "Power", "ops": [{"op": "config", "set": {"usePowerCharges": True}}]}])
    assert res["restoreCheck"]["verified"] is True
    assert "drifted" in res["restoreCheck"]["fallback"]
    assert res["restoreFallbacks"]
    assert w._serialize() == orig


def test_set_switching_ops_take_an_export_checkpoint() -> None:
    assert ops_need_lossless_snapshot([{"op": "new_build"}])
    assert ops_need_lossless_snapshot([{"op": "import-tree-url", "url": "x"}])
    for key in ("spec", "itemSet", "skill_set_id", "configSet", "loadout"):
        assert ops_need_lossless_snapshot([{"op": "switch_main", key: 2}]), key
    assert not ops_need_lossless_snapshot([{"op": "switch_main", "skillPart": 2, "spec": None}])
    assert not ops_need_lossless_snapshot([{"op": "config", "set": {"x": 1}}, "junk"])  # type: ignore[list-item]

    w = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    run_mutation(w, _bump, apply=False, lossless=True)
    methods = [m for m, _ in w.calls]
    assert "cow_snapshot" not in methods
    assert methods.count("export_code") == 1 and methods.count("import_code") == 1


_AFFIX_ENTRIES = [
    {"id": "life", "source": "Explicit", "generation_type": "Prefix", "group": "Life", "affix": "Robust", "level": 30, "stats": ["+(70-79) to maximum Life"]},
    {"id": "fres", "source": "Explicit", "generation_type": "Suffix", "group": "FireRes", "affix": "of the Furnace", "level": 48, "stats": ["+(30-35)% to Fire Resistance"]},
    {"id": "cres", "source": "Explicit", "generation_type": "Suffix", "group": "ColdRes", "affix": "of the Ice", "level": 48, "stats": ["+(30-35)% to Cold Resistance"]},
    {"id": "multi", "source": "Explicit", "generation_type": "Suffix", "group": "CritMulti", "affix": "of Ferocity", "level": 59, "stats": ["+(30-38)% to Global Critical Strike Multiplier"]},
    {"id": "int", "source": "Explicit", "generation_type": "Suffix", "group": "Intelligence", "affix": "of the Virtuoso", "level": 74, "stats": ["+(43-50) to Intelligence"]},
    {"id": "wed", "source": "Explicit", "generation_type": "Prefix", "group": "WED", "affix": "Overpowering", "level": 81, "stats": ["(37-42)% increased Elemental Damage with Attack Skills"]},
    {"id": "mana", "source": "Bench", "generation_type": "Bench Prefix", "group": "ManaCost", "affix": "Upgraded", "level": 1, "stats": ["Non-Channelling Skills have -7 to Total Mana Cost"]},
]
_MAGIC_AMULET_RAW = "\n".join([
    "Rarity: MAGIC", "Robust Turquoise Amulet of the Furnace", "Item Level: 85", "Implicits: 1",
    "+24 to Dexterity and Intelligence", "+78 to maximum Life", "+35% to Fire Resistance",
])


@pytest.fixture
def _affix_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(item_inspect, "_load_entries", lambda *a, **k: (_AFFIX_ENTRIES, True, frozenset({"amulet", "default"})))
    monkeypatch.setattr(item_inspect, "classify_mod_affix_types", lambda lines, **k: {ln: {"suffix"} for ln in lines})


@pytest.mark.usefixtures("_affix_db")
def test_magic_item_edit_enforces_one_prefix_one_suffix() -> None:
    w = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    w.slot_items["Amulet"] = _MAGIC_AMULET_RAW
    with pytest.raises(DaemonRequestError, match=r"Cannot edit Magic item.*1/1 suffixes"):
        edit_item_mods(w, slot_name="Amulet", add_mods=["+34% to Cold Resistance"], apply=False)
    ok = edit_item_mods(w, slot_name="Amulet", replace_mods=[("Fire Resistance", "+34% to Cold Resistance")], apply=False)
    assert ok["applied"] is False


@pytest.mark.usefixtures("_affix_db")
def test_batch_and_sweep_item_edit_accept_allow_illegal_affixes() -> None:
    w = _CowFakeWorker(caps={"cowSnapshot": True, "directCalc": True})
    w.slot_items["Amulet"] = _SOL_TALISMAN_3P3S_RAW
    add = {"op": "item_edit", "slot": "Amulet", "addMods": ["+34% to Cold Resistance"]}
    sw = run_sweep(w, [{"name": "capped", "ops": [add]}, {"name": "forced", "ops": [dict(add, allowIllegalAffixes=True)]}])
    assert sw["variants"][0]["ok"] is False and "3/3 suffixes" in sw["variants"][0]["error"]
    assert sw["variants"][1]["ok"] is True
    res = run_batch(w, [dict(add, allow_illegal_affixes=True)])
    assert res["applied"] is False


def test_item_edit_turns_game_data_errors_into_a_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(*a: Any, **k: Any) -> dict[str, Any]:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(item_inspect, "inspect_item", _broken)
    w = _CowFakeWorker(caps={})
    w.slot_items["Amulet"] = _SOL_TALISMAN_3P3S_RAW
    with pytest.raises(DaemonRequestError, match=r"(?s)could not be read.*allowIllegalAffixes"):
        edit_item_mods(w, slot_name="Amulet", add_mods=["+34% to Cold Resistance"], apply=False)


# ---------------------------------------------------------------------------
# Live engine
# ---------------------------------------------------------------------------

_BREAKDOWN_ONLY = re.compile(r"Flask|Tincture")


@pytest.fixture(scope="module")
def live_sets_worker():
    if POB_DIR is None:
        pytest.skip("No runnable PoB engine found")
    worker = BridgeWorker(slot_name="test-cow-review", pob_dir=POB_DIR)
    try:
        worker.start()
        if not (supports_cow_snapshot(worker) and supports_direct_calc(worker)):
            pytest.skip("Engine does not advertise cowSnapshot/directCalc capabilities")
        worker.call("add_group", {"label": "Molten"})
        worker.call("add_gem", {"group": 1, "nameSpec": "Molten Strike", "level": 20, "quality": 0, "enabled": True})
        worker.call("add_group", {"label": "Caster"})
        worker.call("add_gem", {"group": 2, "nameSpec": "Fireball", "level": 20, "quality": 0, "enabled": True})
        worker.call("add_group", {"label": "Auras"})
        for aura in ("Hatred", "Anger", "Wrath"):
            worker.call("add_gem", {"group": 3, "nameSpec": aura, "level": 1, "quality": 0, "enabled": True})
        worker.call("set_build_field", {"field": "mainSocketGroup", "value": 2})
        ring = "\n".join(["Rarity: RARE", "Cow Ring", "Ruby Ring", "Item Level: 84", "Implicits: 1", "+30% to Fire Resistance", "+75 to maximum Life"])
        worker.call("add_item", {"raw": ring, "slotName": "Ring 1", "equip": True})
        worker.call("spec_op", {"op": "copy", "index": 1, "title": "Tree 2"})
        worker.call("set_active_spec", {"index": 1})
        worker.call("new_item_set", {"title": "Items 2", "copyCurrent": True})
        worker.call("set_item_set", {"id": 1})
        worker.call("new_skill_set", {"title": "Skills 2"})
        worker.call("add_group", {"label": "Set 2 group"})
        worker.call("add_gem", {"group": 1, "nameSpec": "Fireball", "level": 20, "quality": 0, "enabled": True})
        worker.call("set_skill_set", {"id": 1})
        worker.call("new_config_set", {"title": "Config 2", "copyCurrent": True})
        worker.call("set_config_set", {"id": 1})
        # One import round-trip so later exports compare byte for byte.
        worker.call("import_code", {"code": worker.call("export_code", {})["code"], "mode": "replace"})
        worker.update_checkpoint()
        yield worker
    finally:
        worker.stop()


def _export(w: BridgeWorker) -> str:
    return str(w.call("export_code", {})["code"])


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "switch_main", "spec": 2}, {"op": "switch_main", "itemSet": 2},
         {"op": "item", "slot": "Ring 1", "raw": "Rarity: NORMAL\nIron Ring"}],
        [{"op": "switch_main", "skillSet": 2}, {"op": "skill_add", "group": 1, "gem": "Faster Casting Support"}],
        [{"op": "switch_main", "configSet": 2}, {"op": "config", "set": {"usePowerCharges": True}}],
    ],
    ids=["spec+item-set", "skill-set", "config-set"],
)
def test_live_set_switch_what_if_leaves_every_set_unchanged(live_sets_worker: BridgeWorker, ops: list[dict[str, Any]]) -> None:
    before = _export(live_sets_worker)
    res = run_batch(live_sets_worker, ops)
    assert res["applied"] is False
    assert _export(live_sets_worker) == before


def test_live_stale_snapshot_is_refused_and_falls_back(live_sets_worker: BridgeWorker) -> None:
    before = _export(live_sets_worker)

    def _switch_and_edit(w: BridgeWorker) -> None:
        w.call("set_item_set", {"id": 2})
        w.call("add_item", {"raw": "Rarity: NORMAL\nIron Ring", "slotName": "Ring 1", "equip": True})

    # Called without lossless=True on purpose: Lua must refuse the stale CoW restore.
    res = run_mutation(live_sets_worker, _switch_and_edit, apply=False)
    assert "Stale cow snapshot" in res["restoreFallback"]
    assert _export(live_sets_worker) == before

    snap = take_snapshot(live_sets_worker)
    live_sets_worker.call("new_build", {})
    with pytest.raises(BridgeError, match=r"Stale cow snapshot.*build was replaced"):
        live_sets_worker.call("cow_restore", {"snapId": snap.token})
    note = restore_snapshot(live_sets_worker, snap, drop=True)
    assert note and "Unknown cow snapshot" in note
    assert _export(live_sets_worker) == before


def test_live_skill_part_what_if_does_not_leak(live_sets_worker: BridgeWorker) -> None:
    before = _export(live_sets_worker)
    switch_main(live_sets_worker, skill_group=1, skill_part_index=2, apply=False)
    assert _export(live_sets_worker) == before

    sw = run_sweep(
        live_sets_worker,
        [{"name": "part 2", "ops": [{"op": "switch_main", "skillGroup": 1, "skillPart": 2}]}, {"name": "base", "ops": []}],
    )
    assert sw["succeeded"] == 2
    assert sw["variants"][1]["diffVsBase"]["deltas"] == []
    assert _export(live_sets_worker) == before


def test_live_snapshot_table_is_capped(live_sets_worker: BridgeWorker) -> None:
    ids = [live_sets_worker.call("cow_snapshot", {})["snapId"] for _ in range(20)]
    with pytest.raises(BridgeError, match="Unknown cow snapshot"):
        live_sets_worker.call("cow_restore", {"snapId": ids[0]})
    live_sets_worker.call("cow_restore", {"snapId": ids[-1], "drop": True})
    for sid in ids:
        live_sets_worker.call("cow_drop", {"snapId": sid})


def test_live_direct_calc_matches_main_warnings_points_and_keys(live_sets_worker: BridgeWorker) -> None:
    w = live_sets_worker
    sidebar_before = [x.get("text") for x in w.call("get_sidebar", {}).get("warnings", [])]
    ref = w.call("get_stats", {})["stats"]
    info_pts = w.call("get_build_info", {})["points"]
    got = w.call("direct_calc", {})
    assert any("You do not have enough Mana" in s for s in sidebar_before)
    assert set(got["warnings"]) == set(sidebar_before)
    for key in ("ManaCostWarning", "ManaCost", "ManaUnreserved", "LifeCostWarning"):
        assert got["stats"].get(key) == ref.get(key), key
    assert (got["points"]["usedMax"], got["points"]["ascMax"]) == (info_pts["usedMax"], info_pts["ascMax"])
    # direct_calc borrows the sidebar controls; they must describe the session build again afterwards.
    assert [x.get("text") for x in w.call("get_sidebar", {}).get("warnings", [])] == sidebar_before

    missing = {k for k in set(ref) - set(got["stats"]) if not _BREAKDOWN_ONLY.search(k)}
    assert not missing, f"get_stats keys missing from direct_calc: {sorted(missing)[:10]}"
    assert "TotalEHP" in got["stats"]
    assert "TotalEHP" not in w.call("direct_calc", {"skipEHP": True})["stats"]
    for key, _, _ in CORE_STAT_KEYS:
        assert (key in ref) == (key in got["stats"]), key

    sw = run_sweep(w, [{"name": "noop", "ops": []}])
    assert any("You do not have enough Mana" in s for s in sw["baseline"]["warnings"])
    assert sw["variants"][0]["diffVsBase"]["newWarnings"] == []
    assert sw["variants"][0]["diffVsBase"]["resolvedWarnings"] == []
